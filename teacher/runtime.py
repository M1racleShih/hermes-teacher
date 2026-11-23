"""Teacher's client of the profile gateway. The owner is the sole Hermes session writer."""

from __future__ import annotations

import asyncio
import uuid

from .i18n import get_language, localized, tr
from .learning import Learning
from .library import file_lease
from .models import ModelSettings, native_model
from .profile import activate
from .records import Records
from .session_links import lesson_for_session


HANDOFF_PREFIX = "[Confirmed lesson handoff]\n"
LEGACY_HANDOFF_PREFIX = "〔已确认的续课交接〕\n"
GOAL_CONTINUATION_PREFIX = "[Goal continuation request]\n"
LEGACY_GOAL_CONTINUATION_PREFIX = "〔目标模式的继续请求〕\n"
LITERAL_INPUT_PREFIX = "[Classroom input]\n"
LEGACY_LITERAL_INPUT_PREFIX = "〔课堂原文〕\n"


def handoff_message(body, *, language=None):
    return tr(
        HANDOFF_PREFIX + "Briefly resume teaching from the teacher's handoff notes.\n",
        LEGACY_HANDOFF_PREFIX + "请按教师续课说明简短衔接下一步。\n",
        language=language,
    ) + body


def feedback_confirmation(learning, lesson_id, question_id):
    event_id = "teacher-confirm-" + question_id
    language = learning.request_language(lesson_id, event_id)
    return tr("continue", "继续", language=language), event_id


class SettledTurnError(RuntimeError):
    """The authority has recorded a final failure or cancellation for this input."""


class Tutor:
    def __init__(self, profile, *, activate_profile=True):
        if activate_profile:
            self.profile = activate(profile)
        else:
            from pathlib import Path

            from hermes_constants import get_hermes_home

            self.profile = Path(profile).resolve()
            if self.profile != get_hermes_home().resolve():
                raise ValueError(tr("This lesson belongs to another Hermes profile", "课堂不属于当前 Hermes profile"))
        self.records = Records(self.profile)
        self.learning = Learning(self.profile, incoming_handoff=self.records.incoming_handoff)

    @localized
    def lesson(self, lesson_id):
        row = self.records.get(lesson_id)
        if not row:
            raise ValueError(tr("Lesson not found", "课堂不存在"))
        return row

    def route(self, lesson_id):
        state = self.learning.state(lesson_id)
        if state["model"]:
            canonical = state.get("canonical")
            current = native_model(self.profile, canonical) if canonical else {}
            return {**state["model"], **current}
        choice = ModelSettings(self.profile).lesson()
        self.learning.update(lesson_id, model=choice)
        return choice

    def seed_route(self, lesson_id, source_id=None):
        if source_id and self.learning.state(lesson_id)["model"] is None:
            route = self.model(source_id)
            selected = ModelSettings(self.profile).lesson(
                route.get("model"), route.get("provider"), route.get("reasoning_effort")
            )
            self.learning.update(lesson_id, model=selected)
            return selected
        return self.route(lesson_id)

    def handoff_progress(self, lesson_id):
        progress = self.learning.handoff_progress(lesson_id)
        return {**progress, "model": self.model(lesson_id)}

    async def _session(self, client, lesson_id):
        with self.records.lease(lesson_id):
            row = self.lesson(lesson_id)
            current = self.learning.state(lesson_id).get("canonical")
            if not current:
                route = self.route(lesson_id)
                policy = dict(source="cli", cwd=row["vault"], ignore_rules=True, toolsets=["teacher"])
                for key in ("provider", "model"):
                    if route.get(key):
                        policy[key] = route[key]
                if route.get("reasoning_effort"):
                    policy["reasoning"] = route["reasoning_effort"]
                # Stable identity recovers a lost creation reply, even after gateway restart.
                created = await client.rpc("session.create", request_id="teacher-" + lesson_id, **policy)
                current = created["session_id"]
                linked = self.records.by_session(current)
                if linked is None:
                    self.records.bind_session(lesson_id, current, parent_id=row["session_id"])
                elif linked["id"] != lesson_id:
                    raise ValueError(tr("The Hermes session is linked to another lesson", "Hermes 会话已关联其他课堂"))
                self.learning.update(lesson_id, canonical=current, workspace=row["vault"])
            else:
                lesson_for_session(self.records, row["session_id"], follow=True)
                if self.learning.state(lesson_id).get("workspace") != row["vault"]:
                    from hermes_cli.gateway_mutations import PreparedMutations

                    await PreparedMutations().apply(client, current, "workspace", {"cwd": row["vault"]})
                    self.learning.update(lesson_id, workspace=row["vault"])
        return current

    @localized
    async def open(self, lesson_id):
        from hermes_cli.gateway_client import connect_gateway

        async with connect_gateway() as client:
            return await self._session(client, lesson_id)

    @staticmethod
    async def _receipt(client, session_id, admission):
        while True:
            receipt = await client.rpc(
                "prompt.receipt", session_id=session_id, admission_id=admission, include_result=True
            )
            if receipt["status"] == "terminal":
                result = receipt.get("result") or {}
                if result.get("failed") or receipt.get("outcome") in ("failed", "interrupted", "cancelled"):
                    reason = result.get("turn_exit_reason") or receipt.get("outcome")
                    raise SettledTurnError(
                        tr(f"Hermes turn ended early ({reason}). ", f"Hermes 回合未完成（{reason}）。")
                        + str(result.get("final_response") or "")
                    )
                return result
            if receipt["status"] == "unknown":
                raise RuntimeError(tr(
                    f"Check receipt {admission} in the original Hermes session to confirm this input.",
                    f"网关需要核对此次输入，请在原会话查看回执 {admission}",
                ))
            # The event stream wakes active clients. The receipt identifies the exact submission
            # even if another viewer acts or if we connect after completion.
            try:
                while True:
                    frame = await asyncio.wait_for(client.events.get(), timeout=3.0)
                    if isinstance(frame, Exception):
                        raise frame
                    params = frame.get("params", {})
                    if params.get("session_id") != session_id:
                        continue
                    kind = params.get("type")
                    if kind in {"approval.request", "clarify.request"}:
                        raise RuntimeError(tr(
                            f"Open the original Hermes session {session_id} to respond to {kind}.",
                            f"原会话需要处理 {kind}，请在 Hermes 中打开 {session_id}。",
                        ))
                    if kind in {"message.complete", "session.replay_gap"}:
                        break
            except asyncio.TimeoutError:
                pass

    async def _submit(self, lesson_id, text, event_id, attachments, on_admission=None):
        from hermes_cli.gateway_client import connect_gateway

        async with connect_gateway() as client:
            session_id = await self._session(client, lesson_id)
            await client.rpc("session.resume", session_id=session_id)
            media = []
            for part in attachments or []:
                if part.get("type") == "text":
                    text += "\n\n" + part["text"]
                elif part.get("type") == "image_url":
                    head, sep, data = part["image_url"]["url"].partition(";base64,")
                    if not sep or not head.startswith("data:image/"):
                        raise ValueError(tr("Provide a received local image snapshot", "请提供已接收的本地图像快照"))
                    staged = await client.rpc(
                        "image.attach_bytes",
                        session_id=session_id,
                        content_base64=data,
                        filename="image." + head.split("/")[-1],
                    )
                    media.append({"path": staged["path"], "mime": staged["mime"]})
                else:
                    raise ValueError(tr("Unknown attachment type", "未知附件类型"))
            params = dict(session_id=session_id, input_id=event_id, text=text, finite=True)
            if media:
                params["attachments"] = media
            receipt = await client.rpc("prompt.submit", **params)
            if on_admission:
                on_admission(session_id, receipt["admission_id"])
            return await self._receipt(client, session_id, receipt["admission_id"])

    @localized
    async def recover_turn(self, session_id, admission):
        from hermes_cli.gateway_client import connect_gateway

        async with connect_gateway() as client:
            result = await self._receipt(client, session_id, admission)
            if not result:
                raise RuntimeError(tr(
                    "The native receipt contains no reply. Check the original lesson.",
                    "原生回执没有保存回合正文，请核对原课堂。",
                ))
            return result

    @localized
    async def interrupt(self, lesson_id):
        from hermes_cli.gateway_client import connect_gateway

        session_id = self.learning.state(lesson_id).get("canonical")
        if not session_id:
            return tr("This lesson has no running turn.", "本课尚无运行中的回合。")
        async with connect_gateway() as client:
            state = await client.rpc("session.resume", session_id=session_id, omit_messages=True)
            if not state["running"]:
                return tr("This lesson has no running turn.", "本课当前没有运行中的回合。")
            await client.rpc("session.interrupt", session_id=session_id,
                             execution_generation=state["execution_generation"])
        return tr("Requested that the current turn stop.", "已请求停止本课当前回合。")

    @localized
    def turn(
        self, lesson_id, text, *, event_id=None, attachments=None, expected_id=None,
        generated=False, on_admission=None,
    ):
        self.lesson(lesson_id)
        marks_input = generated or expected_id or text.lstrip().startswith("/")
        language = (
            self.learning.request_language(lesson_id, event_id)
            if event_id and marks_input else get_language()
        )
        if generated:
            if self.learning.pending(lesson_id):
                raise ValueError(tr("The classroom is waiting for the learner's answer", "课堂正在等待学习者作答"))
            text = tr(GOAL_CONTINUATION_PREFIX, LEGACY_GOAL_CONTINUATION_PREFIX, language=language) + text
        if expected_id:
            text = tr(
                f"[Classroom answer {expected_id}]\n", f"〔课堂作答 {expected_id}〕\n", language=language,
            ) + text
        elif text.lstrip().startswith("/"):
            text = tr(LITERAL_INPUT_PREFIX, LEGACY_LITERAL_INPUT_PREFIX, language=language) + text
        key = event_id or uuid.uuid4().hex
        with file_lease(self.profile / "teacher", f"requests/{lesson_id}.lock"):
            result = asyncio.run(self._submit(lesson_id, text, key, attachments, on_admission))
        self.last_result = result
        return result.get("final_response") or tr("Hermes returned no text for this turn.", "Hermes 本轮没有返回正文。")

    async def _mutate(self, lesson_id, op, payload):
        from hermes_cli.gateway_client import connect_gateway
        from hermes_cli.gateway_mutations import PreparedMutations

        async with connect_gateway() as client:
            sid = await self._session(client, lesson_id)
            return await PreparedMutations().apply(client, sid, op, payload)

    @localized
    def compact(self, lesson_id, focus=""):
        from hermes_cli.gateway_mutations import compress_payload

        result = asyncio.run(self._mutate(lesson_id, "compress", compress_payload(focus)))
        if result.get("lines"):
            return "\n".join(result["lines"])
        return str(result.get("message") or result.get("status") or tr("Compaction completed", "压缩已完成"))

    def rename(self, lesson_id, title):
        return asyncio.run(self._mutate(lesson_id, "rename", {"title": title}))

    async def _native_model_settings(self, lesson_id):
        from hermes_cli.gateway_client import connect_gateway

        sid = self.learning.state(lesson_id)["canonical"]
        async with connect_gateway() as client:
            await client.rpc("session.resume", session_id=sid)
            info = await client.rpc("session.info", session_id=sid)
        return {key: info[key] for key in ("model", "provider", "reasoning_effort") if info.get(key)}

    def model(self, lesson_id, model=None, provider=None, effort=None):
        route = self.route(lesson_id)
        canonical = self.learning.state(lesson_id).get("canonical")
        if canonical and (self.profile / "state.db").is_file():
            route.update(asyncio.run(self._native_model_settings(lesson_id)))
        if not any([model, provider, effort]):
            return route
        updated = ModelSettings(self.profile).lesson(
            model or route.get("model"),
            provider or route.get("provider"),
            effort or route.get("reasoning_effort"),
        )
        if canonical:
            if model or provider:
                payload = {k: updated[k] for k in ("model", "provider")}
                if effort:
                    payload["reasoning"] = effort
                asyncio.run(self._mutate(lesson_id, "model", payload))
            elif effort:
                asyncio.run(self._mutate(lesson_id, "reasoning", {"effort": effort}))
            updated.update(asyncio.run(self._native_model_settings(lesson_id)))
        self.learning.update(lesson_id, model=updated)
        return updated

    def organize(self, lesson_id):
        async def submit():
            from hermes_cli.gateway_client import connect_gateway

            async with connect_gateway() as client:
                sid = await self._session(client, lesson_id)
                await client.rpc("session.resume", session_id=sid)
                reply = await client.rpc(
                    "slash.exec", session_id=sid, command="teacher organize", _timeout=300
                )
                return reply.get("output") or str(reply)

        return asyncio.run(submit())

"""Classroom operations requested by the Hermes Desktop renderer."""

import asyncio
import json
from pathlib import Path

from .i18n import get_language, localized, tr
from .library import management_root, vault_directory
from .models import read_config
from .navigation import Navigator
from .roles import Roles
from .runtime import Tutor, handoff_message
from .session_links import lesson_for_session


class DesktopClassroom:
    def __init__(self, profile, session_id):
        self.profile = Path(profile)
        self.tutor = Tutor(profile, activate_profile=False)
        self.records = self.tutor.records
        self.lesson = lesson_for_session(self.records, session_id, follow=True)
        self.session_id = self.lesson["session_id"] if self.lesson else session_id

    def _vault(self, override):
        if self.lesson:
            return vault_directory(self.lesson["vault"])
        default = read_config(self.tutor.profile / "config.yaml")["teacher"]["vault"]
        return vault_directory(override or default)

    def _nav(self, vault):
        nav = Navigator(self.tutor, vault)
        nav.lesson = self.lesson
        return nav

    async def _destination(self, lesson, *, draft="", send="", handoff=False):
        key = lesson["id"]
        if handoff:
            self.tutor.learning.seed_handoff(key)
        session = await self.tutor.open(key)
        result = {"session": session, "topic": lesson["topic"], "draft": draft, "send": send}
        if handoff:
            result["handoff"] = f"handoff-{key}"
            if not self.records.has_learner_input(key):
                language = self.tutor.learning.request_language(key, result["handoff"])
                result["kickoff"] = handoff_message(lesson["handoff"], language=language)
        return result

    @localized
    async def request(self, raw):
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or any(not isinstance(v, str) for v in payload.values()):
                raise ValueError(tr("Invalid classroom request", "课堂请求无效"))
            result = await self.handle(**payload)
        except (OSError, KeyError, ValueError, RuntimeError, TypeError) as error:
            result = {"error": str(error)}
        return json.dumps({**result, "language": get_language()}, ensure_ascii=False)

    async def handle(self, *, action, argument="", vault=""):
        current = self._vault(vault)
        if action == "state":
            return {
                "lesson": {**{k: self.lesson[k] for k in ("id", "topic", "recording", "record")},
                           "handoff": bool(self.lesson["handoff"])}
                if self.lesson else None,
                "vault": str(current),
            }
        root = management_root(current)
        if action == "list":
            return {"vault": str(root), "lessons": [
                {**{k: row[k] for k in ("id", "topic", "temporary")}, "preview": row["preview"][:160]}
                for row in self.records.search(root, argument)[:40]
            ]}
        if action == "browse":
            folder, children = await asyncio.to_thread(self._nav(current).browse, argument)
            return {
                "directory": str(folder), "parent": str(folder.parent) if folder != folder.parent else "",
                "directories": [{"path": str(child), "name": child.name} for child in children],
            }
        if action == "prose" and self.lesson and self.tutor.learning.pending(self.lesson["id"]):
            return {"pass": True}
        if action in {
            "topic", "new", "open", "resume", "temporary", "lobby", "workspace", "workspace-create", "vaults", "prose"
        }:
            nav = self._nav(current)
            if action in {"workspace", "workspace-create"}:
                if not argument.strip():
                    raise ValueError(tr("Enter a vault path", "请填写资料库路径"))
                await asyncio.to_thread(nav.switch, argument, create=action == "workspace-create")
                if nav.lesson:
                    return {"message": tr(f"Current vault: {nav.vault}", f"当前资料库：{nav.vault}")}
                return {"lobby": str(nav.vault)}
            if action == "vaults":
                return {"vaults": nav.action("vaults", "").splitlines()}
            if action == "lobby":
                return {"lobby": nav.action("lobby", "")}
            if action == "prose":
                handled = await asyncio.to_thread(nav.prose, argument)
                if not nav.lesson:
                    return {"lobby": str(nav.vault)}
                if self.lesson and nav.lesson["id"] == self.lesson["id"]:
                    return {"pass": not handled or bool(nav.draft)}
                return await self._destination(nav.lesson, draft=nav.draft, send="" if handled else argument)
            if action == "open" and not any(row["id"] == argument for row in self.records.recent(root)):
                raise ValueError(tr(
                    "This lesson is outside the current vault. Choose another lesson", "课堂不在当前资料库，请重新选择",
                ))
            if action == "temporary":
                nav.vault = root
                picked = await asyncio.to_thread(nav.choose, new=True, temporary=True)
            else:
                if action == "new" and not argument:
                    if not self.lesson or self.lesson["temporary"]:
                        raise ValueError(tr("Enter a topic for the new lesson", "请填写新课主题"))
                    argument = self.lesson["topic"]
                picked = await asyncio.to_thread(nav.action, action, argument)
            return await self._destination(picked)
        if not self.lesson:
            raise ValueError(tr("Open a lesson first", "请先打开一节课堂"))
        key = self.lesson["id"]
        roles = Roles(self.tutor.profile, records=self.records)
        if action == "next":
            progress = await asyncio.to_thread(self.tutor.handoff_progress, key)
            target = self.records.handoff(
                key, confirmed=True, busy=roles.busy(key), session_id=self.session_id,
                capture_progress=lambda: progress,
            )
            return await self._destination(target, handoff=True)
        if action == "next-retry":
            if not self.records.incoming_handoff(key):
                raise ValueError(tr("There is no pending handoff for this lesson", "当前课堂没有待传递的续课说明"))
            return await self._destination(self.lesson, handoff=True)
        if action in {"seal", "organize"}:
            result = self.records.seal(key, busy=roles.busy(key))
            if action == "organize":
                from .organizer import Organizer
                from .organizer_plan import NativeOrganizer

                organizer = Organizer(self.records)
                organized = await asyncio.to_thread(
                    organizer.organize, current, NativeOrganizer(self.tutor.profile).complete, topic=self.lesson["topic"]
                )
                return {"message": tr("Lesson notes organized.", "课堂笔记整理完成。"), "organized": organized}
            if result["status"] == "sealed":
                message = tr(
                    f"Sealed the lesson notes at {result['file']}. You can continue this lesson.",
                    f"本次学习笔记已封存：{result['file']}。当前课堂仍可继续。",
                )
            elif result["status"] == "disabled":
                message = tr("Recording is off. You can continue this lesson.", "记录已关闭。当前课堂仍可继续。")
            else:
                message = tr("There are no new notes to seal. You can continue this lesson.", "没有新的学习内容可封存。当前课堂仍可继续。")
            return {"message": message}
        if action == "checkpoint":
            return {"message": self.records.read_checkpoint(key, session_id=self.session_id)["text"]}
        if action in {"record-on", "record-off"}:
            enabled = action == "record-on"
            self.records.set_recording(key, enabled)
            return {"message": (
                tr("Lesson recording resumed.", "已恢复课堂记录。") if enabled
                else tr("Lesson recording stopped.", "已停止课堂记录。")
            )}
        if action == "record-bind":
            if not argument.strip():
                raise ValueError(tr(
                    "Enter the path to an existing Markdown export file in the vault",
                    "请输入资料库内专用于导出的现有 Markdown 路径",
                ))
            note = self.records.bind_record(key, argument, session_id=self.session_id)
            return {"message": tr(f"Linked and backfilled the notes: {note['record']}", f"已绑定并回填笔记：{note['record']}")}
        if action == "roles":
            return {"roles": roles.list_for_lesson(key)[:20]}
        if action == "role":
            op, _, rest = argument.strip().partition(" ")
            which, _, task = rest.strip().partition(" ")
            if op == "start":
                job = roles.dispatch(key, op, role=which, task=task)
            elif op in {"status", "resume", "cancel", "supplement"}:
                job = roles.dispatch(key, op, task_id=which, task=task)
            else:
                raise ValueError(tr("Enter a role action, role name, or task ID", "请输入角色操作、角色名或任务 ID"))
            return {"role": job, "message": f"{job['role']} · {job['status']} · {job['id']}"}
        raise ValueError(tr("Unknown classroom action", "未知课堂操作"))

"""Bind the classroom to Hermes' authenticated session and turn metadata."""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
import re

from .extensions import Extensions
from .i18n import get_language, localized, tr, use_profile
from .learning import Learning
from .library import list_materials, material_page
from .models import ModelSettings, model_allowed, native_model
from .questions import make_question
from .records import Records
from .role_notice import read_envelope, resume_offers
from .roles import Roles
from .runtime import (
    GOAL_CONTINUATION_PREFIX, HANDOFF_PREFIX, LEGACY_GOAL_CONTINUATION_PREFIX,
    LEGACY_HANDOFF_PREFIX, LEGACY_LITERAL_INPUT_PREFIX, LITERAL_INPUT_PREFIX,
)
from .session_links import assistant_text, lesson_for_session, native_compression_path, native_messages, typed_text

ROOT = Path(__file__).resolve().parents[1]
TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
EDGES = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"prerequisite": TEXT, "dependent": TEXT},
        "required": ["prerequisite", "dependent"],
        "additionalProperties": False,
    },
}


def choices(*names):
    return {"type": "string", "enum": list(names)}


def tool(description, fields, required):
    return dict(
        description=description,
        parameters=dict(type="object", properties=fields, required=required, additionalProperties=False),
    )


TOOLS = {
    "teacher_quiz": tool(
        "Check one objective. Phrase it in the teaching language and include any explicit language request. "
        "During assessment, use strand for the current prerequisite strand and strands to list them all. "
        "For a follow-up, use follows to reference a missed or unknown question in the same strand. "
        "Choices are shuffled by default. If order matters, set shuffle=false and specify the order in objective. "
        "The classroom displays the question automatically. End the turn after calling this tool.",
        dict(
            objective=TEXT, mode=choices("single", "multi"), intro=TEXT, strand=TEXT, strands=STRINGS,
            follows=TEXT, shuffle={"type": "boolean"},
        ),
        ["objective", "mode"],
    ),
    "teacher_ask": tool(
        "Ask the learner about preferences or direction. The classroom displays the question. End the turn. "
        "During setup, use purpose=goal and mode=text to request the learner's goal in their own words.",
        dict(
            prompt=TEXT, mode=choices("text", "single", "multi"), options=STRINGS,
            purpose=choices("preference", "goal"),
        ),
        ["prompt", "mode"],
    ),
    "teacher_plan": tool(
        "Submit ordered learning nodes and direct dependencies by node name. Several prerequisites may "
        "lead to the same dependent node. The final node is the learning goal. Explain scope and sequence "
        "and cite a completed research task ID for this lesson. Wait for the learner's approval before teaching.",
        dict(nodes=STRINGS, edges=EDGES, rationale=TEXT, research_id=TEXT),
        ["nodes", "edges", "rationale", "research_id"],
    ),
    "teacher_material": tool(
        "List the current vault's learning materials, or read a UTF-8 file by vault-relative path. "
        "Read line_count lines (default 200) from start_line (1-based). Pass back the next fields to continue.",
        dict(
            path=TEXT, start_line={"type": "integer", "minimum": 1},
            line_count={"type": "integer", "minimum": 1, "maximum": 500},
            start_byte={"type": "integer", "minimum": 0},
        ),
        [],
    ),
    "teacher_role": tool(
        "Run research or create a diagram for this lesson. Include the teaching language in the task. "
        "start returns a task ID. The completed result comes back to this classroom. "
        "End the turn to wait for it, or use wait or status to retrieve it.",
        dict(
            action=choices("start", "wait", "status", "supplement", "resume", "cancel"),
            role=choices("researcher", "mermaid", "svg"),
            task=TEXT,
            task_id=TEXT,
        ),
        ["action"],
    ),
    "teacher_checkpoint": tool(
        "Save a verified lesson handoff. Use actual learner responses to summarize coverage, evidence of "
        "understanding, gaps, and next steps. Call this tool on its own and acknowledge it briefly. "
        "Save again after additional tool use or substantive teaching.",
        dict(covered=TEXT, evidence=TEXT, gaps=TEXT, next_step=TEXT),
        ["covered", "evidence", "gaps", "next_step"],
    ),
}

EXTENSION_TOOLS = {
    "teacher_todo": ("todo", tool(
        "Read or update this session branch's task list. Use text for add and the current 1-based "
        "number for done or remove.",
        dict(action=choices("list", "add", "done", "remove"), text=TEXT, number={"type": "integer", "minimum": 1}),
        ["action"],
    )),
    "teacher_find": ("search", tool(
        "Find classroom vault files by name or path text. Returns vault-relative paths.",
        dict(query=TEXT), ["query"],
    )),
    "teacher_grep": ("search", tool(
        "Find lines containing text in the classroom vault. Returns relative paths, line numbers, and excerpts.",
        dict(query=TEXT), ["query"],
    )),
    "teacher_goal": ("goal", tool(
        "Report the status of a learner-initiated goal. Use active_goal.id from the classroom state "
        "and choose complete, blocked, or wait based on verified progress.",
        dict(action=choices("complete", "blocked", "wait"), goal_id=TEXT, reason=TEXT),
        ["action", "goal_id", "reason"],
    )),
}


def classroom_tools(flags):
    return {
        **TOOLS,
        **{name: schema for name, (flag, schema) in EXTENSION_TOOLS.items() if flag in flags},
    }


def apply_tool_limits(tutor, lesson_id, included, excluded, disabled):
    enabled = set(classroom_tools(Extensions(tutor.profile, records=tutor.records).flags))
    requested = {name.strip() for value in included or () for name in value.split(",") if name.strip()}
    if included and (not requested or requested - enabled):
        raise ValueError(tr(
            "Invalid classroom tool list. Choose from: " + ", ".join(sorted(enabled)),
            "课堂工具名单无效，请使用已启用的工具名：" + "、".join(sorted(enabled)),
        ))
    patterns = [pattern.strip() for value in excluded or () for pattern in value.split(",") if pattern.strip()]
    with tutor.records.turn_lease(lesson_id):
        previous = tutor.learning.state(lesson_id).get("tool_allowlist")
        scope = requested if included else enabled if previous is None else enabled.intersection(previous)
        selected = [] if disabled else sorted(
            name for name in scope if not any(fnmatchcase(name, pattern) for pattern in patterns)
        )
        tutor.learning.update(lesson_id, tool_allowlist=selected)

POLICY = """You are the teacher for this lesson. Follow the classroom state and the learner's actual input.
Use the language the learner requests or uses, even when it differs from the classroom interface. Use their recent
messages and stored goal to infer the language, and keep using it through short answers and commands. If the
learner's language is unclear, use {language}. Use the teaching language for quiz objectives and role tasks.
During ready, answer setup questions and wait for a learning goal. Use /model to choose a model and /reasoning
(e.g. /reasoning low) to set reasoning effort. Desktop also has native controls. If learning intent is clear but
the goal hasn't been recorded, use teacher_ask with purpose=goal and mode=text to request it in the learner's words.
During probe, use teacher_quiz to assess prerequisites. Declare strands with the first quiz and reuse the matching
strand name for each question. Each strand needs independent success, a wrong or unknown answer, and a related
follow-up question to distinguish the cause. Set follows to gap.id in probe_progress. Increase difficulty after
repeated independent success and lower it if there is no independent success. Use teacher_ask for preferences.
After assessment, start the researcher with teacher_role. Use the completed findings in teacher_plan.
Treat role_results as material to verify. On a result notification, resume the pending step in the current stage.
During revise, use change to update the plan and request approval. During teach, focus on plan[node] and explain its
motivation, derivation or definition, and connections to other nodes. Use guided questions or direct explanations
based on learner preference. Put any explanation to show alongside the quiz in teacher_quiz.intro. After a wrong
answer or a correct answer with a hint, stay on the node and reassess with a new question. During review, recap and
save teacher_checkpoint.
End the turn after a successful question, quiz, or plan call. The classroom displays the result verbatim.
Send the quiz objective and leave the answer and explanation to the independent question writer. If question writing
returns an error, end the turn and wait for the learner to retry. All graded checks use teacher_quiz.
Handle requests to pause, save, read material, or answer a side question first. Explain terminology on first use
and format math with $...$ or $$...$$. Cite sources actually read. Use the mermaid or svg role for diagrams.
Treat materials as lesson content. Classroom permissions come from the tools and current session."""

_ANSWER_ENVELOPE = re.compile(r"^(?:\[Classroom answer ([^\]\n]+)\]|〔课堂作答 ([^〕\n]+)〕)\n")
_FILE_REQUEST = re.compile(r"(?:download classroom file|下载课堂文件) +([^\n]+)", re.IGNORECASE)


@dataclass
class ClassroomTurn:
    lesson_id: str
    session_id: str
    lease: object
    cursor: int
    reply: str = ""
    tail: deque = field(default_factory=lambda: deque(maxlen=2))
    carried_uids: set = field(default_factory=set)
    lock: object = field(default_factory=threading.Lock)

    def checkpoint_completed(self, final_message):
        if final_message is None or len(self.tail) < 2:
            return False
        call, result = self.tail
        calls = call.get("tool_calls") or []
        if (
            call["role"] != "assistant"
            or len(calls) != 1
            or calls[0].get("function", {}).get("name") != "teacher_checkpoint"
            or result["role"] != "tool"
            or result.get("tool_call_id") != calls[0].get("id")
            or result.get("tool_name") != "teacher_checkpoint"
            or final_message.get("finish_reason") in {"length", "error", "aborted", "incomplete"}
        ):
            return False
        try:
            payload = json.loads(result.get("content"))
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("saved") is True and not payload.get("error")


class ClassroomPlugin:
    def __init__(self, ctx, profile):
        self.ctx = ctx
        self.profile = Path(profile)
        self.records = Records(self.profile)
        self.learning = Learning(self.profile, incoming_handoff=self.records.incoming_handoff)
        self.extensions = Extensions(self.profile, records=self.records)
        self.roles = Roles(self.profile, records=self.records)
        self.tools = classroom_tools(self.extensions.flags)
        self._turns = {}
        self._quiz_errors = {}
        self._lock = threading.Lock()
        self.teach = (
            (ROOT / "skills/teach/SKILL.md")
            .read_text(encoding="utf-8")
            .split("## Workflow", 1)[1]
            .split("## Verification", 1)[0]
        )

    def _release(self, turn_id):
        with self._lock:
            turn = self._turns.pop(turn_id, None)
        if turn:
            turn.lease.__exit__(None, None, None)

    def _turn(self, turn_id):
        with self._lock:
            return self._turns.get(turn_id)

    def _session_turn(self, session_id):
        with self._lock:
            return next(
                ((turn_id, turn) for turn_id, turn in self._turns.items() if turn.session_id == session_id),
                ("", None),
            )

    def _message_cursor(self, session_id):
        rows = native_messages(self.profile, session_id, limit=1, latest=True)
        return rows[0]["id"] if rows else 0

    def _turn_messages(self, turn):
        messages = native_messages(self.profile, turn.session_id, after_id=turn.cursor)
        return [row for row in messages if row.get("message_uid") not in turn.carried_uids]

    def _record_messages(self, turn, messages):
        for row in messages:
            text = assistant_text(row)
            if text.strip():
                self.records.append(
                    turn.lesson_id, "assistant", text, f"native:{turn.session_id}:{row['id']}",
                    session_id=turn.session_id,
                )
            # Only call identity and the checkpoint result are needed for turn-end validation.
            event = {key: row[key] for key in ("role", "tool_calls", "tool_call_id", "tool_name") if key in row}
            if row.get("tool_name") == "teacher_checkpoint":
                event["content"] = row.get("content")
            turn.tail.append(event)
            turn.cursor = row["id"]

    def _rotate_turn(self, turn, session_id):
        chain = native_compression_path(self.profile, session_id)
        if turn.session_id not in chain:
            return False
        # The child can carry rows already copied from the parent. Hermes preserves their IDs
        # across copies as message_uid, including recovery when a notification was missed.
        start = chain.index(turn.session_id)
        for parent, child in zip(chain[start:], chain[start + 1:]):
            self._record_messages(turn, self._turn_messages(turn))
            turn.carried_uids.update(
                row["message_uid"] for row in native_messages(self.profile, parent, include_inactive=True)
                if row.get("message_uid")
            )
            lesson = lesson_for_session(self.records, child)
            if lesson is None or lesson["id"] != turn.lesson_id:
                return False
            turn.session_id = child
            turn.cursor = 0
            turn.tail.clear()
        return True

    def _check_model(self, session_id, model):
        allowed = ModelSettings(self.profile).allowed()
        if not allowed:
            return
        from agent.auxiliary_client import _runtime_main_value
        from hermes_cli.runtime_provider_custom import session_provider_identity

        if _runtime_main_value("session_id") == session_id:
            provider = session_provider_identity(
                _runtime_main_value("provider"), _runtime_main_value("requested_provider"),
                _runtime_main_value("base_url"), model,
            )
            chosen = {"model": _runtime_main_value("model"), "provider": provider}
        else:
            chosen = native_model(self.profile, session_id)
        provider = chosen.get("provider")
        if not model_allowed(allowed, provider, model or chosen.get("model")):
            raise ValueError(tr(
                "Choose a model listed in teacher.models.allowed and retry.",
                "所选课堂模型不在 teacher.models.allowed 中，请切换至允许的模型后重试。",
            ))

    @localized
    def before(self, *, session_id, turn_id, user_message, parent_session_id="", model="",
               conversation_history=None, **kwargs):
        acquired = False
        lease = None
        try:
            lesson = lesson_for_session(self.records, session_id)
            if not lesson:
                return None
            if lesson["session_id"] != session_id:
                return dict(action="block", message=tr(
                    "This session is archived. Continue from the current classroom.", "这是课堂的历史会话，请从当前课堂继续。",
                ))
            self._check_model(session_id, model)
            lease = self.records.turn_lease(lesson["id"])
            lease.__enter__()
            acquired = True
            self.records.recover_pending_turn(lesson["id"])
            cursor = self._message_cursor(session_id)
            with self._lock:
                self._turns[turn_id] = ClassroomTurn(lesson["id"], session_id, lease, cursor)
                self._quiz_errors.pop(session_id, None)
            text = typed_text(user_message)
            if not text.strip():
                text = tr("Please review the attached files.", "请查看这次发送的附件。")
            file_request = _FILE_REQUEST.fullmatch(text.strip())
            if file_request:
                reply = self.command("file " + file_request[1].strip(), session_id=session_id)
                self._release(turn_id)
                return dict(action="block", message=reply)
            expected = None
            answer = _ANSWER_ENVELOPE.match(text)
            if answer:
                expected = answer[1] or answer[2]
                text = text[answer.end():]
            else:
                text = text.removeprefix(LITERAL_INPUT_PREFIX).removeprefix(LEGACY_LITERAL_INPUT_PREFIX)
            handoff = text.startswith((HANDOFF_PREFIX, LEGACY_HANDOFF_PREFIX))
            envelope = read_envelope(text)
            generated = handoff or envelope is not None or text.startswith(
                (GOAL_CONTINUATION_PREFIX, LEGACY_GOAL_CONTINUATION_PREFIX)
            )
            key = lesson["id"]
            if handoff and self.records.has_learner_input(key):
                self._release(turn_id)
                return dict(action="block", message=tr(
                    "This lesson already has a learning record. Restored its current progress.",
                    "这节课已有学习记录，已恢复当前进度。",
                ))
            if generated and self.learning.pending(key):
                self._release(turn_id)
                return dict(action="block", message=tr("Waiting for the learner's answer.", "正在等待学习者作答。"))
            notices = self.roles.take_notices(key, session_id, envelope) if envelope else None
            if envelope and not notices:
                self._release(turn_id)
                return dict(action="block", message=tr(
                    "This role completion has already been handled.", "这条角色完成回执已处理。",
                ))
            self.records.append(
                key,
                "system" if generated else "user",
                text,
                "user:" + turn_id,
                session_id=session_id,
                kind="internal" if generated else "message",
            )
            reply = self.learning.receive(key, text, turn_id, expected_id=expected, topic=lesson["topic"])
            if reply is not None:
                notice = self.records.take_pressure_notice(key, session_id=session_id)
                if notice:
                    reply += "\n\n" + notice
                self.records.append(key, "assistant", reply, "reply:" + turn_id, session_id=session_id)
                self._release(turn_id)
                if not self.learning.pending(key):
                    resume_offers(self.roles, key)
                return dict(action="block", message=reply)
            state = self.learning.state(key)
            context = {
                k: state[k] for k in ("goal", "stage", "plan", "node", "revision", "change", "preferences")
            }
            context["evidence"] = state["evidence"][-10:]
            context["probe_progress"] = self.learning.probe_progress(state)
            context["plan_edges"] = state.get("plan_edges", [])
            if "goal" in self.extensions.flags:
                context["active_goal"] = self.extensions.active_goal(lesson)
            if state.get("tool_allowlist") is not None:
                context["enabled_tools"] = sorted(self.tools.keys() & set(state["tool_allowlist"]))
            context["topic"] = lesson["topic"]
            context["handoff"] = lesson.get("handoff")
            context["interface_language"] = get_language()
            notices = notices or self.roles.take_notices(key, session_id)
            if notices:
                context["role_results"] = notices
            reply = {
                "context": POLICY.format(language=tr("English", "Simplified Chinese"))
                + "\n"
                + self.teach
                + tr("\nClassroom state\n", "\n课程状态\n")
                + json.dumps(context, ensure_ascii=False)
            }
            if conversation_history and isinstance(conversation_history[-1], dict):
                self.records.begin_native_turn(key, turn_id, session_id, cursor, conversation_history[-1])
            return reply
        except Exception as error:
            if acquired:
                if self._turn(turn_id) is None:
                    lease.__exit__(None, None, None)
                else:
                    self._release(turn_id)
            return dict(action="block", message=tr(
                f"The classroom could not process this input. {error}", f"课堂无法处理本次输入。{error}",
            ))

    @localized
    def guard(self, *, tool_name, session_id="", turn_id="", **kwargs):
        turn = self._turn(turn_id)
        if turn:
            with turn.lock:
                if turn.session_id == session_id or self._rotate_turn(turn, session_id):
                    self._record_messages(turn, self._turn_messages(turn))
        lesson = lesson_for_session(self.records, session_id)
        if not lesson:
            return None
        if lesson["session_id"] != session_id or tool_name not in self.tools:
            return dict(action="block", message=tr(
                "Only teaching tools are available in this classroom.", "当前课堂只开放教学工具。",
            ))
        limits = self.learning.state(lesson["id"]).get("tool_allowlist")
        if limits is not None and tool_name not in limits:
            return dict(action="block", message=tr(
                "This tool is disabled for the current classroom.", "本课堂启动设置已关闭此工具。",
            ))
        return None

    def filter_tools(self, *, request, session_id="", **kwargs):
        lesson = lesson_for_session(self.records, session_id)
        if not lesson:
            return None
        limits = self.learning.state(lesson["id"]).get("tool_allowlist")
        if limits is None:
            return None
        allowed = self.tools.keys() & set(limits)
        request = dict(request)
        tool_config = request.get("toolConfig")
        if isinstance(tool_config, dict):
            tools = [
                item for item in tool_config.get("tools", [])
                if item.get("toolSpec", {}).get("name") in allowed
            ]
            if tools:
                request["toolConfig"] = {"tools": tools}
            else:
                request.pop("toolConfig")
        if isinstance(request.get("tools"), list):
            tools = [
                item for item in request["tools"]
                if (item.get("function", {}).get("name") or item.get("name")) in allowed
            ]
            if tools:
                request["tools"] = tools
                if isinstance(request.get("tool_choice"), dict):
                    request.pop("tool_choice")
            else:
                for field in ("tools", "tool_choice", "parallel_tool_calls"):
                    request.pop(field, None)
        return {"request": request}

    @localized
    def transform(self, *, session_id, response_text, turn_id="", **kwargs):
        # Hermes also transforms pre_llm_call vetoes. Those already contain the exact
        # hint, validation error, attachment directive or duplicate-receipt response.
        with self._lock:
            if turn_id not in self._turns:
                return None
        lesson = lesson_for_session(self.records, session_id)
        if lesson:
            card = self.learning.pending(lesson["id"])
            with self._lock:
                failure = self._quiz_errors.pop(session_id, None)
            text = card["text"] if card else failure or response_text
            notice = self.records.take_pressure_notice(lesson["id"], session_id=session_id)
            return (text + "\n\n" + notice) if notice else text
        return None

    @localized
    def usage(self, *, session_id, turn_id="", usage=None, context_length=None, **kwargs):
        turn = self._turn(turn_id)
        if turn:
            with turn.lock:
                if turn.session_id != session_id:
                    self._rotate_turn(turn, session_id)
        used = (usage or {}).get("prompt_tokens") or 0
        if not context_length or used < 0.7 * context_length:
            return
        lesson = lesson_for_session(self.records, session_id)
        if lesson:
            self.records.pressure(lesson["id"], used=used, limit=context_length, session_id=session_id)

    def after(self, *, session_id, turn_id, assistant_response, **kwargs):
        turn = self._turn(turn_id)
        if turn and isinstance(assistant_response, str):
            turn.reply = assistant_response

    @localized
    def end(self, *, session_id="", turn_id="", completed=False, failed=False, interrupted=False, **kwargs):
        turn = self._turn(turn_id)
        if not turn:
            return
        try:
            with turn.lock:
                if turn.session_id != session_id and not self._rotate_turn(turn, session_id):
                    return
                messages = self._turn_messages(turn)
                final = messages[-1] if messages else None
                if final and assistant_text(final).strip() and not final.get("tool_calls"):
                    self._record_messages(turn, messages[:-1])
                    body = turn.reply or assistant_text(final)
                else:
                    self._record_messages(turn, messages)
                    final = None
                    body = turn.reply
                if body.strip():
                    verified = completed and not failed and not interrupted and turn.checkpoint_completed(final)
                    self.records.append(
                        turn.lesson_id, "assistant", body, "reply:" + turn_id, session_id=session_id,
                        completed_checkpoint_turn=turn_id if verified else None,
                    )
                if failed or interrupted:
                    event = "interrupted" if interrupted else "failed"
                    self.records.append(
                        turn.lesson_id, "system", event, "end:" + turn_id, session_id=session_id, kind="internal",
                    )
                self.records.end_native_turn(turn.lesson_id, turn_id)
        finally:
            self._release(turn_id)
            if completed and not failed and not interrupted and not self.learning.pending(turn.lesson_id):
                resume_offers(self.roles, turn.lesson_id)

    @localized
    def compacted(self, *, session_id, old_session_id="", **kwargs):
        if old_session_id and old_session_id != session_id:
            _, turn = self._session_turn(old_session_id)
            if turn:
                with turn.lock:
                    self._rotate_turn(turn, session_id)
        lesson = lesson_for_session(self.records, session_id)
        if lesson:
            self.records.pressure(lesson["id"], compressed=True, session_id=session_id)

    @localized
    def invoke(self, name, args, session_id):
        lesson = lesson_for_session(self.records, session_id)
        if not lesson:
            raise ValueError(tr("Open a lesson from the Teacher lobby.", "请从 Teacher 大厅打开课堂。"))
        key = lesson["id"]
        limits = self.learning.state(key).get("tool_allowlist")
        if limits is not None and name not in limits:
            raise ValueError(tr("This tool is disabled for the current classroom.", "本课堂启动设置已关闭此工具。"))
        if name in EXTENSION_TOOLS:
            if name not in self.tools or lesson["session_id"] != session_id:
                raise ValueError(tr("Enable the corresponding classroom extension first.", "请在当前课堂启用对应个人增强。"))
            if name == "teacher_todo":
                return self.extensions.todo_tasks(lesson, **args)
            if name == "teacher_find":
                return self.extensions.find(Path(lesson["vault"]), **args)
            if name == "teacher_grep":
                return self.extensions.grep(Path(lesson["vault"]), **args)
            if self.learning.pending(key):
                raise ValueError(tr("Waiting for the learner to finish the classroom interaction.", "正在等待学习者完成课堂交互。"))
            return self.extensions.report_goal(lesson, **args)
        if name == "teacher_quiz":
            card = self.learning.pending(key)
            if card:
                return card
            with self._lock:
                if session_id in self._quiz_errors:
                    return {"error": self._quiz_errors[session_id]}
            state = self.learning.state(key)
            card = make_question(self.ctx.llm, self.learning, key, lesson["topic"], state, args)
            if card is not None:
                return card
            message = tr(
                "The quiz could not be generated. Your progress has been saved. Reply “Retry quiz”.",
                "本次测验未能生成有效题面。学习进度已保留，请回复「重新出题」。",
            )
            with self._lock:
                self._quiz_errors[session_id] = message
            return {"error": message}
        if name == "teacher_ask":
            return self.learning.ask(key, **args)
        if name == "teacher_material":
            path = args.get("path")
            paging = {field: args[field] for field in ("start_line", "line_count", "start_byte") if field in args}
            if path:
                return material_page(lesson["vault"], path, **paging)
            if paging:
                raise ValueError(tr("Provide a vault-relative path to read part of a file", "分段读取需要提供资料库相对路径"))
            return list_materials(lesson["vault"])
        if name == "teacher_checkpoint":
            turn_id, turn = self._session_turn(session_id)
            if turn is None or lesson["session_id"] != session_id:
                raise ValueError(tr("Save the handoff during an active classroom turn", "请在当前课堂回合保存续课说明"))
            fields = {
                "covered": tr("Covered", "已讲内容"),
                "evidence": tr("Evidence of understanding", "理解证据"),
                "gaps": tr("Gaps", "尚待巩固"),
                "next_step": tr("Next steps", "下一步"),
            }
            if any(not isinstance(args.get(k), str) or not args[k].strip() for k in fields):
                raise ValueError(tr("Complete all handoff fields", "请完整填写续课说明"))
            body = "\n\n".join(f"## {title}\n\n{args[k]}" for k, title in fields.items())
            saved = self.records.checkpoint(key, body, session_id=session_id, turn_id=turn_id)
            return {"saved": True, "path": saved["path"]}
        if name == "teacher_role":
            _, turn = self._session_turn(session_id)
            return self.roles.dispatch(key, **args, notify=turn is not None, read=True)
        if name == "teacher_plan":
            job = self.roles.dispatch(key, "status", task_id=args["research_id"], read=True)
            if job["role"] != "researcher" or job["status"] != "completed":
                raise ValueError(tr("Wait for this lesson's researcher to finish verification", "请先等待本课研究员完成核查"))
            result = job["result"]
            summary = result["text"] if "text" in result else result.get("summary")
            if result.get("sources"):
                summary += "\n\n" + "\n".join(s["url"] for s in result["sources"])
            return self.learning.plan(key, args["nodes"], args["rationale"], summary, edges=args.get("edges"))
        raise ValueError(tr("Unknown classroom tool", "未知课堂工具"))

    @localized
    def command(self, raw, *, session_id="", **kwargs):
        action = raw.strip() or "status"
        lesson = lesson_for_session(self.records, session_id)
        if action == "quiz-state":
            view = self.learning.quiz_view(lesson["id"]) if lesson else None
            return json.dumps({"quiz": view, "language": get_language()}, ensure_ascii=False)
        if action == "interaction-state":
            view = self.learning.interaction_view(lesson["id"]) if lesson else None
            return json.dumps({"interaction": view, "language": get_language()}, ensure_ascii=False)
        if not lesson:
            return tr(
                "Prepare a lesson in the Teacher lobby, then open its Hermes session in this profile.",
                "请在 Teacher 大厅准备课堂，然后在本 profile 打开对应的 Hermes 会话。",
            )
        key = lesson["id"]
        if action in {"status", "状态"}:
            card = self.learning.pending(key)
            state = self.learning.state(key)
            return f"{lesson['topic']} · {key}\n{state['stage']}\n" + (
                card["text"] if card else tr("Waiting for the learner's input.", "等待学习者输入。")
            )
        if action == "organize":
            from .organizer import Organizer
            from .organizer_plan import NativeOrganizer

            try:
                result = Organizer(self.records).organize(
                    Path(lesson["vault"]), NativeOrganizer(self.profile).complete, topic=lesson["topic"]
                )
            except (OSError, ValueError, RuntimeError) as exc:
                result = {"error": str(exc)}
            return json.dumps(result, ensure_ascii=False)
        if action == "note":
            if not lesson["recording"]:
                return tr("Recording is off for this lesson.", "本课已停录。")
            return lesson["record"] or tr("There are no active notes for this lesson.", "本课尚无活动笔记。")
        if action.startswith("file "):
            from .gateway import app_artifact

            try:
                return app_artifact(self.profile, lesson["vault"], action[5:].strip(),
                                    records=self.records, lesson=lesson)
            except (OSError, ValueError, RuntimeError) as exc:
                return tr(f"Could not provide the classroom file. {exc}", f"无法提供课堂文件。{exc}")
        if action == "checkpoint":
            return self.records.read_checkpoint(key)["text"]
        filename = tr("FILE", "文件")
        return f"/teacher status · /teacher note · /teacher checkpoint · /teacher file viz/{filename} · /teacher organize"

    @localized
    def desktop_command(self, raw, *, session_id="", **kwargs):
        import asyncio

        from .desktop_classroom import DesktopClassroom

        try:
            classroom = DesktopClassroom(self.profile, session_id)
        except (OSError, ValueError, RuntimeError) as error:
            return json.dumps({"error": str(error), "language": get_language()}, ensure_ascii=False)
        return asyncio.run(classroom.request(raw))


def register(ctx):
    from hermes_constants import get_hermes_home

    plugin = ClassroomPlugin(ctx, get_hermes_home())
    with use_profile(plugin.profile):
        _register(ctx, plugin)


def _register(ctx, plugin):
    ctx.register_auxiliary_task(
        "teacher_question",
        display_name=tr("Classroom question writer", "课堂独立命题"),
        description=tr("Draft questions and private answer keys", "生成题面及私有评分依据"),
        defaults={"reasoning_effort": "low"},
    )
    for name, schema in plugin.tools.items():

        def handler(args, session_id="", *, _name=name, **kwargs):
            try:
                result = plugin.invoke(_name, args, session_id)
            except (ValueError, OSError, KeyError) as error:
                result = {"error": str(error)}
            return json.dumps(result, ensure_ascii=False)

        ctx.register_tool(name=name, toolset="teacher", schema={"name": name, **schema}, handler=handler)
    for name, callback in [
        ("pre_llm_call", plugin.before),
        ("pre_tool_call", plugin.guard),
        ("post_llm_call", plugin.after),
        ("on_session_end", plugin.end),
        ("post_compaction", plugin.compacted),
        ("post_api_request", plugin.usage),
    ]:
        ctx.register_hook(name, callback)
    ctx.register_hook("transform_llm_output", plugin.transform, hold_stream=True)
    ctx.register_middleware("llm_request", plugin.filter_tools)
    ctx.register_command(
        "teacher", plugin.command,
        description=tr("Show the current lesson, question, notes, or handoff", "显示当前课堂、题面、笔记或续课说明"),
    )
    ctx.register_command(
        "teacher-desktop", plugin.desktop_command,
        description=tr("Navigate and manage lessons from Desktop", "桌面课堂的导航和操作"),
    )
    for name in ["teach", "lesson-research", "lesson-visuals", "lesson-organizer", "classroom-control"]:
        ctx.register_skill(name, ROOT / "skills" / name / "SKILL.md")

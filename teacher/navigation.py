"""Local navigation and explicit classroom operations. Every navigation command is model-free."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re

from .i18n import localized, tr
from .library import configure_topic_vault, file_lease, list_materials, management_root, read_material, vault_directory
from .models import ModelSettings, write_private
from .organizer import Organizer
from .roles import Roles
from .runtime import handoff_message


_ENGLISH_TOPIC = re.compile(
    r"^(?:(?P<switch>switch (?:topics? )?to|open (?:a )?topic)|continue learning|"
    r"(?:today )?(?:I want to|I would like to|I['’]d like to) (?:learn|study))"
    r"\b[：:\s]*(?P<title>[^，,。！？!?；;\n]{1,80})$",
    re.IGNORECASE,
)


class Navigator:
    def __init__(self, tutor, vault, lesson=None, *, claim=nullcontext, tool_limits=None):
        self.tutor = tutor
        self.profile = tutor.profile
        self.records = tutor.records
        self.claim = claim
        self.tool_limits = tool_limits
        self.vault = vault_directory(vault)
        self.lesson = None
        self.draft = ""
        self.query = ""
        self.matches = []
        self.selecting = False
        self.recent_path = tutor.profile / "teacher/vaults.json"
        if lesson:
            self.choose(lesson_id=lesson)
        self.remember(self.vault)

    def remember(self, vault):
        with file_lease(self.records.state, "locks/recent-vaults.lock", wait=True):
            values = json.loads(self.recent_path.read_text()) if self.recent_path.exists() else []
            write_private(
                self.recent_path,
                json.dumps([str(vault)] + [v for v in values if v != str(vault)][:29], ensure_ascii=False).encode(),
            )

    def busy(self):
        return bool(self.lesson and Roles(self.tutor.profile).busy(self.lesson["id"]))

    def workspace_path(self, path):
        return Path(os.path.normpath(self.vault / Path(path).expanduser()))

    @localized
    def switch(self, path, *, create=False):
        target = self.workspace_path(path)
        if target == self.vault:
            return
        if self.busy():
            raise ValueError(tr("Wait for this lesson's tasks to finish before switching vaults", "请等本课协作任务完成后切换资料库"))
        chosen = vault_directory(target, create=create)
        if chosen.samefile(self.vault):
            return
        with self.claim(None):
            self.remember(chosen)
        self.vault, self.lesson = chosen, None
        self.matches, self.selecting = [], False

    def browse(self, path=""):
        base = vault_directory(self.workspace_path(path))
        return base, [
            child for child in sorted(base.iterdir())
            if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
        ]

    @localized
    def choose(self, *, topic=None, lesson_id=None, new=False, temporary=False):
        if self.busy():
            raise ValueError(tr("Wait for this lesson's tasks to finish before switching lessons", "请等本课协作任务完成后切课"))
        inherit_from = None
        if lesson_id:
            lesson = self.tutor.lesson(lesson_id)
        else:
            lesson = self.records.prepare(
                self.vault, topic, new=new, temporary=temporary, report_creation=True
            )
            if lesson.pop("newly_created") and self.lesson:
                inherit_from = self.lesson["id"]
        return self.enter(lesson, inherit_from=inherit_from)

    def enter(self, lesson, *, inherit_from=None):
        with self.claim(lesson["id"]):
            self.tutor.seed_route(lesson["id"], inherit_from)
            if self.tool_limits is not None:
                from .plugin import apply_tool_limits

                apply_tool_limits(self.tutor, lesson["id"], *self.tool_limits)
                self.tool_limits = None
            self.records.visit(lesson["id"])
        self.lesson = lesson
        self.vault = Path(lesson["vault"])
        self.matches, self.selecting = [], False
        return lesson

    def catalog(self, query=""):
        self.query = query
        self.matches = self.records.search(management_root(self.vault), query)
        return self.matches

    def prose(self, text):
        """Handle an explicit navigation request and retain a new learning goal as a draft."""
        value = text.strip().rstrip("。！!.")
        if self.draft and text == self.draft and self.lesson:
            self.draft = ""
            return False
        if value in {"继续上次", "继续上次的学习", "继续上次学习", "接着上次", "继续最近的课堂"} or re.fullmatch(
            r"(?:continue|resume) (?:the )?last lesson", value, re.IGNORECASE,
        ):
            self.action("resume", "")
            return True
        if value in {"切换主题", "选择主题", "打开课堂导航", "返回学习大厅"} or re.fullmatch(
            r"open (?:the )?classroom navigator|return to (?:the )?classroom lobby", value, re.IGNORECASE,
        ):
            self.action("lobby", "")
            return True
        match = re.match(
            r"^(?:(?P<switch>(?:切换|换)(?:主题)?(?:到|为)|继续学习)|"
            r"(?:我)?(?:今天|现在|接下来)?(?:我)?想(?:要)?学(?:习)?|今天学(?:习)?|我要学(?:习)?|开始学习)"
            r"[：:\s]*(?P<title>[^，,。！？!?；;\n]{1,80})$", value,
        )
        match = match or _ENGLISH_TOPIC.fullmatch(value)
        if match and match["title"].strip():
            self.choose(topic=match["title"].strip())
            self.draft = "" if match["switch"] else text
            return True
        if not self.lesson:
            self.choose(new=True, temporary=True)
        return False

    def active(self):
        if not self.lesson:
            raise ValueError(tr("Choose a lesson first", "请先选择课堂"))
        return self.lesson["id"]

    @localized
    def action(self, command, argument):
        if command in {"topic", "new"}:
            if command == "topic" and argument.startswith("--new "):
                return self.choose(topic=argument[6:].strip(), new=True)
            return self.choose(topic=argument, new=command == "new")
        if command == "open":
            if argument.isdecimal() and len(argument) < 6:
                n = int(argument)
                if not 1 <= n <= len(self.matches):
                    raise ValueError(tr(
                        "Use /list to find a lesson, then enter its number", "请先用 /list 找到课堂，再输入列表中的序号",
                    ))
                argument = self.matches[n - 1]["id"]
            if not any(row["id"] == argument for row in self.records.recent(management_root(self.vault))):
                raise ValueError(tr("This lesson is outside the current vault", "当前资料库范围内没有这节课"))
            return self.choose(lesson_id=argument)
        if command in {"list", "search"}:
            rows = self.catalog(argument)
            self.selecting = bool(rows)
            if not rows:
                return tr("No matching lessons.", "这里还没有匹配的课堂。")
            return (
                "\n".join(f"{i}. {r['topic']}  {r['id'][:8]}" for i, r in enumerate(rows, 1))
                + tr(
                    "\nUse /open NUMBER to select a lesson, or /cancel to close the list.",
                    "\n/open 序号 选择，/cancel 退出列表。",
                )
            )
        if command == "cancel" and self.selecting:
            self.matches, self.selecting = [], False
            return tr("Closed the lesson list.", "已退出课程列表。")
        if command == "lobby":
            if self.busy():
                raise ValueError(tr("Wait for the lesson's tasks to finish", "请先等协作任务完成"))
            with self.claim(None):
                self.vault = management_root(self.vault)
            self.lesson = None
            self.matches, self.selecting = [], False
            return str(self.vault)
        if command == "resume":
            found = self.records.recent(management_root(self.vault))
            if not found:
                raise ValueError(tr("There are no lessons to resume in this vault", "这里还没有可续接的课堂"))
            return self.choose(lesson_id=found[0]["id"])
        if command == "vaults":
            return "\n".join(json.loads(self.recent_path.read_text()))
        if command == "browse":
            _, children = self.browse(argument)
            return "\n".join(map(str, children))
        if command == "route":
            return str(configure_topic_vault(self.vault, argument, create=True))
        if command == "material":
            return read_material(self.vault, argument) if argument else list_materials(self.vault)
        if command == "models-import":
            dry = argument.startswith("--dry-run ")
            path = argument.removeprefix("--dry-run ").strip()
            return ModelSettings(self.tutor.profile).sync(Path(path).expanduser(), dry_run=dry)
        course = self.active()
        if command == "status":
            lesson = self.tutor.lesson(course)
            return {
                **lesson,
                "pending": self.tutor.learning.pending(course),
                "state": self.tutor.learning.state(course),
            }
        if command == "session":
            return asyncio.run(self.tutor.open(course))
        if command == "model":
            if not argument:
                return self.tutor.model(course)
            provider, sep, model = argument.partition("/")
            return self.tutor.model(
                course, model=model if sep else provider, provider=provider if sep else None
            )
        if command == "reasoning":
            return self.tutor.model(course, effort=argument)
        if command == "record":
            if argument in ("on", "off"):
                return self.records.set_recording(course, argument == "on")
            if argument.startswith("bind "):
                return self.records.bind_record(course, argument[5:])
            lesson = self.tutor.lesson(course)
            return lesson["record"] if lesson["recording"] else tr("Recording is off for this lesson.", "本课已停录。")
        if command == "seal":
            return self.records.seal(course, busy=self.busy())
        if command == "checkpoint":
            return self.records.read_checkpoint(course)["text"]
        if command == "handoff":
            new = self.records.handoff(
                course,
                confirmed=argument.strip().casefold() in {"confirm", "确认"},
                busy=self.busy(),
                capture_progress=lambda: self.tutor.handoff_progress(course),
            )
            self.tutor.learning.seed_handoff(new["id"])
            self.enter(new)
            return self.send_handoff()
        if command == "handoff-retry":
            return self.send_handoff()
        if command == "organize":
            return Organizer(self.records).snapshot(self.vault, topic=self.lesson["topic"])
        if command == "role":
            action, _, task = argument.partition(" ")
            roles = Roles(self.tutor.profile)
            if action == "start":
                role, _, instruction = task.partition(" ")
                return roles.dispatch(course, action, role=role, task=instruction.strip())
            if action == "supplement":
                task_id, _, instruction = task.partition(" ")
                return roles.dispatch(course, action, task_id=task_id, task=instruction.strip())
            return roles.dispatch(course, action, task_id=task.strip())
        raise ValueError(tr(
            f"Unknown command /{command}. Use /help to see available commands.",
            f"未知命令 /{command}，输入 /help 可查看操作。",
        ))

    def send_handoff(self):
        key = self.active()
        row = self.tutor.lesson(key)
        if not row.get("handoff"):
            raise ValueError(tr("There is no pending handoff for this lesson", "当前课堂没有待传递的续课说明"))
        header = tr(f"Opened the new lesson {key} for {row['topic']}.", f"已切换到 {row['topic']} 的新课堂 {key}。")
        if self.records.has_learner_input(key):
            return header + tr("\n\nRestored the current lesson progress.", "\n\n已恢复这节课的当前进度。")
        try:
            event_id = "handoff-" + key
            language = self.tutor.learning.request_language(key, event_id)
            reply = self.tutor.turn(
                key,
                handoff_message(row["handoff"], language=language),
                event_id=event_id,
            )
        except (ValueError, RuntimeError, OSError) as error:
            return header + tr(
                f" Handoff delivery is pending. Use /handoff-retry to check its receipt. {error}",
                f"交接发送尚待确认，可用 /handoff-retry 核对同一回执。{error}",
            )
        return header + "\n\n" + reply

"""Opt-in classroom enhancements using scoped metadata and native callbacks."""

import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .i18n import localized, profile_language, tr
from .learning import Learning
from .library import (
    MARKER, MATERIAL_FOLDERS, MATERIAL_SUFFIXES, LibraryError, read_bytes, safe_path, topic_vaults, vault_directory,
)
from .models import ModelSettings, read_config
from .records import Records


FLAGS = {"todo", "goal", "search", "title", "compact", "model", "ui"}
SEARCH_TEXT = MATERIAL_SUFFIXES | {".svg", ".mmd", ".py", ".js", ".ts", ".ipynb"}


def extensions_config(profile):
    path = Path(profile) / "config.yaml"
    if not path.is_file():
        return set()
    config = read_config(path)
    teacher = config.get("teacher", {})
    if not isinstance(teacher, dict):
        raise ValueError(tr("teacher must be an object.", "teacher 须为对象", language=profile_language(profile)))
    options = teacher.get("extensions", {})
    if (
        not isinstance(options, dict)
        or set(options) - FLAGS
        or any(type(value) is not bool for value in options.values())
    ):
        raise ValueError(tr("teacher.extensions accepts known capabilities with Boolean flags.",
                            "teacher.extensions 仅接受已知能力及布尔开关", language=profile_language(profile)))
    return {name for name, enabled in options.items() if enabled}


class Extensions:
    def __init__(
        self, profile, *, records=None, compact=None, rename=None, turn=None, pending=None, judge=None,
        derive_title=None,
    ):
        self.profile = Path(profile)
        self.flags = extensions_config(profile)
        self.records = records or Records(self.profile)
        self.compact = compact
        self.rename = rename
        self.turn = turn
        self.pending = pending
        self.judge = judge
        self.derive_title = derive_title

    def state(self, lesson, kind, default=None):
        for event in reversed(
            self.records.history(lesson["id"], session_id=lesson["session_id"], include_hidden=True)
        ):
            if event["role"] == "extension" and event["kind"] == kind:
                return json.loads(event["text"])
        return default

    def save(self, lesson, kind, state):
        self.records.append(
            lesson["id"],
            "extension",
            json.dumps(state, ensure_ascii=False),
            f"{kind}:{uuid4().hex}",
            session_id=lesson["session_id"],
            kind=kind,
        )

    @localized
    def invoke(self, lesson, command, argument=""):
        command = command.removeprefix("/")
        flag = "model" if command == "model-info" else command
        if flag not in self.flags:
            return None
        key = lesson["id"]
        if command == "search":
            return self.search(Path(lesson["vault"]), argument)
        if command == "model-info":
            model = Learning(self.profile).state(key)["model"] or ModelSettings(self.profile).lesson()
            default = tr("default", "默认")
            effort = model.get("reasoning_effort", default)
            identifier = f"{model.get('provider', default)}/{model.get('model', default)}"
            return tr(f"Lesson model {identifier} · Reasoning effort {effort}",
                      f"课堂模型 {identifier} · 思考强度 {effort}")
        if command == "title":
            if not argument.strip():
                return self.state(lesson, "title", tr("This lesson has no separate session title.", "当前课堂尚无单独的会话标题。"))
            return self.set_title(lesson, argument)
        if command == "compact":
            if not self.compact:
                return tr("Native session compression is unavailable in this client.", "当前入口无法调用原生会话压缩。")
            return self.compact(key, argument)
        if command == "todo":
            return self.todo(lesson, argument)
        if command == "goal":
            return self.goal(lesson, argument)
        return None

    @localized
    def todo(self, lesson, argument):
        action, _, value = argument.strip().partition(" ")
        if action not in {"", "list", "add", "done", "remove"} or action == "add" and not value.strip():
            return tr("Use /todo add TASK, /todo done NUMBER, or /todo remove NUMBER.", "用 /todo add 任务、/todo done 编号或 /todo remove 编号。")
        number = int(value) if value.isdecimal() and len(value) <= 8 else None
        try:
            tasks = self.todo_tasks(lesson, action or "list", text=value, number=number)
        except ValueError as error:
            return str(error)
        return (
            "\n".join(
                f"{index}. [{'x' if item['done'] else ' '}] {item['text']}"
                for index, item in enumerate(tasks, 1)
            )
            or tr("This session branch has no tasks.", "当前会话分支没有待办。")
        )

    @localized
    def todo_tasks(self, lesson, action, *, text="", number=None):
        with self.records.lease(lesson["id"]):
            current = self.records.get(lesson["id"])
            tasks = self.state(current, "todo", [])
            if action == "list":
                return tasks
            if action == "add":
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise ValueError(tr("Task text must be 1–2,000 characters.", "待办正文须为 1–2,000 个字符。"))
                tasks.append({"text": text.strip(), "done": False})
            elif action in {"done", "remove"}:
                if type(number) is not int or not 1 <= number <= len(tasks):
                    raise ValueError(tr("Use /todo to see the task numbers.", "用 /todo 查看任务编号。"))
                index = number - 1
                if action == "done":
                    tasks[index]["done"] = True
                else:
                    tasks.pop(index)
            else:
                raise ValueError(tr("Unknown task action.", "未知待办操作。"))
            self.save(current, "todo", tasks)
            return tasks

    @staticmethod
    def paths(vault):
        vault = vault_directory(vault)
        other_vaults = set(topic_vaults(vault).values())
        for directory, subdirs, filenames in os.walk(vault, followlinks=False):
            parent = Path(directory)
            subdirs[:] = sorted(
                name
                for name in subdirs
                if not name.startswith(".")
                and not (parent / name).is_symlink()
                and parent / name not in other_vaults
                and not (parent / name / MARKER).exists()
            )
            for name in sorted(filenames):
                if name.startswith(".") or (parent / name).suffix.lower() not in SEARCH_TEXT:
                    continue
                yield (parent / name).relative_to(vault)

    @classmethod
    def find(cls, vault, query="", *, materials_only=False):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError(tr("Filename queries are limited to 500 characters.", "文件名查询最多支持 500 个字符。"))
        terms = query.casefold().split()
        hits = []
        for relative in cls.paths(vault):
            if materials_only and (len(relative.parts) < 2 or relative.parts[0] not in MATERIAL_FOLDERS
                                   or relative.suffix.lower() not in MATERIAL_SUFFIXES):
                continue
            if not all(term in relative.as_posix().casefold() for term in terms):
                continue
            try:
                safe_path(vault, relative, must_exist=True)
            except (OSError, LibraryError):
                continue
            hits.append(relative.as_posix())
            if len(hits) == 20:
                break
        return hits

    @classmethod
    def text_files(cls, vault):
        for relative in cls.paths(vault):
            try:
                text = read_bytes(vault, relative, limit=1024 * 1024).decode("utf-8-sig")
            except (OSError, LibraryError, UnicodeError):
                continue
            yield relative.as_posix(), text

    @classmethod
    def grep(cls, vault, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError(tr("Text queries must be 1–500 characters.", "正文查询须为 1–500 个字符。"))
        query = query.strip().casefold()
        hits = []
        for relative, text in cls.text_files(vault):
            for number, line in enumerate(text.splitlines(), 1):
                if query in line.casefold():
                    hits.append({"path": relative, "line": number, "text": line.strip()[:180]})
                    if len(hits) == 20:
                        return hits
        return hits

    @classmethod
    def search(cls, vault, argument):
        terms = argument.casefold().split()
        if not terms:
            return tr("Use /search KEYWORD to find files in the current library.", "用 /search 关键词 在当前资料库内查找文件。")
        hits = []
        for relative, text in cls.text_files(vault):
            haystack = f"{relative}\n{text}".casefold()
            if all(term in haystack for term in terms):
                source = next(
                    (
                        line.strip()
                        for line in text.splitlines()
                        if any(term in line.casefold() for term in terms)
                    ),
                    "",
                )
                suffix = f" · {source[:140]}" if source else ""
                hits.append(f"{relative}{suffix}")
            if len(hits) == 20:
                return "\n".join(hits)
        return "\n".join(hits) or tr("This library has no matching documents.", "当前资料库没有匹配文档。")

    def complete(self, lesson, text):
        if "search" not in self.flags or not lesson:
            return []
        match = re.search(r"(?:^/material +|(?:^|\s)@)([^\n]*)$", text)
        if not match:
            return []
        prefix = match.group(1)
        try:
            paths = self.find(Path(lesson["vault"]), prefix, materials_only=text.startswith("/material "))
        except (OSError, ValueError):
            return []
        return [(path, -len(prefix)) for path in paths]

    @localized
    def set_title(self, lesson, argument):
        title = " ".join(argument.split())[:100]
        if not title:
            return tr("Enter a session title.", "请输入会话标题。")
        if not self.rename:
            return tr("This client cannot set a Hermes session title.", "当前入口无法设置 Hermes 会话标题。")
        result = self.rename(lesson["id"], title)
        if result is False:
            return tr("Hermes did not save the session title.", "Hermes 会话标题未保存。")
        self.save(self.records.get(lesson["id"]), "title", title)
        return tr(f"Saved session title · {title}", f"会话标题已保存 · {title}")

    def auto_title(self, lesson, user_text):
        if (
            "title" not in self.flags
            or not self.rename
            or not user_text.strip()
            or self.state(lesson, "title")
        ):
            return None
        derive = self.derive_title
        if derive is None:
            from agent.title_generator import derive_title

            derive = derive_title
        title = derive(user_text)
        return self.set_title(lesson, title) if title else None

    @localized
    def goal(self, lesson, argument):
        with self.records.lease(lesson["id"]):
            current = self.records.get(lesson["id"])
            state = self.state(current, "goal")
            action, _, value = argument.strip().partition(" ")
            if not action or action in {"status", "show"}:
                return self.goal_line(state)
            if action == "set" and value.strip():
                state = {"id": uuid4().hex, "text": value.strip(), "status": "active", "turns": 0, "reason": ""}
            elif not state:
                return tr("Use /goal set GOAL to start goal mode.", "用 /goal set 目标 显式开启目标模式。")
            elif action == "pause":
                state = {**state, "status": "paused", "reason": tr("Paused by the user", "用户暂停")}
            elif action == "resume" and state["status"] in {"paused", "waiting", "blocked"}:
                state = {**state, "status": "active", "turns": 0, "reason": ""}
            elif action == "clear":
                state = None
            else:
                return tr("Use /goal set GOAL, /goal status, /goal pause, /goal resume, or /goal clear.",
                          "用 /goal set 目标、/goal status、/goal pause、/goal resume 或 /goal clear。")
            self.save(current, "goal", state)
        return self.goal_line(state)

    def active_goal(self, lesson):
        state = self.state(lesson, "goal")
        if state and state["status"] == "active":
            return state
        return None

    @localized
    def report_goal(self, lesson, action, goal_id, reason):
        statuses = {"complete": "done", "blocked": "blocked", "wait": "waiting"}
        if action not in statuses or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValueError(tr("Provide the goal status and a reason based on actual progress.", "请提交目标状态及基于实际进展的理由。"))
        with self.records.lease(lesson["id"]):
            current = self.records.get(lesson["id"])
            state = self.active_goal(current)
            if not state or not goal_id or state.get("id") != goal_id:
                raise ValueError(tr("Use the goal ID the learner started in this session.", "请使用学习者在当前会话开启的目标 ID。"))
            updated = {**state, "status": statuses[action], "reason": reason.strip()}
            self.save(current, "goal", updated)
        return self.goal_line(updated)

    @staticmethod
    def goal_line(state):
        if not state:
            return tr("Goal mode is inactive for this session branch.", "当前会话分支未开启目标模式。")
        labels = {
            "active": ("Active", "进行中"), "done": ("Done", "完成"), "paused": ("Paused", "已暂停"),
            "waiting": ("Waiting", "等待"), "blocked": ("Blocked", "受阻"),
        }
        label = tr(*labels[state["status"]])
        reason = f" · {state['reason']}" if state["reason"] else ""
        return f"{label} · {state['text']}{reason}"

    @localized
    def continue_goal(self, lesson, response, *, pending=None):
        """Continue only after an explicit goal and while the learner is free to proceed."""
        if "goal" not in self.flags or not self.turn:
            return []
        outputs = []
        key = lesson["id"]
        pending = pending or self.pending
        if pending is None:
            return []
        while not pending(key):
            lesson = self.records.get(key)
            with self.records.lease(key):
                state = self.state(lesson, "goal")
                if not state or state["status"] != "active" or pending(key):
                    break
                judge = self.judge
                if judge is None:
                    from hermes_cli.goals import judge_goal

                    judge = judge_goal
                verdict, reason, unparsed, _, failed = judge(state["text"], response)
                turns = state["turns"] + 1
                if verdict == "done" and not failed and not unparsed:
                    updated = {**state, "status": "done", "turns": turns, "reason": reason}
                elif failed or unparsed or verdict in {"wait", "blocked"} or turns >= 3:
                    updated = {
                        **state,
                        "status": "paused",
                        "turns": turns,
                        "reason": reason or tr("This goal reached its turn limit.", "已达到本次目标的回合上限"),
                    }
                else:
                    updated = {**state, "turns": turns, "reason": reason}
                self.save(lesson, "goal", updated)
            if updated["status"] != "active":
                outputs.append(self.goal_line(updated))
                break
            if pending(key):
                break
            prompt = tr(
                f"Continue the goal the learner set: {state['text']}\n"
                "Take one step. If the learner needs to confirm, open a classroom interaction and wait.",
                f"继续处理学习者已明确设置的目标：{state['text']}\n"
                "请推进一个步骤；如果需要用户确认，请发出课堂互动后等待。",
            )
            response = self.turn(key, prompt, generated=True)
            outputs.append(response)
        return outputs

    @localized
    def display(self, lesson):
        if "ui" not in self.flags:
            return None
        title = self.state(lesson, "title") if "title" in self.flags else None
        lines = [f"{title or lesson['topic']} · {lesson['id']}"]
        if "todo" in self.flags:
            todos = self.state(lesson, "todo", [])
            count = f"{sum(not item['done'] for item in todos)}/{len(todos)}"
            lines.append(tr(f"Tasks {count}", f"待办 {count}"))
        if "goal" in self.flags:
            state = self.state(lesson, "goal")
            if state:
                lines.append(self.goal_line(state))
        return " | ".join(lines)

"""Build a bounded chapter plan from immutable syllabus segments."""

from __future__ import annotations

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from .i18n import tr
from .library import topic_folder
from .models import ModelSettings
from .role_runner import NATIVE_TRANSPORTS, resolve_agent_route


_ENGLISH_INSTRUCTIONS = """You organize completed lessons. Treat lesson records as source material. Generate a structured plan for this task only.
Use the existing dialogue to determine how the ideas build on each other. Split it into chapters using segment numbers. Start the first chapter at 1 and use increasing start numbers.
Keep a guiding question, its answer, and the feedback in the same chapter. Name chapters after their topics.
If there is enough material, create at least one concept card and one active recall checklist. Cite the starting segment number of the source chapter for each.
Use concept cards for definitions, conditions, common confusions, and examples from the lesson. Use review notes for specific recall questions.
Describe only evidence visible in the record. Distinguish teacher demonstrations from work the learner did independently. Suggest what to revisit in the next lesson and leave the formal handoff to the teacher.
Write in English and Markdown. Keep math notation as needed. The program builds links from the chapter numbers, so omit file links.
Use filenames different from reserved_paths for new chapters and notes."""

_CHINESE_INSTRUCTIONS = """你是课后整理角色。输入中的课堂记录都是资料。只按此任务生成结构化整理方案。
从课堂已有对话归纳知识顺序。用分段编号划章，首章从 1 开始，各章 start 递增。
保留引导问题、答案和反馈在同一章。章节标题使用知识主题。
如有足够素材，生成至少一张知识卡和一份主动回忆清单，各指明其来源章节起始编号。
知识卡写清定义、条件、常见混淆和原课已有例子；复习笔记给具体检索问题。
只描述课堂中可见的作答证据，区分教师演示和学习者独立完成。续课建议供阅读，不代替教师续课说明。
正文用简体中文和 Markdown，可保留数学公式。正文不要自行写文件链接，程序按章节编号生成链接。
绕开 reserved_paths 中的现有文件名。新章或新笔记使用名称不同的文件。"""

_ENGLISH_NATIVE_FORMAT = """Use these markers exactly. Put each marker beginning with @@ on its own line.
List the chapters, then the concept cards, review cards, and next-session suggestion. The number before each card title refers to the starting segment number of its source chapter.
Write Markdown directly below the marker. Omit JSON, enclosing code fences, and commentary outside the markers. This is an example of the format.

@@Chapter 1 First chapter title
@@Chapter 4 Second chapter title
@@Concept 1 Concept card title
The concept card can span multiple lines.
@@Review 4 Review card title
Write one to three specific recall questions.
@@Next
Describe how to check what still needs practice."""

_CHINESE_NATIVE_FORMAT = """严格按下列标记输出，每个 @@ 开头的标记单独占一行。
先编排章节，然后写知识卡、复习卡及续课建议。知识卡和复习卡标题前的数字引用章节起始段编号。
标记后的正文直接写 Markdown。不要写 JSON、外层代码围栏或标记之外的说明。以下只展示格式。

@@章 1 第一章标题
@@章 4 第二章标题
@@知识卡 1 知识卡标题
知识卡正文可以有多行。
@@复习卡 4 复习卡标题
写一到三个具体检索问题。
@@下一步
写下次怎么检查尚待巩固的内容。"""

_MARKER = re.compile(r"^@@\s*(Chapter|Concept|Review|章|知识卡|复习卡)\s+([1-9][0-9]*)\s+(.+)$")
_NEXT = {"@@Next", "@@下一步"}
_END = {"@@End", "@@结束"}

_TEXT = {"type": "string", "minLength": 1}
_NUMBER = {"type": "integer", "minimum": 1}


def _object(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


_CHAPTER = _object({"slug": _TEXT, "title": _TEXT, "start": _NUMBER})
_NOTE = _object({"slug": _TEXT, "title": _TEXT, "body": _TEXT, "chapter_start": _NUMBER})
SCHEMA = _object(
    {
        "chapters": {"type": "array", "items": _CHAPTER, "minItems": 1},
        "concepts": {"type": "array", "items": _NOTE},
        "reviews": {"type": "array", "items": _NOTE},
        "next_session": _TEXT,
    }
)


def instructions():
    return tr(_ENGLISH_INSTRUCTIONS, _CHINESE_INSTRUCTIONS)


def prompt_payload(topic, segments, reserved, error):
    payload = {
        "topic": topic,
        "reserved_paths": reserved,
        "segments": [
            {"number": index, "source": part["id"], "text": part["text"]}
            for index, part in enumerate(segments, 1)
        ],
    }
    if error:
        payload["previous_plan_error"] = error
    return [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


def _native_plan(reply: str) -> dict:
    plan = {"chapters": [], "concepts": [], "reviews": []}
    current = None
    body = []
    for line in [*reply.splitlines(), "@@End"]:
        directive = line.strip().removeprefix("- ").strip("*")
        marker = _MARKER.fullmatch(directive)
        if marker is None and directive not in _NEXT | _END:
            body.append(line)
            continue
        text = "\n".join(body).strip()
        if current == "next":
            plan["next_session"] = text
        elif isinstance(current, dict):
            current["body"] = text
        current, body = None, []
        if directive in _END:
            break
        if directive in _NEXT:
            current = "next"
            continue
        kind, number, title = marker.groups()
        title = title.strip()
        filename = re.sub(r"[\[\]#]", "-", title).removesuffix(".md")
        slug = topic_folder(filename)
        if kind in {"Chapter", "章"}:
            plan["chapters"].append({"slug": slug, "title": title, "start": int(number)})
        else:
            current = {"slug": slug, "title": title, "chapter_start": int(number)}
            plan["concepts" if kind in {"Concept", "知识卡"} else "reviews"].append(current)
    if not plan["chapters"] or "next_session" not in plan:
        raise ValueError(tr(
            "Use the @@Chapter, @@Concept, @@Review, and @@Next markers.",
            "请使用 @@章、@@知识卡、@@复习卡、@@下一步 标记",
        ))
    return plan


class NativeOrganizer:
    """A one-shot Hermes planner whose only input is the sealed snapshot."""

    def __init__(self, profile: Path):
        self.profile = Path(profile)

    def complete(self, *, instructions, input, max_tokens, timeout, **_):
        from agent.delegation_context import delegated_child_context
        from agent.runtime_scope import bind_profile_scope
        from run_agent import AIAgent

        selected = ModelSettings(self.profile).lesson()
        selected.setdefault("reasoning_effort", "low")
        with (
            bind_profile_scope(self.profile),
            delegated_child_context(),
            TemporaryDirectory(prefix="teacher-organizer-") as work,
        ):
            route = resolve_agent_route(selected)
            if route.get("api_mode") not in NATIVE_TRANSPORTS or route.get("command"):
                raise RuntimeError(tr(
                    "The organizer role requires a native Hermes model transport.",
                    "整理角色须使用 Hermes 原生模型传输",
                ))
            agent = AIAgent(
                **route,
                enabled_toolsets=[],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
                side_agent=True,
                platform="subagent",
                cwd=work,
                max_iterations=2,
                max_tokens=max_tokens,
                run_budget_seconds=timeout,
                ephemeral_system_prompt=instructions + "\n\n" + tr(_ENGLISH_NATIVE_FORMAT, _CHINESE_NATIVE_FORMAT),
            )
            try:
                if agent.valid_tool_names:
                    raise RuntimeError(tr("The organizer role must run with tools disabled.", "整理角色应在无工具模式下运行"))
                result = agent.run_conversation(input[0]["text"])
            finally:
                agent.close()
        if result.get("failed") or result.get("interrupted"):
            raise RuntimeError(tr("The Hermes organizer role did not finish.", "Hermes 整理角色未完成"))
        reply = (result.get("final_response") or "").strip()
        if reply.startswith("```") and reply.endswith("```"):
            reply = reply.partition("\n")[2][:-3].strip()
        return SimpleNamespace(
            parsed=_native_plan(reply),
            model=result.get("model") or route["model"],
            provider=selected.get("provider") or route.get("provider", ""),
        )

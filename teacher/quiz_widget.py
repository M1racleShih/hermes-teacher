"""Bridge Hermes Ink's question widget to the classroom in its active session."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import uuid

from .i18n import get_language, tr, use_profile
from .runtime import Tutor, feedback_confirmation
from .session_links import native_compression_path


def active_session(marker):
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session_id = value.get("session_id") if isinstance(value, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _current_lesson(tutor, session_id):
    lesson = tutor.records.by_session(session_id)
    if not lesson:
        return None
    recorded = lesson["session_id"]
    canonical = tutor.learning.state(lesson["id"]).get("canonical")
    if not canonical or session_id not in (canonical, recorded):
        return None
    if canonical == recorded:
        return lesson

    # The canonical ID remains a valid gateway route across compaction. The
    # physical ID is valid only when both Teacher and Hermes know it as the tip.
    chain = native_compression_path(tutor.records.profile, canonical, forward=True)
    if not chain or chain[0] != canonical or recorded not in chain:
        return None
    if session_id not in (canonical, chain[-1]):
        return None
    for member in chain:
        linked = tutor.records.by_session(member)
        if linked is None and member == canonical:
            return None
        if linked is not None and linked["id"] != lesson["id"]:
            return None
    return lesson


def _options(card, request):
    choices = request.get("options")
    if not isinstance(choices, list) or any(not isinstance(item, str) for item in choices):
        raise ValueError(tr("Select an option first.", "请先选择选项。"))
    indices = {option["id"]: index for index, option in enumerate(card["options"])}
    if (
        len(set(choices)) != len(choices)
        or any(option not in indices for option in choices)
        or (card["mode"] == "single" and len(choices) > 1)
    ):
        raise ValueError(tr("Those options have expired. Refresh the question.", "所选选项已失效，请刷新题面。"))
    return [indices[option] for option in choices]


def _answer(card, request, *, language):
    note = request.get("note", "")
    if not isinstance(note, str) or len(note) > 4096:
        raise ValueError(tr("Input is limited to 4,096 characters.", "输入最多支持 4096 个字符。"))
    if card["kind"] == "ask":
        selected = _options(card, request)
        if not selected and not note.strip():
            raise ValueError(tr("Select an option or enter a response.", "请选择选项或填写文字。"))
        if card["mode"] == "text":
            return note
        if card["mode"] == "single" and selected and note.strip():
            raise ValueError(tr("Select a single option or enter a custom response.", "单选只接受一个已有选项或一段自填内容。"))
        fields = [str(index + 1) for index in selected]
        if note.strip():
            fields.append(tr("other ", "自填 ", language=language) + note)
        return " ".join(fields)
    if request.get("action") == "unknown":
        selected = tr("I don't know", "不知道", language=language)
    else:
        indices = _options(card, request)
        if not indices:
            raise ValueError(tr("Select an option first.", "请先选择选项。"))
        selected = " ".join(chr(ord("A") + index) for index in indices)
    return selected + ("\n" + note if note.strip() else "")


def interact(tutor, marker, request):
    with use_profile(tutor.records.profile):
        return {"language": get_language(), **_interact(tutor, marker, request)}


def _interact(tutor, marker, request):
    session_id = active_session(marker)
    if not session_id or request.get("session_id") != session_id:
        raise ValueError(tr("The active Hermes session changed. Open the classroom panel again.", "Hermes 当前会话已变化，请重新打开课堂面板。"))
    lesson = _current_lesson(tutor, session_id)
    if not lesson:
        return {"classroom": False, "card": None}
    key = lesson["id"]
    card = tutor.learning.interaction_view(key)
    action = request.get("action")
    if action == "read":
        return {"classroom": True, "card": card}
    if not card or card["id"] != request.get("question_id"):
        raise ValueError(tr("This question has ended. Refresh the question.", "这道题已结束，请刷新题面。"))
    if action == "continue" and card["status"] == "feedback":
        text, event_id = feedback_confirmation(tutor.learning, key, card["id"])
    elif card["status"] == "open" and (
        action in {"answer", "cancel"} or card["kind"] == "quiz" and action in {"unknown", "hint"}
    ):
        try:
            event_id = "teacher-ink-" + uuid.UUID(request["request_id"]).hex
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(tr("The answer is missing a valid request ID. Refresh the question.", "缺少有效的作答身份，请刷新题面。")) from None
        language = tutor.learning.request_language(key, event_id)
        if action == "hint":
            text = tr("hint", "提示", language=language)
        elif action == "cancel":
            text = tr("cancel", "取消", language=language)
        else:
            text = _answer(card, request, language=language)
    else:
        raise ValueError(tr("The classroom state changed. Refresh the question.", "课堂状态已变化，请刷新题面。"))
    if active_session(marker) != session_id:
        raise ValueError(tr("The active Hermes session changed. Open the classroom panel again.",
                            "Hermes 当前会话已变化，请重新打开课堂面板。"))
    latest = _current_lesson(tutor, session_id)
    if not latest or latest["id"] != key:
        raise ValueError(tr("The classroom session changed. Open the classroom panel again.", "课堂会话已变化，请重新打开课堂面板。"))
    response = tutor.turn(key, text, expected_id=card["id"], event_id=event_id)
    return {"classroom": True, "card": tutor.learning.interaction_view(key), "message": response}


def main():
    profile = os.environ.get("HERMES_HOME")
    try:
        with use_profile(profile) if profile else nullcontext():
            result = request_result(profile)
    except (OSError, ValueError) as error:
        result = {"error": str(error)}
    print(json.dumps(result, ensure_ascii=False))


def request_result(profile):
    try:
        marker = os.environ.get("HERMES_TUI_ACTIVE_SESSION_FILE")
        request = json.load(sys.stdin)
        if not marker or not profile or not isinstance(request, dict):
            raise ValueError(tr("Open a classroom in this profile with Hermes Ink.", "请用 Hermes Ink 打开本 profile 的课堂。"))
        return {"language": get_language(), **_interact(Tutor(profile), Path(marker), request)}
    except (OSError, RuntimeError, ValueError) as error:
        return {"language": get_language(),
                "error": str(error) or tr("Could not read the classroom state.", "无法读取课堂状态。")}


if __name__ == "__main__":
    main()

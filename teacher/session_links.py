"""Repair Teacher links from Hermes' durable compression ancestry."""

from contextlib import closing

from .i18n import tr


def typed_text(message):
    if isinstance(message, list):
        return "\n".join(
            part["text"]
            for part in message
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return message if isinstance(message, str) else ""


def assistant_text(row):
    # hold_stream hides the tool-call row in Hermes; its normalized content still carries the explanation.
    if (
        row["role"] == "assistant"
        and (row.get("tool_calls") or row.get("display_kind") != "hidden")
        and not row.get("_compressed_summary")
    ):
        return typed_text(row.get("content"))
    return ""


def native_messages(profile, session_id, **options):
    database = profile / "state.db"
    if not database.is_file():
        return []
    from hermes_state import SessionDB

    with closing(SessionDB(db_path=database, read_only=True)) as sessions:
        return sessions.get_messages(session_id, **options)


def native_compression_path(profile, session_id, *, forward=False):
    database = profile / "state.db"
    if not database.is_file():
        return []
    from hermes_state import SessionDB

    with closing(SessionDB(db_path=database, read_only=True)) as sessions:
        if forward:
            return sessions.get_compression_chain(session_id)
        return sessions.get_compression_lineage(session_id)


def unfinished_turn_rows(profile, pending, current_session):
    """Find committed rows after the user admitted for an unfinished Teacher turn."""
    source = pending["session_id"]
    chain = native_compression_path(profile, source, forward=True) or [source]
    if source not in chain:
        return []
    chain = chain[chain.index(source):]
    if current_session not in chain:
        return []
    seen = set()
    captured = []
    started = False
    for session_id in chain:
        for row in native_messages(profile, session_id, include_inactive=True):
            uid = row.get("message_uid")
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            if session_id == source and row["id"] < pending["cursor"]:
                continue
            if row.get("_compressed_summary") or row.get("active") == 0 and not row.get("compacted"):
                continue
            if row["role"] == "user":
                metadata = row.get("display_metadata") or {}
                submission = pending["submission_id"]
                if submission:
                    matches = metadata.get("submission_id") == submission
                else:
                    matches = row.get("timestamp") == pending["user_timestamp"]
                if matches:
                    started = True
                elif started and not metadata.get("compaction_inflight_task_restatement"):
                    # Same-turn compression can inject a synthetic continuation in a new segment.
                    from agent.context_compressor import COMPRESSION_CONTINUATION_USER_CONTENT

                    if session_id == source or row.get("content") != COMPRESSION_CONTINUATION_USER_CONTENT:
                        return captured
                continue
            if started:
                captured.append((session_id, row))
    return captured


def lesson_for_session(records, session_id, *, follow=False):
    """Find an exact link or repair its committed compression path.

    An absent link is inherited only through an actual Hermes compression edge. Desktop
    callers can follow the chain from the already associated physical session while
    retaining the original canonical ID for gateway calls.
    """
    if not session_id:
        return None
    direct = records.by_session(session_id)
    if direct and not follow:
        return direct
    chain = native_compression_path(records.profile, session_id, forward=follow and direct is not None)
    if session_id not in chain or len(chain) < 2:
        return direct
    target = len(chain) - 1 if follow else chain.index(session_id)
    linked = {lesson["id"] for sid in chain[: target + 1] if (lesson := records.by_session(sid)) is not None}
    if not linked:
        return direct
    if len(linked) != 1:
        raise ValueError(tr(
            "The Hermes compression chain points to multiple lessons and cannot resume automatically.",
            "Hermes 压缩链关联了多门课堂，无法自动续接",
        ))
    lesson_id = next(iter(linked))
    with records.lease(lesson_id):
        lesson = records.get(lesson_id)
        if not lesson or lesson["session_id"] not in chain[: target + 1]:
            return direct
        start = chain.index(lesson["session_id"])
        for parent, child in zip(chain[start:target], chain[start + 1 : target + 1]):
            lesson = records.bind_session(lesson_id, child, parent_id=parent)
        if target > start:
            records.pressure(lesson_id, compressed=True, session_id=lesson["session_id"])
        return lesson

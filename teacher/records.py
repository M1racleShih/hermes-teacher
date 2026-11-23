"""Durable classroom identity, branch events, Markdown records, and handoffs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import local
from time import time_ns
from uuid import uuid4

from .i18n import tr
from .library import (
    BusyError,
    LibraryError,
    PENDING_FOLDERS,
    REGISTRY,
    TEMPORARY_TOPICS,
    atomic_write,
    configure_topic_vault,
    file_lease,
    management_root,
    read_bytes,
    read_json,
    safe_path,
    topic_folder,
    topic_vaults,
    validate_topic,
    vault_directory,
    vault_topic,
)

READING_ENTRY_NAMES = ("Start Here.md", "开始阅读.md")
CHECKPOINT_PREFIXES = ("Teacher Handoff-", "教师续课说明-")
NEXT_SESSION_PATHS = ("reviews/Next Session.md", "reviews/下次续课.md")
_PRESSURE_MESSAGES = {
    "compressed": ("The context was compressed", "上下文已压缩"),
    "near-limit": ("Context usage reached 70%", "上下文已达 70%"),
}


class RecordsError(LibraryError):
    """A classroom record operation could not be completed."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    vault TEXT NOT NULL,
    topic TEXT NOT NULL,
    temporary INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    record TEXT,
    recording INTEGER NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    handoff TEXT
);
CREATE INDEX IF NOT EXISTS lessons_scope ON lessons(vault, topic, temporary, updated);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    lesson_id TEXT NOT NULL REFERENCES lessons(id),
    parent_id TEXT REFERENCES sessions(id),
    parent_cut INTEGER,
    notified INTEGER NOT NULL DEFAULT 0,
    notice TEXT
);
CREATE INDEX IF NOT EXISTS sessions_lesson ON sessions(lesson_id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id TEXT NOT NULL REFERENCES lessons(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible INTEGER NOT NULL,
    UNIQUE (lesson_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_branch ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS native_turns (
    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id),
    turn_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    cursor INTEGER NOT NULL,
    user_timestamp REAL NOT NULL,
    submission_id TEXT
);
CREATE TABLE IF NOT EXISTS records (
    lesson_id TEXT NOT NULL REFERENCES lessons(id),
    path TEXT NOT NULL,
    digest TEXT,
    sealed_at INTEGER,
    sealed_session TEXT REFERENCES sessions(id),
    archive TEXT,
    PRIMARY KEY (lesson_id, path)
);
CREATE TABLE IF NOT EXISTS writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id TEXT NOT NULL REFERENCES lessons(id),
    path TEXT NOT NULL,
    before_digest TEXT,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    pending INTEGER NOT NULL,
    turn_id TEXT
);
CREATE TABLE IF NOT EXISTS handoffs (
    lesson_id TEXT NOT NULL REFERENCES lessons(id),
    request_id TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES lessons(id),
    progress TEXT,
    PRIMARY KEY (lesson_id, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS handoffs_target ON handoffs(target_id);
CREATE TABLE IF NOT EXISTS migrations (
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    topic TEXT NOT NULL,
    route_pending INTEGER NOT NULL,
    PRIMARY KEY (kind, source, destination, topic)
);
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def optional_bytes(vault: Path, path: str) -> bytes | None:
    try:
        return read_bytes(vault, path)
    except FileNotFoundError:
        return None


def _digest(data: bytes | None) -> str | None:
    return None if data is None else digest(data)


def _token(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 400 or any(ord(c) < 32 for c in value):
        raise RecordsError(tr(f"Invalid {field}.", f"{field} 无效"))
    return value


def _header(topic: str) -> bytes:
    return f"# {topic}\n\n".encode()


def _block(role: str, text: str, kind: str) -> bytes:
    roles = {
        "user": ("Learner", "学习者"),
        "assistant": ("Teacher", "教师"),
        "teacher": ("Teacher", "教师"),
        "note": ("Note", "备注"),
    }
    kinds = {
        "question": ("Question", "题目"),
        "answer": ("Answer", "作答"),
        "feedback": ("Feedback", "反馈"),
        "note": ("Note", "备注"),
        "image": ("Diagram", "配图"),
    }
    label = tr(*kinds.get(kind, roles.get(role, (role, role))))
    return f"> [!note] {label}\n\n{text}\n\n".encode()


def _is_visible(role: str, kind: str) -> bool:
    return role in {"user", "assistant", "teacher", "note"} and kind not in {"internal", "tool"}


class Records:
    def __init__(self, profile: Path):
        self.profile = Path(profile)
        self.state = vault_directory(self.profile / "teacher", create=True)
        self.db = safe_path(self.state, "records.sqlite3")
        self._leases = local()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        additions = (
            ("handoffs", "progress"),
            ("checkpoints", "turn_id"),
            ("sessions", "notice"),
        )
        for table, column in additions:
            with self._connect() as connection:
                if self._has_column(connection, table, column):
                    continue
            with self._connect(write=True) as connection:
                if not self._has_column(connection, table, column):
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    @contextmanager
    def _connect(self, *, write: bool = False):
        safe_path(self.state, self.db.name)
        connection = sqlite3.connect(self.db, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _has_column(connection, table, column):
        return any(row["name"] == column for row in connection.execute(f"PRAGMA table_info({table})"))

    @contextmanager
    def catalog_lease(self):
        with file_lease(self.state, "locks/catalog.lock"):
            yield

    @contextmanager
    def turn_lease(self, lesson_id: str):
        key = _token(lesson_id, tr("lesson ID", "课堂 ID"))
        if not key.isalnum():
            raise RecordsError(tr("Invalid lesson ID.", "课堂 ID 无效"))
        try:
            with file_lease(self.state, f"turns/{key}.lock"):
                yield
        except BusyError as error:
            raise BusyError(tr(
                f"A turn is already running for lesson {lesson_id}.",
                f"课堂已有写者，回合正在运行：{lesson_id}",
            )) from error

    def _recover_routes(self):
        with self._connect() as connection:
            pending = connection.execute("SELECT * FROM migrations WHERE route_pending = 1").fetchall()
        for move in pending:
            root, target = Path(move["source"]), Path(move["destination"])
            configure_topic_vault(root, move["topic"], target.relative_to(root))
            with self._connect(write=True) as connection:
                connection.execute(
                    """UPDATE migrations SET route_pending = 0
                       WHERE kind = ? AND source = ? AND destination = ? AND topic = ?""",
                    (move["kind"], move["source"], move["destination"], move["topic"]),
                )

    def _relocated(self, vault: Path) -> Path:
        with self._connect() as connection:
            moves = sorted(
                (
                    (Path(row["source"]), Path(row["destination"]))
                    for row in connection.execute(
                        "SELECT source, destination FROM migrations WHERE kind = 'root'"
                    )
                ),
                key=lambda pair: len(pair[0].parts),
                reverse=True,
            )
        visited = set()
        while vault not in visited:
            visited.add(vault)
            for source, target in moves:
                if vault == source or source in vault.parents:
                    vault = target / vault.relative_to(source)
                    break
            else:
                return vault
        raise RecordsError(tr("The library relocation map contains a cycle.", "管理根的搬迁关联存在循环"))

    @contextmanager
    def lease(self, lesson_id: str):
        """Acquire a non-blocking writer lease, reentrant on this instance and thread."""
        key = digest(_token(lesson_id, tr("lesson ID", "课堂 ID")).encode())
        held = self._leases.__dict__.setdefault("held", set())
        if key in held:
            yield
            return
        try:
            with file_lease(self.state, f"locks/{key}.lock"):
                held.add(key)
                try:
                    yield
                finally:
                    held.remove(key)
        except BusyError as error:
            raise BusyError(tr(
                f"Another process is writing to lesson {lesson_id}.",
                f"课堂已有写者：{lesson_id}",
            )) from error

    @staticmethod
    def _lesson(connection, lesson_id: str):
        row = connection.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            raise RecordsError(tr(f"Lesson not found: {lesson_id}", f"找不到课堂：{lesson_id}"))
        return row

    @staticmethod
    def _public(row, *, preview: str = "") -> dict:
        data = dict(row)
        if data["record"]:
            data["record"] = str(Path(data["vault"]) / data["record"])
        data["recording"] = bool(data["recording"])
        data["temporary"] = bool(data["temporary"])
        data["preview"] = preview
        return data

    def get(self, lesson_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
            return None if row is None else self._public(row)

    def by_session(self, session_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lessons.* FROM lessons JOIN sessions ON sessions.lesson_id = lessons.id WHERE sessions.id = ?",
                (session_id,),
            ).fetchone()
            return None if row is None else self._public(row)

    @staticmethod
    def _active_path(vault: Path, topic: str, lesson_id: str, temporary: bool) -> str:
        if temporary:
            folder = tr(*TEMPORARY_TOPICS)
        elif vault_topic(vault) == topic:
            folder = tr(*PENDING_FOLDERS)
        else:
            folder = topic_folder(topic)
        stamp = datetime.now().strftime("%Y-%m-%d")
        return f"lessons/{folder}/{stamp}-{lesson_id[:8]}-{uuid4().hex[:10]}.md"

    def _new_lesson(
        self,
        connection,
        vault: Path,
        topic: str,
        temporary: bool,
        *,
        recording: bool = True,
        handoff: str | None = None,
    ):
        lesson_id = uuid4().hex
        session_id = uuid4().hex
        record = self._active_path(vault, topic, lesson_id, temporary) if recording else None
        now = time_ns()
        connection.execute(
            "INSERT INTO lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lesson_id, str(vault), topic, temporary, session_id, record, recording, now, now, handoff),
        )
        connection.execute("INSERT INTO sessions(id, lesson_id) VALUES (?, ?)", (session_id, lesson_id))
        if record:
            connection.execute("INSERT INTO records(lesson_id, path) VALUES (?, ?)", (lesson_id, record))
        return self._lesson(connection, lesson_id)

    def prepare(
        self,
        vault: Path,
        topic: str | None,
        new: bool = False,
        *,
        create: bool = False,
        temporary: bool = False,
        report_creation: bool = False,
    ) -> dict:
        title = validate_topic(topic) if topic is not None else tr(*TEMPORARY_TOPICS)
        if topic is None and not temporary:
            raise RecordsError(tr(
                "Choose a topic or explicitly start a temporary discussion.",
                "请先选择主题，或明确开始临时讨论",
            ))
        with self.catalog_lease():
            self._recover_routes()
            chosen = vault_directory(self._relocated(Path(vault)), create=create)
            root = management_root(chosen)
            selected = root
            mapped = topic_vaults(root)
            if not temporary:
                if title in mapped:
                    selected = mapped[title]
                else:
                    marked = vault_topic(root)
                    if marked and marked != title:
                        raise RecordsError(tr(
                            f"The standalone library belongs to a different topic: {marked}",
                            f"独立资料库属于其他主题：{marked}",
                        ))
                    selected = root
                    with self._connect() as connection:
                        legacy = connection.execute(
                            "SELECT 1 FROM lessons WHERE vault = ? AND topic = ? AND temporary = 0",
                            (str(root), title),
                        ).fetchone()
                    if safe_path(root, REGISTRY).exists() and not legacy:
                        selected = configure_topic_vault(root, title, create=True)
            with self._connect(write=True) as connection:
                row = None
                if not new:
                    titles = TEMPORARY_TOPICS if topic is None else (title, title)
                    row = connection.execute(
                        """SELECT * FROM lessons WHERE vault = ? AND topic IN (?, ?) AND temporary = ?
                           ORDER BY updated DESC, id DESC LIMIT 1""",
                        (str(selected), *titles, temporary),
                    ).fetchone()
                if row is None:
                    created = self._public(self._new_lesson(connection, selected, title, temporary))
                    return {**created, "newly_created": True} if report_creation else created
            with self.lease(row["id"]):
                self._flush(row["id"])
            found = self.get(row["id"])
            return {**found, "newly_created": False} if report_creation else found

    def visit(self, lesson_id: str):
        """Remember selection even when the learner leaves without sending a message."""
        with self._connect(write=True) as connection:
            self._lesson(connection, lesson_id)
            connection.execute("UPDATE lessons SET updated = ? WHERE id = ?", (time_ns(), lesson_id))

    def recent(self, vault: Path | None = None) -> list[dict]:
        chosen = vault_directory(self._relocated(Path(vault))) if vault is not None else None
        mapped = topic_vaults(chosen) if chosen else {}
        owners = {path: topic for topic, path in mapped.items()}
        if chosen and (topic := vault_topic(chosen)):
            owners[chosen] = topic
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM lessons ORDER BY updated DESC, id DESC")
            result = []
            for row in rows:
                location = Path(row["vault"])
                if chosen:
                    if location in owners:
                        if row["temporary"] or row["topic"] != owners[location]:
                            continue
                    elif location != chosen or row["topic"] in mapped:
                        continue
                elif not location.is_dir():
                    continue
                last = next(
                    (
                        event["text"]
                        for event in reversed(self._events(connection, row))
                        if event["role"] == "user" and event["visible"]
                    ),
                    "",
                )
                result.append(self._public(row, preview=last))
            return result

    def search(self, vault: Path, query: str = "") -> list[dict]:
        terms = query.casefold().split()
        return [
            item
            for item in self.recent(vault)
            if all(term in (item["topic"] + "\n" + item["preview"]).casefold() for term in terms)
        ]

    @staticmethod
    def _session(connection, lesson, session_id: str | None = None):
        requested = lesson["session_id"] if session_id is None else session_id
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (requested,)).fetchone()
        if row is None or row["lesson_id"] != lesson["id"]:
            raise RecordsError(tr("The session does not belong to this lesson.", "会话未关联到当前课堂"))
        return row

    def bind_session(self, lesson_id: str, session_id: str, *, parent_id: str | None = None) -> dict:
        session_id = _token(session_id, tr("session ID", "会话 ID"))
        with self.lease(lesson_id):
            self._flush(lesson_id)
            with self._connect(write=True) as connection:
                row = self._lesson(connection, lesson_id)
                existing = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if existing:
                    if existing["lesson_id"] != lesson_id or (
                        parent_id is not None and existing["parent_id"] != parent_id
                    ):
                        raise RecordsError(tr(
                            "The session is already linked to another lesson or parent session and cannot be reassigned.",
                            "会话已有其他课堂或父会话，无法重新绑定",
                        ))
                else:
                    cutoff = None
                    if parent_id is not None:
                        self._session(connection, row, parent_id)
                        cutoff = connection.execute(
                            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
                            (parent_id,),
                        ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO sessions(id, lesson_id, parent_id, parent_cut) VALUES (?, ?, ?, ?)",
                        (session_id, lesson_id, parent_id, cutoff),
                    )
                connection.execute(
                    "UPDATE lessons SET session_id = ?, updated = ? WHERE id = ?",
                    (session_id, time_ns(), lesson_id),
                )
                return self._public(self._lesson(connection, lesson_id))

    def _events(self, connection, lesson, session_id: str | None = None) -> list[sqlite3.Row]:
        current = self._session(connection, lesson, session_id)
        branch = [(current["id"], None)]
        visited = {current["id"]}
        while current["parent_id"] is not None:
            if current["parent_id"] in visited:
                raise RecordsError(tr("The session branch has a damaged parent link.", "会话分支的父级关系已损坏"))
            visited.add(current["parent_id"])
            branch.append((current["parent_id"], current["parent_cut"]))
            current = self._session(connection, lesson, current["parent_id"])
        events = []
        for current_id, cutoff in reversed(branch):
            sql = "SELECT * FROM events WHERE session_id = ?"
            params = (current_id,)
            if cutoff is not None:
                sql += " AND seq <= ?"
                params += (cutoff,)
            events.extend(connection.execute(sql + " ORDER BY seq", params))
        return events

    def history(
        self, lesson_id: str, *, session_id: str | None = None, include_hidden: bool = False
    ) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(event)
                for event in self._events(connection, self._lesson(connection, lesson_id), session_id)
                if event["visible"] or include_hidden
            ]

    def _queue(self, connection, lesson, path: str, before: bytes | None, after: bytes) -> None:
        connection.execute(
            "INSERT INTO writes(lesson_id, path, before_digest, data) VALUES (?, ?, ?, ?)",
            (lesson["id"], path, _digest(before), after),
        )

    def _flush(self, lesson_id: str) -> None:
        with self._connect() as connection:
            lesson = self._lesson(connection, lesson_id)
            pending = list(
                connection.execute("SELECT * FROM writes WHERE lesson_id = ? ORDER BY id", (lesson_id,))
            )
        vault = Path(lesson["vault"])
        for job in pending:
            current = _digest(optional_bytes(vault, job["path"]))
            if current != digest(job["data"]):
                if current != job["before_digest"]:
                    raise RecordsError(tr(
                        f"The record file was edited during a write: {job['path']}",
                        f"记录文件在写入期间发生外部修改：{job['path']}",
                    ))
                atomic_write(vault, job["path"], job["data"], replace=current is not None)
            with self._connect(write=True) as connection:
                connection.execute("DELETE FROM writes WHERE id = ?", (job["id"],))

    def begin_native_turn(self, lesson_id, turn_id, session_id, cursor, user_message):
        timestamp = user_message.get("timestamp")
        if user_message.get("role") != "user" or not isinstance(timestamp, (int, float)):
            return
        submission_id = (user_message.get("display_metadata") or {}).get("submission_id")
        with self._connect(write=True) as connection:
            connection.execute(
                "INSERT INTO native_turns VALUES (?, ?, ?, ?, ?, ?)",
                (lesson_id, turn_id, session_id, cursor, timestamp, submission_id),
            )

    def end_native_turn(self, lesson_id, turn_id):
        with self._connect(write=True) as connection:
            connection.execute(
                "DELETE FROM native_turns WHERE lesson_id = ? AND turn_id = ?", (lesson_id, turn_id)
            )

    def recover(self, lesson_id: str) -> None:
        with self.turn_lease(lesson_id):
            self.recover_pending_turn(lesson_id)

    def recover_pending_turn(self, lesson_id: str) -> None:
        """Recover the abandoned native turn while holding the classroom turn lease."""
        from .session_links import assistant_text, unfinished_turn_rows

        with self.lease(lesson_id):
            self._flush(lesson_id)
            with self._connect() as connection:
                pending = connection.execute(
                    "SELECT * FROM native_turns WHERE lesson_id = ?", (lesson_id,)
                ).fetchone()
                if pending is None:
                    return
                session_id = self._lesson(connection, lesson_id)["session_id"]
                turn_id = pending["turn_id"]
                reply_id = "reply:" + turn_id
                existing = {
                    row["event_id"] for row in connection.execute(
                        "SELECT event_id FROM events WHERE lesson_id = ?", (lesson_id,)
                    )
                }
            if reply_id not in existing:
                rows = unfinished_turn_rows(self.profile, pending, session_id)
                for index, (source, row) in enumerate(rows):
                    text = assistant_text(row)
                    if not text.strip():
                        continue
                    event_id = f"native:{source}:{row['id']}"
                    if index == len(rows) - 1 and not row.get("tool_calls") and row.get("finish_reason") != "incomplete":
                        event_id = reply_id
                    if event_id not in existing:
                        self.append(lesson_id, "assistant", text, event_id, session_id=session_id)
                self.append(lesson_id, "system", "interrupted", "end:" + turn_id, session_id=session_id, kind="internal")
            self.end_native_turn(lesson_id, turn_id)

    def append(
        self,
        lesson_id: str,
        role: str,
        text: str,
        event_id: str,
        *,
        session_id: str | None = None,
        kind: str = "message",
        completed_checkpoint_turn: str | None = None,
    ) -> bool:
        event_id = _token(event_id, tr("event ID", "事件 ID"))
        role = _token(role, tr("event role", "事件角色"))
        kind = _token(kind, tr("event type", "事件类型"))
        if not isinstance(text, str):
            raise RecordsError(tr("Event content must be text.", "事件正文必须是文本"))
        visible = _is_visible(role, kind)
        if visible and not text.strip():
            return False
        with self.lease(lesson_id):
            self._flush(lesson_id)
            with self._connect(write=True) as connection:
                lesson = self._lesson(connection, lesson_id)
                session = self._session(connection, lesson, session_id)
                existing = connection.execute(
                    "SELECT session_id, role, text, kind FROM events WHERE lesson_id = ? AND event_id = ?",
                    (lesson_id, event_id),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (session["id"], role, text, kind):
                        raise RecordsError(tr(
                            f"Event ID already belongs to other content: {event_id}",
                            f"事件 ID 已关联其他内容：{event_id}",
                        ))
                    return False
                checkpoint = None
                if (
                    visible and role == "assistant" and kind == "message"
                    and completed_checkpoint_turn is not None
                    and event_id == "reply:" + completed_checkpoint_turn
                    and session["id"] == lesson["session_id"]
                ):
                    checkpoint = connection.execute(
                        """SELECT * FROM checkpoints
                           WHERE lesson_id = ? AND session_id = ? AND turn_id = ? AND pending = 1""",
                        (lesson_id, session["id"], completed_checkpoint_turn),
                    ).fetchone()
                    if checkpoint is not None and (
                        checkpoint["fingerprint"] != self._fingerprint(connection, lesson, session["id"], hidden=True)
                        or _digest(optional_bytes(Path(lesson["vault"]), checkpoint["path"])) != checkpoint["digest"]
                    ):
                        checkpoint = None
                connection.execute(
                    """INSERT INTO events(lesson_id, session_id, event_id, role, text, kind, visible)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (lesson_id, session["id"], event_id, role, text, kind, visible),
                )
                if checkpoint is not None:
                    connection.execute(
                        "UPDATE checkpoints SET fingerprint = ?, pending = 0 WHERE lesson_id = ?",
                        (self._fingerprint(connection, lesson, session["id"], hidden=True), lesson_id),
                    )
                if visible and lesson["session_id"] == session["id"]:
                    connection.execute("UPDATE lessons SET updated = ? WHERE id = ?", (time_ns(), lesson_id))
                if visible and lesson["recording"] and lesson["session_id"] == session["id"]:
                    record = lesson["record"]
                    vault = Path(lesson["vault"])
                    before = optional_bytes(vault, record)
                    entry = connection.execute(
                        "SELECT digest FROM records WHERE lesson_id = ? AND path = ?",
                        (lesson_id, record),
                    ).fetchone()
                    if entry is None or entry["digest"] != _digest(before):
                        raise RecordsError(tr(
                            f"The active lesson record has changed: {record}",
                            f"活动课堂记录已被修改：{record}",
                        ))
                    after = (before if before is not None else _header(lesson["topic"])) + _block(
                        role, text, kind
                    )
                    self._queue(connection, lesson, record, before, after)
                    connection.execute(
                        "UPDATE records SET digest = ? WHERE lesson_id = ? AND path = ?",
                        (digest(after), lesson_id, record),
                    )
            self._flush(lesson_id)
            return True

    def set_recording(self, lesson_id: str, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise RecordsError(tr("Recording must be set to a Boolean value.", "录入开关必须为布尔值"))
        with self.turn_lease(lesson_id), self.lease(lesson_id):
            self.recover_pending_turn(lesson_id)
            with self._connect(write=True) as connection:
                lesson = self._lesson(connection, lesson_id)
                record = lesson["record"]
                if enabled and not record:
                    record = self._active_path(
                        Path(lesson["vault"]), lesson["topic"], lesson_id, bool(lesson["temporary"])
                    )
                    connection.execute(
                        "INSERT INTO records(lesson_id, path) VALUES (?, ?)", (lesson_id, record)
                    )
                connection.execute(
                    "UPDATE lessons SET recording = ?, record = ? WHERE id = ?", (enabled, record, lesson_id)
                )
                return self._public(self._lesson(connection, lesson_id))

    def bind_record(self, lesson_id: str, path: str | Path, *, session_id: str | None = None) -> dict:
        with self.turn_lease(lesson_id), self.lease(lesson_id):
            self.recover_pending_turn(lesson_id)
            with self._connect(write=True) as connection:
                lesson = self._lesson(connection, lesson_id)
                if session_id is not None and session_id != lesson["session_id"]:
                    raise RecordsError(tr(
                        "Only the active session branch can be linked to the lesson record.",
                        "只能将当前活动分支绑定到课堂记录",
                    ))
                vault = Path(lesson["vault"])
                path = Path(path)
                if path.is_absolute():
                    try:
                        path = path.relative_to(vault)
                    except ValueError:
                        raise RecordsError(tr(
                            "The export file must be inside the current library.",
                            "导出文件必须在当前资料库内",
                        )) from None
                relative = path.as_posix()
                if path.suffix.lower() != ".md" or any(piece.startswith(".") for piece in path.parts):
                    raise RecordsError(tr(
                        "Choose an existing Markdown file in the library dedicated to exports.",
                        "请选择资料库内专用于导出的现有 Markdown 文件",
                    ))
                safe_path(vault, relative, must_exist=True)
                previous = connection.execute(
                    """SELECT records.* FROM records JOIN lessons ON records.lesson_id = lessons.id
                       WHERE lessons.vault = ? AND records.path = ?""",
                    (str(vault), relative),
                ).fetchone()
                if previous is not None and (
                    previous["sealed_at"] is not None or previous["lesson_id"] != lesson_id
                ):
                    raise RecordsError(tr(
                        "Sealed records and records from other lessons cannot be reassigned.",
                        "封存记录或其他课堂的记录不能重新绑定",
                    ))
                try:
                    ledger = read_json(vault, ".teacher-organizer/ledger.json")
                except FileNotFoundError:
                    ledger = {}
                if (
                    not isinstance(ledger, dict)
                    or not isinstance(ledger.get("outputs", {}), dict)
                    or relative in ledger.get("outputs", {})
                    or relative in READING_ENTRY_NAMES
                    or relative in NEXT_SESSION_PATHS
                    or path.name.startswith(CHECKPOINT_PREFIXES)
                ):
                    raise RecordsError(tr(
                        "Organized study notes cannot be used as recording targets.",
                        "已整理的学习笔记不能作为录入目标",
                    ))
                events = self._events(connection, lesson, session_id)
                before = read_bytes(vault, relative)
                after = _header(lesson["topic"]) + b"".join(
                    _block(event["role"], event["text"], event["kind"])
                    for event in events
                    if event["visible"]
                )
                self._queue(connection, lesson, relative, before, after)
                connection.execute(
                    """INSERT INTO records(lesson_id, path, digest) VALUES (?, ?, ?)
                       ON CONFLICT(lesson_id, path) DO UPDATE SET digest = excluded.digest""",
                    (lesson_id, relative, digest(after)),
                )
                connection.execute(
                    "UPDATE lessons SET recording = 1, record = ? WHERE id = ?", (relative, lesson_id)
                )
            self._flush(lesson_id)
            return self.get(lesson_id)

    def _seal(self, connection, lesson, *, rotate: bool) -> dict:
        record = lesson["record"]
        result = {"status": "empty" if lesson["recording"] else "disabled"}
        if record is None:
            return result
        vault = Path(lesson["vault"])
        current = optional_bytes(vault, record)
        row = connection.execute(
            "SELECT * FROM records WHERE lesson_id = ? AND path = ?",
            (lesson["id"], record),
        ).fetchone()
        if row is None or row["sealed_at"] is not None or row["digest"] != _digest(current):
            raise RecordsError(tr(
                "The current record is sealed or has changed, so a seal receipt cannot be created.",
                "当前记录已经封存或被修改，无法生成封存回执",
            ))
        if current is None or current.strip() in {b"", _header(lesson["topic"]).strip()}:
            return result
        connection.execute(
            "UPDATE records SET sealed_at = ?, sealed_session = ? WHERE lesson_id = ? AND path = ?",
            (time_ns(), lesson["session_id"], lesson["id"], record),
        )
        fresh = None
        if lesson["recording"] and rotate:
            fresh = self._active_path(vault, lesson["topic"], lesson["id"], bool(lesson["temporary"]))
            header = _header(lesson["topic"])
            connection.execute(
                "INSERT INTO records(lesson_id, path, digest) VALUES (?, ?, ?)",
                (lesson["id"], fresh, digest(header)),
            )
            self._queue(connection, lesson, fresh, None, header)
        connection.execute("UPDATE lessons SET record = ? WHERE id = ?", (fresh, lesson["id"]))
        return {
            "status": "sealed",
            "file": str(vault / record),
            "digest": row["digest"],
            "session_id": lesson["session_id"],
        }

    def seal(self, lesson_id: str, *, busy: bool = False) -> dict:
        if busy:
            raise BusyError(tr(
                "Wait for the current turn or role task to finish before sealing the lesson.",
                "请等待当前问答或协作任务结束，再封存课堂",
            ))
        with self.turn_lease(lesson_id), self.lease(lesson_id):
            self.recover_pending_turn(lesson_id)
            with self._connect(write=True) as connection:
                result = self._seal(connection, self._lesson(connection, lesson_id), rotate=True)
            self._flush(lesson_id)
            return result

    def sealed(self, vault: Path) -> list[dict]:
        vault = vault_directory(vault)
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT records.*, lessons.topic FROM records JOIN lessons ON records.lesson_id = lessons.id
                   WHERE lessons.vault = ? AND records.sealed_at IS NOT NULL ORDER BY records.sealed_at, records.path""",
                    (str(vault),),
                )
            ]

    def _fingerprint(self, connection, lesson, session_id: str, *, hidden: bool = False) -> str:
        events = [
            (event["event_id"], event["role"], event["text"], event["kind"])
            for event in self._events(connection, lesson, session_id)
            if event["visible"] or hidden
        ]
        data = json.dumps(events, ensure_ascii=False).encode()
        return digest(data)

    def checkpoint(
        self, lesson_id: str, text: str, *, session_id: str | None = None, turn_id: str | None = None,
    ) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise RecordsError(tr(
                "Add a handoff note with learning evidence and next steps.",
                "请提供包含学习证据和下一步的续课说明",
            ))
        with self.lease(lesson_id):
            self._flush(lesson_id)
            with self._connect(write=True) as connection:
                lesson = self._lesson(connection, lesson_id)
                session = self._session(connection, lesson, session_id)
                if lesson["temporary"] or lesson["session_id"] != session["id"]:
                    raise RecordsError(tr(
                        "A teacher handoff can only be saved for the active lesson on this topic.",
                        "只能为当前主题课堂保存教师续课说明",
                    ))
                vault = Path(lesson["vault"])
                saved = connection.execute(
                    "SELECT path FROM checkpoints WHERE lesson_id = ?", (lesson_id,)
                ).fetchone()
                path = saved["path"] if saved else f"reviews/{tr(*CHECKPOINT_PREFIXES)}{lesson_id[:12]}.md"
                data = text.encode()
                self._queue(connection, lesson, path, optional_bytes(vault, path), data)
                connection.execute(
                    """INSERT INTO checkpoints(lesson_id, session_id, path, digest, fingerprint, pending, turn_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_id) DO UPDATE SET session_id = excluded.session_id, path = excluded.path,
                       digest = excluded.digest, fingerprint = excluded.fingerprint, pending = excluded.pending,
                       turn_id = excluded.turn_id""",
                    (
                        lesson_id,
                        session["id"],
                        path,
                        digest(data),
                        self._fingerprint(connection, lesson, session["id"], hidden=turn_id is not None),
                        turn_id is not None,
                        turn_id,
                    ),
                )
            self._flush(lesson_id)
            return {
                "text": text,
                "file": str(vault / path),
                "path": str(vault / path),
                "session_id": session["id"],
                "pending": turn_id is not None,
            }

    def _read_checkpoint(self, connection, lesson, session_id: str | None = None) -> dict:
        session = self._session(connection, lesson, session_id)
        saved = connection.execute(
            "SELECT * FROM checkpoints WHERE lesson_id = ?", (lesson["id"],)
        ).fetchone()
        if saved is None:
            raise RecordsError(tr(
                "Ask the teacher to save a handoff note for this lesson first.",
                "当前课堂没有续课说明，请先让教师保存",
            ))
        if (
            saved["pending"]
            or saved["session_id"] != session["id"]
            or session["id"] != lesson["session_id"]
            or saved["fingerprint"] != self._fingerprint(
                connection, lesson, session["id"], hidden=saved["turn_id"] is not None
            )
        ):
            raise RecordsError(tr(
                "The current session's handoff note is pending or out of date. Ask the teacher to update it.",
                "续课说明在当前会话尚未生效或已过期，请先让教师更新",
            ))
        vault = Path(lesson["vault"])
        if lesson["record"]:
            recorded = connection.execute(
                "SELECT digest FROM records WHERE lesson_id = ? AND path = ?", (lesson["id"], lesson["record"])
            ).fetchone()
            if recorded is None or _digest(optional_bytes(vault, lesson["record"])) != recorded["digest"]:
                raise RecordsError(tr(
                    "The lesson transcript has changed. Check the record and update the handoff note.",
                    "当前课堂原文已修改，请先核对记录并更新续课说明",
                ))
        current = optional_bytes(vault, saved["path"])
        if _digest(current) != saved["digest"]:
            raise RecordsError(tr(
                "The handoff note is missing or has changed. Ask the teacher to update it.",
                "续课说明文件缺失或已修改，请先让教师更新",
            ))
        return {
            "text": current.decode(),
            "file": str(vault / saved["path"]),
            "session_id": session["id"],
            "digest": saved["digest"],
            "fingerprint": saved["fingerprint"],
        }

    def read_checkpoint(self, lesson_id: str, *, session_id: str | None = None) -> dict:
        with self._connect() as connection:
            saved = self._read_checkpoint(connection, self._lesson(connection, lesson_id), session_id)
            return {key: saved[key] for key in ("text", "file", "session_id")}

    @staticmethod
    def _handoff_target(connection, lesson_id, request_id):
        return connection.execute(
            """SELECT lessons.* FROM handoffs JOIN lessons ON lessons.id = handoffs.target_id
               WHERE handoffs.lesson_id = ? AND handoffs.request_id = ?""",
            (lesson_id, request_id),
        ).fetchone()

    def incoming_handoff(self, lesson_id: str) -> dict | None:
        """Find the immutable starting progress linked to a handoff's target."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lesson_id, progress FROM handoffs WHERE target_id = ?", (lesson_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row["lesson_id"],
            "progress": json.loads(row["progress"]) if row["progress"] else None,
        }

    def has_learner_input(self, lesson_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM events WHERE lesson_id = ? AND role = 'user' AND visible = 1 LIMIT 1",
                    (lesson_id,),
                ).fetchone()
                is not None
            )

    def handoff(
        self,
        lesson_id: str,
        *,
        confirmed: bool = False,
        busy: bool = False,
        session_id: str | None = None,
        request_id: str | None = None,
        capture_progress=None,
    ) -> dict:
        if not confirmed:
            raise RecordsError(tr("Switching sessions was cancelled.", "已取消换会话"))
        if busy:
            raise BusyError(tr(
                "Wait for the current turn or role task to finish before starting a new session.",
                "请等待当前问答或协作任务结束，再开始新会话",
            ))
        with self.catalog_lease(), self.turn_lease(lesson_id), self.lease(lesson_id):
            self.recover_pending_turn(lesson_id)
            with self._connect(write=True) as connection:
                lesson = self._lesson(connection, lesson_id)
                if request_id is not None:
                    request_id = _token(request_id, tr("handoff request ID", "交接请求 ID"))
                    existing = self._handoff_target(connection, lesson_id, request_id)
                    if existing is not None:
                        return self._public(existing)
                checkpoint = self._read_checkpoint(connection, lesson, session_id)
                if request_id is None:
                    identity = [checkpoint[key] for key in ("session_id", "digest", "fingerprint")]
                    request_id = "checkpoint-v1:" + digest(json.dumps(identity).encode())
                    existing = self._handoff_target(connection, lesson_id, request_id)
                    if existing is not None:
                        return self._public(existing)
                    legacy = self._handoff_target(connection, lesson_id, checkpoint["session_id"])
                    # Older handoffs recorded the session alone. Visible input and session
                    # changes advance the source timestamp even when the note text is unchanged.
                    if (
                        legacy is not None
                        and legacy["handoff"] == checkpoint["text"]
                        and lesson["updated"] <= legacy["created"]
                    ):
                        return self._public(legacy)
                progress = json.dumps(capture_progress(), ensure_ascii=False) if capture_progress else None
                self._seal(connection, lesson, rotate=True)
                target = self._new_lesson(
                    connection,
                    Path(lesson["vault"]),
                    lesson["topic"],
                    False,
                    recording=bool(lesson["recording"]),
                    handoff=checkpoint["text"],
                )
                connection.execute(
                    "INSERT INTO handoffs(lesson_id, request_id, target_id, progress) VALUES (?, ?, ?, ?)",
                    (lesson_id, request_id, target["id"], progress),
                )
            self._flush(lesson_id)
            return self._public(target)

    def pressure(
        self,
        lesson_id: str,
        *,
        used: float = 0,
        limit: float = 0,
        compressed: bool = False,
        session_id: str | None = None,
    ) -> bool:
        if not compressed and (limit <= 0 or used / limit < 0.7):
            return False
        with self.lease(lesson_id), self._connect(write=True) as connection:
            lesson = self._lesson(connection, lesson_id)
            if lesson["temporary"] or vault_topic(Path(lesson["vault"])) != lesson["topic"]:
                return False
            session = self._session(connection, lesson, session_id)
            if session["id"] != lesson["session_id"]:
                return False
            if connection.execute(
                "SELECT 1 FROM sessions WHERE lesson_id = ? AND notified = 1 LIMIT 1", (lesson_id,)
            ).fetchone():
                return False
            reason = "compressed" if compressed else "near-limit"
            connection.execute("UPDATE sessions SET notified = 1, notice = ? WHERE id = ?", (reason, session["id"]))
            return True

    def take_pressure_notice(self, lesson_id: str, *, session_id: str) -> str:
        with self.lease(lesson_id), self._connect(write=True) as connection:
            lesson = self._lesson(connection, lesson_id)
            if lesson["session_id"] != session_id:
                return ""
            pending = connection.execute(
                "SELECT id, notice FROM sessions WHERE lesson_id = ? AND notice IS NOT NULL", (lesson_id,)
            ).fetchone()
            if pending is None:
                return ""
            connection.execute("UPDATE sessions SET notice = NULL WHERE id = ?", (pending["id"],))
            notice = pending["notice"]
            labels = _PRESSURE_MESSAGES.get(notice)
            if labels is None:
                labels = next((pair for pair in _PRESSURE_MESSAGES.values() if notice in pair), (notice, notice))
            reason = tr(*labels)
            return tr(
                f"{reason}. Ask the teacher to save a handoff note, then use /lesson-next to start a new session.",
                f"{reason}。可请老师保存续课说明，再用 /lesson-next 开启新会话。",
            )

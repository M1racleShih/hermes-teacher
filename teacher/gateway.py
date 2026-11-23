"""Private Feishu classroom admission, durable delivery and native adapter entry."""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import sys
import tempfile
import time
from types import SimpleNamespace

from .i18n import localized, profile_argument, tr, use_language, use_profile
from .library import BusyError, LibraryError, atomic_write, file_lease, read_bytes, vault_topic
from .records import CHECKPOINT_PREFIXES, Records
from .runtime import SettledTurnError, handoff_message


MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024
MAX_INLINE = 1024 * 1024


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class MobileSettings:
    sender: str
    chat: str
    vault: Path
    platform: str = "feishu"

    def __post_init__(self):
        if not self.sender.strip() or not self.chat.strip():
            raise GatewayError(tr("The mobile classroom requires your user ID and private chat ID.", "手机入口需要本人身份与私聊 ID"))
        if not self.vault.is_absolute() or not self.vault.is_dir():
            raise GatewayError(tr("The mobile classroom requires an existing library at an absolute path.", "手机入口需要明确指定已存在的资料库"))


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def owned_file(root, relative, limit):
    """Read one stable, unlinked file through directory descriptors."""
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise GatewayError(tr("The attachment path is outside the allowed directory.", "附件路径越出允许目录"))
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for directory in path.parts[:-1]:
            child = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(source, "rb") as stream:
            first = os.fstat(stream.fileno())
            if not stat.S_ISREG(first.st_mode) or first.st_nlink != 1 or first.st_uid != os.getuid():
                raise GatewayError(tr(
                    "An attachment must be a regular file owned by the current user with one hard link.",
                    "附件必须是当前用户拥有的单链接常规文件",
                ))
            if first.st_size > limit:
                raise GatewayError(tr("Combined attachments exceed 20 MiB.", "附件总大小超过 20 MiB"))
            data = stream.read(limit + 1)
            final = os.fstat(stream.fileno())
            if len(data) != first.st_size or (first.st_mtime_ns, first.st_ctime_ns, first.st_nlink) != (
                final.st_mtime_ns,
                final.st_ctime_ns,
                final.st_nlink,
            ):
                raise GatewayError(tr("The attachment changed while being read. Send it again.", "附件读取期间发生变化，请重新发送"))
        return data
    except OSError as exc:
        raise GatewayError(tr("Could not read the attachment within the allowed directory.", "无法在允许目录内读取附件")) from exc
    finally:
        os.close(descriptor)


def capture_media(paths, roots, names=(), *, storage=None):
    if len(paths) > MAX_FILES:
        raise GatewayError(tr("A message can contain up to 10 attachments.", "每条消息最多接收 10 个附件"))
    total = 0
    parts = []
    for index, source in enumerate(paths):
        path = Path(source)
        root = next((r for r in roots if path.is_relative_to(r)), None)
        if root is None:
            raise GatewayError(tr("The mobile attachment is outside the designated cache directories.", "手机附件不在指定缓存目录内"))
        data = owned_file(root, path.relative_to(root), MAX_BYTES - total)
        total += len(data)
        label = Path(names[index]).name if index < len(names) and names[index] else path.name
        label = "".join(char for char in label if char.isprintable())[:240]
        mime = image_type(data)
        if mime:
            encoded = base64.b64encode(data).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            continue
        if len(data) <= MAX_INLINE and b"\0" not in data:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                pass
            else:
                parts.append({"type": "text", "text": tr(f"Attachment {label}\n{text}", f"附件 {label}\n{text}")})
                continue
        description = tr(f"Attachment {label} ({len(data)} bytes)", f"附件 {label}（{len(data)} 字节）")
        if storage:
            snapshot = Path("attachments") / hashlib.sha256(data).hexdigest()
            try:
                atomic_write(storage, snapshot, data, replace=False)
            except FileExistsError:
                if read_bytes(storage, snapshot, limit=MAX_BYTES) != data:
                    raise GatewayError(tr("Attachment snapshot verification failed.", "附件快照校验失败"))
            description += tr(f". The original file was saved to {storage / snapshot}.", f"。原文件已保存为 {storage / snapshot}。")
        parts.append({"type": "text", "text": description})
    return parts


def capture_product(vault, relative, *, lesson_files=()):
    path = Path(relative)
    if not path.parts or (path.parts[0] != "viz" and path not in lesson_files) or path.name.startswith("."):
        raise GatewayError(tr("You can send the current lesson record, handoff, or a file from viz.", "可发送当前课堂的记录、续课说明或 viz 目录中的产物"))
    data = owned_file(Path(vault), path, MAX_BYTES)
    return {
        "type": "image" if image_type(data) else "file",
        "name": path.name,
        "data": base64.b64encode(data).decode("ascii"),
    }


def lesson_artifact(records, lesson, selection):
    vault = Path(lesson["vault"])
    allowed = {}
    if lesson["record"] and Path(lesson["record"]).is_file():
        allowed["record"] = Path(lesson["record"]).relative_to(vault)
    checkpoint_paths = {f"reviews/{prefix}{lesson['id'][:12]}.md" for prefix in CHECKPOINT_PREFIXES}
    if selection in {"handoff", "续课说明", *checkpoint_paths}:
        checkpoint = Path(records.read_checkpoint(lesson["id"])["file"]).relative_to(vault)
        allowed[selection] = checkpoint
    for item in reversed(records.sealed(vault)):
        if item["lesson_id"] == lesson["id"]:
            path = Path(item["archive"] or item["path"])
            allowed.setdefault("record", path)
            allowed[str(path)] = path
    if selection == "记录":
        selection = "record"
    return capture_product(vault, allowed.get(selection, selection), lesson_files=allowed.values())


def _mobile_selection(choice, options, quiz):
    if re.fullmatch(r"[0-9]+(?:[\s,，、]+[0-9]+)*", choice):
        return choice
    if quiz and choice.casefold() in {"i don't know", "i don’t know", "unsure", "我不知道", "不知道", "不确定"}:
        return tr("I don't know", "不知道")
    # Match the entire label before treating a comma inside it as a separator.
    labels = sorted(enumerate(options, 1), key=lambda item: len(item[1]["label"]), reverse=True)
    indices = []
    remaining = choice
    while remaining:
        for index, option in labels:
            label = option["label"]
            if not remaining.startswith(label):
                continue
            tail = remaining[len(label):]
            separator = re.match(r"^\s*[,，、]\s*", tail)
            if not tail or separator:
                indices.append(str(index))
                remaining = tail[separator.end():] if separator else ""
                break
        else:
            return choice if quiz and re.fullmatch(r"[A-La-l]+(?:[\s,，、]+[A-La-l]+)*", choice) else None
    return ",".join(indices) if indices else None


def mobile_answer(text, view):
    """Map the displayed mobile label or position onto the classroom's answer protocol."""
    if not view or view["status"] != "open":
        return text
    line, newline, remainder = text.strip().partition("\n")
    if text.strip().lower() in {"cancel", "取消"}:
        return tr("cancel", "取消")
    if view["mode"] == "text":
        return text
    quiz = view["kind"] == "quiz"
    ends = [len(line)] + [match.start() for match in reversed(list(re.finditer(r"\|", line)))]
    for end in ends:
        answer = _mobile_selection(line[:end].strip(), view["options"], quiz)
        if answer:
            note = line[end + 1:].lstrip()
            return answer + ("\n" + note if note else "") + newline + remainder
    custom = re.match(
        r"^\s*(?:[0-9]+(?:[\s,，、]+[0-9]+)*[\s,，、]+)?(?:custom|other|自填|其他)(?:[ \t：:]|$)",
        text, re.IGNORECASE,
    )
    if not quiz and text.strip() and not custom:
        return tr("other ", "自填 ") + text
    return text


def card_choices(view):
    if view["status"] == "feedback":
        return [(tr("Continue", "继续"), tr("continue", "继续"))]
    choices = []
    if view["mode"] == "single":
        choices = [(opt["label"], str(n)) for n, opt in enumerate(view["options"], 1)]
    if view["kind"] == "quiz":
        choices.append((tr("I don't know", "我不知道"), tr("I don't know", "不知道")))
    choices.append((tr("Cancel", "取消"), tr("cancel", "取消")))
    return choices


def app_artifact(profile, vault, relative, *, records=None, lesson=None):
    """Persist a published classroom export and return Hermes' media directive."""
    artifact = lesson_artifact(records, lesson, relative) if lesson else capture_product(vault, relative)
    data = base64.b64decode(artifact["data"], validate=True)
    checksum = hashlib.sha256(data).hexdigest()
    profile = Path(profile).resolve(strict=True)
    saved = Path("cache/documents/teacher-artifacts") / checksum / artifact["name"]
    try:
        atomic_write(profile, saved, data, replace=False)
    except FileExistsError:
        if read_bytes(profile, saved, limit=MAX_BYTES) != data:
            raise GatewayError(tr("The cached classroom attachment changed. Check the file.", "课堂附件缓存发生变化，请核对文件"))
    return f"MEDIA:{(profile / saved).as_uri()}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS route (id INTEGER PRIMARY KEY CHECK(id = 1), lesson TEXT);
CREATE TABLE IF NOT EXISTS inbound (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admissions (id TEXT PRIMARY KEY, session TEXT NOT NULL, admission TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listed (position INTEGER PRIMARY KEY, lesson TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS card_replies (id TEXT PRIMARY KEY, receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, lesson TEXT,
    text TEXT NOT NULL, attachments TEXT NOT NULL, expected TEXT,
    state TEXT NOT NULL, output TEXT, cursor INTEGER NOT NULL DEFAULT 0,
    receipts TEXT NOT NULL DEFAULT '[]', reason TEXT
);
"""


class MobileClassroom:
    def __init__(self, profile, settings, tutor, *, pending, roles_busy=None, media_roots=()):
        self.settings = settings
        self.profile = Path(profile)
        self.tutor = tutor
        self.pending = pending
        self.roles_busy = roles_busy or (lambda lesson: False)
        self.media_roots = tuple(Path(path).absolute() for path in media_roots)
        self.records = Records(Path(profile))
        root = Path(profile) / "mobile-classroom"
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GatewayError(tr("The mobile queue directory must be private to the current user.", "手机队列目录必须由当前用户独占"))
        self.provider_route = hashlib.sha256(
            json.dumps(
                [settings.platform, settings.sender, settings.chat, str(settings.vault.resolve())]
            ).encode()
        ).hexdigest()[:24]
        self.path = root / f"{self.provider_route}.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.database() as db:
            db.executescript(_SCHEMA)
        from .learning import Learning

        self.learning = Learning(self.profile, incoming_handoff=self.records.incoming_handoff)

    @contextmanager
    def database(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    @contextmanager
    def worker(self):
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GatewayError(tr("The mobile classroom service is already running.", "手机入口服务已经在运行")) from exc
            self.recover()
            yield

    def recover(self):
        with self.database(write=True) as db:
            db.execute("UPDATE queue SET state='download_retry' WHERE state='receiving'")
            db.execute("UPDATE queue SET state='reconcile' WHERE state='running' AND id IN (SELECT id FROM admissions)")
            db.execute("""UPDATE queue SET state='review', reason=CASE state
                       WHEN 'sending' THEN 'interrupted-delivery' ELSE 'interrupted-execution' END
                       WHERE state IN ('running', 'sending', 'routing')""")

    def accepted(self, message_id):
        with self.database() as db:
            return db.execute("SELECT 1 FROM queue WHERE id=?", (message_id,)).fetchone() is not None

    def current_lesson(self, db):
        route = db.execute("SELECT lesson FROM route WHERE id=1").fetchone()
        if route is not None:
            return route["lesson"]
        topic = vault_topic(self.settings.vault)
        if topic:
            lesson = self.records.prepare(self.settings.vault, topic)
            db.execute("INSERT INTO route VALUES(1,?)", (lesson["id"],))
            return lesson["id"]
        return None

    def reserve(self, message, sender):
        identifier = message.message_id
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
            return False
        reply = next((getattr(message, key, None) for key in ("parent_id", "upper_message_id", "root_id")
                      if getattr(message, key, None)), None)
        # The SDK's content field retains the original file keys needed for a fresh download.
        payload = json.dumps(
            {"event": {"message": message, "sender": sender}},
            default=lambda value: {k: v for k, v in vars(value).items() if not k.startswith("_")},
            ensure_ascii=False,
        )
        with self.database(write=True) as db:
            if db.execute("SELECT 1 FROM queue WHERE id=?", (identifier,)).fetchone():
                return False
            lesson = self.current_lesson(db)
            quoted = self.referenced_prompt(db, reply) if reply else None
            pending = self.pending(lesson) if lesson and not quoted else None
            lesson, expected = quoted or (lesson, pending["id"] if pending else None)
            db.execute("""INSERT INTO queue(id,lesson,expected,text,attachments,state)
                          VALUES(?,?,?,'','[]','receiving')""", (identifier, lesson, expected))
            db.execute("INSERT INTO inbound VALUES(?,?)", (identifier, payload))
        return True

    def finish_download(self, identifier, *, failed=False):
        with self.database(write=True) as db:
            if failed:
                db.execute("""UPDATE queue SET state='review', reason='incomplete-download'
                              WHERE id=? AND state='receiving'""", (identifier,))
            else:
                # Hermes ignores an empty text event after stripping an @mention.
                db.execute("UPDATE queue SET state='completed' WHERE id=? AND state='receiving'", (identifier,))
                db.execute("DELETE FROM inbound WHERE id=?", (identifier,))

    def next_download(self):
        with self.database(write=True) as db:
            row = db.execute("""SELECT i.* FROM inbound i JOIN queue q ON q.id=i.id
                                WHERE q.state='download_retry' ORDER BY q.seq LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE queue SET state='receiving' WHERE id=?", (row["id"],))
                return json.loads(row["payload"], object_hook=lambda obj: SimpleNamespace(**obj))

    def authorized(self, event):
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None)
        if (
            platform != self.settings.platform
            or source.chat_type != "dm"
            or source.chat_id != self.settings.chat
            or source.is_bot
            or source.thread_id
            or getattr(event, "internal", False)
        ):
            return False
        raw = getattr(getattr(event, "raw_message", None), "event", None)
        raw_id = getattr(getattr(raw, "sender", None), "sender_id", None)
        verified_ids = {source.user_id, source.user_id_alt}
        verified_ids.update(getattr(raw_id, field, None) for field in ("open_id", "user_id", "union_id"))
        return self.settings.sender in verified_ids

    @localized
    def submit(self, event, *, reserved=False):
        if not self.authorized(event):
            return False
        message_id = getattr(event, "message_id", "")
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 256
        ):
            return False
        text = event.text or ""
        media = getattr(event, "media_urls", ()) or ()
        try:
            parts = capture_media(media, self.media_roots, getattr(event, "media_filenames", ()) or (),
                                  storage=self.path.parent)
            if len(text) > 1024 * 1024:
                raise GatewayError(tr("The message exceeds 1 MiB.", "消息正文超过 1 MiB"))
            output = None
        except GatewayError as exc:
            parts = []
            text, output = "", [{"type": "text", "text": str(exc)}]
        expected = None
        creation = None
        with self.database(write=True) as db:
            snapshot = db.execute("SELECT * FROM queue WHERE id=?", (message_id,)).fetchone()
            if (reserved and (not snapshot or snapshot["state"] != "receiving")) or (not reserved and snapshot):
                return False
            current = self.current_lesson(db)
            lesson = snapshot["lesson"] if snapshot else current
            expected = snapshot["expected"] if snapshot else None
            if output is None:
                download = re.fullmatch(r"(?:Download classroom file|下载课堂文件) (.+)", text.strip(), re.IGNORECASE)
                if lesson and download:
                    lesson, output = self.command(db, lesson, "/course", "file " + download[1].strip())
                elif text.lstrip().startswith(("/", "!", "！")):
                    command, _, argument = text.lstrip().partition(" ")
                    if command == "/answer" and lesson:
                        expected, _, text = argument.partition(" ")
                        if not expected or not text:
                            output = [{"type": "text", "text": tr("Use /answer QUESTION_ID ANSWER.", "格式为 /answer 问题ID 答案")}]
                    elif command == "/new" or (command == "/course" and argument.strip().partition(" ")[0] == "new"):
                        # Creating a classroom writes a second database. Persist admission before
                        # starting that side effect, so a crash requires review instead of replay.
                        creation = ("new " + argument if command == "/new" else argument).strip()
                    elif command in {"/lesson-end", "/lesson-next", "/stop"} and lesson and not argument.strip():
                        creation = command
                    else:
                        if snapshot and current != lesson and (command == "/switch" or
                                (command == "/course" and argument.strip().partition(" ")[0] in {"use", "stop"})):
                            output = [{"type": "text", "text": tr("The mobile lesson changed. Confirm again.", "手机课堂已切换，请重新确认。")}]
                        else:
                            lesson, output = self.command(db, lesson, command, argument.strip())
                elif lesson is None:
                    output = [
                        {"type": "text", "text": tr(
                            "Use /course list to find a lesson or /course new TOPIC to start one.",
                            "用 /course list 选择课堂，或用 /course new 主题 开课。",
                        )}
                    ]
                elif not text.strip() and not parts:
                    output = [{"type": "text", "text": tr("Send a classroom message or an attachment.", "请发送课堂消息或附件。")}]
                elif not reserved:
                    referenced = getattr(event, "reply_to_message_id", None)
                    quoted = self.referenced_prompt(db, referenced) if referenced else None
                    if quoted:
                        lesson, expected = quoted
                    else:
                        pending = self.pending(lesson)
                        if pending:
                            expected = pending["id"]
            state = "routing" if creation is not None else ("queued" if output is None else "ready")
            db.execute(
                """INSERT INTO queue(id, lesson, text, attachments, expected, state, output)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lesson=excluded.lesson,
                   text=excluded.text, attachments=excluded.attachments, expected=excluded.expected,
                   state=excluded.state, output=excluded.output""",
                (
                    message_id,
                    lesson,
                    text,
                    json.dumps(parts, ensure_ascii=False),
                    expected,
                    state,
                    json.dumps(output, ensure_ascii=False),
                ),
            )
        if creation is not None:
            self.create_route(message_id, lesson, creation)
        return True

    @staticmethod
    def delivered_prompt(db, receipt):
        rows = db.execute(
            """SELECT q.lesson, json_extract(q.output,'$[' || item.key || ']') AS part
               FROM queue q, json_each(q.receipts) item
                           WHERE item.value=? AND q.output IS NOT NULL AND q.lesson IS NOT NULL""",
            (receipt,),
        )
        for row in rows:
            part = json.loads(row["part"]) if row["part"] else {}
            if part.get("prompt_id"):
                return row["lesson"], part
        return None

    def referenced_prompt(self, db, receipt):
        delivered = self.delivered_prompt(db, receipt)
        return (delivered[0], delivered[1]["prompt_id"]) if delivered else None

    def question_output(self, lesson, response, pending):
        part = {"type": "text", "text": response}
        if pending:
            part["prompt_id"] = pending["id"]
            if pending["status"] == "open":
                part["text"] += tr(
                    f"\n\nTo answer this specific question, use /answer {pending['id']} ANSWER.",
                    f"\n\n指定这道题作答时，用 /answer {pending['id']} 答案。",
                )
            view = self.learning.interaction_view(lesson)
            if view and view["id"] == pending["id"]:
                part.update(type="card", choices=card_choices(view), phase=view["status"])
        return part

    def create_route(self, event_id, previous, creation):
        try:
            with self.database(write=True) as db:
                admission = db.execute("SELECT state FROM queue WHERE id=?", (event_id,)).fetchone()
                if admission["state"] != "routing":
                    return
                route = db.execute("SELECT lesson FROM route WHERE id=1").fetchone()
                current = route["lesson"] if route else None
                if current == previous:
                    if creation.startswith("/"):
                        target, output = self.lesson_command(db, current, creation, event_id)
                        if output is None:
                            return
                    else:
                        target, output = self.command(db, current, "/course", creation)
                else:
                    target, output = (
                        previous,
                        [{"type": "text", "text": tr(
                            "The mobile lesson changed before the new lesson was created. Confirm again.",
                            "手机课堂在开课前已切换，请重新确认。",
                        )}],
                    )
                db.execute(
                    "UPDATE queue SET state='ready',lesson=?,output=? WHERE id=?",
                    (target, json.dumps(output, ensure_ascii=False), event_id),
                )
        except (BusyError, LibraryError, ValueError) as exc:
            with self.database(write=True) as db:
                db.execute("UPDATE queue SET state='ready',output=? WHERE id=? AND state='routing'",
                           (json.dumps([{"type": "text", "text": str(exc)}], ensure_ascii=False), event_id))
        except Exception as exc:
            with self.database(write=True) as db:
                db.execute(
                    "UPDATE queue SET state='review',reason=? WHERE id=? AND state='routing'",
                    (f"routing-{type(exc).__name__}", event_id),
                )

    def lesson_command(self, db, lesson, command, event_id):
        if command == "/stop":
            text = asyncio.run(self.tutor.interrupt(lesson))
        else:
            busy = bool(self.pending(lesson) or self.roles_busy(lesson) or db.execute(
                "SELECT 1 FROM queue WHERE lesson=? AND state IN ('running','queued','reconcile')", (lesson,)
            ).fetchone())
            if command == "/lesson-end":
                result = self.records.seal(lesson, busy=busy)
                file = result.get("file") or ""
                text = tr(f"This segment was archived. The current lesson is still open. {file}",
                          f"本段已封存，留在原课堂。{file}")
            else:
                target = self.records.handoff(lesson, confirmed=True, busy=busy, request_id=event_id,
                                              capture_progress=lambda: self.tutor.handoff_progress(lesson))
                lesson = target["id"]
                self.learning.seed_handoff(lesson)
                db.execute("UPDATE route SET lesson=? WHERE id=1", (lesson,))
                # The normal worker submits the handoff after the route and prompt are durable.
                prompt = handoff_message(target["handoff"])
                db.execute("UPDATE queue SET state='queued',lesson=?,text=?,expected=NULL WHERE id=?",
                           (lesson, prompt, event_id))
                return lesson, None
        return lesson, [{"type": "text", "text": text}]

    @contextmanager
    def lesson_unoccupied(self, lesson):
        if not lesson:
            yield
            return
        root = self.profile / "teacher"
        with file_lease(root, f"requests/{lesson}.lock"), file_lease(root, f"turns/{lesson}.lock"):
            with self.records.lease(lesson):
                yield

    def command(self, db, current, command, argument):
        def result(text):
            return current, [{"type": "text", "text": text}]

        if command == "/status":
            counts = db.execute("SELECT state,count(*) FROM queue WHERE lesson IS ? GROUP BY state", (current,))
            lesson = current or tr("Disconnected", "未连接")
            queue = "\n".join(f"{state} {count}" for state, count in counts)
            return result(tr(f"Current lesson {lesson}", f"当前课堂 {lesson}") + "\n" + queue)
        if command == "/history" and current:
            history = self.records.history(current)[-20:]
            return result("\n".join(f"{item['role']} · {item['text']}" for item in history)
                          or tr("The lesson has no transcript yet.", "课堂暂无原文。"))
        if command == "/whoami":
            return result(tr("Current private chat user ", "当前私聊身份 ") + self.settings.sender)
        if command == "/help":
            return result(tr(
                "/list shows available lessons, /switch NAME_OR_NUMBER switches lessons, /new TOPIC starts a lesson, "
                "/stop interrupts the turn, /lesson-end archives the segment, /lesson-next continues in a new session, "
                "/status shows status, and /course file record or /course file handoff retrieves files.",
                "/list 查看课堂，/switch 名称或序号 切课，/new 主题 开新课，/stop 停止本轮，"
                "/lesson-end 封存，/lesson-next 交接，/status 查看状态，/course file 记录或续课说明 可取回文件。",
            ))
        if command in {"/list", "/sessions"}:
            command, argument = "/course", "list"
        if command == "/switch":
            command, argument = "/course", "use " + argument
        if command != "/course":
            return result(tr(
                "The mobile classroom accepts classroom commands. Use /course list to find a lesson.",
                "手机入口仅接受课堂指令。用 /course list 查看课堂。",
            ))
        action, _, value = argument.partition(" ")
        if action == "list":
            lessons = self.records.recent(self.settings.vault)
            lines = [tr("Lessons", "课堂列表")]
            db.execute("DELETE FROM listed")
            latest = {}
            for item in lessons:
                latest.setdefault((item["topic"], item["temporary"]), item)
            for index, item in enumerate(latest.values(), 1):
                db.execute("INSERT INTO listed VALUES(?,?)", (index, item["id"]))
                lines.append(f"{index}. {item['topic']}  {item['id']}" + (tr(" (current)", "（当前）") if item["id"] == current else ""))
            if not latest:
                lines.append(tr("No lessons yet. Use /course new TOPIC to start one.", "暂无课堂。用 /course new 主题 开课。"))
            return result("\n".join(lines))
        if action == "file" and current:
            try:
                artifact = lesson_artifact(self.records, self.records.get(current), value)
            except (OSError, LibraryError, GatewayError) as exc:
                return result(str(exc))
            return current, [artifact]
        if action not in {"use", "new", "stop"}:
            return result(tr(
                "Use /course list, /course use LESSON_ID, /course new TOPIC, /course stop, or /course file viz/FILE.",
                "用 /course list、/course use 课堂ID、/course new 主题、/course stop 或 /course file viz/文件。",
            ))
        try:
            with self.lesson_unoccupied(current):
                if current and (
                    self.roles_busy(current)
                    or db.execute(
                        "SELECT 1 FROM queue WHERE lesson=? AND state IN ('queued','running')", (current,)
                    ).fetchone()
                ):
                    return result(tr(f"Lesson {current} is working. You can switch when it finishes.", f"当前课堂 {current} 正在工作，完成后可切换。"))
                if action == "use":
                    if value.isdecimal():
                        entry = db.execute("SELECT lesson FROM listed WHERE position=?", (int(value),)).fetchone()
                        if entry:
                            value = entry["lesson"]
                    candidate = next(
                        (item for item in self.records.recent(self.settings.vault) if value in (item["id"], item["topic"])),
                        None,
                    )
                    if candidate is None:
                        return result(tr("The requested lesson was not found in this library.", "该资料库内未找到指定课堂。"))
                    if candidate["id"] != current:
                        with self.lesson_unoccupied(candidate["id"]):
                            if self.roles_busy(candidate["id"]):
                                return result(tr("The target lesson is working.", "目标课堂正在工作。"))
                elif action == "new":
                    topic = vault_topic(self.settings.vault)
                    if topic and topic != value:
                        return result(tr("This mobile classroom is linked to the topic ", "此手机入口仅绑定主题：") + topic)
                    candidate = self.records.prepare(self.settings.vault, value, new=True)
                else:
                    candidate = None
                target = candidate["id"] if candidate else None
                db.execute(
                    "INSERT INTO route VALUES (1,?) ON CONFLICT(id) DO UPDATE SET lesson=excluded.lesson",
                    (target,),
                )
        except (LibraryError, ValueError) as exc:
            return result(str(exc))
        if candidate is None:
            return target, [{"type": "text", "text": tr("Disconnected from the mobile lesson.", "手机课堂已断开。")}]
        output = [{"type": "text", "text": tr(f"Resumed {candidate['topic']} · {target}", f"已接续 {candidate['topic']} · {target}")}]
        pending = self.pending(target)
        if pending:
            output.append(self.question_output(target, pending["text"], pending))
        return target, output

    def row(self, message_id):
        with self.database() as db:
            value = db.execute("SELECT * FROM queue WHERE id=?", (message_id,)).fetchone()
            return dict(value) if value else None

    @localized
    def review(self, message_id=None, decision=None):
        with self.database(write=message_id is not None) as db:
            if message_id is None:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,lesson,reason,cursor,receipts FROM queue WHERE state='review' ORDER BY seq"
                    )
                ]
            row = db.execute("SELECT * FROM queue WHERE id=? AND state='review'", (message_id,)).fetchone()
            if row is None:
                raise GatewayError(tr("No mobile message awaiting review was found.", "未找到待核对的手机消息"))
            if decision == "discard":
                db.execute("UPDATE queue SET state='completed',reason=NULL WHERE id=?", (message_id,))
                db.execute("DELETE FROM inbound WHERE id=?", (message_id,))
            elif decision == "retry" and row["reason"] == "incomplete-download":
                db.execute("UPDATE queue SET state='download_retry',reason=NULL WHERE id=?", (message_id,))
            elif decision == "retry" and row["output"] not in {None, "null"}:
                db.execute("UPDATE queue SET state='ready',reason=NULL WHERE id=?", (message_id,))
            elif decision == "retry" and db.execute("SELECT 1 FROM admissions WHERE id=?", (message_id,)).fetchone():
                db.execute("UPDATE queue SET state='reconcile',reason=NULL WHERE id=?", (message_id,))
            elif decision == "delivered" and row["output"] not in {None, "null"}:
                cursor = row["cursor"] + 1
                status = "completed" if cursor >= len(json.loads(row["output"])) else "ready"
                receipts = [*json.loads(row["receipts"]), None]
                db.execute(
                    "UPDATE queue SET state=?,cursor=?,receipts=?,reason=NULL WHERE id=?",
                    (status, cursor, json.dumps(receipts), message_id),
                )
            else:
                raise GatewayError(tr(
                    "After reviewing the lesson, discard an uncertain model run or retry or confirm a saved message "
                    "based on whether it was delivered.",
                    "不确定的模型执行可在核对课堂后关闭，已保存输出可按实际发送情况重发或确认",
                ))
        return self.row(message_id)["state"]

    @localized
    async def step(self, deliver):
        with self.database(write=True) as db:
            candidates = db.execute("""
                SELECT * FROM queue AS candidate WHERE state IN ('queued','ready','reconcile')
                AND NOT EXISTS (SELECT 1 FROM queue AS earlier
                    WHERE earlier.seq < candidate.seq AND earlier.lesson IS candidate.lesson
                    AND earlier.state != 'completed')
                ORDER BY seq
            """)
            try:
                row = next((item for item in candidates if item["state"] != "queued" or
                            not self.roles_busy(item["lesson"])), None)
            finally:
                candidates.close()
            if row is None:
                return False
            state = "sending" if row["state"] == "ready" else "running"
            db.execute("UPDATE queue SET state=? WHERE id=?", (state, row["id"]))
        if state == "running":
            await asyncio.to_thread(self.execute, row)
        else:
            await self.deliver(row, deliver)
        return True

    def execute(self, row):
        try:
            arguments = {
                "event_id": f"mobile:{self.provider_route}:{row['id']}",
                "attachments": json.loads(row["attachments"]),
            }
            if row["expected"] is not None:
                arguments["expected_id"] = row["expected"]
            if row["state"] == "reconcile":
                with self.database() as db:
                    receipt = db.execute("SELECT session, admission FROM admissions WHERE id=?", (row["id"],)).fetchone()
                settled = asyncio.run(self.tutor.recover_turn(*receipt))
                response = settled.get("final_response") or tr("Hermes returned no text for this turn.", "Hermes 本轮没有返回正文。")
            else:
                if hasattr(self.tutor, "recover_turn"):
                    def admitted(session, receipt):
                        with self.database(write=True) as db:
                            db.execute("INSERT INTO admissions VALUES(?,?,?)", (row["id"], session, receipt))
                    arguments["on_admission"] = admitted
                view = self.learning.interaction_view(row["lesson"]) if row["expected"] else None
                text = row["text"]
                if view and view["id"] == row["expected"]:
                    language = self.learning.request_language(row["lesson"], arguments["event_id"])
                    with use_language(language):
                        text = mobile_answer(text, view)
                response = self.tutor.turn(row["lesson"], text, **arguments)
            pending = self.pending(row["lesson"])
            result = [self.question_output(row["lesson"], response, pending)]
            status, reason = "ready", None
        except BusyError:
            status, reason, result = "queued", None, None
        except SettledTurnError as exc:
            status, reason, result = "ready", None, [{"type": "text", "text": str(exc)}]
        except Exception as exc:
            status, reason, result = "review", f"execution-{type(exc).__name__}", None
        with self.database(write=True) as db:
            db.execute(
                "UPDATE queue SET state=?,output=?,reason=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), reason, row["id"]),
            )
        if status == "queued":
            # Bridge temporary contention with a desktop writer without spinning.
            time.sleep(0.25)

    async def deliver(self, row, sender):
        output = json.loads(row["output"])
        cursor = row["cursor"]
        try:
            with self.database() as db:
                clicked = db.execute("SELECT receipt FROM card_replies WHERE id=?", (row["id"],)).fetchone()
            receipt = await sender(self.settings.chat, clicked[0] if clicked else row["id"], output[cursor])
            if not receipt:
                raise GatewayError(tr("Feishu did not confirm delivery.", "Feishu 未确认发送"))
        except Exception as exc:
            with self.database(write=True) as db:
                db.execute(
                    "UPDATE queue SET state='review',reason=? WHERE id=?",
                    (f"delivery-{type(exc).__name__}", row["id"]),
                )
            return
        receipts = [*json.loads(row["receipts"]), str(receipt)]
        status = "completed" if cursor + 1 == len(output) else "ready"
        with self.database(write=True) as db:
            db.execute(
                "UPDATE queue SET state=?,cursor=?,receipts=?,reason=NULL WHERE id=?",
                (status, cursor + 1, json.dumps(receipts), row["id"]),
            )

    def card_click(self, event):
        context, operator = getattr(event, "context", None), getattr(event, "operator", None)
        if getattr(context, "open_chat_id", None) != self.settings.chat or self.settings.sender not in {
            getattr(operator, key, None) for key in ("open_id", "user_id", "union_id")
        }:
            return False
        receipt = getattr(context, "open_message_id", None)
        action = getattr(getattr(event, "action", None), "value", None)
        if not isinstance(action, dict):
            return False
        with self.database(write=True) as db:
            origin = self.delivered_prompt(db, receipt)
            route = db.execute("SELECT lesson FROM route WHERE id=1").fetchone()
            if not origin or not route or origin[0] != route["lesson"]:
                return False
            lesson, card = origin
            question = card["prompt_id"]
            if card.get("phase") != action.get("phase") or question != action.get("question"):
                return False
            view = self.learning.interaction_view(lesson)
            if not view or view["id"] != question or view["status"] != action.get("phase"):
                return False
            answer = action.get("answer")
            if answer not in {value for _, value in card.get("choices", ())}:
                return False
            identifier = "card-" + hashlib.sha256(f"{question}/{view['status']}".encode()).hexdigest()
            added = db.execute("""INSERT OR IGNORE INTO queue(id,lesson,text,expected,attachments,state)
                                  VALUES(?,?,?,?,'[]','queued')""", (identifier, lesson, answer, question))
            if added.rowcount:
                db.execute("INSERT INTO card_replies VALUES(?,?)", (identifier, receipt))
                return True
            return False


async def send_native(adapter, chat, source, output, staging):
    kind = output["type"]
    if kind == "text":
        reply = await adapter.send(chat, output["text"], reply_to=source)
    elif kind == "card":
        from plugins.platforms.feishu.adapter import _card, _card_button

        card = _card(tr("Classroom interaction", "课堂互动"), "blue", output["text"])
        buttons = [_card_button(label, "default", {"question": output["prompt_id"], "phase": output["phase"],
                                                   "answer": value}) for label, value in output["choices"]]
        card["elements"].extend({"tag": "action", "actions": buttons[i:i + 5]} for i in range(0, len(buttons), 5))
        response = await adapter._feishu_send_with_retry(chat_id=chat, msg_type="interactive",
                        payload=json.dumps(card, ensure_ascii=False), reply_to=source, metadata=None)
        reply = adapter._finalize_send_result(response, tr("Could not send the classroom card.", "发送课堂题卡失败"))
    else:
        with tempfile.TemporaryDirectory(dir=staging) as directory:
            path = Path(directory) / Path(output["name"]).name
            path.write_bytes(base64.b64decode(output["data"], validate=True))
            if kind == "image":
                reply = await adapter.send_image_file(chat, str(path), reply_to=source)
            else:
                reply = await adapter.send_document(chat, str(path), file_name=path.name, reply_to=source)
    return reply.message_id if reply.success else None


def native_adapter(mobile):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    class ClassroomAdapter(FeishuAdapter):
        def _on_message_event(self, data):
            event = getattr(data, "event", None)
            message, sender = getattr(event, "message", None), getattr(event, "sender", None)
            if (not message or not sender or not getattr(message, "message_id", None)
                    or getattr(message, "thread_id", None) or self._admit(sender, message) is not None):
                return
            if not mobile.reserve(message, sender):
                return
            loop = self._loop
            if self._loop_accepts_callbacks(loop):
                submitted = self._submit_on_loop(loop, self._receive_reserved(data=data, message=message,
                                sender_id=sender.sender_id, chat_type=message.chat_type,
                                message_id=message.message_id, is_bot=False))
                if submitted:
                    return
            with mobile.database(write=True) as db:
                db.execute("UPDATE queue SET state='download_retry' WHERE id=? AND state='receiving'",
                           (message.message_id,))

        def _admit(self, sender, message, **kwargs):
            identity = getattr(sender, "sender_id", None)
            if (
                getattr(sender, "sender_type", None) != "user"
                or getattr(message, "chat_type", None) != "p2p"
                or getattr(message, "chat_id", None) != mobile.settings.chat
                or mobile.settings.sender
                not in {getattr(identity, field, None) for field in ("open_id", "user_id", "union_id")}
            ):
                return "outside_teacher_private_chat"
            return super()._admit(sender, message, **kwargs)

        async def _is_duplicate(self, message_id):
            # reserve() checks the durable ID atomically after the native admission filter.
            return False

        async def _process_inbound_message(self, **kwargs):
            data, message = kwargs["data"], kwargs["message"]
            if kwargs["is_bot"] or getattr(message, "thread_id", None):
                return
            if mobile.reserve(message, data.event.sender):
                await self._receive_reserved(**kwargs)

        async def _receive_reserved(self, **kwargs):
            identifier = kwargs["message_id"]
            try:
                await super()._process_inbound_message(**kwargs)
            except Exception:
                mobile.finish_download(identifier, failed=True)
                return
            mobile.finish_download(identifier)

        async def retry_download(self):
            data = await asyncio.to_thread(mobile.next_download)
            if data is None:
                return False
            message, sender = data.event.message, data.event.sender
            await self._receive_reserved(data=data, message=message, sender_id=sender.sender_id,
                                         chat_type=message.chat_type, message_id=message.message_id,
                                         is_bot=False)
            return True

        async def _download_feishu_message_resources(self, *, message_id, normalized):
            result = await super()._download_feishu_message_resources(message_id=message_id, normalized=normalized)
            if len(result[0]) != len(normalized.image_keys) + len(normalized.media_refs):
                raise GatewayError(tr("The download was incomplete. Retry using the original message.", "附件下载不完整，可按原消息重试。"))
            return result

        async def _maybe_extract_text_document(self, cached_path, media_type):
            # Classroom admission enforces its common byte and UTF-8 limits.
            return ""

        async def _dispatch_inbound_event(self, event):
            # Preserve each native message ID rather than batching it in memory.
            event._gateway_accepted = await asyncio.to_thread(mobile.submit, event, reserved=True)

        def _on_drive_comment_event(self, data):
            return None

        def _on_meeting_invited_event(self, data):
            return None

        def _on_reaction_event(self, event_type, data):
            return None

        def _on_card_action_trigger(self, data):
            if mobile.card_click(getattr(data, "event", None)):
                return self._card_response()
            with use_profile(mobile.profile):
                return self._card_toast(tr("This card has ended, was already submitted, or belongs to another lesson.",
                                           "题卡已结束、已提交或不属于当前课堂。"))

    return ClassroomAdapter(
        PlatformConfig(
            enabled=True,
            gateway_restart_notification=False,
            extra={"connection_mode": "websocket", "default_group_policy": "disabled", "allow_bots": "none"},
        )
    )


async def serve(mobile):
    adapter = native_adapter(mobile)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    async def send(chat, source, output):
        return await send_native(adapter, chat, source, output, mobile.path.parent)

    with mobile.worker():
        try:
            if not await adapter.connect():
                raise GatewayError(tr(
                    "Could not start the Feishu adapter. Check the Feishu credentials in the dedicated profile.",
                    "无法启动 Feishu 适配器，请核对专用 profile 的飞书凭据",
                ))
            while not stop.is_set():
                if not await adapter.retry_download() and not await mobile.step(send):
                    try:
                        await asyncio.wait_for(stop.wait(), 0.25)
                    except TimeoutError:
                        pass
        finally:
            await adapter.disconnect()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)


def main(argv=None):
    profile = profile_argument(argv)
    try:
        with use_profile(profile) if profile is not None else nullcontext():
            return run(argv)
    except (OSError, ValueError) as exc:
        print(f"Teacher: {exc}", file=sys.stderr)
        return 1


def run(argv):
    parser = argparse.ArgumentParser(description=tr("Run the mobile classroom for a dedicated Teacher profile.", "运行专用 Teacher 手机课堂入口"))
    parser.add_argument("--profile", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("serve")
    review = commands.add_parser("review")
    review.add_argument("message", nargs="?")
    review.add_argument("decision", nargs="?", choices=["discard", "retry", "delivered"])
    args = parser.parse_args(argv)
    try:
        from .learning import Learning
        from .profile import activate
        from .roles import Roles
        from .runtime import Tutor

        profile = activate(args.profile)
        from tui_gateway.launch_profile_policy import launch_profile_runtime_scope

        with launch_profile_runtime_scope(profile):
            settings = MobileSettings(
                os.getenv("TEACHER_FEISHU_SENDER", ""),
                os.getenv("TEACHER_FEISHU_CHAT", ""),
                Path(os.getenv("TEACHER_MOBILE_VAULT", "")),
            )
            roots = ()
            tutor = None
            if args.action == "serve":
                from gateway.platforms.base import (
                    get_audio_cache_dir,
                    get_document_cache_dir,
                    get_image_cache_dir,
                )

                roots = (get_audio_cache_dir(), get_document_cache_dir(), get_image_cache_dir())
                tutor = Tutor(profile)
            mobile = MobileClassroom(
                profile,
                settings,
                tutor,
                pending=Learning(profile).pending,
                roles_busy=Roles(profile).busy,
                media_roots=roots,
            )
            if args.action == "serve":
                asyncio.run(serve(mobile))
            else:
                print(json.dumps(mobile.review(args.message, args.decision), ensure_ascii=False))
        return 0
    except (GatewayError, OSError, LibraryError, ValueError, ImportError) as exc:
        parser.exit(1, f"Teacher: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

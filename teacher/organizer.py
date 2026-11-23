"""Lossless organization and relocation of Teacher library files."""

from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from .i18n import tr
from .library import (
    BusyError,
    LibraryError,
    MARKER,
    atomic_write,
    file_lease,
    json_bytes,
    read_bytes,
    read_json,
    remove_file,
    safe_path,
    topic_folder,
    topic_vaults,
    validate_topic,
    vault_directory,
    vault_topic,
)
from .records import NEXT_SESSION_PATHS, READING_ENTRY_NAMES, Records, digest, optional_bytes
from . import organizer_plan


_AREA = ".teacher-organizer"
_LEDGER = f"{_AREA}/ledger.json"
_LOCK = f"{_AREA}/operations.lock"
_JOURNAL = f"{_AREA}/pending.json"
_MOVE = f"{_AREA}/migration.json"
_CATEGORIES = ("concepts", "reviews", "materials")
_WIKI = re.compile(r"!?(?<!\\)\[\[([^\]\n]+)\]\]")
_LINK = re.compile(r"""!?(?<!\\)\[[^\[\]\n]*\]\(\s*(<[^>\n]+>|[^)\n]+?)(?:\s+["'][^"'\n]*["'])?\s*\)""")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_MATH = re.compile(r"^\s*(\$\$|\\\[)\s*$")
_PIPE = re.compile(r"^\s*\|.+\|\s*$")
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_BLOCK_START = re.compile(r"^(#{1,3} |>[ ]*\[![^]]+\])")
MAX_PLANNING_BYTES = 120_000


class OrganizerError(LibraryError):
    """An organizer operation could not preserve the source files."""


class _UnretryablePlanError(OrganizerError):
    """Replanning cannot resolve the error."""


def _read_optional_json(vault: Path, path: str, default):
    try:
        return read_json(vault, path)
    except FileNotFoundError:
        return default


def _slug(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or value.startswith(".")
        or Path(value).name != value
        or "\\" in value
        or any(ord(character) < 32 or character in '[]#?<>:*"|' for character in value)
        or len(value.encode()) > 180
        or value.lower().endswith(".md")
    ):
        raise OrganizerError(tr(f"Invalid chapter or note filename: {value}", f"章节或笔记文件名无效：{value}"))
    return value


def _text(value, label: str, *, multiline: bool = True) -> str:
    if not isinstance(value, str) or not value.strip() or (not multiline and len(value.splitlines()) != 1):
        raise OrganizerError(tr(f"Enter a valid {label}.", f"需要有效的{label}"))
    return value


def _markdown(data: bytes, path: str) -> str:
    try:
        return data.decode()
    except UnicodeError as error:
        raise OrganizerError(tr(
            f"Lesson Markdown must use UTF-8: {path}",
            f"课堂 Markdown 必须使用 UTF-8：{path}",
        )) from error


def _lines(data: bytes, path: str) -> list[str]:
    return _markdown(data, path).splitlines(keepends=True)


def _structures(lines: list[str]) -> tuple[set[int], set[int]]:
    """Return unsafe cuts and lines containing fenced code or multiline math."""
    forbidden, code = set(), set()
    region = None
    for number, line in enumerate(lines, 1):
        content = line.rstrip("\r\n")
        content = re.sub(r"^(?:\s*>[ ]?)+", "", content)
        fence = _FENCE.match(content)
        if region:
            code.add(number)
            if (
                region.startswith(("`", "~"))
                and fence
                and not fence[2].strip()
                and fence[1][0] == region[0]
                and len(fence[1]) >= len(region)
            ) or content.strip() == region:
                region = None
            else:
                forbidden.add(number)
        elif fence:
            region = fence[1]
            code.add(number)
            forbidden.add(number)
        elif math := _MATH.match(content):
            region = "$$" if math[1] == "$$" else "\\]"
            code.add(number)
            forbidden.add(number)
        following = re.sub(r"^(?:\s*>[ ]?)+", "", lines[number]) if number < len(lines) else ""
        if _PIPE.match(content) and _PIPE.match(following):
            forbidden.add(number)
    return forbidden, code


def _link_spans(markdown: str):
    lines = markdown.splitlines(keepends=True)
    _, code = _structures(lines)
    offset = 0
    for number, line in enumerate(lines, 1):
        if number not in code:
            visible = re.sub(r"`+[^`]*`+", lambda m: " " * len(m[0]), line)
            for pattern, wiki in ((_WIKI, True), (_LINK, False)):
                for match in pattern.finditer(visible):
                    raw = match[1].split("|", 1)[0] if wiki else match[1].strip("<>")
                    try:
                        link = urlsplit(raw)
                    except ValueError:
                        continue
                    if link.scheme or link.netloc or not link.path:
                        continue
                    start = offset + match.start(1) + int(not wiki and match[1].startswith("<"))
                    yield start, start + len(link.path), unquote(link.path), wiki
        offset += len(line)


def _links(markdown: str):
    for _, _, path, wiki in _link_spans(markdown):
        yield path, wiki


def _chapter_links(vault, text, source, chapter, archives):
    for start, end, link, wiki in sorted(_link_spans(text), reverse=True):
        target = _link_target(vault, source, link, wiki, archives)
        if not target:
            raise OrganizerError(tr(
                f"Local link target not found: {source} → {link}",
                f"本地链接找不到目标：{source} → {link}",
            ))
        target = archives.get(target, target)
        if _link_target(vault, chapter, link, wiki, set(archives.values())) == target:
            continue
        spelling = target if wiki else quote(os.path.relpath(target, Path(chapter).parent))
        text = text[:start] + spelling + text[end:]
    return text


def _link_target(vault: Path, path: str, link: str, wiki: bool, available: set[str]) -> str | None:
    if not link:
        return ""
    if link.startswith("/"):
        return None
    if wiki:
        tries = [Path(link), Path(path).parent / link]
    else:
        tries = [Path(path).parent / link]
    for target in tries:
        stack = []
        for piece in target.parts:
            if piece == "..":
                if not stack:
                    break
                stack.pop()
            else:
                stack.append(piece)
        else:
            if not stack:
                continue
            normalized = Path(*stack)
            candidates = [normalized]
            if wiki and not normalized.suffix:
                candidates.append(normalized.with_suffix(".md"))
            for candidate in candidates:
                name = candidate.as_posix()
                if name in available or safe_path(vault, name).is_file():
                    return name
    if wiki and "/" not in link:
        names = {link, f"{link}.md"}
        matches = {entry for entry in available if Path(entry).name in names}
        for entry in vault.rglob("*"):
            if (
                entry.name in names
                and entry.is_file()
                and not any(piece.startswith(".") for piece in entry.relative_to(vault).parts)
            ):
                item = entry.relative_to(vault).as_posix()
                safe_path(vault, item, must_exist=True)
                matches.add(item)
        if len(matches) == 1:
            return matches.pop()
    return None


def _append_outputs(outputs: dict, path: str, data: bytes) -> None:
    if path in outputs:
        raise OrganizerError(tr(f"The organization plan produced a duplicate file: {path}", f"本次整理重复生成文件：{path}"))
    outputs[path] = data


def _bundle_digest(bundle: dict) -> str:
    return digest(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode())


def _managed_path(paths, existing):
    return next((path for path in paths if path in existing), tr(*paths))


def _reading_entry(chapters: dict, supplemental, path: str) -> bytes:
    language = "zh" if path == READING_ENTRY_NAMES[1] else "en"
    listing = [
        tr("# Start Here", "# 开始阅读", language=language),
        "",
        tr("## Lesson Chapters", "## 课堂章节", language=language),
        "",
    ]
    listing.extend(f"- [[{path[:-3]}|{title}]]" for path, title in chapters.items())
    supplemental = [path for path in dict.fromkeys(supplemental) if path.split("/", 1)[0] in _CATEGORIES]
    if supplemental:
        listing.extend((
            "",
            tr("## Concept Cards, Reviews, and Materials", "## 知识卡、复习和资料", language=language),
            "",
        ))
        listing.extend(f"- [[{path[:-3]}|{Path(path).stem}]]" for path in supplemental)
    return ("\n".join(listing) + "\n").encode()


def _segments(sources: dict) -> list[dict]:
    segments = []
    for source, (_, lines) in sources.items():
        forbidden, code = _structures(lines)
        starts = [1]
        for number, line in enumerate(lines[1:], 2):
            if _BLOCK_START.match(line) and number not in code and number - 1 not in forbidden:
                starts.append(number)
        ends = [start - 1 for start in starts[1:]] + [len(lines)]
        segments.extend(
            {"id": source, "start": start, "end": end, "text": "".join(lines[start - 1 : end])}
            for start, end in zip(starts, ends)
        )
    return segments


def _planned_bundle(topic: str, segments: list[dict], plan: dict) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("chapters"), list) or not plan["chapters"]:
        raise OrganizerError(tr("The plan must include a list of chapters.", "规划结果需要章节列表"))
    starts = [chapter.get("start") if isinstance(chapter, dict) else None for chapter in plan["chapters"]]
    if (
        any(type(start) is not int or not 1 <= start <= len(segments) for start in starts)
        or starts[0] != 1
        or starts != sorted(set(starts))
    ):
        raise OrganizerError(tr("Chapter start numbers must begin at 1 and increase.", "章节起始编号需从 1 开始，并且递增"))
    chapters, names = [], {}
    for item, start, stop in zip(plan["chapters"], starts, [*starts[1:], len(segments) + 1]):
        name = _slug(item.get("slug"))
        names[start] = name
        slices = []
        for segment in segments[start - 1 : stop - 1]:
            if slices and slices[-1]["id"] == segment["id"]:
                slices[-1]["end"] = segment["end"]
            else:
                slices.append({key: segment[key] for key in ("id", "start", "end")})
        chapters.append(
            {"slug": name, "title": _text(item.get("title"), tr(
                "chapter title",
                "章节标题",
            ), multiline=False), "slices": slices}
        )
    bundle = {
        "schema_version": 2,
        "topic": topic,
        "chapters": chapters,
        "next_session": _text(plan.get("next_session"), tr("next-session suggestion", "续课参考")),
    }
    for folder in ("concepts", "reviews"):
        notes = plan.get(folder)
        if not isinstance(notes, list):
            raise OrganizerError(tr(f"The plan must include a {folder} list.", f"规划结果需要 {folder} 列表"))
        bundle[folder] = []
        for note in notes:
            if (
                not isinstance(note, dict)
                or type(note.get("chapter_start")) is not int
                or note["chapter_start"] not in names
            ):
                raise OrganizerError(tr(
                    f"The {folder} entries must reference chapter start numbers.",
                    f"{folder} 需引用章节起始编号",
                ))
            chapter = names[note["chapter_start"]]
            references = [chapter, *(name for name in names.values() if name != chapter)]
            source_links = " · ".join(f"[[lessons/{name}]]" for name in references)
            body = _text(note.get("body"), tr("note content", "笔记正文"))
            source_label = tr("Sources", "来源")
            bundle[folder].append(
                {
                    "slug": _slug(note.get("slug")),
                    "title": _text(note.get("title"), tr("note title", "笔记标题"), multiline=False),
                    "body": f"{body}\n\n{source_label} {source_links}",
                }
            )
    return bundle


class Organizer:
    def __init__(self, records: Records, *, roles_busy=None):
        self.records = records
        self.roles_busy = roles_busy

    def scan(self, vault: Path, *, topic: str | None = None) -> list[dict]:
        if topic is not None:
            topic = validate_topic(topic)
        return [entry for entry in self.records.sealed(vault) if topic is None or entry["topic"] == topic]

    def _ledger(self, vault: Path) -> dict:
        ledger = _read_optional_json(
            vault, _LEDGER, {"version": 1, "outputs": {}, "sources": {}, "chapters": {}}
        )
        if (
            not isinstance(ledger, dict)
            or ledger.get("version") != 1
            or any(not isinstance(ledger.get(field), dict) for field in ("outputs", "sources", "chapters"))
        ):
            raise OrganizerError(tr("The organizer ledger is damaged.", "整理账本损坏"))
        if not isinstance(ledger.setdefault("topics", {}), dict):
            raise OrganizerError(tr("The organizer ledger's topic index is damaged.", "整理账本的主题索引损坏"))
        return ledger

    @staticmethod
    def _stable(vault: Path, source: dict) -> bytes:
        selected = source.get("archive") or source["path"]
        data = optional_bytes(vault, selected)
        if data is None or digest(data) != source["digest"]:
            raise _UnretryablePlanError(tr(
                f"The sealed transcript is missing or has changed: {selected}",
                f"封存原文缺失或发生修改：{selected}",
            ))
        return data

    def snapshot(self, vault: Path, *, topic: str | None = None, source: Path | str | None = None) -> dict:
        vault = vault_directory(vault)
        with file_lease(vault, _LOCK):
            if safe_path(vault, _JOURNAL).exists():
                raise OrganizerError(tr(
                    "The previous organizer transaction needs recovery. Reapply that snapshot first.",
                    "上次整理仍待恢复，请先重新应用该快照",
                ))
            sources = [item for item in self.scan(vault, topic=topic) if not item["archive"]]
            if source is not None:
                selected = Path(source)
                if selected.is_absolute():
                    try:
                        selected = selected.relative_to(vault)
                    except ValueError:
                        raise OrganizerError(tr(
                            "The source must be inside the current library.",
                            "整理来源必须位于当前资料库内",
                        )) from None
                safe_path(vault, selected, must_exist=True)
                sources = [item for item in sources if item["path"] == selected.as_posix()]
            if not sources:
                raise OrganizerError(tr(
                    "There are no sealed lesson records awaiting organization.",
                    "没有待整理的封存课堂记录",
                ))
            if len({item["topic"] for item in sources}) != 1:
                raise OrganizerError(tr(
                    "Choose a single topic before creating an organizer snapshot.",
                    "请指定单个主题后创建整理快照",
                ))
            run = f"{_AREA}/runs/{uuid4().hex}"
            entries = []
            for index, source in enumerate(sources, 1):
                name = f"s{index:03}"
                source_data = self._stable(vault, source)
                entries.append(
                    {
                        "id": name,
                        "lesson_id": source["lesson_id"],
                        "path": source["path"],
                        "sha256": source["digest"],
                        "lines": len(_lines(source_data, source["path"])),
                    }
                )
                atomic_write(vault, f"{run}/sources/{name}.md", source_data, replace=False)
            manifest = {"version": 1, "topic": sources[0]["topic"], "sources": entries}
            atomic_write(vault, f"{run}/manifest.json", json_bytes(manifest), replace=False)
            return {"run": str(vault / run), "manifest": str(vault / run / "manifest.json"), **manifest}

    @staticmethod
    def _run(vault: Path, path: str | Path) -> str:
        path = Path(path)
        if path.is_absolute():
            try:
                path = path.relative_to(vault)
            except ValueError:
                raise _UnretryablePlanError(tr(
                    "The organizer snapshot must be inside the current library.",
                    "整理快照必须位于当前资料库内",
                )) from None
        if path.parts[:2] != (_AREA, "runs") or len(path.parts) != 3 or not _RUN_ID.fullmatch(path.name):
            raise _UnretryablePlanError(tr("Invalid organizer snapshot path.", "整理快照路径无效"))
        safe_path(vault, path, must_exist=True)
        return path.as_posix()

    def _sources(self, vault: Path, run: str):
        manifest = read_json(vault, f"{run}/manifest.json")
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or not isinstance(manifest.get("sources"), list)
        ):
            raise _UnretryablePlanError(tr("Invalid organizer snapshot manifest.", "整理快照清单无效"))
        topic = validate_topic(manifest.get("topic"))
        sealed = {(item["lesson_id"], item["path"]): item for item in self.scan(vault, topic=topic)}
        result = {}
        for item in manifest["sources"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"s[0-9]{3,}", item["id"])
                or item["id"] in result
                or not isinstance(item.get("lesson_id"), str)
                or not isinstance(item.get("path"), str)
            ):
                raise _UnretryablePlanError(tr("Invalid source ID in the organizer snapshot.", "整理快照来源标识无效"))
            source = sealed.get((item["lesson_id"], item["path"]))
            if source is None or source["digest"] != item.get("sha256"):
                raise _UnretryablePlanError(tr(
                    f"No matching seal receipt in the organizer snapshot: {item['id']}",
                    f"整理快照找不到匹配的封存回执：{item['id']}",
                ))
            content = read_bytes(vault, f"{run}/sources/{item['id']}.md")
            lines = _lines(content, source["path"])
            if digest(content) != source["digest"] or item.get("lines") != len(lines):
                raise _UnretryablePlanError(tr(
                    f"The organizer snapshot has changed: {item['id']}",
                    f"整理快照内容发生变化：{item['id']}",
                ))
            self._stable(vault, source)
            result[item["id"]] = (source, lines)
        if not result:
            raise _UnretryablePlanError(tr("The organizer snapshot has no sources.", "整理快照没有来源"))
        return topic, result

    def _render(self, vault: Path, bundle: dict, topic: str, sources: dict, archives: dict, next_session: str) -> tuple[dict, dict]:
        if (
            not isinstance(bundle, dict)
            or bundle.get("schema_version") != 2
            or bundle.get("topic") != topic
            or not isinstance(bundle.get("chapters"), list)
            or not bundle["chapters"]
        ):
            raise OrganizerError(tr(
                "The organization plan must include schema_version 2, the snapshot topic, and chapters.",
                "整理方案需包含 schema_version 2、快照主题和章节",
            ))
        sections = {}
        outputs = {}
        coverage = defaultdict(list)
        boundaries = {name: _structures(lines)[0] for name, (_, lines) in sources.items()}
        for chapter in bundle["chapters"]:
            if not isinstance(chapter, dict) or set(chapter) != {"slug", "title", "slices"}:
                raise OrganizerError(tr(
                    "Each chapter must contain slug, title, and transcript slices.",
                    "章节使用 slug、title 和原文 slices",
                ))
            name = _slug(chapter["slug"])
            title = _text(chapter["title"], tr("chapter title", "章节标题"), multiline=False)
            if not isinstance(chapter["slices"], list) or not chapter["slices"]:
                raise OrganizerError(tr(f"The chapter has no transcript ranges: {name}", f"章节缺少原文范围：{name}"))
            path = f"lessons/{name}.md"
            sections[path] = title
            parts = [f"# {title}\n\n".encode()]
            for piece in chapter["slices"]:
                if not isinstance(piece, dict) or set(piece) != {"id", "start", "end"}:
                    raise OrganizerError(tr(f"Invalid chapter transcript range: {name}", f"章节原文范围无效：{name}"))
                key, start, end = piece["id"], piece["start"], piece["end"]
                if (
                    not isinstance(key, str)
                    or key not in sources
                    or type(start) is not int
                    or type(end) is not int
                ):
                    raise OrganizerError(tr(
                        f"The chapter references an invalid source or line number: {name}",
                        f"章节引用的来源或行号无效：{name}",
                    ))
                source, lines = sources[key]
                if not 1 <= start <= end <= len(lines):
                    raise OrganizerError(tr(
                        f"The chapter references lines outside the source: {key} {start}–{end}",
                        f"章节引用行号越界：{key} {start}–{end}",
                    ))
                if start - 1 in boundaries[key] or (end < len(lines) and end in boundaries[key]):
                    raise OrganizerError(tr(
                        f"The chapter splits a code block, formula, or table: {key} {start}–{end}",
                        f"章节切断代码块、公式或表格：{key} {start}–{end}",
                    ))
                coverage[key].append((start, end))
                source_path = source["path"]
                archive = quote(os.path.relpath(archives[source_path], Path(path).parent))
                reference = tr(
                    f"> [{key} Original Record]({archive}) · Lines {start}–{end}\n\n",
                    f"> [{key} 原始记录]({archive}) · 第 {start}–{end} 行\n\n",
                )
                excerpt = _chapter_links(vault, "".join(lines[start - 1 : end]), source_path, path, archives)
                parts.extend((reference.encode(), excerpt.encode(), b"\n\n"))
            _append_outputs(outputs, path, b"".join(parts))
        for key, (_, lines) in sources.items():
            expected = 1
            for start, end in sorted(coverage[key]):
                if start != expected:
                    raise _UnretryablePlanError(tr(
                        f"The transcript ranges have a gap or overlap: {key}, line {min(start, expected)}.",
                        f"原文覆盖存在遗漏或重叠：{key} 第 {min(start, expected)} 行",
                    ))
                expected = end + 1
            if expected != len(lines) + 1:
                raise _UnretryablePlanError(tr(
                    f"The plan omits the transcript starting at {key}, line {expected}.",
                    f"原文覆盖不完整：{key} 第 {expected} 行起",
                ))
        for category in _CATEGORIES:
            notes = bundle.get(category, [])
            if not isinstance(notes, list):
                raise OrganizerError(tr(
                    f"The plan's {category} field must be a list.",
                    f"整理方案中的 {category} 必须是列表",
                ))
            for note in notes:
                if not isinstance(note, dict) or set(note) != {"slug", "title", "body"}:
                    raise OrganizerError(tr(
                        f"Each {category} entry must contain slug, title, and body.",
                        f"{category} 条目需要 slug、title 和 body",
                    ))
                path = f"{category}/{_slug(note['slug'])}.md"
                title = _text(note["title"], tr("note title", "笔记标题"), multiline=False)
                body = _text(note["body"], tr("note content", "笔记正文"))
                _append_outputs(outputs, path, f"# {title}\n\n{body}\n".encode())
        if bundle.get("next_session") is not None:
            summary = _text(bundle["next_session"], tr("next-session suggestion", "续课参考"))
            language = "zh" if next_session == NEXT_SESSION_PATHS[1] else "en"
            title = tr("Next Session", "下次续课", language=language)
            _append_outputs(outputs, next_session, f"# {title}\n\n{summary}\n".encode())
        return outputs, sections

    def _plan(self, vault: Path, run: str, bundle: dict) -> dict:
        topic, sources = self._sources(vault, run)
        ledger = self._ledger(vault)
        archive_map = {key.split(":", 1)[1]: item["archive"] for key, item in ledger["sources"].items()}
        for source, _ in sources.values():
            archive_map[source["path"]] = f"{_AREA}/archive/{source['lesson_id']}/{Path(source['path']).name}"
        next_session = _managed_path(NEXT_SESSION_PATHS, ledger["outputs"])
        outputs, sections = self._render(vault, bundle, topic, sources, archive_map, next_session)
        source_keys = {f"{source['lesson_id']}:{source['path']}" for source, _ in sources.values()}
        archived = {key for key in source_keys if key in ledger["sources"]}
        run_key = _bundle_digest(bundle)
        for path, expected in ledger["outputs"].items():
            current = optional_bytes(vault, path)
            if current is None or digest(current) != expected:
                raise _UnretryablePlanError(tr(f"The organized file has changed: {path}", f"已整理文件被修改：{path}"))
        if archived:
            if archived == source_keys and all(
                ledger["sources"][key].get("bundle") == run_key for key in archived
            ):
                for path in outputs:
                    current = optional_bytes(vault, path)
                    if current is None or digest(current) != ledger["outputs"].get(path):
                        raise _UnretryablePlanError(tr(
                            f"The organized file has changed: {path}",
                            f"已整理文件被修改：{path}",
                        ))
                return {"status": "unchanged", "outputs": list(outputs), "archives": []}
            raise _UnretryablePlanError(tr(
                "The organizer snapshot includes sources already used by another plan.",
                "整理快照含已经应用到另一方案的来源",
            ))
        ledger["chapters"].update(sections)
        ledger["topics"].update({path: topic for path in outputs})
        entry = _managed_path(READING_ENTRY_NAMES, ledger["outputs"])
        outputs[entry] = _reading_entry(ledger["chapters"], [*ledger["outputs"], *outputs], entry)
        available = set(ledger["outputs"]) | set(outputs) | set(archive_map.values())
        for path, data in outputs.items():
            for link, wiki in _links(_markdown(data, path)):
                if _link_target(vault, path, link, wiki, available) is None:
                    raise OrganizerError(tr(
                        f"Local link target not found: {path} → {link}",
                        f"本地链接找不到目标：{path} → {link}",
                    ))
        updates = []
        for path, data in outputs.items():
            before = optional_bytes(vault, path)
            before_hash = digest(before) if before is not None else None
            expected = ledger["outputs"].get(path)
            if before_hash != expected:
                raise OrganizerError(tr(
                    f"The file contains manual content or has changed: {path}",
                    f"文件已有人工内容或已被修改：{path}",
                ))
            if path in sections and expected is not None:
                raise OrganizerError(tr(
                    f"Keep the existing chapter and choose a new chapter filename: {path}",
                    f"已有章节需保留，请采用新的章节文件名：{path}",
                ))
            updates.append((path, before_hash, data))
            ledger["outputs"][path] = digest(data)
        archives = []
        for source, _ in sources.values():
            key = f"{source['lesson_id']}:{source['path']}"
            relative = archive_map[source["path"]]
            if safe_path(vault, relative).exists():
                raise _UnretryablePlanError(tr(
                    f"The archive file path is already in use: {relative}",
                    f"归档文件已被占用：{relative}",
                ))
            updates.append((relative, None, self._stable(vault, source)))
            archives.append(
                {
                    "lesson_id": source["lesson_id"],
                    "path": source["path"],
                    "archive": relative,
                    "digest": source["digest"],
                }
            )
            ledger["sources"][key] = {"archive": relative, "bundle": run_key, "sha256": source["digest"]}
        ledger_data = optional_bytes(vault, _LEDGER)
        updates.append((_LEDGER, None if ledger_data is None else digest(ledger_data), json_bytes(ledger)))
        return {
            "status": "ready",
            "outputs": list(outputs),
            "archives": archives,
            "updates": updates,
            "bundle": run_key,
        }

    @staticmethod
    def _stage(vault: Path, run: str, plan: dict) -> dict:
        updates = []
        stage = uuid4().hex
        for number, (path, before, data) in enumerate(plan["updates"]):
            staged = f"{run}/stage-{stage}/{number}"
            atomic_write(vault, staged, data, replace=False)
            updates.append({"path": path, "before": before, "after": digest(data), "stage": staged})
        transaction = {
            "version": 1,
            "run": run,
            "bundle": plan["bundle"],
            "updates": updates,
            "outputs": plan["outputs"],
            "archives": plan["archives"],
        }
        atomic_write(vault, _JOURNAL, json_bytes(transaction), replace=False)
        return transaction

    def _finish(self, vault: Path, transaction: dict) -> dict:
        if not isinstance(transaction, dict) or transaction.get("version") != 1:
            raise OrganizerError(tr("Invalid organizer transaction awaiting recovery.", "待恢复整理事务无效"))
        changes = []
        for update in transaction["updates"]:
            data = read_bytes(vault, update["stage"])
            if digest(data) != update["after"]:
                raise OrganizerError(tr(
                    f"The staged file has changed: {update['path']}",
                    f"待提交文件内容发生变化：{update['path']}",
                ))
            before = optional_bytes(vault, update["path"])
            observed = None if before is None else digest(before)
            if observed != update["after"]:
                if observed != update["before"]:
                    raise OrganizerError(tr(
                        f"Manual edits were found during recovery: {update['path']}",
                        f"恢复时发现人工修改：{update['path']}",
                    ))
                changes.append((update["path"], data, before is not None))
        for archive in transaction["archives"]:
            original = optional_bytes(vault, archive["path"])
            archived = optional_bytes(vault, archive["archive"])
            if (
                (original is None and archived is None)
                or (original is not None and digest(original) != archive["digest"])
                or (archived is not None and digest(archived) != archive["digest"])
            ):
                raise OrganizerError(tr(
                    f"The complete sealed source was missing during recovery: {archive['path']}",
                    f"恢复时找不到完整封存来源：{archive['path']}",
                ))
        for path, data, replace in changes:
            atomic_write(vault, path, data, replace=replace)
        with self.records._connect(write=True) as connection:
            for archive in transaction["archives"]:
                change = connection.execute(
                    """UPDATE records SET archive = ? WHERE lesson_id = ? AND path = ?
                       AND digest = ? AND sealed_at IS NOT NULL AND (archive IS NULL OR archive = ?)""",
                    (
                        archive["archive"],
                        archive["lesson_id"],
                        archive["path"],
                        archive["digest"],
                        archive["archive"],
                    ),
                )
                if change.rowcount != 1:
                    raise OrganizerError(tr(
                        f"The archive has no matching seal receipt: {archive['path']}",
                        f"归档缺少对应的封存回执：{archive['path']}",
                    ))
        for archive in transaction["archives"]:
            content = optional_bytes(vault, archive["path"])
            if content is not None:
                if digest(content) != archive["digest"]:
                    raise OrganizerError(tr(
                        f"The sealed source was edited manually during archiving: {archive['path']}",
                        f"封存来源在归档期间发生人工修改：{archive['path']}",
                    ))
                remove_file(vault, archive["path"])
        remove_file(vault, _JOURNAL)
        return {"status": "applied", "outputs": transaction["outputs"], "archives": transaction["archives"]}

    def apply(self, vault: Path, run: str | Path, bundle: dict | str | Path, *, check: bool = False) -> dict:
        vault = vault_directory(vault)
        run = self._run(vault, run)
        if not isinstance(bundle, dict):
            selected = Path(bundle)
            if selected.is_absolute():
                try:
                    selected = selected.relative_to(vault)
                except ValueError:
                    raise OrganizerError(tr(
                        "The plan must be stored in an organizer snapshot in the library.",
                        "整理方案须位于资料库的整理快照中",
                    )) from None
            elif len(selected.parts) == 1:
                selected = Path(run) / selected
            if selected.parent != Path(run):
                raise OrganizerError(tr(
                    "The plan must be stored in the current organizer snapshot.",
                    "整理方案须位于当前整理快照中",
                ))
            bundle = read_json(vault, selected)
        with file_lease(vault, _LOCK):
            pending = _read_optional_json(vault, _JOURNAL, None)
            if pending is not None:
                if (
                    not isinstance(pending, dict)
                    or pending.get("run") != run
                    or pending.get("bundle") != _bundle_digest(bundle)
                ):
                    raise _UnretryablePlanError(tr(
                        "Another organizer plan needs recovery. Reapply its snapshot first.",
                        "另一个整理方案待恢复，请先重新应用对应快照",
                    ))
                if check:
                    return {
                        "status": "pending",
                        "outputs": pending["outputs"],
                        "archives": pending["archives"],
                    }
                return self._finish(vault, pending)
            plan = self._plan(vault, run, bundle)
            if check or plan["status"] == "unchanged":
                return {name: plan[name] for name in ("status", "outputs", "archives")}
            return self._finish(vault, self._stage(vault, run, plan))

    def plan(self, vault: Path, run: Path | str, complete) -> dict:
        """Call the explicit organizer completion and persist a checked chapter bundle."""
        vault = vault_directory(vault)
        run = self._run(vault, run)
        path = f"{run}/bundle.json"
        if safe_path(vault, path).exists():
            checked = self.apply(vault, run, Path(vault / path), check=True)
            return {**checked, "bundle": str(vault / path), "calls": 0}
        topic, sources = self._sources(vault, run)
        ledger = self._ledger(vault)
        reserved = set(ledger["outputs"])
        for folder in ("lessons", *_CATEGORIES):
            base = safe_path(vault, folder)
            if base.is_dir():
                reserved.update(path.relative_to(vault).as_posix() for path in base.glob("*.md"))
        segments = _segments(sources)
        correction = ""
        for calls in (1, 2):
            payload = organizer_plan.prompt_payload(topic, segments, sorted(reserved), correction)
            if len(payload[0]["text"].encode()) > MAX_PLANNING_BYTES:
                raise OrganizerError(
                    tr(
                        f"The organizer input exceeds {MAX_PLANNING_BYTES} bytes. Select individual sealed "
                        f"records or specify chapter ranges in the existing snapshot: {vault / run}",
                        f"整理规划输入超过 {MAX_PLANNING_BYTES} 字节。可逐份选择封存记录，"
                        f"或基于现有快照指定章节范围：{vault / run}",
                    )
                )
            try:
                result = complete(
                    instructions=organizer_plan.instructions(),
                    input=payload,
                    json_schema=organizer_plan.SCHEMA,
                    schema_name="teacher_organizer",
                    purpose="organize-sealed-lessons",
                    max_tokens=3600,
                    timeout=100,
                )
            except ValueError as error:
                correction = str(error)
                continue
            except Exception as error:
                raise OrganizerError(
                    tr(
                        f"The organizer role failed ({type(error).__name__}). Snapshot: {vault / run}",
                        f"整理角色调用失败（{type(error).__name__}）。快照位于 {vault / run}",
                    )
                ) from error
            try:
                bundle = _planned_bundle(topic, segments, result.parsed)
                checked = self.apply(vault, run, bundle, check=True)
            except _UnretryablePlanError:
                raise
            except OrganizerError as error:
                correction = str(error)
                continue
            try:
                atomic_write(vault, path, json_bytes(bundle), replace=False)
            except FileExistsError:
                checked = self.apply(vault, run, vault / path, check=True)
            return {
                **checked,
                "bundle": str(vault / path),
                "calls": calls,
                "model": getattr(result, "model", ""),
                "provider": getattr(result, "provider", ""),
            }
        raise OrganizerError(tr(
            f"The organization plan failed validation ({correction}). Snapshot: {vault / run}",
            f"整理方案未通过检查：{correction}；快照位于 {vault / run}",
        ))

    def organize(
        self,
        vault: Path,
        complete,
        *,
        topic: str | None = None,
        source: Path | str | None = None,
        check: bool = False,
    ) -> dict:
        vault = vault_directory(vault)
        transaction = _read_optional_json(vault, _JOURNAL, None)
        if transaction is not None:
            if not isinstance(transaction, dict) or not isinstance(transaction.get("run"), str):
                raise OrganizerError(tr("Invalid organizer transaction awaiting recovery.", "待恢复整理事务无效"))
            run = self._run(vault, transaction["run"])
            selected, _ = self._sources(vault, run)
            if source is not None or (topic is not None and selected != validate_topic(topic)):
                raise OrganizerError(tr(
                    f"Another organizer plan needs recovery: {vault / run}",
                    f"另一个整理方案待恢复：{vault / run}",
                ))
            snapshot = {"run": str(vault / run)}
        else:
            snapshot = self.snapshot(vault, topic=topic, source=source)
        planned = self.plan(vault, snapshot["run"], complete)
        if check:
            return {**snapshot, **planned}
        applied = self.apply(vault, snapshot["run"], planned["bundle"])
        return {**snapshot, **planned, **applied}

    @staticmethod
    def _tree(vault: Path, start: str | None = None, *, include_dirs: bool = False, resources=False):
        base = safe_path(vault, start) if start else vault
        if not base.exists():
            return
        if not base.is_dir():
            raise OrganizerError(tr(f"Relocation requires a directory: {base}", f"搬迁范围需要目录：{base}"))
        for directory, folders, files in os.walk(base, followlinks=False):
            if resources:
                folders[:] = [name for name in folders if name not in {
                    ".git", "node_modules", "target", "__pycache__", ".cache", ".pytest_cache",
                    ".mypy_cache", ".ruff_cache", ".venv", "venv",
                }]
            for name in folders:
                relative = (Path(directory) / name).relative_to(vault)
                safe_path(vault, relative, must_exist=True)
                if include_dirs:
                    yield relative.as_posix() + "/"
            for name in files:
                relative = (Path(directory) / name).relative_to(vault)
                path = relative.as_posix()
                if (
                    path.endswith((f"/{_LOCK}", f"/{_MOVE}"))
                    or path in {_LOCK, _MOVE}
                    or relative.name == ".teacher-vaults.lock"
                ):
                    continue
                if path == _JOURNAL or path.endswith(f"/{_JOURNAL}"):
                    raise OrganizerError(tr(
                        f"The organizer transaction is still pending: {path}",
                        f"整理事务尚未完成：{path}",
                    ))
                safe_path(vault, path, must_exist=True)
                yield path

    def _move_lessons(self, connection, source: Path, topic: str | None):
        result = []
        for lesson in connection.execute("SELECT * FROM lessons ORDER BY id"):
            vault = Path(lesson["vault"])
            if topic is None:
                selected = vault == source or source in vault.parents
            else:
                selected = vault == source and lesson["topic"] == topic and not lesson["temporary"]
            if selected:
                result.append(lesson)
        return result

    def _lesson_files(self, connection, lessons) -> tuple[set[str], set[str]]:
        paths, external = set(), set()
        for lesson in lessons:
            vault = vault_directory(lesson["vault"])
            if connection.execute("SELECT 1 FROM writes WHERE lesson_id = ?", (lesson["id"],)).fetchone():
                raise OrganizerError(tr(
                    f"Open the lesson to recover pending writes: {lesson['topic']}",
                    f"课堂尚有待恢复写入，请先打开课堂：{lesson['topic']}",
                ))
            if (
                lesson["record"]
                and not connection.execute(
                    "SELECT 1 FROM records WHERE lesson_id = ? AND path = ?",
                    (lesson["id"], lesson["record"]),
                ).fetchone()
            ):
                raise OrganizerError(tr(
                    f"The link to the active lesson record is damaged: {lesson['id']}",
                    f"活动记录的关联损坏：{lesson['id']}",
                ))
            record_locations = []
            for record in connection.execute("SELECT * FROM records WHERE lesson_id = ?", (lesson["id"],)):
                record_locations.append(record["path"])
                location = record["archive"] or record["path"]
                content = optional_bytes(vault, location)
                if (None if content is None else digest(content)) != record["digest"]:
                    raise OrganizerError(tr(
                        f"The lesson record is missing or has changed: {location}",
                        f"课堂记录缺失或内容已修改：{location}",
                    ))
                if content is not None:
                    markdown = _markdown(content, location)
                    for link, wiki in _links(markdown):
                        target = _link_target(vault, record["path"], link, wiki, set())
                        if target:
                            paths.add(target)
                    paths.add(location)
            for session in connection.execute("SELECT id FROM sessions WHERE lesson_id = ?", (lesson["id"],)):
                self.records._events(connection, lesson, session["id"])
            saved = connection.execute(
                "SELECT * FROM checkpoints WHERE lesson_id = ?", (lesson["id"],)
            ).fetchone()
            if saved is not None:
                content = optional_bytes(vault, saved["path"])
                if content is None or digest(content) != saved["digest"]:
                    raise OrganizerError(tr(
                        f"The handoff note is missing or has changed: {saved['path']}",
                        f"续课说明缺失或内容已修改：{saved['path']}",
                    ))
                paths.add(saved["path"])
            for event in connection.execute(
                "SELECT text FROM events WHERE lesson_id = ? AND visible = 1",
                (lesson["id"],),
            ):
                for link, wiki in _links(event["text"]):
                    linked = _link_target(vault, "课堂.md", link, wiki, set())
                    if not linked:
                        linked = next((found for origin in record_locations
                                       if (found := _link_target(vault, origin, link, wiki, set()))), None)
                    if linked:
                        paths.add(linked)
                        figure = re.fullmatch(
                            r"(viz/teacher-(mermaid|svg)-[0-9a-f]{32}-[1-9][0-9]*)\.png", linked
                        )
                        if figure:
                            ext = ".mmd" if figure[2] == "mermaid" else ".svg"
                            companion = figure[1] + ext
                            if safe_path(vault, companion).is_file():
                                paths.add(companion)
                    elif linked is None:
                        external.add(link)
        return paths, external

    def _topic_files(
        self, source: Path, topic: str, paths: set[str], external: set[str], lessons
    ) -> tuple[set[str], dict[str, bytes]]:
        ledger = self._ledger(source)
        output = {
            path: hashed for path, hashed in ledger["outputs"].items() if ledger["topics"].get(path) == topic
        }
        for path, hashed in output.items():
            content = optional_bytes(source, path)
            if content is None or digest(content) != hashed:
                raise OrganizerError(tr(
                    f"The organized note is missing or has changed: {path}",
                    f"已整理笔记缺失或内容已修改：{path}",
                ))
        paths.update(output)
        topic_folders = dict.fromkeys(topic_folder(topic, language=language) for language in ("en", "zh"))
        for folder in ("lessons", "exercises", "练习", "materials"):
            for name in topic_folders:
                paths.update(self._tree(source, f"{folder}/{name}", resources=True))
        paths.update(self._tree(source, "viz", resources=True))
        runs = safe_path(source, f"{_AREA}/runs")
        if runs.exists():
            for run in runs.iterdir():
                if _RUN_ID.fullmatch(run.name):
                    relative = run.relative_to(source).as_posix()
                    manifest = _read_optional_json(source, f"{relative}/manifest.json", {})
                    if manifest.get("topic") == topic:
                        paths.update(self._tree(source, relative))
        chapters = {path: title for path, title in ledger["chapters"].items() if path in output}
        selected_ids = {lesson["id"] for lesson in lessons}
        archived = {
            key: item for key, item in ledger["sources"].items() if key.split(":", 1)[0] in selected_ids
        }
        generated = {MARKER: json_bytes({"version": 1, "topic": topic})}
        if output or archived:
            entry_path = _managed_path(READING_ENTRY_NAMES, ledger["outputs"])
            entry = _reading_entry(chapters, output, entry_path)
            generated[entry_path] = entry
            generated[_LEDGER] = json_bytes(
                {
                    "version": 1,
                    "sources": archived,
                    "chapters": chapters,
                    "topics": {path: topic for path in output},
                    "outputs": {**output, entry_path: digest(entry)},
                }
            )
        # Include linked assets transitively while preserving each relative location.
        projects = safe_path(source, "exercises")
        candidates = {p.name: p for p in projects.iterdir()} if projects.is_dir() else {}

        def referenced_projects(text):
            for match in re.finditer(r"(?:^|(?<![\w/]))exercises/([^/\\<>\"'`|&;()\[\]{},\n\r]+)", unquote(text)):
                name = match[1].strip().rstrip(".:!?。，；")
                if name in candidates:
                    path = safe_path(source, Path("exercises") / name)
                    if path.is_dir():
                        yield from self._tree(source, f"exercises/{name}", resources=True)
                    else:
                        yield f"exercises/{name}"

        with self.records._connect() as db:
            for lesson in lessons:
                for row in db.execute("SELECT text FROM events WHERE lesson_id=? AND visible=1", (lesson["id"],)):
                    paths.update(referenced_projects(row["text"]))
        remaining = list(paths)
        while remaining:
            current = remaining.pop()
            if current.startswith(_AREA + "/") or Path(current).suffix.lower() != ".md":
                continue
            content = _markdown(read_bytes(source, current), current)
            for resource in referenced_projects(content):
                if resource not in paths:
                    paths.add(resource)
                    remaining.append(resource)
            for link, wiki in _links(content):
                target = _link_target(source, current, link, wiki, paths)
                if (
                    target
                    and target not in paths
                    and not target.startswith(_AREA + "/")
                    and target not in generated
                ):
                    paths.add(target)
                    remaining.append(target)
                elif target is None:
                    external.add(link)
        return paths - generated.keys(), generated

    @staticmethod
    def _existing_target(destination: Path, manifest: dict):
        if not destination.exists() and not destination.is_symlink():
            return False
        vault_directory(destination)
        found = _read_optional_json(destination, _MOVE, None)
        if found != manifest:
            raise OrganizerError(tr(
                f"The relocation destination is already in use: {destination}",
                f"搬迁目标已被占用：{destination}",
            ))
        for path, expected in manifest["files"].items():
            data = optional_bytes(destination, path)
            if data is None or digest(data) != expected:
                raise OrganizerError(tr(
                    f"The relocation destination file is missing or has changed: {path}",
                    f"搬迁目标内容缺失或已修改：{path}",
                ))
        for path in manifest["directories"]:
            vault_directory(safe_path(destination, path, must_exist=True))
        return True

    @staticmethod
    def _publish(source: Path, destination: Path, generated: dict, manifest: dict):
        parent = vault_directory(destination.parent, create=True)
        with tempfile.TemporaryDirectory(prefix=".teacher-move-", dir=parent) as directory:
            stage = Path(directory)
            for path in manifest["directories"]:
                vault_directory(safe_path(stage, path), create=True)
            for path, expected in manifest["files"].items():
                content = generated[path] if path in generated else read_bytes(source, path)
                if digest(content) != expected:
                    raise OrganizerError(tr(
                        f"The source changed while preparing the relocation: {path}",
                        f"搬迁准备期间来源发生变化：{path}",
                    ))
                atomic_write(stage, path, content, replace=False)
            atomic_write(stage, _MOVE, json_bytes(manifest), replace=False)
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1) != 0:
                error = ctypes.get_errno()
                raise OrganizerError(tr(
                    f"Cannot publish the relocation directory {destination} ({os.strerror(error)}).",
                    f"无法发布搬迁目录：{destination}（{os.strerror(error)}）",
                ))
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _topic_destination(source: Path, topic: str) -> Path:
        primary = source / "vaults" / topic_folder(topic)
        alternate_language = tr("zh", "en")
        alternate = source / "vaults" / topic_folder(topic, language=alternate_language)
        if alternate != primary and not alternate.is_symlink() and (alternate / _MOVE).is_file():
            manifest = _read_optional_json(alternate, _MOVE, None)
            if isinstance(manifest, dict) and (
                manifest.get("kind"), manifest.get("source"), manifest.get("topic")
            ) == ("topic", str(source), topic):
                return alternate
        return primary

    def _move(self, source: Path, destination: Path | None, *, topic: str | None, check: bool) -> dict:
        source = vault_directory(source)
        kind = "root" if topic is None else "topic"
        if self.records.state == source or source in self.records.state.parents:
            raise OrganizerError(tr(
                "A lesson library cannot contain the Hermes profile.",
                "课程资料库不能包含 Hermes profile",
            ))
        with self.records.catalog_lease(), ExitStack() as locks:
            if destination is None:
                destination = self._topic_destination(source, topic)
            lookup = (kind, str(source), str(destination), topic or "")
            if (
                not destination.is_absolute()
                or destination == source
                or ".." in destination.parts
                or (topic is None and (source in destination.parents or destination in source.parents))
                or (topic is not None and source not in destination.parents)
            ):
                raise OrganizerError(tr(
                    "Invalid relative locations for the relocation source and destination.",
                    "搬迁目标与来源的目录位置无效",
                ))
            if topic is not None:
                safe_path(source, destination.relative_to(source))
            self.records._recover_routes()
            with self.records._connect() as connection:
                succeeded = connection.execute(
                    "SELECT * FROM migrations WHERE kind = ? AND source = ? AND destination = ? AND topic = ?",
                    lookup,
                ).fetchone()
                if succeeded is not None:
                    vault_directory(destination)
                    return {"status": "unchanged", "source": str(source), "destination": str(destination)}
                lessons = self._move_lessons(connection, source, topic)
            if topic is not None:
                if not lessons:
                    raise OrganizerError(tr(
                        f"No lessons for this topic can be moved from the source library: {topic}",
                        f"来源资料库找不到可搬迁的主题课堂：{topic}",
                    ))
                if vault_topic(source) is not None:
                    raise OrganizerError(tr(
                        "Use library root relocation to move an entire standalone topic library.",
                        "独立主题库整目录搬迁请使用管理根搬迁",
                    ))
                for title, target in topic_vaults(source).items():
                    if (
                        title == topic
                        or destination == target
                        or target in destination.parents
                        or destination in target.parents
                    ):
                        raise OrganizerError(tr(
                            f"The relocation destination conflicts with an existing library mapping: {title} → {target}",
                            f"搬迁目标与现有资料库映射冲突：{title} → {target}",
                        ))
            organizer_scope = {source, *(Path(lesson["vault"]) for lesson in lessons)}
            if topic is None:
                organizer_scope.update(topic_vaults(source).values())
            for vault in sorted(organizer_scope):
                locks.enter_context(file_lease(vault, _LOCK))
                if safe_path(vault, _JOURNAL).exists():
                    raise OrganizerError(tr(
                        f"An organizer transaction needs recovery: {vault}",
                        f"有待恢复的整理事务：{vault}",
                    ))
                self._ledger(vault)
            for lesson in lessons:
                locks.enter_context(self.records.turn_lease(lesson["id"]))
                if self.roles_busy is not None and self.roles_busy(lesson["id"]):
                    raise BusyError(tr(
                        f"A lesson role is running: {lesson['topic']}",
                        f"课堂角色正在运行：{lesson['topic']}",
                    ))
                locks.enter_context(self.records.lease(lesson["id"]))
            with self.records._connect() as connection:
                paths, external = self._lesson_files(connection, lessons)
            generated = {}
            directories = []
            if topic is None:
                tree = set(self._tree(source, include_dirs=True))
                directories = sorted(path[:-1] for path in tree if path.endswith("/"))
                paths = tree - {path + "/" for path in directories}
            else:
                paths, generated = self._topic_files(source, topic, paths, external, lessons)
            contents = {path: digest(read_bytes(source, path)) for path in sorted(paths)}
            contents.update({path: digest(data) for path, data in generated.items()})
            manifest = {
                "version": 1,
                "kind": kind,
                "source": str(source),
                "topic": topic,
                "files": contents,
                "directories": directories,
                "lessons": [lesson["id"] for lesson in lessons],
            }
            already_published = self._existing_target(destination, manifest)
            summary = {
                "status": "ready",
                "source": str(source),
                "destination": str(destination),
                "files": len(contents),
                "lessons": manifest["lessons"],
                "external_links": sorted(external),
            }
            if check:
                return summary
            if not already_published:
                self._publish(source, destination, generated, manifest)
            with self.records._connect(write=True) as connection:
                for lesson in lessons:
                    current = self.records._lesson(connection, lesson["id"])
                    record = current["record"]
                    if topic is not None:
                        sealed = self.records._seal(connection, current, rotate=False)
                        if sealed["status"] == "sealed":
                            record = None
                        if current["recording"]:
                            record = self.records._active_path(
                                destination, current["topic"], current["id"], False
                            )
                            connection.execute(
                                "INSERT INTO records(lesson_id, path) VALUES (?, ?)", (current["id"], record)
                            )
                    new_vault = destination / Path(current["vault"]).relative_to(source)
                    connection.execute(
                        "UPDATE lessons SET vault = ?, record = ? WHERE id = ?",
                        (str(new_vault), record, current["id"]),
                    )
                connection.execute(
                    "INSERT INTO migrations VALUES (?, ?, ?, ?, ?)", (*lookup, int(topic is not None))
                )
            self.records._recover_routes()
            return {**summary, "status": "moved"}

    def migrate_topic(
        self, source: Path, topic: str, destination: Path | None = None, *, check: bool = False
    ) -> dict:
        return self._move(
            Path(source), Path(destination) if destination is not None else None,
            topic=validate_topic(topic), check=check,
        )

    def relocate(self, source: Path, destination: Path, *, check: bool = False) -> dict:
        return self._move(Path(source), Path(destination), topic=None, check=check)

"""Path validation and topic routing for Teacher libraries."""

from __future__ import annotations

import codecs
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from uuid import uuid4

from .i18n import tr


REGISTRY = ".teacher-vaults.json"
MARKER = ".teacher-vault.json"
TEMPORARY_TOPICS = ("Temporary Discussion", "临时讨论")
PENDING_FOLDERS = ("_pending", "_待整理")
MATERIAL_FOLDERS = frozenset({"concepts", "reviews", "materials"})
MATERIAL_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".tex", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".xml",
})
MATERIAL_PAGE_BYTES = 24_000
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class LibraryError(ValueError):
    """A library path or topic cannot be used safely."""


class BusyError(LibraryError):
    """Another writer holds the operation's lease."""


def validate_topic(value: str) -> str:
    if not isinstance(value, str):
        raise LibraryError(tr("Enter a lesson topic.", "请提供课堂主题"))
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 200 or _CONTROL.search(normalized):
        raise LibraryError(tr(
            "A lesson topic must contain 1–200 characters and cannot include control characters.",
            "课堂主题必须是 1–200 个字符，且不能包含控制字符",
        ))
    return normalized


def topic_folder(topic: str, *, language=None) -> str:
    topic = validate_topic(topic)
    cleaned = re.sub(r'[/\\:*?"<>|]', "_", topic).strip(". ")
    while len(cleaned.encode()) > 155:
        cleaned = cleaned[:-1]
    if cleaned == topic and cleaned not in {*TEMPORARY_TOPICS, *PENDING_FOLDERS}:
        return cleaned
    name = cleaned or tr("Topic", "学习主题", language=language)
    return f"{name}--{hashlib.sha256(topic.encode()).hexdigest()[:10]}"


def _relative(value: str | Path) -> tuple[str, ...]:
    spelling = os.fspath(value)
    pieces = tuple(spelling.split("/"))
    if "\\" in spelling or _CONTROL.search(spelling) or any(part in {"", ".", ".."} for part in pieces):
        raise LibraryError(tr(f"Invalid path relative to the library: {spelling}", f"资料库相对路径无效：{spelling}"))
    return pieces


@contextmanager
def _directories(start: str | int, parts: tuple[str, ...], *, create: bool = False):
    parent = os.open(start, _DIR) if isinstance(start, str) else os.dup(start)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR, dir_fd=parent)
            os.close(parent)
            parent = child
        yield parent
    finally:
        os.close(parent)


@contextmanager
def _root(path: Path, *, create: bool = False):
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or _CONTROL.search(str(path)):
        raise LibraryError(tr(f"Enter an absolute library path: {path}", f"请提供资料库的绝对目录：{path}"))
    with _directories("/", path.parts[1:], create=create) as descriptor:
        yield descriptor


def vault_directory(value: str | Path, *, create: bool = False) -> Path:
    path = Path(value)
    try:
        with _root(path, create=create):
            return path
    except OSError as error:
        raise LibraryError(tr(
            f"Cannot access the library directory {path} ({error.strerror}).",
            f"无法访问资料库目录：{path}（{error.strerror}）",
        )) from error


def safe_path(vault: str | Path, relative: str | Path, *, must_exist: bool = False) -> Path:
    """Return an in-vault path after rejecting traversal and symbolic links."""
    root = vault_directory(vault)
    candidate = root
    missing = False
    for part in _relative(relative):
        candidate = candidate / part
        if missing:
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if must_exist:
                raise LibraryError(tr(
                    f"Library path does not exist: {relative}",
                    f"资料库路径不存在：{relative}",
                )) from None
            missing = True
            continue
        except OSError as error:
            raise LibraryError(tr(f"Invalid library path: {relative}", f"资料库路径无效：{relative}")) from error
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise LibraryError(tr(f"Library paths cannot contain links: {relative}", f"资料库路径不能包含链接：{relative}"))
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise LibraryError(tr(f"Invalid library path type: {relative}", f"资料库路径类型无效：{relative}"))
    return candidate


def _file_info(descriptor: int, name: str):
    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LibraryError(tr(f"Managed files must be standalone regular files: {name}", f"受管文件必须是独立的普通文件：{name}"))
    return info


@contextmanager
def _read_file(vault: Path, relative: str | Path):
    parts = _relative(relative)
    try:
        with _root(vault) as root, _directories(root, parts[:-1]) as directory:
            fd = os.open(parts[-1], _READ, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise LibraryError(tr(
                        f"Managed files must be standalone regular files: {relative}",
                        f"受管文件必须是独立的普通文件：{relative}",
                    ))
                yield stream
    except FileNotFoundError:
        raise
    except OSError as error:
        raise LibraryError(tr(
            f"Cannot read managed file {relative} ({error.strerror}).",
            f"无法读取受管文件：{relative}（{error.strerror}）",
        )) from error


def read_bytes(vault: Path, relative: str | Path, *, limit: int | None = None) -> bytes:
    with _read_file(vault, relative) as stream:
        data = stream.read() if limit is None else stream.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise LibraryError(tr(f"File exceeds {limit} bytes: {relative}", f"文件超过 {limit} 字节：{relative}"))
        return data


def atomic_write(vault: Path, relative: str | Path, data: bytes, *, replace: bool = True) -> None:
    parts = _relative(relative)
    with _root(vault) as root, _directories(root, parts[:-1], create=True) as directory:
        if replace:
            try:
                _file_info(directory, parts[-1])
            except FileNotFoundError:
                pass
        temporary = f".teacher-{uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(
                    temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
                )
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def remove_file(vault: Path, relative: str | Path) -> None:
    parts = _relative(relative)
    with _root(vault) as root, _directories(root, parts[:-1]) as directory:
        _file_info(directory, parts[-1])
        os.unlink(parts[-1], dir_fd=directory)
        os.fsync(directory)


@contextmanager
def file_lease(vault: Path, relative: str | Path, *, wait=False):
    parts = _relative(relative)
    with _root(vault) as root, _directories(root, parts[:-1], create=True) as directory:
        fd = os.open(
            parts[-1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LibraryError(tr(
                    f"Lock files must be standalone regular files: {relative}",
                    f"受管锁必须是独立的普通文件：{relative}",
                ))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyError(tr(f"Another process is writing to {relative}.", f"另一写者正在操作：{relative}")) from None
            yield
        finally:
            os.close(fd)


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def read_json(vault: Path, relative: str | Path):
    try:
        return json.loads(read_bytes(vault, relative, limit=8_000_000))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LibraryError(tr(f"Invalid JSON file: {relative}", f"JSON 文件无效：{relative}")) from error


def vault_topic(vault: Path) -> str | None:
    try:
        data = read_json(vault, MARKER)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise LibraryError(tr(f"Invalid library topic marker: {vault / MARKER}", f"资料库主题标记无效：{vault / MARKER}"))
    topic = data.get("topic")
    if validate_topic(topic) != topic:
        raise LibraryError(tr(f"Invalid library topic marker: {vault / MARKER}", f"资料库主题标记无效：{vault / MARKER}"))
    return topic


def topic_vaults(root: str | Path) -> dict[str, Path]:
    root = vault_directory(root)
    try:
        data = read_json(root, REGISTRY)
    except FileNotFoundError:
        return {}
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("vaults"), dict)
    ):
        raise LibraryError(tr(f"Invalid library registry: {root / REGISTRY}", f"资料库注册表无效：{root / REGISTRY}"))
    mapped: dict[str, Path] = {}
    for topic, relative in data["vaults"].items():
        if validate_topic(topic) != topic or not isinstance(relative, str):
            raise LibraryError(tr(f"Invalid library mapping: {topic}", f"资料库映射无效：{topic}"))
        target = vault_directory(safe_path(root, relative, must_exist=True))
        nested = safe_path(target, REGISTRY)
        if nested.exists() or vault_topic(target) != topic:
            raise LibraryError(tr(
                f"Library marker conflicts with the mapping or contains a nested registry: {topic} → {relative}",
                f"资料库标记与映射不符或包含嵌套注册表：{topic} → {relative}",
            ))
        if any(
            existing == target or existing in target.parents or target in existing.parents
            for existing in mapped.values()
        ):
            raise LibraryError(tr(f"Mapped library directories overlap: {target}", f"资料库映射目录重叠：{target}"))
        mapped[topic] = target
    return mapped


def management_root(vault: Path) -> Path:
    current = vault_topic(vault)
    if current:
        for parent in vault.parents:
            if parent == Path("/"):
                break
            registry = safe_path(parent, REGISTRY)
            if registry.exists() and topic_vaults(parent).get(current) == vault:
                return parent
    return vault


def configure_topic_vault(
    root: str | Path, topic: str, relative: str | Path | None = None, *, create: bool = False
) -> Path:
    root = vault_directory(root)
    topic = validate_topic(topic)
    with file_lease(root, ".teacher-vaults.lock"):
        mappings = topic_vaults(root)
        if relative is None:
            relative = (
                mappings[topic].relative_to(root) if topic in mappings else f"vaults/{topic_folder(topic)}"
            )
        target = safe_path(root, relative)
        if topic in mappings:
            if mappings[topic] != target:
                raise LibraryError(tr(
                    f"Topic already belongs to another library: {topic}",
                    f"主题已绑定另一个资料库：{topic}",
                ))
            return target
        if vault_topic(root) is not None:
            raise LibraryError(tr(
                "A standalone topic library cannot have its own registry.",
                "独立主题库不能建立自己的资料库注册表",
            ))
        if any(
            existing == target or existing in target.parents or target in existing.parents
            for existing in mappings.values()
        ):
            raise LibraryError(tr(f"Mapped library directories overlap: {target}", f"资料库映射目录重叠：{target}"))
        if not target.exists():
            if not create:
                raise LibraryError(tr(f"Library directory has yet to be created: {target}", f"资料库目录尚未创建：{target}"))
            vault_directory(target, create=True)
        else:
            vault_directory(target)
        if safe_path(target, REGISTRY).exists():
            raise LibraryError(tr(f"Library contains a nested registry: {target}", f"资料库包含嵌套注册表：{target}"))
        marker = vault_topic(target)
        if marker is None:
            if not create:
                raise LibraryError(tr(f"Missing library topic marker: {target}", f"缺少资料库主题标记：{target}"))
            if any(target.iterdir()):
                raise LibraryError(tr(
                    f"Library directory already contains files: {target}",
                    f"资料库目录已有内容：{target}",
                ))
            atomic_write(target, MARKER, json_bytes({"version": 1, "topic": topic}), replace=False)
        elif marker != topic:
            raise LibraryError(tr(f"Library already belongs to another topic: {marker}", f"资料库已属于其他主题：{marker}"))
        mappings[topic] = target
        atomic_write(
            root,
            REGISTRY,
            json_bytes(
                {
                    "version": 1,
                    "vaults": {key: path.relative_to(root).as_posix() for key, path in mappings.items()},
                }
            ),
        )
        return target


def list_materials(vault: str | Path) -> list[str]:
    root = vault_directory(vault)
    paths = []
    for category in sorted(MATERIAL_FOLDERS):
        folder = safe_path(root, category)
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise LibraryError(tr(f"Invalid study materials directory: {folder}", f"学习资料目录无效：{folder}"))
        for directory, folders, files in os.walk(folder, followlinks=False):
            folders[:] = sorted(name for name in folders if not name.startswith("."))
            for name in sorted(files):
                path = Path(directory) / name
                if not name.startswith(".") and path.suffix.lower() in MATERIAL_SUFFIXES:
                    relative = path.relative_to(root)
                    safe_path(root, relative, must_exist=True)
                    paths.append(relative.as_posix())
    return paths


def _material_path(vault, path):
    root = vault_directory(vault)
    parts = _relative(path)
    if (
        len(parts) < 2
        or parts[0] not in MATERIAL_FOLDERS
        or any(part.startswith(".") for part in parts)
        or Path(parts[-1]).suffix.lower() not in MATERIAL_SUFFIXES
    ):
        raise LibraryError(tr(
            "Readable text materials are limited to supported files in concepts, reviews, and materials.",
            "只能读取 concepts、reviews、materials 中支持的文本资料",
        ))
    return root


def read_material(vault: str | Path, path: str | Path) -> str:
    root = _material_path(vault, path)
    try:
        return read_bytes(root, path, limit=1_000_000).decode()
    except (FileNotFoundError, UnicodeError) as error:
        raise LibraryError(tr(f"Cannot find readable study material: {path}", f"找不到可读的学习资料：{path}")) from error


def material_page(vault, path, *, start_line=1, line_count=200, start_byte=0):
    """Read a bounded UTF-8 page. start_byte is an offset within the starting line."""
    if type(start_line) is not int or start_line < 1 or type(line_count) is not int or not 1 <= line_count <= 500:
        raise LibraryError(tr(
            "Use a positive starting line and read 1–500 lines at a time.",
            "起始行须为正整数，每段读取 1–500 行",
        ))
    if type(start_byte) is not int or start_byte < 0:
        raise LibraryError(tr("The byte offset within a line must be a non-negative integer.", "行内起始字节须为非负整数"))
    root = _material_path(vault, path)
    try:
        with _read_file(root, path) as stream:
            line = 1
            while line < start_line:
                chunk = stream.readline(MATERIAL_PAGE_BYTES)
                if not chunk:
                    break
                if chunk.endswith(b"\n"):
                    line += 1
            remaining = start_byte
            while remaining:
                chunk = stream.readline(min(remaining, MATERIAL_PAGE_BYTES))
                if not chunk or chunk.endswith(b"\n"):
                    raise LibraryError(tr("The byte offset exceeds the length of the line.", "起始字节超出该行范围"))
                remaining -= len(chunk)
            data = bytearray()
            offset = start_byte
            while line < start_line + line_count and len(data) < MATERIAL_PAGE_BYTES:
                chunk = stream.readline(MATERIAL_PAGE_BYTES - len(data))
                if not chunk:
                    break
                data.extend(chunk)
                if chunk.endswith(b"\n"):
                    line += 1
                    offset = 0
                else:
                    offset += len(chunk)
            more = bool(stream.peek(1))
            decoder = codecs.getincrementaldecoder("utf-8")()
            content = decoder.decode(data, final=not more)
            offset -= len(decoder.getstate()[0])
            return {
                "path": str(path),
                "text": content,
                "start_line": start_line,
                "start_byte": start_byte,
                "next": {"start_line": line, "start_byte": offset} if more else None,
            }
    except (FileNotFoundError, UnicodeError) as error:
        raise LibraryError(tr(
            f"Cannot find readable UTF-8 study material: {path}",
            f"找不到可读的 UTF-8 学习资料：{path}",
        )) from error

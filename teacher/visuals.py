"""Render scoped classroom SVG and Mermaid sources to reviewable PNG files."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from .i18n import tr
from .models import write_private


SVG_NS = "{http://www.w3.org/2000/svg}"
SVG_ELEMENTS = {
    "svg",
    "g",
    "defs",
    "title",
    "desc",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "path",
    "text",
    "tspan",
    "marker",
    "clipPath",
}
SVG_ATTRIBUTES = set(
    """
    id viewBox width height preserveAspectRatio transform opacity fill fill-opacity fill-rule
    stroke stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray
    stroke-dashoffset stroke-miterlimit x y x1 x2 y1 y2 cx cy r rx ry points d dx dy
    text-anchor dominant-baseline alignment-baseline font-family font-size font-weight
    font-style letter-spacing marker-start marker-mid marker-end markerWidth markerHeight
    markerUnits refX refY orient clip-path vector-effect
""".split()
)
LOCAL_IRI = re.compile(r"url\(#[A-Za-z_][\w.-]*\)\Z")
MERMAID_UNSAFE = re.compile(
    r"%%\s*\{|^\s*---\s*$|^\s*(?:click|link|href|callback)\b|<\s*/?\s*[a-zA-Z]|"
    r"\burl\s*\(|@import\b|!\[|(?:https?|file)://",
    re.M | re.I,
)


@dataclass(frozen=True)
class Preview:
    source: Path
    image: Path
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "source": str(self.source),
            "image": str(self.image),
            "width": self.width,
            "height": self.height,
        }


def validate_source(role: str, source: str) -> None:
    if not isinstance(source, str) or not source.strip() or len(source) > 32_000:
        raise ValueError(tr("Diagram source must contain 1–32,000 characters.", "图源须为 1–32000 个字符"))
    if role == "mermaid":
        if MERMAID_UNSAFE.search(source):
            raise ValueError(tr(
                "Mermaid source may contain only static shapes and text.",
                "Mermaid 图源只能使用静态图形和文字",
            ))
        return
    if role != "svg":
        raise ValueError(tr("Unknown diagram source format.", "未知图源格式"))
    if re.search(r"<!DOCTYPE|<!ENTITY|<\?", source, re.I):
        raise ValueError(tr("SVG source may contain only static shapes and text.", "SVG 图源只能使用静态图形和文字"))
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ValueError(tr(f"Invalid SVG source ({exc}).", f"SVG 图源格式无效（{exc}）")) from None
    if root.tag != f"{SVG_NS}svg" or not (root.get("viewBox") or (root.get("width") and root.get("height"))):
        raise ValueError(tr(
            "The SVG root element must declare the SVG namespace and a viewBox or dimensions.",
            "SVG 根元素须声明 SVG 命名空间及 viewBox 或尺寸",
        ))
    for element in root.iter():
        if not element.tag.startswith(SVG_NS) or element.tag[len(SVG_NS) :] not in SVG_ELEMENTS:
            raise ValueError(tr("The SVG contains an unsupported element.", "SVG 包含未支持的元素"))
        for key, value in element.attrib.items():
            unsafe_reference = (
                "url" in value.lower() or "://" in value or "\\" in value or "/*" in value
            ) and not LOCAL_IRI.fullmatch(value)
            if key not in SVG_ATTRIBUTES or unsafe_reference:
                raise ValueError(tr(
                    "The SVG contains an unsupported attribute or external reference.",
                    "SVG 包含未支持的属性或外部引用",
                ))


def _env() -> dict:
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "TMPDIR",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "PUPPETEER_EXECUTABLE_PATH",
        "LD_LIBRARY_PATH",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def render(role: str, source: str, directory: Path, label: str) -> Preview:
    validate_source(role, source)
    if not re.fullmatch(r"[0-9-]+", label):
        raise ValueError(tr("Invalid preview revision.", "预览版本无效"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    ext = "mmd" if role == "mermaid" else "svg"
    # Chromium uses Unix sockets whose paths must also fit for deeply nested classroom directories.
    with tempfile.TemporaryDirectory(prefix="htv-") as staging:
        work = Path(staging)
        source_file, image = work / f"image.{ext}", work / "image.png"
        source_file.write_text(source, encoding="utf-8")
        if role == "mermaid":
            config, browser = work / "mermaid.json", work / "puppeteer.json"
            config.write_text(
                json.dumps(
                    {
                        "securityLevel": "strict",
                        "htmlLabels": False,
                        "maxTextSize": 32000,
                        "fontFamily": "Noto Sans CJK SC, sans-serif",
                        "themeVariables": {"fontSize": "20px"},
                    }
                ),
                encoding="utf-8",
            )
            chrome = (
                os.getenv("PUPPETEER_EXECUTABLE_PATH")
                or shutil.which("google-chrome")
                or shutil.which("chromium")
            )
            options = {
                "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
                "userDataDir": str(work / "chrome-profile"),
                "pipe": True,
            }
            if chrome:
                options["executablePath"] = chrome
            browser.write_text(json.dumps(options), encoding="utf-8")
            command = [
                os.getenv("TEACHER_MMDC", "mmdc"),
                "-i",
                source_file,
                "-o",
                image,
                "-c",
                config,
                "-p",
                browser,
                "-b",
                "white",
                "-s",
                "2",
            ]
        else:
            command = [
                os.getenv("TEACHER_RSVG", "rsvg-convert"),
                "-b",
                "white",
                "-w",
                "1100",
                "-h",
                "1100",
                "--keep-aspect-ratio",
                "-o",
                image,
                source_file,
            ]
        environment = {
            **_env(),
            "TMPDIR": str(work),
            "XDG_CONFIG_HOME": str(work / "chrome-config"),
            "XDG_CACHE_HOME": str(work / "chrome-cache"),
            "XDG_DATA_HOME": str(work / "chrome-data"),
        }
        try:
            process = subprocess.run(
                command,
                cwd=work,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
        except FileNotFoundError:
            raise ValueError(tr(
                f"Diagram renderer not found: {Path(command[0]).name}",
                f"找不到图形渲染器 {Path(command[0]).name}",
            )) from None
        except subprocess.TimeoutExpired:
            raise ValueError(tr("Diagram rendering timed out.", "图形渲染超时")) from None
        if process.returncode or not image.is_file():
            diagnostics = process.stderr.strip() or process.stdout.strip() or tr(
                f"Exit status {process.returncode}",
                f"退出状态 {process.returncode}",
            )
            diagnostics = diagnostics.replace(str(work), tr("<temporary preview directory>", "<临时预览目录>"))
            raise ValueError(tr(f"Diagram rendering failed: {diagnostics[:1600]}", f"图形渲染失败：{diagnostics[:1600]}"))
        if image.stat().st_size > 8_000_000:
            raise ValueError(tr("The preview is too large. Simplify the diagram.", "预览图片过大，请简化图示"))
        png = image.read_bytes()
        if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n") or png[12:16] != b"IHDR":
            raise ValueError(tr("The renderer did not return a valid PNG.", "渲染器未返回有效的 PNG"))
        width, height = struct.unpack(">II", png[16:24])
        if not 20 <= width <= 5000 or not 20 <= height <= 5000:
            raise ValueError(tr("The preview image dimensions are outside the readable range.", "预览图片尺寸超出可读范围"))
        saved_source = directory / f"preview-{label}.{ext}"
        saved_image = directory / f"preview-{label}.png"
        write_private(saved_source, source_file.read_bytes())
        write_private(saved_image, png)
    return Preview(saved_source, saved_image, width, height)


def publish(vault: Path, role: str, task_id: str, revision: int, preview: Preview) -> dict:
    if role not in {"svg", "mermaid"} or not re.fullmatch(r"[0-9a-f]{32}", task_id) or revision < 1:
        raise ValueError(tr("Invalid task ID for publishing a diagram.", "发布图示的任务标识无效"))
    root = vault.resolve(strict=True)
    directory = root / "viz"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise ValueError(tr("The viz directory must be inside the library.", "资料库 viz 须为库内目录"))
    stem = f"teacher-{role}-{task_id}-{revision}"
    source = directory / f"{stem}{preview.source.suffix}"
    image = directory / f"{stem}.png"
    for path, content in ((source, preview.source.read_bytes()), (image, preview.image.read_bytes())):
        if path.is_symlink() or (path.exists() and path.read_bytes() != content):
            raise ValueError(tr("The diagram output path already contains other content.", "图示发布路径已有其他内容"))
        if not path.exists():
            write_private(path, content)
    reference = f"viz/{image.name}"
    return {
        "source": f"viz/{source.name}",
        "image": reference,
        "embed": f"![[{reference}]]",
        "width": preview.width,
        "height": preview.height,
    }

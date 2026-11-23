"""Create a new, self-contained Teacher profile with an explicit provider route."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import parse_qsl, urlsplit

from .i18n import get_language, normalize, tr, use_language

ROOT = Path(__file__).resolve().parents[1]


def initialize(profile, vault, *, source=None, provider="newapi", model="Qwen3.8-Flash-Next", language=None):
    language = normalize(language or get_language())
    with use_language(language):
        return _initialize(profile, vault, source, provider, model, language)


def _initialize(profile, vault, source, provider, model, language):
    profile, vault = Path(profile).expanduser().absolute(), Path(vault).expanduser().absolute()
    if profile.is_symlink() or profile.exists() and (not profile.is_dir() or any(profile.iterdir())):
        raise ValueError(tr("Choose an empty directory for the new Teacher profile.", "请为新 Teacher profile 选择空目录"))
    if profile == vault or profile in vault.parents or vault in profile.parents:
        raise ValueError(tr("Choose separate directories for the classroom vault and profile.", "课堂资料库和 profile 请使用独立目录"))
    settings = dict(
        model={"provider": provider, "default": model},
        plugins={"enabled": ["hermes-teacher"]},
        platform_toolsets={s: ["teacher"] for s in ("cli", "tui", "desktop", "api_server", "feishu")},
        skills={"project_discovery": False, "external_dirs": [], "creation_nudge_interval": 0},
        agent={"coding_context": "off", "image_input_mode": "native"},
        memory={"memory_enabled": False, "user_profile_enabled": False},
        auxiliary={
            "title_generation": {"enabled": False},
            "teacher_question": {"provider": provider, "model": model, "reasoning_effort": "low"},
        },
        delegation={"enabled": False},
        telemetry={"enabled": False},
        tools={"tool_search": {"enabled": "off"}},
        toolsets=["teacher"],
        teacher={"vault": str(vault), "language": language},
    )
    if source:
        from .models import read_config

        data = read_config(Path(source).expanduser() / "config.yaml")
        row = data.get("providers", {}).get(provider)
        if not isinstance(row, dict):
            raise ValueError(tr(f"The source profile has no providers.{provider} entry.", f"来源 profile 缺少 providers.{provider}"))
        if row.get("models") and model not in row["models"]:
            raise ValueError(tr("Use the exact model name declared in the source profile (case-sensitive).", "请使用来源 profile 中声明的模型名称（区分大小写）"))
        imported = {k: row[k] for k in ("api", "transport", "key_env") if k in row}
        if imported.get("api"):
            if not isinstance(imported["api"], str):
                raise ValueError(tr("The source provider URL must be a string.", "来源服务地址须为文字"))
            url = urlsplit(imported["api"])
            if (
                url.username is not None
                or url.password is not None
                or any(
                    re.search(r"key|token|secret|signature|credential", name, re.I)
                    for name, _ in parse_qsl(url.query)
                )
            ):
                raise ValueError(tr("The source provider URL contains credentials. Use a dedicated credential environment variable.", "来源服务地址含凭据，请使用独立的凭据环境变量"))
        if "key_env" in imported and (
            not isinstance(imported["key_env"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", imported["key_env"])
        ):
            raise ValueError(tr("The source provider's key_env must name an environment variable.", "来源服务的 key_env 须为环境变量名"))
        if "User-Agent" in (row.get("extra_headers") or {}):
            imported["extra_headers"] = {"User-Agent": row["extra_headers"]["User-Agent"]}
        entry = row.get("models", {}).get(model, {}) if isinstance(row.get("models", {}), dict) else {}
        metadata = {
            k: entry[k]
            for k in ("context_length", "supports_vision")
            if isinstance(entry, dict) and k in entry
        }
        imported["models"] = {model: metadata}
        settings["providers"] = {provider: imported}
        settings["model"]["context_length"] = metadata.get("context_length", 65536)
        if type(metadata.get("supports_vision")) is bool:
            settings["model"]["supports_vision"] = metadata["supports_vision"]
        active = data.get("model", {})
        if isinstance(active, dict) and active.get("provider") == provider and active.get("default") == model:
            if type(active.get("supports_vision")) is bool:
                settings["model"]["supports_vision"] = active["supports_vision"]
    profile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=profile.parent, prefix=".teacher-init-") as tmp:
        staged = Path(tmp) / "profile"
        plugin = staged / "plugins/hermes-teacher"
        plugin.mkdir(parents=True)
        for name in ("__init__.py", "plugin.yaml", "LICENSE"):
            shutil.copy2(ROOT / name, plugin / name)
        for name in ("teacher", "skills", "bin", "desktop", "tui"):
            shutil.copytree(ROOT / name, plugin / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        widgets = staged / "tui-widgets"
        widgets.mkdir()
        shutil.copy2(plugin / "tui/hermes-teacher-quiz.mjs", widgets)
        (staged / "teacher").mkdir(mode=0o700)
        (staged / "config.yaml").write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staged / "profile.yaml").write_text(
            "display_name: Teacher\ndescription: " + tr("Personal classroom", "中文课堂") + "\n", encoding="utf-8"
        )
        from .library import vault_directory

        vault_directory(vault, create=True)
        staged.chmod(0o700)
        staged.replace(profile)
    return profile


def activate(profile):
    profile = Path(profile).expanduser().resolve()
    if not (profile / "plugins/hermes-teacher/plugin.yaml").is_file():
        raise ValueError(tr("Select an initialized Teacher profile.", "请选择已初始化的 Teacher profile"))
    os.environ["HERMES_HOME"] = str(profile)
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
    return profile

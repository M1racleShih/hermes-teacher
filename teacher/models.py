"""Classroom and specialist model preferences, without imported credentials."""

from contextlib import closing
from copy import deepcopy
import json
from io import StringIO
import os
from pathlib import Path
import tempfile

from .i18n import tr
from .library import atomic_write, file_lease, read_bytes


ROLES = ("researcher", "mermaid", "svg")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
COMPRESSION = {
    "enabled": bool,
    "threshold": (float, int),
    "threshold_tokens": (int, type(None)),
    "target_ratio": (float, int),
    "protect_first_n": int,
    "protect_last_n": int,
}


def _parse_config(text: str, path: Path, *, editable=False):
    if path.suffix == ".json" or text.lstrip().startswith("{"):
        try:
            return _object(json.loads(text), path.name), None
        except json.JSONDecodeError as exc:
            if path.suffix == ".json":
                raise ValueError(tr(
                    f"Cannot read model config {path.name} ({type(exc).__name__}).",
                    f"无法读取模型配置 {path.name}（{type(exc).__name__}）",
                )) from None
    try:
        import hermes_yaml

        if editable:
            yaml = hermes_yaml.roundtrip_yaml()
            return _object(yaml.load(text), path.name), yaml
        return _object(hermes_yaml.safe_load(text), path.name), None
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_yaml":
            raise
        raise ValueError(tr(
            f"Use the Hermes Python interpreter to read YAML config {path.name}.",
            f"使用 Hermes 解释器读取 YAML 配置 {path.name}",
        )) from None
    except Exception as exc:
        if type(exc).__module__.startswith(("yaml", "ruamel")):
            raise ValueError(tr(
                f"Model config {path.name} contains invalid YAML.",
                f"模型配置 {path.name} 的 YAML 无效",
            )) from None
        raise


def read_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        raise ValueError(tr(
            f"Cannot read model config {path.name} ({type(exc).__name__}).",
            f"无法读取模型配置 {path.name}（{type(exc).__name__}）",
        )) from None
    return _parse_config(raw, path)[0]


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(tr(f"{field} must be an object.", f"{field} 须为对象"))
    return value


def _name(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(c.isspace() for c in value):
        raise ValueError(tr(f"{field} must be a valid name.", f"{field} 须为有效名称"))
    return value


def _effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError(tr(
            "reasoning_effort must be a reasoning effort supported by Hermes.",
            "reasoning_effort 须为 Hermes 支持的思考强度",
        ))
    return value


def _route(values: dict) -> dict:
    route = {}
    for field in ("provider", "model"):
        if field in values:
            route[field] = _name(values[field], field)
    if "reasoning_effort" in values:
        route["reasoning_effort"] = _effort(values["reasoning_effort"])
    return route


def _compression(values: dict) -> dict:
    filtered = {}
    for field, expected in COMPRESSION.items():
        if field not in values:
            continue
        value = values[field]
        types = expected if isinstance(expected, tuple) else (expected,)
        if not isinstance(value, types) or (type(value) is bool and bool not in types):
            raise ValueError(tr(f"Invalid compression.{field}.", f"compression.{field} 格式无效"))
        if field in {"threshold", "target_ratio"} and not 0 < value <= 1:
            raise ValueError(tr(
                f"compression.{field} must be greater than 0 and at most 1.",
                f"compression.{field} 须大于 0 且至多为 1",
            ))
        if field == "threshold_tokens" and value is not None and value < 1:
            raise ValueError(tr(
                "compression.threshold_tokens must be a positive integer.",
                "compression.threshold_tokens 须为正整数",
            ))
        if field in {"protect_first_n", "protect_last_n"} and value < 0:
            raise ValueError(tr(
                f"compression.{field} must be a non-negative integer.",
                f"compression.{field} 须为非负整数",
            ))
        filtered[field] = value
    return filtered


def _extract(config: dict) -> dict:
    model = config.get("model", {})
    if isinstance(model, str):
        model = {"default": model} if model else {}
    model = _object(model, "model")
    route = {"model": model["default"]} if model.get("default") else {}
    if model.get("provider"):
        route["provider"] = model["provider"]
    effort = _object(config.get("agent", {}), "agent").get("reasoning_effort")
    if effort:
        route["reasoning_effort"] = effort
    return {
        **_route(route),
        "compression": _compression(_object(config.get("compression", {}), "compression")),
    }


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _profile_models(config: dict) -> dict:
    teacher = _object(config.get("teacher", {}), "teacher")
    return _object(teacher.get("models", {}), "teacher.models")


def _allowed_models(teacher: dict) -> set[str]:
    allowed = teacher.get("allowed", [])
    if not isinstance(allowed, list):
        raise ValueError(tr(
            "teacher.models.allowed must be a list of provider/model names.",
            "teacher.models.allowed 须为 provider/model 名称列表",
        ))
    return {_name(item, "teacher.models.allowed") for item in allowed}


def model_allowed(allowed, provider, model):
    if not allowed:
        return True
    effective = f"{str(provider or '').lower().removeprefix('custom:')}/{model or ''}"
    return any(
        effective == f"{name.split('/', 1)[0].lower().removeprefix('custom:')}/{name.partition('/')[2]}"
        for name in allowed
    )


def native_model(profile: Path, session_id: str) -> dict:
    path = profile / "state.db"
    if not path.is_file():
        return {}
    from hermes_state import SessionDB

    with closing(SessionDB(db_path=path, read_only=True)) as database:
        row = database.get_session(database.get_compression_tip(session_id) or session_id)
    if not row:
        return {}
    config = json.loads(row.get("model_config") or "{}")
    return {key: value for key, value in (
        ("provider", config.get("provider")), ("model", row.get("model")),
    ) if value}


def _native_compression(config, teacher, imported):
    current = _object(config.get("compression", {}), "compression")
    local = dict(current)
    prior = _object(teacher.get("compression_sync", {}), "teacher.models.compression_sync")
    for field, entry in prior.items():
        entry = _object(entry, "teacher.models.compression_sync")
        if field not in COMPRESSION or "applied" not in entry:
            raise ValueError(tr("Invalid compression sync record.", "压缩同步记录无效"))
        _compression({field: entry["applied"]})
        if "previous" in entry:
            _compression({field: entry["previous"]})
        if field in current and current[field] == entry["applied"]:
            local.pop(field)
            if "previous" in entry:
                local[field] = entry["previous"]
    _compression(local)
    explicit = _object(teacher.get("default", {}), "teacher.models.default")
    overrides = _compression(_object(explicit.get("compression", {}), "compression"))
    desired = {**imported, **local, **overrides}
    maintained = {}
    for field in imported.keys() | overrides.keys():
        if field not in local:
            maintained[field] = {"applied": desired[field]}
        elif field in overrides and desired[field] != local[field]:
            maintained[field] = {"applied": desired[field], "previous": local[field]}
    return desired, maintained


class ModelSettings:
    def __init__(self, profile: Path):
        self.profile = Path(profile).absolute()
        self.config = self.profile / "config.yaml"
        self.snapshot = self.profile / "teacher" / "models.json"

    def sync(self, source: Path, *, dry_run=False) -> dict:
        source = Path(source)
        if dry_run or source.resolve() == self.config.resolve():
            return _extract(read_config(source))
        with file_lease(self.profile, "teacher/locks/models-import.lock"):
            candidate = _extract(read_config(source))
            before = read_bytes(self.profile, self.config.name)
            config, yaml = _parse_config(before.decode("utf-8-sig"), self.config, editable=True)
            teacher = _profile_models(config)
            desired, maintained = _native_compression(config, teacher, candidate["compression"])
            if (
                teacher.get("imported") == candidate
                and teacher.get("compression_sync", {}) == maintained
                and config.get("compression", {}) == desired
            ):
                return candidate
            # Detach YAML aliases before editing a profile setting shared with another block.
            teacher_config = deepcopy(config.get("teacher", {}))
            settings = teacher_config.setdefault("models", {})
            settings.update(imported=candidate, compression_sync=maintained)
            config["teacher"] = teacher_config
            if desired:
                compression = deepcopy(config.get("compression", {}))
                for key in compression.keys() - desired.keys():
                    del compression[key]
                compression.update(desired)
                config["compression"] = compression
            else:
                config.pop("compression", None)
            if yaml is None:
                payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            else:
                stream = StringIO()
                yaml.dump(config, stream)
                payload = stream.getvalue()
            if read_bytes(self.profile, self.config.name) != before:
                raise ValueError(tr("The Hermes config changed during import. Try again.", "Hermes 配置在导入期间改变，请重试"))
            atomic_write(self.profile, self.config.name, payload.encode())
        return candidate

    def _settings(self):
        config = read_config(self.config) if self.config.is_file() else {}
        return config, _profile_models(config)

    def allowed(self) -> set[str]:
        return _allowed_models(self._settings()[1])

    def lesson(self, model=None, provider=None, reasoning_effort=None) -> dict:
        config, teacher = self._settings()
        local = _extract(config)
        if "imported" in teacher:
            imported = _object(teacher["imported"], "teacher.models.imported")
            imported_budget = {}
        else:
            imported = read_config(self.snapshot) if self.snapshot.is_file() else {}
            imported_budget = _compression(_object(imported.get("compression", {}), "compression"))
        explicit = _object(teacher.get("default", {}), "teacher.models.default")
        allowed = _allowed_models(teacher)
        override = _route(
            {
                key: value
                for key, value in {
                    "model": model,
                    "provider": provider,
                    "reasoning_effort": reasoning_effort,
                }.items()
                if value is not None
            }
        )
        selected = {**_route(local), **_route(imported), **_route(explicit), **override}
        if (
            not model_allowed(allowed, selected.get("provider"), selected.get("model"))
            and not {"model", "provider"} & override.keys()
        ):
            selected = {**_route(local), **_route(explicit), **override}
        if not model_allowed(allowed, selected.get("provider"), selected.get("model")):
            raise ValueError(tr(
                "The selected lesson model is missing from teacher.models.allowed.",
                "所选课堂模型不在 teacher.models.allowed 中",
            ))
        selected["compression"] = {
            **local["compression"],
            **imported_budget,
            **_compression(_object(explicit.get("compression", {}), "compression")),
        }
        return selected

    def role(self, role: str) -> dict:
        if role not in ROLES:
            raise ValueError(tr(f"Unknown lesson role: {role}", f"未知课堂角色：{role}"))
        config, teacher = self._settings()
        roles = _object(teacher.get("roles", {}), "teacher.models.roles")
        return {**_route(_extract(config)), **_route(_object(roles.get(role, {}), role))}

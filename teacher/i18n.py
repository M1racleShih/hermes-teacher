"""Select the language of Teacher's own interface for the active profile."""

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction
import os
from pathlib import Path


_language = ContextVar("teacher_language", default=None)


def normalize(language):
    """Accept English and Chinese locale tags, including en-US and zh_CN."""
    code = str(language).strip().lower().replace("_", "-").split("-", 1)[0]
    if code not in {"en", "zh"}:
        raise ValueError(f"Unsupported Teacher interface language {language!r}. Choose en or zh.")
    return code


def get_language():
    return _language.get() or normalize(os.getenv("HERMES_TEACHER_LANGUAGE") or "en")


def profile_language(profile):
    override = os.getenv("HERMES_TEACHER_LANGUAGE")
    if override:
        return normalize(override)
    path = Path(profile).expanduser() / "config.yaml"
    if path.is_file():
        from .models import read_config

        teacher = read_config(path).get("teacher")
        if isinstance(teacher, dict):
            if teacher.get("language"):
                return normalize(teacher["language"])
            if teacher.get("vault"):
                return "zh"  # Profiles created before language selection used Chinese.
    return "en"


def tr(english, chinese, *, language=None):
    return chinese if (normalize(language) if language else get_language()) == "zh" else english


@contextmanager
def use_language(language):
    token = _language.set(normalize(language))
    try:
        yield
    finally:
        _language.reset(token)


def use_profile(profile):
    return use_language(profile_language(profile))


def profile_argument(argv, *, default=None, short=False):
    """Find the profile to localize help before building the command parser."""
    probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    probe.add_argument(*(["-p"] if short else []), "--profile", type=Path, default=default)
    try:
        return probe.parse_known_args(argv)[0].profile
    except argparse.ArgumentError:
        return default


def localized(method):
    """Bind a callback's profile for its duration, including asynchronous work."""
    if iscoroutinefunction(method):
        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            with use_profile(self.profile):
                return await method(self, *args, **kwargs)
        return asynchronous

    @wraps(method)
    def synchronous(self, *args, **kwargs):
        with use_profile(self.profile):
            return method(self, *args, **kwargs)
    return synchronous

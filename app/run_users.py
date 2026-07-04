from __future__ import annotations

import pwd
import re

from . import config


USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")


class RunUserError(ValueError):
    pass


def allowed_run_users() -> list[str]:
    raw = config.ALLOWED_RUN_USERS.strip()
    if not raw:
        return [config.CRON_USER]
    users: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        user = item.strip()
        if user and user not in seen:
            seen.add(user)
            users.append(user)
    return users or [config.CRON_USER]


def effective_run_user(value: str | None) -> str:
    return (value or "").strip() or config.CRON_USER


def describe_run_user(value: str | None) -> str:
    user = (value or "").strip()
    if not user:
        return f"default ({config.CRON_USER})"
    return user


def validate_run_user(value: str | None) -> None:
    user = (value or "").strip()
    if not user:
        return
    if not USERNAME_RE.fullmatch(user):
        raise RunUserError("Run user is invalid")
    if user not in allowed_run_users():
        raise RunUserError(f"Run user '{user}' is not allowed")
    try:
        pwd.getpwnam(user)
    except KeyError as exc:
        raise RunUserError(f"Run user '{user}' does not exist on this system") from exc

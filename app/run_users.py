from __future__ import annotations

import getpass
import os
import pwd
import re
import shutil
import subprocess

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


def manual_runner_command(target_flag: str, target_id: str, run_user_value: str | None, source: str = "manual") -> list[str]:
    if target_flag not in {"--job-id", "--group-id"}:
        raise RunUserError("Manual runner target is invalid")
    validate_run_user(run_user_value)
    target_user = effective_run_user(run_user_value)
    runner_args = [config.RUNNER_PATH, target_flag, target_id, "--source", source]
    current_user = getpass.getuser()
    if current_user == target_user:
        return runner_args
    if os.geteuid() != 0:
        raise RunUserError(
            f"Manual run cannot switch from {current_user} to {target_user}; run the web service as root or configure user switching"
        )
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, "-u", target_user, *runner_args]
    runuser = shutil.which("runuser")
    if runuser:
        return [runuser, "-u", target_user, "--", *runner_args]
    raise RunUserError("Manual run cannot switch users because neither sudo nor runuser is available")


def launch_command(command: list[str]) -> None:
    subprocess.Popen(command, cwd=str(config.SCHEDULER_HOME), start_new_session=True)


def launch_manual_runner(target_flag: str, target_id: str, run_user_value: str | None) -> None:
    launch_command(manual_runner_command(target_flag, target_id, run_user_value))

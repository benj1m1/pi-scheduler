from __future__ import annotations

import grp
import logging
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from . import config, run_users


LOGGER = logging.getLogger(__name__)


def setup_hint() -> str:
    return f"Run: sudo {config.SCHEDULER_HOME}/deploy/setup-runtime-user.sh"


def expected_models_path(user: str | None = None) -> Path:
    runtime_user = user or config.RUNTIME_USER
    try:
        home = Path(pwd.getpwnam(runtime_user).pw_dir)
    except KeyError:
        home = Path("/home") / runtime_user
    return home / ".pi" / "agent" / "models.json"


def runtime_directories() -> list[Path]:
    return [config.DATA_DIR, config.LOG_DIR, config.LOCK_DIR, config.SCHEDULER_HOME / "tmp"]


def _user_group_ids(user_info) -> set[int]:
    group_ids = {user_info.pw_gid}
    for group in grp.getgrall():
        if user_info.pw_name in group.gr_mem:
            group_ids.add(group.gr_gid)
    return group_ids


def _mode_allows_write(user_info, path: Path) -> bool:
    mode = path.stat().st_mode
    if path.stat().st_uid == user_info.pw_uid:
        return bool(mode & stat.S_IWUSR)
    if path.stat().st_gid in _user_group_ids(user_info):
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _can_write_as_runtime_user(user_info, path: Path) -> bool:
    runuser = shutil.which("runuser")
    if os.geteuid() == 0 and runuser:
        result = subprocess.run(
            [runuser, "-u", user_info.pw_name, "--", "test", "-w", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    return _mode_allows_write(user_info, path)


def check_runtime_setup() -> list[str]:
    warnings: list[str] = []
    try:
        user_info = pwd.getpwnam(config.RUNTIME_USER)
    except KeyError:
        warnings.append(f"Runtime user '{config.RUNTIME_USER}' does not exist. {setup_hint()}")
        return warnings

    allowed = run_users.allowed_run_users()
    if config.RUNTIME_USER not in allowed:
        warnings.append(
            f"Runtime user '{config.RUNTIME_USER}' is not in PI_SCHEDULER_ALLOWED_RUN_USERS ({', '.join(allowed)})."
        )

    for directory in runtime_directories():
        if not directory.exists():
            warnings.append(f"Runtime directory is missing: {directory}. {setup_hint()}")
        elif not directory.is_dir():
            warnings.append(f"Runtime path is not a directory: {directory}. {setup_hint()}")
        elif not _can_write_as_runtime_user(user_info, directory):
            warnings.append(f"Runtime user '{config.RUNTIME_USER}' cannot write to {directory}. {setup_hint()}")

    models_path = expected_models_path(config.RUNTIME_USER)
    if not models_path.exists():
        warnings.append(f"Runtime models file is missing at {models_path}. {setup_hint()}")
    else:
        owner = pwd.getpwuid(models_path.stat().st_uid).pw_name
        if owner != config.RUNTIME_USER:
            warnings.append(f"Runtime models file {models_path} is owned by {owner}, expected {config.RUNTIME_USER}.")

    return warnings


def log_runtime_setup_warnings() -> list[str]:
    warnings = check_runtime_setup()
    for warning in warnings:
        LOGGER.warning("Runtime setup warning: %s", warning)
    return warnings

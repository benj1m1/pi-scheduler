from __future__ import annotations

import logging
import pwd
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


def check_runtime_setup() -> list[str]:
    warnings: list[str] = []
    try:
        pwd.getpwnam(config.RUNTIME_USER)
    except KeyError:
        warnings.append(f"Runtime user '{config.RUNTIME_USER}' does not exist. {setup_hint()}")
        return warnings

    allowed = run_users.allowed_run_users()
    if config.RUNTIME_USER not in allowed:
        warnings.append(
            f"Runtime user '{config.RUNTIME_USER}' is not in PI_SCHEDULER_ALLOWED_RUN_USERS ({', '.join(allowed)})."
        )

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

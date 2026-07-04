from __future__ import annotations

import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from . import config


SYSTEM_CRON_DIR = Path("/etc/cron.d")


def _is_system_cron_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(SYSTEM_CRON_DIR.resolve())
        return True
    except ValueError:
        return False


def _cron_service_active() -> bool | None:
    if not shutil.which("systemctl"):
        return None
    result = subprocess.run(
        ["systemctl", "is-active", "cron"],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )
    if result.returncode == 0:
        return True
    if result.stdout.strip() in {"inactive", "failed", "deactivating", "activating"}:
        return False
    return None


def _file_owner(path: Path) -> str | None:
    try:
        stat_result = path.stat()
        return pwd.getpwuid(stat_result.st_uid).pw_name
    except (FileNotFoundError, KeyError):
        return None


def _file_mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except FileNotFoundError:
        return None


def inspect(generated_content: str | None = None) -> dict:
    target = config.CRON_FILE
    warnings: list[str] = []
    recommendations: list[str] = []
    is_system_path = _is_system_cron_path(target)
    exists = target.exists()
    content_matches: bool | None = None
    service_active = _cron_service_active()

    if exists and generated_content is not None:
        try:
            content_matches = target.read_text(encoding="utf-8") == generated_content
        except OSError as exc:
            warnings.append(f"Could not read target file {target}: {exc}")
            content_matches = None

    if not is_system_path:
        status = "preview_only"
        warnings.append("Target file is outside /etc/cron.d. System cron will not read this file automatically.")
        recommendations.append("For active system cron, set PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs before starting the app.")
        recommendations.append("Alternatively use the systemd deployment.")
    elif not exists:
        status = "missing"
        warnings.append(f"Target cron file does not exist: {target}")
        recommendations.append("Restart the app or save/toggle a job to regenerate the cron file.")
    elif content_matches is False:
        status = "out_of_sync"
        warnings.append("Target cron file does not match the generated preview.")
        recommendations.append("Restart the app or save/toggle a job to rewrite the cron file.")
    else:
        status = "active_candidate"

    if service_active is False:
        warnings.append("The cron service does not appear to be active.")
        recommendations.append("Start cron with: sudo systemctl enable --now cron")
    elif service_active is None:
        warnings.append("Could not confirm cron service status with systemctl.")

    return {
        "target_file": str(target),
        "is_system_cron_path": is_system_path,
        "file_exists": exists,
        "content_matches": content_matches,
        "file_mode": _file_mode(target),
        "file_owner": _file_owner(target),
        "cron_service_active": service_active,
        "status": status,
        "warnings": warnings,
        "recommendations": recommendations,
    }

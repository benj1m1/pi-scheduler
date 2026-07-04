from __future__ import annotations

import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from . import config


SYSTEM_CRON_DIR = Path("/etc/cron.d")
CRON_SERVICE_NAMES = ("cron", "crond")


def _is_system_cron_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(SYSTEM_CRON_DIR.resolve())
        return True
    except ValueError:
        return False


def _cron_service_active() -> bool | None:
    if not shutil.which("systemctl"):
        return None

    saw_inactive = False
    for service_name in CRON_SERVICE_NAMES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        state = result.stdout.strip()
        if result.returncode == 0:
            return True
        if state in {"inactive", "failed", "deactivating", "activating"}:
            saw_inactive = True

    if saw_inactive:
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


def _check(label: str, state: str, detail: str) -> dict[str, str]:
    return {"label": label, "state": state, "detail": detail}


def _content_check_state(exists: bool, content_matches: bool | None) -> tuple[str, str]:
    if not exists:
        return "unknown", "No target file to compare"
    if content_matches is True:
        return "pass", "On-disk file matches generated preview"
    if content_matches is False:
        return "fail", "On-disk file differs from generated preview"
    return "unknown", "Could not compare on-disk file to generated preview"


def _service_check_state(service_active: bool | None) -> tuple[str, str]:
    if service_active is True:
        return "pass", "cron/crond service appears active"
    if service_active is False:
        return "fail", "cron/crond service does not appear active"
    return "unknown", "Could not confirm cron/crond service with systemctl"


def _automatic_summary(status: str, service_active: bool | None) -> tuple[str, str, str]:
    if status == "preview_only":
        return (
            "not_active",
            "Automatic jobs are not active",
            "Cron file is only a preview-only file. System cron does not read this path.",
        )
    if status == "missing":
        return (
            "not_active",
            "Automatic jobs are not active",
            "The configured system cron file does not exist yet, so automatic jobs cannot run from it.",
        )
    if status == "out_of_sync":
        return (
            "not_active",
            "Automatic jobs need attention",
            "A system cron file exists, but it does not match the generated schedule shown below.",
        )
    if status == "active_candidate" and service_active is True:
        return (
            "active",
            "Automatic jobs are active",
            "The cron file is under /etc/cron.d, exists, matches the generated schedule, and cron service is running.",
        )
    if status == "active_candidate":
        return (
            "likely_active",
            "Automatic jobs are likely active",
            "The cron file is under /etc/cron.d and matches the generated schedule, but cron service status could not be confirmed.",
        )
    return (
        "unknown",
        "Automatic job status is unknown",
        "Pi Scheduler could not fully inspect the configured cron target.",
    )


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
        warnings.append("Could not confirm cron/crond service status with systemctl.")

    content_state, content_detail = _content_check_state(exists, content_matches)
    service_state, service_detail = _service_check_state(service_active)
    automatic_status, headline, summary = _automatic_summary(status, service_active)
    checks = [
        _check(
            "System cron path",
            "pass" if is_system_path else "fail",
            "Target is under /etc/cron.d" if is_system_path else "Target is outside /etc/cron.d",
        ),
        _check(
            "Target file exists",
            "pass" if exists else "fail",
            "File exists on disk" if exists else "File is missing",
        ),
        _check("Content matches generated preview", content_state, content_detail),
        _check("Cron service", service_state, service_detail),
    ]

    return {
        "target_file": str(target),
        "is_system_cron_path": is_system_path,
        "file_exists": exists,
        "content_matches": content_matches,
        "file_mode": _file_mode(target),
        "file_owner": _file_owner(target),
        "cron_service_active": service_active,
        "status": status,
        "automatic_status": automatic_status,
        "headline": headline,
        "summary": summary,
        "checks": checks,
        "warnings": warnings,
        "recommendations": recommendations,
    }

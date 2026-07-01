from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import config, db


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def validate_cron_expr(value: str) -> None:
    parts = value.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have exactly 5 fields")
    if not croniter.is_valid(value):
        raise ValueError("Cron expression is invalid")


def next_run(cron_expr: str) -> str | None:
    try:
        validate_cron_expr(cron_expr)
        return croniter(cron_expr, datetime.now(BEIJING_TZ)).get_next(datetime).isoformat(timespec="seconds")
    except ValueError:
        return None


def interval_to_cron(every: str, unit: str) -> str:
    try:
        value = int(every)
    except ValueError as exc:
        raise ValueError("Schedule interval must be a number") from exc

    if unit == "minutes":
        if value < 1 or value > 59:
            raise ValueError("Minute interval must be between 1 and 59")
        return f"*/{value} * * * *" if value > 1 else "* * * * *"
    if unit == "hours":
        if value < 1 or value > 23:
            raise ValueError("Hour interval must be between 1 and 23")
        return f"0 */{value} * * *" if value > 1 else "0 * * * *"
    raise ValueError("Schedule unit must be minutes or hours")


def cron_to_interval(cron_expr: str) -> dict[str, str]:
    parts = cron_expr.split()
    if parts == ["*", "*", "*", "*", "*"]:
        return {"every": "1", "unit": "minutes"}
    if len(parts) == 5 and parts[0].startswith("*/") and parts[1:] == ["*", "*", "*", "*"]:
        return {"every": parts[0][2:], "unit": "minutes"}
    if parts == ["0", "*", "*", "*", "*"]:
        return {"every": "1", "unit": "hours"}
    if len(parts) == 5 and parts[0] == "0" and parts[1].startswith("*/") and parts[2:] == ["*", "*", "*"]:
        return {"every": parts[1][2:], "unit": "hours"}
    return {"every": "5", "unit": "minutes"}


def describe_cron(cron_expr: str) -> str:
    parts = cron_expr.split()
    if parts == ["*", "*", "*", "*", "*"]:
        return "Every minute"
    if len(parts) == 5 and parts[0].startswith("*/") and parts[1:] == ["*", "*", "*", "*"]:
        value = parts[0][2:]
        return f"Every {value} minutes"
    if parts == ["0", "*", "*", "*", "*"]:
        return "Every hour"
    if len(parts) == 5 and parts[0] == "0" and parts[1].startswith("*/") and parts[2:] == ["*", "*", "*"]:
        value = parts[1][2:]
        return f"Every {value} hours"
    return cron_expr


def render_cron_file(jobs: list[dict] | None = None) -> str:
    jobs = jobs if jobs is not None else db.list_jobs_for_cron()
    lines = [
        "# Managed by pi-scheduler. Do not edit manually.",
        "SHELL=/bin/bash",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        f"PI_SCHEDULER_HOME={config.SCHEDULER_HOME}",
        "",
    ]

    for job in jobs:
        if job.get("deleted_at") or not int(job.get("enabled", 0)):
            continue
        validate_cron_expr(job["cron_expr"])
        lines.append(
            f"{job['cron_expr']} {config.CRON_USER} {config.RUNNER_PATH} --job-id {job['id']}"
        )

    lines.append("")
    return "\n".join(lines)


def write_cron_file(path: Path | None = None) -> None:
    target = path or config.CRON_FILE
    content = render_cron_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent), text=True)
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db


@dataclass
class CleanupResult:
    runs_deleted: int = 0
    files_deleted: int = 0
    files_missing: int = 0
    files_skipped: int = 0

    def add_file(self, status: str) -> None:
        if status == "deleted":
            self.files_deleted += 1
        elif status == "missing":
            self.files_missing += 1
        else:
            self.files_skipped += 1

    def merge(self, other: "CleanupResult") -> None:
        self.runs_deleted += other.runs_deleted
        self.files_deleted += other.files_deleted
        self.files_missing += other.files_missing
        self.files_skipped += other.files_skipped


def cutoff_for_days(days: int) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=days)


def utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_unlink(path_value: str | None) -> str:
    if not path_value:
        return "skipped"
    path = Path(path_value)
    try:
        resolved_path = path.resolve()
        log_root = config.LOG_DIR.resolve()
        if not resolved_path.is_relative_to(log_root):
            return "skipped"
        if resolved_path.is_file():
            resolved_path.unlink()
            return "deleted"
        return "missing"
    except OSError:
        return "skipped"


def cleanup_daily_summaries(cutoff: datetime) -> CleanupResult:
    result = CleanupResult()
    cutoff_date = cutoff.date()
    jobs_dir = config.LOG_DIR / "jobs"
    if not jobs_dir.exists():
        return result
    for path in jobs_dir.glob("*/????-??-??.jsonl"):
        try:
            summary_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if summary_date < cutoff_date:
            result.add_file(safe_unlink(str(path)))
    return result


def cleanup_all_daily_summaries() -> CleanupResult:
    result = CleanupResult()
    jobs_dir = config.LOG_DIR / "jobs"
    if not jobs_dir.exists():
        return result
    for path in jobs_dir.glob("*/????-??-??.jsonl"):
        result.add_file(safe_unlink(str(path)))
    return result


def cleanup_run_files(runs: list[dict]) -> CleanupResult:
    result = CleanupResult(runs_deleted=len(runs))
    for run in runs:
        result.add_file(safe_unlink(run.get("stdout_path")))
        result.add_file(safe_unlink(run.get("stderr_path")))
        result.add_file(safe_unlink(run.get("jsonl_path")))
    return result


def cleanup_runs_before(cutoff: datetime) -> CleanupResult:
    old_runs = db.list_runs_before(utc_string(cutoff))
    result = cleanup_run_files(old_runs)
    result.merge(cleanup_daily_summaries(cutoff))
    db.delete_runs([run["id"] for run in old_runs])
    return result


def cleanup_all_runs() -> CleanupResult:
    runs = db.list_deletable_runs()
    result = cleanup_run_files(runs)
    result.merge(cleanup_all_daily_summaries())
    db.delete_runs([run["id"] for run in runs])
    return result


def cleanup_old_logs(days: int | None = None) -> int:
    days = config.LOG_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0

    return cleanup_runs_before(cutoff_for_days(days)).runs_deleted

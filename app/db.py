from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    config.ensure_runtime_dirs()
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists jobs (
              id text primary key,
              name text not null,
              skill_name text not null,
              task_prompt text not null,
              cron_expr text not null,
              enabled integer not null default 1,
              timeout_seconds integer not null default 240,
              prevent_overlap integer not null default 1,
              output_mode text not null default 'summary',
              session_mode text not null default 'no_session',
              tool_mode text not null default 'full',
              created_at text not null,
              updated_at text not null,
              deleted_at text
            );

            create table if not exists runs (
              id text primary key,
              job_id text not null,
              source text not null default 'auto',
              started_at text not null,
              finished_at text,
              status text not null,
              exit_code integer,
              duration_ms integer,
              command text not null,
              stdout_path text,
              stderr_path text,
              jsonl_path text,
              error_summary text,
              foreign key (job_id) references jobs(id)
            );

            create index if not exists idx_runs_job_started_at on runs(job_id, started_at desc);
            create index if not exists idx_runs_job_source_started_at on runs(job_id, source, started_at desc);
            create index if not exists idx_runs_started_at on runs(started_at desc);
            create index if not exists idx_jobs_deleted_created_at on jobs(deleted_at, created_at desc);
            create index if not exists idx_runs_job_active_started_at on runs(job_id, started_at desc) where status != 'disabled';
            create index if not exists idx_runs_running_job on runs(job_id) where status = 'running';
            create index if not exists idx_runs_job_source_active_started_at on runs(job_id, source, started_at desc) where status != 'disabled';
            """
        )
        columns = {row[1] for row in conn.execute("pragma table_info(jobs)").fetchall()}
        if "provider_name" not in columns:
            conn.execute("alter table jobs add column provider_name text")
        if "model_id" not in columns:
            conn.execute("alter table jobs add column model_id text")
        if "work_start" not in columns:
            conn.execute("alter table jobs add column work_start text")
        if "work_end" not in columns:
            conn.execute("alter table jobs add column work_end text")
        if "output_mode" not in columns:
            conn.execute("alter table jobs add column output_mode text not null default 'events'")
        if "session_mode" not in columns:
            conn.execute("alter table jobs add column session_mode text not null default 'save'")
        if "tool_mode" not in columns:
            conn.execute("alter table jobs add column tool_mode text not null default 'full'")
        run_columns = {row[1] for row in conn.execute("pragma table_info(runs)").fetchall()}
        if "source" not in run_columns:
            conn.execute("alter table runs add column source text not null default 'auto'")
        conn.execute("update jobs set prevent_overlap = 1 where prevent_overlap != 1")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "job"


def unique_job_id(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while conn.execute("select 1 from jobs where id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_jobs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select
              j.*,
              r.status as last_status,
              r.started_at as last_started_at,
              r.finished_at as last_finished_at,
              exists(
                select 1 from runs running
                where running.job_id = j.id and running.status = 'running'
              ) as has_running_run
            from jobs j
            left join runs r on r.id = (
              select id from runs
              where job_id = j.id and status != 'disabled'
              order by started_at desc limit 1
            )
            where j.deleted_at is null
            order by j.created_at desc
            """
        ).fetchall()
        return [dict(row) for row in rows]


def list_jobs_for_cron() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select id, cron_expr, enabled, deleted_at
            from jobs
            where deleted_at is null
            order by created_at desc
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select * from jobs where id = ? and deleted_at is null", (job_id,)
        ).fetchone()
        return row_to_dict(row)


def get_job_runs_status(
    job_id: str,
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
) -> dict[str, Any] | None:
    with connect() as conn:
        job = row_to_dict(
            conn.execute(
                "select * from jobs where id = ? and deleted_at is null", (job_id,)
            ).fetchone()
        )
        if job is None:
            return None

        running = conn.execute(
            "select 1 from runs where job_id = ? and status = 'running' limit 1",
            (job_id,),
        ).fetchone()
        return {
            "job": job,
            "runs": list_recent_runs_for_connection(conn, job_id, limit, offset, source),
            "has_running_run": running is not None,
        }


def create_job(data: dict[str, Any]) -> str:
    now = utc_now()
    with connect() as conn:
        job_id = unique_job_id(conn, data["name"])
        conn.execute(
            """
            insert into jobs (
              id, name, skill_name, task_prompt, cron_expr, provider_name, model_id, enabled,
              work_start, work_end, timeout_seconds, prevent_overlap, output_mode, session_mode,
              tool_mode, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                data["name"],
                data["skill_name"],
                data["task_prompt"],
                data["cron_expr"],
                data.get("provider_name"),
                data.get("model_id"),
                int(data.get("enabled", 1)),
                data.get("work_start"),
                data.get("work_end"),
                int(data.get("timeout_seconds", 240)),
                1,
                data.get("output_mode", "summary"),
                data.get("session_mode", "no_session"),
                data.get("tool_mode", "full"),
                now,
                now,
            ),
        )
        return job_id


def update_job(job_id: str, data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            update jobs
            set name = ?, skill_name = ?, task_prompt = ?, cron_expr = ?, enabled = ?,
                provider_name = ?, model_id = ?, work_start = ?, work_end = ?,
                timeout_seconds = ?, prevent_overlap = ?, output_mode = ?, session_mode = ?,
                tool_mode = ?, updated_at = ?
            where id = ? and deleted_at is null
            """,
            (
                data["name"],
                data["skill_name"],
                data["task_prompt"],
                data["cron_expr"],
                int(data.get("enabled", 0)),
                data.get("provider_name"),
                data.get("model_id"),
                data.get("work_start"),
                data.get("work_end"),
                int(data.get("timeout_seconds", 240)),
                1,
                data.get("output_mode", "summary"),
                data.get("session_mode", "no_session"),
                data.get("tool_mode", "full"),
                utc_now(),
                job_id,
            ),
        )


def set_job_enabled(job_id: str, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "update jobs set enabled = ?, updated_at = ? where id = ? and deleted_at is null",
            (int(enabled), utc_now(), job_id),
        )


def soft_delete_job(job_id: str) -> None:
    with connect() as conn:
        now = utc_now()
        conn.execute(
            "update jobs set deleted_at = ?, updated_at = ?, enabled = 0 where id = ? and deleted_at is null",
            (now, now, job_id),
        )


def list_recent_runs(
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
) -> list[dict[str, Any]]:
    with connect() as conn:
        return list_recent_runs_for_connection(conn, job_id, limit, offset, source)


def list_recent_runs_for_connection(
    conn: sqlite3.Connection,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
) -> list[dict[str, Any]]:
    filters = ["status != 'disabled'"]
    params: list[Any] = []
    if job_id:
        filters.append("job_id = ?")
        params.append(job_id)
    if source:
        filters.append("source = ?")
        params.append(source)
    params.extend([limit, offset])
    rows = conn.execute(
        f"""
        select id, job_id, source, started_at, finished_at, status, exit_code, duration_ms
        from runs
        where {' and '.join(filters)}
        order by started_at desc
        limit ? offset ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def has_running_run(job_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "select 1 from runs where job_id = ? and status = 'running' limit 1",
            (job_id,),
        ).fetchone()
        return row is not None


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        return row_to_dict(row)


def list_runs_before(cutoff: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("select * from runs where started_at < ?", (cutoff,)).fetchall()
        return [dict(row) for row in rows]


def list_deletable_runs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("select * from runs where status != 'running'").fetchall()
        return [dict(row) for row in rows]


def delete_runs(run_ids: list[str]) -> None:
    if not run_ids:
        return
    placeholders = ", ".join("?" for _ in run_ids)
    with connect() as conn:
        conn.execute(f"delete from runs where id in ({placeholders})", run_ids)


def insert_run(run: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            insert into runs (
              id, job_id, source, started_at, finished_at, status, exit_code, duration_ms,
              command, stdout_path, stderr_path, jsonl_path, error_summary
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["job_id"],
                run.get("source", "auto"),
                run["started_at"],
                run.get("finished_at"),
                run["status"],
                run.get("exit_code"),
                run.get("duration_ms"),
                run.get("command", ""),
                run.get("stdout_path"),
                run.get("stderr_path"),
                run.get("jsonl_path"),
                run.get("error_summary"),
            ),
        )


def update_run(run_id: str, data: dict[str, Any]) -> None:
    fields = [f"{key} = ?" for key in data]
    values = list(data.values()) + [run_id]
    with connect() as conn:
        conn.execute(f"update runs set {', '.join(fields)} where id = ?", values)

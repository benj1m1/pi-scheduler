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
              group_run_id text,
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

            create table if not exists job_groups (
              id text primary key,
              name text not null,
              cron_expr text not null,
              enabled integer not null default 1,
              prevent_overlap integer not null default 1,
              continue_on_failure integer not null default 0,
              work_start text,
              work_end text,
              created_at text not null,
              updated_at text not null,
              deleted_at text
            );

            create table if not exists job_group_members (
              group_id text not null,
              job_id text not null,
              position integer not null,
              created_at text not null,
              primary key (group_id, position),
              unique (group_id, job_id),
              foreign key (group_id) references job_groups(id) on delete cascade,
              foreign key (job_id) references jobs(id)
            );

            create table if not exists group_runs (
              id text primary key,
              group_id text not null,
              source text not null default 'auto',
              started_at text not null,
              finished_at text,
              status text not null,
              duration_ms integer,
              error_summary text,
              foreign key (group_id) references job_groups(id)
            );

            create table if not exists group_run_steps (
              id text primary key,
              group_run_id text not null,
              group_id text not null,
              job_id text not null,
              run_id text,
              position integer not null,
              status text not null,
              started_at text,
              finished_at text,
              error_summary text,
              foreign key (group_run_id) references group_runs(id) on delete cascade,
              foreign key (group_id) references job_groups(id),
              foreign key (job_id) references jobs(id),
              foreign key (run_id) references runs(id) on delete set null
            );

            create index if not exists idx_runs_job_started_at on runs(job_id, started_at desc);
            create index if not exists idx_runs_started_at on runs(started_at desc);
            create index if not exists idx_jobs_deleted_created_at on jobs(deleted_at, created_at desc);
            create index if not exists idx_runs_job_active_started_at on runs(job_id, started_at desc) where status != 'disabled';
            create index if not exists idx_runs_running_job on runs(job_id) where status = 'running';
            create index if not exists idx_job_groups_deleted_created_at on job_groups(deleted_at, created_at desc);
            create index if not exists idx_job_group_members_job on job_group_members(job_id);
            create index if not exists idx_group_runs_group_started_at on group_runs(group_id, started_at desc);
            create index if not exists idx_group_runs_running_group on group_runs(group_id) where status = 'running';
            create index if not exists idx_group_run_steps_run on group_run_steps(run_id);
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
        conn.execute("create index if not exists idx_runs_job_source_started_at on runs(job_id, source, started_at desc)")
        conn.execute("create index if not exists idx_runs_job_source_active_started_at on runs(job_id, source, started_at desc) where status != 'disabled'")
        if "group_run_id" not in run_columns:
            conn.execute("alter table runs add column group_run_id text")
        conn.execute("create index if not exists idx_runs_group_run_id on runs(group_run_id)")
        group_columns = {row[1] for row in conn.execute("pragma table_info(job_groups)").fetchall()}
        if "continue_on_failure" not in group_columns:
            conn.execute("alter table job_groups add column continue_on_failure integer not null default 0")
        conn.execute("update jobs set prevent_overlap = 1 where prevent_overlap != 1")
        conn.execute("update job_groups set prevent_overlap = 1 where prevent_overlap != 1")


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


def unique_group_id(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while conn.execute("select 1 from job_groups where id = ?", (candidate,)).fetchone():
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


def list_groups_for_cron() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select id, cron_expr, enabled, deleted_at
            from job_groups
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
        group = conn.execute(
            """
            select g.name
            from job_group_members m
            join job_groups g on g.id = m.group_id
            where m.job_id = ? and g.deleted_at is null
            limit 1
            """,
            (job_id,),
        ).fetchone()
        if group is not None:
            raise ValueError(f"Job is used by group {group['name']}")
        now = utc_now()
        conn.execute(
            "update jobs set deleted_at = ?, updated_at = ?, enabled = 0 where id = ? and deleted_at is null",
            (now, now, job_id),
        )


def _members_for_connection(conn: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select m.group_id, m.job_id, m.position, j.name as job_name, j.deleted_at as job_deleted_at
        from job_group_members m
        join jobs j on j.id = m.job_id
        where m.group_id = ?
        order by m.position
        """,
        (group_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_groups() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select
              g.*,
              gr.id as last_group_run_id,
              gr.status as last_status,
              gr.started_at as last_started_at,
              gr.finished_at as last_finished_at,
              gr.duration_ms as last_duration_ms,
              exists(
                select 1 from group_runs running
                where running.group_id = g.id and running.status = 'running'
              ) as has_running_run
            from job_groups g
            left join group_runs gr on gr.id = (
              select id from group_runs
              where group_id = g.id
              order by started_at desc limit 1
            )
            where g.deleted_at is null
            order by g.created_at desc
            """
        ).fetchall()
        groups = [dict(row) for row in rows]
        for group in groups:
            group["members"] = _members_for_connection(conn, group["id"])
        return groups


def get_group(group_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select * from job_groups where id = ? and deleted_at is null", (group_id,)
        ).fetchone()
        return row_to_dict(row)


def get_group_with_members(group_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        group = row_to_dict(
            conn.execute(
                "select * from job_groups where id = ? and deleted_at is null", (group_id,)
            ).fetchone()
        )
        if group is None:
            return None
        group["members"] = _members_for_connection(conn, group_id)
        return group


def validate_group_members(conn: sqlite3.Connection, member_job_ids: list[str]) -> None:
    if not member_job_ids:
        raise ValueError("Choose at least one job")
    if len(set(member_job_ids)) != len(member_job_ids):
        raise ValueError("A job can only appear once in a group")
    placeholders = ", ".join("?" for _ in member_job_ids)
    rows = conn.execute(
        f"select id from jobs where id in ({placeholders}) and deleted_at is null", member_job_ids
    ).fetchall()
    if {row["id"] for row in rows} != set(member_job_ids):
        raise ValueError("Group members must be active jobs")


def create_group(data: dict[str, Any], member_job_ids: list[str]) -> str:
    now = utc_now()
    with connect() as conn:
        validate_group_members(conn, member_job_ids)
        group_id = unique_group_id(conn, data["name"])
        conn.execute(
            """
            insert into job_groups (
              id, name, cron_expr, enabled, prevent_overlap, continue_on_failure,
              work_start, work_end, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                data["name"],
                data["cron_expr"],
                int(data.get("enabled", 1)),
                1,
                int(data.get("continue_on_failure", 0)),
                data.get("work_start"),
                data.get("work_end"),
                now,
                now,
            ),
        )
        for index, job_id in enumerate(member_job_ids, start=1):
            conn.execute(
                """
                insert into job_group_members (group_id, job_id, position, created_at)
                values (?, ?, ?, ?)
                """,
                (group_id, job_id, index, now),
            )
        return group_id


def update_group(group_id: str, data: dict[str, Any], member_job_ids: list[str]) -> None:
    now = utc_now()
    with connect() as conn:
        validate_group_members(conn, member_job_ids)
        conn.execute(
            """
            update job_groups
            set name = ?, cron_expr = ?, enabled = ?, prevent_overlap = ?, continue_on_failure = ?,
                work_start = ?, work_end = ?, updated_at = ?
            where id = ? and deleted_at is null
            """,
            (
                data["name"],
                data["cron_expr"],
                int(data.get("enabled", 0)),
                1,
                int(data.get("continue_on_failure", 0)),
                data.get("work_start"),
                data.get("work_end"),
                now,
                group_id,
            ),
        )
        conn.execute("delete from job_group_members where group_id = ?", (group_id,))
        for index, job_id in enumerate(member_job_ids, start=1):
            conn.execute(
                """
                insert into job_group_members (group_id, job_id, position, created_at)
                values (?, ?, ?, ?)
                """,
                (group_id, job_id, index, now),
            )


def set_group_enabled(group_id: str, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "update job_groups set enabled = ?, updated_at = ? where id = ? and deleted_at is null",
            (int(enabled), utc_now(), group_id),
        )


def soft_delete_group(group_id: str) -> None:
    with connect() as conn:
        now = utc_now()
        conn.execute(
            "update job_groups set deleted_at = ?, updated_at = ?, enabled = 0 where id = ? and deleted_at is null",
            (now, now, group_id),
        )


def has_running_group_run(group_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "select 1 from group_runs where group_id = ? and status = 'running' limit 1",
            (group_id,),
        ).fetchone()
        return row is not None


def list_recent_runs(
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
) -> list[dict[str, Any]]:
    with connect() as conn:
        return list_recent_runs_for_connection(conn, job_id, limit, offset, source)


def list_runs(
    limit: int = 50,
    offset: int = 0,
    job_id: str | None = None,
    source: str | None = None,
    status: str | None = None,
    group_id: str | None = None,
    started_at_from: str | None = None,
    started_at_before: str | None = None,
) -> list[dict[str, Any]]:
    filters = ["r.status != 'disabled'"]
    params: list[Any] = []
    if job_id:
        filters.append("r.job_id = ?")
        params.append(job_id)
    if source:
        filters.append("r.source = ?")
        params.append(source)
    if status:
        filters.append("r.status = ?")
        params.append(status)
    if group_id:
        filters.append("gr.group_id = ?")
        params.append(group_id)
    if started_at_from:
        filters.append("r.started_at >= ?")
        params.append(started_at_from)
    if started_at_before:
        filters.append("r.started_at < ?")
        params.append(started_at_before)
    params.extend([limit, offset])
    with connect() as conn:
        rows = conn.execute(
            f"""
            select
              r.id, r.job_id, r.source, r.started_at, r.finished_at, r.status,
              r.exit_code, r.duration_ms, r.error_summary, r.stdout_path, r.stderr_path,
              r.jsonl_path, r.group_run_id, j.name as job_name, j.deleted_at as job_deleted_at,
              gr.group_id, g.name as group_name, g.deleted_at as group_deleted_at
            from runs r
            left join jobs j on j.id = r.job_id
            left join group_runs gr on gr.id = r.group_run_id
            left join job_groups g on g.id = gr.group_id
            where {' and '.join(filters)}
            order by r.started_at desc
            limit ? offset ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


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
        row = conn.execute(
            """
            select r.*, gr.group_id, g.name as group_name, g.deleted_at as group_deleted_at
            from runs r
            left join group_runs gr on gr.id = r.group_run_id
            left join job_groups g on g.id = gr.group_id
            where r.id = ?
            """,
            (run_id,),
        ).fetchone()
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
              id, job_id, group_run_id, source, started_at, finished_at, status, exit_code, duration_ms,
              command, stdout_path, stderr_path, jsonl_path, error_summary
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["job_id"],
                run.get("group_run_id"),
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


def insert_group_run(run: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            insert into group_runs (
              id, group_id, source, started_at, finished_at, status, duration_ms, error_summary
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["group_id"],
                run.get("source", "auto"),
                run["started_at"],
                run.get("finished_at"),
                run["status"],
                run.get("duration_ms"),
                run.get("error_summary"),
            ),
        )


def update_group_run(group_run_id: str, data: dict[str, Any]) -> None:
    fields = [f"{key} = ?" for key in data]
    values = list(data.values()) + [group_run_id]
    with connect() as conn:
        conn.execute(f"update group_runs set {', '.join(fields)} where id = ?", values)


def insert_group_run_step(step: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            insert into group_run_steps (
              id, group_run_id, group_id, job_id, run_id, position, status,
              started_at, finished_at, error_summary
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step["id"],
                step["group_run_id"],
                step["group_id"],
                step["job_id"],
                step.get("run_id"),
                step["position"],
                step["status"],
                step.get("started_at"),
                step.get("finished_at"),
                step.get("error_summary"),
            ),
        )


def update_group_run_step(step_id: str, data: dict[str, Any]) -> None:
    fields = [f"{key} = ?" for key in data]
    values = list(data.values()) + [step_id]
    with connect() as conn:
        conn.execute(f"update group_run_steps set {', '.join(fields)} where id = ?", values)


def list_group_runs(group_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            select *
            from group_runs
            where group_id = ?
            order by started_at desc
            limit ?
            """,
            (group_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_group_run_with_steps(group_run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        group_run = row_to_dict(
            conn.execute(
                """
                select gr.*, g.name as group_name, g.deleted_at as group_deleted_at
                from group_runs gr
                left join job_groups g on g.id = gr.group_id
                where gr.id = ?
                """,
                (group_run_id,),
            ).fetchone()
        )
        if group_run is None:
            return None
        rows = conn.execute(
            """
            select s.*, j.name as job_name, j.deleted_at as job_deleted_at
            from group_run_steps s
            left join jobs j on j.id = s.job_id
            where s.group_run_id = ?
            order by s.position
            """,
            (group_run_id,),
        ).fetchall()
        group_run["steps"] = [dict(row) for row in rows]
        return group_run

from __future__ import annotations

import argparse
import fcntl
import json
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import approved_skills, config, cron, db, governance, pi_models, retention, work_window


class RunnerConfigError(ValueError):
    pass


@dataclass
class JobExecutionResult:
    run_id: str | None
    status: str
    exit_code: int
    error_summary: str | None = None


def build_prompt(job: dict) -> str:
    return job["task_prompt"]


def output_mode(job: dict) -> str:
    return "summary" if job.get("output_mode") == "summary" else "events"


def session_mode(job: dict) -> str:
    return "no_session" if job.get("session_mode") == "no_session" else "save"


def tool_mode(job: dict) -> str:
    value = job.get("tool_mode")
    return value if value in {"read_only", "no_tools"} else "full"


def skills_mode(job: dict) -> str:
    value = job.get("skills_mode")
    return value if value in {"approved", "runtime"} else "none"


def skill_ids(job: dict) -> list[str]:
    return approved_skills.parse_skill_ids(job.get("skill_ids"))


def build_command(job: dict, validate_model: bool = True) -> tuple[list[str], str]:
    prompt = build_prompt(job)
    argv = [config.PI_BINARY]
    selected_skills_mode = skills_mode(job)
    if selected_skills_mode in {"none", "approved"}:
        argv.append("--no-skills")
    if selected_skills_mode == "approved":
        for skill_id in skill_ids(job):
            try:
                path = approved_skills.resolve_skill_path(skill_id)
            except approved_skills.SkillCatalogError as exc:
                raise RunnerConfigError(f"Approved skill {skill_id!r} is not available") from exc
            argv.extend(["--skill", str(path)])
    if session_mode(job) == "no_session":
        argv.append("--no-session")
    selected_tool_mode = tool_mode(job)
    if selected_tool_mode == "read_only":
        argv.extend(["--tools", "read,grep,find,ls"])
    elif selected_tool_mode == "no_tools":
        argv.append("--no-tools")
    if job.get("name"):
        argv.extend(["--name", f"pi-scheduler: {job['name']}"])
    if output_mode(job) == "summary":
        argv.append("-p")
    else:
        argv.extend(["--mode", "json"])
    provider = job.get("provider_name")
    model_id = job.get("model_id")
    if provider or model_id:
        if validate_model:
            pi_models.validate_selection(provider, model_id)
        argv.extend(["--provider", str(provider), "--model", str(model_id)])
    argv.append(prompt)
    display = shlex.join(argv)
    return argv, display


def new_run_id(job_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}-{job_id}"


def truncate(value: str, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} characters"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, indent=2)

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(json.dumps(item, ensure_ascii=False, indent=2))
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(str(item.get("text", "")))
        elif item_type == "thinking":
            parts.append(f"[thinking]\n{item.get('thinking', '')}")
        elif item_type == "toolCall":
            parts.append(
                "[tool call] "
                + str(item.get("name", "unknown"))
                + "\n"
                + json.dumps(item.get("arguments", {}), ensure_ascii=False, indent=2)
            )
        elif item_type == "image":
            parts.append(f"[image {item.get('mimeType', 'unknown')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False, indent=2))
    return "\n\n".join(part for part in parts if part)


def message_to_text(message: dict) -> str:
    role = message.get("role", "message")
    if role == "bashExecution":
        output = message.get("output", "")
        exit_code = message.get("exitCode")
        return f"[bash exit={exit_code}]\n$ {message.get('command', '')}\n{output}"
    if role == "custom":
        return f"[custom:{message.get('customType', '')}]\n{content_to_text(message.get('content', ''))}"
    if role == "branchSummary":
        return f"[branch summary]\n{message.get('summary', '')}"
    if role == "compactionSummary":
        return f"[compaction summary]\n{message.get('summary', '')}"
    if role == "toolResult":
        label = f"[tool result:{message.get('toolName', '')} error={message.get('isError', False)}]"
        return f"{label}\n{content_to_text(message.get('content', ''))}"
    return f"[{role}]\n{content_to_text(message.get('content', ''))}"


def pi_events_to_transcript(events_jsonl: str) -> str:
    lines: list[str] = []
    seen_messages: set[str] = set()
    for raw_line in events_jsonl.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            lines.append(f"[raw]\n{raw_line}")
            continue

        event_type = event.get("type")
        if event_type == "session":
            lines.append(f"[session] id={event.get('id')} cwd={event.get('cwd')}")
        elif event_type == "agent_start":
            lines.append("[agent start]")
        elif event_type == "agent_end":
            lines.append("[agent end]")
        elif event_type == "turn_start":
            lines.append("[turn start]")
        elif event_type == "turn_end":
            lines.append("[turn end]")
        elif event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict):
                fingerprint = json.dumps(message, ensure_ascii=False, sort_keys=True)
                if fingerprint not in seen_messages:
                    seen_messages.add(fingerprint)
                    lines.append(message_to_text(message))
        elif event_type == "tool_execution_start":
            lines.append(
                f"[tool start:{event.get('toolName', '')}]\n"
                + json.dumps(event.get("args", {}), ensure_ascii=False, indent=2)
            )
        elif event_type == "tool_execution_update":
            lines.append(
                f"[tool update:{event.get('toolName', '')}]\n"
                + json.dumps(event.get("partialResult"), ensure_ascii=False, indent=2)
            )
        elif event_type == "tool_execution_end":
            lines.append(
                f"[tool end:{event.get('toolName', '')} error={event.get('isError', False)}]\n"
                + json.dumps(event.get("result"), ensure_ascii=False, indent=2)
            )
        elif event_type in {"queue_update", "compaction_start", "compaction_end", "auto_retry_start", "auto_retry_end"}:
            lines.append(f"[{event_type}]\n" + json.dumps(event, ensure_ascii=False, indent=2))

    return "\n\n".join(lines) if lines else events_jsonl


def create_terminal_run(
    job_id: str,
    status: str,
    command: str,
    error_summary: str | None = None,
    source: str = "auto",
    group_run_id: str | None = None,
) -> str:
    now = db.utc_now()
    run_id = new_run_id(job_id)
    job_log_dir = config.LOG_DIR / "jobs" / job_id
    runs_dir = job_log_dir / "runs"
    stdout_path = runs_dir / f"{run_id}.stdout.log"
    stderr_path = runs_dir / f"{run_id}.stderr.log"
    write_text(stdout_path, "")
    write_text(stderr_path, error_summary or "")
    db.insert_run(
        {
            "id": run_id,
            "job_id": job_id,
            "group_run_id": group_run_id,
            "source": source,
            "started_at": now,
            "finished_at": now,
            "status": status,
            "duration_ms": 0,
            "command": command,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "error_summary": error_summary,
        }
    )
    return run_id


def execute_job(
    job: dict,
    source: str = "auto",
    apply_job_schedule_guards: bool = True,
    group_run_id: str | None = None,
) -> JobExecutionResult:
    job_id = job["id"]
    manual = source == "manual"
    if apply_job_schedule_guards and not manual and not int(job.get("enabled", 0)):
        cron.write_cron_file()
        return JobExecutionResult(None, "disabled", 0)
    if apply_job_schedule_guards and not manual and not work_window.is_within_window(job.get("work_start"), job.get("work_end")):
        return JobExecutionResult(None, "outside_work_window", 0)

    try:
        _, command_display = build_command(job)
    except pi_models.ModelConfigError as exc:
        _, command_display = build_command(job, validate_model=False)
        run_id = create_terminal_run(job_id, "failed", command_display, str(exc), source, group_run_id)
        return JobExecutionResult(run_id, "failed", 1, str(exc))
    except RunnerConfigError as exc:
        command_display = shlex.join([config.PI_BINARY, "<configuration-error>"])
        run_id = create_terminal_run(job_id, "failed", command_display, str(exc), source, group_run_id)
        return JobExecutionResult(run_id, "failed", 1, str(exc))

    lock_handle = None
    lock_path = config.LOCK_DIR / f"{job_id}.lock"
    if int(job.get("prevent_overlap", 1)):
        config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_handle = lock_path.open("w")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            run_id = create_terminal_run(
                job_id,
                "skipped_overlap",
                command_display,
                "Previous run still active",
                source,
                group_run_id,
            )
            return JobExecutionResult(run_id, "skipped_overlap", 0, "Previous run still active")

    run_id = new_run_id(job_id)
    started_at = db.utc_now()
    started_monotonic = time.monotonic()
    db.insert_run(
        {
            "id": run_id,
            "job_id": job_id,
            "group_run_id": group_run_id,
            "source": source,
            "started_at": started_at,
            "status": "running",
            "command": command_display,
        }
    )

    stdout = ""
    stderr = ""
    pi_events = ""
    exit_code: int | None = None
    status = "failed"
    error_summary = None

    try:
        argv, _ = build_command(job)
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=int(job["timeout_seconds"]),
            check=False,
        )
        if output_mode(job) == "events":
            pi_events = result.stdout or ""
            stdout = pi_events_to_transcript(pi_events)
        else:
            stdout = result.stdout or ""
            pi_events = ""
        stderr = result.stderr or ""
        exit_code = result.returncode
        status = "success" if result.returncode == 0 else "failed"
        if result.returncode != 0:
            error_summary = truncate(stderr or stdout or f"Command exited {result.returncode}", 500)
    except subprocess.TimeoutExpired as exc:
        pi_events = exc.stdout or ""
        stdout = pi_events
        stderr = exc.stderr or ""
        if isinstance(pi_events, bytes):
            pi_events = pi_events.decode(errors="replace")
            stdout = pi_events
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        if output_mode(job) == "events":
            stdout = pi_events_to_transcript(stdout)
        else:
            pi_events = ""
        status = "timeout"
        error_summary = f"Timed out after {job['timeout_seconds']} seconds"
    except FileNotFoundError as exc:
        status = "failed"
        error_summary = str(exc)
        stderr = str(exc)
    finally:
        finished_at = db.utc_now()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        job_log_dir = config.LOG_DIR / "jobs" / job_id
        runs_dir = job_log_dir / "runs"
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stdout_path = runs_dir / f"{run_id}.stdout.log"
        stderr_path = runs_dir / f"{run_id}.stderr.log"
        pi_jsonl_path = runs_dir / f"{run_id}.pi-events.jsonl" if output_mode(job) == "events" else None
        summary_jsonl_path = job_log_dir / f"{day}.jsonl"
        write_text(stdout_path, stdout)
        write_text(stderr_path, stderr)
        if pi_jsonl_path is not None:
            write_text(pi_jsonl_path, pi_events)
        record = {
            "run_id": run_id,
            "job_id": job_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "status": status,
            "source": source,
            "stdout": truncate(stdout),
            "stderr": truncate(stderr),
        }
        append_jsonl(summary_jsonl_path, record)
        db.update_run(
            run_id,
            {
                "finished_at": finished_at,
                "status": status,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "jsonl_path": str(pi_jsonl_path) if pi_jsonl_path is not None else None,
                "error_summary": error_summary,
            },
        )
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        retention.cleanup_old_logs()

    exit_status = 0 if status in {"success", "skipped_overlap", "disabled"} else 1
    return JobExecutionResult(run_id, status, exit_status, error_summary)


def run_job(job_id: str, source: str = "auto") -> int:
    db.init_db()
    retention.cleanup_old_logs()
    job = db.get_job(job_id)
    if job is None:
        return 1
    if governance.is_paused():
        create_terminal_run(job_id, "disabled", "", "Scheduler is globally paused", source)
        return 0
    if governance.is_target_expired(job):
        create_terminal_run(job_id, "disabled", "", "Job is expired", source)
        return 0

    result = execute_job(job, source=source, apply_job_schedule_guards=True)
    return result.exit_code


def new_group_run_id(group_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}-{group_id}"


def create_terminal_group_run(
    group_id: str,
    status: str,
    error_summary: str | None = None,
    source: str = "auto",
) -> str:
    now = db.utc_now()
    group_run_id = new_group_run_id(group_id)
    db.insert_group_run(
        {
            "id": group_run_id,
            "group_id": group_id,
            "source": source,
            "started_at": now,
            "finished_at": now,
            "status": status,
            "duration_ms": 0,
            "error_summary": error_summary,
        }
    )
    return group_run_id


def run_group(group_id: str, source: str = "auto") -> int:
    db.init_db()
    retention.cleanup_old_logs()
    group = db.get_group_with_members(group_id)
    if group is None:
        return 1
    if governance.is_paused():
        create_terminal_group_run(group_id, "disabled", "Scheduler is globally paused", source)
        return 0
    if governance.is_target_expired(group):
        create_terminal_group_run(group_id, "disabled", "Group is expired", source)
        return 0

    manual = source == "manual"
    if not manual and not int(group.get("enabled", 0)):
        cron.write_cron_file()
        return 0
    if not manual and not work_window.is_within_window(group.get("work_start"), group.get("work_end")):
        return 0

    lock_handle = None
    lock_path = config.LOCK_DIR / "groups" / f"{group_id}.lock"
    if int(group.get("prevent_overlap", 1)):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = lock_path.open("w")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            create_terminal_group_run(group_id, "skipped_overlap", "Previous group run still active", source)
            return 0

    group_run_id = new_group_run_id(group_id)
    started_at = db.utc_now()
    started_monotonic = time.monotonic()
    db.insert_group_run(
        {
            "id": group_run_id,
            "group_id": group_id,
            "source": source,
            "started_at": started_at,
            "status": "running",
        }
    )

    status = "success"
    error_summary = None
    exit_code = 0
    continue_on_failure = bool(int(group.get("continue_on_failure", 0)))

    try:
        for member in group["members"]:
            step_id = f"{group_run_id}-{member['position']}"
            step_started = db.utc_now()
            db.insert_group_run_step(
                {
                    "id": step_id,
                    "group_run_id": group_run_id,
                    "group_id": group_id,
                    "job_id": member["job_id"],
                    "position": member["position"],
                    "status": "running",
                    "started_at": step_started,
                }
            )
            job = db.get_job(member["job_id"])
            if job is None:
                step_error = "Group member job is no longer active"
                db.update_group_run_step(
                    step_id,
                    {"status": "failed", "finished_at": db.utc_now(), "error_summary": step_error},
                )
                status = "failed"
                if error_summary is None:
                    error_summary = f"Step {member['position']} failed: {step_error}"
                exit_code = 1
                if not continue_on_failure:
                    break
                continue

            result = execute_job(
                job,
                source=source,
                apply_job_schedule_guards=False,
                group_run_id=group_run_id,
            )
            db.update_group_run_step(
                step_id,
                {
                    "run_id": result.run_id,
                    "status": result.status,
                    "finished_at": db.utc_now(),
                    "error_summary": result.error_summary,
                },
            )
            if result.status != "success":
                if result.status == "timeout":
                    status = "timeout"
                elif status == "success":
                    status = "failed"
                step_summary = (
                    f"Step {member['position']} {job['name']} ended with {result.status}"
                )
                if result.error_summary:
                    step_summary = f"{step_summary}: {result.error_summary}"
                if error_summary is None:
                    error_summary = step_summary
                exit_code = 1
                if not continue_on_failure:
                    break

        if status != "success" and not continue_on_failure:
            completed_positions = {
                step["position"]
                for step in db.get_group_run_with_steps(group_run_id)["steps"]
            }
            for member in group["members"]:
                if member["position"] in completed_positions:
                    continue
                db.insert_group_run_step(
                    {
                        "id": f"{group_run_id}-{member['position']}",
                        "group_run_id": group_run_id,
                        "group_id": group_id,
                        "job_id": member["job_id"],
                        "position": member["position"],
                        "status": "skipped",
                        "error_summary": "Previous step failed",
                    }
                )
    finally:
        finished_at = db.utc_now()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        db.update_group_run(
            group_run_id,
            {
                "finished_at": finished_at,
                "status": status,
                "duration_ms": duration_ms,
                "error_summary": error_summary,
            },
        )
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        retention.cleanup_old_logs()

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a pi-scheduler job or group by id")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job-id")
    target.add_argument("--group-id")
    parser.add_argument("--source", choices=["auto", "manual"], default="auto")
    args = parser.parse_args()
    if args.group_id:
        return run_group(args.group_id, source=args.source)
    return run_job(args.job_id, source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())

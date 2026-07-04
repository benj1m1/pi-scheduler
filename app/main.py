from __future__ import annotations

import secrets
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import approved_skills, config, cron, cron_status, db, governance, pi_models, retention, runner, run_users, runtime_setup, work_window


app = FastAPI(title="Pi Scheduler")
security = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
RUNS_PER_PAGE = 10
LOGS_PER_PAGE = 50
LOG_PREVIEW_BYTES = 200 * 1024
RUN_SOURCE_FILTERS = {
    "all": "All",
    "auto": "Automatic",
    "manual": "Manual",
}
LOG_STATUS_FILTERS = {
    "all": "All statuses",
    "running": "Running",
    "success": "Success",
    "failed": "Failed",
    "timeout": "Timed out",
}
OUTPUT_MODES = {"summary", "events"}
SESSION_MODES = {"save", "no_session"}
TOOL_MODES = {"full", "read_only", "no_tools"}
SKILLS_MODES = {"none", "approved", "runtime"}


def governance_options() -> dict[str, list[str]]:
    return {
        "environment_options": sorted(governance.ENVIRONMENTS),
        "risk_level_options": ["low", "medium", "high"],
    }


def hour_options() -> list[dict[str, str]]:
    options = []
    for hour in range(24):
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        options.append({"value": f"{hour:02d}:00", "label": f"{display_hour}:00 {suffix}"})
    return options


def beijing_time(value: str | None) -> str:
    if not value:
        return ""
    parsed = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S Beijing")


def seconds_duration(value: int | None) -> str:
    return str(math.ceil((value or 0) / 1000))


def describe_skills_mode(value: str | None) -> str:
    if value == "approved":
        return "Approved skills"
    if value == "runtime":
        return "Runtime user default skills"
    return "No skills"


def describe_job_skills(job: dict) -> str:
    mode = job.get("skills_mode") or "none"
    if mode == "runtime":
        return "Runtime user default skills"
    if mode != "approved":
        return "No skills"
    ids = approved_skills.parse_skill_ids(job.get("skill_ids"))
    if not ids:
        return "Approved skills: none selected"
    return "Approved: " + ", ".join(ids)


def run_status_class(status_value: str) -> str:
    if status_value == "success":
        return "ok"
    if status_value in {"failed", "timeout"}:
        return "bad"
    return "muted"


def format_run_summary(run: dict) -> dict:
    return {
        "id": run["id"],
        "started_at": beijing_time(run.get("started_at")),
        "source": "manual" if run.get("source") == "manual" else "auto",
        "status": run.get("status", ""),
        "status_class": run_status_class(run.get("status", "")),
        "duration": seconds_duration(run.get("duration_ms")),
        "exit_code": run.get("exit_code"),
        "url": f"/runs/{run['id']}",
    }


def logs_path(filters: dict[str, str], page: int) -> str:
    params = {key: value for key, value in filters.items() if value and value != "all"}
    if page > 1:
        params["page"] = str(page)
    query = urlencode(params)
    return f"/logs?{query}" if query else "/logs"


def group_run_path(group_run_id: str, group_id: str | None = None) -> str:
    if group_id:
        return f"/groups/{group_id}/runs/{group_run_id}"
    return f"/group-runs/{group_run_id}"


def parse_beijing_day(value: str, next_day: bool = False) -> str | None:
    if not value:
        return None
    day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
    if next_day:
        day += timedelta(days=1)
    return day.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def logs_context(
    request: Request,
    page: int = 1,
    job_id: str = "",
    group_id: str = "",
    source: str = "all",
    run_status: str = "all",
    start_date: str = "",
    end_date: str = "",
    errors: list[str] | None = None,
    result: retention.CleanupResult | None = None,
    mode: str | None = None,
    days: int = 30,
) -> dict:
    filters = {
        "job_id": job_id,
        "group_id": group_id,
        "source": source,
        "status": run_status,
        "start_date": start_date,
        "end_date": end_date,
    }
    context_errors = list(errors or [])
    started_at_from = None
    started_at_before = None
    try:
        started_at_from = parse_beijing_day(start_date)
    except ValueError:
        context_errors.append("Start date must use YYYY-MM-DD")
    try:
        started_at_before = parse_beijing_day(end_date, next_day=True)
    except ValueError:
        context_errors.append("End date must use YYYY-MM-DD")
    if start_date and end_date and not context_errors and start_date > end_date:
        context_errors.append("Start date must be on or before end date")

    runs_page = db.list_runs(
        limit=LOGS_PER_PAGE + 1,
        offset=(page - 1) * LOGS_PER_PAGE,
        job_id=job_id or None,
        group_id=group_id or None,
        source=None if source == "all" else source,
        status=None if run_status == "all" else run_status,
        started_at_from=started_at_from,
        started_at_before=started_at_before,
    )
    runs = runs_page[:LOGS_PER_PAGE]
    return {
        "request": request,
        "runs": runs,
        "jobs": db.list_jobs(),
        "groups": db.list_groups(),
        "page": page,
        "has_next_page": len(runs_page) > LOGS_PER_PAGE,
        "filters": filters,
        "previous_url": logs_path(filters, page - 1) if page > 1 else "",
        "next_url": logs_path(filters, page + 1) if len(runs_page) > LOGS_PER_PAGE else "",
        "run_source_filters": RUN_SOURCE_FILTERS,
        "log_status_filters": LOG_STATUS_FILTERS,
        "errors": context_errors,
        "result": result,
        "mode": mode,
        "days": days,
    }


templates.env.filters["beijing_time"] = beijing_time
templates.env.filters["seconds_duration"] = seconds_duration
templates.env.filters["describe_cron"] = cron.describe_cron
templates.env.filters["describe_work_window"] = work_window.describe
templates.env.filters["describe_run_user"] = run_users.describe_run_user
templates.env.filters["describe_skills_mode"] = describe_skills_mode
templates.env.filters["describe_job_skills"] = describe_job_skills
templates.env.filters["group_run_path"] = group_run_path


def require_auth(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    username_ok = secrets.compare_digest(credentials.username, config.ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, config.ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    retention.cleanup_old_logs()
    runtime_setup.log_runtime_setup_warnings()
    cron.write_cron_file()


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def ensure_execution_allowed(target: dict, target_label: str) -> None:
    if governance.is_paused():
        raise HTTPException(status_code=400, detail="Scheduler is globally paused")
    if governance.is_target_expired(target):
        raise HTTPException(status_code=400, detail=f"{target_label} is expired")


def parse_bool(value: str | None) -> int:
    return 1 if value in {"on", "1", "true", "yes"} else 0


def validate_job_form(data: dict) -> list[str]:
    errors: list[str] = []
    for field, label in [
        ("name", "Name"),
        ("task_prompt", "Prompt"),
    ]:
        if not data[field].strip():
            errors.append(f"{label} is required")
    if data.get("schedule_error"):
        errors.append(data["schedule_error"])
    elif data.get("cron_expr"):
        try:
            cron.validate_cron_expr(data["cron_expr"])
        except ValueError as exc:
            errors.append(str(exc))
    if data.get("model_error"):
        errors.append(data["model_error"])
    else:
        try:
            pi_models.validate_selection(data.get("provider_name"), data.get("model_id"))
        except pi_models.ModelConfigError as exc:
            errors.append(str(exc))
    try:
        work_window.validate(data.get("work_start"), data.get("work_end"))
    except ValueError as exc:
        errors.append(str(exc))
    errors.extend(governance.validate_metadata(data))
    try:
        run_users.validate_run_user(data.get("run_user"))
    except run_users.RunUserError as exc:
        errors.append(str(exc))
    if data.get("output_mode") not in OUTPUT_MODES:
        errors.append("Output mode is invalid")
    if data.get("session_mode") not in SESSION_MODES:
        errors.append("Session mode is invalid")
    if data.get("tool_mode") not in TOOL_MODES:
        errors.append("Tool access is invalid")
    if data.get("skills_mode") not in SKILLS_MODES:
        errors.append("Skills policy is invalid")
    if data.get("skills_mode") == "approved":
        ids = approved_skills.parse_skill_ids(data.get("skill_ids"))
        if not ids:
            errors.append("At least one approved skill is required")
        for skill_id in ids:
            try:
                approved_skills.resolve_skill_path(skill_id)
            except approved_skills.SkillCatalogError:
                errors.append(f"Approved skill {skill_id!r} is not available")
    try:
        timeout = int(data["timeout_seconds"])
        if timeout < 10 or timeout > 3600:
            errors.append("Timeout must be between 10 and 3600 seconds")
    except ValueError:
        errors.append("Timeout must be a number")
    return errors


def form_data(
    name: str,
    task_prompt: str,
    schedule_every: str,
    schedule_unit: str,
    model_selection: str,
    output_mode: str,
    session_mode: str,
    tool_mode: str,
    work_start: str,
    work_end: str,
    timeout_seconds: str,
    run_user: str,
    enabled: str | None,
    prevent_overlap: str | None,
    skills_mode: str = "none",
    skill_ids: str | list[str] | None = None,
    *,
    owner: str = "",
    purpose: str = "",
    scope: str = "",
    environment: str = "local",
    risk_level: str = "low",
    expires_at: str = "",
) -> dict:
    cron_expr = ""
    schedule_error = None
    try:
        cron_expr = cron.interval_to_cron(schedule_every.strip(), schedule_unit)
    except ValueError as exc:
        schedule_error = str(exc)
    provider_name = None
    model_id = None
    model_error = None
    try:
        provider_name, model_id = pi_models.decode_selection(model_selection)
    except pi_models.ModelConfigError as exc:
        model_error = str(exc)
    selected_skill_ids = approved_skills.parse_skill_ids(skill_ids)
    return {
        "name": name.strip(),
        "skill_name": "general",
        "task_prompt": task_prompt.strip(),
        "cron_expr": cron_expr,
        "schedule_every": schedule_every.strip(),
        "schedule_unit": schedule_unit,
        "schedule_error": schedule_error,
        "provider_name": provider_name,
        "model_id": model_id,
        "model_selection": model_selection,
        "model_error": model_error,
        "output_mode": output_mode,
        "session_mode": session_mode,
        "tool_mode": tool_mode,
        "skills_mode": skills_mode,
        "skill_ids": "\n".join(selected_skill_ids),
        "skill_paths": "",
        "work_start": work_start.strip() or None,
        "work_end": work_end.strip() or None,
        "timeout_seconds": timeout_seconds.strip(),
        "run_user": run_user.strip() or None,
        "owner": owner.strip(),
        "purpose": purpose.strip(),
        "scope": scope.strip(),
        "environment": environment,
        "risk_level": risk_level,
        "expires_at": expires_at.strip() or None,
        "enabled": parse_bool(enabled),
        "prevent_overlap": 1,
    }


def with_schedule(job: dict) -> dict:
    job = dict(job)
    schedule = cron.cron_to_interval(job.get("cron_expr", "*/5 * * * *"))
    job["schedule_every"] = schedule["every"]
    job["schedule_unit"] = schedule["unit"]
    if job.get("provider_name") and job.get("model_id"):
        job["model_selection"] = pi_models.encode_selection(job["provider_name"], job["model_id"])
    else:
        job["model_selection"] = ""
    job["work_start"] = job.get("work_start") or ""
    job["work_end"] = job.get("work_end") or ""
    job["output_mode"] = job.get("output_mode") or "summary"
    job["session_mode"] = job.get("session_mode") or "no_session"
    job["tool_mode"] = job.get("tool_mode") or "full"
    job["skills_mode"] = job.get("skills_mode") or "none"
    job["skill_ids"] = job.get("skill_ids") or ""
    job["skill_paths"] = job.get("skill_paths") or ""
    job["run_user"] = job.get("run_user") or ""
    job["owner"] = job.get("owner") or ""
    job["purpose"] = job.get("purpose") or ""
    job["scope"] = job.get("scope") or ""
    job["environment"] = job.get("environment") or "local"
    job["risk_level"] = job.get("risk_level") or "low"
    job["expires_at"] = job.get("expires_at") or ""
    job["is_expired"] = governance.is_target_expired(job)
    return job


def selectable_run_users() -> list[str]:
    default_user = config.CRON_USER
    return [user for user in run_users.allowed_run_users() if user != default_user]


def job_form_context(request: Request, job: dict, errors: list[str], action: str, title: str) -> dict:
    model_config_error = None
    try:
        model_options = pi_models.list_configured_models()
    except pi_models.ModelConfigError as exc:
        model_options = []
        model_config_error = str(exc)
    catalog = approved_skills.list_skills()
    selected_skill_ids = set(approved_skills.parse_skill_ids(job.get("skill_ids")))
    catalog_ids = {entry.id for entry in catalog}
    missing_skill_ids = sorted(selected_skill_ids - catalog_ids)
    return {
        "request": request,
        "job": job,
        "errors": errors,
        "action": action,
        "title": title,
        "model_options": model_options,
        "model_config_error": model_config_error,
        "models_file": str(config.PI_MODELS_FILE),
        "hour_options": hour_options(),
        "allowed_run_users": selectable_run_users(),
        "default_run_user": config.CRON_USER,
        "approved_skills": catalog,
        "selected_skill_ids": selected_skill_ids,
        "missing_skill_ids": missing_skill_ids,
        **governance_options(),
    }


def group_form_data(
    name: str,
    schedule_every: str,
    schedule_unit: str,
    work_start: str,
    work_end: str,
    run_user: str,
    enabled: str | None,
    continue_on_failure: str | None,
    member_job_ids: list[str] | None,
    *,
    owner: str = "",
    purpose: str = "",
    scope: str = "",
    environment: str = "local",
    risk_level: str = "low",
    expires_at: str = "",
) -> dict:
    cron_expr = ""
    schedule_error = None
    try:
        cron_expr = cron.interval_to_cron(schedule_every.strip(), schedule_unit)
    except ValueError as exc:
        schedule_error = str(exc)
    members = [job_id for job_id in (member_job_ids or []) if job_id]
    return {
        "name": name.strip(),
        "cron_expr": cron_expr,
        "schedule_every": schedule_every.strip(),
        "schedule_unit": schedule_unit,
        "schedule_error": schedule_error,
        "work_start": work_start.strip() or None,
        "work_end": work_end.strip() or None,
        "run_user": run_user.strip() or None,
        "owner": owner.strip(),
        "purpose": purpose.strip(),
        "scope": scope.strip(),
        "environment": environment,
        "risk_level": risk_level,
        "expires_at": expires_at.strip() or None,
        "enabled": parse_bool(enabled),
        "prevent_overlap": 1,
        "continue_on_failure": parse_bool(continue_on_failure),
        "member_job_ids": members,
    }


def validate_group_form(data: dict) -> list[str]:
    errors: list[str] = []
    if not data["name"].strip():
        errors.append("Name is required")
    if data.get("schedule_error"):
        errors.append(data["schedule_error"])
    elif data.get("cron_expr"):
        try:
            cron.validate_cron_expr(data["cron_expr"])
        except ValueError as exc:
            errors.append(str(exc))
    try:
        work_window.validate(data.get("work_start"), data.get("work_end"))
    except ValueError as exc:
        errors.append(str(exc))
    errors.extend(governance.validate_metadata(data))
    try:
        run_users.validate_run_user(data.get("run_user"))
    except run_users.RunUserError as exc:
        errors.append(str(exc))
    members = data.get("member_job_ids", [])
    if not members:
        errors.append("Choose at least one job")
    if len(set(members)) != len(members):
        errors.append("A job can only appear once in a group")
    active_job_ids = {job["id"] for job in db.list_jobs()}
    if any(job_id not in active_job_ids for job_id in members):
        errors.append("Group members must be active jobs")
    return errors


def with_group_schedule(group: dict) -> dict:
    group = dict(group)
    schedule = cron.cron_to_interval(group.get("cron_expr", "*/5 * * * *"))
    group["schedule_every"] = schedule["every"]
    group["schedule_unit"] = schedule["unit"]
    group["work_start"] = group.get("work_start") or ""
    group["work_end"] = group.get("work_end") or ""
    group["run_user"] = group.get("run_user") or ""
    group["owner"] = group.get("owner") or ""
    group["purpose"] = group.get("purpose") or ""
    group["scope"] = group.get("scope") or ""
    group["environment"] = group.get("environment") or "local"
    group["risk_level"] = group.get("risk_level") or "low"
    group["expires_at"] = group.get("expires_at") or ""
    group["is_expired"] = governance.is_target_expired(group)
    group["continue_on_failure"] = int(group.get("continue_on_failure", 0))
    group["member_job_ids"] = [member["job_id"] for member in group.get("members", [])]
    return group


def group_form_context(request: Request, group: dict, errors: list[str], action: str, title: str) -> dict:
    member_job_ids = list(group.get("member_job_ids", []))
    return {
        "request": request,
        "group": group,
        "errors": errors,
        "action": action,
        "title": title,
        "jobs": db.list_jobs(),
        "member_job_ids": member_job_ids,
        "hour_options": hour_options(),
        "allowed_run_users": selectable_run_users(),
        "default_run_user": config.CRON_USER,
        **governance_options(),
    }


@app.get("/", dependencies=[Depends(require_auth)])
def index(request: Request, queued: str = ""):
    jobs = db.list_jobs()
    groups = db.list_groups()
    for job in jobs:
        job["is_expired"] = governance.is_target_expired(job)
        job["next_run"] = cron.next_run(job["cron_expr"]) if job.get("enabled") and not job["is_expired"] else None
        if queued and job["id"] == queued:
            job["has_running_run"] = 1
    for group in groups:
        group["is_expired"] = governance.is_target_expired(group)
        group["next_run"] = cron.next_run(group["cron_expr"]) if group.get("enabled") and not group["is_expired"] else None
        if queued and group["id"] == queued:
            group["has_running_run"] = 1
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "jobs": jobs, "groups": groups, "pause_status": governance.pause_status()},
    )


@app.get("/jobs/new", dependencies=[Depends(require_auth)])
def new_job(request: Request):
    job = {
        "name": "",
        "task_prompt": "",
        "cron_expr": "*/5 * * * *",
        "schedule_every": "5",
        "schedule_unit": "minutes",
        "provider_name": None,
        "model_id": None,
        "model_selection": "",
        "work_start": "",
        "work_end": "",
        "timeout_seconds": 240,
        "enabled": 1,
        "prevent_overlap": 1,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "full",
        "skills_mode": "none",
        "skill_ids": "",
        "skill_paths": "",
        "run_user": "",
        "owner": "",
        "purpose": "",
        "scope": "",
        "environment": "local",
        "risk_level": "low",
        "expires_at": "",
    }
    return templates.TemplateResponse(
        request,
        "job_form.html",
        job_form_context(request, job, [], "/jobs", "New Job"),
    )


@app.post("/jobs", dependencies=[Depends(require_auth)])
def create_job(
    request: Request,
    name: Annotated[str, Form()],
    task_prompt: Annotated[str, Form()],
    schedule_every: Annotated[str, Form()],
    schedule_unit: Annotated[str, Form()],
    timeout_seconds: Annotated[str, Form()],
    work_start: Annotated[str, Form()] = "",
    work_end: Annotated[str, Form()] = "",
    model_selection: Annotated[str, Form()] = "",
    output_mode: Annotated[str, Form()] = "summary",
    session_mode: Annotated[str, Form()] = "no_session",
    tool_mode: Annotated[str, Form()] = "full",
    skills_mode: Annotated[str, Form()] = "none",
    skill_ids: Annotated[list[str] | None, Form()] = None,
    run_user: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    prevent_overlap: Annotated[str | None, Form()] = None,
    owner: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "",
    scope: Annotated[str, Form()] = "",
    environment: Annotated[str, Form()] = "local",
    risk_level: Annotated[str, Form()] = "low",
    expires_at: Annotated[str, Form()] = "",
):
    data = form_data(
        name,
        task_prompt,
        schedule_every,
        schedule_unit,
        model_selection,
        output_mode,
        session_mode,
        tool_mode,
        work_start,
        work_end,
        timeout_seconds,
        run_user,
        enabled,
        prevent_overlap,
        skills_mode,
        skill_ids,
        owner=owner,
        purpose=purpose,
        scope=scope,
        environment=environment,
        risk_level=risk_level,
        expires_at=expires_at,
    )
    errors = validate_job_form(data)
    if errors:
        return templates.TemplateResponse(
            request,
            "job_form.html",
            job_form_context(request, data, errors, "/jobs", "New Job"),
            status_code=400,
        )
    data["timeout_seconds"] = int(data["timeout_seconds"])
    job_id = db.create_job(data)
    cron.write_cron_file()
    return redirect_to(f"/jobs/{job_id}")


@app.get("/groups/new", dependencies=[Depends(require_auth)])
def new_group(request: Request):
    group = {
        "name": "",
        "cron_expr": "*/5 * * * *",
        "schedule_every": "5",
        "schedule_unit": "minutes",
        "work_start": "",
        "work_end": "",
        "enabled": 1,
        "prevent_overlap": 1,
        "continue_on_failure": 0,
        "run_user": "",
        "owner": "",
        "purpose": "",
        "scope": "",
        "environment": "local",
        "risk_level": "low",
        "expires_at": "",
        "member_job_ids": [],
    }
    return templates.TemplateResponse(
        request,
        "group_form.html",
        group_form_context(request, group, [], "/groups", "New Job Group"),
    )


@app.post("/groups", dependencies=[Depends(require_auth)])
def create_group(
    request: Request,
    name: Annotated[str, Form()],
    schedule_every: Annotated[str, Form()],
    schedule_unit: Annotated[str, Form()],
    work_start: Annotated[str, Form()] = "",
    work_end: Annotated[str, Form()] = "",
    run_user: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    continue_on_failure: Annotated[str | None, Form()] = None,
    member_job_ids: Annotated[list[str] | None, Form()] = None,
    owner: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "",
    scope: Annotated[str, Form()] = "",
    environment: Annotated[str, Form()] = "local",
    risk_level: Annotated[str, Form()] = "low",
    expires_at: Annotated[str, Form()] = "",
):
    data = group_form_data(
        name,
        schedule_every,
        schedule_unit,
        work_start,
        work_end,
        run_user,
        enabled,
        continue_on_failure,
        member_job_ids,
        owner=owner,
        purpose=purpose,
        scope=scope,
        environment=environment,
        risk_level=risk_level,
        expires_at=expires_at,
    )
    errors = validate_group_form(data)
    if errors:
        return templates.TemplateResponse(
            request,
            "group_form.html",
            group_form_context(request, data, errors, "/groups", "New Job Group"),
            status_code=400,
        )
    group_id = db.create_group(data, data["member_job_ids"])
    cron.write_cron_file()
    return redirect_to(f"/groups/{group_id}")


@app.get("/groups/{group_id}", dependencies=[Depends(require_auth)])
def group_detail(
    request: Request,
    group_id: str,
    queued: Annotated[int, Query(ge=0, le=1)] = 0,
):
    group = db.get_group_with_members(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group["is_expired"] = governance.is_target_expired(group)
    runs = db.list_group_runs(group_id, RUNS_PER_PAGE)
    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "request": request,
            "group": group,
            "runs": runs,
            "has_running_run": bool(queued) or db.has_running_group_run(group_id),
        },
    )


@app.get("/groups/{group_id}/edit", dependencies=[Depends(require_auth)])
def edit_group(request: Request, group_id: str):
    group = db.get_group_with_members(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return templates.TemplateResponse(
        request,
        "group_form.html",
        group_form_context(request, with_group_schedule(group), [], f"/groups/{group_id}", "Edit Job Group"),
    )


@app.post("/groups/{group_id}", dependencies=[Depends(require_auth)])
def update_group(
    request: Request,
    group_id: str,
    name: Annotated[str, Form()],
    schedule_every: Annotated[str, Form()],
    schedule_unit: Annotated[str, Form()],
    work_start: Annotated[str, Form()] = "",
    work_end: Annotated[str, Form()] = "",
    run_user: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    continue_on_failure: Annotated[str | None, Form()] = None,
    member_job_ids: Annotated[list[str] | None, Form()] = None,
    owner: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "",
    scope: Annotated[str, Form()] = "",
    environment: Annotated[str, Form()] = "local",
    risk_level: Annotated[str, Form()] = "low",
    expires_at: Annotated[str, Form()] = "",
):
    if db.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail="Group not found")
    data = group_form_data(
        name,
        schedule_every,
        schedule_unit,
        work_start,
        work_end,
        run_user,
        enabled,
        continue_on_failure,
        member_job_ids,
        owner=owner,
        purpose=purpose,
        scope=scope,
        environment=environment,
        risk_level=risk_level,
        expires_at=expires_at,
    )
    errors = validate_group_form(data)
    if errors:
        data["id"] = group_id
        return templates.TemplateResponse(
            request,
            "group_form.html",
            group_form_context(request, data, errors, f"/groups/{group_id}", "Edit Job Group"),
            status_code=400,
        )
    db.update_group(group_id, data, data["member_job_ids"])
    cron.write_cron_file()
    return redirect_to(f"/groups/{group_id}")


@app.post("/groups/{group_id}/toggle", dependencies=[Depends(require_auth)])
def toggle_group(group_id: str, return_to: Annotated[str, Form()] = ""):
    group = db.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    db.set_group_enabled(group_id, not bool(group["enabled"]))
    cron.write_cron_file()
    if return_to == "detail":
        return redirect_to(f"/groups/{group_id}")
    return redirect_to("/")


@app.post("/groups/{group_id}/delete", dependencies=[Depends(require_auth)])
def delete_group(group_id: str):
    if db.get_group(group_id) is None:
        raise HTTPException(status_code=404, detail="Group not found")
    db.soft_delete_group(group_id)
    cron.write_cron_file()
    return redirect_to("/")


@app.post("/groups/{group_id}/run", dependencies=[Depends(require_auth)])
def manual_group_run(
    group_id: str,
    background_tasks: BackgroundTasks,
    return_to: Annotated[str, Form()] = "",
):
    group = db.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    ensure_execution_allowed(group, "Group")
    try:
        command = run_users.manual_runner_command("--group-id", group_id, group.get("run_user"))
    except run_users.RunUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(run_users.launch_command, command)
    if return_to == "index":
        return redirect_to(f"/?queued={group_id}")
    return redirect_to(f"/groups/{group_id}?queued=1")


@app.get("/groups/{group_id}/runs/{group_run_id}", dependencies=[Depends(require_auth)])
def group_run_detail(request: Request, group_id: str, group_run_id: str):
    group_run = db.get_group_run_with_steps(group_run_id)
    if group_run is None or group_run["group_id"] != group_id:
        raise HTTPException(status_code=404, detail="Group run not found")
    return templates.TemplateResponse(
        request,
        "group_run_detail.html",
        {"request": request, "group_run": group_run},
    )


@app.get("/group-runs/{group_run_id}", dependencies=[Depends(require_auth)])
def group_run_redirect(group_run_id: str):
    group_run = db.get_group_run_with_steps(group_run_id)
    if group_run is None:
        raise HTTPException(status_code=404, detail="Group run not found")
    return redirect_to(f"/groups/{group_run['group_id']}/runs/{group_run_id}")


@app.get("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def job_detail(
    request: Request,
    job_id: str,
    queued: Annotated[int, Query(ge=0, le=1)] = 0,
):
    status_data = db.get_job_runs_status(job_id, RUNS_PER_PAGE, 0)
    if status_data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job = status_data["job"]
    job["is_expired"] = governance.is_target_expired(job)
    command_error = None
    try:
        command = runner.build_command(job)[1]
    except pi_models.ModelConfigError as exc:
        command = runner.build_command(job, validate_model=False)[1]
        command_error = str(exc)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "request": request,
            "job": job,
            "runs": status_data["runs"],
            "command": command,
            "command_error": command_error,
            "has_running_run": bool(queued) or status_data["has_running_run"],
        },
    )


@app.get("/jobs/{job_id}/runs/status", dependencies=[Depends(require_auth)])
def job_runs_status(
    job_id: str,
    page: Annotated[int, Query(ge=1)] = 1,
    source: Annotated[str, Query(pattern="^(all|auto|manual)$")] = "all",
):
    run_source = None if source == "all" else source
    status_data = db.get_job_runs_status(
        job_id, RUNS_PER_PAGE + 1, (page - 1) * RUNS_PER_PAGE, run_source
    )
    if status_data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    runs_page = status_data["runs"]
    runs = runs_page[:RUNS_PER_PAGE]
    return {
        "has_running_run": status_data["has_running_run"],
        "runs": [format_run_summary(run) for run in runs],
        "page": page,
        "has_next_page": len(runs_page) > RUNS_PER_PAGE,
    }


@app.get("/logs", dependencies=[Depends(require_auth)])
def logs_page(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    job_id: str = "",
    group_id: str = "",
    source: Annotated[str, Query(pattern="^(all|auto|manual)$")] = "all",
    run_status: Annotated[
        str, Query(alias="status", pattern="^(all|running|success|failed|timeout)$")
    ] = "all",
    start_date: str = "",
    end_date: str = "",
):
    return templates.TemplateResponse(
        request,
        "logs.html",
        logs_context(request, page, job_id, group_id, source, run_status, start_date, end_date),
    )


@app.get("/maintenance/logs", dependencies=[Depends(require_auth)])
def maintenance_logs():
    return redirect_to("/logs")


@app.post("/logs/cleanup", dependencies=[Depends(require_auth)])
@app.post("/maintenance/logs", dependencies=[Depends(require_auth)])
def cleanup_logs(
    request: Request,
    mode: Annotated[str, Form()],
    days: Annotated[int, Form()] = 30,
):
    errors: list[str] = []
    result = None

    if mode == "older_than":
        if days < 1:
            errors.append("Days must be at least 1")
        if not errors:
            result = retention.cleanup_runs_before(retention.cutoff_for_days(days))
    elif mode == "all":
        result = retention.cleanup_all_runs()
    else:
        errors.append("Cleanup mode is invalid")

    return templates.TemplateResponse(
        request,
        "logs.html",
        logs_context(request, errors=errors, result=result, mode=mode, days=days),
    )


@app.get("/jobs/{job_id}/edit", dependencies=[Depends(require_auth)])
def edit_job(request: Request, job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        request,
        "job_form.html",
        job_form_context(request, with_schedule(job), [], f"/jobs/{job_id}", "Edit Job"),
    )


@app.post("/jobs/{job_id}", dependencies=[Depends(require_auth)])
def update_job(
    request: Request,
    job_id: str,
    name: Annotated[str, Form()],
    task_prompt: Annotated[str, Form()],
    schedule_every: Annotated[str, Form()],
    schedule_unit: Annotated[str, Form()],
    timeout_seconds: Annotated[str, Form()],
    work_start: Annotated[str, Form()] = "",
    work_end: Annotated[str, Form()] = "",
    model_selection: Annotated[str, Form()] = "",
    output_mode: Annotated[str, Form()] = "summary",
    session_mode: Annotated[str, Form()] = "no_session",
    tool_mode: Annotated[str, Form()] = "full",
    skills_mode: Annotated[str, Form()] = "none",
    skill_ids: Annotated[list[str] | None, Form()] = None,
    run_user: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    prevent_overlap: Annotated[str | None, Form()] = None,
    owner: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "",
    scope: Annotated[str, Form()] = "",
    environment: Annotated[str, Form()] = "local",
    risk_level: Annotated[str, Form()] = "low",
    expires_at: Annotated[str, Form()] = "",
):
    if db.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    data = form_data(
        name,
        task_prompt,
        schedule_every,
        schedule_unit,
        model_selection,
        output_mode,
        session_mode,
        tool_mode,
        work_start,
        work_end,
        timeout_seconds,
        run_user,
        enabled,
        prevent_overlap,
        skills_mode,
        skill_ids,
        owner=owner,
        purpose=purpose,
        scope=scope,
        environment=environment,
        risk_level=risk_level,
        expires_at=expires_at,
    )
    errors = validate_job_form(data)
    if errors:
        data["id"] = job_id
        return templates.TemplateResponse(
            request,
            "job_form.html",
            job_form_context(request, data, errors, f"/jobs/{job_id}", "Edit Job"),
            status_code=400,
        )
    data["timeout_seconds"] = int(data["timeout_seconds"])
    db.update_job(job_id, data)
    cron.write_cron_file()
    return redirect_to(f"/jobs/{job_id}")


@app.post("/jobs/{job_id}/toggle", dependencies=[Depends(require_auth)])
def toggle_job(job_id: str, return_to: Annotated[str, Form()] = ""):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.set_job_enabled(job_id, not bool(job["enabled"]))
    cron.write_cron_file()
    if return_to == "detail":
        return redirect_to(f"/jobs/{job_id}")
    return redirect_to("/")


@app.post("/jobs/{job_id}/delete", dependencies=[Depends(require_auth)])
def delete_job(job_id: str):
    if db.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        db.soft_delete_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cron.write_cron_file()
    return redirect_to("/")


@app.post("/jobs/{job_id}/run", dependencies=[Depends(require_auth)])
def manual_run(
    job_id: str,
    background_tasks: BackgroundTasks,
    return_to: Annotated[str, Form()] = "",
):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ensure_execution_allowed(job, "Job")
    try:
        command = run_users.manual_runner_command("--job-id", job_id, job.get("run_user"))
    except run_users.RunUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(run_users.launch_command, command)
    if return_to == "index":
        return redirect_to(f"/?queued={job_id}")
    return redirect_to(f"/jobs/{job_id}?queued=1")


@app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
def run_detail(request: Request, run_id: str):
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    job = db.get_job(run["job_id"])
    stdout = read_log(run.get("stdout_path"))
    stderr = read_log(run.get("stderr_path"))
    jsonl = read_log(run.get("jsonl_path"))
    has_jsonl = bool(run.get("jsonl_path"))
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "request": request,
            "run": run,
            "job": job,
            "stdout": stdout,
            "stderr": stderr,
            "jsonl": jsonl,
            "has_jsonl": has_jsonl,
            "group_run_url": group_run_path(run["group_run_id"], run.get("group_id")) if run.get("group_run_id") else "",
        },
    )


@app.get("/cron", dependencies=[Depends(require_auth)])
def cron_preview(request: Request):
    error = None
    content = ""
    try:
        content = cron.render_cron_file()
    except ValueError as exc:
        error = str(exc)
    status_info = cron_status.inspect(content if not error else None)
    return templates.TemplateResponse(
        request,
        "cron_preview.html",
        {
            "request": request,
            "content": content,
            "error": error,
            "cron_file": str(config.CRON_FILE),
            "cron_status": status_info,
            "pause_status": governance.pause_status(),
        },
    )


def read_log(path: str | None, max_bytes: int = LOG_PREVIEW_BYTES) -> str:
    if not path:
        return ""
    log_path = Path(path)
    if not log_path.exists():
        return "Log file not found"
    size = log_path.stat().st_size
    if size <= max_bytes:
        return log_path.read_text(encoding="utf-8", errors="replace")
    with log_path.open("rb") as handle:
        handle.seek(-max_bytes, 2)
        content = handle.read().decode("utf-8", errors="replace")
    return f"[Showing last {max_bytes // 1024} KiB of {math.ceil(size / 1024)} KiB log]\n\n{content}"

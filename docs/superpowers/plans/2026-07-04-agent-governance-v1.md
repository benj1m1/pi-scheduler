# Agent Governance v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add global pause, governance metadata, expiration guards, and audit logging to pi-scheduler.

**Architecture:** Store scheduler-wide state and audit events in SQLite, expose governance helpers from a focused `app/governance.py` module, and wire checks into cron rendering, manual launch routes, and runner execution. Extend existing job/group forms and detail/index pages using the current FastAPI + Jinja2 patterns.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLite, pytest, croniter, existing pi-scheduler modules.

## Global Constraints

- Do not mention private company names, internal document titles, or internal-source context in public docs, commit messages, comments, or UI text.
- Global pause defaults to inactive.
- Existing jobs/groups migrate with empty owner/purpose/scope, environment `local`, risk `low`, and no expiration.
- `expires_at` is stored as normalized `YYYY-MM-DD` and evaluated using Beijing local date semantics: expired when current Beijing date is later than `expires_at`.
- Paused or expired jobs/groups must be blocked in cron rendering, manual Run Now routes, and runner entrypoints.
- Audit logging for v1 covers job/group create, update, enable/disable, delete, manual run request, pause, and resume.
- If audit insertion fails during an audited admin request, fail the request rather than silently losing traceability.
- Preserve existing HTTP Basic admin model; actor is the authenticated username.
- Use TDD for every implementation task.

---

## File Structure

- Create `app/governance.py`: central helpers for app settings, pause state, expiration, governance validation, and audit event insertion/listing.
- Modify `app/db.py`: migrate new columns/tables and expose audit listing if not kept entirely in `governance.py`.
- Modify `app/cron.py`: skip executable cron lines when paused and skip expired jobs/groups.
- Modify `app/runner.py`: check paused/expired state before invoking `pi`.
- Modify `app/main.py`: form data, validation, routes, manual-run guards, pause/resume routes, audit page route, and context values.
- Modify templates: `base.html`, `index.html`, `job_form.html`, `job_detail.html`, `group_form.html`, `group_detail.html`, `cron_preview.html`, new `audit.html`.
- Modify `app/static/styles.css`: compact governance and audit styles.
- Modify `tests/test_core.py`: focused integration tests for DB migration, form validation, pause/expiration behavior, audit events, and UI rendering.
- Modify `README.md`: public, neutral documentation for governance controls.

---

### Task 1: Governance Storage, Validation, and Audit Helpers

**Files:**
- Create: `app/governance.py`
- Modify: `app/db.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `governance.ENVIRONMENTS`, `governance.RISK_LEVELS`, `governance.validate_metadata(data) -> list[str]`, `governance.normalize_expires_at(value) -> str | None`, `governance.is_expired(value, today=None) -> bool`, `governance.is_target_expired(target) -> bool`, `governance.pause_status() -> dict`, `governance.is_paused() -> bool`, `governance.pause(actor, reason, source_ip=None) -> None`, `governance.resume(actor, reason, source_ip=None) -> None`, `governance.record_audit_event(...) -> str`, `governance.list_audit_events(...) -> list[dict]`.
- Consumes: `db.connect()`, `db.utc_now()`.

- [ ] **Step 1: Write failing DB/governance tests**

Add these tests near existing DB/config tests in `tests/test_core.py`:

```python
def test_governance_migration_defaults_and_settings(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "flow", "cron_expr": "*/10 * * * *", "enabled": 1},
        [job_id],
    )

    job = db.get_job(job_id)
    group = db.get_group(group_id)
    assert job["owner"] == ""
    assert job["purpose"] == ""
    assert job["scope"] == ""
    assert job["environment"] == "local"
    assert job["risk_level"] == "low"
    assert job["expires_at"] is None
    assert group["environment"] == "local"
    assert group["risk_level"] == "low"
    assert group["expires_at"] is None
    assert governance.pause_status()["paused"] is False


def test_governance_validation_and_expiration_helpers():
    from datetime import date
    from app import governance

    assert governance.normalize_expires_at("") is None
    assert governance.normalize_expires_at("2026-07-04") == "2026-07-04"
    assert "Environment is invalid" in governance.validate_metadata({"environment": "prod", "risk_level": "low"})
    assert "Risk level is invalid" in governance.validate_metadata({"environment": "local", "risk_level": "critical"})
    assert "Expiration date must use YYYY-MM-DD" in governance.validate_metadata(
        {"environment": "local", "risk_level": "low", "expires_at": "07/04/2026"}
    )
    assert governance.is_expired("2026-07-03", today=date(2026, 7, 4)) is True
    assert governance.is_expired("2026-07-04", today=date(2026, 7, 4)) is False
    assert governance.is_expired(None, today=date(2026, 7, 4)) is False


def test_audit_event_round_trip(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    event_id = governance.record_audit_event(
        actor="admin",
        event_type="job.created",
        target_type="job",
        target_id="agent",
        summary="Created job agent",
        before=None,
        after={"name": "agent"},
        source_ip="127.0.0.1",
    )

    events = governance.list_audit_events(limit=10)
    assert events[0]["id"] == event_id
    assert events[0]["actor"] == "admin"
    assert events[0]["event_type"] == "job.created"
    assert events[0]["target_type"] == "job"
    assert events[0]["target_id"] == "agent"
    assert events[0]["summary"] == "Created job agent"
    assert events[0]["after"]["name"] == "agent"
    assert events[0]["source_ip"] == "127.0.0.1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_governance_migration_defaults_and_settings \
  tests/test_core.py::test_governance_validation_and_expiration_helpers \
  tests/test_core.py::test_audit_event_round_trip -v
```

Expected: FAIL with import/column/function errors.

- [ ] **Step 3: Implement DB migration**

In `app/db.py`, extend `init_db()` create scripts with new columns on both `jobs` and `job_groups`, plus settings/audit tables and indexes. Also add alter-table migration checks for existing databases.

Required new create-table columns for `jobs` and `job_groups`:

```sql
owner text not null default '',
purpose text not null default '',
scope text not null default '',
environment text not null default 'local',
risk_level text not null default 'low',
expires_at text,
```

Required new tables:

```sql
create table if not exists app_settings (
  key text primary key,
  value text not null,
  updated_at text not null
);

create table if not exists audit_events (
  id text primary key,
  created_at text not null,
  actor text not null,
  event_type text not null,
  target_type text not null,
  target_id text,
  summary text not null,
  before_json text,
  after_json text,
  source_ip text
);

create index if not exists idx_audit_events_created_at on audit_events(created_at desc);
create index if not exists idx_audit_events_target on audit_events(target_type, target_id, created_at desc);
```

Migration pattern:

```python
for column, ddl in {
    "owner": "text not null default ''",
    "purpose": "text not null default ''",
    "scope": "text not null default ''",
    "environment": "text not null default 'local'",
    "risk_level": "text not null default 'low'",
    "expires_at": "text",
}.items():
    if column not in columns:
        conn.execute(f"alter table jobs add column {column} {ddl}")
```

Repeat for `group_columns` and `job_groups`.

- [ ] **Step 4: Include metadata in create/update SQL**

Update `db.create_job()`, `db.update_job()`, `db.create_group()`, and `db.update_group()` so fields are persisted:

```python
data.get("owner", ""),
data.get("purpose", ""),
data.get("scope", ""),
data.get("environment", "local"),
data.get("risk_level", "low"),
data.get("expires_at"),
```

- [ ] **Step 5: Create `app/governance.py`**

Implement:

```python
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import db

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ENVIRONMENTS = {"local", "dev", "staging", "production"}
RISK_LEVELS = {"low", "medium", "high"}


def normalize_expires_at(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("Expiration date must use YYYY-MM-DD") from exc


def validate_metadata(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if (data.get("environment") or "local") not in ENVIRONMENTS:
        errors.append("Environment is invalid")
    if (data.get("risk_level") or "low") not in RISK_LEVELS:
        errors.append("Risk level is invalid")
    try:
        normalize_expires_at(data.get("expires_at"))
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def beijing_today() -> date:
    return datetime.now(BEIJING_TZ).date()


def is_expired(expires_at: str | None, today: date | None = None) -> bool:
    normalized = normalize_expires_at(expires_at)
    if normalized is None:
        return False
    current = today or beijing_today()
    return current > datetime.strptime(normalized, "%Y-%m-%d").date()


def is_target_expired(target: dict[str, Any]) -> bool:
    return is_expired(target.get("expires_at"))


def get_setting(key: str, default: str = "") -> str:
    with db.connect() as conn:
        row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    now = db.utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            insert into app_settings (key, value, updated_at) values (?, ?, ?)
            on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def pause_status() -> dict[str, str | bool]:
    return {
        "paused": get_setting("global_pause_enabled", "0") == "1",
        "reason": get_setting("global_pause_reason", ""),
        "updated_at": get_setting("global_pause_updated_at", ""),
        "updated_by": get_setting("global_pause_updated_by", ""),
    }


def is_paused() -> bool:
    return bool(pause_status()["paused"])


def _set_pause(enabled: bool, actor: str, reason: str, source_ip: str | None) -> None:
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("Reason is required")
    now = db.utc_now()
    set_setting("global_pause_enabled", "1" if enabled else "0")
    set_setting("global_pause_reason", cleaned if enabled else "")
    set_setting("global_pause_updated_at", now)
    set_setting("global_pause_updated_by", actor)
    record_audit_event(
        actor=actor,
        event_type="system.paused" if enabled else "system.resumed",
        target_type="system",
        target_id=None,
        summary=("Paused scheduler: " if enabled else "Resumed scheduler: ") + cleaned,
        before=None,
        after={"paused": enabled, "reason": cleaned, "updated_at": now},
        source_ip=source_ip,
    )


def pause(actor: str, reason: str, source_ip: str | None = None) -> None:
    _set_pause(True, actor, reason, source_ip)


def resume(actor: str, reason: str, source_ip: str | None = None) -> None:
    _set_pause(False, actor, reason, source_ip)


def record_audit_event(
    actor: str,
    event_type: str,
    target_type: str,
    target_id: str | None,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    source_ip: str | None = None,
) -> str:
    event_id = f"audit-{uuid.uuid4().hex}"
    with db.connect() as conn:
        conn.execute(
            """
            insert into audit_events (
              id, created_at, actor, event_type, target_type, target_id, summary,
              before_json, after_json, source_ip
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                db.utc_now(),
                actor,
                event_type,
                target_type,
                target_id,
                summary,
                json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
                json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
                source_ip,
            ),
        )
    return event_id


def list_audit_events(
    limit: int = 50,
    offset: int = 0,
    target_type: str | None = None,
    event_type: str | None = None,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if target_type:
        filters.append("target_type = ?")
        params.append(target_type)
    if event_type:
        filters.append("event_type = ?")
        params.append(event_type)
    if target_id:
        filters.append("target_id like ?")
        params.append(f"%{target_id}%")
    where = " where " + " and ".join(filters) if filters else ""
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            select * from audit_events
            {where}
            order by created_at desc
            limit ? offset ?
            """,
            params,
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["before"] = json.loads(event.pop("before_json")) if event.get("before_json") else None
        event["after"] = json.loads(event.pop("after_json")) if event.get("after_json") else None
        events.append(event)
    return events
```

- [ ] **Step 6: Run task tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_governance_migration_defaults_and_settings \
  tests/test_core.py::test_governance_validation_and_expiration_helpers \
  tests/test_core.py::test_audit_event_round_trip -v
```

Expected: PASS.

- [ ] **Step 7: Run full tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
cd /opt/pi-scheduler
git add app/db.py app/governance.py tests/test_core.py
git commit -m "feat: add governance storage and audit helpers"
```

---

### Task 2: Metadata Forms, Persistence, and Display

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/job_form.html`
- Modify: `app/templates/job_detail.html`
- Modify: `app/templates/group_form.html`
- Modify: `app/templates/group_detail.html`
- Modify: `app/templates/index.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: Task 1 `governance.validate_metadata()`, `governance.normalize_expires_at()`, `governance.ENVIRONMENTS`, `governance.RISK_LEVELS`, `governance.is_target_expired()`.
- Produces: form contexts include `environment_options`, `risk_level_options`, and persisted metadata fields.

- [ ] **Step 1: Write failing tests for form data and validation**

Add tests:

```python
def test_job_form_data_includes_governance_metadata(monkeypatch):
    data = web.form_data(
        "agent", "check logs", "5", "minutes", "", "summary", "no_session", "full",
        "", "", "240", "", "on", None, "none", "", 
        owner="Ben", purpose="Monitor logs", scope="/opt/app only",
        environment="dev", risk_level="medium", expires_at="2026-07-04",
    )

    assert data["owner"] == "Ben"
    assert data["purpose"] == "Monitor logs"
    assert data["scope"] == "/opt/app only"
    assert data["environment"] == "dev"
    assert data["risk_level"] == "medium"
    assert data["expires_at"] == "2026-07-04"
    assert web.validate_job_form(data) == []


def test_group_form_data_includes_governance_metadata():
    data = web.group_form_data(
        "flow", "5", "minutes", "", "", "", "on", None, ["job-a"],
        owner="Ben", purpose="Review chain", scope="local repo", environment="staging",
        risk_level="high", expires_at="2026-07-04",
    )

    assert data["owner"] == "Ben"
    assert data["purpose"] == "Review chain"
    assert data["scope"] == "local repo"
    assert data["environment"] == "staging"
    assert data["risk_level"] == "high"
    assert data["expires_at"] == "2026-07-04"


def test_validate_job_form_rejects_invalid_governance_metadata():
    data = web.form_data(
        "agent", "check logs", "5", "minutes", "", "summary", "no_session", "full",
        "", "", "240", "", "on", None, "none", "",
        owner="", purpose="", scope="", environment="invalid", risk_level="critical", expires_at="2026/07/04",
    )

    errors = web.validate_job_form(data)
    assert "Environment is invalid" in errors
    assert "Risk level is invalid" in errors
    assert "Expiration date must use YYYY-MM-DD" in errors
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_job_form_data_includes_governance_metadata \
  tests/test_core.py::test_group_form_data_includes_governance_metadata \
  tests/test_core.py::test_validate_job_form_rejects_invalid_governance_metadata -v
```

Expected: FAIL with unexpected keyword arguments or missing fields.

- [ ] **Step 3: Update `app/main.py` imports/constants**

Add `governance` to the existing import list:

```python
from . import approved_skills, config, cron, cron_status, db, governance, pi_models, retention, runner, run_users, runtime_setup, work_window
```

Add form context options:

```python
def governance_options() -> dict[str, list[str]]:
    return {
        "environment_options": sorted(governance.ENVIRONMENTS),
        "risk_level_options": ["low", "medium", "high"],
    }
```

- [ ] **Step 4: Extend `form_data()` signature and return values**

Add keyword-only args after `skill_ids`:

```python
*,
owner: str = "",
purpose: str = "",
scope: str = "",
environment: str = "local",
risk_level: str = "low",
expires_at: str = "",
```

Add to returned dict:

```python
"owner": owner.strip(),
"purpose": purpose.strip(),
"scope": scope.strip(),
"environment": environment,
"risk_level": risk_level,
"expires_at": governance.normalize_expires_at(expires_at),
```

- [ ] **Step 5: Extend `group_form_data()` signature and return values**

Add keyword-only args after `member_job_ids`:

```python
*,
owner: str = "",
purpose: str = "",
scope: str = "",
environment: str = "local",
risk_level: str = "low",
expires_at: str = "",
```

Add to returned dict:

```python
"owner": owner.strip(),
"purpose": purpose.strip(),
"scope": scope.strip(),
"environment": environment,
"risk_level": risk_level,
"expires_at": governance.normalize_expires_at(expires_at),
```

- [ ] **Step 6: Validate metadata**

In `validate_job_form()` and `validate_group_form()`, append:

```python
errors.extend(governance.validate_metadata(data))
```

If direct `normalize_expires_at()` in form_data raises before validation, adjust by storing raw date in form_data and normalizing only after validation passes in route handlers. The final behavior must preserve user-entered invalid value and show the validation error instead of raising a 500.

- [ ] **Step 7: Include metadata in `with_schedule()` and `with_group_schedule()`**

Set defaults:

```python
job["owner"] = job.get("owner") or ""
job["purpose"] = job.get("purpose") or ""
job["scope"] = job.get("scope") or ""
job["environment"] = job.get("environment") or "local"
job["risk_level"] = job.get("risk_level") or "low"
job["expires_at"] = job.get("expires_at") or ""
job["is_expired"] = governance.is_target_expired(job)
```

Repeat for group.

- [ ] **Step 8: Add options to form contexts**

In both `job_form_context()` and `group_form_context()`, include:

```python
**governance_options(),
```

- [ ] **Step 9: Extend route form parameters**

For job create/update routes, add:

```python
owner: Annotated[str, Form()] = "",
purpose: Annotated[str, Form()] = "",
scope: Annotated[str, Form()] = "",
environment: Annotated[str, Form()] = "local",
risk_level: Annotated[str, Form()] = "low",
expires_at: Annotated[str, Form()] = "",
```

Pass them to `form_data(..., owner=owner, purpose=purpose, scope=scope, environment=environment, risk_level=risk_level, expires_at=expires_at)`.

For group create/update routes, add and pass the same fields to `group_form_data()`.

- [ ] **Step 10: Add Governance fieldsets to forms**

In `app/templates/job_form.html` and `app/templates/group_form.html`, add a fieldset before actions:

```html
<fieldset>
  <legend>Governance</legend>
  <label>Owner
    <input type="text" name="owner" value="{{ job.owner if job is defined else group.owner }}" placeholder="Human owner">
  </label>
  <label>Purpose
    <textarea name="purpose" rows="2" placeholder="Why this automation exists">{{ job.purpose if job is defined else group.purpose }}</textarea>
  </label>
  <label>Scope / boundaries
    <textarea name="scope" rows="2" placeholder="Allowed systems, data, and boundaries">{{ job.scope if job is defined else group.scope }}</textarea>
  </label>
  <label>Environment
    <select name="environment">
      {% for value in environment_options %}
      <option value="{{ value }}" {% if (job.environment if job is defined else group.environment) == value %}selected{% endif %}>{{ value }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Risk level
    <select name="risk_level">
      {% for value in risk_level_options %}
      <option value="{{ value }}" {% if (job.risk_level if job is defined else group.risk_level) == value %}selected{% endif %}>{{ value }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Expires at
    <input type="date" name="expires_at" value="{{ job.expires_at if job is defined else group.expires_at }}">
  </label>
</fieldset>
```

If Jinja expression reuse is awkward, use separate job/group-specific blocks instead of a shared expression.

- [ ] **Step 11: Display governance metadata**

In job/group detail templates, add rows:

```html
<dt>Owner</dt><dd>{{ job.owner or 'Not documented' }}</dd>
<dt>Purpose</dt><dd>{{ job.purpose or 'Not documented' }}</dd>
<dt>Scope</dt><dd>{{ job.scope or 'Not documented' }}</dd>
<dt>Environment</dt><dd>{{ job.environment }}</dd>
<dt>Risk</dt><dd>{{ job.risk_level }}</dd>
<dt>Expires</dt><dd>{{ job.expires_at or 'Never' }}{% if job.is_expired %} <span class="badge bad">expired</span>{% endif %}</dd>
```

Repeat for groups.

On index cards, add compact rows for owner/environment/risk/expires.

- [ ] **Step 12: Run task tests and full tests**

Run targeted tests from Step 2. Then run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 13: Commit**

```bash
cd /opt/pi-scheduler
git add app/main.py app/templates/job_form.html app/templates/job_detail.html app/templates/group_form.html app/templates/group_detail.html app/templates/index.html app/static/styles.css tests/test_core.py
git commit -m "feat: add governance metadata to jobs and groups"
```

---

### Task 3: Pause and Expiration Execution Guards

**Files:**
- Modify: `app/cron.py`
- Modify: `app/main.py`
- Modify: `app/runner.py`
- Modify: `app/templates/index.html`
- Modify: `app/templates/cron_preview.html`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `governance.is_paused()`, `governance.pause_status()`, `governance.is_target_expired()`.
- Produces: paused/expired jobs do not execute via cron, manual, or runner.

- [ ] **Step 1: Write failing tests for cron guards**

Add:

```python
def test_render_cron_file_omits_entries_when_globally_paused(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root", raising=False)
    monkeypatch.setattr(config, "CRON_USER", "root")

    db.init_db()
    governance.pause("admin", "maintenance")
    content = cron.render_cron_file(
        jobs=[{"id": "agent", "cron_expr": "*/5 * * * *", "enabled": 1, "deleted_at": None, "run_user": None}],
        groups=[],
    )

    assert "pi-scheduler is globally paused" in content
    assert "--job-id agent" not in content


def test_render_cron_file_omits_expired_targets(monkeypatch):
    from datetime import date
    from app import governance

    monkeypatch.setattr(governance, "beijing_today", lambda: date(2026, 7, 5))
    content = cron.render_cron_file(
        jobs=[{"id": "agent", "cron_expr": "*/5 * * * *", "enabled": 1, "deleted_at": None, "run_user": None, "expires_at": "2026-07-04"}],
        groups=[{"id": "flow", "cron_expr": "*/10 * * * *", "enabled": 1, "deleted_at": None, "run_user": None, "expires_at": "2026-07-04"}],
    )

    assert "--job-id agent" not in content
    assert "--group-id flow" not in content
```

- [ ] **Step 2: Write failing tests for manual guard**

Add:

```python
def test_manual_run_blocked_when_paused(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job({"name": "agent", "skill_name": "general", "task_prompt": "check", "cron_expr": "*/5 * * * *"})
    governance.pause("admin", "maintenance")
    request = Request({"type": "http", "method": "POST", "path": f"/jobs/{job_id}/run", "headers": [], "client": ("127.0.0.1", 12345)})

    try:
        web.manual_run(request, BackgroundTasks(), job_id, actor="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "paused" in str(exc.detail).lower()
    else:
        raise AssertionError("Expected paused manual run to be blocked")
```

Adjust `actor` dependency invocation if route signature differs after implementation.

- [ ] **Step 3: Run tests to verify failure**

Run targeted tests. Expected: FAIL because guards do not exist.

- [ ] **Step 4: Implement cron guard**

In `app/cron.py`, import governance:

```python
from . import config, db, governance, run_users
```

At the start of `render_cron_file()` after header lines:

```python
if governance.is_paused():
    status = governance.pause_status()
    lines.append(f"# pi-scheduler is globally paused: {status.get('reason', '')}")
    lines.append("")
    return "\n".join(lines)
```

In job/group loops before validation:

```python
if governance.is_target_expired(job):
    continue
```

Repeat for groups.

- [ ] **Step 5: Implement manual guard helpers**

In `app/main.py`, add:

```python
def source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def ensure_execution_allowed(target: dict, target_label: str) -> None:
    if governance.is_paused():
        raise HTTPException(status_code=400, detail="Scheduler is globally paused")
    if governance.is_target_expired(target):
        raise HTTPException(status_code=400, detail=f"{target_label} is expired")
```

Call `ensure_execution_allowed(job, "Job")` in manual job route before building command. Call `ensure_execution_allowed(group, "Group")` in manual group route.

- [ ] **Step 6: Implement runner guard**

In `app/runner.py`, import governance:

```python
from . import approved_skills, config, cron, db, governance, pi_models, retention, work_window
```

In `run_job()` after loading job and before overlap/work-window/command build:

```python
if governance.is_paused():
    return create_terminal_run(job_id, "disabled", "", "Scheduler is globally paused", source, group_run_id)
if governance.is_target_expired(job):
    return create_terminal_run(job_id, "disabled", "", "Job is expired", source, group_run_id)
```

In group execution entrypoint, check pause and group expiration before running steps. Use existing group-run terminal status helpers; if there is no helper, insert a group run with status `disabled` or `failed` consistently with existing status conventions.

- [ ] **Step 7: Add paused UI status**

In index route context include `pause_status`. In cron preview route context include `pause_status`.

In `index.html`, show:

```html
{% if pause_status.paused %}
<section class="banner warning-banner">
  <strong>Scheduler paused.</strong> {{ pause_status.reason }}
</section>
{% endif %}
```

In `cron_preview.html`, show paused status near automatic scheduling card.

- [ ] **Step 8: Run full tests and commit**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

Commit:

```bash
git add app/cron.py app/main.py app/runner.py app/templates/index.html app/templates/cron_preview.html tests/test_core.py
git commit -m "feat: block execution when paused or expired"
```

---

### Task 4: Pause/Resume Controls and Audited Admin Actions

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/index.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `governance.pause()`, `governance.resume()`, `governance.record_audit_event()`.
- Produces: pause/resume routes and audit events for admin actions.

- [ ] **Step 1: Write failing audit route tests**

Add tests:

```python
def test_pause_and_resume_routes_record_audit_events(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    db.init_db()
    request = Request({"type": "http", "method": "POST", "path": "/governance/pause", "headers": [], "client": ("127.0.0.1", 12345)})

    response = web.pause_scheduler(request, actor="admin", reason="maintenance")
    assert response.status_code == 303
    assert governance.is_paused() is True

    response = web.resume_scheduler(request, actor="admin", reason="done")
    assert response.status_code == 303
    assert governance.is_paused() is False

    events = governance.list_audit_events(limit=10)
    assert [event["event_type"] for event in events[:2]] == ["system.resumed", "system.paused"]


def test_job_create_records_audit_event(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(web.cron, "write_cron_file", lambda: None)
    db.init_db()
    request = Request({"type": "http", "method": "POST", "path": "/jobs", "headers": [], "client": ("127.0.0.1", 12345)})

    response = web.create_job(
        request, actor="admin", name="agent", task_prompt="check", schedule_every="5", schedule_unit="minutes",
        model_selection="", output_mode="summary", session_mode="no_session", tool_mode="full",
        work_start="", work_end="", timeout_seconds="240", run_user="", enabled="on",
        prevent_overlap=None, skills_mode="none", skill_ids=[], owner="Ben", purpose="test",
        scope="local", environment="local", risk_level="low", expires_at="",
    )

    assert response.status_code == 303
    events = governance.list_audit_events(limit=1)
    assert events[0]["event_type"] == "job.created"
    assert events[0]["actor"] == "admin"
    assert events[0]["after"]["owner"] == "Ben"
```

- [ ] **Step 2: Run tests to verify failure**

Run targeted tests. Expected: FAIL because routes/audit calls are missing.

- [ ] **Step 3: Add pause/resume routes**

In `app/main.py`:

```python
@app.post("/governance/pause", dependencies=[Depends(require_auth)])
def pause_scheduler(request: Request, actor: Annotated[str, Depends(require_auth)], reason: Annotated[str, Form()]):
    governance.pause(actor, reason, source_ip(request))
    cron.write_cron_file()
    return redirect_to("/")


@app.post("/governance/resume", dependencies=[Depends(require_auth)])
def resume_scheduler(request: Request, actor: Annotated[str, Depends(require_auth)], reason: Annotated[str, Form()]):
    governance.resume(actor, reason, source_ip(request))
    cron.write_cron_file()
    return redirect_to("/")
```

Avoid duplicate dependency declarations if existing route style changes; final actor must come from `require_auth`.

- [ ] **Step 4: Add governance panel to index**

Index context:

```python
"pause_status": governance.pause_status(),
```

Template:

```html
<section class="panel governance-panel">
  <div class="card-head">
    <div>
      <p class="eyebrow">Governance</p>
      <h2>Scheduler control</h2>
      <p class="hint">Pause or resume all automatic and manual agent execution.</p>
    </div>
    <span class="badge {{ 'bad' if pause_status.paused else 'ok' }}">{{ 'paused' if pause_status.paused else 'active' }}</span>
  </div>
  {% if pause_status.paused %}
  <p><strong>Reason:</strong> {{ pause_status.reason }}</p>
  <form method="post" action="/governance/resume" class="inline-form">
    <input name="reason" required placeholder="Resume reason">
    <button type="submit">Resume</button>
  </form>
  {% else %}
  <form method="post" action="/governance/pause" class="inline-form">
    <input name="reason" required placeholder="Pause reason">
    <button class="danger" type="submit">Pause all</button>
  </form>
  {% endif %}
</section>
```

- [ ] **Step 5: Record audit events for job/group admin actions**

For each route after successful DB write and before redirect:

Create:

```python
after = db.get_job(job_id)
governance.record_audit_event(actor, "job.created", "job", job_id, f"Created job {after['name']}", after=after, source_ip=source_ip(request))
```

Update:

```python
before = db.get_job(job_id)
db.update_job(job_id, data)
after = db.get_job(job_id)
governance.record_audit_event(actor, "job.updated", "job", job_id, f"Updated job {after['name']}", before=before, after=after, source_ip=source_ip(request))
```

Toggle:

```python
event_type = "job.enabled" if enabled else "job.disabled"
```

Delete:

```python
governance.record_audit_event(actor, "job.deleted", "job", job_id, f"Deleted job {before['name']}", before=before, after={"deleted": True}, source_ip=source_ip(request))
```

Repeat with `group.*` event types for groups.

- [ ] **Step 6: Record manual run request audit events**

In manual job/group routes after command is built and before background launch:

```python
governance.record_audit_event(
    actor,
    "job.manual_run_requested",
    "job",
    job_id,
    f"Manual run requested for job {job['name']}",
    after={"run_user": run_users.effective_run_user(job.get("run_user")), "source": "manual"},
    source_ip=source_ip(request),
)
```

Use `group.manual_run_requested` for groups.

- [ ] **Step 7: Run full tests and commit**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Commit:

```bash
git add app/main.py app/templates/index.html app/static/styles.css tests/test_core.py
git commit -m "feat: audit governance admin actions"
```

---

### Task 5: Audit Log Page

**Files:**
- Create: `app/templates/audit.html`
- Modify: `app/main.py`
- Modify: `app/templates/base.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `governance.list_audit_events()`.
- Produces: `/audit` page with filters and pagination.

- [ ] **Step 1: Write failing page test**

Add:

```python
def test_audit_page_renders_events(tmp_path, monkeypatch):
    from app import governance

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    db.init_db()
    governance.record_audit_event("admin", "job.created", "job", "agent", "Created job agent", after={"name": "agent"})
    request = Request({"type": "http", "method": "GET", "path": "/audit", "headers": []})

    response = web.audit_log(request)
    html = web.templates.env.get_template("audit.html").render(response.context)

    assert "Audit Log" in html
    assert "job.created" in html
    assert "Created job agent" in html
    assert "admin" in html
```

- [ ] **Step 2: Run test to verify failure**

Run targeted test. Expected: FAIL because route/template missing.

- [ ] **Step 3: Add route and context**

In `app/main.py`:

```python
AUDIT_PER_PAGE = 50

@app.get("/audit", dependencies=[Depends(require_auth)])
def audit_log(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    target_type: str = "",
    event_type: str = "",
    target_id: str = "",
):
    events_page = governance.list_audit_events(
        limit=AUDIT_PER_PAGE + 1,
        offset=(page - 1) * AUDIT_PER_PAGE,
        target_type=target_type or None,
        event_type=event_type or None,
        target_id=target_id or None,
    )
    events = events_page[:AUDIT_PER_PAGE]
    filters = {"target_type": target_type, "event_type": event_type, "target_id": target_id}
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "request": request,
            "events": events,
            "filters": filters,
            "page": page,
            "has_next_page": len(events_page) > AUDIT_PER_PAGE,
        },
    )
```

Add a helper for audit pagination if desired; keep it simple for v1.

- [ ] **Step 4: Create `audit.html`**

```html
{% extends "base.html" %}
{% block title %}Audit Log - Pi Scheduler{% endblock %}
{% block content %}
<section class="panel page-panel">
  <div class="card-head">
    <div>
      <p class="eyebrow">Governance</p>
      <h1>Audit Log</h1>
    </div>
  </div>
  <form class="filters" method="get" action="/audit">
    <label>Target type <input name="target_type" value="{{ filters.target_type }}" placeholder="job, group, system"></label>
    <label>Event type <input name="event_type" value="{{ filters.event_type }}" placeholder="job.updated"></label>
    <label>Target id <input name="target_id" value="{{ filters.target_id }}"></label>
    <button type="submit">Filter</button>
    <a class="button-secondary" href="/audit">Clear</a>
  </form>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Target</th><th>Summary</th><th>Details</th></tr></thead>
      <tbody>
      {% for event in events %}
      <tr>
        <td>{{ event.created_at|beijing_time }}</td>
        <td>{{ event.actor }}</td>
        <td><code>{{ event.event_type }}</code></td>
        <td>{{ event.target_type }}{% if event.target_id %}: {{ event.target_id }}{% endif %}</td>
        <td>{{ event.summary }}</td>
        <td>
          <details>
            <summary>JSON</summary>
            <pre>{{ {'before': event.before, 'after': event.after}|tojson(indent=2) }}</pre>
          </details>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6">No audit events found.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endblock %}
```

- [ ] **Step 5: Add nav link**

In `base.html` primary nav, add:

```html
<a href="/audit" {% if request.url.path.startswith('/audit') %}aria-current="page"{% endif %}>Audit</a>
```

- [ ] **Step 6: Run full tests and commit**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Commit:

```bash
git add app/main.py app/templates/base.html app/templates/audit.html app/static/styles.css tests/test_core.py
git commit -m "feat: add audit log page"
```

---

### Task 6: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Test: full verification commands

**Interfaces:**
- Consumes: completed Tasks 1-5.
- Produces: public documentation for governance controls without private organization references.

- [ ] **Step 1: Update README**

Add a neutral section:

```markdown
## Agent Governance

Pi Scheduler includes governance controls for scheduled Pi agents:

- Global pause stops automatic cron execution and manual Run Now requests.
- Jobs and groups can document owner, purpose, scope, environment, risk level, and expiration date.
- Expired jobs and groups are not executed by cron, manual runs, or direct runner invocation.
- Audit logs record administrative changes, manual run requests, and pause/resume events.

These controls are intended to support accountability, scoped execution, traceability, and emergency disablement.
```

Document fields and behavior in the existing configuration/usage sections. Do not mention private companies or internal document names.

- [ ] **Step 2: Search for disallowed public references**

Run a repository search for private organization names, private internal document titles, and internal-source wording before committing. Keep the exact private terms out of this public plan file and out of committed scripts.

Expected: no matches in tracked public files.

- [ ] **Step 3: Run full tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 4: Run syntax checks**

Run:

```bash
cd /opt/pi-scheduler
python -m compileall app bin
bash -n deploy/run-local.sh
bash -n deploy/setup-runtime-user.sh
```

Expected: commands exit 0.

- [ ] **Step 5: Check git diff**

Run:

```bash
cd /opt/pi-scheduler
git diff --check
git status --short
```

Expected: only intended README changes before commit; no whitespace errors.

- [ ] **Step 6: Commit docs**

```bash
cd /opt/pi-scheduler
git add README.md
git commit -m "docs: document agent governance controls"
```

- [ ] **Step 7: Final status summary**

Run:

```bash
cd /opt/pi-scheduler
git log --oneline -8
git status --short
```

Expected: working tree clean.

---

## Self-Review

- Spec coverage: Tasks cover storage/settings/audit, metadata UI, pause/expiration guards in cron/manual/runner, pause/resume controls, audit UI, and public docs.
- No private organization references: plan uses neutral wording only and includes a final search gate.
- Type consistency: `governance.py` function names are introduced in Task 1 and reused consistently in later tasks.
- Execution safety: paused/expired checks are deliberately duplicated at cron, manual route, and runner layers.
- Backward compatibility: migrations default old records to local/low/no expiration and pause defaults inactive.

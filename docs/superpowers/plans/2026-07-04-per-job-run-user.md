# Per-Job Run User Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional Linux run-user selection for standalone jobs and job groups, with allowlist validation and matching scheduled/manual execution behavior.

**Architecture:** Introduce a focused `app/run_users.py` module for username validation, effective-user resolution, and manual runner subprocess launch construction. Persist nullable `run_user` on `jobs` and `job_groups`; cron rendering uses explicit `run_user` or global default. Manual Run Now routes launch `bin/pi-job-runner --source manual` as the effective run user rather than calling runner functions in-process.

**Tech Stack:** Python 3.10+, FastAPI forms/background tasks, SQLite inline migrations, system cron, pytest.

## Global Constraints

- Existing installations remain backward-compatible: unset `run_user` falls back to `PI_SCHEDULER_CRON_USER`.
- Group execution uses one group-level Linux user; member job `run_user` values do not apply inside group runs.
- Non-empty run users must be syntactically safe, allowlisted, and present in the system passwd database.
- If `PI_SCHEDULER_ALLOWED_RUN_USERS` is unset or empty, only `PI_SCHEDULER_CRON_USER` is allowed.
- Manual Run Now must use the same effective user as scheduled cron when the web process can switch users.
- Use subprocess argument lists, not shell interpolation, for manual user switching.
- Do not implement filesystem sandboxing or per-member user switching in this feature.

---

## File Structure

- `app/run_users.py`: new focused module for username allowlist parsing, validation, display labels, and manual runner subprocess helpers.
- `app/config.py`: add `ALLOWED_RUN_USERS` environment-derived string.
- `app/db.py`: add `run_user` migrations and persist/list fields for jobs and groups.
- `app/cron.py`: use effective run user for job/group cron entries.
- `app/main.py`: parse/validate form run users, pass UI context, and launch manual runs through `run_users`.
- `app/runner.py`: accept `--source` CLI option.
- Templates: add run-user form fields and detail/dashboard display.
- `tests/test_core.py`: unit tests for validation, DB persistence, cron rendering, CLI source, and manual launch behavior.
- `README.md` and `deploy/pi-scheduler-web.service`: document allowed users and deployment permissions.

---

### Task 1: Add run-user validation and configuration

**Files:**
- Create: `app/run_users.py`
- Modify: `app/config.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `run_users.allowed_run_users() -> list[str]`
- Produces: `run_users.effective_run_user(value: str | None) -> str`
- Produces: `run_users.describe_run_user(value: str | None) -> str`
- Produces: `run_users.validate_run_user(value: str | None) -> None`
- Produces: `run_users.RunUserError(ValueError)`

- [ ] **Step 1: Write failing tests for allowlist and validation**

Add these tests to `tests/test_core.py` near existing config/cron tests:

```python
def test_run_user_allowlist_defaults_to_cron_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "", raising=False)

    assert run_users.allowed_run_users() == ["root"]
    assert run_users.effective_run_user(None) == "root"
    assert run_users.effective_run_user("") == "root"
    assert run_users.describe_run_user(None) == "default (root)"


def test_validate_run_user_rejects_unsafe_and_unallowed_users(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    run_users.validate_run_user(None)
    run_users.validate_run_user("piagent")

    for value in ["bad user", "bad;user", "../root"]:
        try:
            run_users.validate_run_user(value)
        except run_users.RunUserError as exc:
            assert "invalid" in str(exc).lower()
        else:
            raise AssertionError(f"Expected invalid username rejection for {value}")

    try:
        run_users.validate_run_user("bjli")
    except run_users.RunUserError as exc:
        assert "not allowed" in str(exc).lower()
    else:
        raise AssertionError("Expected allowlist rejection")


def test_validate_run_user_rejects_missing_system_user(monkeypatch):
    from app import run_users

    def missing_user(name):
        raise KeyError(name)

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,ghost", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", missing_user)

    try:
        run_users.validate_run_user("ghost")
    except run_users.RunUserError as exc:
        assert "does not exist" in str(exc).lower()
    else:
        raise AssertionError("Expected missing system user rejection")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_run_user_allowlist_defaults_to_cron_user tests/test_core.py::test_validate_run_user_rejects_unsafe_and_unallowed_users tests/test_core.py::test_validate_run_user_rejects_missing_system_user -v
```

Expected: FAIL with `ImportError` or `AttributeError` because `app.run_users` / `ALLOWED_RUN_USERS` does not exist.

- [ ] **Step 3: Add config value**

In `app/config.py`, after `CRON_USER = ...`, add:

```python
ALLOWED_RUN_USERS = os.environ.get("PI_SCHEDULER_ALLOWED_RUN_USERS", "")
```

- [ ] **Step 4: Create `app/run_users.py`**

Create file with:

```python
from __future__ import annotations

import pwd
import re

from . import config


USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")


class RunUserError(ValueError):
    pass


def allowed_run_users() -> list[str]:
    raw = config.ALLOWED_RUN_USERS.strip()
    if not raw:
        return [config.CRON_USER]
    users: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        user = item.strip()
        if user and user not in seen:
            seen.add(user)
            users.append(user)
    return users or [config.CRON_USER]


def effective_run_user(value: str | None) -> str:
    return (value or "").strip() or config.CRON_USER


def describe_run_user(value: str | None) -> str:
    user = (value or "").strip()
    if not user:
        return f"default ({config.CRON_USER})"
    return user


def validate_run_user(value: str | None) -> None:
    user = (value or "").strip()
    if not user:
        return
    if not USERNAME_RE.fullmatch(user):
        raise RunUserError("Run user is invalid")
    if user not in allowed_run_users():
        raise RunUserError(f"Run user '{user}' is not allowed")
    try:
        pwd.getpwnam(user)
    except KeyError as exc:
        raise RunUserError(f"Run user '{user}' does not exist on this system") from exc
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_run_user_allowlist_defaults_to_cron_user tests/test_core.py::test_validate_run_user_rejects_unsafe_and_unallowed_users tests/test_core.py::test_validate_run_user_rejects_missing_system_user -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/run_users.py tests/test_core.py
git commit -m "feat: add run user validation"
```

---

### Task 2: Persist run users and render cron entries with explicit users

**Files:**
- Modify: `app/db.py`
- Modify: `app/cron.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `run_users.validate_run_user(value)`
- Produces: `jobs.run_user` and `job_groups.run_user` storage
- Produces: `db.list_jobs_for_cron()` rows containing `run_user`
- Produces: `db.list_groups_for_cron()` rows containing `run_user`

- [ ] **Step 1: Write failing DB and cron tests**

Add these tests to `tests/test_core.py` near DB/cron tests:

```python
def test_db_persists_job_run_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "run_user": "piagent",
        }
    )

    assert db.get_job(job_id)["run_user"] == "piagent"

    db.update_job(
        job_id,
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent again",
            "cron_expr": "*/10 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "run_user": "root",
        },
    )

    assert db.get_job(job_id)["run_user"] == "root"
    assert db.list_jobs_for_cron()[0]["run_user"] == "root"


def test_db_persists_group_run_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "flow", "cron_expr": "*/10 * * * *", "enabled": 1, "run_user": "piagent"},
        [job_id],
    )

    assert db.get_group(group_id)["run_user"] == "piagent"

    db.update_group(
        group_id,
        {"name": "flow", "cron_expr": "*/15 * * * *", "enabled": 1, "run_user": "root"},
        [job_id],
    )

    assert db.get_group(group_id)["run_user"] == "root"
    assert db.list_groups_for_cron()[0]["run_user"] == "root"


def test_render_cron_file_uses_job_and_group_run_users(monkeypatch):
    monkeypatch.setattr(config, "CRON_USER", "root")

    content = cron.render_cron_file(
        [
            {"id": "default-job", "cron_expr": "*/5 * * * *", "enabled": 1, "deleted_at": None, "run_user": None},
            {"id": "user-job", "cron_expr": "0 * * * *", "enabled": 1, "deleted_at": None, "run_user": "piagent"},
        ],
        [
            {"id": "user-group", "cron_expr": "*/30 * * * *", "enabled": 1, "deleted_at": None, "run_user": "piagent"},
        ],
    )

    assert "*/5 * * * * root " in content
    assert "0 * * * * piagent " in content
    assert "*/30 * * * * piagent " in content
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_db_persists_job_run_user tests/test_core.py::test_db_persists_group_run_user tests/test_core.py::test_render_cron_file_uses_job_and_group_run_users -v
```

Expected: FAIL because `run_user` columns/queries/cron output do not exist yet.

- [ ] **Step 3: Add DB migrations and persistence**

In `app/db.py`:

- Add `run_user text` to the `jobs` table definition after `tool_mode`.
- Add `run_user text` to the `job_groups` table definition after `continue_on_failure`.
- In `init_db()`, add ALTER migrations:

```python
if "run_user" not in columns:
    conn.execute("alter table jobs add column run_user text")
```

and for groups:

```python
if "run_user" not in group_columns:
    conn.execute("alter table job_groups add column run_user text")
```

- Update `list_jobs_for_cron()` SELECT to include `run_user`.
- Update `list_groups_for_cron()` SELECT to include `run_user`.
- Update `create_job()` INSERT column list and values with `run_user`.
- Update `update_job()` SET list and parameters with `run_user`.
- Update `create_group()` INSERT column list and values with `run_user`.
- Update `update_group()` SET list and parameters with `run_user`.

- [ ] **Step 4: Update cron rendering**

In `app/cron.py`, import run-user helpers:

```python
from . import config, db, run_users
```

Replace job cron user interpolation with:

```python
run_user = run_users.effective_run_user(job.get("run_user"))
run_users.validate_run_user(job.get("run_user"))
lines.append(
    f"{job['cron_expr']} {run_user} {config.RUNNER_PATH} --job-id {job['id']}"
)
```

Replace group cron user interpolation similarly:

```python
run_user = run_users.effective_run_user(group.get("run_user"))
run_users.validate_run_user(group.get("run_user"))
lines.append(
    f"{group['cron_expr']} {run_user} {config.RUNNER_PATH} --group-id {group['id']}"
)
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_db_persists_job_run_user tests/test_core.py::test_db_persists_group_run_user tests/test_core.py::test_render_cron_file_uses_job_and_group_run_users -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db.py app/cron.py tests/test_core.py
git commit -m "feat: persist run users in cron targets"
```

---

### Task 3: Add run-user fields to web forms and pages

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/job_form.html`
- Modify: `app/templates/group_form.html`
- Modify: `app/templates/index.html`
- Modify: `app/templates/job_detail.html`
- Modify: `app/templates/group_detail.html`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `run_users.validate_run_user`, `run_users.allowed_run_users`, `run_users.describe_run_user`
- Produces: form data dictionaries containing `run_user`
- Produces: template contexts containing `allowed_run_users` and `default_run_user`

- [ ] **Step 1: Write failing form tests**

Add tests:

```python
def test_job_form_data_includes_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.form_data(
        "agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "piagent",
        "on",
        None,
    )

    assert data["run_user"] == "piagent"
    assert web.validate_job_form(data) == []


def test_group_form_data_includes_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.group_form_data(
        "flow",
        "5",
        "minutes",
        "",
        "",
        "piagent",
        "on",
        None,
        ["job-a"],
    )

    assert data["run_user"] == "piagent"
    assert "Run user" not in "\n".join(web.validate_group_form(data))


def test_validate_job_form_rejects_invalid_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.form_data(
        "agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "bad user",
        "on",
        None,
    )

    assert "Run user is invalid" in web.validate_job_form(data)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_job_form_data_includes_run_user tests/test_core.py::test_group_form_data_includes_run_user tests/test_core.py::test_validate_job_form_rejects_invalid_run_user -v
```

Expected: FAIL because form function signatures do not include `run_user`.

- [ ] **Step 3: Update `app/main.py` form handling**

In imports, include run users:

```python
from . import config, cron, db, pi_models, retention, runner, run_users, work_window
```

Add Jinja filter:

```python
templates.env.filters["describe_run_user"] = run_users.describe_run_user
```

Update `validate_job_form()` and `validate_group_form()` to call:

```python
try:
    run_users.validate_run_user(data.get("run_user"))
except run_users.RunUserError as exc:
    errors.append(str(exc))
```

Update `form_data()` signature by inserting `run_user: str` before `enabled`, and add to returned dict:

```python
"run_user": run_user.strip() or None,
```

Update all calls to `form_data()` to pass the FastAPI form parameter:

```python
run_user: Annotated[str, Form()] = "",
```

Update `group_form_data()` similarly by inserting `run_user: str` before `enabled` and returning `"run_user": run_user.strip() or None`.

Update new job/group defaults with:

```python
"run_user": "",
```

Update `with_schedule()` and `with_group_schedule()` to normalize:

```python
job["run_user"] = job.get("run_user") or ""
group["run_user"] = group.get("run_user") or ""
```

Update `job_form_context()` and `group_form_context()` context dicts with:

```python
"allowed_run_users": run_users.allowed_run_users(),
"default_run_user": config.CRON_USER,
```

- [ ] **Step 4: Update templates**

In `app/templates/job_form.html`, after the tool access fieldset and before Schedule, add:

```html
    <fieldset>
      <legend>Run user</legend>
      <label>Run as user
        <select name="run_user">
          <option value="" {% if not job.run_user %}selected{% endif %}>Default ({{ default_run_user }})</option>
          {% for user in allowed_run_users %}
          <option value="{{ user }}" {% if job.run_user == user %}selected{% endif %}>{{ user }}</option>
          {% endfor %}
        </select>
      </label>
      <p class="hint">Scheduled and manual runs use this Linux user. Leave as default to use <code>{{ default_run_user }}</code>.</p>
    </fieldset>
```

In `app/templates/group_form.html`, after Members fieldset and before Schedule, add the same fieldset but replace hint with:

```html
      <p class="hint">The whole group runs as this Linux user. Member job run users are ignored during group execution.</p>
```

In `app/templates/index.html`, add to group card metadata after Work:

```html
        <dt>Run as</dt><dd>{{ group.run_user|describe_run_user }}</dd>
```

Add to job card metadata after Work:

```html
      <dt>Run as</dt><dd>{{ job.run_user|describe_run_user }}</dd>
```

In `app/templates/job_detail.html`, add after Work window:

```html
    <dt>Run as</dt><dd>{{ job.run_user|describe_run_user }}</dd>
```

In `app/templates/group_detail.html`, add after Work window:

```html
    <dt>Run as</dt><dd>{{ group.run_user|describe_run_user }}</dd>
```

and update Member guards text to:

```html
    <dt>Member guards</dt><dd>Member enabled states, work windows, and standalone run users do not gate group execution. Member overlap still applies.</dd>
```

- [ ] **Step 5: Run targeted tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_job_form_data_includes_run_user tests/test_core.py::test_group_form_data_includes_run_user tests/test_core.py::test_validate_job_form_rejects_invalid_run_user -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/templates/job_form.html app/templates/group_form.html app/templates/index.html app/templates/job_detail.html app/templates/group_detail.html tests/test_core.py
git commit -m "feat: expose run users in scheduler UI"
```

---

### Task 4: Run manual jobs/groups through the runner subprocess with the effective user

**Files:**
- Modify: `app/run_users.py`
- Modify: `app/main.py`
- Modify: `app/runner.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `run_users.manual_runner_command(target_flag: str, target_id: str, run_user_value: str | None, source: str = "manual") -> list[str]`
- Produces: `run_users.launch_manual_runner(target_flag: str, target_id: str, run_user_value: str | None) -> None`
- Consumes: `runner.main()` `--source` argument

- [ ] **Step 1: Write failing tests for command construction and runner source**

Add tests:

```python
def test_manual_runner_command_direct_when_already_target_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "piagent")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "piagent")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 1001)

    assert run_users.manual_runner_command("--job-id", "job-a", None) == [
        "/opt/pi-scheduler/bin/pi-job-runner",
        "--job-id",
        "job-a",
        "--source",
        "manual",
    ]


def test_manual_runner_command_uses_sudo_from_root(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "root")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 0)
    monkeypatch.setattr(run_users.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)

    assert run_users.manual_runner_command("--group-id", "flow", "piagent") == [
        "/usr/bin/sudo",
        "-u",
        "piagent",
        "/opt/pi-scheduler/bin/pi-job-runner",
        "--group-id",
        "flow",
        "--source",
        "manual",
    ]


def test_manual_runner_command_rejects_switch_from_non_root(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "bjli")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 1000)

    try:
        run_users.manual_runner_command("--job-id", "job-a", "piagent")
    except run_users.RunUserError as exc:
        assert "cannot switch" in str(exc).lower()
    else:
        raise AssertionError("Expected non-root switch rejection")


def test_runner_cli_accepts_manual_source(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_job", lambda job_id, source="auto": calls.append((job_id, source)) or 0)
    monkeypatch.setattr("sys.argv", ["pi-job-runner", "--job-id", "job-a", "--source", "manual"])

    assert runner.main() == 0
    assert calls == [("job-a", "manual")]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_manual_runner_command_direct_when_already_target_user tests/test_core.py::test_manual_runner_command_uses_sudo_from_root tests/test_core.py::test_manual_runner_command_rejects_switch_from_non_root tests/test_core.py::test_runner_cli_accepts_manual_source -v
```

Expected: FAIL because manual runner helpers and `--source` do not exist.

- [ ] **Step 3: Implement manual runner helpers**

In `app/run_users.py`, add imports:

```python
import getpass
import os
import shutil
import subprocess
```

Add functions:

```python
def manual_runner_command(target_flag: str, target_id: str, run_user_value: str | None, source: str = "manual") -> list[str]:
    if target_flag not in {"--job-id", "--group-id"}:
        raise RunUserError("Manual runner target is invalid")
    validate_run_user(run_user_value)
    target_user = effective_run_user(run_user_value)
    runner_args = [config.RUNNER_PATH, target_flag, target_id, "--source", source]
    current_user = getpass.getuser()
    if current_user == target_user:
        return runner_args
    if os.geteuid() != 0:
        raise RunUserError(
            f"Manual run cannot switch from {current_user} to {target_user}; run the web service as root or configure user switching"
        )
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, "-u", target_user, *runner_args]
    runuser = shutil.which("runuser")
    if runuser:
        return [runuser, "-u", target_user, "--", *runner_args]
    raise RunUserError("Manual run cannot switch users because neither sudo nor runuser is available")


def launch_manual_runner(target_flag: str, target_id: str, run_user_value: str | None) -> None:
    command = manual_runner_command(target_flag, target_id, run_user_value)
    subprocess.Popen(command, cwd=str(config.SCHEDULER_HOME), start_new_session=True)
```

- [ ] **Step 4: Update runner CLI**

In `app/runner.py`, update `main()` parser:

```python
parser.add_argument("--source", choices=["auto", "manual"], default="auto")
```

Update dispatch:

```python
if args.group_id:
    return run_group(args.group_id, source=args.source)
return run_job(args.job_id, source=args.source)
```

- [ ] **Step 5: Update manual routes**

In `app/main.py`, replace job manual route background task:

```python
job = db.get_job(job_id)
if job is None:
    raise HTTPException(status_code=404, detail="Job not found")
try:
    background_tasks.add_task(run_users.launch_manual_runner, "--job-id", job_id, job.get("run_user"))
except run_users.RunUserError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
```

Replace group route similarly:

```python
group = db.get_group(group_id)
if group is None:
    raise HTTPException(status_code=404, detail="Group not found")
try:
    background_tasks.add_task(run_users.launch_manual_runner, "--group-id", group_id, group.get("run_user"))
except run_users.RunUserError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 6: Run targeted tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_core.py::test_manual_runner_command_direct_when_already_target_user tests/test_core.py::test_manual_runner_command_uses_sudo_from_root tests/test_core.py::test_manual_runner_command_rejects_switch_from_non_root tests/test_core.py::test_runner_cli_accepts_manual_source -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/run_users.py app/main.py app/runner.py tests/test_core.py
git commit -m "feat: launch manual runs as configured user"
```

---

### Task 5: Document deployment and run the full verification suite

**Files:**
- Modify: `README.md`
- Modify: `deploy/pi-scheduler-web.service`
- Test: full pytest suite

**Interfaces:**
- Consumes: completed run-user behavior from Tasks 1-4
- Produces: operator documentation for `PI_SCHEDULER_ALLOWED_RUN_USERS` and dedicated user setup

- [ ] **Step 1: Update README configuration table**

Add row after `PI_SCHEDULER_CRON_USER`:

```markdown
| `PI_SCHEDULER_ALLOWED_RUN_USERS` | `<cron user>` | Comma-separated allowlist for per-job/per-group Linux run users. Empty means only `PI_SCHEDULER_CRON_USER` is allowed. |
```

- [ ] **Step 2: Add README run-user section**

After the Work Windows section, add:

```markdown
### Run Users

Jobs and groups can optionally specify a Linux user to run as. Leave the field blank to use `PI_SCHEDULER_CRON_USER`.

Standalone jobs use their own run user. Groups use the group run user for the whole pipeline; member job run users are ignored during group execution.

For safety, non-default users must be allowlisted:

```bash
PI_SCHEDULER_ALLOWED_RUN_USERS=root,piagent
```

A typical dedicated user setup:

```bash
sudo useradd --create-home --shell /bin/bash piagent
sudo groupadd -f pi-scheduler
sudo usermod -aG pi-scheduler piagent
sudo chgrp -R pi-scheduler /opt/pi-scheduler/data /opt/pi-scheduler/logs /opt/pi-scheduler/locks /opt/pi-scheduler/tmp
sudo chmod -R g+rwX /opt/pi-scheduler/data /opt/pi-scheduler/logs /opt/pi-scheduler/locks /opt/pi-scheduler/tmp
sudo find /opt/pi-scheduler/data /opt/pi-scheduler/logs /opt/pi-scheduler/locks /opt/pi-scheduler/tmp -type d -exec chmod g+s {} \;
```

The run user needs Pi CLI credentials and model configuration under its own home directory, for example `/home/piagent/.pi/agent/`.

Manual Run Now uses the configured run user by launching `bin/pi-job-runner --source manual`. If the web service runs as root, it switches users with `sudo -u` or `runuser`. If it cannot switch users, the manual run is rejected instead of running as the wrong user.
```

- [ ] **Step 3: Update service example**

In `deploy/pi-scheduler-web.service`, add:

```ini
Environment=PI_SCHEDULER_ALLOWED_RUN_USERS=root,piagent
```

- [ ] **Step 4: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: all tests PASS.

- [ ] **Step 5: Inspect cron preview manually**

Run:

```bash
.venv/bin/python - <<'PY'
from app import cron
print(cron.render_cron_file(
    [{"id":"job-a","cron_expr":"*/5 * * * *","enabled":1,"deleted_at":None,"run_user":None}],
    [{"id":"group-a","cron_expr":"0 * * * *","enabled":1,"deleted_at":None,"run_user":"root"}],
))
PY
```

Expected output contains one `root` job line and one `root` group line without exceptions.

- [ ] **Step 6: Commit docs**

```bash
git add README.md deploy/pi-scheduler-web.service
git commit -m "docs: document scheduler run users"
```

- [ ] **Step 7: Final status check**

Run:

```bash
git status --short
```

Expected: no uncommitted changes.

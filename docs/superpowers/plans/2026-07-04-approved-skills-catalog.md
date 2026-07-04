# Approved Skills Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-form job skill paths with a controlled Approved Skills Catalog under `/opt/pi-scheduler/approved-skills`, while keeping `No skills` as the default for every job.

**Architecture:** Add a focused `app/approved_skills.py` module that lists and resolves catalog skills by safe IDs. Persist selected `skill_ids` on jobs, update form validation and UI to submit IDs instead of paths, and update the runner to resolve IDs into `--skill <catalog path>` arguments only after safety validation.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLite, pytest, Pi CLI `--no-skills` and repeated `--skill <path>` flags.

## Global Constraints

- New and existing jobs must default to `skills_mode = 'none'` and run with `pi --no-skills` unless explicitly configured otherwise.
- Approved skills root defaults to `/opt/pi-scheduler/approved-skills`.
- Jobs store newline-separated skill IDs, not arbitrary absolute paths.
- Valid skill IDs match `^[A-Za-z0-9][A-Za-z0-9_-]*$`.
- Catalog entries must be direct child directories containing `SKILL.md`.
- Symlinked catalog entries are invalid.
- Resolved skill paths must remain inside the approved skills root.
- UI must not expose a free-form skill path textarea.
- Approved mode requires at least one selected valid catalog skill.
- If a selected approved skill is missing at run time, the run must fail safely and must not fall back to runtime default skills.
- FastAPI startup may warn about missing/unreadable catalog state but must not create or chmod directories.
- Deployment scripts may create `/opt/pi-scheduler/approved-skills` with root/admin-controlled permissions.

---

## File Structure

- Create `app/approved_skills.py`
  - Owns catalog scanning, safe ID validation, `SKILL.md` metadata parsing, and ID-to-path resolution.
- Modify `app/config.py`
  - Adds `APPROVED_SKILLS_DIR` from `PI_SCHEDULER_APPROVED_SKILLS_DIR`, default `/opt/pi-scheduler/approved-skills`.
- Modify `app/db.py`
  - Adds `skill_ids text not null default ''` to `jobs`.
  - Persists `skill_ids` in create/update paths.
  - Keeps `skill_paths` for compatibility but stops relying on it for new UI behavior.
- Modify `app/main.py`
  - Validates approved skills against the catalog.
  - Accepts repeated `skill_ids` form fields.
  - Passes catalog entries to templates.
  - Updates display helper to describe selected skills.
- Modify `app/runner.py`
  - Replaces approved-mode path parsing with catalog ID resolution.
  - Keeps default `--no-skills` and runtime mode behavior.
- Modify `app/templates/job_form.html`
  - Replaces `skill_paths` textarea with catalog checkbox list.
- Modify `app/templates/job_detail.html`
  - Displays selected skill IDs/names and missing skill warnings.
- Modify `app/templates/index.html`
  - Displays compact selected skills summary.
- Modify `deploy/setup-runtime-user.sh`
  - Creates `/opt/pi-scheduler/approved-skills` with safe permissions.
- Modify `README.md`
  - Documents Approved Skills Catalog and removal of free-form paths.
- Modify `tests/test_core.py`
  - Adds TDD coverage for catalog scanning, validation, command building, UI form data, and migration.

---

### Task 1: Add Approved Skills Catalog Reader

**Files:**
- Create: `app/approved_skills.py`
- Modify: `app/config.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `approved_skills.SkillEntry`, `approved_skills.SkillCatalogError`, `approved_skills.is_valid_skill_id(skill_id: str) -> bool`, `approved_skills.list_skills() -> list[SkillEntry]`, `approved_skills.resolve_skill_path(skill_id: str) -> Path`, `approved_skills.parse_skill_ids(raw: str | list[str] | None) -> list[str]`
- Consumes: `config.APPROVED_SKILLS_DIR: Path`

- [ ] **Step 1: Write failing catalog tests**

Add these tests near the skills command tests in `tests/test_core.py`:

```python
def test_approved_skills_catalog_lists_valid_directories(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Work with PDFs\n---\n\n# PDF\n",
        encoding="utf-8",
    )
    (root / "obsidian-markdown").mkdir()
    (root / "obsidian-markdown" / "SKILL.md").write_text(
        "---\nname: obsidian-markdown\ndescription: Work with Obsidian markdown\n---\n",
        encoding="utf-8",
    )
    (root / "missing-skill-md").mkdir()
    (root / "bad id").mkdir()
    (root / "bad id" / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
    (root / "file-skill").write_text("not a directory", encoding="utf-8")

    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    entries = approved_skills.list_skills()

    assert [entry.id for entry in entries] == ["obsidian-markdown", "pdf"]
    assert entries[0].name == "obsidian-markdown"
    assert entries[1].description == "Work with PDFs"


def test_approved_skills_catalog_rejects_traversal_absolute_and_missing(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    assert approved_skills.resolve_skill_path("pdf") == (root / "pdf").resolve()

    for value in ["../pdf", "/tmp/pdf", "bad id", ".hidden", "missing"]:
        try:
            approved_skills.resolve_skill_path(value)
        except approved_skills.SkillCatalogError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"expected {value!r} to be rejected")


def test_approved_skills_catalog_rejects_symlink_entries(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    external = tmp_path / "external"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: external\n---\n", encoding="utf-8")
    root.mkdir()
    (root / "external").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    assert approved_skills.list_skills() == []
    try:
        approved_skills.resolve_skill_path("external")
    except approved_skills.SkillCatalogError as exc:
        assert "not an approved skill" in str(exc)
    else:
        raise AssertionError("expected symlinked skill to be rejected")


def test_parse_skill_ids_normalizes_repeated_and_newline_values():
    from app import approved_skills

    assert approved_skills.parse_skill_ids(None) == []
    assert approved_skills.parse_skill_ids("pdf\nobsidian-markdown\n") == ["pdf", "obsidian-markdown"]
    assert approved_skills.parse_skill_ids(["pdf", "", "obsidian-markdown"]) == ["pdf", "obsidian-markdown"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_approved_skills_catalog_lists_valid_directories \
  tests/test_core.py::test_approved_skills_catalog_rejects_traversal_absolute_and_missing \
  tests/test_core.py::test_approved_skills_catalog_rejects_symlink_entries \
  tests/test_core.py::test_parse_skill_ids_normalizes_repeated_and_newline_values -v
```

Expected: FAIL because `app.approved_skills` does not exist.

- [ ] **Step 3: Add catalog configuration**

In `app/config.py`, add:

```python
APPROVED_SKILLS_DIR = Path(
    os.getenv("PI_SCHEDULER_APPROVED_SKILLS_DIR", "/opt/pi-scheduler/approved-skills")
).expanduser().resolve()
```

Place it near the other path configuration constants.

- [ ] **Step 4: Implement `app/approved_skills.py`**

Create `app/approved_skills.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from . import config

SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SkillCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class SkillEntry:
    id: str
    name: str
    description: str
    path: Path


def is_valid_skill_id(skill_id: str) -> bool:
    return bool(SKILL_ID_RE.fullmatch(skill_id or ""))


def parse_skill_ids(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.splitlines()
    else:
        values = list(raw)
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in result:
            result.append(item)
    return result


def _frontmatter_value(text: str, key: str) -> str:
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        prefix = f"{key}:"
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
    return ""


def _metadata(skill_id: str, skill_file: Path) -> tuple[str, str]:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return skill_id, ""
    name = _frontmatter_value(text, "name") or skill_id
    description = _frontmatter_value(text, "description")
    return name, description


def _safe_child_path(root: Path, skill_id: str) -> Path:
    if not is_valid_skill_id(skill_id):
        raise SkillCatalogError("Skill ID is invalid")
    resolved_root = root.resolve()
    candidate = root / skill_id
    if candidate.is_symlink():
        raise SkillCatalogError(f"Skill {skill_id!r} is not an approved skill")
    resolved = candidate.resolve()
    if resolved.parent != resolved_root:
        raise SkillCatalogError("Skill path escapes the approved skills directory")
    return resolved


def list_skills() -> list[SkillEntry]:
    root = config.APPROVED_SKILLS_DIR
    if not root.exists() or not root.is_dir():
        return []
    entries: list[SkillEntry] = []
    for child in root.iterdir():
        skill_id = child.name
        if not is_valid_skill_id(skill_id):
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            resolved = _safe_child_path(root, skill_id)
        except SkillCatalogError:
            continue
        skill_file = resolved / "SKILL.md"
        if not skill_file.is_file():
            continue
        name, description = _metadata(skill_id, skill_file)
        entries.append(SkillEntry(id=skill_id, name=name, description=description, path=resolved))
    return sorted(entries, key=lambda entry: entry.id)


def resolve_skill_path(skill_id: str) -> Path:
    root = config.APPROVED_SKILLS_DIR
    resolved = _safe_child_path(root, skill_id)
    if not resolved.is_dir() or not (resolved / "SKILL.md").is_file():
        raise SkillCatalogError(f"Skill {skill_id!r} is not an approved skill")
    return resolved
```

- [ ] **Step 5: Run catalog tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_approved_skills_catalog_lists_valid_directories \
  tests/test_core.py::test_approved_skills_catalog_rejects_traversal_absolute_and_missing \
  tests/test_core.py::test_approved_skills_catalog_rejects_symlink_entries \
  tests/test_core.py::test_parse_skill_ids_normalizes_repeated_and_newline_values -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /opt/pi-scheduler
git add app/config.py app/approved_skills.py tests/test_core.py
git commit -m "feat: add approved skills catalog reader"
```

---

### Task 2: Persist Skill IDs and Validate Forms Against the Catalog

**Files:**
- Modify: `app/db.py`
- Modify: `app/main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `approved_skills.parse_skill_ids()`, `approved_skills.resolve_skill_path()`, `approved_skills.SkillCatalogError`
- Produces: job dictionaries with `skill_ids: str`

- [ ] **Step 1: Write failing persistence and validation tests**

Add tests near existing form-data tests in `tests/test_core.py`:

```python
def test_form_data_accepts_selected_approved_skill_ids():
    data = web.form_data(
        "pi-agent",
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
        "",
        "on",
        None,
        "approved",
        ["pdf", "obsidian-markdown"],
    )

    assert data["skills_mode"] == "approved"
    assert data["skill_ids"] == "pdf\nobsidian-markdown"
    assert data["skill_paths"] == ""


def test_validate_job_form_accepts_existing_catalog_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    data = {
        "name": "agent",
        "task_prompt": "check logs",
        "cron_expr": "*/5 * * * *",
        "schedule_error": None,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "full",
        "skills_mode": "approved",
        "skill_ids": "pdf",
        "run_user": "",
        "timeout_seconds": "240",
    }

    assert "Approved skill" not in "\n".join(web.validate_job_form(data))


def test_validate_job_form_rejects_missing_catalog_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    data = {
        "name": "agent",
        "task_prompt": "check logs",
        "cron_expr": "*/5 * * * *",
        "schedule_error": None,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "full",
        "skills_mode": "approved",
        "skill_ids": "pdf",
        "run_user": "",
        "timeout_seconds": "240",
    }

    assert "Approved skill 'pdf' is not available" in web.validate_job_form(data)


def test_database_persists_skill_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
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
            "skills_mode": "approved",
            "skill_ids": "pdf\nobsidian-markdown",
        }
    )

    job = db.get_job(job_id)
    assert job["skill_ids"] == "pdf\nobsidian-markdown"
```

- [ ] **Step 2: Run tests to verify failures**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_form_data_accepts_selected_approved_skill_ids \
  tests/test_core.py::test_validate_job_form_accepts_existing_catalog_skill_ids \
  tests/test_core.py::test_validate_job_form_rejects_missing_catalog_skill_ids \
  tests/test_core.py::test_database_persists_skill_ids -v
```

Expected: FAIL because `skill_ids` is not wired yet.

- [ ] **Step 3: Add `skill_ids` to database schema and migrations**

In `app/db.py`, update the `jobs` table definition:

```python
              skills_mode text not null default 'none',
              skill_paths text not null default '',
              skill_ids text not null default '',
              run_user text,
```

In the migration block after `skill_paths`:

```python
        if "skill_ids" not in columns:
            conn.execute("alter table jobs add column skill_ids text not null default ''")
```

In `create_job`, add `skill_ids` to the insert column list after `skill_paths`, add one `?` placeholder, and add:

```python
                data.get("skill_ids", ""),
```

In `update_job`, add `skill_ids = ?` after `skill_paths = ?`, and add:

```python
                data.get("skill_ids", ""),
```

- [ ] **Step 4: Update form data and validation**

In `app/main.py`, import the catalog module:

```python
from . import approved_skills, config, cron, cron_status, db, pi_models, retention, run_users, runner, runtime_setup, work_window
```

Update `form_data` signature so the end is:

```python
    prevent_overlap: str | None,
    skills_mode: str = "none",
    skill_ids: str | list[str] | None = None,
) -> dict:
```

Inside `form_data`, replace `skill_paths` handling with:

```python
    selected_skill_ids = approved_skills.parse_skill_ids(skill_ids)
```

Return these fields:

```python
        "skills_mode": skills_mode,
        "skill_ids": "\n".join(selected_skill_ids),
        "skill_paths": "",
```

In `validate_job_form`, replace approved `skill_paths` validation with:

```python
    if data.get("skills_mode") == "approved":
        ids = approved_skills.parse_skill_ids(data.get("skill_ids"))
        if not ids:
            errors.append("At least one approved skill is required")
        for skill_id in ids:
            try:
                approved_skills.resolve_skill_path(skill_id)
            except approved_skills.SkillCatalogError:
                errors.append(f"Approved skill {skill_id!r} is not available")
```

In `with_schedule`, add:

```python
    job["skill_ids"] = job.get("skill_ids") or ""
```

In `new_job`, add:

```python
        "skill_ids": "",
```

- [ ] **Step 5: Update create/edit route parameters**

In both `create_job` and `update_job`, replace the existing `skill_paths` form parameter with repeated `skill_ids` support:

```python
    skill_ids: Annotated[list[str] | None, Form()] = None,
```

Keep `skills_mode` as:

```python
    skills_mode: Annotated[str, Form()] = "none",
```

Update the `form_data(...)` calls so the final arguments are:

```python
        enabled,
        prevent_overlap,
        skills_mode,
        skill_ids,
```

- [ ] **Step 6: Run tests from this task**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_form_data_accepts_selected_approved_skill_ids \
  tests/test_core.py::test_validate_job_form_accepts_existing_catalog_skill_ids \
  tests/test_core.py::test_validate_job_form_rejects_missing_catalog_skill_ids \
  tests/test_core.py::test_database_persists_skill_ids -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /opt/pi-scheduler
git add app/db.py app/main.py tests/test_core.py
git commit -m "feat: persist approved skill ids"
```

---

### Task 3: Resolve Approved Skill IDs in Runner

**Files:**
- Modify: `app/runner.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `approved_skills.parse_skill_ids()`, `approved_skills.resolve_skill_path()`, `approved_skills.SkillCatalogError`
- Produces: `runner.build_command(job: dict, validate_model: bool = True) -> tuple[list[str], str]` with catalog-resolved `--skill` arguments

- [ ] **Step 1: Replace old path-based runner tests with ID-based tests**

Update `test_build_command_loads_only_approved_skill_paths` to become:

```python
def test_build_command_loads_only_approved_catalog_skills(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    (root / "obsidian-markdown").mkdir()
    (root / "obsidian-markdown" / "SKILL.md").write_text("---\nname: obsidian-markdown\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    argv, display = runner.build_command(
        {
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
            "skills_mode": "approved",
            "skill_ids": "pdf\nobsidian-markdown",
        }
    )

    assert argv == [
        "pi",
        "--no-skills",
        "--skill",
        str((root / "pdf").resolve()),
        "--skill",
        str((root / "obsidian-markdown").resolve()),
        "--no-session",
        "-p",
        "summarize status",
    ]
    assert f"--skill {root / 'pdf'}" in display
```

Add:

```python
def test_build_command_rejects_missing_approved_catalog_skill(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    try:
        runner.build_command(
            {
                "task_prompt": "summarize status",
                "skills_mode": "approved",
                "skill_ids": "missing",
            }
        )
    except runner.RunnerConfigError as exc:
        assert "Approved skill 'missing' is not available" in str(exc)
    else:
        raise AssertionError("expected missing catalog skill to fail")
```

- [ ] **Step 2: Run tests to verify failures**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_build_command_loads_only_approved_catalog_skills \
  tests/test_core.py::test_build_command_rejects_missing_approved_catalog_skill -v
```

Expected: FAIL because runner still reads `skill_paths`.

- [ ] **Step 3: Implement ID-based runner resolution**

In `app/runner.py`, import the catalog module:

```python
from . import approved_skills, config, db, pi_models, work_window
```

Add a runner configuration error if one does not already exist:

```python
class RunnerConfigError(ValueError):
    pass
```

Replace `skill_paths(job)` with:

```python
def skill_ids(job: dict) -> list[str]:
    return approved_skills.parse_skill_ids(job.get("skill_ids"))
```

Update the approved-mode block in `build_command`:

```python
    if selected_skills_mode == "approved":
        for skill_id in skill_ids(job):
            try:
                path = approved_skills.resolve_skill_path(skill_id)
            except approved_skills.SkillCatalogError as exc:
                raise RunnerConfigError(f"Approved skill {skill_id!r} is not available") from exc
            argv.extend(["--skill", str(path)])
```

- [ ] **Step 4: Ensure run failures are recorded safely**

Find the existing `runner.run_job()` error handling around `build_command(job)`. If `RunnerConfigError` is not caught by the broad existing exception path, add a focused catch that records a failed run with the error message. Use the same status/logging path as model configuration errors.

The desired behavior is: a job configured with a deleted approved skill creates a failed run record and stderr/log output mentioning `Approved skill 'missing' is not available`.

- [ ] **Step 5: Add a run-time failure test**

Add:

```python
def test_run_job_fails_safely_when_approved_skill_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    called = {"run": False}

    def fake_run(argv, **kwargs):
        called["run"] = True
        raise AssertionError("pi should not be invoked when skill resolution fails")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
            "skills_mode": "approved",
            "skill_ids": "missing",
        }
    )

    exit_code = runner.run_job(job_id)
    run = db.get_run(db.list_recent_runs(job_id)[0]["id"])

    assert exit_code == 1
    assert called["run"] is False
    assert run["status"] == "failed"
    assert "Approved skill 'missing' is not available" in Path(run["stderr_path"]).read_text(encoding="utf-8")
```

- [ ] **Step 6: Run runner tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_build_command_disables_skills_by_default \
  tests/test_core.py::test_build_command_loads_only_approved_catalog_skills \
  tests/test_core.py::test_build_command_rejects_missing_approved_catalog_skill \
  tests/test_core.py::test_build_command_can_use_runtime_default_skills \
  tests/test_core.py::test_run_job_fails_safely_when_approved_skill_disappears -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /opt/pi-scheduler
git add app/runner.py tests/test_core.py
git commit -m "feat: resolve approved skills by catalog id"
```

---

### Task 4: Replace Free-Form Skill Path UI With Catalog Checkboxes

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/job_form.html`
- Modify: `app/templates/job_detail.html`
- Modify: `app/templates/index.html`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `approved_skills.list_skills()`, `approved_skills.parse_skill_ids()`
- Produces: template context keys `approved_skills: list[SkillEntry]`, `selected_skill_ids: set[str]`, `missing_skill_ids: list[str]`

- [ ] **Step 1: Write UI context test**

Add:

```python
def test_job_form_context_includes_catalog_and_selected_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: PDFs\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    context = web.job_form_context(
        request=None,
        job={
            "name": "agent",
            "task_prompt": "check logs",
            "schedule_every": "5",
            "schedule_unit": "minutes",
            "skills_mode": "approved",
            "skill_ids": "pdf\nmissing",
        },
        errors=[],
        action="/jobs/new",
        title="New Job",
    )

    assert [entry.id for entry in context["approved_skills"]] == ["pdf"]
    assert context["selected_skill_ids"] == {"pdf", "missing"}
    assert context["missing_skill_ids"] == ["missing"]
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_job_form_context_includes_catalog_and_selected_skill_ids -v
```

Expected: FAIL because context keys are missing.

- [ ] **Step 3: Update `job_form_context`**

In `app/main.py`, update `job_form_context` to add:

```python
    catalog = approved_skills.list_skills()
    selected_skill_ids = set(approved_skills.parse_skill_ids(job.get("skill_ids")))
    catalog_ids = {entry.id for entry in catalog}
    missing_skill_ids = sorted(selected_skill_ids - catalog_ids)
```

Return these keys:

```python
        "approved_skills": catalog,
        "selected_skill_ids": selected_skill_ids,
        "missing_skill_ids": missing_skill_ids,
```

If `job_form_context` currently returns a dict inline, add these fields to that dict.

- [ ] **Step 4: Replace `job_form.html` free-form textarea**

In `app/templates/job_form.html`, replace the current Skills policy select and `skill_paths` textarea with:

```html
      <label>Skills policy
        <select name="skills_mode" required>
          <option value="none" {% if job.skills_mode == 'none' %}selected{% endif %}>No skills</option>
          <option value="approved" {% if job.skills_mode == 'approved' %}selected{% endif %}>Approved skills</option>
          <option value="runtime" {% if job.skills_mode == 'runtime' %}selected{% endif %}>Runtime user default skills</option>
        </select>
      </label>
      <p class="hint">Default is <code>No skills</code>, which invokes <code>pi --no-skills</code>. Approved skills are loaded from the scheduler-managed catalog.</p>
      <div class="field-block">
        <p class="field-label">Approved skills catalog</p>
        {% if approved_skills %}
          <div class="checkbox-list">
            {% for skill in approved_skills %}
            <label class="checkbox-card">
              <input type="checkbox" name="skill_ids" value="{{ skill.id }}" {% if skill.id in selected_skill_ids %}checked{% endif %}>
              <span><strong>{{ skill.name }}</strong><small>{{ skill.id }}</small>{% if skill.description %}<em>{{ skill.description }}</em>{% endif %}</span>
            </label>
            {% endfor %}
          </div>
        {% else %}
          <p class="hint warning-text">No approved skills are installed in the catalog.</p>
        {% endif %}
        {% if missing_skill_ids %}
          <p class="hint bad-text">This job references missing approved skills: {{ missing_skill_ids|join(', ') }}.</p>
        {% endif %}
      </div>
```

If existing CSS has no `.checkbox-list`, use the existing form/list classes first. Add CSS only if the page looks broken.

- [ ] **Step 5: Add selected-skill display helper**

In `app/main.py`, add:

```python
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
```

Register it:

```python
templates.env.filters["describe_job_skills"] = describe_job_skills
```

- [ ] **Step 6: Update display templates**

In `app/templates/job_detail.html`, replace the current Skills row with:

```html
    <dt>Skills</dt><dd>{{ job|describe_job_skills }}</dd>
```

In `app/templates/index.html`, replace the job Skills row with:

```html
      <dt>Skills</dt><dd>{{ job|describe_job_skills }}</dd>
```

- [ ] **Step 7: Run UI/form tests and full suite**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_job_form_context_includes_catalog_and_selected_skill_ids -v
.venv/bin/python -m pytest
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
cd /opt/pi-scheduler
git add app/main.py app/templates/job_form.html app/templates/job_detail.html app/templates/index.html tests/test_core.py
git commit -m "feat: select approved skills from catalog"
```

---

### Task 5: Deployment Setup and Documentation

**Files:**
- Modify: `deploy/setup-runtime-user.sh`
- Modify: `README.md`
- Test: `tests/test_core.py` or shell checks where existing deploy-script tests live

**Interfaces:**
- Consumes: `PI_SCHEDULER_APPROVED_SKILLS_DIR`
- Produces: deploy script creates approved skills directory with admin-controlled permissions

- [ ] **Step 1: Extend the existing setup script test**

In `tests/test_core.py`, update `test_setup_runtime_user_script_syntax_and_defaults` by adding these assertions after the `MODELS_FILE` assertion:

```python
    assert 'APPROVED_SKILLS_DIR="${PI_SCHEDULER_APPROVED_SKILLS_DIR:-/opt/pi-scheduler/approved-skills}"' in content
    assert 'install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$APPROVED_SKILLS_DIR"' in content
```

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_setup_runtime_user_script_syntax_and_defaults -v
```

Expected: FAIL until `deploy/setup-runtime-user.sh` creates the approved skills directory.

- [ ] **Step 2: Update `deploy/setup-runtime-user.sh`**

Add a configurable variable near the existing user/group/home variables:

```bash
APPROVED_SKILLS_DIR="${PI_SCHEDULER_APPROVED_SKILLS_DIR:-/opt/pi-scheduler/approved-skills}"
```

After runtime directories are created, add:

```bash
install -d -o root -g "$RUNTIME_GROUP" -m 0750 "$APPROVED_SKILLS_DIR"
```

Print a status line:

```bash
echo "Approved skills directory: $APPROVED_SKILLS_DIR"
```

Do not copy any root skills into this directory.

- [ ] **Step 3: Update README**

In `README.md`, replace the free-form approved paths documentation with this text:

````markdown
### Approved Skills Catalog

Jobs run with `--no-skills` by default. To allow a job to use skills, install reviewed skills under the scheduler-managed catalog:

```bash
sudo install -d -o root -g pi-scheduler -m 0750 /opt/pi-scheduler/approved-skills
sudo cp -a /source/pdf /opt/pi-scheduler/approved-skills/pdf
sudo chown -R root:pi-scheduler /opt/pi-scheduler/approved-skills
sudo chmod -R u=rwX,g=rX,o= /opt/pi-scheduler/approved-skills
```

Each catalog entry must be a direct child directory with `SKILL.md`:

```text
/opt/pi-scheduler/approved-skills/pdf/SKILL.md
```

The job form lists catalog skills as checkboxes. Jobs store skill IDs, not arbitrary paths. At runtime the scheduler invokes:

```bash
pi --no-skills --skill /opt/pi-scheduler/approved-skills/pdf ...
```

Use `Runtime user default skills` only for advanced cases where you intentionally want Pi to discover whatever skills are available to the runtime user.
````

- [ ] **Step 4: Verify scripts and tests**

Run:

```bash
cd /opt/pi-scheduler
bash -n deploy/setup-runtime-user.sh
.venv/bin/python -m pytest
```

Expected: `bash -n` exits 0 and pytest reports all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /opt/pi-scheduler
git add deploy/setup-runtime-user.sh README.md tests/test_core.py
git commit -m "docs: document approved skills catalog setup"
```

---

### Task 6: Final Verification and Operational Check

**Files:**
- No code changes expected unless verification exposes a defect.

**Interfaces:**
- Consumes all previous tasks.
- Produces evidence that the feature is safe to ship.

- [ ] **Step 1: Run full tests**

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests PASS. Known warning about FastAPI `on_event` deprecation may remain.

- [ ] **Step 2: Verify command generation manually**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python - <<'PY'
from pathlib import Path
from app import config, runner
root = Path('/tmp/pi-scheduler-approved-skills-check')
(root / 'pdf').mkdir(parents=True, exist_ok=True)
(root / 'pdf' / 'SKILL.md').write_text('---\nname: pdf\n---\n', encoding='utf-8')
config.APPROVED_SKILLS_DIR = root
argv, display = runner.build_command({
    'task_prompt': 'check',
    'output_mode': 'summary',
    'session_mode': 'no_session',
    'skills_mode': 'approved',
    'skill_ids': 'pdf',
})
print(argv)
print(display)
assert '--no-skills' in argv
assert '--skill' in argv
assert str((root / 'pdf').resolve()) in argv
PY
```

Expected: output contains `--no-skills --skill /tmp/pi-scheduler-approved-skills-check/pdf`.

- [ ] **Step 3: Verify no free-form skill path UI remains**

Run:

```bash
cd /opt/pi-scheduler
rg -n "skill_paths|Approved skill paths|Approved skill paths only|textarea name=\"skill_paths\"" app/templates app/main.py app/runner.py README.md
```

Expected: remaining `skill_paths` references are limited to DB compatibility/migration notes, not templates or runner command construction.

- [ ] **Step 4: Check git status**

```bash
cd /opt/pi-scheduler
git status --short
```

Expected: clean working tree.

- [ ] **Step 5: Report completion**

Report:

```text
Implemented Approved Skills Catalog.
Verification: pytest passed; bash -n deploy/setup-runtime-user.sh passed; manual command generation confirmed --no-skills --skill <catalog path>.
```

Do not claim completion unless all commands above have passed.

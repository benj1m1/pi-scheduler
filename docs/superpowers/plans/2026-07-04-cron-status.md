# Cron Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show whether the Cron Preview target is actually active in system cron or only a local preview file.

**Architecture:** Add a read-only `app/cron_status.py` inspector that compares generated cron content to the configured target file and classifies status. The existing `/cron` route passes inspection results to the existing template, which displays target, status, checks, warnings, and recommendations.

**Tech Stack:** Python stdlib (`Path`, `pwd`, `subprocess`, `shutil`, `stat`), FastAPI/Jinja2 templates, pytest tests in `tests/test_core.py`.

## Global Constraints

- The status page must be read-only: it must not write cron files, restart services, change permissions, or mutate configuration.
- Local deploy default `PI_SCHEDULER_CRON_FILE=<home>/tmp/pi-agent-jobs` remains unchanged.
- Paths outside `/etc/cron.d` must be reported as `preview_only` and not active in system cron.
- A matching file under `/etc/cron.d` is `active_candidate`, not a full guarantee that every job will run.
- If `systemctl` is unavailable or fails, `cron_service_active` is `None` and the page still renders.

---

## File Structure

- Create `app/cron_status.py`: read-only inspection logic.
- Modify `app/main.py`: import `cron_status` and pass `cron_status.inspect(content)` to `/cron` template context.
- Modify `app/templates/cron_preview.html`: render status/checks/warnings/recommendations.
- Modify `tests/test_core.py`: unit tests for inspector plus route/template status text.
- Modify `README.md`: document Cron Preview status and local preview behavior.

---

### Task 1: Cron Status Inspector

**Files:**
- Create: `app/cron_status.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `app.config.CRON_FILE`
- Produces: `cron_status.inspect(generated_content: str | None = None) -> dict`

- [ ] **Step 1: Write failing inspector tests**

Add near cron tests in `tests/test_core.py`:

```python
def test_cron_status_preview_only_for_non_system_path(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "tmp" / "pi-agent-jobs"
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: None)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "preview_only"
    assert status["is_system_cron_path"] is False
    assert any("outside /etc/cron.d" in warning for warning in status["warnings"])
    assert any("PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs" in item for item in status["recommendations"])


def test_cron_status_active_candidate_for_matching_system_file(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    target.parent.mkdir(parents=True)
    content = "# generated\n* * * * * root echo ok\n"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect(content)

    assert status["status"] == "active_candidate"
    assert status["file_exists"] is True
    assert status["content_matches"] is True
    assert status["cron_service_active"] is True
    assert status["warnings"] == []


def test_cron_status_out_of_sync_for_different_existing_file(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    target.parent.mkdir(parents=True)
    target.write_text("# old\n", encoding="utf-8")
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "out_of_sync"
    assert status["content_matches"] is False
    assert any("does not match" in warning for warning in status["warnings"])


def test_cron_status_missing_for_system_target(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "missing"
    assert status["file_exists"] is False
    assert any("does not exist" in warning for warning in status["warnings"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_cron_status_preview_only_for_non_system_path \
  tests/test_core.py::test_cron_status_active_candidate_for_matching_system_file \
  tests/test_core.py::test_cron_status_out_of_sync_for_different_existing_file \
  tests/test_core.py::test_cron_status_missing_for_system_target -v
```

Expected: FAIL because `app.cron_status` does not exist.

- [ ] **Step 3: Implement inspector**

Create `app/cron_status.py`:

```python
from __future__ import annotations

import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from . import config


SYSTEM_CRON_DIR = Path("/etc/cron.d")


def _is_system_cron_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(SYSTEM_CRON_DIR.resolve())
        return True
    except ValueError:
        return False


def _cron_service_active() -> bool | None:
    if not shutil.which("systemctl"):
        return None
    result = subprocess.run(
        ["systemctl", "is-active", "cron"],
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )
    if result.returncode == 0:
        return True
    if result.stdout.strip() in {"inactive", "failed", "deactivating", "activating"}:
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
        warnings.append("Could not confirm cron service status with systemctl.")

    return {
        "target_file": str(target),
        "is_system_cron_path": is_system_path,
        "file_exists": exists,
        "content_matches": content_matches,
        "file_mode": _file_mode(target),
        "file_owner": _file_owner(target),
        "cron_service_active": service_active,
        "status": status,
        "warnings": warnings,
        "recommendations": recommendations,
    }
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest \
  tests/test_core.py::test_cron_status_preview_only_for_non_system_path \
  tests/test_core.py::test_cron_status_active_candidate_for_matching_system_file \
  tests/test_core.py::test_cron_status_out_of_sync_for_different_existing_file \
  tests/test_core.py::test_cron_status_missing_for_system_target -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /opt/pi-scheduler
git add app/cron_status.py tests/test_core.py
git commit -m "feat: inspect cron target status"
```

---

### Task 2: Route and Template Display

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/cron_preview.html`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `cron_status.inspect(generated_content: str | None) -> dict`
- Produces: `/cron` page displaying status text and warnings

- [ ] **Step 1: Write failing route/template test**

Add to `tests/test_core.py`:

```python
def test_cron_preview_displays_cron_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    monkeypatch.setattr(web.cron_status, "inspect", lambda content: {
        "target_file": str(tmp_path / "pi-agent-jobs"),
        "is_system_cron_path": False,
        "file_exists": False,
        "content_matches": None,
        "file_mode": None,
        "file_owner": None,
        "cron_service_active": None,
        "status": "preview_only",
        "warnings": ["Target file is outside /etc/cron.d. System cron will not read this file automatically."],
        "recommendations": ["For active system cron, set PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs before starting the app."],
    })

    response = web.cron_preview(fake_request("/cron"))
    html = response.body.decode()

    assert "Cron Status" in html
    assert "preview_only" in html
    assert "outside /etc/cron.d" in html
    assert "PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_cron_preview_displays_cron_status -v
```

Expected: FAIL because `app.main` does not import `cron_status` and template does not render the status.

- [ ] **Step 3: Update route**

In `app/main.py`, update import:

```python
from . import config, cron, cron_status, db, pi_models, retention, runner, run_users, runtime_setup, work_window
```

Update `/cron` route:

```python
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
        },
    )
```

- [ ] **Step 4: Update template**

In `app/templates/cron_preview.html`, insert after the section head and before the error block:

```html
  <div class="status-card">
    <h2>Cron Status</h2>
    <dl>
      <dt>Configured target</dt><dd><code>{{ cron_status.target_file }}</code></dd>
      <dt>Status</dt><dd><strong>{{ cron_status.status }}</strong></dd>
      <dt>System cron path</dt><dd>{{ 'yes' if cron_status.is_system_cron_path else 'no' }}</dd>
      <dt>File exists</dt><dd>{{ 'yes' if cron_status.file_exists else 'no' }}</dd>
      <dt>Content matches preview</dt><dd>{{ cron_status.content_matches if cron_status.content_matches is not none else 'unknown' }}</dd>
      <dt>Owner</dt><dd>{{ cron_status.file_owner or 'unknown' }}</dd>
      <dt>Mode</dt><dd>{{ cron_status.file_mode or 'unknown' }}</dd>
      <dt>Cron service active</dt><dd>{{ cron_status.cron_service_active if cron_status.cron_service_active is not none else 'unknown' }}</dd>
    </dl>
    {% if cron_status.warnings %}
    <h3>Warnings</h3>
    <ul>
      {% for warning in cron_status.warnings %}<li>{{ warning }}</li>{% endfor %}
    </ul>
    {% endif %}
    {% if cron_status.recommendations %}
    <h3>Recommended fixes</h3>
    <ul>
      {% for recommendation in cron_status.recommendations %}<li><code>{{ recommendation }}</code></li>{% endfor %}
    </ul>
    {% endif %}
  </div>
```

- [ ] **Step 5: Run targeted test**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_cron_preview_displays_cron_status -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /opt/pi-scheduler
git add app/main.py app/templates/cron_preview.html tests/test_core.py
git commit -m "feat: display cron status in preview"
```

---

### Task 3: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Test: full test suite

**Interfaces:**
- Produces documented explanation of preview-only local cron target and system cron activation path

- [ ] **Step 1: Update README**

In the local quick start section, extend the local cron sentence to say:

```markdown
The Cron Preview page will mark this as `preview_only` because system cron does not read files under `tmp/`.
```

In the API or operational notes section, add:

```markdown
### Cron Status

The `/cron` page shows both the generated cron content and whether the configured target appears active. Files under `/etc/cron.d` with matching generated content are shown as `active_candidate`. Local deploy defaults to `tmp/pi-agent-jobs`, which is shown as `preview_only` because system cron does not read it automatically.

To make local deploy write the system cron file, start it with:

```bash
export PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs
deploy/run-local.sh
```

or use the systemd deployment.
```

- [ ] **Step 2: Run full verification**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
cd /opt/pi-scheduler
git add README.md
git commit -m "docs: explain cron status preview"
```

---

### Task 4: Final Manual Check

**Files:**
- No source changes expected

**Interfaces:**
- Verifies current host status matches expectations

- [ ] **Step 1: Render current status from Python**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python - <<'PY'
from app import cron, cron_status, config
content = cron.render_cron_file()
status = cron_status.inspect(content)
print('target:', status['target_file'])
print('status:', status['status'])
print('warnings:', status['warnings'])
print('recommendations:', status['recommendations'])
PY
```

Expected on local deploy with default temp target: `status: preview_only`. Expected on system deploy with `/etc/cron.d/pi-agent-jobs` matching content: `status: active_candidate`.

- [ ] **Step 2: Final test suite**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 3: Check git status**

Run:

```bash
git status --short
```

Expected: no output.

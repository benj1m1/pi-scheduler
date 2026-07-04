# Runtime User Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate creation and validation of the dedicated `pi-scheduler-agent` runtime user for local Pi Scheduler deployments.

**Architecture:** System-level mutations live in explicit deploy shell scripts; FastAPI startup only performs non-privileged health checks and logs warnings. Local deployment uses a wrapper script that runs setup when possible, exports safe local defaults, then starts Uvicorn.

**Tech Stack:** Bash deploy scripts, Python FastAPI startup hook, stdlib `pwd`/`grp`/`os`/`subprocess`, pytest tests in `tests/test_core.py`.

## Global Constraints

- Default runtime user: `pi-scheduler-agent`.
- Default runtime group: `pi-scheduler`.
- Default scheduler home: `/opt/pi-scheduler`.
- Default source models file: `/root/.pi/agent/models.json`.
- Destination models file: `/home/pi-scheduler-agent/.pi/agent/models.json`.
- FastAPI startup must not create users, change groups, chmod directories, or copy files.
- Missing source models file in deploy setup warns and continues.
- Health-check warnings must not block the app from starting.
- Preserve backward compatibility: global `PI_SCHEDULER_CRON_USER` default remains `root`; local run script defaults it to `pi-scheduler-agent`.

---

## File Structure

- Create `deploy/setup-runtime-user.sh`: idempotent privileged setup for user/group/directories/models copy.
- Create `deploy/run-local.sh`: local deploy wrapper that calls setup when possible and starts Uvicorn with local defaults.
- Create `app/runtime_setup.py`: non-privileged runtime health-check helpers with testable pure functions and one subprocess-based write check.
- Modify `app/config.py`: add runtime setup config values.
- Modify `app/main.py`: call runtime health check during startup and log warnings.
- Modify `deploy/pi-scheduler-web.service`: update default allowlist from `piagent` to `pi-scheduler-agent`.
- Modify `README.md`: document local deploy automation and the new default runtime user.
- Modify `tests/test_core.py`: tests for runtime health check and script syntax/defaults.

---

### Task 1: Runtime Setup Configuration and Health Check Module

**Files:**
- Create: `app/runtime_setup.py`
- Modify: `app/config.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `app.config.RUNTIME_USER`, `RUNTIME_GROUP`, `MODELS_SOURCE_FILE`, `DATA_DIR`, `LOG_DIR`, `LOCK_DIR`, `SCHEDULER_HOME`, `ALLOWED_RUN_USERS`
- Produces:
  - `runtime_setup.expected_models_path(user: str | None = None) -> Path`
  - `runtime_setup.check_runtime_setup() -> list[str]`
  - `runtime_setup.log_runtime_setup_warnings() -> list[str]`

- [ ] **Step 1: Write failing tests for config defaults and missing-user warnings**

Add near the run-user tests in `tests/test_core.py`:

```python
def test_runtime_setup_config_defaults():
    assert config.RUNTIME_USER == "pi-scheduler-agent"
    assert config.RUNTIME_GROUP == "pi-scheduler"
    assert str(config.MODELS_SOURCE_FILE) == "/root/.pi/agent/models.json"


def test_runtime_setup_reports_missing_runtime_user(monkeypatch):
    from app import runtime_setup

    monkeypatch.setattr(config, "RUNTIME_USER", "pi-scheduler-agent")

    def missing_user(name):
        raise KeyError(name)

    monkeypatch.setattr(runtime_setup.pwd, "getpwnam", missing_user)

    warnings = runtime_setup.check_runtime_setup()

    assert any("Runtime user 'pi-scheduler-agent' does not exist" in warning for warning in warnings)
    assert any("setup-runtime-user.sh" in warning for warning in warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_runtime_setup_config_defaults tests/test_core.py::test_runtime_setup_reports_missing_runtime_user -v
```

Expected: FAIL because `config.RUNTIME_USER` and `app.runtime_setup` do not exist.

- [ ] **Step 3: Add config defaults**

In `app/config.py`, add after `ALLOWED_RUN_USERS`:

```python
RUNTIME_USER = os.environ.get("PI_SCHEDULER_RUNTIME_USER", "pi-scheduler-agent")
RUNTIME_GROUP = os.environ.get("PI_SCHEDULER_RUNTIME_GROUP", "pi-scheduler")
MODELS_SOURCE_FILE = Path(os.environ.get("PI_SCHEDULER_MODELS_SOURCE", "/root/.pi/agent/models.json")).expanduser().resolve()
```

- [ ] **Step 4: Create minimal health-check module**

Create `app/runtime_setup.py`:

```python
from __future__ import annotations

import logging
import pwd
from pathlib import Path

from . import config, run_users

LOGGER = logging.getLogger(__name__)
SETUP_HINT = f"Run: sudo {config.SCHEDULER_HOME}/deploy/setup-runtime-user.sh"


def expected_models_path(user: str | None = None) -> Path:
    runtime_user = user or config.RUNTIME_USER
    try:
        home = Path(pwd.getpwnam(runtime_user).pw_dir)
    except KeyError:
        home = Path("/home") / runtime_user
    return home / ".pi" / "agent" / "models.json"


def check_runtime_setup() -> list[str]:
    warnings: list[str] = []
    try:
        pwd.getpwnam(config.RUNTIME_USER)
    except KeyError:
        warnings.append(f"Runtime user '{config.RUNTIME_USER}' does not exist. {SETUP_HINT}")
        return warnings

    allowed = run_users.allowed_run_users()
    if config.RUNTIME_USER not in allowed:
        warnings.append(
            f"Runtime user '{config.RUNTIME_USER}' is not in PI_SCHEDULER_ALLOWED_RUN_USERS ({', '.join(allowed)})."
        )

    models_path = expected_models_path(config.RUNTIME_USER)
    if not models_path.exists():
        warnings.append(f"Runtime models file is missing at {models_path}. {SETUP_HINT}")
    else:
        owner = pwd.getpwuid(models_path.stat().st_uid).pw_name
        if owner != config.RUNTIME_USER:
            warnings.append(f"Runtime models file {models_path} is owned by {owner}, expected {config.RUNTIME_USER}.")

    return warnings


def log_runtime_setup_warnings() -> list[str]:
    warnings = check_runtime_setup()
    for warning in warnings:
        LOGGER.warning("Runtime setup warning: %s", warning)
    return warnings
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_runtime_setup_config_defaults tests/test_core.py::test_runtime_setup_reports_missing_runtime_user -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /opt/pi-scheduler
git add app/config.py app/runtime_setup.py tests/test_core.py
git commit -m "feat: add runtime setup health checks"
```

---

### Task 2: Complete Health Check Coverage and Startup Integration

**Files:**
- Modify: `app/runtime_setup.py`
- Modify: `app/main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `runtime_setup.log_runtime_setup_warnings() -> list[str]`
- Produces: startup invokes `runtime_setup.log_runtime_setup_warnings()` without blocking startup

- [ ] **Step 1: Write failing tests for valid setup and startup invocation**

Add to `tests/test_core.py`:

```python
def test_runtime_setup_accepts_valid_mocked_setup(tmp_path, monkeypatch):
    from app import runtime_setup

    models = tmp_path / "home" / "pi-scheduler-agent" / ".pi" / "agent" / "models.json"
    models.parent.mkdir(parents=True)
    models.write_text('{"providers": []}', encoding="utf-8")

    class Pw:
        pw_dir = str(tmp_path / "home" / "pi-scheduler-agent")

    monkeypatch.setattr(config, "RUNTIME_USER", "pi-scheduler-agent")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,pi-scheduler-agent", raising=False)
    monkeypatch.setattr(runtime_setup.pwd, "getpwnam", lambda name: Pw())
    monkeypatch.setattr(runtime_setup.pwd, "getpwuid", lambda uid: type("Owner", (), {"pw_name": "pi-scheduler-agent"})())

    assert runtime_setup.check_runtime_setup() == []


def test_startup_logs_runtime_setup_warnings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "cron")
    monkeypatch.setattr(web.runtime_setup, "log_runtime_setup_warnings", lambda: calls.append("checked") or [])

    web.startup()

    assert calls == ["checked"]
```

- [ ] **Step 2: Run tests to verify startup test fails**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_runtime_setup_accepts_valid_mocked_setup tests/test_core.py::test_startup_logs_runtime_setup_warnings -v
```

Expected: first test may PASS after Task 1; startup test FAILS because `app.main` does not import/call `runtime_setup`.

- [ ] **Step 3: Integrate startup health check**

In `app/main.py`, change import:

```python
from . import config, cron, db, pi_models, retention, runner, run_users, runtime_setup, work_window
```

Then update `startup()`:

```python
@app.on_event("startup")
def startup() -> None:
    db.init_db()
    retention.cleanup_old_logs()
    runtime_setup.log_runtime_setup_warnings()
    cron.write_cron_file()
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_runtime_setup_accepts_valid_mocked_setup tests/test_core.py::test_startup_logs_runtime_setup_warnings tests/test_core.py::test_startup_syncs_existing_jobs_to_cron_file -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /opt/pi-scheduler
git add app/runtime_setup.py app/main.py tests/test_core.py
git commit -m "feat: warn about incomplete runtime user setup"
```

---

### Task 3: Privileged Runtime User Setup Script

**Files:**
- Create: `deploy/setup-runtime-user.sh`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces executable script `deploy/setup-runtime-user.sh` supporting `--user`, `--group`, `--home`, `--models-file`, `--help`

- [ ] **Step 1: Write failing script syntax and content tests**

Add to `tests/test_core.py`:

```python
def test_setup_runtime_user_script_syntax_and_defaults():
    script = Path("/opt/pi-scheduler/deploy/setup-runtime-user.sh")
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert 'RUNTIME_USER="pi-scheduler-agent"' in content
    assert 'RUNTIME_GROUP="pi-scheduler"' in content
    assert 'MODELS_FILE="/root/.pi/agent/models.json"' in content
    assert "useradd" in content
    assert "usermod -aG" in content
    assert "chgrp -R" in content
    assert "chmod -R g+rwX" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_setup_runtime_user_script_syntax_and_defaults -v
```

Expected: FAIL because the script does not exist.

- [ ] **Step 3: Create setup script**

Create `deploy/setup-runtime-user.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RUNTIME_USER="pi-scheduler-agent"
RUNTIME_GROUP="pi-scheduler"
SCHEDULER_HOME="/opt/pi-scheduler"
MODELS_FILE="/root/.pi/agent/models.json"

usage() {
  cat <<'USAGE'
Usage: setup-runtime-user.sh [options]

Options:
  --user NAME          Runtime Linux user (default: pi-scheduler-agent)
  --group NAME         Runtime group (default: pi-scheduler)
  --home PATH          Scheduler home (default: /opt/pi-scheduler)
  --models-file PATH   Source models.json (default: /root/.pi/agent/models.json)
  --help               Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      RUNTIME_USER="$2"
      shift 2
      ;;
    --group)
      RUNTIME_GROUP="$2"
      shift 2
      ;;
    --home)
      SCHEDULER_HOME="$2"
      shift 2
      ;;
    --models-file)
      MODELS_FILE="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup-runtime-user.sh must run as root. Try: sudo $0" >&2
  exit 1
fi

if ! getent group "${RUNTIME_GROUP}" >/dev/null; then
  groupadd "${RUNTIME_GROUP}"
fi

if ! id -u "${RUNTIME_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${RUNTIME_USER}"
fi

usermod -aG "${RUNTIME_GROUP}" "${RUNTIME_USER}"

for dir in "${SCHEDULER_HOME}/data" "${SCHEDULER_HOME}/logs" "${SCHEDULER_HOME}/locks" "${SCHEDULER_HOME}/tmp"; do
  mkdir -p "${dir}"
done

chgrp -R "${RUNTIME_GROUP}" \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp"
chmod -R g+rwX \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp"
find \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp" \
  -type d -exec chmod g+s {} \;

USER_HOME="$(getent passwd "${RUNTIME_USER}" | cut -d: -f6)"
PI_DIR="${USER_HOME}/.pi"
AGENT_DIR="${PI_DIR}/agent"
mkdir -p "${AGENT_DIR}"
chown "${RUNTIME_USER}:${RUNTIME_USER}" "${PI_DIR}" "${AGENT_DIR}"
chmod 700 "${PI_DIR}" "${AGENT_DIR}"

if [[ -f "${MODELS_FILE}" ]]; then
  cp "${MODELS_FILE}" "${AGENT_DIR}/models.json"
  chown "${RUNTIME_USER}:${RUNTIME_USER}" "${AGENT_DIR}/models.json"
  chmod 600 "${AGENT_DIR}/models.json"
  echo "Copied ${MODELS_FILE} to ${AGENT_DIR}/models.json"
else
  echo "Warning: source models file not found: ${MODELS_FILE}" >&2
  echo "Runtime user was created, but Pi model config was not copied." >&2
fi

echo "Runtime setup complete for ${RUNTIME_USER}."
```

- [ ] **Step 4: Make script executable and verify syntax**

Run:

```bash
cd /opt/pi-scheduler
chmod +x deploy/setup-runtime-user.sh
bash -n deploy/setup-runtime-user.sh
```

Expected: no output and exit 0.

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_setup_runtime_user_script_syntax_and_defaults -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /opt/pi-scheduler
git add deploy/setup-runtime-user.sh tests/test_core.py
git commit -m "feat: add runtime user setup script"
```

---

### Task 4: Local Deployment Wrapper Script

**Files:**
- Create: `deploy/run-local.sh`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces executable script `deploy/run-local.sh`
- Script sets default env: `PI_SCHEDULER_CRON_USER=pi-scheduler-agent`, `PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent`

- [ ] **Step 1: Write failing local script test**

Add to `tests/test_core.py`:

```python
def test_run_local_script_syntax_and_defaults():
    script = Path("/opt/pi-scheduler/deploy/run-local.sh")
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert "deploy/setup-runtime-user.sh" in content
    assert 'PI_SCHEDULER_CRON_USER="${PI_SCHEDULER_CRON_USER:-pi-scheduler-agent}"' in content
    assert 'PI_SCHEDULER_ALLOWED_RUN_USERS="${PI_SCHEDULER_ALLOWED_RUN_USERS:-root,pi-scheduler-agent}"' in content
    assert "uvicorn app.main:app" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_run_local_script_syntax_and_defaults -v
```

Expected: FAIL because `deploy/run-local.sh` does not exist.

- [ ] **Step 3: Create local run script**

Create `deploy/run-local.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
SCHEDULER_HOME="${PI_SCHEDULER_HOME:-${REPO_ROOT}}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
SETUP_SCRIPT="${SCRIPT_DIR}/setup-runtime-user.sh"

if [[ "${EUID}" -eq 0 ]]; then
  "${SETUP_SCRIPT}" --home "${SCHEDULER_HOME}"
elif command -v sudo >/dev/null 2>&1; then
  sudo "${SETUP_SCRIPT}" --home "${SCHEDULER_HOME}"
else
  echo "Warning: sudo is not available; skipping runtime user setup." >&2
  echo "Run as root: ${SETUP_SCRIPT} --home ${SCHEDULER_HOME}" >&2
fi

export PI_SCHEDULER_HOME="${SCHEDULER_HOME}"
export PI_SCHEDULER_CRON_FILE="${PI_SCHEDULER_CRON_FILE:-${SCHEDULER_HOME}/tmp/pi-agent-jobs}"
export PI_SCHEDULER_CRON_USER="${PI_SCHEDULER_CRON_USER:-pi-scheduler-agent}"
export PI_SCHEDULER_ALLOWED_RUN_USERS="${PI_SCHEDULER_ALLOWED_RUN_USERS:-root,pi-scheduler-agent}"

cd "${SCHEDULER_HOME}"
exec "${SCHEDULER_HOME}/.venv/bin/uvicorn" app.main:app --host "${HOST}" --port "${PORT}"
```

- [ ] **Step 4: Make script executable and verify syntax**

Run:

```bash
cd /opt/pi-scheduler
chmod +x deploy/run-local.sh
bash -n deploy/run-local.sh
```

Expected: no output and exit 0.

- [ ] **Step 5: Run targeted test**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest tests/test_core.py::test_run_local_script_syntax_and_defaults -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /opt/pi-scheduler
git add deploy/run-local.sh tests/test_core.py
git commit -m "feat: add local deployment wrapper"
```

---

### Task 5: Documentation and Service Defaults

**Files:**
- Modify: `README.md`
- Modify: `deploy/pi-scheduler-web.service`
- Test: full test suite plus script syntax checks

**Interfaces:**
- Produces documented local deployment command and updated default allowlist in service file

- [ ] **Step 1: Update service allowlist**

In `deploy/pi-scheduler-web.service`, replace:

```ini
Environment=PI_SCHEDULER_ALLOWED_RUN_USERS=root,piagent
```

with:

```ini
Environment=PI_SCHEDULER_CRON_USER=pi-scheduler-agent
Environment=PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent
```

- [ ] **Step 2: Update README quick start**

In `README.md` Quick Start, replace the manual `export ... uvicorn ...` local startup block with:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export PI_SCHEDULER_PASSWORD='set-a-real-password'
deploy/run-local.sh
```

Keep a short note that `deploy/run-local.sh` prepares `pi-scheduler-agent`, grants runtime directory permissions, copies `/root/.pi/agent/models.json` when available, and writes local cron output to `tmp/pi-agent-jobs`.

- [ ] **Step 3: Update README run-user section**

Replace references to `piagent` in the run-user setup example with `pi-scheduler-agent`. Add:

```markdown
For local deploy, the recommended command is:

```bash
sudo deploy/setup-runtime-user.sh
```

It creates `pi-scheduler-agent`, grants scheduler runtime directory permissions, and copies `/root/.pi/agent/models.json` to `/home/pi-scheduler-agent/.pi/agent/models.json` when the source exists.
```

- [ ] **Step 4: Run verification**

Run:

```bash
cd /opt/pi-scheduler
bash -n deploy/setup-runtime-user.sh
bash -n deploy/run-local.sh
.venv/bin/python -m pytest
```

Expected: both `bash -n` commands exit 0; pytest reports all tests passing.

- [ ] **Step 5: Commit**

```bash
cd /opt/pi-scheduler
git add README.md deploy/pi-scheduler-web.service
git commit -m "docs: document automated runtime user setup"
```

---

### Task 6: Manual Setup Verification on This Host

**Files:**
- No source changes expected

**Interfaces:**
- Verifies deploy script works on the local host with the real `pi-scheduler-agent` user

- [ ] **Step 1: Run setup script**

Run:

```bash
cd /opt/pi-scheduler
sudo deploy/setup-runtime-user.sh
```

Expected: exits 0 and prints `Runtime setup complete for pi-scheduler-agent.` Missing `/root/.pi/agent/models.json` only emits a warning.

- [ ] **Step 2: Verify user, group, directory writes**

Run:

```bash
id pi-scheduler-agent
getent group pi-scheduler
for d in /opt/pi-scheduler/data /opt/pi-scheduler/logs /opt/pi-scheduler/locks /opt/pi-scheduler/tmp; do
  sudo -u pi-scheduler-agent bash -lc "touch '$d/.write-test' && rm '$d/.write-test'"
  echo "ok: pi-scheduler-agent can write $d"
done
```

Expected: `id` succeeds, group includes `pi-scheduler-agent`, and every directory prints `ok`.

- [ ] **Step 3: Verify copied model config if source exists**

Run:

```bash
if [ -f /root/.pi/agent/models.json ]; then
  sudo -u pi-scheduler-agent test -r /home/pi-scheduler-agent/.pi/agent/models.json
  stat -c '%U %G %a %n' /home/pi-scheduler-agent/.pi/agent/models.json
else
  echo 'source models file does not exist; copy verification skipped'
fi
```

Expected if source exists: owner user/group are `pi-scheduler-agent pi-scheduler-agent`; mode is `600` or stricter readable by owner only.

- [ ] **Step 4: Final full test suite**

Run:

```bash
cd /opt/pi-scheduler
.venv/bin/python -m pytest
```

Expected: all tests pass.

- [ ] **Step 5: Commit only if manual verification required doc/test changes**

If no files changed, do not commit. If README or tests required corrections, commit with:

```bash
git add README.md tests/test_core.py deploy/*.sh
git commit -m "fix: align runtime setup automation"
```

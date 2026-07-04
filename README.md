# Pi Scheduler

Scheduling and execution platform for Pi CLI agents — define jobs and job groups with interval schedules, model selection, work windows, and run them via system cron with full logging, retention, and a web dashboard.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│ FastAPI web UI (uvicorn)    │  /etc/cron.d/pi-       │
│                             │  agent-jobs             │
│ ┌─────────┐  ┌───────────┐  │                        │
│ │ jobs    │  │ groups    │──┼── invokes ────────────┐│
│ │ CRUD    │  │ CRUD      │  │  bin/pi-job-runner    ││
│ └────┬────┘  └─────┬─────┘  │   --job-id <id>       ││
│      │             │         │   --group-id <id>     ││
│      ▼             ▼         │                       ││
│ ┌─────────────────────────┐  │          │            ││
│ │   app/runner.py         │◄─┼──────────┘            ││
│ │   execute_job()         │  │                       ││
│ │   run_group()           │  │  ┌─────────────────┐  ││
│ └───────────┬─────────────┘  │  │ pi CLI binary   │  ││
│             │                │  │ + models.json   │  ││
│      ┌──────┴──────┐        │  └─────────────────┘  ││
│      ▼             ▼        │                        ││
│  SQLite DB    logs/jobs/    │                        ││
│  data/        locks/        │                        ││
└──────────────────────────────────────────────────────┘
```

- **Web panel** — FastAPI + Jinja2 + Basic Auth, single CSS file, no build step.
- **Runner** — Standalone Python CLI invoked by cron; acquires file locks, runs `pi`, writes logs.
- **Storage** — SQLite (`data/pi-scheduler.sqlite3`), structured log files under `logs/jobs/<job-id>/runs/`.
- **Scheduling** — App regenerates `/etc/cron.d/pi-agent-jobs` on every mutation (create/edit/toggle/delete/startup).

## Quick Start (Local Development)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export PI_SCHEDULER_PASSWORD='set-a-real-password'
deploy/run-local.sh
```

Open `http://127.0.0.1:8080`, log in with `admin` / your password.  
Default password is `pi-scheduler` if the env var is not set — change it before network exposure.

`deploy/run-local.sh` prepares the dedicated runtime user `pi-scheduler-agent` when it can use root/sudo, grants scheduler runtime directory permissions, copies `/root/.pi/agent/models.json` when available, sets local run-user defaults, and writes local cron output to `tmp/pi-agent-jobs` instead of `/etc/cron.d`. The Cron Preview page will mark this as `preview_only` because system cron does not read files under `tmp/`.

## Install on Ubuntu

These steps also work inside an LXC container.

Before installing Pi Scheduler, verify the Pi CLI works from cron's minimal `PATH`:

```bash
command -v pi
command -v node
pi --version
env -i PATH=/usr/local/bin:/usr/bin:/bin pi --version
```

If the final check hangs or fails, fix the Pi CLI installation first.

```bash
sudo apt update
sudo apt install -y git python3-venv cron tmux
sudo git clone https://github.com/benj1m1/pi-scheduler.git /opt/pi-scheduler
cd /opt/pi-scheduler
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chmod +x /opt/pi-scheduler/bin/pi-job-runner
sudo deploy/setup-runtime-user.sh
sudo cp deploy/pi-scheduler-web.service /etc/systemd/system/pi-scheduler-web.service
```

Edit the service file to set a real `PI_SCHEDULER_PASSWORD` and adjust `--host` if needed, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-scheduler-web.service
```

Ensure the system cron daemon is running:

```bash
sudo systemctl status cron --no-pager
```

## Jobs

Each job represents one Pi agent invocation with a prompt, schedule, and execution configuration.

### Scheduling

Jobs use simple interval controls — no raw cron syntax in the form:

| Form input | Cron expression |
|---|---|
| Every 1 minute | `* * * * *` |
| Every 5 minutes | `*/5 * * * *` |
| Every 1 hour | `0 * * * *` |
| Every 2 hours | `0 */2 * * *` |

The job detail page shows the generated cron expression for verification.

### Model Selection

The job form lists provider/model options from `~/.pi/agent/models.json` (read-only). The runner validates the selected pair before each run — if the config has changed or is invalid, the run is recorded as failed without invoking `pi`.

Command shape when a model is selected:

```bash
pi --no-session --name 'pi-scheduler: <job name>' -p --provider <provider> --model <model> "<prompt>"
```

When no model is selected, Pi uses its default behavior:

```bash
pi --no-session --name 'pi-scheduler: <job name>' -p "<prompt>"
```

### Run Output, Sessions & Tools

| Setting | Options |
|---|---|
| Output mode | **Summary only** — `pi -p`, stores stdout as transcript |
| | **Detailed event log** — `pi --mode json`, stores raw JSONL + rendered transcript |
| Session | **Do not save** — appends `--no-session` |
| | **Save** — sessions named `pi-scheduler: <job name>` |
| Tool access | **Full tools** — Pi defaults |
| | **Read-only** — `--tools read,grep,find,ls` (no `bash`) |
| | **No tools** — `--no-tools` |

Defaults for new jobs: Summary only, Do not save session, Full tools.  
Legacy databases (pre-output/session/tool settings) migrate existing jobs to Detailed event log, Save session, Full tools.

### Work Windows

Work windows use Beijing time (HH:MM). Leave both start and end as `All day` for no limit. Overnight windows are supported (e.g., `22:00 – 06:00`).

- Cron runs outside the work window exit without invoking `pi` and without creating a run record.
- Manual "Run Now" bypasses both the enabled toggle and the work window check.

### Run Users

Jobs and groups can optionally specify a Linux user to run as. Leave the field blank to use `PI_SCHEDULER_CRON_USER`.

Standalone jobs use their own run user. Groups use the group run user for the whole pipeline; member job run users are ignored during group execution.

For safety, non-default users must be allowlisted:

```bash
PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent
```

For local deploy, the recommended setup command is:

```bash
sudo deploy/setup-runtime-user.sh
```

It creates `pi-scheduler-agent`, creates/reuses the `pi-scheduler` group, grants scheduler runtime directory permissions, and copies `/root/.pi/agent/models.json` to `/home/pi-scheduler-agent/.pi/agent/models.json` when the source exists.

The run user needs Pi CLI credentials and model configuration under its own home directory, for example `/home/pi-scheduler-agent/.pi/agent/`.

Manual Run Now uses the configured run user by launching `bin/pi-job-runner --source manual`. If the web service runs as root, it switches users with `sudo -u` or `runuser`. If it cannot switch users, the manual run is rejected instead of running as the wrong user.

### Time Display

Timestamps are stored in UTC (`2026-06-27T14:00:13Z`) and displayed as Beijing time (`2026-06-27 22:00:13 Beijing`).

## Job Groups

Groups execute multiple jobs sequentially as a pipeline. Each member is an existing job — groups do not define new jobs, they chain existing ones.

### Key Behaviors

- Members execute in the configured order (drag-to-reorder in the form).
- Group-enabled state, schedule, and work window control whether a group starts.
- Member job enabled state and work window are **ignored** during group execution — the pipeline runs deterministically.
- Member job **overlap prevention** still applies: a job already running (standalone or in another group) is skipped with `skipped_overlap` status.
- The same group cannot run concurrently (group-level lock at `locks/groups/<group-id>.lock`).

### Failure Policy

- **Stop on first failure** (default): the group stops after any member fails, times out, or is skipped. Remaining members are marked `skipped`.
- **Continue after failed steps**: later members still run after a failed/timed-out/skipped step. The final group run is still marked `failed` or `timeout` if any step failed or timed out.

### Group Run Visibility

- Group detail page shows the member chain and recent group runs.
- Group run detail shows each step's status with links to individual job run logs.
- Homepage group cards show the latest group run status badge and duration.
- Running group run detail pages auto-refresh every 5 seconds.

## Run Logs

Each job run writes files under `logs/jobs/<job-id>/runs/`:

| File | Description |
|---|---|
| `<run-id>.stdout.log` | Run transcript (stdout or rendered Pi events) |
| `<run-id>.stderr.log` | `pi` CLI stderr (often empty on success) |
| `<run-id>.pi-events.jsonl` | Raw Pi JSONL event stream (Detailed event log only) |

Daily summary JSONL files at `logs/jobs/<job-id>/<yyyy-mm-dd>.jsonl` provide quick metadata access.

The `/logs` page supports filtering by job, group, source, status, and date range, with pagination and cleanup controls.

## Log Retention & Cleanup

- Automatic cleanup on startup and after each run, governed by `PI_SCHEDULER_LOG_RETENTION_DAYS` (default 30).
- Manual cleanup via the `/logs` page: delete runs older than N days, or delete all completed runs.

### Cron Status

The `/cron` page shows both the generated cron content and whether the configured target appears active. Files under `/etc/cron.d` with matching generated content are shown as `active_candidate`. Local deploy defaults to `tmp/pi-agent-jobs`, which is shown as `preview_only` because system cron does not read it automatically.

To make local deploy write the system cron file, start it with:

```bash
export PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs
deploy/run-local.sh
```

or use the systemd deployment.

## Important Paths

```
data/pi-scheduler.sqlite3          SQLite database
logs/jobs/<job-id>/runs/           Per-job run logs
locks/<job-id>.lock                Per-job overlap lock (flock)
locks/groups/<group-id>.lock       Per-group concurrency lock
/etc/cron.d/pi-agent-jobs          Managed cron file
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PI_SCHEDULER_HOME` | `/opt/pi-scheduler` | Base directory |
| `PI_SCHEDULER_DB` | `<home>/data/pi-scheduler.sqlite3` | SQLite path |
| `PI_SCHEDULER_DATA_DIR` | `<home>/data` | Data directory |
| `PI_SCHEDULER_LOG_DIR` | `<home>/logs` | Log directory |
| `PI_SCHEDULER_LOCK_DIR` | `<home>/locks` | Lock directory |
| `PI_SCHEDULER_CRON_FILE` | `/etc/cron.d/pi-agent-jobs` | Managed cron file |
| `PI_SCHEDULER_RUNNER` | `<home>/bin/pi-job-runner` | Runner path in cron entries |
| `PI_BINARY` | `pi` | Pi CLI binary |
| `PI_MODELS_FILE` | `~/.pi/agent/models.json` | Read-only model config |
| `PI_SCHEDULER_USERNAME` | `admin` | Basic Auth username |
| `PI_SCHEDULER_PASSWORD` | `pi-scheduler` | Basic Auth password |
| `PI_SCHEDULER_CRON_USER` | `root` | Default user in cron entries. `deploy/run-local.sh` defaults this to `pi-scheduler-agent`. |
| `PI_SCHEDULER_ALLOWED_RUN_USERS` | `<cron user>` | Comma-separated allowlist for per-job/per-group Linux run users. Empty means only `PI_SCHEDULER_CRON_USER` is allowed. |
| `PI_SCHEDULER_RUNTIME_USER` | `pi-scheduler-agent` | Dedicated runtime user expected by setup scripts and startup health checks |
| `PI_SCHEDULER_RUNTIME_GROUP` | `pi-scheduler` | Runtime group granted write access to scheduler data/log/lock/tmp directories |
| `PI_SCHEDULER_MODELS_SOURCE` | `/root/.pi/agent/models.json` | Source model config copied by `deploy/setup-runtime-user.sh` |
| `PI_SCHEDULER_LOG_RETENTION_DAYS` | `30` | Auto-cleanup cutoff |

## Database Schema

Six SQLite tables with inline, idempotent migrations in `init_db()`:

| Table | Purpose |
|---|---|
| `jobs` | Job definitions (name, prompt, cron, output/session/tool mode, timeout, work window) |
| `runs` | Individual job execution records (status, timing, exit code, log paths, group association) |
| `job_groups` | Group definitions (name, cron, failure policy, work window) |
| `job_group_members` | Ordered member jobs per group |
| `group_runs` | Group execution records |
| `group_run_steps` | Per-step status within a group run |

All jobs and groups support soft delete. Schema changes are backward-compatible ALTER TABLE additions in `db.init_db()` — no standalone migration files.

## Running Tests

```bash
.venv/bin/python -m pytest
```

Tests use in-memory/tmp SQLite databases, monkeypatched config paths, and cover DB, cron, runner, web layer, retention, and job group workflows. No external test dependencies beyond pytest.

## API Routes

All routes require Basic Auth.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard: jobs + groups with latest run status |
| GET | `/jobs/new` | Create job form |
| POST | `/jobs` | Create job |
| GET | `/jobs/{id}` | Job detail with run history (live-polling) |
| GET | `/jobs/{id}/edit` | Edit job form |
| POST | `/jobs/{id}` | Update job |
| POST | `/jobs/{id}/toggle` | Enable/disable job |
| POST | `/jobs/{id}/delete` | Soft-delete (blocked if referenced by a group) |
| POST | `/jobs/{id}/run` | Background: manual run |
| GET | `/groups/new` | Create group form |
| POST | `/groups` | Create group |
| GET | `/groups/{id}` | Group detail + run history |
| GET | `/groups/{id}/edit` | Edit group form |
| POST | `/groups/{id}` | Update group |
| POST | `/groups/{id}/toggle` | Enable/disable group |
| POST | `/groups/{id}/delete` | Soft-delete group |
| POST | `/groups/{id}/run` | Background: manual group run |
| GET | `/groups/{gid}/runs/{rid}` | Group run step detail (auto-refresh if running) |
| GET | `/group-runs/{rid}` | Convenience redirect |
| GET | `/runs/{rid}` | Single run detail (stdout, stderr, JSONL) |
| GET | `/logs` | Filterable/paginated log viewer |
| POST | `/logs/cleanup` | Manual log cleanup |
| GET | `/cron` | Read-only cron file preview |
| GET | `/maintenance/logs` | Legacy redirect → `/logs` |

## Notes

- The web app only manages `/etc/cron.d/pi-agent-jobs`. It does not touch user crontabs or other system cron files.
- If scheduled runs don't appear, verify the cron file exists and `cron.service` is active.
- Pi Scheduler, the Pi CLI, `~/.pi/agent/models.json`, and `~/.pi/agent/skills/` should belong to the same runtime user.
- Job overlap prevention is always enforced (`prevent_overlap` forced to 1).

## Dependencies

- Python ≥ 3.10
- FastAPI ≥ 0.111, Uvicorn ≥ 0.30, Jinja2 ≥ 3.1
- python-multipart ≥ 0.0.9, croniter ≥ 2.0
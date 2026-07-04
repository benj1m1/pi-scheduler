# Pi Scheduler

A small FastAPI scheduler for Pi CLI agents. Define jobs and job groups, run them through system cron, execute them under controlled Linux users, and review logs, governance metadata, and audit history from a web UI.

## What it does

- **Schedules Pi CLI jobs** via a managed cron file, defaulting to `/etc/cron.d/pi-agent-jobs`.
- **Runs jobs and groups safely** with file locks, timeouts, work windows, per-job/per-group run users, and an allowlist for non-default users.
- **Defaults to no skills** (`pi --no-skills`) and supports an approved skills catalog by skill ID.
- **Supports governance controls**: global pause, owner/purpose/scope/environment/risk/expiration metadata, and audit logging.
- **Provides operational UI**: compact dashboard, progressive job/group forms, log viewer, audit activity feed, and read-only cron status.

## Architecture

- **Web UI**: FastAPI + Jinja2 + Basic Auth, no frontend build step.
- **Runner**: `bin/pi-job-runner`, invoked by cron or manual Run Now.
- **Storage**: SQLite at `data/pi-scheduler.sqlite3`.
- **Logs**: structured run files under `logs/jobs/<job-id>/runs/`.
- **Cron**: regenerated after mutations and on startup.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export PI_SCHEDULER_PASSWORD='set-a-real-password'
deploy/run-local.sh
```

Open `http://127.0.0.1:8080` and log in with `admin` / your password.

`deploy/run-local.sh` prepares the `pi-scheduler-agent` runtime user, grants runtime directory permissions, copies `/root/.pi/agent/models.json` when available, and writes `/etc/cron.d/pi-agent-jobs` so automatic jobs run locally. It restarts with `sudo -E` when needed because updating `/etc/cron.d` requires privileges.

## Install on Ubuntu

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
sudo systemctl daemon-reload
sudo systemctl enable --now pi-scheduler-web.service
```

Before relying on cron, verify the Pi CLI works in cron's minimal environment:

```bash
env -i PATH=/usr/local/bin:/usr/bin:/bin pi --version
sudo systemctl status cron --no-pager
```

Edit `/etc/systemd/system/pi-scheduler-web.service` to set a real `PI_SCHEDULER_PASSWORD` before exposing the service.

## Web UI

- **Dashboard `/`**: compact job/group cards with owner, environment, risk, run user, schedule, next check, and latest status.
- **Header Scheduler control**: compact global pause/resume entry point.
- **Job/group forms**: basic setup, schedule, and governance first; advanced execution settings are collapsed.
- **Logs `/logs`**: filter runs, view stdout/stderr/JSONL, and clean old runs.
- **Audit `/audit`**: governance activity feed for admin changes, manual run requests, and pause/resume events.
- **Cron `/cron`**: read-only preview and activation status for the managed cron file.

## Jobs and groups

A **job** is one Pi CLI invocation with a prompt, model selection, schedule, work window, run user, skills policy, timeout, and governance metadata.

A **group** runs existing jobs sequentially as a pipeline. Group schedule, work window, run user, and failure policy control the whole chain. Member job enabled state and member work windows are ignored during group execution, but member overlap locks still apply.

### Scheduling and work windows

Jobs and groups use interval controls instead of raw cron syntax:

| Form input | Cron expression |
|---|---|
| Every 1 minute | `* * * * *` |
| Every 5 minutes | `*/5 * * * *` |
| Every 1 hour | `0 * * * *` |
| Every 2 hours | `0 */2 * * *` |

Work windows use Beijing time. Cron runs outside the window exit before invoking Pi. Manual Run Now bypasses schedule and work-window checks, but not pause/expiration/run-user validation.

### Execution defaults

New jobs default to:

- Summary output (`pi -p`)
- No saved Pi session (`--no-session`)
- Full tools
- No skills (`--no-skills`)
- Overlap prevention enabled

When a provider/model is selected, the runner validates it against `~/.pi/agent/models.json` before invoking Pi.

### Run users

Jobs and groups can specify a Linux user. Blank means `PI_SCHEDULER_CRON_USER`; local deploy defaults this to `pi-scheduler-agent`.

Non-default users must be allowlisted:

```bash
PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent
```

Manual Run Now uses the configured run user by launching `bin/pi-job-runner --source manual`. If the web service cannot switch to the target user, the run is rejected instead of running as the wrong user.

### Skills policy

Jobs run with `--no-skills` by default. To enable reviewed skills, install them under the approved catalog:

```bash
sudo install -d -o root -g pi-scheduler -m 0750 /opt/pi-scheduler/approved-skills
sudo cp -a /source/pdf /opt/pi-scheduler/approved-skills/pdf
sudo chown -R root:pi-scheduler /opt/pi-scheduler/approved-skills
sudo chmod -R u=rwX,g=rX,o= /opt/pi-scheduler/approved-skills
```

Each catalog entry is a direct child directory with `SKILL.md`, for example:

```text
/opt/pi-scheduler/approved-skills/pdf/SKILL.md
```

Jobs store skill IDs, not arbitrary paths. Missing approved skills fail safely at runtime.

## Governance

- **Global pause** blocks cron rendering/execution, manual Run Now, and direct/stale runner invocation.
- **Metadata** on jobs and groups: owner, purpose, scope, environment, risk level, and expiration date.
- **Expiration** uses `YYYY-MM-DD` Beijing-date semantics. Expired jobs/groups are blocked but not disabled.
- **Audit log** records admin changes, manual run requests, and pause/resume events with expandable before/after details.

## Logs and retention

Each run writes files under `logs/jobs/<job-id>/runs/`:

| File | Description |
|---|---|
| `<run-id>.stdout.log` | Transcript or rendered Pi events |
| `<run-id>.stderr.log` | Pi CLI stderr |
| `<run-id>.pi-events.jsonl` | Raw Pi JSONL events for detailed mode |

Daily summary JSONL files live under `logs/jobs/<job-id>/<yyyy-mm-dd>.jsonl`.

Retention is controlled by `PI_SCHEDULER_LOG_RETENTION_DAYS` (default `30`). Cleanup runs on startup and after each run; `/logs` also has manual cleanup controls.

## Key configuration

| Variable | Default | Description |
|---|---|---|
| `PI_SCHEDULER_HOME` | `/opt/pi-scheduler` | Base directory |
| `PI_SCHEDULER_DB` | `<home>/data/pi-scheduler.sqlite3` | SQLite path |
| `PI_SCHEDULER_CRON_FILE` | `/etc/cron.d/pi-agent-jobs` | Managed cron file |
| `PI_SCHEDULER_RUNNER` | `<home>/bin/pi-job-runner` | Runner path used in cron |
| `PI_BINARY` | `pi` | Pi CLI binary |
| `PI_MODELS_FILE` | `~/.pi/agent/models.json` | Read-only model config |
| `PI_SCHEDULER_USERNAME` | `admin` | Basic Auth username |
| `PI_SCHEDULER_PASSWORD` | `pi-scheduler` | Basic Auth password; change this |
| `PI_SCHEDULER_CRON_USER` | `root` | Default execution user; local deploy sets `pi-scheduler-agent` |
| `PI_SCHEDULER_ALLOWED_RUN_USERS` | `<cron user>` | Comma-separated run-user allowlist |
| `PI_SCHEDULER_APPROVED_SKILLS_DIR` | `/opt/pi-scheduler/approved-skills` | Approved skills catalog |
| `PI_SCHEDULER_RUNTIME_USER` | `pi-scheduler-agent` | Runtime user expected by setup scripts |
| `PI_SCHEDULER_RUNTIME_GROUP` | `pi-scheduler` | Runtime group for data/log/lock access |
| `PI_SCHEDULER_LOG_RETENTION_DAYS` | `30` | Automatic log cleanup cutoff |

## Important paths

```text
data/pi-scheduler.sqlite3          SQLite database
logs/jobs/<job-id>/runs/           Per-job run logs
locks/<job-id>.lock                Per-job overlap lock
locks/groups/<group-id>.lock       Per-group concurrency lock
/etc/cron.d/pi-agent-jobs          Managed cron file
/opt/pi-scheduler/approved-skills  Approved skills catalog
```

## Database schema

SQLite tables are created/migrated idempotently in `db.init_db()`:

- `jobs`
- `runs`
- `job_groups`
- `job_group_members`
- `group_runs`
- `group_run_steps`
- `app_settings`
- `audit_events`

Jobs and groups support soft delete. Schema changes are backward-compatible `ALTER TABLE` additions.

## API routes

All routes require Basic Auth.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| GET/POST | `/jobs/new`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/edit` | Job create/update/detail |
| POST | `/jobs/{id}/toggle`, `/jobs/{id}/delete`, `/jobs/{id}/run` | Job actions |
| GET/POST | `/groups/new`, `/groups`, `/groups/{id}`, `/groups/{id}/edit` | Group create/update/detail |
| POST | `/groups/{id}/toggle`, `/groups/{id}/delete`, `/groups/{id}/run` | Group actions |
| GET | `/groups/{gid}/runs/{rid}`, `/group-runs/{rid}` | Group run details |
| GET | `/runs/{rid}` | Single run detail |
| GET/POST | `/logs`, `/logs/cleanup` | Logs and cleanup |
| GET | `/audit` | Audit activity feed |
| POST | `/governance/pause`, `/governance/resume` | Global pause controls |
| GET | `/cron` | Read-only cron preview/status |

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall app bin
bash -n deploy/run-local.sh
bash -n deploy/setup-runtime-user.sh
```

Tests use temporary SQLite databases and monkeypatched paths. No external services are required.

## Notes

- The web app only manages `/etc/cron.d/pi-agent-jobs`; it does not edit user crontabs.
- Pi Scheduler, the Pi CLI, and `~/.pi/agent/models.json` should be available to the configured runtime user.
- Timestamps are stored in UTC and displayed as Beijing time.
- Job overlap prevention is always enforced.

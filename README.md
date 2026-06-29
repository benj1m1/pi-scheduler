# Pi Scheduler

Personal web panel for managing cron-driven Pi prompt runs on a Linux server.

## What It Does

- Stores jobs in a local SQLite file.
- Renders app-owned cron entries to `/etc/cron.d/pi-agent-jobs`.
- Runs jobs through `bin/pi-job-runner --job-id <id>`.
- Generates Pi commands from each job's prompt.
- Lets each job select a configured Pi provider/model from `~/.pi/agent/models.json`.
- Lets each job choose summary-only output or detailed Pi event logging, whether Pi should save a session, and which tools Pi can use.
- Lets you schedule jobs as simple intervals, such as every 5 minutes or every 2 hours.
- Supports Beijing-time work windows for automatic cron runs.
- Captures run metadata in SQLite.
- Writes stdout, stderr, optional Pi event JSONL, and daily summary JSONL logs under `logs/jobs/<job-id>/`.
- Provides a Basic Auth protected FastAPI web UI.
- Displays run times in Beijing time while storing timestamps in UTC.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export PI_SCHEDULER_PASSWORD='change-this-password'
export PI_SCHEDULER_CRON_FILE="$PWD/tmp/pi-agent-jobs"
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` and log in with username `admin` and the password from `PI_SCHEDULER_PASSWORD`.

The default password is `pi-scheduler` if `PI_SCHEDULER_PASSWORD` is not set. Change it before exposing the panel to a network.

Local development writes cron content to `tmp/pi-agent-jobs` in the command above, so it does not modify `/etc/cron.d`.

## Install On Ubuntu

These steps install Pi Scheduler on an Ubuntu machine. They also work inside an Ubuntu LXC container, because LXC is just a Linux container environment.

Install and configure the Pi CLI first. Before installing Pi Scheduler, verify that `pi` and `node` work from the minimal `PATH` used by `systemd` and cron:

```bash
command -v pi
command -v node
pi --version
env -i PATH=/usr/local/bin:/usr/bin:/bin pi --version
```

If the final check hangs or fails, fix the Pi CLI installation before continuing.

```bash
sudo apt update
sudo apt install -y git python3-venv cron tmux
sudo git clone https://github.com/benj1m1/pi-scheduler.git /opt/pi-scheduler
cd /opt/pi-scheduler
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chmod +x /opt/pi-scheduler/bin/pi-job-runner
sudo cp deploy/pi-scheduler-web.service /etc/systemd/system/pi-scheduler-web.service
```

If you are installing over SSH, you can run the install commands inside tmux:

```bash
tmux new -s pi-scheduler
```

Edit `/etc/systemd/system/pi-scheduler-web.service` before starting the service:

- Set a real `PI_SCHEDULER_PASSWORD`.
- Change the `--host` value if you want to expose the web UI beyond localhost.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-scheduler-web.service
```

The machine must have the system cron daemon installed and running:

```bash
sudo systemctl status cron --no-pager
```

On app startup, create, edit, toggle, or delete, the app regenerates the managed cron file from SQLite.

## Scheduling

The web form uses simple interval controls instead of raw cron syntax:

- Every `N` minutes, where `N` is `1-59`.
- Every `N` hours, where `N` is `1-23`.

The app stores the generated cron expression internally and writes it to `/etc/cron.d/pi-agent-jobs`.

Examples:

```text
Every 5 minutes -> */5 * * * *
Every 2 hours   -> 0 */2 * * *
```

The job detail page still shows the generated cron expression for verification.

## Model Selection

The job form lists provider/model options from `~/.pi/agent/models.json` only. The scheduler treats this file as read-only: it does not create it, edit it, infer model settings, or expose provider secrets such as `apiKey`, `headers`, or `baseUrl` in the UI.

If a job selects a model, the runner builds the Pi command with the current configured pair. The exact output/session flags depend on the job's run output settings:

```bash
pi --no-session --name 'pi-scheduler: <job name>' -p --provider <provider> --model <model> "<prompt>"
```

If no model is selected, the runner keeps Pi's default model behavior:

```bash
pi --no-session --name 'pi-scheduler: <job name>' -p "<prompt>"
```

The runner validates the selected provider/model against the current `models.json` before every run. If the pair has been removed or the config is invalid, the run is recorded as failed and Pi is not invoked.

## Run Output, Sessions, And Tools

Each job has three Pi execution settings:

- Output mode `Summary only`: runs `pi -p "<prompt>"`, stores stdout as the run transcript, and does not create a Pi event JSONL file.
- Output mode `Detailed event log`: runs `pi --mode json "<prompt>"`, stores the raw Pi JSONL event stream, and renders a readable transcript from those events.
- Session `Do not save Pi session`: adds `--no-session` to the Pi command.
- Session `Save Pi session`: omits `--no-session`; saved sessions are named `pi-scheduler: <job name>`.
- Tool access `Full tools`: uses Pi's default tool access.
- Tool access `Read-only tools`: adds `--tools read,grep,find,ls`.
- Tool access `No tools`: adds `--no-tools`.

New jobs default to `Summary only`, `Do not save Pi session`, and `Full tools`. Existing databases that predate output/session settings migrate existing jobs to `Detailed event log` and `Save Pi session` to preserve prior behavior. Existing databases that predate tool access migrate jobs to `Full tools`.

Read-only tool access intentionally does not include `bash`, because shell commands can still write files or run arbitrary local processes.

## Time Display

The runner stores timestamps in UTC, for example `2026-06-27T14:00:13Z`. The web UI displays them as Beijing time, for example `2026-06-27 22:00:13 Beijing`.

## Work Windows And Manual Runs

Work windows use Beijing time. Leave both start and end as `All day` for no limit. When a window is set, the start time is included and the end time is excluded; overnight windows are supported.

Automatic cron runs outside the work window exit successfully without invoking `pi` and without creating a run record. Manual `Run Now` bypasses both the enabled toggle and the work-window check, so it can be used as a higher-permission test trigger.

The job detail page separates Recent Runs by source: `All`, `Automatic`, and `Manual`. It shows 10 runs per page, supports `Previous / Page / Next` navigation, and keeps `source` and `page` in the URL for refresh/back-forward behavior.

## Run Logs

Each run writes files under `logs/jobs/<job-id>/runs/`:

- `<run-id>.stdout.log`: stdout from `Summary only` runs, or a readable transcript generated from Pi JSON events for `Detailed event log` runs.
- `<run-id>.stderr.log`: process stderr from the `pi` CLI. It is often empty on successful runs because normal agent output is written to stdout.
- `<run-id>.pi-events.jsonl`: raw `pi --mode json` event stream for full audit/debugging. This file is only written for `Detailed event log` runs.

The job directory also contains daily `<yyyy-mm-dd>.jsonl` summary records for quick run metadata inspection.

## Important Paths

```text
/opt/pi-scheduler/data/pi-scheduler.sqlite3
/opt/pi-scheduler/logs/jobs/<job-id>/
/opt/pi-scheduler/locks/<job-id>.lock
/etc/cron.d/pi-agent-jobs
```

## Configuration

Environment variables:

- `PI_SCHEDULER_HOME`: base directory, defaults to the project root.
- `PI_SCHEDULER_DB`: SQLite path, defaults to `data/pi-scheduler.sqlite3`.
- `PI_SCHEDULER_LOG_DIR`: logs directory, defaults to `logs`.
- `PI_SCHEDULER_LOCK_DIR`: locks directory, defaults to `locks`.
- `PI_SCHEDULER_CRON_FILE`: cron file path, defaults to `/etc/cron.d/pi-agent-jobs`.
- `PI_SCHEDULER_RUNNER`: runner path used in cron, defaults to `<home>/bin/pi-job-runner`.
- `PI_BINARY`: Pi binary path, defaults to `pi`.
- `PI_MODELS_FILE`: read-only Pi custom models path, defaults to `~/.pi/agent/models.json`.
- `PI_SCHEDULER_USERNAME`: Basic Auth username, defaults to `admin`.
- `PI_SCHEDULER_PASSWORD`: Basic Auth password, defaults to `pi-scheduler`.

## Notes

The web app only manages `/etc/cron.d/pi-agent-jobs`. It does not edit user crontabs or other system cron files.

If `Next` updates but no scheduled runs appear, check that `/etc/cron.d/pi-agent-jobs` exists and that `cron.service` is active.

Pi Scheduler, the Pi CLI, `~/.pi/agent/models.json`, and `~/.pi/agent/skills/` should belong to the same runtime user. If the service runs as a dedicated user, install or configure Pi under that user's home directory, or set `PI_MODELS_FILE` to the intended read-only model config path.

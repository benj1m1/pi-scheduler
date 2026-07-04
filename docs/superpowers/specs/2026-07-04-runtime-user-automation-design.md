# Runtime User Automation Design

Date: 2026-07-04

## Goal

Make local deployment and runtime setup automatic for the dedicated Linux user used by Pi Scheduler jobs. The project should be able to prepare a dedicated user, grant scheduler runtime directory permissions, copy Pi model configuration, and warn clearly when setup is incomplete.

## Defaults

- Runtime user: `pi-scheduler-agent`
- Runtime group: `pi-scheduler`
- Scheduler home: `/opt/pi-scheduler`
- Source models file: `/root/.pi/agent/models.json`
- Destination models file: `/home/pi-scheduler-agent/.pi/agent/models.json`

The username intentionally includes the project name to reduce collisions with existing system users.

## Approach

Use a combined deploy-time setup plus app startup health-check model.

System-level changes happen in a deploy script, not inside FastAPI startup. The application only checks the expected state and logs warnings. This keeps privileged operations explicit and repeatable while still making local deploy easy.

## Deploy Script

Add `deploy/setup-runtime-user.sh`.

The script must be idempotent and safe to rerun. Default invocation:

```bash
sudo deploy/setup-runtime-user.sh
```

Default behavior:

1. Create group `pi-scheduler` if missing.
2. Create user `pi-scheduler-agent` if missing, with:
   - home: `/home/pi-scheduler-agent`
   - shell: `/bin/bash`
3. Add `pi-scheduler-agent` to group `pi-scheduler`.
4. Ensure scheduler runtime directories exist:
   - `/opt/pi-scheduler/data`
   - `/opt/pi-scheduler/logs`
   - `/opt/pi-scheduler/locks`
   - `/opt/pi-scheduler/tmp`
5. Set those directories recursively to:
   - group owner: `pi-scheduler`
   - group writable
   - setgid on directories so new files inherit `pi-scheduler`
6. Create `/home/pi-scheduler-agent/.pi/agent`.
7. Copy `/root/.pi/agent/models.json` to `/home/pi-scheduler-agent/.pi/agent/models.json` when the source exists.
8. Set ownership and permissions:
   - `/home/pi-scheduler-agent/.pi`: `pi-scheduler-agent:pi-scheduler-agent`, mode `700`
   - `/home/pi-scheduler-agent/.pi/agent`: `pi-scheduler-agent:pi-scheduler-agent`, mode `700`
   - copied `models.json`: `pi-scheduler-agent:pi-scheduler-agent`, mode `600`

If the source models file does not exist, the script should print a clear warning but continue. The runtime user and directory permissions are still useful without a copied model config.

## Script Parameters

`deploy/setup-runtime-user.sh` should support override flags while keeping the defaults above:

```bash
sudo deploy/setup-runtime-user.sh \
  --user pi-scheduler-agent \
  --group pi-scheduler \
  --home /opt/pi-scheduler \
  --models-file /root/.pi/agent/models.json
```

Supported flags:

- `--user <name>`
- `--group <name>`
- `--home <path>` for scheduler home, not Linux user home
- `--models-file <path>`
- `--help`

The Linux user home should be discovered from passwd after user creation, defaulting to `/home/<user>` for a newly-created user.

## Local Run Script

Add `deploy/run-local.sh`.

Default invocation:

```bash
cd /opt/pi-scheduler
deploy/run-local.sh
```

Behavior:

1. Resolve scheduler home from the repo root or `PI_SCHEDULER_HOME`.
2. If running as root, call `deploy/setup-runtime-user.sh` directly.
3. If not root and `sudo` exists, call `sudo deploy/setup-runtime-user.sh`.
4. If setup cannot run, print a clear warning and continue so development remains possible.
5. Export local-deploy defaults unless already set by the caller:
   - `PI_SCHEDULER_HOME=/opt/pi-scheduler`
   - `PI_SCHEDULER_CRON_FILE=/opt/pi-scheduler/tmp/pi-agent-jobs`
   - `PI_SCHEDULER_CRON_USER=pi-scheduler-agent`
   - `PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent`
6. Start Uvicorn:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The script may allow `HOST` and `PORT` environment overrides, but defaults remain `127.0.0.1` and `8080`.

## App Startup Health Check

FastAPI startup should run a non-privileged health check. It must not create users, change groups, chmod directories, or copy files.

Add a small runtime setup check module, for example `app/runtime_setup.py`, responsible for checking:

1. Expected runtime user exists.
2. Expected runtime user is in `PI_SCHEDULER_ALLOWED_RUN_USERS` when per-user scheduling is configured.
3. Runtime directories exist and are writable by the runtime user:
   - `DATA_DIR`
   - `LOG_DIR`
   - `LOCK_DIR`
   - `SCHEDULER_HOME/tmp`
4. `/home/pi-scheduler-agent/.pi/agent/models.json` exists.
5. The destination `models.json` is owned by `pi-scheduler-agent` when it exists.

Warnings should be logged with enough detail to fix the issue, including:

```bash
sudo /opt/pi-scheduler/deploy/setup-runtime-user.sh
```

Health-check warnings must not block the app from starting.

## Configuration

Add config defaults for runtime setup automation:

- `PI_SCHEDULER_RUNTIME_USER`, default `pi-scheduler-agent`
- `PI_SCHEDULER_RUNTIME_GROUP`, default `pi-scheduler`
- `PI_SCHEDULER_MODELS_SOURCE`, default `/root/.pi/agent/models.json`

Update existing defaults or local scripts so local deploy defaults to:

- `PI_SCHEDULER_CRON_USER=pi-scheduler-agent`
- `PI_SCHEDULER_ALLOWED_RUN_USERS=root,pi-scheduler-agent`

The global app default for `PI_SCHEDULER_CRON_USER` may remain `root` for backward compatibility unless launched through `deploy/run-local.sh`.

## Error Handling

- Missing source models file: setup script warns and continues.
- Existing user/group: setup script reuses them.
- Permission failure: setup script exits non-zero with the failed command context.
- No sudo in local run: `deploy/run-local.sh` warns and continues.
- Health-check failures: app logs warnings and continues.

## Testing

Automated tests should cover:

1. Runtime setup health-check reports missing runtime user.
2. Health-check reports missing models file.
3. Health-check accepts a valid mocked runtime setup.
4. Local run script exports expected default env values where practical.

Shell scripts can be validated with `bash -n` in tests or via the full test suite.

## Documentation

Update README local deployment instructions to recommend:

```bash
cd /opt/pi-scheduler
deploy/run-local.sh
```

Document the default runtime user `pi-scheduler-agent`, the setup script, source and destination model paths, and how to override defaults.

## Out of Scope

- Copying API credentials or all Pi CLI state beyond `models.json`.
- Guaranteeing the runtime user can authenticate to every provider.
- Running privileged setup from FastAPI startup.
- Changing production systemd service behavior beyond documenting or optionally referencing the setup script.

# Per-Job and Per-Group Run User Design

## Summary

Pi Scheduler will support choosing the Linux user that executes each standalone job and each job group. The feature adds an optional `run_user` field to jobs and groups. If unset, scheduler entries continue to use the global default `PI_SCHEDULER_CRON_USER`.

For maintainability, a job group runs as one user for the entire pipeline. Member jobs inherit the group process user during group execution, even if those jobs have their own standalone `run_user` values.

## Goals

- Allow some scheduled jobs to run as a dedicated low-privilege user instead of root.
- Keep existing installations backward-compatible.
- Make scheduled and manual runs use the same effective Linux user when possible.
- Keep group execution semantics simple and predictable.
- Prevent the web UI from becoming an unrestricted arbitrary-user cron editor.

## Non-Goals

- Do not support switching Linux users between individual member jobs inside one group run.
- Do not implement filesystem sandboxing in this feature.
- Do not manage Linux user creation from the web UI.
- Do not move Pi credentials automatically between users.

## Data Model

Add nullable text columns:

```sql
alter table jobs add column run_user text;
alter table job_groups add column run_user text;
```

Meaning:

- `NULL` or empty string: use `PI_SCHEDULER_CRON_USER`.
- Non-empty value: use that Linux user for the cron entry and manual runner subprocess.

Existing rows migrate with `run_user` unset, preserving current behavior.

## Configuration

Existing variable:

```env
PI_SCHEDULER_CRON_USER=root
```

continues to define the default run user.

Add:

```env
PI_SCHEDULER_ALLOWED_RUN_USERS=root,piagent
```

Rules:

- Empty `run_user` is always allowed and resolves to `PI_SCHEDULER_CRON_USER`.
- Non-empty `run_user` must match a safe Linux username pattern.
- Non-empty `run_user` must be present in `PI_SCHEDULER_ALLOWED_RUN_USERS`.
- If `PI_SCHEDULER_ALLOWED_RUN_USERS` is unset or empty, only `PI_SCHEDULER_CRON_USER` is allowed.
- Validation should check that the selected user exists on the system before saving.

The username pattern should reject whitespace and shell metacharacters. A conservative pattern is:

```text
^[a-z_][a-z0-9_-]*[$]?$
```

## Cron Rendering

Standalone jobs render with:

```python
run_user = job.get("run_user") or config.CRON_USER
```

Groups render with:

```python
run_user = group.get("run_user") or config.CRON_USER
```

Example output:

```cron
# Managed by pi-scheduler. Do not edit manually.
SHELL=/bin/bash
PATH=/root/.local/share/pi-node/node-v22.23.1-linux-x64/bin:/usr/local/bin:/usr/bin:/bin
PI_SCHEDULER_HOME=/opt/pi-scheduler

*/5 * * * * piagent /opt/pi-scheduler/bin/pi-job-runner --job-id user-job
0 * * * * root /opt/pi-scheduler/bin/pi-job-runner --job-id admin-job
*/30 * * * * piagent /opt/pi-scheduler/bin/pi-job-runner --group-id daily-flow
```

## Group Semantics

A group has a single effective run user. All member jobs execute inside the group runner process as that user.

Example:

```text
Job A run_user = root
Job B run_user = bjli
Group G run_user = piagent
Members: A -> B
```

When running `Group G`:

```text
Job A executes as piagent
Job B executes as piagent
```

When running jobs standalone:

```text
Job A executes as root
Job B executes as bjli
```

This avoids per-step user switching and keeps group logs, locks, and status updates in one consistent process context.

## Manual Run Now Behavior

Manual runs should use the same effective Linux user as scheduled runs.

Current behavior runs manual jobs in the FastAPI web process using `BackgroundTasks`. That would ignore per-job `run_user`, so it must change.

New behavior:

1. Web route resolves the effective user for the job or group.
2. Web route validates the user against the allowlist and system accounts.
3. Web route starts a background subprocess using `bin/pi-job-runner` with `--source manual`.
4. If the web process is already running as the target user, execute the runner directly.
5. If the web process is root and target user differs, execute via `sudo -u <run_user>` or an equivalent safe user-switch command.
6. If the web process cannot switch to the target user, record or display a clear failure message rather than silently running as the wrong user.

The runner CLI should accept:

```bash
pi-job-runner --job-id <id> --source manual
pi-job-runner --group-id <id> --source manual
```

Scheduled cron entries omit `--source`, defaulting to `auto`.

## UI Changes

### Job Form

Add field:

```text
Run as user
```

Help text:

```text
Leave blank to use default: <PI_SCHEDULER_CRON_USER>. Allowed users: <allowed users>.
```

### Group Form

Add the same field and help text.

### Detail Pages and Dashboard

Display effective run user:

- Explicit value: `Run as: piagent`
- Default value: `Run as: default (root)`

Group detail should make inheritance clear:

```text
Group members run as the group user during group execution.
```

## Validation and Error Handling

Form validation should reject:

- Unknown Linux users.
- Users not in `PI_SCHEDULER_ALLOWED_RUN_USERS`.
- Usernames with whitespace or shell metacharacters.

Cron rendering should also validate stored users before writing the cron file. Invalid stored users should fail loudly rather than generating unsafe cron entries.

Manual run failures should be visible to the user. If a manual run cannot switch users, the request should not enqueue a misleading run. The UI should show a message such as:

```text
Manual run could not start as piagent. The web service must run as root or be configured with sudo permission to switch users.
```

## Deployment Notes

A typical dedicated user setup:

```bash
sudo useradd --create-home --shell /bin/bash piagent
```

The run user must be able to:

- Execute `/opt/pi-scheduler/bin/pi-job-runner`.
- Execute `/opt/pi-scheduler/.venv/bin/python3`.
- Read the Pi Scheduler application code.
- Write SQLite data, logs, and locks:
  - `/opt/pi-scheduler/data/pi-scheduler.sqlite3`
  - `/opt/pi-scheduler/logs/`
  - `/opt/pi-scheduler/locks/`
- Access its own Pi configuration:
  - `/home/piagent/.pi/agent/models.json`
  - `/home/piagent/.pi/agent/auth.json`
  - `/home/piagent/.pi/agent/skills/`

The systemd service should include the allowlist:

```ini
Environment=PI_SCHEDULER_ALLOWED_RUN_USERS=root,piagent
```

If all scheduled work should use the dedicated user by default, also set:

```ini
Environment=PI_SCHEDULER_CRON_USER=piagent
```

## Testing Plan

Unit and integration tests should cover:

- Database migration adds `run_user` to `jobs` and `job_groups`.
- New jobs and groups default to unset `run_user`.
- Job and group create/update persist `run_user`.
- Invalid usernames are rejected.
- Users outside the allowlist are rejected.
- Cron rendering uses explicit job/group users when set.
- Cron rendering falls back to `PI_SCHEDULER_CRON_USER` when unset.
- Group cron entries use group `run_user`, not member job users.
- Manual run subprocess command uses direct execution when already the target user.
- Manual run subprocess command uses `sudo -u` when web process is root and target user differs.
- Manual run reports a clear error when user switching is not possible.

## Compatibility

Existing installations continue to work because `run_user` defaults to unset and cron rendering falls back to `PI_SCHEDULER_CRON_USER`.

Existing scheduled jobs keep the same effective user after migration unless edited to set a specific run user or the global default changes.

## Open Implementation Detail

The implementation should prefer an argument-list subprocess call, not shell string interpolation, for user switching. For example:

```python
["sudo", "-u", run_user, str(config.RUNNER_PATH), "--job-id", job_id, "--source", "manual"]
```

This keeps validated usernames out of shell parsing and reduces injection risk.

# Agent Governance v1 Design

Date: 2026-07-04
Project: `/opt/pi-scheduler`

## Goal

Add a first governance layer to pi-scheduler aligned with general AI agent security and governance principles:

- Accountability: every job/group can document who owns it and why it exists.
- Scope: every job/group can document its allowed boundary, target environment, risk level, and expiration.
- Traceability: administrative changes and manual run requests are auditable.
- Reversibility: operators can pause scheduler activity globally and reliably.

This design intentionally avoids a heavy approval workflow for v1. It establishes the data model, controls, and audit trail needed for later risk-based approval.

## Non-goals

- No multi-user RBAC. Existing HTTP Basic admin remains the actor identity.
- No external SIEM integration.
- No forced human approval workflow for high-risk jobs yet.
- No hard filesystem/network sandbox yet.
- No automatic deletion of expired jobs; expiration disables execution behavior but preserves history.

## Feature 1: Global Pause / Emergency Stop

### Behavior

A global scheduler state controls whether jobs and groups may execute.

States:

- `active`: current normal behavior.
- `paused`: automatic and manual execution are blocked.

When paused:

1. Generated cron content contains no executable job/group lines. It should include clear comments explaining that pi-scheduler is globally paused.
2. Manual job and group Run Now requests return a user-facing error instead of launching `bin/pi-job-runner`.
3. `bin/pi-job-runner` checks pause state before starting a job/group and records a terminal skipped/disabled result rather than invoking `pi`.
4. The home page and cron preview page show a visible paused banner/status.
5. Job/group configuration pages remain editable.

The runner-side check is required because a stale cron file, already-spawned cron command, or direct runner invocation should still respect the pause state.

### Control Surface

Add a small governance/status panel, likely on the index page first:

- Current status: `Active` or `Paused`.
- `Pause all` button when active.
- `Resume` button when paused.
- Required reason text for pause/resume actions.

Pause/resume operations write audit events.

### Persistence

Add a simple key/value settings table:

```sql
create table if not exists app_settings (
  key text primary key,
  value text not null,
  updated_at text not null
);
```

Keys for v1:

- `global_pause_enabled`: `0` or `1`.
- `global_pause_reason`: free text, empty when not paused.
- `global_pause_updated_at`: UTC timestamp string.
- `global_pause_updated_by`: admin username.

A helper module can expose:

- `governance.is_paused() -> bool`
- `governance.pause(actor, reason) -> None`
- `governance.resume(actor, reason) -> None`
- `governance.pause_status() -> dict`

## Feature 2: Governance Metadata

### Job Metadata

Add nullable/defaulted fields to `jobs`:

```sql
owner text not null default '';
purpose text not null default '';
scope text not null default '';
environment text not null default 'local';
risk_level text not null default 'low';
expires_at text;
```

### Group Metadata

Add the same fields to `job_groups`:

```sql
owner text not null default '';
purpose text not null default '';
scope text not null default '';
environment text not null default 'local';
risk_level text not null default 'low';
expires_at text;
```

### Allowed Values

`environment` values:

- `local`
- `dev`
- `staging`
- `production`

`risk_level` values:

- `low`
- `medium`
- `high`

### Validation

For v1:

- `owner`, `purpose`, and `scope` are optional to avoid breaking existing jobs.
- If provided, trim whitespace.
- `environment` and `risk_level` must be valid enum values.
- `expires_at`, if provided, must parse as `YYYY-MM-DD` and be stored as that normalized date string. Expiration is evaluated in Beijing local date semantics: a job/group is considered expired when the current Beijing date is later than `expires_at`.

### Expiration Behavior

Expired jobs/groups should not execute automatically or manually.

Rules:

1. Cron rendering skips expired jobs/groups.
2. Manual Run Now returns a user-facing error for expired jobs/groups.
3. Runner checks expiration before execution and records a terminal skipped/disabled result.
4. UI displays expired status on index/detail pages.
5. Expiration does not modify `enabled`; it is an execution guard and display state.

This provides scoped-by-time behavior without destructive changes.

### UI

Job and group forms gain a `Governance` section:

- Owner
- Purpose
- Scope / boundaries
- Environment
- Risk level
- Expires at

Job and group detail/index cards display compact metadata:

- Owner
- Environment
- Risk
- Expiration status

## Feature 3: Audit Log

### Events to Record in v1

Record audit events for:

- Job created
- Job updated
- Job enabled/disabled
- Job deleted
- Group created
- Group updated
- Group enabled/disabled
- Group deleted
- Manual job run requested
- Manual group run requested
- Global pause enabled
- Global pause disabled

Cron rewrites happen as a side effect of many actions. For v1, do not record every successful cron rewrite to avoid noisy logs. Instead, action events imply the cron state changed. Cron write failures should continue surfacing as errors through existing request handling.

### Schema

Add table:

```sql
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
```

Indexes:

```sql
create index if not exists idx_audit_events_created_at on audit_events(created_at desc);
create index if not exists idx_audit_events_target on audit_events(target_type, target_id, created_at desc);
```

### Actor and Source IP

- Actor is the HTTP Basic username returned by `require_auth`.
- Source IP comes from `request.client.host` when available.
- Runner-created terminal records are not audit events in v1; run records already capture execution outcome.

### Before/After JSON

For create:

- `before_json = null`
- `after_json = normalized object`

For update/toggle/delete:

- `before_json = previous object`
- `after_json = new object or deleted marker`

For manual run:

- `before_json = null`
- `after_json` includes target summary and resolved run user command metadata where safe. Do not store secrets.

### UI

Add `/audit` page with filters:

- target type: all/job/group/system
- event type: all or specific
- target id text filter
- page navigation

Display:

- timestamp in Beijing time
- actor
- event type
- target
- summary
- optional collapsible before/after JSON

Also link recent audit events from the home page or governance panel if simple.

## Data Flow

### Create/update job or group

1. Route authenticates admin and obtains actor.
2. Form data includes governance metadata.
3. Validation checks existing scheduling/security fields plus governance enum/date fields.
4. Route loads `before` object for updates.
5. DB create/update runs.
6. DB loads `after` object.
7. Audit event records actor, event type, target, before/after.
8. Cron file is rewritten.
9. Route redirects.

### Manual run

1. Route authenticates admin and obtains actor.
2. Route loads job/group.
3. Route checks global pause and expiration.
4. Route builds user-switching command.
5. Audit event records manual run requested.
6. Background task launches command.

### Automatic run

1. Cron only includes active, enabled, non-expired jobs/groups when not paused.
2. Runner independently checks paused/expired before execution.
3. If blocked, runner writes a terminal run/group-run status and exits successfully enough to avoid retries/noise.

## Error Handling

- Pause/resume requires a non-empty reason; invalid requests return form errors.
- Invalid governance enum values return form errors.
- Invalid expiration date returns form errors.
- Manual run while paused returns HTTP 400 with a clear message.
- Manual run after expiration returns HTTP 400 with a clear message.
- Runner pause/expiration guard should never invoke `pi`.
- Audit logging should be part of the same request path; if audit insert fails, the request should fail rather than silently losing traceability.

## Testing Strategy

Unit/integration tests should cover:

- DB migration adds metadata/settings/audit tables and defaults.
- Job/group form data persists governance fields.
- Invalid environment/risk/expires values are rejected.
- Cron rendering omits all executable entries while paused.
- Cron rendering omits expired jobs/groups.
- Manual run is blocked while paused.
- Manual run is blocked when expired.
- Runner skips paused/expired jobs before building Pi command.
- Audit events are written for create/update/toggle/delete/manual run/pause/resume.
- `/audit` renders recent events and filters.

## Rollout and Backward Compatibility

Existing jobs/groups migrate with:

- empty owner/purpose/scope
- environment `local`
- risk `low`
- no expiration

Global pause defaults to inactive.

Existing run history is unchanged.

## Open Follow-ups After v1

- Risk-based confirmation or approval workflow.
- Policy presets.
- Security/health dashboard.
- Strong workspace sandbox / execution boundary enforcement.
- External audit sink export.

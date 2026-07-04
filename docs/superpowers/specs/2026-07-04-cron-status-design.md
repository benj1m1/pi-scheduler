# Cron Status Design

Date: 2026-07-04

## Goal

Make the `/cron` page show whether the generated cron file is actually active on the host, not just what Pi Scheduler would render. This should help diagnose deployments where `PI_SCHEDULER_CRON_FILE` points to a local preview file such as `/opt/pi-scheduler/tmp/pi-agent-jobs` instead of a system cron file under `/etc/cron.d`.

## Problem

The current Cron Preview page shows generated content and the configured target path. Users can mistake a local preview file for an active system cron entry. In local deploy, `deploy/run-local.sh` intentionally defaults `PI_SCHEDULER_CRON_FILE` to `<home>/tmp/pi-agent-jobs`, which cron does not read automatically.

## Approach

Add a non-mutating cron inspection layer and display its findings on the existing `/cron` page.

Do not change local deploy defaults. Local deploy should remain safe and write to `tmp/pi-agent-jobs` by default. The UI should clearly mark that path as preview-only/not active in system cron.

## New Module

Create `app/cron_status.py`.

It should expose:

```python
def inspect(generated_content: str | None = None) -> dict:
    ...
```

The returned dictionary should include:

- `target_file: str`
- `is_system_cron_path: bool`
- `file_exists: bool`
- `content_matches: bool | None`
- `file_mode: str | None`
- `file_owner: str | None`
- `cron_service_active: bool | None`
- `status: str`
- `warnings: list[str]`
- `recommendations: list[str]`

## Status Semantics

Use simple status labels:

- `active_candidate`: target is under `/etc/cron.d`, file exists, and disk content matches generated content.
- `preview_only`: target is outside `/etc/cron.d`; cron will not read it automatically.
- `missing`: target is under `/etc/cron.d` but file does not exist.
- `out_of_sync`: target file exists but content differs from generated content.
- `error`: inspection failed unexpectedly.

`active_candidate` intentionally does not guarantee that cron will run every job. It means Pi Scheduler wrote a file in a path system cron normally reads and the file matches the generated preview.

## Checks

The inspector should check:

1. Configured target file: `config.CRON_FILE`.
2. Whether the target path is inside `/etc/cron.d`.
3. Whether the target file exists.
4. Whether the on-disk content matches the generated content.
5. File owner and mode when the file exists.
6. Whether `cron` service appears active via `systemctl is-active cron`, if `systemctl` is available.

If `systemctl` is unavailable or fails, `cron_service_active` should be `None` and the UI should show an informational warning rather than failing the page.

## UI

Update `app/templates/cron_preview.html` to add a status section above the generated cron content.

The section should show:

- Configured target path.
- Overall status.
- Checks with readable labels.
- Warnings.
- Recommended fixes.

For local preview files, show language like:

```text
Preview only — not active in system cron.
Target file is outside /etc/cron.d. System cron will not read this file automatically.
```

Recommended fix:

```bash
export PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs
deploy/run-local.sh
```

or use the systemd deployment.

## Route Changes

Modify `app/main.py` `/cron` route:

1. Render generated cron content as it does today.
2. Call `cron_status.inspect(content)`.
3. Pass `cron_status` into the template.
4. If cron rendering fails, still call inspection with `None` where possible so target-path diagnostics remain visible.

## Safety

The status page must be read-only. It must not write cron files, restart services, change permissions, or mutate configuration.

## Testing

Add tests for:

1. Target outside `/etc/cron.d` returns `preview_only` and recommendations.
2. Target under `/etc/cron.d` with matching file returns `active_candidate`.
3. Existing target with different content returns `out_of_sync`.
4. Missing system target returns `missing`.
5. `/cron` route passes cron status into the template context or rendered HTML includes status text.

## Documentation

Update README to explain:

- Cron Preview now shows status.
- Local deploy default cron file is preview-only.
- To make local deploy use system cron, set `PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs` before `deploy/run-local.sh`, or use the systemd deployment.

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import db


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ENVIRONMENTS = {"local", "dev", "staging", "production"}
RISK_LEVELS = {"low", "medium", "high"}


def normalize_expires_at(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("Expiration date must use YYYY-MM-DD") from exc


def validate_metadata(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if (data.get("environment") or "local") not in ENVIRONMENTS:
        errors.append("Environment is invalid")
    if (data.get("risk_level") or "low") not in RISK_LEVELS:
        errors.append("Risk level is invalid")
    try:
        normalize_expires_at(data.get("expires_at"))
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def beijing_today() -> date:
    return datetime.now(BEIJING_TZ).date()


def is_expired(expires_at: str | None, today: date | None = None) -> bool:
    normalized = normalize_expires_at(expires_at)
    if normalized is None:
        return False
    current = today or beijing_today()
    return current > datetime.strptime(normalized, "%Y-%m-%d").date()


def is_target_expired(target: dict[str, Any]) -> bool:
    return is_expired(target.get("expires_at"))


def get_setting(key: str, default: str = "") -> str:
    with db.connect() as conn:
        row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    now = db.utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            insert into app_settings (key, value, updated_at) values (?, ?, ?)
            on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def pause_status() -> dict[str, str | bool]:
    return {
        "paused": get_setting("global_pause_enabled", "0") == "1",
        "reason": get_setting("global_pause_reason", ""),
        "updated_at": get_setting("global_pause_updated_at", ""),
        "updated_by": get_setting("global_pause_updated_by", ""),
    }


def is_paused() -> bool:
    return bool(pause_status()["paused"])


def _set_pause(enabled: bool, actor: str, reason: str, source_ip: str | None) -> None:
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("Reason is required")
    now = db.utc_now()
    set_setting("global_pause_enabled", "1" if enabled else "0")
    set_setting("global_pause_reason", cleaned if enabled else "")
    set_setting("global_pause_updated_at", now)
    set_setting("global_pause_updated_by", actor)
    record_audit_event(
        actor=actor,
        event_type="system.paused" if enabled else "system.resumed",
        target_type="system",
        target_id=None,
        summary=("Paused scheduler: " if enabled else "Resumed scheduler: ") + cleaned,
        before=None,
        after={"paused": enabled, "reason": cleaned, "updated_at": now},
        source_ip=source_ip,
    )


def pause(actor: str, reason: str, source_ip: str | None = None) -> None:
    _set_pause(True, actor, reason, source_ip)


def resume(actor: str, reason: str, source_ip: str | None = None) -> None:
    _set_pause(False, actor, reason, source_ip)


def record_audit_event(
    actor: str,
    event_type: str,
    target_type: str,
    target_id: str | None,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    source_ip: str | None = None,
) -> str:
    event_id = f"audit-{uuid.uuid4().hex}"
    with db.connect() as conn:
        conn.execute(
            """
            insert into audit_events (
              id, created_at, actor, event_type, target_type, target_id, summary,
              before_json, after_json, source_ip
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                db.utc_now(),
                actor,
                event_type,
                target_type,
                target_id,
                summary,
                json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
                json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
                source_ip,
            ),
        )
    return event_id


def list_audit_events(
    limit: int = 50,
    offset: int = 0,
    target_type: str | None = None,
    event_type: str | None = None,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if target_type:
        filters.append("target_type = ?")
        params.append(target_type)
    if event_type:
        filters.append("event_type = ?")
        params.append(event_type)
    if target_id:
        filters.append("target_id like ?")
        params.append(f"%{target_id}%")
    where = " where " + " and ".join(filters) if filters else ""
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            select * from audit_events
            {where}
            order by created_at desc
            limit ? offset ?
            """,
            params,
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        before_json = event.pop("before_json")
        after_json = event.pop("after_json")
        event["before"] = json.loads(before_json) if before_json else None
        event["after"] = json.loads(after_json) if after_json else None
        events.append(event)
    return events

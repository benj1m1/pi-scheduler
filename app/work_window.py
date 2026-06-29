from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Work window times must use HH:MM format") from exc


def validate(start: str | None, end: str | None) -> None:
    if not start and not end:
        return
    if not start or not end:
        raise ValueError("Work window needs both start and end times")
    start_time = parse_time(start)
    end_time = parse_time(end)
    if start_time == end_time:
        raise ValueError("Work window start and end must be different")


def is_overnight(start: str | None, end: str | None) -> bool:
    start_time = parse_time(start)
    end_time = parse_time(end)
    if start_time is None or end_time is None:
        return False
    return start_time > end_time


def is_within_window(start: str | None, end: str | None, now: datetime | None = None) -> bool:
    validate(start, end)
    start_time = parse_time(start)
    end_time = parse_time(end)
    if start_time is None or end_time is None:
        return True

    current = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).time()
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def describe(start: str | None, end: str | None) -> str:
    if not start and not end:
        return "All day"
    suffix = " (overnight)" if is_overnight(start, end) else ""
    return f"{start} - {end} Beijing{suffix}"

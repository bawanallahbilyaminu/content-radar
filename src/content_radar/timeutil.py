"""Local time handling. All dates and windows in Content Radar use Africa/Lagos."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Africa/Lagos"
_tz_name = DEFAULT_TIMEZONE


def set_timezone(name: str) -> None:
    """Called by config loading so the whole run shares one zone."""
    global _tz_name
    ZoneInfo(name)  # raises early if the name is invalid
    _tz_name = name


def local_tz() -> ZoneInfo:
    return ZoneInfo(_tz_name)


def now_local() -> datetime:
    return datetime.now(local_tz())


def to_local(dt: datetime) -> datetime:
    """Make a datetime timezone-aware in the local zone. Naive values are taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def from_timestamp(ts: float | int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=UTC).astimezone(local_tz())


def today_local() -> str:
    return now_local().date().isoformat()

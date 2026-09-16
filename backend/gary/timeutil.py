"""Timestamp rules.

Operational timestamps are stored as ISO 8601 strings normalized to UTC with an
explicit offset, e.g. ``2026-09-16T20:30:00+00:00``. One offset keeps plain
string comparison in SQL (``due_at <= ?``) correct across daylight saving
changes. Tools convert back to the user's timezone before Gary sees them.
"""

import datetime as dt
from zoneinfo import ZoneInfo


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_utc(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def parse_timestamp(value, field: str = "timestamp") -> str:
    """Validate an ISO 8601 date-time with an offset and normalize it to UTC."""
    example = "2026-10-10T17:00:00-05:00"
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO 8601 date-time such as {example}")

    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"{field} must be an ISO 8601 date-time such as {example}, "
            f"not {value!r}"
        ) from None

    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset, e.g. {example}")

    return format_utc(parsed)


def to_datetime(stored: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stored)


def to_local(stored: str | None, timezone: ZoneInfo) -> str | None:
    if not stored:
        return stored
    return to_datetime(stored).astimezone(timezone).isoformat()

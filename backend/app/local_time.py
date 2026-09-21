"""Timestamps in Alex's local time, for the calendar and for being spoken.

Storage is UTC (gary.timeutil); these are the display forms only.
"""

from zoneinfo import ZoneInfo

from app.config import LOCAL_TIMEZONE
from gary.timeutil import to_datetime, to_local


def local_iso(value: str) -> str:
    return to_local(value, ZoneInfo(LOCAL_TIMEZONE))


def spoken_time(value: str) -> str:
    local = to_datetime(value).astimezone(ZoneInfo(LOCAL_TIMEZONE))
    return local.strftime("%A %B %-d at %-I:%M %p")


def spoken_clock(value: str) -> str:
    return to_datetime(value).astimezone(ZoneInfo(LOCAL_TIMEZONE)).strftime("%-I:%M %p")

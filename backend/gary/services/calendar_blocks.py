"""Working time: work hours, work days, protected times (meals, breaks), and
free blocks around busy calendar time.

Pure functions over UTC ISO strings (see gary/timeutil.py). Gary uses these to
find reasonable work blocks without filling every minute, and to estimate
whether a deadline is realistic.
"""

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from gary.timeutil import format_utc, to_datetime

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class WorkHours:
    start: int
    end: int

    def label(self) -> str:
        return f"{self.start:02d}:00-{self.end:02d}:00"


@dataclass(frozen=True)
class WorkWeek:
    hours: WorkHours = WorkHours(9, 17)
    days: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    # Local (start, end) times kept free every work day, e.g. lunch.
    protected: tuple[tuple[dt.time, dt.time], ...] = field(default_factory=tuple)

    def describe(self) -> dict:
        return {
            "working_hours": self.hours.label(),
            "working_days": [WEEKDAY_NAMES[day] for day in sorted(self.days)],
            "protected_times": [
                f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
                for start, end in self.protected
            ],
        }


def parse_work_hours(value: str) -> WorkHours:
    try:
        start, end = (int(part) for part in value.split("-"))
    except ValueError:
        raise ValueError(f"WORK_HOURS must look like 9-17, not {value!r}") from None
    if not 0 <= start < end <= 24:
        raise ValueError("WORK_HOURS must be two hours from 0 to 24, start before end")
    return WorkHours(start, end)


def parse_weekdays(value: str) -> frozenset[int]:
    days = set()
    for item in filter(None, (part.strip().lower() for part in value.split(","))):
        if item not in WEEKDAY_NAMES:
            raise ValueError(f"weekday entries must be from {WEEKDAY_NAMES}, not {item!r}")
        days.add(WEEKDAY_NAMES.index(item))
    return frozenset(days)


def parse_protected_times(value: str) -> tuple[tuple[dt.time, dt.time], ...]:
    """``12:00-13:00,15:00-15:15``; empty for none."""
    ranges = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        start, _, end = item.partition("-")
        try:
            start_time = dt.time.fromisoformat(start.strip())
            end_time = dt.time.fromisoformat(end.strip())
        except ValueError:
            raise ValueError(f"PROTECTED_TIMES entries must look like 12:00-13:00, not {item!r}") from None
        if end_time <= start_time:
            raise ValueError(f"PROTECTED_TIMES range {item!r} must end after it starts")
        ranges.append((start_time, end_time))
    return tuple(sorted(ranges))


def _clip(intervals, start: str, end: str):
    return [
        (max(a, start), min(b, end))
        for a, b in intervals
        if max(a, start) < min(b, end)
    ]


def _subtract(intervals, busy):
    """Remove busy (start, end) ranges from sorted, non-overlapping intervals."""
    result = []
    busy = sorted(busy)
    for start, end in intervals:
        cursor = start
        for busy_start, busy_end in busy:
            if busy_end <= cursor or busy_start >= end:
                continue
            if busy_start > cursor:
                result.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def minutes_between(start: str, end: str) -> int:
    return int((to_datetime(end) - to_datetime(start)).total_seconds() // 60)


def _local_days(start: str, end: str, timezone: ZoneInfo):
    day = to_datetime(start).astimezone(timezone).date()
    last = to_datetime(end).astimezone(timezone).date()
    while day <= last:
        yield day
        day += dt.timedelta(days=1)


def _at(day: dt.date, hour_or_time, timezone: ZoneInfo) -> str:
    if isinstance(hour_or_time, dt.time):
        local = dt.datetime.combine(day, hour_or_time, timezone)
    else:
        local = dt.datetime.combine(day, dt.time(), timezone) + dt.timedelta(hours=hour_or_time)
    return format_utc(local)


def protected_intervals(start: str, end: str, week: WorkWeek, timezone: ZoneInfo) -> list[tuple[str, str]]:
    return _clip(
        [
            (_at(day, protected_start, timezone), _at(day, protected_end, timezone))
            for day in _local_days(start, end, timezone)
            if day.weekday() in week.days
            for protected_start, protected_end in week.protected
        ],
        start,
        end,
    )


def work_windows(start: str, end: str, week: WorkWeek, timezone: ZoneInfo) -> list[tuple[str, str]]:
    """Working time between start and end, minus protected times."""
    windows = [
        (_at(day, week.hours.start, timezone), _at(day, week.hours.end, timezone))
        for day in _local_days(start, end, timezone)
        if day.weekday() in week.days
    ]
    return _subtract(_clip(windows, start, end), protected_intervals(start, end, week, timezone))


def working_minutes(start: str, end: str, week: WorkWeek, timezone: ZoneInfo) -> int:
    if end <= start:
        return 0
    return sum(minutes_between(a, b) for a, b in work_windows(start, end, week, timezone))


def find_free_blocks(
    start: str,
    end: str,
    busy: list[dict],
    week: WorkWeek,
    timezone: ZoneInfo,
    min_minutes: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Free working time of at least ``min_minutes``, earliest first."""
    busy_ranges = [(item["start"], item["end"]) for item in busy]
    free = _subtract(work_windows(start, end, week, timezone), busy_ranges)
    blocks = [
        {"start": a, "end": b, "minutes": minutes_between(a, b)}
        for a, b in free
        if minutes_between(a, b) >= min_minutes
    ]
    return blocks[:limit]


def working_time_problem(start: str, end: str, week: WorkWeek, timezone: ZoneInfo) -> str | None:
    """Why a block falls outside reasonable working time, or None if it fits."""
    # Compare in one offset: interval strings are only ordered when normalized.
    start, end = format_utc(to_datetime(start)), format_utc(to_datetime(end))
    local_start = to_datetime(start).astimezone(timezone)
    local_end = to_datetime(end).astimezone(timezone)
    day_start = dt.datetime.combine(local_start.date(), dt.time(), timezone)
    if (
        local_start.weekday() not in week.days
        or local_start < day_start + dt.timedelta(hours=week.hours.start)
        or local_end > day_start + dt.timedelta(hours=week.hours.end)
    ):
        days = ", ".join(WEEKDAY_NAMES[day] for day in sorted(week.days))
        return f"it is outside working hours ({week.hours.label()}, {days})"
    for protected_start, protected_end in protected_intervals(start, end, week, timezone):
        if start < protected_end and protected_start < end:
            label = ", ".join(f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in week.protected)
            return f"it overlaps protected time ({label})"
    return None

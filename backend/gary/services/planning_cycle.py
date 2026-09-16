"""Scheduled planning cycle (spec section 44).

    context  = planning service (SQLite)
    notes    = Gary's planning notes and yesterday's summary (Joplin)
    busy     = busy calendar times (no event titles)
    plan     = ONE model call with a strict JSON schema
    accepted = deterministic Python validation of each proposed action
    results  = action service (policy: green runs, yellow waits, red refused)
    record   = planning_runs row + audit
    summary  = daily summary note (Joplin)

There is no tool loop and no automatic retry: a cycle makes one model call,
and a failed cycle is recorded as failed. The model only proposes; Python
decides what is valid and policy decides what runs.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from gary.container import Gary
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.policy import GARY_ACTOR
from gary.services.readiness import CLOSED_STATUSES, task_readiness
from gary.timeutil import format_utc, parse_timestamp, to_datetime, to_local
from gary.tools.base import TIMESTAMP_FIELDS

logger = logging.getLogger("gary.planning_cycle")

SCHEDULED_TYPES = ("morning", "midday", "evening")
CYCLE_ACTION_TYPES = ("schedule_task", "move_calendar_event")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CATCH_UP_MINUTES = 90
MIN_LEAD_MINUTES = 10
MIN_BLOCK_MINUTES = 15
MAX_BLOCK_MINUTES = 240
MAX_RELEVANT_NOTES = 5
DAILY_SUMMARY_PREFIX = "Daily summary "


class Planner(Protocol):
    async def create_plan(self, planning_input: dict) -> dict: ...


class NotebookService(Protocol):
    async def get_relevant_notes(self, project_names: list[str], today: dt.date) -> list[dict]: ...

    async def write_daily_summary(self, day: dt.date, markdown: str) -> None: ...


class BusyCalendar(Protocol):
    async def busy_intervals(self, start: str, end: str) -> list[dict]: ...


@dataclass(frozen=True)
class WorkHours:
    start: int
    end: int

    def label(self) -> str:
        return f"{self.start:02d}:00-{self.end:02d}:00"


# ---------------------------------------------------------------- settings

def parse_work_hours(value: str) -> WorkHours:
    try:
        start, end = (int(part) for part in value.split("-"))
    except ValueError:
        raise ValueError(f"WORK_HOURS must look like 9-17, not {value!r}") from None
    if not 0 <= start < end <= 24:
        raise ValueError("WORK_HOURS must be two hours from 0 to 24, start before end")
    return WorkHours(start, end)


def parse_schedule(value: str) -> dict[str, dt.time]:
    """``morning=08:00,midday=12:30,evening=17:30``; empty turns runs off."""
    schedule = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, _, clock = item.partition("=")
        name = name.strip()
        if name not in SCHEDULED_TYPES:
            raise ValueError(f"PLANNING_TIMES names must be {SCHEDULED_TYPES}, not {name!r}")
        try:
            schedule[name] = dt.time.fromisoformat(clock.strip())
        except ValueError:
            raise ValueError(f"PLANNING_TIMES time for {name} must look like 08:00") from None
    return dict(sorted(schedule.items(), key=lambda item: item[1]))


def parse_weekdays(value: str) -> frozenset[int]:
    days = set()
    for item in filter(None, (part.strip().lower() for part in value.split(","))):
        if item not in WEEKDAY_NAMES:
            raise ValueError(f"PLANNING_WEEKDAYS entries must be from {WEEKDAY_NAMES}")
        days.add(WEEKDAY_NAMES.index(item))
    return frozenset(days)


def due_planning_types(
    now_local: dt.datetime,
    schedule: dict[str, dt.time],
    weekdays: frozenset[int],
    already_started: set[str],
) -> list[str]:
    """Scheduled runs due now: past their time by less than CATCH_UP_MINUTES,
    on a planning weekday, and not yet started today."""
    if now_local.weekday() not in weekdays:
        return []
    due = []
    for planning_type, at in schedule.items():
        scheduled = dt.datetime.combine(now_local.date(), at, now_local.tzinfo)
        in_window = scheduled <= now_local < scheduled + dt.timedelta(minutes=CATCH_UP_MINUTES)
        if in_window and planning_type not in already_started:
            due.append(planning_type)
    return due


# ------------------------------------------------------------------- notes

def normalize_title(value: str) -> str:
    return " ".join(value.split()).casefold()


def select_relevant_notes(notes: list[dict], project_names: list[str]) -> list[dict]:
    """Planning notes whose title is an active project's name."""
    wanted = {normalize_title(name) for name in project_names}
    return [note for note in notes if normalize_title(note.get("title", "")) in wanted][
        :MAX_RELEVANT_NOTES
    ]


def daily_summary_title(day: dt.date) -> str:
    return f"{DAILY_SUMMARY_PREFIX}{day.isoformat()}"


def previous_summary(notes: list[dict], today: dt.date) -> dict | None:
    """The latest daily summary note from before today."""
    dated = []
    for note in notes:
        title = note.get("title", "")
        if not title.startswith(DAILY_SUMMARY_PREFIX):
            continue
        try:
            day = dt.date.fromisoformat(title.removeprefix(DAILY_SUMMARY_PREFIX))
        except ValueError:
            continue
        if day < today:
            dated.append((day, note))
    return max(dated, key=lambda item: item[0])[1] if dated else None


# -------------------------------------------------------------- validation

def overlaps(start: str, end: str, intervals: list[tuple[str, str]]) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in intervals)


def validate_cycle_actions(
    gary: Gary,
    actions: list[dict],
    *,
    now: str,
    busy: list[dict],
    calendar_available: bool,
    work_hours: WorkHours,
    work_days: frozenset[int],
    horizon_hours: int,
    max_actions: int,
) -> tuple[list[dict], list[dict]]:
    """Deterministic checks on every proposed action. Returns (accepted
    proposals ready for the action service, rejected with reasons)."""
    accepted, rejected, planned = [], [], []
    seen_tasks = set()
    earliest = format_utc(to_datetime(now) + dt.timedelta(minutes=MIN_LEAD_MINUTES))
    latest = format_utc(to_datetime(now) + dt.timedelta(hours=horizon_hours))

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        for proposal in actions:
            def reject(reason: str):
                rejected.append({"proposal": proposal, "reason": reason})

            action_type = proposal.get("action_type")
            if action_type not in CYCLE_ACTION_TYPES:
                reject(f"{action_type} is not allowed in a scheduled planning cycle")
                continue
            if len(accepted) >= max_actions:
                reject(f"more than {max_actions} actions proposed")
                continue
            if not calendar_available:
                reject("the calendar could not be read, so nothing can be scheduled")
                continue

            task_id = proposal.get("task_id")
            task = repos.tasks.get(task_id) if isinstance(task_id, str) else None
            if task is None:
                reject("unknown task")
                continue
            if task_id in seen_tasks:
                reject("the task already has an action in this plan")
                continue
            if task["status"] in CLOSED_STATUSES:
                reject(f"the task is {task['status']}")
                continue

            start_key, end_key = (
                ("start", "end") if action_type == "schedule_task" else ("new_start", "new_end")
            )
            try:
                start = parse_timestamp(proposal.get(start_key), start_key)
                end = parse_timestamp(proposal.get(end_key), end_key)
            except ValueError as exc:
                reject(str(exc))
                continue

            if action_type == "schedule_task":
                if task["calendar_event_id"]:
                    reject("the task is already on the calendar")
                    continue
                readiness = task_readiness(
                    task, repos.dependencies.list_dependencies(task_id), now
                )
                if not readiness["ready"]:
                    reject(f"the task is not ready: {', '.join(readiness['reasons'])}")
                    continue
            elif not task["calendar_event_id"]:
                reject("the task is not on the calendar")
                continue

            minutes = (to_datetime(end) - to_datetime(start)).total_seconds() / 60
            if not MIN_BLOCK_MINUTES <= minutes <= MAX_BLOCK_MINUTES:
                reject(f"blocks must be {MIN_BLOCK_MINUTES} to {MAX_BLOCK_MINUTES} minutes")
                continue
            if start < earliest or end > latest:
                reject("outside the scheduling horizon")
                continue

            local_start = to_datetime(start).astimezone(gary.timezone)
            local_end = to_datetime(end).astimezone(gary.timezone)
            midnight = dt.datetime.combine(local_start.date(), dt.time(), gary.timezone)
            day_end = midnight + dt.timedelta(hours=work_hours.end)
            if (
                local_start.weekday() not in work_days
                or local_start.hour < work_hours.start
                or local_end > day_end
            ):
                reject(f"outside working hours ({work_hours.label()} on working days)")
                continue

            others = [
                (interval["start"], interval["end"])
                for interval in busy
                if interval.get("event_id") != task["calendar_event_id"]
            ]
            if overlaps(start, end, others + planned):
                reject("overlaps another calendar event or proposed block")
                continue

            payload = (
                {"task_id": task_id, "start": start, "end": end}
                if action_type == "schedule_task"
                else {"task_id": task_id, "new_start": start, "new_end": end}
            )
            accepted.append(
                {
                    "action_type": action_type,
                    "payload": payload,
                    "reason": str(proposal.get("reason") or "")[:1000] or None,
                    "task_id": task_id,
                    "project_id": task["project_id"],
                    "task_title": task["title"],
                }
            )
            planned.append((start, end))
            seen_tasks.add(task_id)

    return accepted, rejected


# ------------------------------------------------------------------ summary

def render_summary(
    planning_type: str,
    local_now: dt.datetime,
    plan: dict,
    results: list[dict],
    rejected: list[dict],
    context: dict,
    timezone: ZoneInfo,
) -> str:
    def when(value):
        return to_datetime(value).astimezone(timezone).strftime("%a %-I:%M %p") if value else ""

    lines = [
        f"## {planning_type.capitalize()} planning, {local_now.strftime('%-I:%M %p')}",
        "",
        plan["summary"] or "No summary.",
    ]

    if results:
        lines += ["", "**Actions**", ""]
        for item in results:
            result = item["result"]
            status = result.get("status", "error").replace("_", " ")
            proposal = item["proposal"]
            detail = result.get("summary") or f"{proposal['action_type']} {proposal['task_title']}"
            error = f": {result['error']}" if result.get("error") else ""
            lines.append(f"- {detail} ({status}{error})")

    if rejected:
        lines += ["", "**Not accepted**", ""]
        for item in rejected:
            proposal = item["proposal"]
            lines.append(f"- {proposal.get('action_type')}: {item['reason']}")

    if context["overdue_tasks"]:
        lines += ["", "**Overdue**", ""]
        lines += [f"- {t['title']} (due {when(t['deadline'])})" for t in context["overdue_tasks"]]
    if context["blocked_tasks"]:
        lines += ["", "**Blocked**", ""]
        for task in context["blocked_tasks"]:
            waiting = ", ".join(task["blocked_by"])
            lines.append(
                f"- {task['title']}: " + (f"waiting on {waiting}" if waiting else task["status"])
            )
    if context["due_followups"]:
        lines += ["", "**Follow-ups due**", ""]
        lines += [f"- {f['title']}" for f in context["due_followups"]]
    if context["open_commitments"]:
        lines += ["", "**Open commitments**", ""]
        lines += [
            f"- {c['description']}" + (f" (due {when(c['deadline'])})" if c["deadline"] else "")
            for c in context["open_commitments"]
        ]
    return "\n".join(lines)


# -------------------------------------------------------------------- cycle

class PlanningCycleError(RuntimeError):
    pass


class PlanningCycle:
    def __init__(
        self,
        gary: Gary,
        planner: Planner,
        notebook: NotebookService,
        calendar: BusyCalendar,
        *,
        work_hours: WorkHours,
        work_days: frozenset[int],
        max_actions: int = 5,
        horizon_hours: int = 72,
    ):
        self.gary = gary
        self.planner = planner
        self.notebook = notebook
        self.calendar = calendar
        self.work_hours = work_hours
        self.work_days = work_days
        self.max_actions = max_actions
        self.horizon_hours = horizon_hours
        # One cycle at a time, even if a scheduled and a manual run collide.
        self._lock = asyncio.Lock()

    def _local(self, value):
        return to_local(value, self.gary.timezone)

    def _localize(self, value):
        if isinstance(value, list):
            return [self._localize(item) for item in value]
        if isinstance(value, dict):
            return {
                key: (
                    self._local(item)
                    if key in TIMESTAMP_FIELDS and isinstance(item, str)
                    else self._localize(item)
                )
                for key, item in value.items()
                if not key.endswith("_json")
            }
        return value

    async def run(self, planning_type: str) -> dict:
        async with self._lock:
            return await self._run(planning_type)

    async def _run(self, planning_type: str) -> dict:
        context = await asyncio.to_thread(self.gary.planning.get_planning_context, planning_type)
        run_id = context["planning_run_id"]
        now = context["now"]
        local_now = to_datetime(now).astimezone(self.gary.timezone)
        today = local_now.date()

        try:
            notes, notes_error = [], None
            try:
                notes = await self.notebook.get_relevant_notes(
                    [p["name"] for p in context["active_projects"]], today
                )
            except Exception as exc:
                logger.warning("Planning notes unavailable: %s", exc)
                notes_error = str(exc) or type(exc).__name__

            busy, calendar_error = [], None
            horizon_end = format_utc(to_datetime(now) + dt.timedelta(hours=self.horizon_hours))
            try:
                busy = await self.calendar.busy_intervals(now, horizon_end)
            except Exception as exc:
                logger.warning("Calendar unavailable for planning: %s", exc)
                calendar_error = str(exc) or type(exc).__name__

            operations = {key: value for key, value in context.items() if key != "planning_run_id"}
            planning_input = {
                "planning_type": planning_type,
                "now_local": local_now.isoformat(),
                "timezone": str(self.gary.timezone),
                "working_hours": self.work_hours.label(),
                "working_days": [WEEKDAY_NAMES[day] for day in sorted(self.work_days)],
                "scheduling_horizon_hours": self.horizon_hours,
                "max_actions": self.max_actions,
                "calendar_available": calendar_error is None,
                "busy_times": [
                    {"start": self._local(b["start"]), "end": self._local(b["end"])} for b in busy
                ],
                "operations": self._localize(operations),
                "planning_notes": notes,
            }

            plan = await self.planner.create_plan(planning_input)

            accepted, rejected = await asyncio.to_thread(
                validate_cycle_actions,
                self.gary,
                plan["actions"],
                now=now,
                busy=busy,
                calendar_available=calendar_error is None,
                work_hours=self.work_hours,
                work_days=self.work_days,
                horizon_hours=self.horizon_hours,
                max_actions=self.max_actions,
            )

            results = []
            for proposal in accepted:
                try:
                    result = await self.gary.actions.propose(
                        ProposeActionRequest(
                            action_type=proposal["action_type"],
                            payload=proposal["payload"],
                            reason=proposal["reason"],
                            project_id=proposal["project_id"],
                            task_id=proposal["task_id"],
                        ),
                        actor=GARY_ACTOR,
                    )
                except ValueError as exc:
                    result = {"status": "invalid", "error": str(exc)}
                results.append({"proposal": proposal, "result": result})

            record = {
                "summary": plan["summary"],
                "briefing": plan["briefing"],
                "proposed_actions": plan["actions"],
                "results": results,
                "rejected": rejected,
                "notes_used": [note["title"] for note in notes],
                "notes_error": notes_error,
                "calendar_error": calendar_error,
            }
            await asyncio.to_thread(self.gary.planning.complete_cycle, run_id, record)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            logger.exception("%s planning cycle failed", planning_type)
            await asyncio.to_thread(self.gary.planning.fail_run, run_id, error)
            raise PlanningCycleError(error) from exc

        summary_error = None
        try:
            await self.notebook.write_daily_summary(
                today,
                render_summary(
                    planning_type, local_now, plan, results, rejected, context, self.gary.timezone
                ),
            )
        except Exception as exc:
            logger.warning("Could not write the daily summary: %s", exc)
            summary_error = str(exc) or type(exc).__name__

        return {
            "planning_run_id": run_id,
            "planning_type": planning_type,
            "briefing": plan["briefing"],
            "results": results,
            "rejected": rejected,
            "summary_error": summary_error,
        }

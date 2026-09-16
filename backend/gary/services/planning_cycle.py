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
from typing import Protocol

from gary.container import Gary
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.policy import GARY_ACTOR
from gary.services.calendar_blocks import (
    WEEKDAY_NAMES,
    WorkHours,
    WorkWeek,
    parse_protected_times,
    parse_weekdays,
    parse_work_hours,
    protected_intervals,
)
from gary.services.readiness import CLOSED_STATUSES, task_readiness
from gary.timeutil import format_utc, parse_timestamp, to_datetime, to_local
from gary.tools.base import TIMESTAMP_FIELDS

logger = logging.getLogger("gary.planning_cycle")

__all__ = [
    "PlanningCycle",
    "PlanningCycleError",
    "WorkHours",
    "WorkWeek",
    "due_planning_types",
    "parse_protected_times",
    "parse_schedule",
    "parse_weekdays",
    "parse_work_hours",
]

SCHEDULED_TYPES = ("morning", "midday", "evening")
CYCLE_TYPES = ("morning", "midday", "evening", "manual", "event_triggered")
CYCLE_ACTION_TYPES = ("schedule_task", "move_calendar_event", "create_followup")
CATCH_UP_MINUTES = 90
MIN_LEAD_MINUTES = 10
MIN_BLOCK_MINUTES = 15
MAX_BLOCK_MINUTES = 240
# Moves smaller than this are tiny deviations, not worth churning the calendar.
MIN_MOVE_MINUTES = 30
FOLLOWUP_HORIZON_DAYS = 7
MAX_RELEVANT_NOTES = 5
PREFERENCES_NOTE_TITLE = "Preferences"
DAILY_SUMMARY_PREFIX = "Daily summary "
# Which brief each cycle type is built on.
BRIEF_KIND = {
    "morning": "morning",
    "midday": "midday",
    "evening": "evening",
    "manual": "midday",
    "event_triggered": "midday",
}
SUMMARY_TITLE = {"manual": "Replan", "event_triggered": "Replan after a change"}


class Planner(Protocol):
    async def create_plan(self, planning_input: dict) -> dict: ...


class NotebookService(Protocol):
    async def get_relevant_notes(self, project_names: list[str], today: dt.date) -> list[dict]: ...

    async def write_daily_summary(self, day: dt.date, markdown: str) -> None: ...


class BusyCalendar(Protocol):
    async def busy_intervals(self, start: str, end: str) -> list[dict]: ...


class EmailSource(Protocol):
    async def unread_summaries(self) -> list[dict]: ...


# ---------------------------------------------------------------- settings

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
    """Planning notes titled like an active project, or "Preferences"."""
    wanted = {normalize_title(name) for name in project_names}
    wanted.add(normalize_title(PREFERENCES_NOTE_TITLE))
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
    week: WorkWeek,
    horizon_hours: int,
    max_actions: int,
) -> tuple[list[dict], list[dict]]:
    """Deterministic checks on every proposed action. Returns (accepted
    proposals ready for the action service, rejected with reasons)."""
    accepted, rejected, planned = [], [], []
    seen_tasks, seen_followups = set(), set()
    earliest = format_utc(to_datetime(now) + dt.timedelta(minutes=MIN_LEAD_MINUTES))
    latest = format_utc(to_datetime(now) + dt.timedelta(hours=horizon_hours))
    followup_latest = format_utc(to_datetime(now) + dt.timedelta(days=FOLLOWUP_HORIZON_DAYS))
    protected = protected_intervals(now, latest, week, gary.timezone)

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        pending_followups = {
            normalize_title(f["title"]) for f in repos.followups.list_pending()
        }

        for proposal in actions:
            def reject(reason: str):
                rejected.append({"proposal": proposal, "reason": reason})

            action_type = proposal.get("action_type")
            if action_type not in CYCLE_ACTION_TYPES:
                reject(f"{action_type} is not allowed in a planning cycle")
                continue
            if len(accepted) >= max_actions:
                reject(f"more than {max_actions} actions proposed")
                continue

            task_id = proposal.get("task_id") or None
            task = repos.tasks.get(task_id) if isinstance(task_id, str) else None

            if action_type == "create_followup":
                title = " ".join(str(proposal.get("title") or "").split())[:300]
                if not title:
                    reject("a follow-up needs a title")
                    continue
                if task_id and task is None:
                    reject("unknown task")
                    continue
                try:
                    due_at = parse_timestamp(proposal.get("due_at"), "due_at")
                except ValueError as exc:
                    reject(str(exc))
                    continue
                if due_at < now or due_at > followup_latest:
                    reject(f"follow-ups must be due within {FOLLOWUP_HORIZON_DAYS} days")
                    continue
                if normalize_title(title) in pending_followups | seen_followups:
                    reject("a pending follow-up with that title already exists")
                    continue
                payload = {"title": title, "due_at": due_at}
                if task:
                    payload["task_id"] = task["id"]
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": payload,
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": task["id"] if task else None,
                        "project_id": task["project_id"] if task else None,
                        "task_title": title,
                    }
                )
                seen_followups.add(normalize_title(title))
                continue

            if not calendar_available:
                reject("the calendar could not be read, so nothing can be scheduled")
                continue
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
            else:
                if not task["calendar_event_id"]:
                    reject("the task is not on the calendar")
                    continue
                shift_minutes = abs(
                    (to_datetime(start) - to_datetime(task["scheduled_start"])).total_seconds()
                ) / 60
                if shift_minutes < MIN_MOVE_MINUTES:
                    reject(
                        f"moves under {MIN_MOVE_MINUTES} minutes are not worth "
                        "changing the calendar"
                    )
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
            day_end = midnight + dt.timedelta(hours=week.hours.end)
            if (
                local_start.weekday() not in week.days
                or local_start.hour < week.hours.start
                or local_end > day_end
            ):
                reject(f"outside working hours ({week.hours.label()} on working days)")
                continue
            if overlaps(start, end, protected):
                reject("overlaps protected time such as a meal break")
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


# -------------------------------------------------------------------- cycle

class PlanningCycleError(RuntimeError):
    pass


def action_line(item: dict) -> str:
    result = item["result"]
    status = result.get("status", "error").replace("_", " ")
    proposal = item["proposal"]
    detail = result.get("summary") or f"{proposal['action_type']}: {proposal['task_title']}"
    error = f": {result['error']}" if result.get("error") else ""
    return f"{detail} ({status}{error})"


class PlanningCycle:
    def __init__(
        self,
        gary: Gary,
        planner: Planner,
        notebook: NotebookService,
        calendar: BusyCalendar,
        email: EmailSource | None = None,
        *,
        max_actions: int = 5,
        horizon_hours: int = 72,
    ):
        self.gary = gary
        self.planner = planner
        self.notebook = notebook
        self.calendar = calendar
        self.email = email
        self.max_actions = max_actions
        self.horizon_hours = horizon_hours
        # One cycle at a time, even if a scheduled and a requested run collide.
        self._lock = asyncio.Lock()

    def _localize(self, value):
        if isinstance(value, list):
            return [self._localize(item) for item in value]
        if isinstance(value, dict):
            return {
                key: (
                    to_local(item, self.gary.timezone)
                    if key in TIMESTAMP_FIELDS and isinstance(item, str)
                    else self._localize(item)
                )
                for key, item in value.items()
                if not key.endswith("_json")
            }
        return value

    async def minutes_since_last_cycle(self) -> float | None:
        latest = await asyncio.to_thread(self.gary.planning.last_cycle_finished_at)
        if latest is None:
            return None
        now = to_datetime(format_utc(self.gary.planning.clock()))
        return (now - to_datetime(latest)).total_seconds() / 60

    async def run(self, planning_type: str, min_gap_minutes: float = 0) -> dict:
        if planning_type not in CYCLE_TYPES:
            raise ValueError(f"planning_type must be one of {CYCLE_TYPES}")
        async with self._lock:
            if min_gap_minutes:
                since = await self.minutes_since_last_cycle()
                if since is not None and since < min_gap_minutes:
                    raise PlanningCycleError(
                        f"A planning cycle ran {int(since)} minutes ago; wait "
                        f"{int(min_gap_minutes - since) + 1} more minutes"
                    )
            return await self._run(planning_type)

    async def _gather(self, coroutine, label: str):
        try:
            return await coroutine, None
        except Exception as exc:
            logger.warning("%s unavailable for planning: %s", label, exc)
            return None, str(exc) or type(exc).__name__

    async def _run(self, planning_type: str) -> dict:
        context = await asyncio.to_thread(self.gary.planning.get_planning_context, planning_type)
        brief = await asyncio.to_thread(self.gary.briefing.build, BRIEF_KIND[planning_type])
        run_id = context["planning_run_id"]
        now = context["now"]
        local_now = to_datetime(now).astimezone(self.gary.timezone)
        today = local_now.date()

        try:
            horizon_end = format_utc(to_datetime(now) + dt.timedelta(hours=self.horizon_hours))
            notes, notes_error = await self._gather(
                self.notebook.get_relevant_notes(
                    [p["name"] for p in context["active_projects"]], today
                ),
                "Planning notes",
            )
            busy, calendar_error = await self._gather(
                self.calendar.busy_intervals(now, horizon_end), "Calendar"
            )
            emails, email_error = (
                await self._gather(self.email.unread_summaries(), "Email")
                if self.email
                else ([], None)
            )
            notes, busy, emails = notes or [], busy or [], emails or []

            operations = {key: value for key, value in context.items() if key != "planning_run_id"}
            planning_input = {
                "planning_type": planning_type,
                "now_local": local_now.isoformat(),
                "timezone": str(self.gary.timezone),
                **self.gary.week.describe(),
                "scheduling_horizon_hours": self.horizon_hours,
                "max_actions": self.max_actions,
                "calendar_available": calendar_error is None,
                "busy_times": [
                    {
                        "start": to_local(b["start"], self.gary.timezone),
                        "end": to_local(b["end"], self.gary.timezone),
                    }
                    for b in busy
                ],
                "brief": self._localize(brief),
                "operations": self._localize(operations),
                "planning_notes": notes,
                "unread_email": emails,
            }

            plan = await self.planner.create_plan(planning_input)

            accepted, rejected = await asyncio.to_thread(
                validate_cycle_actions,
                self.gary,
                plan["actions"],
                now=now,
                busy=busy,
                calendar_available=calendar_error is None,
                week=self.gary.week,
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
                "emails_seen": len(emails),
                "notes_error": notes_error,
                "calendar_error": calendar_error,
                "email_error": email_error,
            }
            await asyncio.to_thread(self.gary.planning.complete_cycle, run_id, record)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            logger.exception("%s planning cycle failed", planning_type)
            await asyncio.to_thread(self.gary.planning.fail_run, run_id, error)
            raise PlanningCycleError(error) from exc

        # The brief after actions ran, so the note shows the resulting schedule.
        final_brief = await asyncio.to_thread(self.gary.briefing.build, BRIEF_KIND[planning_type])
        markdown = self.gary.briefing.render_markdown(
            final_brief,
            plan_summary=plan["summary"],
            actions=[action_line(item) for item in results],
            rejected=[
                f"{item['proposal'].get('action_type')}: {item['reason']}" for item in rejected
            ],
        )
        if planning_type in SUMMARY_TITLE:
            _, _, rest = markdown.partition("\n")
            markdown = f"## {SUMMARY_TITLE[planning_type]}, {local_now.strftime('%-I:%M %p')}\n{rest}"

        summary_error = None
        try:
            await self.notebook.write_daily_summary(today, markdown)
        except Exception as exc:
            logger.warning("Could not write the daily summary: %s", exc)
            summary_error = str(exc) or type(exc).__name__

        return {
            "planning_run_id": run_id,
            "planning_type": planning_type,
            "briefing": plan["briefing"],
            "summary": plan["summary"],
            "brief": final_brief,
            "results": results,
            "rejected": rejected,
            "summary_error": summary_error,
        }

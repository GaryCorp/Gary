"""Gary as Alex's manager: a morning assignment, an evening check-in.

Every morning Gary tells Alex what today is for: what is on the calendar,
the few ready tasks that matter most, and anything whose deadline is close.
He says it out loud and emails it, so it reaches Alex away from the desk.
Every evening he asks about whatever he assigned that is not done yet, and
the answer is how the task list catches up with reality.

All of it is arithmetic over SQLite, with no model call. Which tasks matter
is the planning score everything else already uses, so the assignment and
the planner can never disagree about priorities. The day's assignment is
recorded in the audit log, keyed by date, which is what the evening check-in
reads and what stops either happening twice.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.policy import GARY_ACTOR
from gary.services.calendar_blocks import WorkWeek
from gary.services.common import Clock, clock_now, default_clock
from gary.services.planning_service import collect_operations
from gary.services.readiness import CLOSED_STATUSES
from gary.timeutil import format_utc, to_datetime

FOCUS_LIMIT = 3
AT_RISK_HOURS = 48
ASSIGNED_EVENT = "daily_assignment_given"
CHECKIN_EVENT = "daily_checkin_asked"


def _spoken_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


class Accountability:
    def __init__(
        self,
        db: Database,
        actions,
        timezone: ZoneInfo,
        week: WorkWeek,
        clock: Clock = default_clock,
        principal: str = "Alex",
    ):
        self.db = db
        self.actions = actions
        self.timezone = timezone
        self.week = week
        self.clock = clock
        self.principal = principal

    def _local(self, value: str) -> dt.datetime:
        return to_datetime(value).astimezone(self.timezone)

    def _clock(self, value: str) -> str:
        return self._local(value).strftime("%-I:%M %p")

    def _day(self, value: str) -> str:
        return self._local(value).strftime("%A %-I:%M %p")

    # ------------------------------------------------------------- morning

    def assignment(self) -> dict:
        """Today's assignment as data. Pure read; nothing is recorded."""
        now = clock_now(self.clock)
        today = self._local(now).date()
        day_start = format_utc(dt.datetime.combine(today, dt.time(0, 0), tzinfo=self.timezone))
        day_end = format_utc(
            dt.datetime.combine(today + dt.timedelta(days=1), dt.time(0, 0), tzinfo=self.timezone)
        )
        at_risk_until = format_utc(to_datetime(now) + dt.timedelta(hours=AT_RISK_HOURS))
        working_day = today.weekday() in self.week.days

        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            blocks = [
                t for t in repos.tasks.list_scheduled_between(day_start, day_end)
                if t["status"] not in CLOSED_STATUSES
            ]
            operations = collect_operations(repos, now)
            due_soon = repos.tasks.list_deadlines_between(now, at_risk_until)
            overdue = repos.tasks.list_overdue(now)

        on_calendar = {t["id"] for t in blocks}
        # Off days are for what is already booked, such as a Saturday shoot;
        # nothing else is handed out.
        focus = [] if not working_day else [
            t for t in operations["ready_tasks"] if t["task_id"] not in on_calendar
        ][:FOCUS_LIMIT]
        # Warnings are for what is not already in today's assignment, so
        # nothing is said twice.
        assigned = on_calendar | {t["task_id"] for t in focus}
        at_risk = [
            t for t in due_soon
            if t["status"] != "in_progress" and t["id"] not in assigned
        ] if working_day else []
        overdue = [t for t in overdue if t["id"] not in assigned]

        return {
            "date": today.isoformat(),
            "weekday": today.strftime("%A %B %-d"),
            "calendar": [
                {"task_id": t["id"], "title": t["title"], "start": t["scheduled_start"],
                 "end": t["scheduled_end"]}
                for t in blocks
            ],
            "focus": [
                {"task_id": t["task_id"], "title": t["title"], "deadline": t["deadline"],
                 "estimated_minutes": t["estimated_minutes"]}
                for t in focus
            ],
            "at_risk": [
                {"task_id": t["id"], "title": t["title"], "deadline": t["deadline"]}
                for t in at_risk
            ],
            "overdue": [
                {"task_id": t["id"], "title": t["title"], "deadline": t["deadline"]}
                for t in overdue
            ],
        }

    @staticmethod
    def assigned_ids(assignment: dict) -> list[str]:
        """What Alex is being held to today: the calendar and the focus list.
        At-risk and overdue items are warnings, not new assignments."""
        ids = [t["task_id"] for t in assignment["calendar"]]
        ids += [t["task_id"] for t in assignment["focus"] if t["task_id"] not in ids]
        return ids

    def spoken_assignment(self, assignment: dict) -> str:
        parts = [f"Good morning {self.principal}. Here is today."]
        if assignment["calendar"]:
            booked = [f"{t['title']} at {self._clock(t['start'])}" for t in assignment["calendar"]]
            parts.append(f"On your calendar: {_spoken_list(booked)}.")
        if assignment["focus"]:
            parts.append(
                "Your priorities: "
                + _spoken_list([t["title"] for t in assignment["focus"]]) + "."
            )
        if assignment["at_risk"]:
            due = [f"{t['title']} by {self._day(t['deadline'])}" for t in assignment["at_risk"][:3]]
            parts.append(f"Due soon and not started: {_spoken_list(due)}.")
        if assignment["overdue"]:
            late = [t["title"] for t in assignment["overdue"][:3]]
            parts.append(f"Already overdue: {_spoken_list(late)}.")
        parts.append("Tell me as you finish each one.")
        return " ".join(parts)

    def email_assignment(self, assignment: dict) -> dict:
        def when(value: str | None) -> str:
            return self._local(value).strftime("%a %b %-d, %-I:%M %p") if value else "no deadline"

        lines = [f"Good morning {self.principal},", "", "Here is today.", ""]
        if assignment["calendar"]:
            lines.append("On your calendar")
            lines += [
                f"  {self._clock(t['start'])}-{self._clock(t['end'])}  {t['title']}"
                for t in assignment["calendar"]
            ]
            lines.append("")
        if assignment["focus"]:
            lines.append("Your priorities")
            for n, t in enumerate(assignment["focus"], 1):
                length = f", about {t['estimated_minutes']} min" if t["estimated_minutes"] else ""
                lines.append(f"  {n}. {t['title']} (due {when(t['deadline'])}{length})")
            lines.append("")
        if assignment["at_risk"]:
            lines.append("Due soon and not started")
            lines += [f"  - {t['title']} (due {when(t['deadline'])})" for t in assignment["at_risk"]]
            lines.append("")
        if assignment["overdue"]:
            lines.append("Overdue")
            lines += [f"  - {t['title']} (was due {when(t['deadline'])})" for t in assignment["overdue"]]
            lines.append("")
        lines += [
            "Tell me as you finish each one, or close its GitHub issue.",
            "I will check in with you this evening.",
            "",
            "Gary",
        ]
        return {"subject": f"Today's assignment: {assignment['weekday']}", "body": "\n".join(lines)}

    async def morning(self) -> dict | None:
        """Give today's assignment once: record it, email it, and return the
        words to say. None when it was already given or there is nothing to
        give."""
        assignment = self.assignment()
        assigned = self.assigned_ids(assignment)
        if not (assigned or assignment["at_risk"] or assignment["overdue"]):
            return None
        if not self._record_once(ASSIGNED_EVENT, assignment["date"], {
            "assigned_task_ids": assigned,
            "at_risk_task_ids": [t["task_id"] for t in assignment["at_risk"]],
            "overdue_task_ids": [t["task_id"] for t in assignment["overdue"]],
        }):
            return None

        email = self.email_assignment(assignment)
        emailed = await self.actions.propose(
            ProposeActionRequest(
                action_type="email_principal",
                payload=email,
                reason="The morning assignment",
            ),
            actor=GARY_ACTOR,
        )
        return {
            "spoken": self.spoken_assignment(assignment),
            "assigned": assigned,
            "email_status": emailed.get("status"),
            "email_error": emailed.get("error"),
        }

    # ------------------------------------------------------------- evening

    def evening(self) -> dict | None:
        """The check-in on today's assignment, once. Returns what to say and
        whether it is a question, or None when there was no assignment or the
        check-in already happened."""
        now = clock_now(self.clock)
        today = self._local(now).date().isoformat()
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            given = next(
                (e for e in repos.audit.list_for_entity("assignment", today)
                 if e["event_type"] == ASSIGNED_EVENT),
                None,
            )
            if given is None or repos.audit.has_event(CHECKIN_EVENT, today):
                return None
            assigned = json.loads(given["details_json"] or "{}").get("assigned_task_ids", [])
            tasks = [repos.tasks.get(task_id) for task_id in assigned]
        tasks = [t for t in tasks if t is not None]
        open_tasks = [t for t in tasks if t["status"] not in CLOSED_STATUSES]
        done = len(tasks) - len(open_tasks)

        if not self._record_once(CHECKIN_EVENT, today, {
            "assigned": len(tasks),
            "done": done,
            "open_task_ids": [t["id"] for t in open_tasks],
        }):
            return None
        if not open_tasks:
            return {
                "spoken": "Everything I gave you today is done. Good work.",
                "question": False,
                "open": [],
            }
        titles = _spoken_list([t["title"] for t in open_tasks[:4]])
        return {
            "spoken": (
                f"Checking in before you stop. {done} of {len(tasks)} done today. "
                f"Did you finish {titles}? Tell me which are done, and I will "
                "replan the rest."
            ),
            "question": True,
            "open": [t["id"] for t in open_tasks],
        }

    def _record_once(self, event: str, day: str, details: dict) -> bool:
        """Write the day's event unless it exists. True if this call wrote it."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            if repos.audit.has_event(event, day):
                return False
            summary = (
                f"Gave {self.principal} the assignment for {day}"
                if event == ASSIGNED_EVENT
                else f"Checked in with {self.principal} on {day}"
            )
            repos.audit.write(GARY_ACTOR, event, summary, "assignment", day, details, now=now)
        return True

    # -------------------------------------------------------------- weekly

    def week_summary(self, days: int = 7) -> dict:
        """How Alex did against what he was given: for the weekly review."""
        now = clock_now(self.clock)
        since = format_utc(to_datetime(now) - dt.timedelta(days=days))
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            checkins = [
                json.loads(e["details_json"] or "{}")
                for e in repos.audit.list_since(CHECKIN_EVENT, since)
            ]
            overdue = repos.tasks.list_overdue(now)
        assigned = sum(c.get("assigned", 0) for c in checkins)
        done = sum(c.get("done", 0) for c in checkins)
        return {
            "days_checked_in": len(checkins),
            "assigned": assigned,
            "done_same_day": done,
            "overdue_now": len(overdue),
        }

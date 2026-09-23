"""The day's report, emailed to Alex.

A week of GaryCorp running itself is only worth anything if Alex can see what
it did without being at the machine. This is that: what ran, what it cost,
what is waiting for him, and what is stuck, assembled from SQLite with no
model call, so it still arrives on the day the spend ceiling stopped
everything else.

It reports; it never decides. Anything needing Alex is listed under what
needs you, with where to go and answer it.
"""

import datetime as dt

from gary.db import Database
from gary.db.repositories import Repositories
from gary.policy import GARY_ACTOR
from gary.services.common import Clock, clock_now, default_clock
from gary.services.planning_service import company_health
from gary.timeutil import format_utc, to_datetime, to_local

REPORT_EVENT = "daily_report_sent"
LIST_LIMIT = 8


class DailyReport:
    def __init__(
        self,
        db: Database,
        timezone,
        usage=None,
        operating=None,
        clock: Clock = default_clock,
    ):
        self.db = db
        self.timezone = timezone
        self.usage = usage
        self.operating = operating
        self.clock = clock

    def _local(self, value: str | None) -> str:
        return to_local(value, self.timezone)[:16].replace("T", " ") if value else ""

    def collect(self, hours: int = 24) -> dict:
        now = clock_now(self.clock)
        since = format_utc(to_datetime(now) - dt.timedelta(hours=hours))
        end = format_utc(to_datetime(now) + dt.timedelta(seconds=1))

        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            completed = repos.tasks.list_completed_between(since, end)
            actions = repos.actions.list_recent(since, 100)
            assignments = [
                a for a in repos.assignments.list_recent(None, None, 50)
                if (a["created_at"] or "") >= since
            ]
            cycles = [
                r for r in repos.planning_runs.list_recent(50)
                if (r["started_at"] or "") >= since
            ]
            pending_approvals = repos.approvals.list_pending()
            questions = [
                m for m in repos.spoken.list_open() if m["expects_reply"]
            ]
            scheduled = repos.tasks.list_scheduled_between(now, end_of_tomorrow(now, self.timezone))
            health = company_health(repos, now)

        return {
            "since": since,
            "to": now,
            "paused": self.operating.state() if self.operating else None,
            "cycles": len(cycles),
            "completed_tasks": completed,
            "actions_succeeded": sum(1 for a in actions if a["status"] == "succeeded"),
            "actions_failed": [a for a in actions if a["status"] == "failed"],
            "assignments": assignments,
            "pending_approvals": pending_approvals,
            "open_questions": questions,
            "next_up": scheduled,
            "health": health,
            "spend": self._spend(),
        }

    def _spend(self) -> dict:
        if self.usage is None:
            return {"measured": False}
        today = self.usage.spent_today()
        return {
            "measured": not today.get("unpriced_calls"),
            "cost_usd": today.get("cost_usd") or 0,
            "calls": today.get("calls") or 0,
        }

    # --------------------------------------------------------------- render

    def render_email(self, data: dict) -> dict:
        day = to_datetime(data["to"]).astimezone(self.timezone)
        lines = [f"GaryCorp, {day.strftime('%A %B %-d')}", ""]

        paused = data["paused"]
        if paused and paused["paused"]:
            lines += [
                "PAUSED. Nothing unattended is running; reply \"resume\" to start again.",
                f"Paused {self._local(paused['changed_at'])} by {paused['changed_by']}.",
                "",
            ]

        needs_you = [
            f"- Approval waiting: {a['summary'][:120]} (localhost:8000/approvals)"
            for a in data["pending_approvals"][:LIST_LIMIT]
        ] + [
            f"- Question unanswered: {m['text'][:120]}"
            for m in data["open_questions"][:LIST_LIMIT]
        ] + [
            f"- Action failed: {a['action_type']} — {(a['error_message'] or '')[:100]}"
            for a in data["actions_failed"][:LIST_LIMIT]
        ]
        health = data["health"]
        if not health["healthy"]:
            needs_you += [
                f"- {count} {name.replace('_', ' ')}"
                for name, count in health["counts"].items()
                if count
            ]
        lines += ["What needs you"] + (needs_you or ["- Nothing."]) + [""]

        lines += ["Done today"] + (
            [f"- {t['title']}" for t in data["completed_tasks"][:LIST_LIMIT]] or ["- Nothing."]
        ) + [""]

        reports = [a for a in data["assignments"] if a["status"] == "completed"]
        lines += [
            "The company",
            f"- {data['cycles']} planning cycle{'s' if data['cycles'] != 1 else ''}",
            f"- {data['actions_succeeded']} action"
            f"{'s' if data['actions_succeeded'] != 1 else ''} completed,"
            f" {len(data['actions_failed'])} failed",
            f"- {len(reports)} specialist report{'s' if len(reports) != 1 else ''} back",
        ]
        spend = data["spend"]
        lines.append(
            f"- ${spend['cost_usd']:.2f} spent on AI today"
            if spend.get("measured")
            else "- Spending could not be measured today (a model has no price)"
        )
        lines.append("")

        lines += ["Next up"] + (
            [
                f"- {self._local(t['scheduled_start'])}  {t['title']}"
                for t in data["next_up"][:LIST_LIMIT]
            ] or ["- Nothing on the calendar."]
        ) + ["", "Reply \"pause\" to stop everything unattended, \"resume\" to start again.", "", "Gary"]
        return {
            "subject": f"GaryCorp daily report: {day.strftime('%A %B %-d')}",
            "body": "\n".join(lines),
        }

    def spoken_summary(self, data: dict) -> str:
        done = len(data["completed_tasks"])
        waiting = len(data["pending_approvals"]) + len(data["open_questions"])
        parts = [
            f"Today's report is in your inbox. {done} task{'s' if done != 1 else ''} "
            f"finished, {data['cycles']} planning cycle{'s' if data['cycles'] != 1 else ''}."
        ]
        if data["actions_failed"]:
            count = len(data["actions_failed"])
            parts.append(f"{count} action{'s' if count != 1 else ''} failed.")
        if not waiting:
            parts.append("Nothing needs you.")
        elif waiting == 1:
            parts.append("One thing needs you.")
        else:
            parts.append(f"{waiting} things need you.")
        return " ".join(parts)

    # ----------------------------------------------------------------- send

    def sent_today(self) -> bool:
        day = to_datetime(clock_now(self.clock)).astimezone(self.timezone).date().isoformat()
        with self.db.read() as conn:
            return Repositories.bind(conn).audit.has_event(REPORT_EVENT, day)

    def record_sent(self, status: str) -> None:
        now = clock_now(self.clock)
        day = to_datetime(now).astimezone(self.timezone).date().isoformat()
        with self.db.transaction() as conn:
            Repositories.bind(conn).audit.write(
                GARY_ACTOR,
                REPORT_EVENT,
                f"Emailed the daily report for {day}",
                "report",
                day,
                {"email_status": status},
                now=now,
            )


def end_of_tomorrow(now: str, timezone) -> str:
    day = to_datetime(now).astimezone(timezone).date() + dt.timedelta(days=2)
    return format_utc(dt.datetime.combine(day, dt.time(0, 0), tzinfo=timezone))

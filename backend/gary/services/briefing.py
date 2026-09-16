"""Deterministic Chief of Staff briefs.

The morning brief, midday review, and end-of-day review are assembled here
from SQLite, so the facts (what is scheduled, finished, blocked, overdue, at
risk, or waiting for a decision) never depend on the model. Gary or the
planning model only phrases and prioritizes them.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

from gary.db import Database
from gary.db.repositories import Repositories
from gary.services.approval_service import expire_stale_approvals
from gary.services.calendar_blocks import WorkWeek, working_minutes
from gary.services.common import Clock, clock_now, default_clock
from gary.services.planning_service import collect_operations
from gary.services.readiness import CLOSED_STATUSES
from gary.timeutil import format_utc, to_datetime

BRIEF_KINDS = ("morning", "midday", "evening")
DEFAULT_TASK_MINUTES = 60
COMMITMENT_WARNING_HOURS = 48
LIST_LIMIT = 8


def local_day_bounds(now: str, timezone: ZoneInfo, offset_days: int = 0) -> tuple[str, str]:
    day = to_datetime(now).astimezone(timezone).date() + dt.timedelta(days=offset_days)
    start = dt.datetime.combine(day, dt.time(), timezone)
    return format_utc(start), format_utc(start + dt.timedelta(days=1))


def project_capacity(
    project: dict,
    open_tasks: list[dict],
    now: str,
    week: WorkWeek,
    timezone: ZoneInfo,
) -> dict:
    """Estimated remaining work against working time left before the
    deadline. Ignores meetings, so "at_risk" is a conservative signal."""
    remaining = sum(task["estimated_minutes"] or DEFAULT_TASK_MINUTES for task in open_tasks)
    unestimated = sum(1 for task in open_tasks if task["estimated_minutes"] is None)
    if not project["deadline"]:
        status, available = "no_deadline", None
    else:
        available = working_minutes(now, project["deadline"], week, timezone)
        if remaining > available:
            status = "at_risk"
        elif remaining > available * 0.7:
            status = "tight"
        else:
            status = "on_track"
    return {
        "status": status,
        "remaining_minutes": remaining,
        "unestimated_tasks": unestimated,
        "available_working_minutes": available,
    }


class BriefingService:
    def __init__(
        self,
        db: Database,
        timezone: ZoneInfo,
        week: WorkWeek,
        clock: Clock = default_clock,
    ):
        self.db = db
        self.timezone = timezone
        self.week = week
        self.clock = clock

    def _when(self, value: str | None) -> str:
        if not value:
            return ""
        return to_datetime(value).astimezone(self.timezone).strftime("%a %-I:%M %p")

    def _clock_time(self, value: str) -> str:
        return to_datetime(value).astimezone(self.timezone).strftime("%-I:%M %p")

    def build(self, kind: str) -> dict:
        if kind not in BRIEF_KINDS:
            raise ValueError(f"kind must be one of {BRIEF_KINDS}")

        now = clock_now(self.clock)
        today_start, today_end = local_day_bounds(now, self.timezone)
        tomorrow_start, tomorrow_end = local_day_bounds(now, self.timezone, 1)

        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            expire_stale_approvals(repos, now)
            ops = collect_operations(repos, now)
            open_tasks = repos.tasks.list_open()
            projects = repos.projects.list_active()
            scheduled_today = repos.tasks.list_scheduled_between(today_start, today_end)
            scheduled_tomorrow = repos.tasks.list_scheduled_between(tomorrow_start, tomorrow_end)
            completed_today = repos.tasks.list_completed_between(today_start, today_end)
            moved_today = repos.actions.list_executed_between(
                today_start, today_end, "move_calendar_event"
            )
            followups_today = repos.followups.list_created_between(today_start, today_end)
            earlier_runs = [
                run
                for run in repos.planning_runs.list_started_between(today_start, today_end)
                if run["status"] == "completed" and run["plan_json"]
            ]
            moved_ids = [json.loads(a["payload_json"]).get("task_id") for a in moved_today]
            task_titles = {t["id"]: t["title"] for t in repos.tasks.get_many(moved_ids)}

        brief = {
            "kind": kind,
            "now": now,
            "primary_objective": self._primary_objective(ops, projects, open_tasks, now),
            "today": [
                {
                    "title": task["title"],
                    "start": task["scheduled_start"],
                    "end": task["scheduled_end"],
                    "status": task["status"],
                }
                for task in scheduled_today
            ],
            "risks": self._risks(ops, projects, open_tasks, now),
            "decisions_needed": [a["summary"] for a in ops["pending_approvals"]],
            "due_followups": [f["title"] for f in ops["due_followups"]],
            "next_ready_tasks": [t["title"] for t in ops["ready_tasks"][:3]],
        }

        if kind in ("midday", "evening"):
            brief["completed_today"] = [t["title"] for t in completed_today]
            brief["unfinished_today"] = [
                t["title"] for t in scheduled_today if t["status"] not in CLOSED_STATUSES
            ]
            brief["earlier_plans_today"] = [
                {
                    "planning_type": run["planning_type"],
                    "summary": json.loads(run["plan_json"]).get("summary", ""),
                }
                for run in earlier_runs
            ]

        if kind == "evening":
            brief["blocked"] = [
                f"{t['title']}: waiting on {', '.join(t['blocked_by'])}"
                if t["blocked_by"]
                else f"{t['title']}: {t['status']}"
                for t in ops["blocked_tasks"][:LIST_LIMIT]
            ]
            brief["moved_today"] = [
                task_titles.get(json.loads(a["payload_json"]).get("task_id"), "a task")
                for a in moved_today
            ]
            brief["new_followups"] = [f["title"] for f in followups_today]
            brief["tomorrow"] = {
                "scheduled": [
                    {"title": t["title"], "start": t["scheduled_start"], "end": t["scheduled_end"]}
                    for t in scheduled_tomorrow
                ],
                "likely_priority": [t["title"] for t in ops["ready_tasks"][:3]],
            }

        return brief

    def _primary_objective(self, ops, projects, open_tasks, now) -> dict | None:
        ranked = ops["ready_tasks"] + ops["in_progress_tasks"] + ops["blocked_tasks"]
        if not ranked and not projects:
            return None

        by_name = {p["name"]: p for p in projects}
        top = max(ranked, key=lambda t: t["planning_score"]) if ranked else None
        project = by_name.get(top["project"]) if top and top["project"] else None
        if project is None and projects:
            # Most important project, earliest deadline first on ties.
            project = min(projects, key=lambda p: (-p["priority"], p["deadline"] or "~"))

        result = {"next_task": None, "project": None}
        if project:
            project_tasks = [t for t in open_tasks if t["project_id"] == project["id"]]
            ready_in_project = [t for t in ops["ready_tasks"] if t["project"] == project["name"]]
            result["project"] = {
                "name": project["name"],
                "objective": project["objective"],
                "deadline": project["deadline"],
                "capacity": project_capacity(project, project_tasks, now, self.week, self.timezone),
            }
            if ready_in_project:
                result["next_task"] = ready_in_project[0]["title"]
        if result["next_task"] is None and ops["ready_tasks"]:
            result["next_task"] = ops["ready_tasks"][0]["title"]
        return result

    def _risks(self, ops, projects, open_tasks, now) -> list[str]:
        risks = []
        for block in ops["missed_scheduled_blocks"][:LIST_LIMIT]:
            until = self._when(block["scheduled_end"])
            text = f"{block['title']} was scheduled until {until} but is not finished"
            if block["affects"]:
                text += f"; it holds up {', '.join(block['affects'])}"
            risks.append(text + ".")
        for task in ops["overdue_tasks"][:LIST_LIMIT]:
            risks.append(f"{task['title']} is overdue (was due {self._when(task['deadline'])}).")
        blocking = [
            t for t in ops["blocked_tasks"] if t["blocked_by"] and t["priority"] >= 7
        ]
        for task in blocking[:3]:
            risks.append(f"{task['title']} is waiting on {', '.join(task['blocked_by'])}.")
        warning_end = format_utc(to_datetime(now) + dt.timedelta(hours=COMMITMENT_WARNING_HOURS))
        for commitment in ops["open_commitments"]:
            if commitment["deadline"] and commitment["deadline"] <= warning_end:
                who = f" to {commitment['committed_to']}" if commitment["committed_to"] else ""
                state = (
                    "overdue"
                    if commitment["deadline"] < now
                    else f"due {self._when(commitment['deadline'])}"
                )
                risks.append(f"Commitment{who}: {commitment['description']} is {state}.")
        for project in projects:
            project_tasks = [t for t in open_tasks if t["project_id"] == project["id"]]
            capacity = project_capacity(project, project_tasks, now, self.week, self.timezone)
            if capacity["status"] == "at_risk":
                risks.append(
                    f"{project['name']}: about {round(capacity['remaining_minutes'] / 60, 1)} hours "
                    f"of work left and {round(capacity['available_working_minutes'] / 60, 1)} "
                    f"working hours before {self._when(project['deadline'])}."
                )
        return risks

    def render_markdown(
        self,
        brief: dict,
        plan_summary: str = "",
        actions: list[str] | None = None,
        rejected: list[str] | None = None,
    ) -> str:
        """Concise note text for the daily summary."""
        kind = brief["kind"]
        title = {
            "morning": "Morning brief",
            "midday": "Midday review",
            "evening": "Daily summary",
        }[kind]
        local_now = to_datetime(brief["now"]).astimezone(self.timezone)
        lines = [f"## {title}, {local_now.strftime('%-I:%M %p')}", ""]

        def section(name: str, items: list[str], empty: str | None = None):
            if not items and empty is None:
                return
            lines.append(f"**{name}:**" + ("" if items else f" {empty}"))
            lines.extend(f"- {item}" for item in items)
            lines.append("")

        objective = brief["primary_objective"]
        if objective:
            parts = []
            if objective["project"]:
                project = objective["project"]
                due = ""
                if project["deadline"]:
                    status = project["capacity"]["status"].replace("_", " ")
                    due = f" (due {self._when(project['deadline'])}, {status})"
                parts.append(f"{project['name']}{due}")
            if objective["next_task"]:
                parts.append(f"next: {objective['next_task']}")
            lines += [f"**Primary objective:** {'; '.join(parts)}", ""]

        if kind == "evening":
            section("Completed", brief["completed_today"], "nothing marked done")
            section("Incomplete", brief["unfinished_today"])
            section("Blocked", brief["blocked"])
            section("Moved", brief["moved_today"])
            section("New follow-ups", brief["new_followups"])
        else:
            section(
                "Today",
                [
                    f"{self._clock_time(item['start'])}-{self._clock_time(item['end'])} {item['title']}"
                    + ("" if item["status"] not in CLOSED_STATUSES else f" ({item['status']})")
                    for item in brief["today"]
                ],
                "nothing scheduled",
            )
            if kind == "midday":
                section("Finished so far", brief["completed_today"])
                section("Remaining today", brief["unfinished_today"])

        if plan_summary:
            lines += [f"**Plan:** {plan_summary}", ""]
        section("Actions", actions or [])
        section("Not accepted", rejected or [])
        section("Risk", brief["risks"], "none")
        section("Decision needed", brief["decisions_needed"], "none")
        section("Follow-ups due", brief["due_followups"])

        if kind == "evening":
            tomorrow = brief["tomorrow"]
            items = [
                f"{self._clock_time(item['start'])}-{self._clock_time(item['end'])} {item['title']}"
                for item in tomorrow["scheduled"]
            ]
            if tomorrow["likely_priority"]:
                items.append(f"Likely priority: {', '.join(tomorrow['likely_priority'])}")
            section("Tomorrow", items, "nothing planned yet")

        return "\n".join(lines).rstrip()

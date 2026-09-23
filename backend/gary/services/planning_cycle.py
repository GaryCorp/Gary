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
from gary.models.conversation import MESSAGE_MAX, MESSAGE_MIN, spoken_text
from gary.models.reorg import CHANGE_KINDS
from gary.models.reorg import MAX_CHANGES as MAX_REORG_CHANGES
from gary.services.conversation_service import quiet_since
from gary.services.common import (
    looks_like_repeat,
    normalize_title,
    objective_key,
    task_readiness_in,
)
from gary.services.readiness import CLOSED_STATUSES
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
CYCLE_TYPES = (
    "morning",
    "midday",
    "evening",
    "manual",
    "event_triggered",
    # The continuous loop between the scheduled cycles: short, reactive, and
    # only run when something actually changed.
    "management",
)
CYCLE_ACTION_TYPES = (
    "schedule_task",
    "move_calendar_event",
    "create_followup",
    "create_internal_task",
    "delegate_to_agent",
    "run_management_review",
    # Gary deciding the cycle found something Alex himself needs to hear.
    "ask_user",
    # Gary running Alex's engineering queue between conversations.
    "create_engineering_ticket",
    "set_engineering_priority",
    # Gary arguing that the company is organised wrongly.
    "propose_reorganisation",
    # Gary arguing the company is missing someone. Yellow, approved on the
    # web page, and approving it only files a ticket: the colleague exists
    # when Alex has written them into roster.py and deployed.
    "hire_employee",
)
# Caps for unattended cycles. The company runs itself between conversations,
# so the limits are what stops a cycle commissioning work all day: a cycle
# may start a little, a day may start a little more.
MAX_CYCLE_TASKS = 3
MAX_CYCLE_DELEGATIONS = 2
MAX_CYCLE_REVIEWS = 1
# A cycle may interrupt Alex about one thing, not several. The rest keeps
# until the next briefing.
MAX_CYCLE_QUESTIONS = 1
# How much engineering work Gary may put on Alex unattended. A backlog should
# build over a week; a stuck state must not fill it in an afternoon.
MAX_CYCLE_TICKETS = 1
MAX_DAILY_TICKETS = 3
# Reshuffling the queue is cheaper than adding to it, but not free.
MAX_CYCLE_PRIORITY_CHANGES = 2
# A reorganisation is one per cycle, and not again for a fortnight: a company
# that reshuffles itself weekly is malfunctioning, not managing.
MAX_CYCLE_REORGS = 1
REORG_QUIET_DAYS = 14
# Hiring is the same kind of argument and gets the same restraint: one at a
# time, and a fortnight between proposals. A company that proposes a new
# colleague every day is not noticing gaps, it is generating them.
MAX_CYCLE_HIRES = 1
HIRE_QUIET_DAYS = 14
MAX_DAILY_DELEGATIONS = 4
MAX_DAILY_REVIEWS = 1
# An objective close enough to recent work for the same specialist is a loop,
# not a new question.
REPEAT_ASSIGNMENT_DAYS = 3
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
    "management": "midday",
}
SUMMARY_TITLE = {
    "manual": "Replan",
    "event_triggered": "Replan after a change",
    "management": "Management check",
}


class Planner(Protocol):
    async def create_plan(self, planning_input: dict) -> dict: ...


class NotebookService(Protocol):
    async def get_relevant_notes(self, project_names: list[str], today: dt.date) -> list[dict]: ...

    async def write_daily_summary(self, day: dt.date, markdown: str) -> None: ...


class BusyCalendar(Protocol):
    async def busy_intervals(self, start: str, end: str) -> list[dict]: ...


class EmailSource(Protocol):
    async def unread_summaries(self) -> list[dict]: ...


class TeamSource(Protocol):
    """The specialist team, as a planning cycle sees it: who exists, who is
    busy, and which reports have come back."""

    def team(self) -> dict: ...

    def list_assignments(self, agent_id: str | None = None, status: str | None = None,
                         limit: int = 10) -> list[dict]: ...


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


TICKET_PRIORITIES = ("P0", "P1", "P2", "P3")


def _bounded_lines(value, limit: int = 15, length: int = 500) -> list[str]:
    """Clean a proposed list of requirements or acceptance criteria."""
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        text = " ".join(str(item).split())[:length]
        if text:
            lines.append(text)
    return lines[:limit]


def _bounded_priority(value) -> int:
    return min(10, max(1, value)) if isinstance(value, int) else 5


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
    team: dict | None = None,
    day_start: str = "",
) -> tuple[list[dict], list[dict]]:
    """Deterministic checks on every proposed action. Returns (accepted
    proposals ready for the action service, rejected with reasons)."""
    accepted, rejected, planned = [], [], []
    seen_tasks, seen_followups = set(), set()
    seen_titles: set[str] = set()
    seen_messages: list[frozenset[str]] = []
    counts = {
        "create_internal_task": 0,
        "delegate_to_agent": 0,
        "run_management_review": 0,
        "ask_user": 0,
        "create_engineering_ticket": 0,
        "set_engineering_priority": 0,
        "hire_employee": 0,
        "propose_reorganisation": 0,
    }
    delegated_agents: set[str] = set()
    earliest = format_utc(to_datetime(now) + dt.timedelta(minutes=MIN_LEAD_MINUTES))
    latest = format_utc(to_datetime(now) + dt.timedelta(hours=horizon_hours))
    followup_latest = format_utc(to_datetime(now) + dt.timedelta(days=FOLLOWUP_HORIZON_DAYS))
    protected = protected_intervals(now, latest, week, gary.timezone)

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        pending_followups = {
            normalize_title(f["title"]) for f in repos.followups.list_pending()
        }
        open_task_titles = {normalize_title(t["title"]) for t in repos.tasks.list_open()}
        # What Gary has already put to Alex, so a cycle cannot ask it again:
        # still waiting on an answer, or settled in the last few days.
        open_message_keys = [
            objective_key(m["topic_key"]) for m in repos.spoken.list_open()
        ] + [
            objective_key(topic)
            for topic in repos.spoken.answered_topic_keys(quiet_since(now))
        ]
        # Alex's engineering queue, and how much of it Gary opened today.
        tickets_by_task = {
            ticket["task_id"]: ticket for ticket in repos.engineering.list_all(100)
        }
        tickets_by_id = {ticket["id"]: ticket for ticket in tickets_by_task.values()}
        tickets_today = repos.engineering.count_created_since(day_start) if day_start else 0
        seen_tickets: set[str] = set()
        # When the company was last reshuffled, so it is not done again yet.
        reorg_since = format_utc(to_datetime(now) - dt.timedelta(days=REORG_QUIET_DAYS))
        recent_actions = repos.actions.list_recent(reorg_since, 200)
        reorganised_recently = any(
            action["action_type"] == "propose_reorganisation"
            and action["status"] not in ("rejected", "failed")
            for action in recent_actions
        )
        hire_since = format_utc(to_datetime(now) - dt.timedelta(days=HIRE_QUIET_DAYS))
        hired_recently = any(
            action["action_type"] == "hire_employee"
            and action["status"] not in ("rejected", "failed")
            and (action["created_at"] or "") >= hire_since
            for action in recent_actions
        )
        # A hire Alex has not answered yet: proposing another is noise.
        hire_awaiting_alex = any(
            approval["action_type"] == "hire_employee"
            for approval in repos.approvals.list_pending()
        )
        # What the team is already doing, so a cycle cannot re-commission it.
        since = format_utc(to_datetime(now) - dt.timedelta(days=REPEAT_ASSIGNMENT_DAYS))
        recent_assignments: dict[str, list[frozenset[str]]] = {}
        busy_agents: set[str] = set()
        daily_delegations = daily_reviews = 0
        for row in repos.assignments.list_recent(None, None, 60):
            agent_id = row["assigned_to"]
            if row["status"] in ("queued", "running"):
                busy_agents.add(agent_id)
            if row["created_at"] >= since:
                recent_assignments.setdefault(agent_id, []).append(objective_key(row["objective"]))
            if row["created_at"] >= day_start:
                if row["review_id"]:
                    daily_reviews += 1
                else:
                    daily_delegations += 1
        if team is not None:
            known_agents = set(team.get("employee_ids") or [])
        else:
            known_agents = set()

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

            if action_type == "create_internal_task":
                title = " ".join(str(proposal.get("title") or "").split())[:300]
                if not title:
                    reject("a task needs a title")
                    continue
                if counts[action_type] >= MAX_CYCLE_TASKS:
                    reject(f"at most {MAX_CYCLE_TASKS} new tasks per cycle")
                    continue
                key = normalize_title(title)
                if key in open_task_titles | seen_titles:
                    reject("an open task with that title already exists")
                    continue
                project_id = proposal.get("project_id") or None
                if project_id and repos.projects.get(project_id) is None:
                    reject("unknown project")
                    continue
                payload = {"title": title, "priority": _bounded_priority(proposal.get("priority"))}
                if project_id:
                    payload["project_id"] = project_id
                minutes = proposal.get("estimated_minutes")
                if isinstance(minutes, int) and 0 < minutes <= 100_000:
                    payload["estimated_minutes"] = minutes
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": payload,
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": None,
                        "project_id": project_id,
                        "task_title": title,
                    }
                )
                seen_titles.add(key)
                counts[action_type] += 1
                continue

            if action_type in ("delegate_to_agent", "run_management_review"):
                if team is None:
                    reject("the GaryCorp team is not available in this deployment")
                    continue
                if action_type == "delegate_to_agent":
                    agent_id = str(proposal.get("agent_id") or "").strip().lower()
                    objective = " ".join(str(proposal.get("objective") or "").split())[:2000]
                    if agent_id not in known_agents:
                        reject(f"unknown specialist {agent_id!r}")
                        continue
                    if len(objective) < 10:
                        reject("an assignment needs an objective")
                        continue
                    if counts[action_type] >= MAX_CYCLE_DELEGATIONS:
                        reject(f"at most {MAX_CYCLE_DELEGATIONS} assignments per cycle")
                        continue
                    if daily_delegations + counts[action_type] >= MAX_DAILY_DELEGATIONS:
                        reject(f"at most {MAX_DAILY_DELEGATIONS} assignments a day")
                        continue
                    if agent_id in busy_agents or agent_id in delegated_agents:
                        reject(f"{agent_id} already has an assignment in progress")
                        continue
                    if looks_like_repeat(
                        objective_key(objective), recent_assignments.get(agent_id, [])
                    ):
                        reject(
                            f"{agent_id} was asked something very like this in the last "
                            f"{REPEAT_ASSIGNMENT_DAYS} days"
                        )
                        continue
                    project_id = proposal.get("project_id") or None
                    if project_id and repos.projects.get(project_id) is None:
                        reject("unknown project")
                        continue
                    payload = {"agent_id": agent_id, "objective": objective}
                    if project_id:
                        payload["project_id"] = project_id
                    accepted.append(
                        {
                            "action_type": action_type,
                            "payload": payload,
                            "reason": str(proposal.get("reason") or "")[:1000] or None,
                            "task_id": None,
                            "project_id": project_id,
                            "task_title": f"{agent_id}: {objective[:80]}",
                        }
                    )
                    delegated_agents.add(agent_id)
                    counts[action_type] += 1
                    continue

                topic = " ".join(str(proposal.get("topic") or "").split())[:2000]
                agents = [
                    str(a).strip().lower()
                    for a in (proposal.get("agents") or [])
                    if str(a).strip()
                ]
                agents = list(dict.fromkeys(agents))
                if len(topic) < 10:
                    reject("a review needs a topic")
                    continue
                if counts[action_type] >= MAX_CYCLE_REVIEWS or daily_reviews >= MAX_DAILY_REVIEWS:
                    reject(f"at most {MAX_DAILY_REVIEWS} management review a day")
                    continue
                unknown = [a for a in agents if a not in known_agents]
                if unknown:
                    reject(f"unknown specialists: {', '.join(unknown)}")
                    continue
                if len(agents) < 2:
                    reject("a review needs at least two specialists")
                    continue
                if busy_agents.intersection(agents):
                    reject(
                        "already working: "
                        + ", ".join(sorted(busy_agents.intersection(agents)))
                    )
                    continue
                project_id = proposal.get("project_id") or None
                if project_id and repos.projects.get(project_id) is None:
                    reject("unknown project")
                    continue
                payload = {"topic": topic, "agents": agents}
                if project_id:
                    payload["project_id"] = project_id
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": payload,
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": None,
                        "project_id": project_id,
                        "task_title": f"review: {topic[:80]}",
                    }
                )
                counts[action_type] += 1
                continue

            if action_type == "propose_reorganisation":
                if counts[action_type] >= MAX_CYCLE_REORGS:
                    reject(f"at most {MAX_CYCLE_REORGS} reorganisation per cycle")
                    continue
                if reorganised_recently:
                    reject(
                        f"the company was reorganised in the last {REORG_QUIET_DAYS} days"
                    )
                    continue
                proposed = proposal.get("changes")
                rationale = " ".join(str(proposal.get("rationale") or "").split())[:2000]
                if not isinstance(proposed, list) or not proposed:
                    reject("a reorganisation needs at least one change")
                    continue
                if len(rationale) < 20:
                    reject("a reorganisation needs a rationale")
                    continue
                changes = []
                for item in proposed[:MAX_REORG_CHANGES]:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("change") or "").strip()
                    if kind not in CHANGE_KINDS:
                        continue
                    changes.append(
                        {
                            "agent_id": str(item.get("agent_id") or "").strip().lower()[:32],
                            "change": kind,
                            "to": " ".join(str(item.get("to") or "").split())[:600],
                            "reason": " ".join(str(item.get("reason") or "").split())[:600],
                        }
                    )
                if not changes:
                    reject("no valid changes in that reorganisation")
                    continue
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": {"changes": changes, "rationale": rationale},
                        "reason": str(proposal.get("reason") or rationale)[:1000] or None,
                        "task_id": None,
                        "project_id": None,
                        "task_title": f"reorganise {len(changes)} role(s)",
                    }
                )
                counts[action_type] += 1
                continue

            if action_type == "hire_employee":
                if counts[action_type] >= MAX_CYCLE_HIRES:
                    reject(f"at most {MAX_CYCLE_HIRES} hire proposal per cycle")
                    continue
                if hire_awaiting_alex:
                    reject("a hire is already waiting for Alex to decide")
                    continue
                if hired_recently:
                    reject(f"a colleague was proposed in the last {HIRE_QUIET_DAYS} days")
                    continue
                gap = " ".join(str(proposal.get("capability_gap") or "").split())[:2000]
                if len(gap) < 20:
                    reject("a hire needs the gap it fills, from the record")
                    continue
                tools = [
                    str(tool).strip()[:64]
                    for tool in (proposal.get("tools") or [])
                    if str(tool).strip()
                ]
                if not tools:
                    reject("a hire needs the tools it would hold")
                    continue
                fields = {
                    key: " ".join(str(proposal.get(key) or "").split())
                    for key in ("agent_id", "name", "title", "department", "notebook", "specialty")
                }
                missing = [key for key, value in fields.items() if not value]
                if missing:
                    reject(f"a hire needs {', '.join(missing)}")
                    continue
                # Everything else -- the id pattern, the tool ceiling, names
                # already taken -- is re-checked by the hire handler when the
                # action runs, and again when Alex approves it.
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": {
                            **fields,
                            "agent_id": fields["agent_id"].lower()[:32],
                            "specialty": fields["specialty"][:2000],
                            "personality": " ".join(
                                str(proposal.get("personality") or "").split()
                            )[:1000] or None,
                            "capability_gap": gap,
                            "tools": tools,
                        },
                        "reason": str(proposal.get("reason") or gap)[:1000] or None,
                        "task_id": None,
                        "project_id": None,
                        "task_title": f"hire {fields['name']} as {fields['title']}",
                    }
                )
                counts[action_type] += 1
                continue

            if action_type == "create_engineering_ticket":
                task_id = proposal.get("task_id") or None
                task = repos.tasks.get(task_id) if isinstance(task_id, str) else None
                if task is None:
                    reject("unknown task")
                    continue
                if task["status"] in CLOSED_STATUSES:
                    reject(f"the task is {task['status']}")
                    continue
                existing = tickets_by_task.get(task["id"])
                if existing and existing["sync_state"] == "synced":
                    reject("that task already has an engineering ticket")
                    continue
                if counts[action_type] >= MAX_CYCLE_TICKETS:
                    reject(f"at most {MAX_CYCLE_TICKETS} engineering ticket per cycle")
                    continue
                if tickets_today + counts[action_type] >= MAX_DAILY_TICKETS:
                    reject(f"at most {MAX_DAILY_TICKETS} engineering tickets a day")
                    continue

                title = " ".join(str(proposal.get("title") or "").split())[:240]
                objective = " ".join(str(proposal.get("objective") or "").split())[:4000]
                requirements = _bounded_lines(proposal.get("requirements"))
                acceptance = _bounded_lines(proposal.get("acceptance_criteria"))
                if len(title) < 5 or len(objective) < 20:
                    reject("an engineering ticket needs a title and an objective")
                    continue
                if not requirements or not acceptance:
                    reject("an engineering ticket needs requirements and acceptance criteria")
                    continue
                priority = str(proposal.get("priority") or "P2").upper()
                if priority not in TICKET_PRIORITIES:
                    reject(f"priority must be one of {', '.join(TICKET_PRIORITIES)}")
                    continue

                payload = {
                    "task_id": task["id"],
                    "title": title,
                    "objective": objective,
                    "requirements": requirements,
                    "acceptance_criteria": acceptance,
                    "priority": priority,
                    "kind": "bug" if proposal.get("kind") == "bug" else "feature",
                    "security_review_required": bool(
                        proposal.get("security_review_required", False)
                    ),
                }
                if isinstance(task["estimated_minutes"], int) and task["estimated_minutes"] > 0:
                    payload["estimated_minutes"] = task["estimated_minutes"]
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": payload,
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": task["id"],
                        "project_id": task["project_id"],
                        "task_title": title,
                    }
                )
                counts[action_type] += 1
                continue

            if action_type == "set_engineering_priority":
                ticket_id = proposal.get("ticket_id") or None
                ticket = tickets_by_id.get(ticket_id) if isinstance(ticket_id, str) else None
                if ticket is None:
                    reject("unknown engineering ticket")
                    continue
                priority = str(proposal.get("priority") or "").upper()
                if priority not in TICKET_PRIORITIES:
                    reject(f"priority must be one of {', '.join(TICKET_PRIORITIES)}")
                    continue
                if ticket["priority"] == priority:
                    reject(f"that ticket is already {priority}")
                    continue
                if ticket["status"] == "done":
                    reject("that ticket is done")
                    continue
                if ticket_id in seen_tickets:
                    reject("the ticket already has an action in this plan")
                    continue
                if counts[action_type] >= MAX_CYCLE_PRIORITY_CHANGES:
                    reject(f"at most {MAX_CYCLE_PRIORITY_CHANGES} priority changes per cycle")
                    continue
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": {
                            "ticket_id": ticket["id"],
                            "priority": priority,
                            "reason": str(proposal.get("reason") or "")[:1000] or None,
                        },
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": ticket["task_id"],
                        "project_id": None,
                        "task_title": f"#{ticket['github_issue_number']} -> {priority}",
                    }
                )
                seen_tickets.add(ticket_id)
                counts[action_type] += 1
                continue

            if action_type == "ask_user":
                message = spoken_text(str(proposal.get("message") or ""))[:MESSAGE_MAX]
                if len(message) < MESSAGE_MIN:
                    reject(f"a message to the user needs at least {MESSAGE_MIN} characters")
                    continue
                if counts[action_type] >= MAX_CYCLE_QUESTIONS:
                    reject(f"at most {MAX_CYCLE_QUESTIONS} thing raised with the user per cycle")
                    continue
                key = objective_key(message)
                if looks_like_repeat(key, open_message_keys + seen_messages):
                    reject("the user has already been asked something very like this")
                    continue
                accepted.append(
                    {
                        "action_type": action_type,
                        "payload": {
                            "message": message,
                            "expects_reply": bool(proposal.get("expects_reply", True)),
                            # Python decides where this came from, not the model.
                            "source": "planning_cycle",
                        },
                        "reason": str(proposal.get("reason") or "")[:1000] or None,
                        "task_id": None,
                        "project_id": None,
                        "task_title": message[:80],
                    }
                )
                seen_messages.append(key)
                counts[action_type] += 1
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
                readiness = task_readiness_in(repos, task, now)
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
        team: "TeamSource | None" = None,
        usage=None,
    ):
        self.gary = gary
        self.planner = planner
        self.notebook = notebook
        self.calendar = calendar
        self.email = email
        # The GaryCorp specialists. Without it a cycle plans Alex's own work
        # only, and every delegation is rejected.
        self.team = team
        # The model-usage ledger. Without it cycles still run, uncosted.
        self.usage = usage
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

    def _record_usage(self, plan: dict, run_id: str, planning_type: str) -> None:
        """What this cycle's model call cost, if the ledger is connected."""
        if self.usage is None:
            return
        from gary.finance.pricing import usage_from_openai

        self.usage.record(
            "planning_cycle",
            plan.get("_model") or "unknown",
            usage_from_openai(plan.get("_usage")),
            entity_type="planning_run",
            entity_id=run_id,
            detail=f"{planning_type} cycle",
            # Planning is Gary's own thinking, and is costed as his.
            agent_id=GARY_ACTOR,
        )

    async def _team_state(self, now: str) -> tuple[dict | None, list[dict], str | None]:
        """Who the specialists are, what they are already doing, and the
        reports that arrived since the last cycle. A team that cannot be read
        is reported, and delegation is then refused rather than guessed at."""
        if self.team is None:
            return None, [], None
        try:
            return await asyncio.to_thread(self._read_team, now)
        except Exception as exc:
            logger.warning("Team state unavailable for planning: %s", exc)
            return None, [], str(exc) or type(exc).__name__

    def _read_team(self, now: str) -> tuple[dict, list[dict], None]:
        since = self.gary.planning.last_cycle_finished_at()
        overview = self.team.team()
        members, employee_ids = [], []
        for member in overview.get("members", []):
            if not member.get("can_delegate") and member.get("status") == "active":
                employee_ids.append(member["agent_id"])
                members.append(
                    {
                        "agent_id": member["agent_id"],
                        "name": member["name"],
                        "title": member["title"],
                        "department": member["department"],
                        "assignments": member.get("assignments", {}),
                    }
                )

        working, reports = [], []
        for assignment in self.team.list_assignments(limit=25):
            if assignment["status"] in ("queued", "running"):
                working.append(
                    {
                        "agent": assignment["agent"],
                        "objective": assignment["objective"][:200],
                        "status": assignment["status"],
                    }
                )
                continue
            # Reports Gary has not seen yet: they came in after the last cycle.
            finished = assignment.get("completed_at")
            if (
                assignment["status"] == "completed"
                and assignment.get("report")
                and finished
                and (since is None or to_datetime(finished) >= to_datetime(since))
            ):
                report = assignment["report"]
                reports.append(
                    {
                        "assignment_id": assignment["assignment_id"],
                        "agent": assignment["agent"],
                        "objective": assignment["objective"][:200],
                        "summary": (report.get("summary") or "")[:800],
                        "recommendation": str(
                            report.get("recommendation")
                            or report.get("recommended_option")
                            or ""
                        )[:500],
                        "decisions_needed": (report.get("decisions_needed") or [])[:3],
                    }
                )
        return (
            {
                "employee_ids": employee_ids,
                "members": members,
                "working_on": working,
                "note": "Specialists are advisory: a report changes nothing by itself.",
            },
            reports[:5],
            None,
        )

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

            team_state, reports, team_error = await self._team_state(now)

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
                "team": team_state,
                "department_reports": reports,
            }

            plan = await self.planner.create_plan(planning_input)
            self._record_usage(plan, run_id, planning_type)

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
                team=team_state,
                day_start=format_utc(
                    dt.datetime.combine(today, dt.time(), self.gary.timezone)
                ),
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
                "team_error": team_error,
                "reports_read": [report["assignment_id"] for report in reports],
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

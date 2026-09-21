import datetime as dt

from pydantic import Field

from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.common import EntityId, RequestModel
from gary.policy import GARY_ACTOR, SYSTEM_ACTOR
from gary.services.approval_service import expire_stale_approvals
from gary.services.common import Clock, NotFoundError, clock_now, default_clock
from gary.services.readiness import (
    calculate_task_score,
    task_is_overdue,
    task_readiness,
)
from gary.timeutil import format_utc, to_datetime

UPCOMING_DEADLINE_DAYS = 7
UPCOMING_FOLLOWUP_HOURS = 24
RECENT_ACTION_HOURS = 48
STALE_PLANNING_RUN_HOURS = 2
CONTEXT_LIST_LIMIT = 25
PLANNING_TYPES = (
    "morning",
    "midday",
    "evening",
    "event_triggered",
    "manual",
    # The continuous loop's short reactive cycle.
    "management",
)


class RecordPlanRequest(RequestModel):
    # Omit when no planning_get_context call started a run in this conversation.
    planning_run_id: EntityId | None = None
    summary: str = Field(min_length=1, max_length=4000)
    action_ids: list[EntityId] = Field(default_factory=list, max_length=50)


def shift(now: str, **delta) -> str:
    return format_utc(to_datetime(now) + dt.timedelta(**delta))


# How far back a failure still counts as "nobody has dealt with this".
HEALTH_WINDOW_HOURS = 72


def company_health(repos: Repositories, now: str) -> dict:
    """What has gone wrong and nobody has looked at.

    A company that runs itself for days accumulates failures quietly: an
    assignment that died, a ticket that fell off the board, an approval or a
    question that expired unanswered. Each is individually small and none of
    them raises itself, so without this Gary has no way to tell a quiet week
    from a stuck one -- which is what "escalate only what needs you" depends
    on.

    Pure reads, and counts rather than dumps, so it is cheap enough to run on
    every management tick.
    """
    since = shift(now, hours=-HEALTH_WINDOW_HOURS)

    failed_assignments = [
        assignment
        for assignment in repos.assignments.list_recent(None, None, 50)
        if assignment["status"] == "failed" and (assignment["created_at"] or "") >= since
    ]
    broken_tickets = [
        ticket
        for ticket in repos.engineering.list_all(50)
        if ticket["sync_state"] in ("degraded", "needs_reconciliation")
    ]
    expired_approvals = [
        approval
        for approval in repos.approvals.list_recent_resolved(50)
        if approval["status"] == "expired" and (approval["resolved_at"] or "") >= since
    ]
    expired_questions = [
        message
        for message in repos.spoken.list_recent(since, 50)
        if message["status"] == "expired" and message["expects_reply"]
    ]
    overdue = repos.tasks.list_overdue(now)

    problems = {
        "failed_assignments": [
            {"assignment_id": a["id"], "agent_id": a["assigned_to"],
             "objective": a["objective"][:120], "at": a["completed_at"] or a["created_at"]}
            for a in failed_assignments
        ],
        "broken_engineering_tickets": [
            {"ticket_id": t["id"], "issue_number": t["github_issue_number"],
             "sync_state": t["sync_state"], "error": (t["sync_error"] or "")[:200],
             "at": t["updated_at"]}
            for t in broken_tickets
        ],
        "expired_approvals": [
            {"approval_id": a["id"], "summary": a["summary"][:120], "at": a["resolved_at"]}
            for a in expired_approvals
        ],
        "unanswered_questions": [
            {"message_id": m["id"], "asked": m["text"][:120], "at": m["created_at"]}
            for m in expired_questions
        ],
        "overdue_tasks": [
            {"task_id": t["id"], "title": t["title"], "deadline": t["deadline"],
             "at": t["deadline"]}
            for t in overdue
        ],
    }
    counts = {name: len(items) for name, items in problems.items()}
    return {
        "healthy": not any(counts.values()),
        "counts": counts,
        # Trimmed: the planner needs to know what, not every instance.
        **{name: items[:5] for name, items in problems.items()},
    }


# How far back the team's record is read for planning.
SCORECARD_DAYS = 30


def team_scorecards(repos: Repositories, now: str) -> list[dict]:
    """Each employee's record, for arguing about the company's shape."""
    from gary.services.performance import employee_scorecard

    since = shift(now, hours=-24 * SCORECARD_DAYS)
    return [
        employee_scorecard(repos, row["id"], since, now)
        for row in repos.agents.list_all()
        if row["is_employee"] and row["active"]
    ]


def engineering_queue(repos: Repositories) -> list[dict]:
    """Open engineering tickets, as the planner needs to see them: what the
    work is, how urgent, and whether it is already on the calendar."""
    queue = []
    for ticket in repos.engineering.list_open():
        task = repos.tasks.get(ticket["task_id"])
        queue.append(
            {
                "ticket_id": ticket["id"],
                "task_id": ticket["task_id"],
                "title": (task or {}).get("title", ""),
                "issue_number": ticket["github_issue_number"],
                "status": ticket["status"],
                "priority": ticket["priority"],
                "security_review_required": bool(ticket["security_review_required"]),
                "scheduled_start": (task or {}).get("scheduled_start"),
                "deadline": (task or {}).get("deadline"),
                "sync_state": ticket["sync_state"],
            }
        )
    # Most urgent first, so a truncated list keeps what matters.
    return sorted(queue, key=lambda t: (t["priority"], t["status"]))


def collect_operations(repos: Repositories, now: str) -> dict:
    upcoming_end = shift(now, days=UPCOMING_DEADLINE_DAYS)
    projects = repos.projects.list_active()
    all_projects = {p["id"]: p for p in repos.projects.list_all()}
    blocking_ids = repos.dependencies.task_ids_blocking_open_tasks()
    commitment_task_ids = repos.commitments.open_task_ids()

    ready, blocked, underway, overdue = [], [], [], []
    open_tasks = repos.tasks.list_open()
    for task in open_tasks:
        dependencies = repos.dependencies.list_dependencies(task["id"])
        readiness = task_readiness(task, dependencies, now)
        project = all_projects.get(task["project_id"])
        entry = {
            "task_id": task["id"],
            "title": task["title"],
            "project": project["name"] if project else None,
            "status": task["status"],
            "priority": task["priority"],
            "planning_score": calculate_task_score(
                task,
                now,
                blocks_other_tasks=task["id"] in blocking_ids,
                has_external_commitment=task["id"] in commitment_task_ids,
                project_priority=project["priority"] if project else None,
            ),
            "deadline": task["deadline"],
            "estimated_minutes": task["estimated_minutes"],
            "scheduled_start": task["scheduled_start"],
            "scheduled_end": task["scheduled_end"],
        }

        if task_is_overdue(task, now):
            overdue.append(entry)
        if readiness["ready"]:
            ready.append(entry)
        elif task["status"] == "in_progress":
            underway.append(entry)
        else:
            blocked.append(
                {
                    **entry,
                    "blocked_by": readiness["blocked_by"],
                    "reasons": readiness["reasons"],
                }
            )

    def by_score(entry):
        return (-entry["planning_score"], entry["deadline"] or "~")

    ready.sort(key=by_score)
    blocked.sort(key=by_score)
    underway.sort(key=by_score)
    overdue.sort(key=lambda entry: entry["deadline"])

    missed = [
        {
            "task_id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "scheduled_start": task["scheduled_start"],
            "scheduled_end": task["scheduled_end"],
            "affects": [t["title"] for t in repos.dependencies.open_downstream_of(task["id"])],
        }
        for task in repos.tasks.list_missed_blocks(now)
    ]

    open_commitments = repos.commitments.list_open()
    upcoming_deadlines = sorted(
        [
            {"type": "task", "title": t["title"], "deadline": t["deadline"]}
            for t in repos.tasks.list_deadlines_between(now, upcoming_end)
        ]
        + [
            {"type": "project", "title": p["name"], "deadline": p["deadline"]}
            for p in projects
            if p["deadline"] and now <= p["deadline"] <= upcoming_end
        ]
        + [
            {"type": "commitment", "title": c["description"], "deadline": c["deadline"]}
            for c in open_commitments
            if c["deadline"] and c["deadline"] <= upcoming_end
        ],
        key=lambda item: item["deadline"],
    )

    return {
        "now": now,
        "active_projects": [
            {
                "project_id": p["id"],
                "name": p["name"],
                "status": p["status"],
                "priority": p["priority"],
                "deadline": p["deadline"],
                "open_tasks": sum(1 for t in open_tasks if t["project_id"] == p["id"]),
            }
            for p in projects
        ],
        "ready_tasks": ready[:CONTEXT_LIST_LIMIT],
        "in_progress_tasks": underway[:CONTEXT_LIST_LIMIT],
        "blocked_tasks": blocked[:CONTEXT_LIST_LIMIT],
        "overdue_tasks": overdue[:CONTEXT_LIST_LIMIT],
        "missed_scheduled_blocks": missed[:CONTEXT_LIST_LIMIT],
        "upcoming_deadlines": upcoming_deadlines[:CONTEXT_LIST_LIMIT],
        "due_followups": repos.followups.list_due(now)[:CONTEXT_LIST_LIMIT],
        "upcoming_followups": repos.followups.list_pending_between(
            now, shift(now, hours=UPCOMING_FOLLOWUP_HOURS)
        )[:CONTEXT_LIST_LIMIT],
        "open_commitments": open_commitments[:CONTEXT_LIST_LIMIT],
        "pending_approvals": repos.approvals.list_pending()[:CONTEXT_LIST_LIMIT],
        "recent_actions": repos.actions.list_recent(shift(now, hours=-RECENT_ACTION_HOURS)),
        # Alex's engineering queue. Without it a cycle cannot tell which work
        # is already ticketed, what is most urgent, or what to schedule.
        "engineering_tickets": engineering_queue(repos)[:CONTEXT_LIST_LIMIT],
        # What is stuck. A quiet week and a broken one look identical without it.
        "company_health": company_health(repos, now),
        # What each person's record shows, so a proposal about the company's
        # shape argues from the same numbers a review would.
        "team_scorecards": team_scorecards(repos, now),
        # What Gary has put to Alex himself. Without these a cycle re-asks
        # what is already open and never acts on what Alex answered.
        "open_questions": [
            {
                "message_id": message["id"],
                "asked": message["text"],
                "asked_at": message["spoken_at"],
                "status": message["status"],
            }
            for message in repos.spoken.list_open()
        ][:CONTEXT_LIST_LIMIT],
        "answered_questions": [
            {
                "message_id": message["id"],
                "asked": message["text"],
                "answer": message["answer"],
                "answered_at": message["answered_at"],
            }
            for message in repos.spoken.list_answered_since(
                shift(now, hours=-RECENT_ACTION_HOURS), CONTEXT_LIST_LIMIT
            )
        ],
        "truncated_lists": {
            name: count
            for name, count in {
                "ready_tasks": len(ready),
                "blocked_tasks": len(blocked),
                "overdue_tasks": len(overdue),
            }.items()
            if count > CONTEXT_LIST_LIMIT
        },
    }


class PlanningService:
    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def get_planning_context(self, planning_type: str = "manual") -> dict:
        """Everything Gary needs to plan, collected in one consistent read, so
        the model does not make ten separate calls. Starts a planning run."""
        if planning_type not in PLANNING_TYPES:
            raise ValueError(f"planning_type must be one of {PLANNING_TYPES}")

        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            repos.planning_runs.fail_stale(shift(now, hours=-STALE_PLANNING_RUN_HOURS), now)
            expire_stale_approvals(repos, now)
            context = collect_operations(repos, now)

            counts = {
                key: len(value)
                for key, value in context.items()
                if isinstance(value, list)
            }
            run = repos.planning_runs.start(
                planning_type,
                input_summary=", ".join(f"{key}={value}" for key, value in counts.items()),
                now=now,
            )

        return {"planning_run_id": run["id"], **context}

    def snapshot(self) -> dict:
        """The same operational picture without starting a planning run."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            expire_stale_approvals(repos, now)
            return collect_operations(repos, now)

    def latest_run(self) -> dict | None:
        with self.db.read() as conn:
            return Repositories.bind(conn).planning_runs.latest()

    def last_cycle_finished_at(self) -> str | None:
        """When the last full planning cycle completed or failed. Context
        lookups in conversation do not count."""
        with self.db.read() as conn:
            return Repositories.bind(conn).audit.latest_timestamp(
                ("planning_cycle_completed", "planning_cycle_failed")
            )

    def record_plan(self, request: RecordPlanRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            if request.planning_run_id is None:
                run = repos.planning_runs.start("manual", "recorded in conversation", now)
            else:
                run = repos.planning_runs.get(request.planning_run_id)
            if run is None:
                raise NotFoundError(f"No planning run with id {request.planning_run_id}")
            if run["status"] != "running":
                raise ValueError(f"Planning run is already {run['status']}")

            unknown = [aid for aid in request.action_ids if repos.actions.get(aid) is None]
            if unknown:
                raise NotFoundError(f"Unknown action ids: {unknown}")

            run = repos.planning_runs.complete(
                run["id"],
                {"summary": request.summary, "action_ids": request.action_ids},
                now,
            )
            repos.audit.write(
                actor,
                "planning_run_completed",
                f"Recorded {run['planning_type']} plan",
                "planning_run",
                run["id"],
                {"action_ids": request.action_ids},
                now=now,
            )
        return run

    def complete_cycle(self, run_id: str, plan: dict, actor: str = GARY_ACTOR) -> dict:
        """Record the outcome of a scheduled planning cycle."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            run = repos.planning_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"No planning run with id {run_id}")
            if run["status"] != "running":
                raise ValueError(f"Planning run is already {run['status']}")
            run = repos.planning_runs.complete(run_id, plan, now)
            repos.audit.write(
                actor,
                "planning_cycle_completed",
                f"Completed {run['planning_type']} planning cycle",
                "planning_run",
                run_id,
                {
                    "results": [
                        {
                            "action_type": item["proposal"]["action_type"],
                            "status": item["result"].get("status"),
                        }
                        for item in plan.get("results", [])
                    ],
                    "rejected": len(plan.get("rejected", [])),
                },
                now=now,
            )
        return run

    def fail_run(self, run_id: str, error: str) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            run = repos.planning_runs.fail(run_id, error[:1000], now)
            repos.audit.write(
                SYSTEM_ACTOR,
                "planning_cycle_failed",
                f"Planning cycle failed: {error[:200]}",
                "planning_run",
                run_id,
                {"error": error[:1000]},
                now=now,
            )
        return run

    def run_types_started_on(self, day: dt.date, timezone) -> set[str]:
        """Planning types that already started on a local calendar day, in any
        status, so a failed scheduled run is not retried in a loop."""
        start = dt.datetime.combine(day, dt.time(), timezone)
        end = start + dt.timedelta(days=1)
        with self.db.read() as conn:
            runs = Repositories.bind(conn).planning_runs.list_started_between(
                format_utc(start), format_utc(end)
            )
        return {run["planning_type"] for run in runs}

    def find_overdue_tasks(self) -> list[dict]:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            return Repositories.bind(conn).tasks.list_overdue(now)

    def collect_new_alerts(self) -> dict:
        """Due follow-ups and newly overdue tasks not yet reported.

        Each is recorded in the audit log when first reported, so a restart
        neither repeats nor loses alerts.
        """
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            expire_stale_approvals(repos, now)

            followups = [
                followup
                for followup in repos.followups.list_due(now)
                if not repos.audit.has_event("followup_due_announced", followup["id"])
            ]
            for followup in followups:
                repos.audit.write(
                    SYSTEM_ACTOR,
                    "followup_due_announced",
                    f"Follow-up due: {followup['title']}",
                    "followup",
                    followup["id"],
                    {"due_at": followup["due_at"]},
                    now=now,
                )

            overdue = [
                task
                for task in repos.tasks.list_overdue(now)
                if not repos.audit.has_event("task_overdue_detected", task["id"])
            ]
            for task in overdue:
                repos.audit.write(
                    SYSTEM_ACTOR,
                    "task_overdue_detected",
                    f"Task overdue: {task['title']}",
                    "task",
                    task["id"],
                    {"deadline": task["deadline"], "status": task["status"]},
                    now=now,
                )

            missed = []
            for task in repos.tasks.list_missed_blocks(now):
                block_key = f"{task['id']}@{task['scheduled_end']}"
                if repos.audit.has_event("scheduled_block_missed", block_key):
                    continue
                downstream = [t["title"] for t in repos.dependencies.open_downstream_of(task["id"])]
                repos.audit.write(
                    SYSTEM_ACTOR,
                    "scheduled_block_missed",
                    f"Scheduled time passed but not finished: {task['title']}",
                    "task_block",
                    block_key,
                    {
                        "task_id": task["id"],
                        "scheduled_end": task["scheduled_end"],
                        "affects": downstream,
                    },
                    now=now,
                )
                missed.append({**task, "affects": downstream})

        return {"due_followups": followups, "overdue_tasks": overdue, "missed_blocks": missed}

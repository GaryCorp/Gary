"""Deterministic task readiness and planning score.

Pure functions: no database access, so they are easy to test and cannot be
influenced by the model. User-set priority is an input and is never
overwritten by the computed score.
"""

from gary.timeutil import to_datetime

READY_STATUSES = ("todo", "scheduled")
# A project that is planned but not started holds its work back: its tasks
# keep their dependencies and estimates but are not offered for scheduling.
NOT_STARTED_PROJECT_STATUSES = ("planned",)
CLOSED_STATUSES = ("completed", "cancelled")


def task_readiness(
    task: dict, dependencies: list[dict], now: str, project_status: str | None = None
) -> dict:
    """A task is ready when its status is todo or scheduled, every dependency
    is completed, its earliest start has passed, and its project (if any) has
    started."""
    blocked_by = [dep["title"] for dep in dependencies if dep["status"] != "completed"]
    reasons = []

    if task["status"] not in READY_STATUSES:
        reasons.append(f"status is {task['status']}")
    if blocked_by:
        reasons.append("waiting on unfinished dependencies")
    if task["earliest_start"] and task["earliest_start"] > now:
        reasons.append("earliest start has not arrived")
    if project_status in NOT_STARTED_PROJECT_STATUSES:
        reasons.append("its project has not started")

    return {
        "task_id": task["id"],
        "task": task["title"],
        "ready": not reasons,
        "blocked_by": blocked_by,
        "reasons": reasons,
    }


def hours_until(deadline: str | None, now: str) -> float | None:
    if not deadline:
        return None
    return (to_datetime(deadline) - to_datetime(now)).total_seconds() / 3600


def task_is_overdue(task: dict, now: str) -> bool:
    return bool(
        task["deadline"]
        and task["deadline"] < now
        and task["status"] not in CLOSED_STATUSES
    )


def calculate_task_score(
    task: dict,
    now: str,
    blocks_other_tasks: bool,
    has_external_commitment: bool,
    project_priority: int | None = None,
) -> int:
    score = task["priority"] * 10

    # Tasks in a more important project rank higher; a normal-priority (5)
    # project, or no project, adds nothing.
    if project_priority is not None:
        score += (project_priority - 5) * 2

    if task_is_overdue(task, now):
        score += 40

    hours_until_deadline = hours_until(task["deadline"], now)
    if hours_until_deadline is not None:
        if hours_until_deadline <= 24:
            score += 30
        elif hours_until_deadline <= 72:
            score += 20
        elif hours_until_deadline <= 168:
            score += 10

    if blocks_other_tasks:
        score += 15

    if has_external_commitment:
        score += 20

    return score

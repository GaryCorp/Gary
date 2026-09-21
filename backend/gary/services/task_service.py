from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.common import provided_fields
from gary.models.task import (
    CompleteTaskRequest,
    CreateTaskRequest,
    DependencyRequest,
    UpdateTaskRequest,
)
from gary.policy import GARY_ACTOR
from gary.services.common import (
    Clock,
    NotFoundError,
    clock_now,
    default_clock,
    metadata_json,
    require_project,
    require_task,
    task_readiness_in,
)
from gary.services.readiness import CLOSED_STATUSES

TASK_LIST_LIMIT = 100


def create_task_in(
    repos: Repositories, request: CreateTaskRequest, now: str, actor: str
) -> dict:
    """Create a task inside an existing transaction."""
    require_project(repos, request.project_id)
    task = repos.tasks.create(
        title=request.title,
        project_id=request.project_id,
        description=request.description,
        priority=request.priority,
        estimated_minutes=request.estimated_minutes,
        deadline=request.deadline,
        earliest_start=request.earliest_start,
        metadata=request.metadata,
        created_by=actor,
        now=now,
    )
    repos.audit.write(
        actor,
        "task_created",
        f"Created task: {task['title']}",
        "task",
        task["id"],
        {
            "project_id": task["project_id"],
            "priority": task["priority"],
            "deadline": task["deadline"],
        },
        now=now,
    )
    return task


def update_task_in(
    repos: Repositories, request: UpdateTaskRequest, now: str, actor: str
) -> dict:
    """Update a task inside an existing transaction."""
    before = require_task(repos, request.task_id)
    if before["status"] in CLOSED_STATUSES:
        raise ValueError(
            f"Task {before['title']} is {before['status']} and cannot be changed"
        )

    changes = provided_fields(request, exclude={"task_id", "metadata"})
    if "metadata" in request.model_fields_set:
        changes["metadata_json"] = metadata_json(request.metadata)
    if "project_id" in changes:
        require_project(repos, changes["project_id"])

    status = changes.get("status")
    if status == "in_progress" and not before["started_at"]:
        changes["started_at"] = now

    task = repos.tasks.update(request.task_id, now=now, **changes)

    closed_followups = []
    if status == "cancelled":
        closed_followups = repos.followups.close_pending_for_task(
            task["id"], "cancelled", now
        )

    repos.audit.write(
        actor,
        "task_cancelled" if status == "cancelled" else "task_updated",
        f"Updated task: {task['title']}",
        "task",
        task["id"],
        {
            "changes": {
                key: {"from": before.get(key), "to": value}
                for key, value in changes.items()
            },
            "cancelled_followups": closed_followups,
        },
        now=now,
    )
    return task


def add_dependency_in(
    repos: Repositories, task: dict, prerequisite: dict, now: str, actor: str
) -> bool:
    """Add a dependency inside an existing transaction, rejecting self and
    circular dependencies. Returns False if it already existed."""
    if task["id"] == prerequisite["id"]:
        raise ValueError("A task cannot depend on itself")
    if repos.dependencies.depends_on_transitively(prerequisite["id"], task["id"]):
        raise ValueError(
            f"Circular dependency: {prerequisite['title']} already depends "
            f"on {task['title']}"
        )
    added = repos.dependencies.add(task["id"], prerequisite["id"], now)
    if added:
        repos.audit.write(
            actor,
            "dependency_added",
            f"{task['title']} now depends on {prerequisite['title']}",
            "task",
            task["id"],
            {"depends_on_task_id": prerequisite["id"]},
            now=now,
        )
    return added


class TaskService:
    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def create_task(self, request: CreateTaskRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            return create_task_in(Repositories.bind(conn), request, now, actor)

    def update_task(self, request: UpdateTaskRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            return update_task_in(Repositories.bind(conn), request, now, actor)

    def complete_task(self, request: CompleteTaskRequest, actor: str = GARY_ACTOR) -> dict:
        """Complete the task, close its pending follow-ups, and audit, atomically."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            before = require_task(repos, request.task_id)
            if before["status"] == "completed":
                return {**before, "already_completed": True, "unblocked_tasks": []}
            if before["status"] == "cancelled":
                raise ValueError(f"Task {before['title']} was cancelled")

            task = repos.tasks.mark_completed(
                request.task_id, request.actual_minutes, now=now
            )
            closed = repos.followups.close_pending_for_task(task["id"], "completed", now)

            # Tasks whose last unfinished dependency was this one.
            unblocked = [
                dependent["title"]
                for dependent in repos.dependencies.list_dependents(task["id"])
                if dependent["status"] not in CLOSED_STATUSES
                and all(
                    dep["status"] == "completed"
                    for dep in repos.dependencies.list_dependencies(dependent["id"])
                )
            ]

            repos.audit.write(
                actor,
                "task_completed",
                f"Completed task: {task['title']}",
                "task",
                task["id"],
                {
                    "actual_minutes": task["actual_minutes"],
                    "completed_followups": closed,
                    "unblocked_tasks": unblocked,
                },
                now=now,
            )
        return {**task, "already_completed": False, "unblocked_tasks": unblocked}

    def mark_started(self, task_id: str, actor: str = GARY_ACTOR) -> dict:
        return self.update_task(UpdateTaskRequest(task_id=task_id, status="in_progress"), actor)

    def get_task(self, task_id: str) -> dict:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            task = repos.tasks.get(task_id)
            if task is None:
                raise NotFoundError(f"No task with id {task_id}")
            dependencies = repos.dependencies.list_dependencies(task_id)
            readiness = task_readiness_in(repos, task, now)
            return {
                **task,
                "depends_on": dependencies,
                "blocks": repos.dependencies.list_dependents(task_id),
                "ready": readiness["ready"],
                "blocked_by": readiness["blocked_by"],
                "not_ready_reasons": readiness["reasons"],
            }

    def list_tasks(self, project_id: str | None = None, status: str = "open") -> list[dict]:
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            if project_id is not None:
                require_project(repos, project_id)
                tasks = repos.tasks.list_for_project(project_id)
                if status == "open":
                    tasks = [t for t in tasks if t["status"] not in CLOSED_STATUSES]
                else:
                    tasks = [t for t in tasks if t["status"] == status]
            elif status == "open":
                tasks = repos.tasks.list_open()
            else:
                tasks = repos.tasks.list_by_status((status,))
        return tasks[:TASK_LIST_LIMIT]

    def list_overdue(self) -> list[dict]:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            return Repositories.bind(conn).tasks.list_overdue(now)

    def add_dependency(self, request: DependencyRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        # BEGIN IMMEDIATE holds the write lock, so the cycle check and insert
        # cannot interleave with another writer.
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            task = require_task(repos, request.task_id)
            prerequisite = require_task(repos, request.depends_on_task_id)
            added = add_dependency_in(repos, task, prerequisite, now, actor)
        return {
            "task": task["title"],
            "depends_on": prerequisite["title"],
            "already_existed": not added,
        }

    def remove_dependency(self, request: DependencyRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            task = require_task(repos, request.task_id)
            prerequisite = require_task(repos, request.depends_on_task_id)
            removed = repos.dependencies.remove(task["id"], prerequisite["id"])
            if removed:
                repos.audit.write(
                    actor,
                    "dependency_removed",
                    f"{task['title']} no longer depends on {prerequisite['title']}",
                    "task",
                    task["id"],
                    {"depends_on_task_id": prerequisite["id"]},
                    now=now,
                )
        return {
            "task": task["title"],
            "depends_on": prerequisite["title"],
            "removed": removed,
        }

    def is_task_ready(self, task_id: str) -> dict:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            task = require_task(repos, task_id)
            return task_readiness_in(repos, task, now)

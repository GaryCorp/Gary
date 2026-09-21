from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.common import provided_fields
from gary.models.project import (
    CreateProjectRequest,
    CreateProjectWithTasksRequest,
    UpdateProjectRequest,
)
from gary.models.task import CreateTaskRequest
from gary.policy import GARY_ACTOR
from gary.services.common import (
    Clock,
    NotFoundError,
    clock_now,
    default_clock,
    metadata_json,
    task_readiness_in,
)
from gary.services.task_service import add_dependency_in, create_task_in


class ProjectService:
    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def create_project(self, request: CreateProjectRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            project = repos.projects.create(
                name=request.name,
                objective=request.objective,
                status=request.status,
                priority=request.priority,
                deadline=request.deadline,
                metadata=request.metadata,
                now=now,
            )
            repos.audit.write(
                actor,
                "project_created",
                f"Created project: {project['name']}",
                "project",
                project["id"],
                {"priority": project["priority"], "deadline": project["deadline"]},
                now=now,
            )
        return project

    def create_project_with_tasks(
        self, request: CreateProjectWithTasksRequest, actor: str = GARY_ACTOR
    ) -> dict:
        """Project, tasks, and dependencies in one transaction: all or nothing."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            project = repos.projects.create(
                name=request.name,
                objective=request.objective,
                status=request.status,
                priority=request.priority,
                deadline=request.deadline,
                metadata=request.metadata,
                now=now,
            )
            repos.audit.write(
                actor,
                "project_created",
                f"Created project: {project['name']}",
                "project",
                project["id"],
                {"priority": project["priority"], "deadline": project["deadline"],
                 "tasks": len(request.tasks)},
                now=now,
            )

            by_key = {}
            for item in request.tasks:
                task = create_task_in(
                    repos,
                    CreateTaskRequest(
                        project_id=project["id"],
                        title=item.title,
                        description=item.description,
                        priority=item.priority,
                        estimated_minutes=item.estimated_minutes,
                        deadline=item.deadline,
                        earliest_start=item.earliest_start,
                    ),
                    now,
                    actor,
                )
                by_key[" ".join(item.title.split()).casefold()] = task

            for item in request.tasks:
                task = by_key[" ".join(item.title.split()).casefold()]
                for title in item.depends_on:
                    prerequisite = by_key[" ".join(title.split()).casefold()]
                    add_dependency_in(repos, task, prerequisite, now, actor)

            tasks = []
            for task in by_key.values():
                readiness = task_readiness_in(repos, task, now)
                tasks.append(
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "estimated_minutes": task["estimated_minutes"],
                        "ready": readiness["ready"],
                        "blocked_by": readiness["blocked_by"],
                    }
                )
        return {"project": project, "tasks": tasks}

    def get_project(self, project_id: str) -> dict:
        now = clock_now(self.clock)
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            project = repos.projects.get(project_id)
            if project is None:
                raise NotFoundError(f"No project with id {project_id}")
            tasks = repos.tasks.list_for_project(project_id)
            for task in tasks:
                readiness = task_readiness_in(repos, task, now)
                task["ready"] = readiness["ready"]
                task["blocked_by"] = readiness["blocked_by"]
            followups = repos.followups.list_pending_for_project(project_id)
        return {**project, "tasks": tasks, "pending_followups": followups}

    def list_projects(self, include_closed: bool = False) -> list[dict]:
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            if include_closed:
                return repos.projects.list_all()
            return repos.projects.list_active()

    def update_project(self, request: UpdateProjectRequest, actor: str = GARY_ACTOR) -> dict:
        now = clock_now(self.clock)
        changes = provided_fields(request, exclude={"project_id", "metadata"})
        if "metadata" in request.model_fields_set:
            changes["metadata_json"] = metadata_json(request.metadata)
        if not changes:
            raise ValueError("no changes given")

        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            before = repos.projects.get(request.project_id)
            if before is None:
                raise NotFoundError(f"No project with id {request.project_id}")

            status = changes.get("status")
            if status == "completed" and before["status"] != "completed":
                changes["completed_at"] = now
            elif status is not None and status != "completed":
                changes["completed_at"] = None

            project = repos.projects.update(request.project_id, now=now, **changes)
            event = (
                "project_completed"
                if status == "completed" and before["status"] != "completed"
                else "project_updated"
            )
            repos.audit.write(
                actor,
                event,
                f"Updated project: {project['name']}",
                "project",
                project["id"],
                {
                    "changes": {
                        key: {"from": before.get(key), "to": value}
                        for key, value in changes.items()
                    }
                },
                now=now,
            )
        return project

    def mark_completed(self, project_id: str, actor: str = GARY_ACTOR) -> dict:
        return self.update_project(
            UpdateProjectRequest(project_id=project_id, status="completed"), actor
        )

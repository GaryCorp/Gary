"""Action handlers that only change Gary's own SQLite state.

External handlers (calendar, email) need Google credentials and are supplied
by the backend application when it builds Gary.
"""

from gary.db.repositories import Repositories
from gary.models.task import CreateTaskRequest, UpdateTaskRequest
from gary.policy import GARY_ACTOR
from gary.services.action_service import ActionHandler
from gary.services.common import require_project, require_task
from gary.services.task_service import create_task_in, update_task_in


def _check_create(repos: Repositories, payload: CreateTaskRequest) -> dict:
    require_project(repos, payload.project_id)
    return {}


def _record_create(repos, payload: CreateTaskRequest, result: dict, now: str) -> dict:
    task = create_task_in(repos, payload, now, GARY_ACTOR)
    return {"task_id": task["id"], "title": task["title"]}


def _check_update(repos: Repositories, payload: UpdateTaskRequest) -> dict:
    return {"task": require_task(repos, payload.task_id)}


def _record_update(repos, payload: UpdateTaskRequest, result: dict, now: str) -> dict:
    task = update_task_in(repos, payload, now, GARY_ACTOR)
    return {"task_id": task["id"], "status": task["status"]}


def internal_action_handlers() -> dict[str, ActionHandler]:
    return {
        "create_internal_task": ActionHandler(
            payload_model=CreateTaskRequest,
            summarize=lambda payload, context: f"Create task: {payload.title}",
            check=_check_create,
            record=_record_create,
        ),
        "update_internal_task": ActionHandler(
            payload_model=UpdateTaskRequest,
            summarize=lambda payload, context: (
                f"Update task: {context['task']['title']}"
                if context.get("task")
                else "Update task"
            ),
            check=_check_update,
            record=_record_update,
        ),
    }

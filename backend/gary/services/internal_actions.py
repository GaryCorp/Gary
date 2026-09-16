"""Action handlers that only change Gary's own SQLite state.

External handlers (calendar, email) need Google credentials and are supplied
by the backend application when it builds Gary.
"""

from gary.db.repositories import Repositories
from gary.models.commitment import COMMITMENT_FIELDS, ChangeCommitmentPayload
from gary.models.followup import CreateFollowupRequest
from gary.models.task import CreateTaskRequest, UpdateTaskRequest
from gary.policy import GARY_ACTOR
from gary.services.action_service import ActionHandler
from gary.services.common import NotFoundError, require_project, require_task
from gary.services.followup_service import create_followup_in
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


def _check_followup(repos: Repositories, payload: CreateFollowupRequest) -> dict:
    require_project(repos, payload.project_id)
    require_task(repos, payload.task_id)
    return {}


def _record_followup(repos, payload: CreateFollowupRequest, result: dict, now: str) -> dict:
    followup = create_followup_in(repos, payload, now, GARY_ACTOR)
    return {"followup_id": followup["id"], "title": followup["title"]}


def _check_commitment_change(repos: Repositories, payload: ChangeCommitmentPayload) -> dict:
    commitment = repos.commitments.get(payload.commitment_id)
    if commitment is None:
        raise NotFoundError(f"No commitment with id {payload.commitment_id}")
    if commitment["status"] != "open":
        raise ValueError(f"That commitment is already {commitment['status']}")
    return {"commitment": commitment}


def _summarize_commitment_change(payload: ChangeCommitmentPayload, context: dict) -> str:
    commitment = context.get("commitment")
    if not commitment:
        return "Change a commitment"
    who = f" to {commitment['committed_to']}" if commitment["committed_to"] else ""
    changed = ", ".join(
        name.replace("_", " ")
        for name in COMMITMENT_FIELDS
        if name in payload.model_fields_set
    )
    return f"Change the commitment{who} \"{commitment['description']}\" ({changed})"


def _record_commitment_change(repos, payload: ChangeCommitmentPayload, result: dict, now: str):
    before = repos.commitments.get(payload.commitment_id)
    changes = {
        name: getattr(payload, name)
        for name in COMMITMENT_FIELDS
        if name in payload.model_fields_set
    }
    commitment = repos.commitments.update(payload.commitment_id, now=now, **changes)
    repos.audit.write(
        GARY_ACTOR,
        "commitment_changed",
        f"Changed commitment: {commitment['description']}",
        "commitment",
        commitment["id"],
        {"changes": {k: {"from": before[k], "to": v} for k, v in changes.items()}},
        now=now,
    )
    return {"commitment_id": commitment["id"]}


def internal_action_handlers() -> dict[str, ActionHandler]:
    return {
        "create_followup": ActionHandler(
            payload_model=CreateFollowupRequest,
            summarize=lambda payload, context: f"Follow up: {payload.title}",
            check=_check_followup,
            record=_record_followup,
        ),
        "change_external_commitment": ActionHandler(
            payload_model=ChangeCommitmentPayload,
            summarize=_summarize_commitment_change,
            check=_check_commitment_change,
            record=_record_commitment_change,
        ),
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

"""Action handlers that put work on Alex's engineering queue.

Opening a ticket and re-prioritising one are actions, not direct service
calls, so an unattended planning cycle reaches GitHub through the same path
as everything else: policy sets the risk, the proposal is validated
deterministically, the outcome is recorded, and a GitHub failure is recorded
as a failure rather than reported as a created ticket.

Both are green. A ticket is a specification for work Alex chooses to do, not
a side effect on the world, and every ticket is private to GaryCorp's own
repository. What stops Gary burying Alex is the caps in planning_cycle.py,
not an approval prompt.

The service is resolved lazily, and is absent when GitHub is not configured;
the handlers then refuse rather than erroring, which is the same degraded
behaviour as the engineering tools.
"""

import asyncio
from typing import Callable

from gary.db.repositories import Repositories
from gary.models.engineering import (
    CreateEngineeringTicketPayload,
    SetEngineeringPriorityPayload,
)
from gary.services.action_service import ActionHandler
from gary.services.common import NotFoundError
from gary.services.readiness import CLOSED_STATUSES


def engineering_action_handlers(
    service: Callable[[], object | None],
) -> dict[str, ActionHandler]:
    """``service`` returns the EngineeringService, or None when GitHub is not
    configured on this deployment."""

    def require_service():
        engineering = service()
        if engineering is None:
            raise ValueError(
                "The GitHub engineering integration is not configured on this deployment"
            )
        return engineering

    # ------------------------------------------------------------ create

    def _check_create(repos: Repositories, payload: CreateEngineeringTicketPayload) -> dict:
        require_service()
        task = repos.tasks.get(payload.task_id)
        if task is None:
            raise NotFoundError(f"No task with id {payload.task_id}")
        if task["status"] in CLOSED_STATUSES:
            raise ValueError(f"Task {task['title']!r} is {task['status']}; it needs an open task")
        existing = repos.engineering.get_for_task(payload.task_id)
        if existing and existing["sync_state"] == "synced":
            raise ValueError(
                f"Task {task['title']!r} already has engineering ticket "
                f"#{existing['github_issue_number']}"
            )
        return {"task_title": task["title"]}

    def _summarize_create(payload: CreateEngineeringTicketPayload, context: dict) -> str:
        title = context.get("task_title") or payload.title
        return f"Open a {payload.priority} engineering ticket for Alex: {title}"

    async def _create(payload: CreateEngineeringTicketPayload, context: dict) -> dict:
        from gary.models.engineering import CreateEngineeringTicketRequest

        ticket = await require_service().create_ticket(
            CreateEngineeringTicketRequest(**payload.model_dump(exclude_none=True))
        )
        return {
            "ticket_id": ticket.id,
            "issue_number": ticket.github_issue_number,
            "url": ticket.github_url,
            "priority": ticket.priority,
            "sync_state": ticket.sync_state,
        }

    def _record_create(repos, payload, result: dict, now: str) -> dict:
        return {"ticket_id": result["ticket_id"], "issue_number": result["issue_number"]}

    # ---------------------------------------------------------- priority

    def _check_priority(repos: Repositories, payload: SetEngineeringPriorityPayload) -> dict:
        require_service()
        ticket = repos.engineering.get(payload.ticket_id)
        if ticket is None:
            raise NotFoundError(f"No engineering ticket with id {payload.ticket_id}")
        if ticket["priority"] == payload.priority:
            raise ValueError(
                f"Issue #{ticket['github_issue_number']} is already {payload.priority}"
            )
        if ticket["status"] in ("done",):
            raise ValueError("That ticket is done; its priority no longer means anything")
        task = repos.tasks.get(ticket["task_id"])
        return {
            "from": ticket["priority"],
            "issue_number": ticket["github_issue_number"],
            "title": (task or {}).get("title", ""),
        }

    def _summarize_priority(payload: SetEngineeringPriorityPayload, context: dict) -> str:
        issue = context.get("issue_number")
        where = f"#{issue}" if issue else "an engineering ticket"
        return (
            f"Move {where} {context.get('title', '')} from "
            f"{context.get('from', '?')} to {payload.priority}"
        ).replace("  ", " ")

    async def _set_priority(payload: SetEngineeringPriorityPayload, context: dict) -> dict:
        engineering = require_service()
        row = await asyncio.to_thread(engineering.resolve, payload.ticket_id)
        ticket = await engineering.set_priority(row, payload.priority, payload.reason)
        return {
            "ticket_id": ticket.id,
            "issue_number": ticket.github_issue_number,
            "priority": ticket.priority,
        }

    def _record_priority(repos, payload, result: dict, now: str) -> dict:
        return {"ticket_id": result["ticket_id"], "priority": result["priority"]}

    return {
        "create_engineering_ticket": ActionHandler(
            payload_model=CreateEngineeringTicketPayload,
            summarize=_summarize_create,
            check=_check_create,
            execute=_create,
            record=_record_create,
            audit_event="engineering_ticket_created",
        ),
        "set_engineering_priority": ActionHandler(
            payload_model=SetEngineeringPriorityPayload,
            summarize=_summarize_priority,
            check=_check_priority,
            execute=_set_priority,
            record=_record_priority,
            audit_event="engineering_priority_changed",
        ),
    }


__all__ = ["engineering_action_handlers"]

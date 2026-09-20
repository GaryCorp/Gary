"""Engineering ticket domain model and tool inputs.

``EngineeringTicket`` is the read model Gary sees: the Gary task it belongs
to, the GitHub issue and Project item it maps to, and where it is in the
workflow. GitHub field and option node ids never appear here.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gary.integrations.github.models import EngineeringStatus, Priority
from gary.models.common import EntityId, Minutes, RequestModel, Timestamp

REQUIREMENT_LIMIT = 500
LIST_LIMIT = 15
SyncState = Literal["pending", "synced", "degraded", "needs_reconciliation"]


class EngineeringTicket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    task_id: str
    title: str = ""
    status: EngineeringStatus
    priority: Priority

    requested_by: str = "gary"
    assigned_to: str = "alex"
    assignment_confirmed: bool = False

    github_owner: str
    github_repository: str
    github_issue_number: int | None = None
    github_issue_node_id: str | None = None
    github_project_id: str | None = None
    github_project_item_id: str | None = None
    github_url: str | None = None

    project_id: str | None = None
    estimated_minutes: int | None = None
    due_at: dt.datetime | None = None
    security_review_required: bool = False

    sync_state: SyncState = "pending"
    sync_error: str | None = None
    last_synced_at: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict, task: dict | None = None) -> "EngineeringTicket":
        task = task or {}
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            title=task.get("title", ""),
            status=EngineeringStatus(row["status"]),
            priority=row["priority"],
            assigned_to=row["assigned_to"],
            assignment_confirmed=bool(row["assignment_confirmed"]),
            github_owner=row["github_owner"],
            github_repository=row["github_repository"],
            github_issue_number=row["github_issue_number"],
            github_issue_node_id=row["github_issue_node_id"],
            github_project_id=row["github_project_id"],
            github_project_item_id=row["github_project_item_id"],
            github_url=row["github_url"],
            project_id=task.get("project_id"),
            estimated_minutes=task.get("estimated_minutes"),
            due_at=task.get("deadline"),
            security_review_required=bool(row["security_review_required"]),
            sync_state=row["sync_state"],
            sync_error=row["sync_error"],
            last_synced_at=row["last_synced_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def brief(self) -> dict:
        """What Gary reads out: no node ids, no internal sync plumbing."""
        return {
            "ticket_id": self.id,
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status.value,
            "priority": self.priority,
            "issue_number": self.github_issue_number,
            "url": self.github_url,
            "assigned_to": self.assigned_to,
            "assignment_confirmed": self.assignment_confirmed,
            "security_review_required": self.security_review_required,
            "sync_state": self.sync_state,
            "sync_error": self.sync_error,
        }


class CreateEngineeringTicketRequest(RequestModel):
    task_id: EntityId
    title: str = Field(min_length=5, max_length=240)
    objective: str = Field(min_length=20, max_length=4000)
    requirements: list[str] = Field(min_length=1, max_length=LIST_LIMIT)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=LIST_LIMIT)
    priority: Priority = "P2"
    kind: Literal["feature", "bug"] = "feature"
    dependencies: list[str] = Field(default_factory=list, max_length=LIST_LIMIT)
    security_requirements: str | None = Field(default=None, max_length=2000)
    estimated_minutes: Minutes | None = None
    due_at: Timestamp | None = None
    security_review_required: bool = False

    @classmethod
    def bounded(cls, values: list[str]) -> list[str]:
        return values


class CreateEngineeringTicketPayload(CreateEngineeringTicketRequest):
    """The ``create_engineering_ticket`` action payload.

    Identical to the tool request: a cycle proposing a ticket has to write the
    same specification Gary writes in conversation, so nothing vaguer than a
    real ticket can reach GitHub.
    """


class SetEngineeringPriorityPayload(RequestModel):
    """The ``set_engineering_priority`` action payload.

    Only by ticket_id: the planner is given ticket ids in operations, so there
    is no reason to let it name a ticket any looser way.
    """

    ticket_id: EntityId
    priority: Priority
    reason: str | None = Field(default=None, max_length=1000)


class TicketLookupRequest(RequestModel):
    ticket_id: EntityId | None = None
    task_id: EntityId | None = None
    issue_number: int | None = Field(default=None, strict=True, ge=1)


class ListTicketsRequest(RequestModel):
    status: str | None = Field(default=None, max_length=32)
    open_only: bool = True
    limit: int = Field(default=10, strict=True, ge=1, le=50)


class TransitionRequest(RequestModel):
    ticket_id: EntityId | None = None
    task_id: EntityId | None = None
    reason: str | None = Field(default=None, max_length=1000)


class SetPriorityRequest(RequestModel):
    ticket_id: EntityId | None = None
    task_id: EntityId | None = None
    priority: Priority
    reason: str | None = Field(default=None, max_length=1000)


class CommentRequest(RequestModel):
    ticket_id: EntityId | None = None
    task_id: EntityId | None = None
    comment: str = Field(min_length=5, max_length=4000)

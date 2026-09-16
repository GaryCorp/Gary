from typing import Literal

from pydantic import Field

from gary.models.common import EntityId, Priority, RequestModel, Timestamp


class CreateFollowupRequest(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    due_at: Timestamp
    priority: Priority = 5
    project_id: EntityId | None = None
    task_id: EntityId | None = None


class CompleteFollowupRequest(RequestModel):
    followup_id: EntityId
    status: Literal["completed", "cancelled"] = "completed"

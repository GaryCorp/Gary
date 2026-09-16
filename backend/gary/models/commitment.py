from typing import Literal

from pydantic import Field

from gary.models.common import EntityId, RequestModel, Timestamp


class CreateCommitmentRequest(RequestModel):
    description: str = Field(min_length=1, max_length=1000)
    committed_to: str | None = Field(default=None, max_length=200)
    deadline: Timestamp | None = None
    project_id: EntityId | None = None
    task_id: EntityId | None = None


class ResolveCommitmentRequest(RequestModel):
    commitment_id: EntityId
    status: Literal["fulfilled", "missed", "cancelled"]

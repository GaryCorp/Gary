from typing import Literal

from pydantic import Field, model_validator

from gary.models.common import EntityId, RequestModel, Timestamp

COMMITMENT_FIELDS = ("description", "committed_to", "deadline")


class CreateCommitmentRequest(RequestModel):
    description: str = Field(min_length=1, max_length=1000)
    committed_to: str | None = Field(default=None, max_length=200)
    deadline: Timestamp | None = None
    project_id: EntityId | None = None
    task_id: EntityId | None = None


class ResolveCommitmentRequest(RequestModel):
    commitment_id: EntityId
    status: Literal["fulfilled", "missed", "cancelled"]


class ListCommitmentsRequest(RequestModel):
    status: Literal["open", "all"] = "open"


class ChangeCommitmentPayload(RequestModel):
    """Changing what was promised to someone else needs approval."""

    commitment_id: EntityId
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    committed_to: str | None = Field(default=None, max_length=200)
    deadline: Timestamp | None = None

    @model_validator(mode="after")
    def has_changes(self):
        if not (self.model_fields_set & set(COMMITMENT_FIELDS)):
            raise ValueError("no changes given")
        if "description" in self.model_fields_set and self.description is None:
            raise ValueError("description cannot be cleared")
        return self


class UpdateCommitmentRequest(RequestModel):
    commitment_id: EntityId
    status: Literal["fulfilled", "missed", "cancelled"] | None = None
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    committed_to: str | None = Field(default=None, max_length=200)
    deadline: Timestamp | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def one_kind_of_change(self):
        field_changes = self.model_fields_set & set(COMMITMENT_FIELDS)
        if self.status is None and not field_changes:
            raise ValueError("give a status or a change to the commitment")
        if self.status is not None and field_changes:
            raise ValueError("change the status and the commitment's terms separately")
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set & set(COMMITMENT_FIELDS)}

from typing import Literal

from pydantic import Field, field_validator

from gary.models.common import EntityId, Metadata, Priority, RequestModel, Timestamp

ProjectStatus = Literal[
    "planned", "active", "blocked", "completed", "cancelled", "archived"
]


class CreateProjectRequest(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    status: Literal["planned", "active"] = "active"
    priority: Priority = 5
    deadline: Timestamp | None = None
    metadata: Metadata = None


class GetProjectRequest(RequestModel):
    project_id: EntityId


class ListProjectsRequest(RequestModel):
    include_closed: bool = False


class UpdateProjectRequest(RequestModel):
    project_id: EntityId
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = Field(default=None, min_length=1, max_length=2000)
    status: ProjectStatus | None = None
    priority: Priority | None = None
    deadline: Timestamp | None = None
    metadata: Metadata = None

    @field_validator("name", "objective", "status", "priority")
    @classmethod
    def not_null(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} cannot be cleared")
        return value

from typing import Literal

from pydantic import Field, field_validator, model_validator

from gary.models.common import (
    EntityId,
    Metadata,
    Minutes,
    Priority,
    RequestModel,
    Timestamp,
)

TaskStatus = Literal[
    "todo", "scheduled", "in_progress", "blocked", "waiting", "completed", "cancelled"
]
# Completion goes through task_complete so follow-ups close in the same
# transaction, and scheduling goes through the calendar action.
UpdatableTaskStatus = Literal["todo", "in_progress", "blocked", "waiting", "cancelled"]


class CreateTaskRequest(RequestModel):
    project_id: EntityId | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    priority: Priority = 5
    estimated_minutes: Minutes | None = None
    deadline: Timestamp | None = None
    earliest_start: Timestamp | None = None
    metadata: Metadata = None


class GetTaskRequest(RequestModel):
    task_id: EntityId


class ListTasksRequest(RequestModel):
    project_id: EntityId | None = None
    status: TaskStatus | Literal["open"] = "open"


class UpdateTaskRequest(RequestModel):
    task_id: EntityId
    project_id: EntityId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    status: UpdatableTaskStatus | None = None
    priority: Priority | None = None
    estimated_minutes: Minutes | None = None
    actual_minutes: Minutes | None = None
    deadline: Timestamp | None = None
    earliest_start: Timestamp | None = None
    metadata: Metadata = None

    @field_validator("title", "status", "priority")
    @classmethod
    def not_null(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} cannot be cleared")
        return value

    @model_validator(mode="after")
    def has_changes(self):
        if not (self.model_fields_set - {"task_id"}):
            raise ValueError("no changes given")
        return self


class CompleteTaskRequest(RequestModel):
    task_id: EntityId
    actual_minutes: Minutes | None = None


class DependencyRequest(RequestModel):
    task_id: EntityId
    depends_on_task_id: EntityId

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


class PlanTask(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    priority: Priority = 5
    estimated_minutes: Minutes | None = None
    deadline: Timestamp | None = None
    earliest_start: Timestamp | None = None
    # Titles of other tasks in the same request that must be completed first.
    depends_on: list[str] = Field(default_factory=list, max_length=20)


class CreateProjectWithTasksRequest(CreateProjectRequest):
    tasks: list[PlanTask] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def dependencies_refer_to_tasks(self):
        keys = [" ".join(task.title.split()).casefold() for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("task titles must be unique within the project")
        for task in self.tasks:
            own = " ".join(task.title.split()).casefold()
            for title in task.depends_on:
                key = " ".join(title.split()).casefold()
                if key == own:
                    raise ValueError(f"{task.title} cannot depend on itself")
                if key not in keys:
                    raise ValueError(f"{task.title} depends on unknown task {title!r}")
        return self

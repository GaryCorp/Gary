"""Project board operations, in Gary's vocabulary.

Gary asks for ``EngineeringStatus.READY``; this module owns the translation to
the Project's Status field id and option id. Field and option ids are
discovered once and cached (they are non-secret GitHub node ids), so normal
operation does not re-read the board's schema on every call, and no field is
ever created twice.
"""

import logging

from gary.integrations.github.client import GitHubClient
from gary.integrations.github.exceptions import GitHubConfigurationError, GitHubError
from gary.integrations.github.models import (
    EngineeringStatus,
    ProjectField,
    ProjectItem,
    status_from_project_option,
)

logger = logging.getLogger("gary.github.projects")

STATUS_FIELD = "Status"
# Optional fields: used when the board has them, never required.
PRIORITY_FIELD = "Priority"
DEPARTMENT_FIELD = "Department"
ESTIMATE_FIELD = "Estimate"
GARY_TASK_FIELD = "Gary Task ID"
ENGINEERING_DEPARTMENT = "Engineering"


class ProjectBoard:
    """One configured Project, with its field ids cached."""

    def __init__(self, client: GitHubClient, cache=None):
        self._client = client
        self._fields: dict[str, ProjectField] | None = None
        self._project_id: str | None = None
        # Optional persistence for discovered ids (non-secret), e.g. SQLite.
        self._cache = cache

    def reset(self) -> None:
        self._fields = None
        self._project_id = None

    async def fields(self, project_id: str) -> dict[str, ProjectField]:
        if self._fields is not None and self._project_id == project_id:
            return self._fields
        if self._cache is not None and self._project_id != project_id:
            stored = self._cache.load(project_id)
            if stored:
                self._fields = {name: ProjectField(**value) for name, value in stored.items()}
                self._project_id = project_id
                return self._fields

        fields = await self._client.get_project_fields(project_id)
        if STATUS_FIELD not in fields:
            raise GitHubConfigurationError(
                f"The Project has no {STATUS_FIELD} field. Run the setup command "
                "(python -m gary.integrations.github.setup) and add it in GitHub."
            )
        self._fields = fields
        self._project_id = project_id
        if self._cache is not None:
            self._cache.save(project_id, {name: f.model_dump() for name, f in fields.items()})
        return fields

    async def status_field(self, project_id: str) -> ProjectField:
        return (await self.fields(project_id))[STATUS_FIELD]

    async def option_id(self, project_id: str, status: EngineeringStatus) -> str | None:
        field = await self.status_field(project_id)
        return field.option_id(status.project_option)

    async def add_issue(self, project_id: str, issue_node_id: str) -> str:
        """Add the issue to the board, or return the existing item id."""
        existing = await self._client.find_project_item(project_id, issue_node_id)
        if existing:
            return existing
        return await self._client.add_issue_to_project(project_id, issue_node_id)

    async def set_status(
        self, project_id: str, item_id: str, status: EngineeringStatus
    ) -> str | None:
        """Set the Project Status. Returns the option name actually set, or
        None when the board has no option for this status (e.g. Blocked on a
        board that never defined it)."""
        field = await self.status_field(project_id)
        option = field.option_id(status.project_option)
        if option is None:
            # Re-read once: the board may have gained the option since caching.
            self.reset()
            field = await self.status_field(project_id)
            option = field.option_id(status.project_option)
        if option is None:
            logger.warning(
                "Project has no %r Status option; leaving the board unchanged",
                status.project_option,
            )
            return None
        await self._client.update_project_field(
            project_id, item_id, field.id, {"singleSelectOptionId": option}
        )
        return status.project_option

    async def set_optional_fields(
        self,
        project_id: str,
        item_id: str,
        *,
        priority: str | None = None,
        estimate_minutes: int | None = None,
        task_id: str | None = None,
        department: str = ENGINEERING_DEPARTMENT,
    ) -> dict[str, str]:
        """Fill Priority, Department, Estimate, and Gary Task ID when the board
        has them. Missing fields are skipped; a failure here never fails the
        ticket."""
        fields = await self.fields(project_id)
        written: dict[str, str] = {}

        async def write(name: str, value: dict, shown: str) -> None:
            field = fields.get(name)
            if field is None:
                return
            try:
                await self._client.update_project_field(project_id, item_id, field.id, value)
                written[name] = shown
            except GitHubError as exc:
                logger.warning("Could not set Project field %s: %s", name, exc)

        if priority:
            field = fields.get(PRIORITY_FIELD)
            if field is not None:
                option = field.option_id(priority)
                if option:
                    await write(PRIORITY_FIELD, {"singleSelectOptionId": option}, priority)
        field = fields.get(DEPARTMENT_FIELD)
        if field is not None:
            option = field.option_id(department)
            if option:
                await write(DEPARTMENT_FIELD, {"singleSelectOptionId": option}, department)
        if estimate_minutes is not None and ESTIMATE_FIELD in fields:
            if fields[ESTIMATE_FIELD].data_type == "NUMBER":
                await write(
                    ESTIMATE_FIELD,
                    {"number": round(estimate_minutes / 60, 2)},
                    f"{round(estimate_minutes / 60, 2)} h",
                )
        if task_id and GARY_TASK_FIELD in fields:
            if fields[GARY_TASK_FIELD].data_type == "TEXT":
                await write(GARY_TASK_FIELD, {"text": task_id}, task_id)
        return written

    async def item(self, item_id: str) -> ProjectItem:
        return await self._client.get_project_item(item_id)

    async def status_of(self, item_id: str) -> EngineeringStatus | None:
        return status_from_project_option((await self.item(item_id)).status_option)

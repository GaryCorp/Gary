"""Typed results from the GitHub API, and the engineering vocabulary.

Gary works in semantic statuses (``EngineeringStatus``); GitHub's Project
field and option node ids stay inside this integration.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

Priority = Literal["P0", "P1", "P2", "P3"]
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")


class EngineeringStatus(StrEnum):
    BACKLOG = "backlog"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    SECURITY_REVIEW = "security_review"
    DONE = "done"
    BLOCKED = "blocked"

    @property
    def project_option(self) -> str:
        """The Status option name in the GitHub Project."""
        return PROJECT_STATUS_NAMES[self]


PROJECT_STATUS_NAMES: dict[EngineeringStatus, str] = {
    EngineeringStatus.BACKLOG: "Backlog",
    EngineeringStatus.READY: "Ready",
    EngineeringStatus.IN_PROGRESS: "In Progress",
    EngineeringStatus.REVIEW: "Review",
    EngineeringStatus.SECURITY_REVIEW: "Security Review",
    EngineeringStatus.DONE: "Done",
    EngineeringStatus.BLOCKED: "Blocked",
}

# The Status options a GaryCorp Engineering Project needs. Blocked is a Gary
# state that only reaches the Project when the field happens to offer it.
REQUIRED_STATUS_OPTIONS: tuple[str, ...] = (
    "Backlog",
    "Ready",
    "In Progress",
    "Review",
    "Security Review",
    "Done",
)


def status_from_project_option(name: str | None) -> EngineeringStatus | None:
    if not name:
        return None
    wanted = " ".join(name.split()).casefold()
    for status, option in PROJECT_STATUS_NAMES.items():
        if option.casefold() == wanted:
            return status
    return None


# Allowed moves. Blocked is reachable from any active state, and leaves to
# Ready or In Progress. Done is never reached from Review when a security
# review is required; EngineeringTicketService enforces that separately.
ALLOWED_TRANSITIONS: dict[EngineeringStatus, frozenset[EngineeringStatus]] = {
    EngineeringStatus.BACKLOG: frozenset({EngineeringStatus.READY, EngineeringStatus.BLOCKED}),
    EngineeringStatus.READY: frozenset(
        {EngineeringStatus.IN_PROGRESS, EngineeringStatus.BACKLOG, EngineeringStatus.BLOCKED}
    ),
    EngineeringStatus.IN_PROGRESS: frozenset(
        {EngineeringStatus.REVIEW, EngineeringStatus.READY, EngineeringStatus.BLOCKED}
    ),
    EngineeringStatus.REVIEW: frozenset(
        {
            EngineeringStatus.SECURITY_REVIEW,
            EngineeringStatus.DONE,
            EngineeringStatus.IN_PROGRESS,
            EngineeringStatus.BLOCKED,
        }
    ),
    EngineeringStatus.SECURITY_REVIEW: frozenset(
        {EngineeringStatus.DONE, EngineeringStatus.IN_PROGRESS, EngineeringStatus.BLOCKED}
    ),
    EngineeringStatus.DONE: frozenset(),
    EngineeringStatus.BLOCKED: frozenset({EngineeringStatus.READY, EngineeringStatus.IN_PROGRESS}),
}

ACTIVE_STATUSES = frozenset(
    {
        EngineeringStatus.BACKLOG,
        EngineeringStatus.READY,
        EngineeringStatus.IN_PROGRESS,
        EngineeringStatus.REVIEW,
        EngineeringStatus.SECURITY_REVIEW,
    }
)

# Labels the integration uses. Existing labels are reused, never duplicated.
BASE_LABELS: tuple[str, ...] = ("gary-assigned", "engineering")
LABEL_COLORS: dict[str, str] = {
    "gary-assigned": "5319e7",
    "engineering": "1d76db",
    "feature": "0e8a16",
    "bug": "d73a4a",
    "security-review": "b60205",
    "blocked": "e99695",
    "needs-alex": "fbca04",
    "P0": "b60205",
    "P1": "d93f0b",
    "P2": "fbca04",
    "P3": "c2e0c6",
}

TicketKind = Literal["feature", "bug"]


class Repository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    owner: str
    name: str
    node_id: str
    private: bool
    has_issues: bool = True
    html_url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    node_id: str
    title: str
    state: Literal["open", "closed"]
    state_reason: str | None = None
    html_url: str = ""
    assignees: list[str] = []
    labels: list[str] = []


class ProjectField(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    data_type: str
    # Option name -> option id, for single-select fields.
    options: dict[str, str] = {}

    def option_id(self, name: str) -> str | None:
        wanted = " ".join(name.split()).casefold()
        for option, option_id in self.options.items():
            if option.casefold() == wanted:
                return option_id
        return None


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    number: int
    title: str
    # GitHub reports Projects v2 visibility as public/not public.
    public: bool
    owner_login: str
    url: str = ""

    @property
    def private(self) -> bool:
        return not self.public


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    # Current value of each single-select field, by field name.
    field_values: dict[str, str] = {}

    @property
    def status_option(self) -> str | None:
        return self.field_values.get("Status")

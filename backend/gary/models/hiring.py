"""Hiring a new AI employee: what Gary may propose.

The payload carries identity, the capability gap that justifies the role, the
specialty Gary wrote, and the tools requested. What the tools may be is
decided in code (``agents/hiring.py``), not here.
"""

from pydantic import Field

from gary.models.common import RequestModel

MAX_TOOLS = 10


class HireEmployeePayload(RequestModel):
    """The action Alex approves on the approvals page."""

    agent_id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=2, max_length=60)
    title: str = Field(min_length=3, max_length=80)
    department: str = Field(min_length=2, max_length=80)
    notebook: str = Field(min_length=2, max_length=60)
    capability_gap: str = Field(min_length=20, max_length=1000)
    specialty: str = Field(min_length=40, max_length=1500)
    personality: str | None = Field(default=None, max_length=400)
    tools: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)


class ProposeHireRequest(HireEmployeePayload):
    """Gary's tool input. Same shape: the tool proposes exactly what Alex
    will see and approve, with no field added on the way through."""

    reason: str = Field(min_length=20, max_length=1000)

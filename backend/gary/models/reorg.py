"""What a reorganisation may propose.

The shape is the safeguard. `modify_permissions` is RED in policy, so Gary
may never change what anyone is allowed to do -- and rather than instruct him
not to, there is simply no field here in which to put a tool. A proposal
naming `tools` or `can_delegate` fails validation before policy ever sees it,
because RequestModel forbids unknown fields and `change` is a closed set.

Reporting lines and titles move the org chart. `focus` is the one that
changes behaviour, because it feeds the agent's prompt.
"""

from typing import Literal

from pydantic import Field, field_validator

from gary.models.common import RequestModel

# What may be changed about a colleague. Deliberately does not include
# anything that grants or removes capability.
CHANGE_KINDS = ("title", "department", "reports_to", "focus")
ChangeKind = Literal["title", "department", "reports_to", "focus"]

MAX_CHANGES = 5
VALUE_LIMIT = 600


class RosterChange(RequestModel):
    agent_id: str = Field(min_length=1, max_length=32)
    change: ChangeKind
    # The new title, department, manager's agent_id, or focus.
    to: str = Field(min_length=2, max_length=VALUE_LIMIT)
    reason: str = Field(min_length=10, max_length=600)

    @field_validator("agent_id", "to")
    @classmethod
    def one_line(cls, value: str) -> str:
        return " ".join(value.split())


class ReorganisationPayload(RequestModel):
    """One coherent restructuring, not a pile of unrelated edits."""

    changes: list[RosterChange] = Field(min_length=1, max_length=MAX_CHANGES)
    rationale: str = Field(min_length=20, max_length=2000)


__all__ = ["CHANGE_KINDS", "MAX_CHANGES", "ChangeKind", "ReorganisationPayload", "RosterChange"]

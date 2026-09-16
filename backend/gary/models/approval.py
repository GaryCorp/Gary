from typing import Literal

from pydantic import Field

from gary.models.common import EntityId, RequestModel


class ResolveApprovalRequest(RequestModel):
    approval_id: EntityId
    decision: Literal["approved", "rejected"]
    confirmed: bool = False
    note: str | None = Field(default=None, max_length=500)

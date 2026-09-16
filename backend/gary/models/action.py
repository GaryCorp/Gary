import re
from typing import Any

from pydantic import Field, field_validator, model_validator

from gary.models.common import EntityId, RequestModel, Timestamp

# One plain address; the same rule as the voice email tools.
EMAIL_ADDRESS_PATTERN = re.compile(
    r"[^@\s,;:<>()\[\]\"'{}]+@[^@\s,;:<>()\[\]\"'{}]+\.[A-Za-z]{2,}"
)
NO_REPLY_PATTERN = re.compile(
    r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce",
    re.IGNORECASE,
)


class ProposeActionRequest(RequestModel):
    action_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    payload: dict[str, Any]
    reason: str | None = Field(default=None, max_length=1000)
    project_id: EntityId | None = None
    task_id: EntityId | None = None


class TimeRangeMixin(RequestModel):
    @model_validator(mode="after")
    def end_after_start(self):
        start, end = self.range()
        if end <= start:
            raise ValueError("end must be later than start")
        return self

    def range(self) -> tuple[str, str]:
        raise NotImplementedError


class ScheduleTaskPayload(TimeRangeMixin):
    task_id: EntityId
    start: Timestamp
    end: Timestamp

    def range(self):
        return self.start, self.end


class MoveCalendarEventPayload(TimeRangeMixin):
    task_id: EntityId
    new_start: Timestamp
    new_end: Timestamp

    def range(self):
        return self.new_start, self.new_end


class SendExternalEmailPayload(RequestModel):
    to: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)

    @field_validator("to")
    @classmethod
    def one_plain_address(cls, value: str) -> str:
        if not EMAIL_ADDRESS_PATTERN.fullmatch(value):
            raise ValueError("must be exactly one email address, like name@example.com")
        if NO_REPLY_PATTERN.search(value):
            raise ValueError("that address does not accept email")
        return value

    @field_validator("subject")
    @classmethod
    def single_line(cls, value: str) -> str:
        return re.sub(r"[\r\n]+", " ", value).strip()

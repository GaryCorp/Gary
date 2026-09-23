"""Tool inputs for performance reviews."""

from pydantic import Field, model_validator

from gary.models.common import EntityId, RequestModel

SUBJECT = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


class ScorecardRequest(RequestModel):
    subject: str = SUBJECT
    days: int = Field(default=28, strict=True, ge=1, le=365)


class WriteReviewRequest(RequestModel):
    subject: str = SUBJECT
    days: int = Field(default=28, strict=True, ge=1, le=365)


class ListReviewsRequest(RequestModel):
    subject: str | None = Field(default=None, max_length=64)
    unacknowledged_of_me: bool = False
    limit: int = Field(default=10, strict=True, ge=1, le=20)

    @model_validator(mode="after")
    def whose(self):
        if not self.subject and not self.unacknowledged_of_me:
            raise ValueError("give a subject, or ask for your own unanswered reviews")
        return self


class AcknowledgeReviewRequest(RequestModel):
    review_id: EntityId
    response: str = Field(min_length=20, max_length=4000)

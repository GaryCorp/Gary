from pydantic import Field, field_validator

from gary.models.common import EntityId, RequestModel

BRIEF_LIMIT = 800
CONSTRAINT_LIMIT = 300
MAX_CONSTRAINTS = 8


class StartProductSearchRequest(RequestModel):
    """What GaryCorp is looking for, and what it has to live within."""

    brief: str = Field(min_length=10, max_length=BRIEF_LIMIT)
    # Budget, skills, time, markets to avoid: short named facts, not prose.
    constraints: dict[str, str] | None = None
    # None means the deployment's default ceiling.
    max_rounds: int | None = Field(default=None, strict=True, ge=1, le=10)

    @field_validator("constraints")
    @classmethod
    def bounded_constraints(cls, value):
        if value is None:
            return value
        if len(value) > MAX_CONSTRAINTS:
            raise ValueError(f"at most {MAX_CONSTRAINTS} constraints")
        for name, text in value.items():
            if not name.strip() or len(text) > CONSTRAINT_LIMIT:
                raise ValueError(f"each constraint needs a name and at most {CONSTRAINT_LIMIT} characters")
        return value


class ProductSearchLookup(RequestModel):
    search_id: EntityId | None = None


class StopProductSearchRequest(RequestModel):
    search_id: EntityId | None = None
    reason: str = Field(default="", max_length=300)

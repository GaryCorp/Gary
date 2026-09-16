import json
import uuid
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from gary.timeutil import parse_timestamp

# Core queryable fields belong in real columns, never in metadata_json.
RESERVED_METADATA_KEYS = frozenset(
    {
        "id",
        "status",
        "priority",
        "deadline",
        "due_at",
        "earliest_start",
        "scheduled_start",
        "scheduled_end",
        "calendar_event_id",
        "project_id",
        "task_id",
        "created_at",
        "updated_at",
        "completed_at",
    }
)
METADATA_JSON_LIMIT = 4000


def _timestamp(value: str) -> str:
    return parse_timestamp(value, "timestamp")


def _entity_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("must be an id returned by a Gary tool (a UUID)") from None


def _metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    reserved = RESERVED_METADATA_KEYS & set(value)
    if reserved:
        raise ValueError(
            f"metadata cannot contain core fields {sorted(reserved)}; "
            "use the dedicated fields instead"
        )
    if len(json.dumps(value)) > METADATA_JSON_LIMIT:
        raise ValueError(f"metadata cannot exceed {METADATA_JSON_LIMIT} characters")
    return value


# ISO 8601 with a timezone offset, normalized to UTC.
Timestamp = Annotated[str, AfterValidator(_timestamp)]
Priority = Annotated[int, Field(strict=True, ge=1, le=10)]
Minutes = Annotated[int, Field(strict=True, ge=0, le=100_000)]
EntityId = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_entity_id)]
Metadata = Annotated[dict[str, Any] | None, AfterValidator(_metadata)]


class RequestModel(BaseModel):
    """Base for all tool inputs: unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "input"
        message = error["msg"].removeprefix("Value error, ")
        parts.append(f"{location}: {message}")
    return "; ".join(parts)


def validate_request(model: type[RequestModel], data: dict) -> RequestModel:
    """Validate untrusted input, raising ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("arguments must be an object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(validation_message(exc)) from None


def provided_fields(request: RequestModel, exclude: set[str]) -> dict:
    """Fields the caller actually sent, so an omitted field is left unchanged
    while an explicit null clears it."""
    return {
        name: getattr(request, name)
        for name in request.model_fields_set
        if name not in exclude
    }

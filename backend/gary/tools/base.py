import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from gary.container import Gary
from gary.timeutil import to_local

TIMESTAMP_FIELDS = frozenset(
    {
        "now",
        "deadline",
        "earliest_start",
        "scheduled_start",
        "scheduled_end",
        "due_at",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "resolved_at",
        "executed_at",
        "timestamp",
        "start",
        "end",
        "new_start",
        "new_end",
    }
)
JSON_FIELDS = {
    "metadata_json": "metadata",
    "payload_json": "payload",
    "result_json": "result",
    "plan_json": "plan",
    "details_json": "details",
}


@dataclass
class ToolContext:
    gary: Gary
    # Per voice conversation. approval_ids holds approvals Gary has shown the
    # user in this conversation; only those can be resolved by voice.
    session: dict = field(default_factory=dict)
    # Optional backend integrations: "calendar" (busy times), "notebook"
    # (planning notes), "planning_cycle". Tools that need one report when it
    # is unavailable.
    integrations: dict = field(default_factory=dict)

    def approval_ids(self) -> set[str]:
        return self.session.setdefault("approval_ids", set())

    def integration(self, name: str):
        service = self.integrations.get(name)
        if service is None:
            raise ValueError(f"The {name} integration is not available right now")
        return service


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[dict]]

    def schema(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def present(value, ctx: ToolContext):
    """Make service output readable for Gary: local times, parsed JSON."""
    if isinstance(value, list):
        return [present(item, ctx) for item in value]
    if not isinstance(value, dict):
        return value

    out = {}
    for key, item in value.items():
        if key in JSON_FIELDS:
            out[JSON_FIELDS[key]] = present(json.loads(item), ctx) if item else None
        elif key in TIMESTAMP_FIELDS and isinstance(item, str):
            out[key] = to_local(item, ctx.gary.timezone)
        else:
            out[key] = present(item, ctx)
    return out


async def run_sync(function, *args, **kwargs):
    # SQLite work is short but blocking; keep it off the event loop.
    return await asyncio.to_thread(function, *args, **kwargs)


# JSON schema helpers for Realtime function tools.

def obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def string(description: str, enum: list[str] | None = None, nullable: bool = False) -> dict:
    schema = {"type": ["string", "null"] if nullable else "string", "description": description}
    if enum:
        schema["enum"] = enum + ([None] if nullable else [])
    return schema


def integer(
    description: str,
    minimum: int | None = None,
    maximum: int | None = None,
    nullable: bool = False,
) -> dict:
    schema = {"type": ["integer", "null"] if nullable else "integer", "description": description}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


def timestamp(description: str, nullable: bool = False) -> dict:
    return string(f"{description} ISO 8601 with offset.", nullable=nullable)


def priority(description: str = "Priority") -> dict:
    return integer(f"{description}, 1-10 (5 normal).", 1, 10)

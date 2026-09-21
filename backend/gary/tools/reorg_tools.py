"""Gary's reorganisation tools.

He can read the company's current shape alongside what each person's record
actually shows, and propose a different shape. He cannot impose one: the
proposal is a yellow action that waits for Alex, and approving it files an
engineering ticket rather than changing anything.

There is deliberately no tool here for changing what anyone is permitted to
do. That is `modify_permissions`, which policy refuses outright.
"""

from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.models.common import validate_request
from gary.models.reorg import CHANGE_KINDS, MAX_CHANGES, ReorganisationPayload
from gary.services.performance import employee_scorecard, manager_scorecard
from gary.tools.base import Tool, ToolContext, obj, present, run_sync, string

# How far back the record is read when arguing about the company's shape.
REORG_WINDOW_DAYS = 30


def _service(ctx: ToolContext):
    return ctx.integration("agents")


async def org_chart(args: dict, ctx: ToolContext) -> dict:
    """The company as it is, with the record behind each person."""
    if args:
        raise ValueError("org_chart takes no arguments")

    import datetime as dt

    from gary.services.common import clock_now
    from gary.timeutil import format_utc, to_datetime

    now = clock_now(ctx.gary.planning.clock)
    since = format_utc(to_datetime(now) - dt.timedelta(days=REORG_WINDOW_DAYS))
    registry = _service(ctx).registry

    def read():
        with ctx.gary.db.read() as conn:
            repos = Repositories.bind(conn)
            people = []
            for definition in registry.employees():
                people.append(
                    {
                        "agent_id": definition.agent_id,
                        "name": definition.name,
                        "title": definition.title,
                        "department": definition.department,
                        "reports_to": definition.reports_to,
                        "focus": getattr(definition, "specialty", "") or "",
                        "record": employee_scorecard(repos, definition.agent_id, since, now),
                    }
                )
            return people, manager_scorecard(repos, since, now)

    people, manager = await run_sync(read)
    return {
        "days": REORG_WINDOW_DAYS,
        "employees": present(people, ctx),
        "gary": present(manager, ctx),
        "note": (
            "Argue from the record: someone idle, someone overloaded, two people "
            "covering the same ground. You may propose a change of title, "
            "department, reporting line or focus. You cannot change what anyone "
            "is permitted to do, and you cannot reorganise yourself."
        ),
    }


async def propose_reorganisation(args: dict, ctx: ToolContext) -> dict:
    payload = validate_request(ReorganisationPayload, args)
    result = await ctx.gary.actions.propose(
        ProposeActionRequest(
            action_type="propose_reorganisation",
            payload=payload.model_dump(mode="json"),
            reason=payload.rationale[:1000],
        )
    )
    if result.get("approval_id"):
        ctx.approval_ids().add(result["approval_id"])
    return {
        "proposal": present(result, ctx),
        "note": (
            "This changes nothing yet. It waits for Alex on the approvals page, "
            "and approving it opens an engineering ticket for him to make the "
            "change in the roster. Do not describe anyone's title or reporting "
            "line as changed until team_list shows it."
        ),
    }


TOOLS = [
    Tool(
        "org_chart",
        "The company's current shape - who holds which title, in which department, "
        "reporting to whom, and what each is for - together with what their record "
        "over the last month actually shows: assignments completed and failed, cost "
        "per report, and how often they were confident and wrong. Read this before "
        "proposing any reorganisation.",
        obj({}),
        org_chart,
    ),
    Tool(
        "propose_reorganisation",
        "Propose a different shape for GaryCorp: a change of title, department, "
        "reporting line, or what someone is for. Alex approves or rejects it, and "
        "approving opens an engineering ticket for him to make the change; nothing "
        "moves until he does. Propose only what the record supports - someone with "
        "no work, someone overloaded, two people covering the same ground - and "
        "never to make the chart look tidier. You cannot change what anyone is "
        "permitted to do, and you cannot reorganise yourself.",
        obj(
            {
                "changes": {
                    "type": "array",
                    "description": (
                        f"Between 1 and {MAX_CHANGES} changes. Each is an object with "
                        "agent_id, change, to, and reason."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["agent_id", "change", "to", "reason"],
                        "properties": {
                            "agent_id": string("Whose role changes."),
                            "change": string("What changes.", list(CHANGE_KINDS)),
                            "to": string(
                                "The new title, department, manager's agent_id, or focus."
                            ),
                            "reason": string("What in their record supports this."),
                        },
                    },
                },
                "rationale": string(
                    "Why the company is better organised this way, in a few sentences."
                ),
            },
            ("changes", "rationale"),
        ),
        propose_reorganisation,
    ),
]

"""Gary's hiring tool.

Gary can propose a colleague; only Alex can hire one. The proposal becomes a
yellow action that waits on the approvals web page, and the tools a new
employee may hold are capped in code, so the worst a bad proposal can do is
waste Alex's time.
"""

from gary.agents.hiring import HIREABLE_TOOLS
from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.models.common import validate_request
from gary.models.hiring import ProposeHireRequest
from gary.services.hiring_actions import proposal_context
from gary.tools.base import Tool, ToolContext, obj, run_sync, string


def _service(ctx: ToolContext):
    return ctx.integration("agents")


async def hiring_context(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("hiring_context takes no arguments")

    def read():
        with ctx.gary.db.read() as conn:
            return proposal_context(_service(ctx).registry, Repositories.bind(conn))

    context = await run_sync(read)
    return {
        **context,
        "note": (
            "Propose a colleague only for a capability the company actually lacks and "
            "keeps needing. Alex approves every hire on the approvals web page."
        ),
    }


async def propose_new_employee(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ProposeHireRequest, args)
    payload = request.model_dump(exclude={"reason"})
    result = await ctx.gary.actions.propose(
        ProposeActionRequest(action_type="hire_employee", payload=payload, reason=request.reason)
    )
    return {
        "proposal": result,
        "note": (
            "This is a proposal, not a hire. It waits for Alex on the approvals page at "
            "http://localhost:8000/approvals and cannot be approved by voice. Do not say "
            f"{request.name} has joined until it is approved."
        ),
    }


TOOLS = [
    Tool(
        "hiring_context",
        "Before proposing a colleague: who already works at GaryCorp, which Joplin "
        "notebooks are taken, and exactly which tools a new employee may be given.",
        obj({}),
        hiring_context,
    ),
    Tool(
        "propose_new_employee",
        "Propose hiring a new AI employee for a capability GaryCorp lacks. Alex approves "
        "or rejects it on the approvals web page; you cannot hire anyone yourself, and you "
        "cannot approve it by voice. Only propose when the gap is real and recurring, not "
        "for one task, and never to make the company look bigger. A new employee is "
        "advisory like the others and may only have the tools hiring_context lists.",
        obj(
            {
                "agent_id": string("Lowercase id, e.g. 'nina'. Letters, digits, underscores."),
                "name": string("Their first name, as the team will say it."),
                "title": string("Their role, e.g. 'Director of Customer Insight'."),
                "department": string("Their department, e.g. 'Customer'."),
                "notebook": string("Their own Joplin notebook name, usually their first name."),
                "capability_gap": string(
                    "The gap in the company this role fills, with the evidence for it."
                ),
                "specialty": string(
                    "What they are for and how they should work: the questions they answer, "
                    "what they must separate, and where their judgment ends."
                ),
                "personality": string("Their manner in a sentence.", nullable=True),
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(HIREABLE_TOOLS)},
                    "description": "The tools they need. Ask for the fewest that do the job.",
                },
                "reason": string("Why the company should hire them now."),
            },
            ("agent_id", "name", "title", "department", "notebook", "capability_gap",
             "specialty", "reason"),
        ),
        propose_new_employee,
    ),
]

"""Gary's tools for managing the specialist team.

Delegation is asynchronous: assignments are persisted and run in the
background, and Gary reads the reports when they are ready. Per conversation,
Gary may create at most max_assignments_per_plan assignments.
"""

from gary.agents.roster import MANAGER_ID
from gary.agents.service import DelegateRequest, FollowUpRequest, ReviewRequest
from pydantic import Field

from gary.models.common import EntityId, RequestModel, validate_request
from gary.tools.base import Tool, ToolContext, integer, obj, present, run_sync, string

AGENT_IDS = ["susan", "dave", "linda", "catherine", "lauren"]


class AssignmentLookup(RequestModel):
    assignment_id: EntityId | None = None
    agent_id: str | None = Field(default=None, max_length=32)
    about: str | None = Field(default=None, min_length=2, max_length=300)


class AssignmentList(RequestModel):
    agent_id: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, pattern=r"^(queued|running|completed|failed|cancelled)$")
    limit: int = Field(default=5, strict=True, ge=1, le=20)


class ReviewLookup(RequestModel):
    review_id: EntityId | None = None


def _service(ctx: ToolContext):
    return ctx.integration("agents")


def _refund_budget(ctx: ToolContext, count: int) -> None:
    ctx.session["agent_assignments_created"] = max(0, ctx.session.get("agent_assignments_created", 0) - count)


def _use_budget(ctx: ToolContext, count: int) -> None:
    service = _service(ctx)
    limit = service.registry.limits.max_assignments_per_plan
    used = ctx.session.get("agent_assignments_created", 0)
    if used + count > limit:
        raise ValueError(
            f"You have already created {used} assignments in this conversation (limit {limit}). "
            "Use the reports you have, or ask Alex before commissioning more work."
        )
    ctx.session["agent_assignments_created"] = used + count


def _brief(assignment: dict) -> dict:
    return {
        "assignment_id": assignment["id"],
        "assigned_to": assignment["assigned_to"],
        "status": assignment["status"],
    }


async def team_list(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("team_list takes no arguments")
    return present(await run_sync(_service(ctx).team), ctx)


async def delegate_to_agent(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(DelegateRequest, args)
    _use_budget(ctx, 1)
    try:
        assignment = await _service(ctx).delegate(request, MANAGER_ID)
    except Exception:
        _refund_budget(ctx, 1)
        raise
    return {
        "assignment": _brief(assignment),
        "note": "Running in the background. You will be told when the report is ready; "
                "read it with agent_assignment_get.",
    }


async def run_management_review(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ReviewRequest, args)
    count = len(request.agents) if request.agents else len(_service(ctx).registry.employee_ids())
    _use_budget(ctx, count)
    try:
        result = await _service(ctx).start_review(request, MANAGER_ID)
    except Exception:
        _refund_budget(ctx, count)
        raise
    return {
        "review_id": result["review"]["id"],
        "assignments": [_brief(a) for a in result["assignments"]],
        "note": "Each specialist reviews independently in the background. Read the results "
                "with management_review_get once they are ready.",
    }


async def management_review_follow_up(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(FollowUpRequest, args)
    _use_budget(ctx, 1)
    try:
        assignment = await _service(ctx).follow_up(request, MANAGER_ID)
    except Exception:
        _refund_budget(ctx, 1)
        raise
    return {"assignment": _brief(assignment)}


async def agent_assignment_get(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(AssignmentLookup, args)
    service = _service(ctx)
    if request.assignment_id:
        return {"assignment": await run_sync(service.get_assignment, request.assignment_id)}
    if request.agent_id and request.about:
        found = await run_sync(service.find_assignment, request.agent_id, request.about)
        return {"assignment": found["match"], "other_recent_assignments": found["other_recent_assignments"],
                "note": found["note"]}
    if request.agent_id:
        assignment = await run_sync(service.latest_assignment, request.agent_id)
        if assignment is None:
            return {"assignment": None, "note": f"{request.agent_id} has no assignments yet"}
        return {"assignment": assignment}
    raise ValueError("give assignment_id or agent_id")


async def agent_assignments_list(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(AssignmentList, args)
    assignments = await run_sync(_service(ctx).list_assignments, request.agent_id, request.status, request.limit)
    return {
        "count": len(assignments),
        "assignments": [
            {k: a[k] for k in ("assignment_id", "agent", "objective", "status", "created_at", "completed_at", "error")}
            | {"summary": (a["report"] or {}).get("summary")}
            for a in assignments
        ],
    }


async def management_review_get(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ReviewLookup, args)
    review = await run_sync(_service(ctx).get_review, request.review_id)
    return {"review": review.model_dump(mode="json")}


TOOLS = [
    Tool(
        "team_list",
        "List GaryCorp's team (Susan: research and strategy; Dave: security; Linda: "
        "operations; Catherine: finance; Marcus: marketing; Lauren: ethics), their status, "
        "and recent assignments and reviews.",
        obj({}),
        team_list,
    ),
    Tool(
        "delegate_to_agent",
        "Give one specialist an assignment when their expertise would materially improve "
        "a decision. Susan: research, options, evidence, strategy. Dave: threat modeling, "
        "permissions, controls. Linda: execution plans, feasibility, dependencies, "
        "scheduling. Catherine: costs, budgets, AI spending, and buying things on "
        "GaryCorp's debit card (she can only request a purchase; the user approves it "
        "on the approvals web page). Marcus: audience, positioning, messaging, channels, "
        "and what the company can honestly claim. Lauren: ethical review of a decision with the EASE "
        "framework (stakeholders, harms, consent, safeguards). Runs in the background. "
        "Do not delegate trivial work.",
        obj(
            {
                "agent_id": string("Which specialist.", AGENT_IDS),
                "objective": string("What you need from them, specifically."),
                "project_id": string("Related project_id, if any."),
                "task_id": string("Related task_id, if any."),
                "context": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Short named pieces of context they need, e.g. {\"proposal\": \"...\"}.",
                },
                "priority": integer("Priority 1-10 (5 normal).", 1, 10),
            },
            ("agent_id", "objective"),
        ),
        delegate_to_agent,
    ),
    Tool(
        "run_management_review",
        "Ask several specialists to review one topic independently (none sees another's "
        "report). Defaults to all five; pass agents to choose a subset. Use for decisions "
        "that need more than one department.",
        obj(
            {
                "topic": string("The proposal or question under review."),
                "agents": {"type": "array", "items": {"type": "string", "enum": AGENT_IDS},
                           "description": "Which specialists; omit for all five."},
                "questions": {"type": "object", "additionalProperties": {"type": "string"},
                              "description": "Optional specific question per agent id."},
                "project_id": string("Related project_id, if any."),
                "context": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "Short named pieces of shared context."},
            },
            ("topic",),
        ),
        run_management_review,
    ),
    Tool(
        "management_review_follow_up",
        "Ask one specialist a single targeted follow-up after a review finished, optionally "
        "sharing named colleagues' reports with them. One follow-up per review.",
        obj(
            {
                "review_id": string("review_id of the finished review."),
                "agent_id": string("Who to ask.", AGENT_IDS),
                "question": string("The targeted question."),
                "share_reports_from": {"type": "array", "items": {"type": "string", "enum": AGENT_IDS},
                                       "description": "Whose reports to share with them, if any."},
            },
            ("review_id", "agent_id", "question"),
        ),
        management_review_follow_up,
    ),
    Tool(
        "agent_assignment_get",
        "Get a specialist's report. For questions like what did Susan find about X, pass "
        "agent_id and about (the topic): it returns the report whose objective matches "
        "and lists the agent's other recent assignments. Pass assignment_id only when "
        "you know exactly which assignment is meant; agent_id alone returns the latest.",
        obj(
            {
                "agent_id": string("The specialist.", AGENT_IDS),
                "about": string("The topic the question is about, e.g. experiment ideas."),
                "assignment_id": string("A specific assignment_id, if certain."),
            }
        ),
        agent_assignment_get,
    ),
    Tool(
        "agent_assignments_list",
        "List recent assignments with status and summary, optionally for one agent or status.",
        obj(
            {
                "agent_id": string("Only this agent.", AGENT_IDS),
                "status": string("Only this status.", ["queued", "running", "completed", "failed", "cancelled"]),
                "limit": integer("How many, default 5.", 1, 20),
            }
        ),
        agent_assignments_list,
    ),
    Tool(
        "management_review_get",
        "Get a management review with each specialist's report side by side, by review_id "
        "or the latest review.",
        obj({"review_id": string("review_id; omit for the latest.")}),
        management_review_get,
    ),
]

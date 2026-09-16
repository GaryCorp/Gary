from gary.models.common import validate_request
from gary.services.planning_service import RecordPlanRequest
from gary.tools.base import Tool, ToolContext, obj, present, run_sync, string


async def planning_get_context(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("planning_get_context takes no arguments")
    context = await run_sync(ctx.gary.planning.get_planning_context, "manual")
    ctx.approval_ids().update(a["id"] for a in context["pending_approvals"])
    return {"context": present(context, ctx)}


async def planning_record_plan(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(RecordPlanRequest, args)
    run = await run_sync(ctx.gary.planning.record_plan, request)
    return {"planning_run": present(run, ctx)}


TOOLS = [
    Tool(
        "planning_get_context",
        "Get the full operational picture in one call: active projects, tasks "
        "that are ready (ranked by planning_score), in progress, blocked (and by "
        "what), overdue tasks, upcoming deadlines, due follow-ups, open "
        "commitments, pending approvals, and recent actions. Use when planning, "
        "when the user asks what to work on, or what is going on. Starts a "
        "planning run.",
        obj({}),
        planning_get_context,
    ),
    Tool(
        "planning_record_plan",
        "After planning with planning_get_context, record the plan you agreed "
        "with the user and the action_ids you proposed, so it can be reviewed "
        "later.",
        obj(
            {
                "planning_run_id": string("planning_run_id from planning_get_context."),
                "summary": string("The plan and why, in a few sentences."),
                "action_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "action_ids from action_propose for this plan.",
                },
            },
            ("planning_run_id", "summary"),
        ),
        planning_record_plan,
    ),
]

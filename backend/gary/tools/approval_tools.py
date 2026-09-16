from gary.models.action import ProposeActionRequest
from gary.models.approval import ResolveApprovalRequest
from gary.models.common import validate_request
from gary.policy import USER_ACTOR
from gary.tools.base import Tool, ToolContext, boolean, obj, present, run_sync, string

ACTION_PAYLOADS = (
    "Payload by action_type: "
    "schedule_task {task_id, start, end} puts a task on the calendar; "
    "move_calendar_event {task_id, new_start, new_end} moves a scheduled task's "
    "calendar event; "
    "send_external_email {to, subject, body} sends one email; "
    "create_internal_task and update_internal_task take the same fields as "
    "task_create and task_update. Times are ISO 8601 with timezone offset."
)


async def action_propose(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ProposeActionRequest, args)
    result = await ctx.gary.actions.propose(request)
    if result.get("approval_id"):
        ctx.approval_ids().add(result["approval_id"])
    return present(result, ctx)


async def approval_list_pending(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("approval_list_pending takes no arguments")
    pending = await run_sync(ctx.gary.approvals.list_pending)
    ctx.approval_ids().update(approval["id"] for approval in pending)
    return {"count": len(pending), "approvals": present(pending, ctx)}


async def approval_resolve(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ResolveApprovalRequest, args)
    if request.confirmed is not True:
        raise ValueError(
            "Not confirmed. Read the request to the user, ask whether to approve "
            "or reject it, then call again with confirmed set to true."
        )
    if request.approval_id not in ctx.approval_ids():
        raise ValueError(
            "Unknown approval_id. Call approval_list_pending first and use an "
            "approval_id it returned in this conversation."
        )
    result = await ctx.gary.approvals.resolve(
        request.approval_id,
        request.decision,
        actor=USER_ACTOR,
        channel="voice",
        note=request.note,
    )
    ctx.approval_ids().discard(request.approval_id)
    return present(result, ctx)


TOOLS = [
    Tool(
        "action_propose",
        "Propose an action for the application to carry out. The application, "
        "not you, decides the risk: green actions run immediately, yellow ones "
        "wait for the user's approval, red ones are refused. Use for scheduling "
        "tasks on the calendar, moving scheduled work, and emails Gary initiates. "
        + ACTION_PAYLOADS,
        obj(
            {
                "action_type": string(
                    "The action.",
                    [
                        "schedule_task",
                        "move_calendar_event",
                        "send_external_email",
                        "create_internal_task",
                        "update_internal_task",
                    ],
                ),
                "payload": {
                    "type": "object",
                    "description": "Action fields; see the tool description.",
                },
                "reason": string("Why, in one sentence."),
                "project_id": string("Related project_id, if any."),
                "task_id": string("Related task_id, if any."),
            },
            ("action_type", "payload", "reason"),
        ),
        action_propose,
    ),
    Tool(
        "approval_list_pending",
        "List actions waiting for the user's approval.",
        obj({}),
        approval_list_pending,
    ),
    Tool(
        "approval_resolve",
        "Approve or reject a pending request on the user's behalf. First read "
        "the request summary to the user and ask. Only call after the user "
        "clearly says approve or reject for that specific request. Approving "
        "runs the action immediately.",
        obj(
            {
                "approval_id": string("approval_id from approval_list_pending or action_propose."),
                "decision": string("The user's decision.", ["approved", "rejected"]),
                "confirmed": boolean(
                    "True only if the user explicitly gave this decision for this request."
                ),
                "note": string("Optional note from the user."),
            },
            ("approval_id", "decision", "confirmed"),
        ),
        approval_resolve,
    ),
]

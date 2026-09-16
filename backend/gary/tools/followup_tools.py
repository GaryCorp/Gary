from gary.models.action import ProposeActionRequest
from gary.models.commitment import (
    CreateCommitmentRequest,
    ListCommitmentsRequest,
    ResolveCommitmentRequest,
    UpdateCommitmentRequest,
)
from gary.models.common import validate_request
from gary.models.followup import CompleteFollowupRequest, CreateFollowupRequest
from gary.tools.base import (
    Tool,
    ToolContext,
    obj,
    present,
    priority,
    run_sync,
    string,
    timestamp,
)


async def followup_create(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CreateFollowupRequest, args)
    followup = await run_sync(ctx.gary.followups.create_followup, request)
    return {"followup": present(followup, ctx)}


async def followup_complete(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CompleteFollowupRequest, args)
    followup = await run_sync(ctx.gary.followups.complete_followup, request)
    return {"followup": present(followup, ctx)}


async def commitment_create(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CreateCommitmentRequest, args)
    commitment = await run_sync(ctx.gary.commitments.create_commitment, request)
    return {"commitment": present(commitment, ctx)}


async def followup_list_due(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("followup_list_due takes no arguments")
    followups = await run_sync(ctx.gary.followups.list_due)
    return {"count": len(followups), "followups": present(followups, ctx)}


async def commitment_list(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ListCommitmentsRequest, args)
    commitments = await run_sync(ctx.gary.commitments.list_commitments, request.status)
    return {"count": len(commitments), "commitments": present(commitments, ctx)}


async def commitment_update(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(UpdateCommitmentRequest, args)
    if request.status is not None:
        commitment = await run_sync(
            ctx.gary.commitments.resolve_commitment,
            ResolveCommitmentRequest(commitment_id=request.commitment_id, status=request.status),
        )
        return {"commitment": present(commitment, ctx)}

    # Changing what was promised to someone is an action that needs approval.
    result = await ctx.gary.actions.propose(
        ProposeActionRequest(
            action_type="change_external_commitment",
            payload={"commitment_id": request.commitment_id, **request.changes()},
            reason=request.reason,
        )
    )
    if result.get("approval_id"):
        ctx.approval_ids().add(result["approval_id"])
    return present(result, ctx)


TOOLS = [
    Tool(
        "followup_create",
        "Create a follow-up: something to check on at a set time, e.g. check "
        "whether filming finished at 3 PM. Gary announces it when it is due.",
        obj(
            {
                "title": string("What to check, e.g. Check whether filming is finished."),
                "description": string("Optional details."),
                "due_at": timestamp("When to follow up."),
                "priority": priority(),
                "project_id": string("Related project_id, if any."),
                "task_id": string("Related task_id, if any."),
            },
            ("title", "due_at"),
        ),
        followup_create,
    ),
    Tool(
        "followup_complete",
        "Close a follow-up once it has been dealt with, or cancel it.",
        obj(
            {
                "followup_id": string("followup_id of the follow-up."),
                "status": string("completed (default) or cancelled.", ["completed", "cancelled"]),
            },
            ("followup_id",),
        ),
        followup_complete,
    ),
    Tool(
        "commitment_create",
        "Record a commitment: something the user or Gary promised another person "
        "or system, e.g. send Sam the draft by Thursday. Commitments get extra "
        "weight in planning. Link the task that fulfils it when there is one.",
        obj(
            {
                "description": string("The promise, e.g. Send Sam the draft."),
                "committed_to": string("Who it was promised to."),
                "deadline": timestamp("When it is due."),
                "project_id": string("Related project_id, if any."),
                "task_id": string("task_id of the task that fulfils it, if any."),
            },
            ("description",),
        ),
        commitment_create,
    ),
    Tool(
        "followup_list_due",
        "List follow-ups that are due now, most important first.",
        obj({}),
        followup_list_due,
    ),
    Tool(
        "commitment_list",
        "List commitments: open ones by default, or all recent ones.",
        obj({"status": string("open (default) or all.", ["open", "all"])}),
        commitment_list,
    ),
    Tool(
        "commitment_update",
        "Update a commitment. Pass status fulfilled, missed, or cancelled to record "
        "what happened. To change what was promised (description, who it was "
        "promised to, or deadline), pass those fields instead: that needs the "
        "user's approval and returns an approval request.",
        obj(
            {
                "commitment_id": string("commitment_id of the commitment."),
                "status": string("Outcome.", ["fulfilled", "missed", "cancelled"]),
                "description": string("New description of the promise."),
                "committed_to": string("Who it is promised to.", nullable=True),
                "deadline": timestamp("New deadline, or null.", nullable=True),
                "reason": string("Why the terms change, for the approval request."),
            },
            ("commitment_id",),
        ),
        commitment_update,
    ),
]

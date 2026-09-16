from gary.models.commitment import CreateCommitmentRequest, ResolveCommitmentRequest
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


async def commitment_resolve(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ResolveCommitmentRequest, args)
    commitment = await run_sync(ctx.gary.commitments.resolve_commitment, request)
    return {"commitment": present(commitment, ctx)}


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
        "commitment_resolve",
        "Mark a commitment fulfilled, missed, or cancelled.",
        obj(
            {
                "commitment_id": string("commitment_id of the commitment."),
                "status": string("Outcome.", ["fulfilled", "missed", "cancelled"]),
            },
            ("commitment_id", "status"),
        ),
        commitment_resolve,
    ),
]

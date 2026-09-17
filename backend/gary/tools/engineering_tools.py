"""Gary's engineering-ticket tools.

These are the only GitHub capabilities Gary has: create a ticket, read
tickets, move one through the workflow, comment, synchronize, and read the
privacy audit. There is deliberately no raw GitHub request, no GraphQL
passthrough, no visibility change, no repository administration, and no way to
write repository contents. Alex remains the engineering execution gate.
"""

from gary.integrations.github.models import EngineeringStatus
from gary.models.common import validate_request
from gary.models.engineering import (
    CommentRequest,
    CreateEngineeringTicketRequest,
    ListTicketsRequest,
    TicketLookupRequest,
    TransitionRequest,
)
from gary.tools.base import (
    Tool,
    ToolContext,
    boolean,
    integer,
    obj,
    run_sync,
    string,
    timestamp,
)

STATUS_VALUES = [status.value for status in EngineeringStatus]


def _service(ctx: ToolContext):
    return ctx.integration("engineering")


def _lookup(request) -> dict:
    return {"ticket_id": request.ticket_id, "task_id": request.task_id}


async def _resolve(ctx: ToolContext, request) -> dict:
    return await run_sync(_service(ctx).resolve, **_lookup(request))


def _result(ticket) -> dict:
    """What Gary may say about a ticket, including when it is not fully set up."""
    brief = ticket.brief()
    if ticket.sync_state != "synced":
        brief["warning"] = (
            f"This ticket is {ticket.sync_state}: {ticket.sync_error or 'GitHub setup did not finish'}. "
            "Say so rather than reporting it as fully created."
        )
    if not ticket.assignment_confirmed:
        brief["warning_assignment"] = (
            f"GitHub has not confirmed {ticket.assigned_to} as the assignee; do not say they are assigned."
        )
    return brief


async def engineering_create_ticket(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CreateEngineeringTicketRequest, args)
    ticket = await _service(ctx).create_ticket(request)
    return {
        "ticket": _result(ticket),
        "note": "The issue is in the private GaryCorp repository and Engineering Project. "
                "Schedule the work with the task's calendar tools if it needs time.",
    }


async def engineering_get_ticket(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(TicketLookupRequest, args)
    service = _service(ctx)
    ticket = await run_sync(
        service.get_ticket,
        ticket_id=request.ticket_id,
        task_id=request.task_id,
        issue_number=request.issue_number,
    )
    return {"ticket": _result(ticket)}


async def engineering_list_tickets(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ListTicketsRequest, args)
    if request.status and request.status not in STATUS_VALUES:
        raise ValueError(f"status must be one of {STATUS_VALUES}")
    tickets = await run_sync(
        _service(ctx).list_tickets, request.status, request.open_only, request.limit
    )
    return {"count": len(tickets), "tickets": [t.brief() for t in tickets]}


def _transition_tool(target: EngineeringStatus):
    async def handler(args: dict, ctx: ToolContext) -> dict:
        request = validate_request(TransitionRequest, args)
        service = _service(ctx)
        row = await _resolve(ctx, request)
        ticket = await service.transition(row, target, request.reason)
        return {"ticket": _result(ticket), "status": ticket.status.value}

    return handler


async def engineering_add_comment(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(CommentRequest, args)
    service = _service(ctx)
    row = await _resolve(ctx, request)
    return await service.add_comment(row, request.comment)


async def engineering_sync(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(TicketLookupRequest, args)
    service = _service(ctx)
    if request.ticket_id or request.task_id or request.issue_number:
        row = await run_sync(
            service.resolve, request.ticket_id, request.task_id, request.issue_number
        )
        return {"sync": await service.sync_ticket(row["id"])}
    result = await service.sync_all()
    return {"sync": {k: result[k] for k in ("checked", "synced", "needs_reconciliation", "failed")}}


async def engineering_status(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("engineering_status takes no arguments")
    service = ctx.integrations.get("engineering")
    if service is None:
        return {
            "github_engineering_status": {
                "state": "unavailable",
                "detail": "The GitHub engineering integration is not configured on this deployment.",
            }
        }
    return {"github_engineering_status": await service.status()}


LOOKUP_PROPERTIES = {
    "ticket_id": string("The engineering ticket id."),
    "task_id": string("The Gary task id, if you know that instead."),
}


def _transition_schema(description: str) -> dict:
    return obj({**LOOKUP_PROPERTIES, "reason": string(description)})


TOOLS = [
    Tool(
        "engineering_create_ticket",
        "Create an engineering ticket for Alex from an existing Gary task: it opens a private "
        "GitHub issue with the specification, assigns Alex, labels it, adds it to the private "
        "GaryCorp Engineering Project, and sets Status to Ready. Say what the company needs "
        "and the constraints, not how to implement it. One ticket per task. Do not use it for "
        "trivial work.",
        obj(
            {
                "task_id": string("The Gary task this implements. Create the task first."),
                "title": string("Short engineering title, e.g. 'Add read-only research integration'."),
                "objective": string("The business or technical result needed, and why."),
                "requirements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the change must do. One requirement per item.",
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Checkable conditions that mean it is finished.",
                },
                "priority": string("P0 urgent to P3 low.", ["P0", "P1", "P2", "P3"]),
                "kind": string("feature or bug.", ["feature", "bug"]),
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Anything this depends on, if any.",
                },
                "security_requirements": string(
                    "Security constraints, or omit for standard review.", nullable=True
                ),
                "estimated_minutes": integer("Rough engineering effort in minutes.", 0, 100000),
                "due_at": timestamp("When it is needed by.", nullable=True),
                "security_review_required": boolean(
                    "True when Dave's security review is needed before it can be done."
                ),
            },
            ("task_id", "title", "objective", "requirements", "acceptance_criteria"),
        ),
        engineering_create_ticket,
    ),
    Tool(
        "engineering_get_ticket",
        "Read one engineering ticket: its status, priority, issue number, and whether GitHub "
        "confirmed Alex as assignee.",
        obj({**LOOKUP_PROPERTIES, "issue_number": integer("The GitHub issue number.", 1)}),
        engineering_get_ticket,
    ),
    Tool(
        "engineering_list_tickets",
        "List engineering tickets with their status, newest first. Defaults to unfinished ones.",
        obj(
            {
                "status": string("Only this status.", STATUS_VALUES),
                "open_only": boolean("Only unfinished tickets (default true)."),
                "limit": integer("How many, default 10.", 1, 50),
            }
        ),
        engineering_list_tickets,
    ),
    Tool(
        "engineering_mark_ready",
        "Move a ticket to Ready: the work is specified and Alex can pick it up.",
        _transition_schema("Why it is ready now, if worth recording on the issue."),
        _transition_tool(EngineeringStatus.READY),
    ),
    Tool(
        "engineering_mark_in_progress",
        "Move a ticket to In Progress: Alex has started. Also marks the Gary task in progress.",
        _transition_schema("Optional note for the issue."),
        _transition_tool(EngineeringStatus.IN_PROGRESS),
    ),
    Tool(
        "engineering_mark_review",
        "Move a ticket to Review: the implementation is ready to be reviewed.",
        _transition_schema("Optional note for the issue."),
        _transition_tool(EngineeringStatus.REVIEW),
    ),
    Tool(
        "engineering_mark_security_review",
        "Move a ticket to Security Review. Required before Done when the ticket needs one.",
        _transition_schema("What should be reviewed, if worth recording."),
        _transition_tool(EngineeringStatus.SECURITY_REVIEW),
    ),
    Tool(
        "engineering_mark_done",
        "Move a ticket to Done: closes the private issue and completes the Gary task. Refused "
        "when the ticket requires security review and has not passed it.",
        _transition_schema("What was delivered, if worth recording."),
        _transition_tool(EngineeringStatus.DONE),
    ),
    Tool(
        "engineering_mark_blocked",
        "Mark a ticket Blocked and say why. Also marks the Gary task blocked.",
        obj(
            {**LOOKUP_PROPERTIES, "reason": string("What is blocking it. Recorded on the issue.")},
            ("reason",),
        ),
        _transition_tool(EngineeringStatus.BLOCKED),
    ),
    Tool(
        "engineering_add_comment",
        "Add a comment to a ticket's private GitHub issue. Use for meaningful operational "
        "updates only, such as a priority change, a moved deadline, or a new dependency.",
        obj({**LOOKUP_PROPERTIES, "comment": string("The update, in one or two sentences.")},
            ("comment",)),
        engineering_add_comment,
    ),
    Tool(
        "engineering_sync",
        "Read the current state from GitHub into Gary: Project Status, open or closed, "
        "assignee, and labels. Without arguments it synchronizes every unfinished ticket.",
        obj({**LOOKUP_PROPERTIES, "issue_number": integer("A specific issue number.", 1)}),
        engineering_sync,
    ),
    Tool(
        "engineering_status",
        "Check the GitHub engineering integration: whether it is healthy, and whether the "
        "repository and Project are private. You cannot change visibility; only Alex can.",
        obj({}),
        engineering_status,
    ),
]


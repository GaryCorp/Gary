"""Gary's performance review tools.

He can read anyone's record as numbers, write a review of an employee or of
Alex, read what has been written, and answer a review of himself.

He cannot write a review of himself: that is what the upward reviews are
for, and they are written by the people who work for him. He cannot delete a
review either — there is no tool for it, and there is no service method to
call. A review changes nothing on its own; what is done about one is Alex's
decision.
"""

from gary.models.common import validate_request
from gary.models.review import (
    AcknowledgeReviewRequest,
    ListReviewsRequest,
    ScorecardRequest,
    WriteReviewRequest,
)
from gary.tools.base import Tool, ToolContext, integer, obj, run_sync, string


def _reviews(ctx: ToolContext):
    service = ctx.integration("reviews")
    if service is None:
        raise ValueError("Performance reviews are not configured on this deployment")
    return service


def _brief(review: dict) -> dict:
    return {
        "review_id": review["id"],
        "subject": review["subject"],
        "reviewer": review["reviewer"],
        "kind": review["subject_kind"],
        "summary": review["summary"],
        "strengths": review["strengths"],
        "concerns": review["concerns"],
        "recommendations": review["recommendations"],
        "evidence": review["evidence"],
        "acknowledged": bool(review.get("acknowledged_at")),
        "acknowledgement": review.get("acknowledgement"),
        "written": review["created_at"],
    }


async def performance_scorecard(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ScorecardRequest, args)
    service = _reviews(ctx)
    kind = service.kind_of(request.subject)
    card = await run_sync(service.scorecard, request.subject, kind, request.days)
    return {"subject": request.subject, "kind": kind, "scorecard": card}


async def performance_write_review(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(WriteReviewRequest, args)
    review = await _reviews(ctx).run(request.subject, days=request.days)
    return {"review": _brief(review)}


async def performance_list_reviews(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ListReviewsRequest, args)
    service = _reviews(ctx)
    if request.unacknowledged_of_me:
        reviews = await run_sync(service.unacknowledged_of_manager)
    else:
        reviews = await run_sync(service.list_for, request.subject, request.limit)
    return {"reviews": [_brief(review) for review in reviews]}


async def performance_acknowledge(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(AcknowledgeReviewRequest, args)
    review = await run_sync(
        _reviews(ctx).acknowledge, request.review_id, request.response
    )
    return {"review": _brief(review)}


TOOLS = [
    Tool(
        "performance_scorecard",
        "The record behind someone's performance, as numbers only: assignments, what was "
        "completed or failed, attempts, time to deliver, cost, the confidence they stated "
        "against what they delivered, tool calls the gateway refused, and reports that "
        "failed validation. For Alex it is his own record instead: work completed and "
        "overdue, blocks missed, approvals and questions answered or left, and how much of "
        "what you assigned him he finished that day. Computed from the database, so it "
        "costs nothing and every figure is checkable.",
        obj(
            {
                "subject": string("An agent_id, 'alex', or 'gary'."),
                "days": integer("How far back to look. 28 by default.", 1, 365),
            },
            ("subject",),
        ),
        performance_scorecard,
    ),
    Tool(
        "performance_write_review",
        "Write and store a performance review of an employee, or of Alex. It reads the "
        "scorecard and adds a judgment: what the numbers mean, strengths, concerns, "
        "recommendations, and the figures relied on. Costs one model call. You cannot "
        "review yourself with this; your own reviews are written by the people who work "
        "for you.",
        obj(
            {
                "subject": string("An agent_id, or 'alex'."),
                "days": integer("The period to review. 28 by default.", 1, 365),
            },
            ("subject",),
        ),
        performance_write_review,
    ),
    Tool(
        "performance_list_reviews",
        "Read performance reviews: someone's most recent ones, or, with "
        "unacknowledged_of_me, the reviews of you that you have not answered yet.",
        obj(
            {
                "subject": string("Whose reviews to read. Omit when asking for your own unanswered ones."),
                "unacknowledged_of_me": {
                    "type": "boolean",
                    "description": "True for reviews of you that you owe an answer to.",
                },
                "limit": integer("How many, up to 20.", 1, 20),
            },
        ),
        performance_list_reviews,
    ),
    Tool(
        "performance_acknowledge",
        "Answer a review of you written by someone who works for you. Say what you accept, "
        "what you disagree with, and what you will do differently. It is stored beside "
        "their review, so neither can be dropped later. A review of you is not finished "
        "until you have answered it.",
        obj(
            {
                "review_id": string("The review you are answering."),
                "response": string("Your answer, in a few sentences."),
            },
            ("review_id", "response"),
        ),
        performance_acknowledge,
    ),
]

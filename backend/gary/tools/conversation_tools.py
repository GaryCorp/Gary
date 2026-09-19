"""Gary's side of starting a conversation.

Speaking first is an action, so ``ask_user`` goes through the policy pipeline
like everything else and is capped in conversation_service.py. The other three
tools are about not losing what was said: reading back recent messages,
saying one again when Alex missed it, and recording his answer so Gary stops
waiting.

Specialists have none of these. Only Gary talks to Alex.
"""

from pydantic import Field

from gary.models.action import ProposeActionRequest
from gary.models.common import RequestModel, validate_request
from gary.models.conversation import (
    MESSAGE_MAX,
    MESSAGE_MIN,
    AnswerQuestionRequest,
)
from gary.services.conversation_service import MAX_REPEATS, RECENT_HOURS
from gary.tools.base import Tool, ToolContext, boolean, integer, obj, present, run_sync, string


class _AskUserArgs(RequestModel):
    message: str = Field(min_length=MESSAGE_MIN, max_length=MESSAGE_MAX)
    expects_reply: bool = True
    urgency: str = "now"
    reason: str | None = Field(default=None, max_length=1000)


class _RecentArgs(RequestModel):
    hours: int = Field(default=RECENT_HOURS, ge=1, le=168)


class _RepeatArgs(RequestModel):
    message_id: str = Field(min_length=1, max_length=64)


def _spoken_ids(ctx: ToolContext) -> set[str]:
    """Messages Gary has seen in this conversation, so he can only repeat or
    answer one he actually read, never an id he invented."""
    return ctx.session.setdefault("spoken_ids", set())


async def ask_user(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(_AskUserArgs, args)
    result = await ctx.gary.actions.propose(
        ProposeActionRequest(
            action_type="ask_user",
            payload={
                "message": request.message,
                "expects_reply": request.expects_reply,
                "urgency": request.urgency,
                # Python sets where this came from; the model never chooses it.
                "source": "voice",
            },
            reason=request.reason,
        )
    )
    message_id = (result.get("result") or {}).get("message_id")
    if message_id:
        _spoken_ids(ctx).add(message_id)
    return present(result, ctx)


async def spoken_recent(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(_RecentArgs, args)
    messages = await run_sync(ctx.gary.conversation.recent, request.hours)
    _spoken_ids(ctx).update(message["id"] for message in messages)
    return {
        "count": len(messages),
        "messages": present(messages, ctx),
        "note": (
            "Everything here is also in the Spoken notebook in Joplin, one note "
            "per day, if Alex would rather read it."
        ),
    }


async def spoken_repeat(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(_RepeatArgs, args)
    if request.message_id not in _spoken_ids(ctx):
        raise ValueError(
            "Unknown message_id. Call spoken_recent first and use an id it "
            "returned in this conversation."
        )
    speech = ctx.integrations.get("speech")
    if speech is None:
        # No delivery wired in: the record still has to be right.
        message = await run_sync(ctx.gary.conversation.repeat, request.message_id)
    else:
        # Gary reads it back himself in this conversation, so it is recorded
        # and written to the notebook but not broadcast a second time.
        message = await speech.repeat(request.message_id, speak_aloud=False)
    return {"message": present(message, ctx), "say": message["text"]}


async def question_answer(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(AnswerQuestionRequest, args)
    if request.message_id not in _spoken_ids(ctx):
        raise ValueError(
            "Unknown message_id. Call spoken_recent first and use an id it "
            "returned in this conversation."
        )
    message = await run_sync(
        ctx.gary.conversation.answer, request.message_id, request.answer
    )
    _spoken_ids(ctx).discard(request.message_id)
    return present(message, ctx)



TOOLS = [
    Tool(
        "ask_user",
        "Say something to Alex when he is not in a conversation with you, and "
        "optionally wait for his answer. Use it when something genuinely needs "
        "him: a decision only he can make, a commitment about to be missed, an "
        "approval about to expire. Do not use it for status updates or anything "
        "that can wait for the next briefing. It is spoken aloud and written to "
        "the Spoken notebook in Joplin. You are already talking to him during a "
        "conversation, so just say it instead.",
        obj(
            {
                "message": string("Exactly what to say, in plain spoken words."),
                "expects_reply": boolean(
                    "True to ask a question you will wait on: he answers when he "
                    "next says the wake word, so say in the message that he should. "
                    "False to tell him something that needs no reply."
                ),
                "urgency": string(
                    "now speaks it aloud at the next opportunity and interrupts "
                    "whatever he is doing. next_time never interrupts: it is held "
                    "and put to him the next time he talks to you. Prefer "
                    "next_time unless it genuinely cannot wait, and note that a "
                    "held message still expires unanswered after 72 hours.",
                    ["now", "next_time"],
                ),
                "reason": string("Why this needs him, in one sentence."),
            },
            ("message",),
        ),
        ask_user,
    ),
    Tool(
        "spoken_recent",
        "What you have said to Alex recently, with the time you said it and "
        "whether you are still waiting on an answer. Use it when he asks what "
        "you said, what he missed, or what you have been telling him.",
        obj({"hours": integer("How far back to look, in hours.", 1, 168)}),
        spoken_recent,
    ),
    Tool(
        "spoken_repeat",
        "Say something again that Alex did not hear. Call spoken_recent first, "
        "then repeat the one he means and read its text back to him. At most "
        f"{MAX_REPEATS} repeats each; after that point him at the Spoken "
        "notebook in Joplin.",
        obj(
            {"message_id": string("message_id from spoken_recent.")},
            ("message_id",),
        ),
        spoken_repeat,
    ),
    Tool(
        "question_answer",
        "Record Alex's answer to a question you asked him, so you stop waiting "
        "on it. Only call it once he has actually answered that question.",
        obj(
            {
                "message_id": string("message_id of the question, from spoken_recent."),
                "answer": string("What Alex said, in his words."),
            },
            ("message_id", "answer"),
        ),
        question_answer,
    ),
]

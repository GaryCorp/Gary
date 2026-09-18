"""Gary starting the conversation.

The point of this feature is that nothing he decides to say can go missing:
not when the voice service is down, not in quiet hours, not when Alex was out
of the room. So the refusals and the retries matter more than the happy path.
"""

import datetime as dt

import pytest

from gary.models.action import ProposeActionRequest
from gary.models.conversation import AskUserPayload
from gary.services.conversation_service import (
    MAX_DAILY_UNATTENDED,
    MAX_OPEN_QUESTIONS,
    MAX_REPEATS,
    MESSAGE_EXPIRY_HOURS,
    SpokenDelivery,
)
from gary.tools import ToolContext, call_tool

from conftest import audit_events, run


class FakeVoice:
    """The voice service, which may or may not be listening."""

    def __init__(self, listening: bool = True):
        self.listening = listening
        self.said: list[tuple[str, bool]] = []

    async def __call__(self, text: str, expects_reply: bool) -> bool:
        if not self.listening:
            return False
        self.said.append((text, expects_reply))
        return True


class FakeNotebook:
    """Joplin, which may be down."""

    def __init__(self):
        self.lines: list[tuple[str, bool]] = []
        self.fail_next = False

    async def append(self, message: dict, again: bool = False) -> str | None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Joplin is unreachable")
        self.lines.append((message["text"], again))
        return "note-1"


@pytest.fixture
def voice():
    return FakeVoice()


@pytest.fixture
def notebook():
    return FakeNotebook()


@pytest.fixture
def delivery(gary, voice, notebook):
    return SpokenDelivery(gary.conversation, voice, notebook)


def ask(gary, message: str, **overrides) -> dict:
    payload = {"message": message, **overrides}
    return run(
        gary.actions.propose(
            ProposeActionRequest(action_type="ask_user", payload=payload, reason="Alex needs to decide")
        )
    )


def open_messages(gary) -> list[dict]:
    with gary.db.read() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM spoken_messages ORDER BY created_at")]


# ------------------------------------------------------------ the action

def test_ask_user_is_green_and_recorded_before_it_is_said(gary):
    result = ask(gary, "Susan finished the pricing research. Do you want me to book time to read it?")

    assert result["status"] == "succeeded"
    assert result["risk_level"] == "green"

    messages = open_messages(gary)
    assert len(messages) == 1
    assert messages[0]["status"] == "pending", "recorded before anything is spoken"
    assert messages[0]["kind"] == "question"
    assert messages[0]["expects_reply"] == 1
    assert messages[0]["spoken_at"] is None
    assert audit_events(gary, "action", result["action_id"]) == [
        "action_proposed",
        "action_succeeded",
        "spoken_message_queued",
    ]


def test_a_notice_wants_no_answer(gary):
    ask(gary, "I have moved the demo block to Thursday morning.", expects_reply=False)

    message = open_messages(gary)[0]
    assert message["kind"] == "notice"
    assert message["expects_reply"] == 0


def test_markdown_never_reaches_the_speaker(gary):
    ask(gary, "**Susan** finished the _pricing_ research and it is [here](x).")

    assert "*" not in open_messages(gary)[0]["text"]
    assert "_" not in open_messages(gary)[0]["text"]


# ----------------------------------------------------------- the refusals

DISTINCT_QUESTIONS = [
    "Should I move tomorrow's filming block into the morning?",
    "Do you want Catherine looking at the Anthropic subscription?",
    "Is the conference keynote still happening in November?",
    "Shall I close the stalled newsletter project?",
    "Who is meant to be reviewing the hardware purchase order?",
    "Would you like Lauren consulted before the layoff announcement?",
    "Has the landlord replied about the lease renewal yet?",
]


def test_gary_may_not_queue_more_than_three_questions(gary):
    for question in DISTINCT_QUESTIONS[:MAX_OPEN_QUESTIONS]:
        assert ask(gary, question)["status"] == "succeeded"

    with pytest.raises(ValueError, match="already waiting"):
        ask(gary, DISTINCT_QUESTIONS[MAX_OPEN_QUESTIONS])


def test_a_notice_is_not_blocked_by_open_questions(gary):
    for question in DISTINCT_QUESTIONS[:MAX_OPEN_QUESTIONS]:
        ask(gary, question)

    # Telling him something costs him nothing to resolve.
    assert ask(gary, "The engineering sync finished without errors.", expects_reply=False)["status"] == "succeeded"


def test_the_same_question_is_not_asked_twice_while_one_is_open(gary):
    ask(gary, "Should I ask Catherine to review the Anthropic subscription cost?")

    with pytest.raises(ValueError, match="already asked"):
        ask(gary, "Should Catherine review the Anthropic subscription cost again?")


def test_the_same_question_may_be_asked_once_the_first_is_answered(gary, delivery, voice):
    ask(gary, "Should I ask Catherine to review the Anthropic subscription cost?")
    run(delivery.deliver_pending())
    gary.conversation.answer(open_messages(gary)[0]["id"], "Not this week")

    assert ask(gary, "Should Catherine review the Anthropic subscription cost now?")["status"] == "succeeded"


def test_the_unattended_loops_have_a_daily_ceiling(gary, delivery):
    # Answer each one so the open-question cap is not what stops it.
    for question in DISTINCT_QUESTIONS[:MAX_DAILY_UNATTENDED]:
        ask(gary, question, source="planning_cycle")
        run(delivery.deliver_pending())
        for message in open_messages(gary):
            if message["status"] == "spoken":
                gary.conversation.answer(message["id"], "noted")

    with pytest.raises(ValueError, match="already raised"):
        ask(gary, DISTINCT_QUESTIONS[MAX_DAILY_UNATTENDED], source="planning_cycle")


def test_a_conversation_is_not_limited_by_the_unattended_ceiling(gary, delivery):
    for question in DISTINCT_QUESTIONS[:MAX_DAILY_UNATTENDED]:
        ask(gary, question, source="planning_cycle")
        run(delivery.deliver_pending())
        for message in open_messages(gary):
            if message["status"] == "spoken":
                gary.conversation.answer(message["id"], "noted")

    assert ask(gary, DISTINCT_QUESTIONS[MAX_DAILY_UNATTENDED])["status"] == "succeeded"


# ------------------------------------------------------------- delivering

def test_nothing_is_marked_spoken_that_nobody_heard(gary, notebook):
    silent = FakeVoice(listening=False)
    delivery = SpokenDelivery(gary.conversation, silent, notebook)
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")

    assert run(delivery.deliver_pending()) == 0
    assert open_messages(gary)[0]["status"] == "pending"
    assert notebook.lines == []

    # The voice service comes back.
    silent.listening = True
    assert run(delivery.deliver_pending()) == 1
    assert open_messages(gary)[0]["status"] == "spoken"
    assert silent.said == [("Do you want me to move tomorrow's demo block to the morning?", True)]


def test_a_question_opens_the_microphone_and_a_notice_does_not(gary, delivery, voice):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    ask(gary, "The engineering sync finished without errors.", expects_reply=False)
    run(delivery.deliver_pending())

    assert [expects_reply for _, expects_reply in voice.said] == [True, False]


def test_speaking_is_audited_once(gary, delivery):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())
    message_id = open_messages(gary)[0]["id"]

    assert audit_events(gary, "spoken_message", message_id) == ["spoke_to_user"]

    # A second pass finds nothing left to say.
    assert run(delivery.deliver_pending()) == 0
    assert audit_events(gary, "spoken_message", message_id) == ["spoke_to_user"]


def test_an_unanswered_question_expires_like_an_approval(gary, delivery, clock):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())

    clock.advance(hours=MESSAGE_EXPIRY_HOURS + 1)
    gary.conversation.pending()

    message = open_messages(gary)[0]
    assert message["status"] == "expired"
    assert audit_events(gary, "spoken_message", message["id"]) == [
        "spoke_to_user",
        "spoken_message_expired",
    ]


# ---------------------------------------------------------- the Joplin note

def test_everything_said_is_written_down(gary, delivery, notebook):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    ask(gary, "The engineering sync finished without errors.", expects_reply=False)
    run(delivery.deliver_pending())

    assert [text for text, _ in notebook.lines] == [
        "Do you want me to move tomorrow's demo block to the morning?",
        "The engineering sync finished without errors.",
    ]
    assert all(message["joplin_written_at"] for message in open_messages(gary))


def test_a_joplin_outage_delays_the_note_and_never_loses_it(gary, delivery, notebook):
    notebook.fail_next = True
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())

    message = open_messages(gary)[0]
    assert message["status"] == "spoken", "it was said, whatever Joplin did"
    assert message["joplin_written_at"] is None
    assert notebook.lines == []

    assert run(delivery.retry_mirror()) == 1
    assert len(notebook.lines) == 1
    assert open_messages(gary)[0]["joplin_written_at"] is not None

    # And it is not written a second time.
    assert run(delivery.retry_mirror()) == 0
    assert len(notebook.lines) == 1


# ------------------------------------------------------------- repeating it

def test_alex_can_have_it_said_again_up_to_a_limit(gary, delivery, voice, notebook):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())
    message_id = open_messages(gary)[0]["id"]

    for _ in range(MAX_REPEATS):
        run(delivery.repeat(message_id))

    assert open_messages(gary)[0]["repeat_count"] == MAX_REPEATS
    assert len(voice.said) == 1 + MAX_REPEATS
    assert [again for _, again in notebook.lines] == [False] + [True] * MAX_REPEATS

    with pytest.raises(ValueError, match="Spoken notebook"):
        run(delivery.repeat(message_id))


def test_something_never_said_cannot_be_repeated(gary, delivery):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")

    with pytest.raises(ValueError, match="has not said"):
        run(delivery.repeat(open_messages(gary)[0]["id"]))


# --------------------------------------------------------------- answering

def test_answering_closes_the_question(gary, delivery):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())
    message_id = open_messages(gary)[0]["id"]

    answered = gary.conversation.answer(message_id, "Yes, move it to nine")
    assert answered["status"] == "answered"
    assert answered["answer"] == "Yes, move it to nine"
    assert gary.conversation.awaiting_answer() == []

    with pytest.raises(ValueError, match="already answered"):
        gary.conversation.answer(message_id, "Yes again")


def test_a_question_not_yet_asked_cannot_be_answered(gary):
    ask(gary, "Do you want me to move tomorrow's demo block to the morning?")

    with pytest.raises(ValueError, match="has not asked"):
        gary.conversation.answer(open_messages(gary)[0]["id"], "Sure")


# ------------------------------------------------------- approvals speak up

def test_a_yellow_action_is_put_to_alex_out_loud(gary, delivery):
    raised = []

    async def raise_it(approval):
        raised.append(approval)
        await delivery.speak(
            f"I need your approval. {approval['summary']}.",
            kind="question",
            source="approval",
            expects_reply=True,
            approval_id=approval["approval_id"],
        )

    gary.actions.on_approval_requested = raise_it
    result = run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="send_external_email",
                payload={"to": "sam@example.com", "subject": "Draft", "body": "Hi"},
                reason="Commitment due today",
            )
        )
    )

    assert result["status"] == "awaiting_approval"
    assert raised and raised[0]["approval_id"] == result["approval_id"]

    message = open_messages(gary)[0]
    assert message["approval_id"] == result["approval_id"]
    assert message["status"] == "spoken"


def test_one_approval_is_raised_once(gary, delivery):
    first = gary.conversation.announce("Approve the thing", approval_id="a-1")
    second = gary.conversation.announce("Approve the thing", approval_id="a-1")

    assert first["id"] == second["id"]
    assert len(open_messages(gary)) == 1


def test_a_failure_to_speak_never_loses_the_approval(gary):
    async def explode(approval):
        raise RuntimeError("the voice service is on fire")

    gary.actions.on_approval_requested = explode
    result = run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="send_external_email",
                payload={"to": "sam@example.com", "subject": "Draft", "body": "Hi"},
                reason="Commitment due today",
            )
        )
    )

    assert result["status"] == "awaiting_approval"
    assert gary.approvals.list_pending()[0]["id"] == result["approval_id"]


# ------------------------------------------------------------- the tools

def call(ctx, name, **arguments):
    return run(call_tool(name, arguments, ctx))


def test_gary_reads_back_what_he_said_and_repeats_it(gary, delivery):
    ctx = ToolContext(gary, {}, {"speech": delivery})
    call(ctx, "ask_user", message="Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())

    recent = call(ctx, "spoken_recent")
    assert recent["success"] is True
    assert recent["count"] == 1
    said = recent["messages"][0]
    assert "demo block" in said["text"]
    # Local time, like everything else Gary reads out.
    assert said["spoken_at"].endswith("-05:00")

    again = call(ctx, "spoken_repeat", message_id=said["id"])
    assert again["success"] is True
    assert again["say"] == said["text"]


def test_gary_cannot_repeat_or_answer_an_id_he_never_saw(gary, delivery):
    ctx = ToolContext(gary, {}, {"speech": delivery})
    unknown = "7d0f4d1c-7e2b-4c55-9d1c-0b1e7f0e9a11"

    assert call(ctx, "spoken_repeat", message_id=unknown)["error"].startswith("Unknown message_id")
    assert call(ctx, "question_answer", message_id=unknown, answer="yes")["error"].startswith(
        "Unknown message_id"
    )


def test_answering_through_the_tool_closes_it(gary, delivery):
    ctx = ToolContext(gary, {}, {"speech": delivery})
    call(ctx, "ask_user", message="Do you want me to move tomorrow's demo block to the morning?")
    run(delivery.deliver_pending())
    message_id = call(ctx, "spoken_recent")["messages"][0]["id"]

    answered = call(ctx, "question_answer", message_id=message_id, answer="Yes, nine o'clock")
    assert answered["success"] is True
    assert answered["status"] == "answered"
    assert gary.conversation.awaiting_answer() == []


def test_the_message_must_be_worth_saying(gary):
    ctx = ToolContext(gary, {}, {})
    assert call(ctx, "ask_user", message="ok")["success"] is False


def test_specialists_cannot_talk_to_alex():
    """Only Gary speaks to Alex. A report is advice for Gary, not a channel
    to the principal, and no roster edit can make it one."""
    from gary.agents.gateway import FORBIDDEN_TOOLS
    from gary.agents.roster import CATHERINE, DAVE, LAUREN, LINDA, SUSAN

    speaking_tools = {"ask_user", "spoken_recent", "spoken_repeat", "question_answer"}
    assert speaking_tools <= FORBIDDEN_TOOLS

    for definition in (SUSAN, DAVE, LINDA, CATHERINE, LAUREN):
        assert not speaking_tools & set(definition.allowed_tools), definition.agent_id


def test_settling_an_approval_stops_gary_waiting_on_it(gary, delivery):
    """However it is actually settled -- by voice or on the page -- Gary has
    his answer and must stop raising it."""
    async def raise_it(approval):
        await delivery.speak(
            f"I need your approval. {approval['summary']}.",
            kind="question",
            source="approval",
            expects_reply=True,
            approval_id=approval["approval_id"],
        )

    gary.actions.on_approval_requested = raise_it
    result = run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="send_external_email",
                payload={"to": "sam@example.com", "subject": "Draft", "body": "Hi"},
                reason="Commitment due today",
            )
        )
    )
    assert len(gary.conversation.awaiting_answer()) == 1

    run(gary.approvals.resolve(result["approval_id"], "rejected", channel="web"))

    assert gary.conversation.awaiting_answer() == []
    assert open_messages(gary)[0]["answer"] == "rejected on the web"

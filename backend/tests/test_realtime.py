"""Realtime turn-taking: one active response at a time.

Server VAD starts responses on its own, so a follow-up requested after a slow
tool call (a GitHub ticket, a web search) can collide with the user's next
turn. The gate queues the follow-up instead of losing it.
"""

from app.realtime import ACTIVE_RESPONSE_ERROR, ResponseGate, needs_follow_up

TOOL_RESPONSE = {"output": [{"type": "function_call", "name": "engineering_create_ticket"}]}
SPOKEN_RESPONSE = {"output": [{"type": "message", "role": "assistant"}]}


def gate_after(*events) -> ResponseGate:
    gate = ResponseGate()
    for event in events:
        gate.observe(event)
    return gate


def test_follow_up_is_sent_immediately_when_nothing_is_active():
    gate = gate_after({"type": "response.created"}, {"type": "response.done"})
    assert needs_follow_up(TOOL_RESPONSE) is True
    assert gate.request() is True  # send response.create now
    assert gate.pending is False


def test_follow_up_is_queued_while_another_response_is_active():
    # The user spoke during the tool call, so VAD started its own response.
    gate = gate_after({"type": "response.created"})
    assert gate.request() is False  # not sent: it would be rejected
    assert gate.pending is True

    gate.observe({"type": "response.done"})
    assert gate.take_pending() is True  # now it is owed
    assert gate.request() is True
    assert gate.pending is False


def test_rejected_follow_up_is_retried_after_the_active_response():
    """The exact failure: response.create rejected while VAD's response ran."""
    gate = gate_after(
        {"type": "response.created"},
        {
            "type": "error",
            "error": {"code": ACTIVE_RESPONSE_ERROR, "message": "Conversation already has..."},
        },
    )
    assert gate.pending is True
    assert gate.take_pending() is False  # not while the other response runs

    gate.observe({"type": "response.done"})
    assert gate.take_pending() is True
    assert gate.take_pending() is False  # only once


def test_unrelated_errors_do_not_queue_a_response():
    gate = gate_after({"type": "error", "error": {"code": "rate_limit_exceeded"}})
    assert gate.pending is False
    assert gate.take_pending() is False


def test_only_tool_calls_need_a_follow_up():
    assert needs_follow_up(SPOKEN_RESPONSE) is False
    assert needs_follow_up({}) is False
    assert needs_follow_up({"output": [{"type": "message"}, {"type": "function_call"}]}) is True


def test_sequence_of_tool_calls_asks_once_per_response():
    gate = ResponseGate()
    gate.observe({"type": "response.created"})
    gate.observe({"type": "response.done"})  # the response that made the calls
    assert gate.request() is True
    gate.observe({"type": "response.created"})  # our follow-up started
    assert gate.request() is False  # a second ask is queued, not sent twice

"""The Realtime connection, driven against a fake OpenAI socket.

The turn-taking rules are tested in test_realtime.py; this checks that the
connection wires them up: the session opens with Gary's instructions and what
is outstanding, tool calls are answered on the socket, usage is recorded, and
the spend ceiling stops a connection before it is made.
"""

import json

import pytest

from app import realtime_session
from app.realtime_session import RealtimeSession
from conftest import run
from gary.finance.usage import SpendCeilingReached


class FakeOpenAI:
    """An upstream Realtime socket: replays events, records what was sent."""

    def __init__(self, events):
        self.events = [json.dumps(event) for event in events]
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeVoiceClient:
    def __init__(self):
        self.received: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.received.append(json.loads(raw))


TOOL_EVENTS = [
    {"type": "response.created"},
    {
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
        "name": "list_calendar_events",
        "arguments": '{"start_time": "a", "end_time": "b"}',
    },
    {
        "type": "response.done",
        "response": {"id": "r1", "output": [{"type": "function_call"}], "usage": {}},
    },
]


def make_session(monkeypatch, upstream, *, stopped=None, opening="Earlier I asked X."):
    async def fake_connect(url, **kwargs):
        upstream.url = url
        upstream.headers = kwargs["additional_headers"]
        return upstream

    monkeypatch.setattr(realtime_session.websockets, "connect", fake_connect)
    calls = {"dispatched": [], "usage": [], "spend_checks": []}

    async def dispatch(name, arguments_json, session):
        calls["dispatched"].append((name, json.loads(arguments_json), session))
        return {"events": []}

    async def spend_stop(what):
        calls["spend_checks"].append(what)
        return stopped

    async def opening_context(session):
        return opening

    client = FakeVoiceClient()
    session = {"event_ids": set()}
    realtime = RealtimeSession(
        client,
        session,
        instructions=lambda: "You are Gary.",
        tools=[{"type": "function", "name": "list_calendar_events"}],
        dispatch=dispatch,
        spend_stop=spend_stop,
        on_usage=calls["usage"].append,
        opening_context=opening_context,
    )
    return realtime, client, session, calls


def test_session_opens_with_instructions_then_what_is_outstanding(monkeypatch):
    upstream = FakeOpenAI([])
    realtime, _, _, calls = make_session(monkeypatch, upstream)

    async def scenario():
        await realtime.connect()
        await realtime.close()

    run(scenario())

    assert calls["spend_checks"] == ["voice"]
    assert upstream.url.endswith(f"?model={realtime_session.OPENAI_REALTIME_MODEL}")
    assert upstream.headers["Authorization"].startswith("Bearer ")
    update, opening = upstream.sent
    assert update["type"] == "session.update"
    assert update["session"]["instructions"] == "You are Gary."
    assert update["session"]["output_modalities"] == ["text"]
    assert opening["item"]["role"] == "assistant"
    assert opening["item"]["content"][0]["text"] == "Earlier I asked X."
    assert upstream.closed


def test_nothing_outstanding_sends_only_the_session_update(monkeypatch):
    upstream = FakeOpenAI([])
    realtime, _, _, _ = make_session(monkeypatch, upstream, opening=None)

    async def scenario():
        await realtime.connect()
        await realtime.close()

    run(scenario())
    assert [message["type"] for message in upstream.sent] == ["session.update"]


def test_tool_call_is_answered_recorded_and_followed_up(monkeypatch):
    upstream = FakeOpenAI(TOOL_EVENTS)
    realtime, client, session, calls = make_session(monkeypatch, upstream, opening=None)

    async def scenario():
        connection = await realtime.connect()
        await realtime.reader  # replays every event, then the socket ends
        return connection

    run(scenario())

    ((name, arguments, dispatched_session),) = calls["dispatched"]
    assert name == "list_calendar_events"
    assert arguments == {"start_time": "a", "end_time": "b"}
    assert dispatched_session is session
    output = upstream.sent[1]
    assert output["item"]["type"] == "function_call_output"
    assert output["item"]["call_id"] == "call-1"
    assert json.loads(output["item"]["output"]) == {"events": []}
    # The response that made the tool call is done, so one follow-up is owed.
    assert upstream.sent[2] == {"type": "response.create"}
    assert [usage["id"] for usage in calls["usage"]] == ["r1"]
    # Every upstream event reaches the voice client, in order.
    assert [event["type"] for event in client.received] == [e["type"] for e in TOOL_EVENTS]
    # The socket ended on its own, so the next audio reconnects.
    assert realtime.connection is None and realtime.dropped


def test_spend_ceiling_stops_the_connection_before_it_is_made(monkeypatch):
    upstream = FakeOpenAI([])
    realtime, _, _, _ = make_session(
        monkeypatch, upstream, stopped={"reason": "Daily ceiling reached."}
    )

    with pytest.raises(SpendCeilingReached, match="Daily ceiling reached."):
        run(realtime.connect())
    assert upstream.sent == []
    assert not hasattr(upstream, "url")

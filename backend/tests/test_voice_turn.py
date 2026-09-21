"""A spoken turn without the Realtime API.

Transcribe the utterance, send the words to a text model, run whatever tools
it asks for, speak the answer. The interesting cases are the failures: a turn
that cannot be completed must say so rather than invent a reply, and a model
that keeps calling tools must be stopped.
"""

import json
from zoneinfo import ZoneInfo

import pytest

from app.voice_turn import MAX_TOOL_ROUNDS, VoiceTurn, VoiceTurnError, reply_text, tool_calls
from gary.finance.pricing import PriceTable
from gary.finance.usage import UsageLedger

from conftest import run

CHICAGO = ZoneInfo("America/Chicago")


def message(text: str) -> dict:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ]
    }


class FakeOpenAI:
    """Queued Responses payloads, and every request it was sent."""

    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.transcripts: list[bytes] = []
        self.transcript_text = "what is on my calendar tomorrow"
        self.fail_transcription = False

    def post(self, url, body, api_key, timeout):
        self.requests.append(body)
        if not self.replies:
            raise AssertionError("the model was called more times than the test expected")
        return self.replies.pop(0)

    def upload(self, audio: bytes) -> dict:
        if self.fail_transcription:
            raise VoiceTurnError("Transcription failed with HTTP 500")
        self.transcripts.append(audio)
        return {"text": self.transcript_text, "model": "gpt-transcribe"}


def build(openai: FakeOpenAI, dispatch=None, ledger=None, tools=None) -> VoiceTurn:
    async def nothing(name, arguments_json, session):
        return {"success": True}

    return VoiceTurn(
        "sk-test-not-used",
        "gpt-5.6-luna",
        "gpt-transcribe",
        instructions=lambda: "You are Gary.",
        tools=tools if tools is not None else [{"type": "function", "name": "task_list"}],
        dispatch=dispatch or nothing,
        usage=ledger,
        post=openai.post,
        upload=openai.upload,
    )


def ledger_for(gary, clock, prices_file, models: dict) -> UsageLedger:
    # Keyed by the exact model id, dots and all, because that is what the
    # lookup uses.
    table = PriceTable(path=prices_file, environ={})
    for model, rates in models.items():
        table.set_price(model, **rates)
    return UsageLedger(gary.db, table, CHICAGO, clock)


@pytest.fixture
def prices_file(tmp_path):
    return tmp_path / "model_prices.json"


# ------------------------------------------------------------- parsing

def test_reply_text_reads_the_words_to_speak():
    assert reply_text(message("Two blocks this afternoon.")) == "Two blocks this afternoon."
    assert reply_text(call("task_list", {})) == "", "a tool call is not an answer"


def test_tool_calls_are_found():
    assert [c["name"] for c in tool_calls(call("task_list", {}))] == ["task_list"]
    assert tool_calls(message("hello")) == []


# --------------------------------------------------------------- a turn

def test_words_in_answer_out():
    openai = FakeOpenAI(message("You have two blocks tomorrow."))
    turn = build(openai)
    session = {}

    said = run(turn.respond(session, "what is on my calendar tomorrow"))

    assert said == "You have two blocks tomorrow."
    sent = openai.requests[0]
    assert sent["model"] == "gpt-5.6-luna"
    assert sent["instructions"] == "You are Gary."
    assert sent["store"] is False, "voice conversations are not kept by OpenAI"
    assert sent["input"][-1] == {"role": "user", "content": "what is on my calendar tomorrow"}


def test_a_tool_call_runs_and_its_result_goes_back():
    openai = FakeOpenAI(call("task_list", {"status": "open"}), message("Three open tasks."))
    seen = []

    async def dispatch(name, arguments_json, session):
        seen.append((name, json.loads(arguments_json)))
        return {"success": True, "count": 3}

    said = run(build(openai, dispatch).respond({}, "what is open"))

    assert said == "Three open tasks."
    assert seen == [("task_list", {"status": "open"})]
    # The second call carries the call and its output, in that order.
    second = openai.requests[1]["input"]
    assert second[-2]["type"] == "function_call"
    assert second[-1]["type"] == "function_call_output"
    assert json.loads(second[-1]["output"]) == {"success": True, "count": 3}


def test_several_tool_calls_in_one_turn_all_run():
    both = {
        "output": [
            {"type": "function_call", "call_id": "a", "name": "task_list", "arguments": "{}"},
            {"type": "function_call", "call_id": "b", "name": "project_list", "arguments": "{}"},
        ]
    }
    openai = FakeOpenAI(both, message("Done."))
    seen = []

    async def dispatch(name, arguments_json, session):
        seen.append(name)
        return {"success": True}

    assert run(build(openai, dispatch).respond({}, "status")) == "Done."
    assert seen == ["task_list", "project_list"]


def test_a_model_that_never_answers_is_stopped():
    """A tool loop that never produces words would otherwise cost money
    until the session ended."""
    openai = FakeOpenAI(*[call("task_list", {}) for _ in range(MAX_TOOL_ROUNDS)])

    with pytest.raises(VoiceTurnError, match=f"{MAX_TOOL_ROUNDS} rounds"):
        run(build(openai).respond({}, "loop please"))


def test_a_failed_turn_never_invents_a_reply():
    openai = FakeOpenAI()
    openai.fail_transcription = True

    with pytest.raises(VoiceTurnError, match="Transcription failed"):
        run(build(openai).transcribe(b"RIFF....", 3.0))


# ------------------------------------------------------------- history

def test_the_second_turn_remembers_the_first():
    openai = FakeOpenAI(message("Two blocks."), message("The first is at nine."))
    turn = build(openai)
    session = {}

    run(turn.respond(session, "what is on tomorrow"))
    run(turn.respond(session, "when is the first"))

    second = openai.requests[1]["input"]
    assert {"role": "user", "content": "what is on tomorrow"} in second
    assert {"role": "assistant", "content": "Two blocks."} in second
    assert second[-1] == {"role": "user", "content": "when is the first"}


def test_history_lives_in_the_session_so_sleeping_forgets_it():
    openai = FakeOpenAI(message("Two blocks."), message("Sorry, what?"))
    turn = build(openai)
    session = {}

    run(turn.respond(session, "what is on tomorrow"))
    session["messages"] = []  # what bridge.reset does
    run(turn.respond(session, "when is the first"))

    assert openai.requests[1]["input"] == [
        {"role": "user", "content": "when is the first"}
    ]


# ---------------------------------------------------------------- money

def test_the_audio_and_the_thinking_are_costed_separately(gary, clock, prices_file):
    book = ledger_for(
        gary,
        clock,
        prices_file,
        {
            "gpt-transcribe": {"per_minute": 0.6},
            "gpt-5.6-luna": {"input": 0.2, "output": 1.2},
        },
    )
    openai = FakeOpenAI(
        {**message("Two blocks."), "usage": {"input_tokens": 1000, "output_tokens": 100}}
    )
    turn = build(openai, ledger=book)
    session = {}

    run(turn.transcribe(b"RIFF....", 120.0))
    run(turn.respond(session, "what is on tomorrow"))

    with gary.db.read() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM model_usage ORDER BY created_at")]

    audio, thinking = rows
    assert audio["source"] == "voice_transcription"
    assert audio["audio_seconds"] == 120
    assert audio["cost_usd"] == pytest.approx(1.2), "two minutes at sixty cents"
    assert thinking["source"] == "voice"
    assert thinking["total_tokens"] == 1100
    assert thinking["cost_usd"] == pytest.approx(0.2 / 1000 + 1.2 / 10_000)


# ------------------------------------------- what the provider billed

def test_a_duration_billed_model_is_costed_on_its_reported_seconds():
    """The endpoint answers {"type": "duration", "seconds": n}. That figure
    is what is charged, so it wins over the voice service's own timing."""
    usage = VoiceTurn.transcription_usage({"type": "duration", "seconds": 7}, 99.0)

    assert usage.audio_seconds == 7
    assert usage.total_tokens == 0


def test_a_token_billed_model_is_costed_on_tokens():
    reported = {
        "type": "tokens",
        "input_tokens": 10,
        "output_tokens": 6,
        "total_tokens": 16,
    }

    usage = VoiceTurn.transcription_usage(reported, 99.0)

    assert usage.input_tokens == 10
    assert usage.output_tokens == 6
    assert usage.audio_seconds == 0, "tokens and duration are not both charged"


def test_a_model_that_reports_nothing_falls_back_to_measured_time():
    usage = VoiceTurn.transcription_usage({}, 12.345)

    assert usage.audio_seconds == pytest.approx(12.35)


def test_the_reported_duration_is_what_reaches_the_ledger(gary, clock, prices_file):
    book = ledger_for(gary, clock, prices_file, {"gpt-transcribe": {"per_minute": 0.6}})
    openai = FakeOpenAI()
    openai.upload = lambda audio: {
        "text": "hello",
        "model": "gpt-transcribe",
        "usage": {"type": "duration", "seconds": 30},
    }
    turn = build(openai, ledger=book)
    turn._upload = openai.upload

    run(turn.transcribe(b"RIFF....", 99.0))

    with gary.db.read() as conn:
        row = dict(conn.execute("SELECT * FROM model_usage").fetchone())
    assert row["audio_seconds"] == 30, "not the 99 seconds the client guessed"
    assert row["cost_usd"] == pytest.approx(0.3)

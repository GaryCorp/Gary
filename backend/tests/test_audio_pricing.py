"""Audio billed by the minute.

Transcription models charge for how long the audio is, not how many tokens
it became. Counting tokens alone would report every spoken conversation as
free, which is exactly the kind of silent zero the ledger exists to prevent.
"""

import pytest

from gary.db.repositories import Repositories
from gary.finance.pricing import PriceTable, Usage
from gary.finance.usage import SpendGate, UsageLedger

from conftest import START

CHICAGO = "America/Chicago"


@pytest.fixture
def prices_file(tmp_path):
    return tmp_path / "model_prices.json"


def table(prices_file, **models) -> PriceTable:
    t = PriceTable(path=prices_file, environ={})
    for model, rates in models.items():
        t.set_price(model.replace("_", "-"), **rates)
    return t


def ledger(gary, clock, prices: PriceTable) -> UsageLedger:
    from zoneinfo import ZoneInfo

    return UsageLedger(gary.db, prices, ZoneInfo(CHICAGO), clock)


def rows(gary) -> list[dict]:
    with gary.db.read() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM model_usage ORDER BY created_at")]


# ------------------------------------------------------------- the rate

def test_audio_is_priced_by_the_minute(prices_file):
    prices = table(prices_file, gpt_transcribe={"per_minute": 0.6})

    # Two minutes at 60 cents a minute.
    assert prices.cost("gpt-transcribe", Usage(audio_seconds=120)) == pytest.approx(1.2)
    # And a part-minute is charged pro rata, not rounded up to a minute.
    assert prices.cost("gpt-transcribe", Usage(audio_seconds=30)) == pytest.approx(0.3)


def test_a_token_model_is_unaffected_by_the_new_rate(prices_file):
    prices = table(prices_file, gpt_5_6_luna={"input": 0.2, "output": 1.2})

    cost = prices.cost("gpt-5-6-luna", Usage(input_tokens=1_000_000, output_tokens=1_000_000))

    assert cost == pytest.approx(1.4)


def test_a_model_can_bill_both_ways(prices_file):
    prices = table(prices_file, both={"output": 1_000.0, "per_minute": 0.6})

    cost = prices.cost("both", Usage(output_tokens=1_000, audio_seconds=60))

    assert cost == pytest.approx(1.0 + 0.6)


def test_an_unpriced_audio_model_is_still_unpriced(prices_file):
    prices = table(prices_file)

    assert prices.cost("gpt-transcribe", Usage(audio_seconds=600)) is None
    assert prices.describe("gpt-transcribe")["priced"] is False


def test_the_per_minute_rate_is_visible(prices_file):
    prices = table(prices_file, gpt_transcribe={"per_minute": 0.6})

    described = prices.describe("gpt-transcribe")

    assert described["priced"] is True
    assert described["rates"] == {"per_minute": 0.6}, "no misleading zero token rates"


# ---------------------------------------------------------- the ledger

def test_audio_seconds_are_recorded_and_costed(gary, clock, prices_file):
    book = ledger(gary, clock, table(prices_file, gpt_transcribe={"per_minute": 0.6}))

    cost = book.record("voice_transcription", "gpt-transcribe", Usage(audio_seconds=90))

    assert cost == pytest.approx(0.9)
    row = rows(gary)[0]
    assert row["source"] == "voice_transcription"
    assert row["audio_seconds"] == 90
    assert row["total_tokens"] == 0, "audio is not tokens"
    assert row["cost_usd"] == pytest.approx(0.9)


def test_spoken_conversation_counts_towards_the_daily_ceiling(gary, clock, prices_file):
    """Without a per-minute rate this spend read as $0 and the ceiling never
    fired, however long Alex talked."""
    prices = table(
        prices_file,
        gpt_transcribe={"per_minute": 1.0},
        gpt_5_6_luna={"input": 0.2, "output": 1.2},
    )
    book = ledger(gary, clock, prices)
    gate = SpendGate(
        book,
        5.0,
        roles={"voice": "gpt-transcribe", "planning": "gpt-5-6-luna"},
        require_priced=True,
    )

    assert gate.state()["allowed"] is True

    # Six minutes of talking, at a dollar a minute.
    book.record("voice_transcription", "gpt-transcribe", Usage(audio_seconds=360))

    state = gate.state(force=True)
    assert state["spent_usd"] == pytest.approx(6.0)
    assert state["allowed"] is False
    assert "daily ceiling" in state["reason"]


def test_the_report_counts_minutes(gary, clock, prices_file):
    book = ledger(gary, clock, table(prices_file, gpt_transcribe={"per_minute": 0.6}))
    book.record("voice_transcription", "gpt-transcribe", Usage(audio_seconds=120))

    summary = book.summary(30)

    assert summary["total"]["audio_seconds"] == 120
    assert summary["total"]["cost_usd"] == pytest.approx(1.2)


# --------------------------------------------------------- reading it out

def test_a_rate_is_described_in_the_unit_it_is_charged_in():
    from gary.finance.pricing import rate_phrase

    assert rate_phrase("per_minute", 0.0045) == "$0.0045 per minute of audio"
    assert rate_phrase("input", 0.2) == "$0.2 per million input tokens"


def test_a_spoken_rate_puts_the_currency_where_a_person_says_it():
    """Piper reads this aloud, and "$" is not a word."""
    from gary.finance.pricing import rate_phrase

    spoken = rate_phrase("per_minute", 0.0045, spoken=True)

    assert spoken == "0.0045 dollars per minute of audio"
    assert "$" not in spoken

"""The daily ceiling on what GaryCorp spends on thinking.

The company runs itself for days at a time, so the thing that must not be
possible is a stuck state quietly spending all week. These tests are mostly
about the refusals, and about the one case the ceiling genuinely cannot
protect against: a model with no price.
"""

import datetime as dt

import pytest

from gary.finance.pricing import PriceTable, Usage
from gary.finance.usage import SpendCeilingReached, SpendGate, UsageLedger

from conftest import START

CHICAGO = "America/Chicago"


class FakeMonotonic:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def prices_file(tmp_path):
    # Never the deployment's real price table: set_price writes to disk.
    return tmp_path / "model_prices.json"


def ledger(gary, clock, prices_file, prices: dict | None = None) -> UsageLedger:
    from zoneinfo import ZoneInfo

    table = PriceTable(path=prices_file, environ={})
    for model, (inp, out) in (prices or {}).items():
        table.set_price(model, input=inp, output=out)
    return UsageLedger(gary.db, table, ZoneInfo(CHICAGO), clock)


def spend(book: UsageLedger, model: str, output_tokens: int, source: str = "planning_cycle"):
    book.record(source, model, Usage(output_tokens=output_tokens))


# ------------------------------------------------------------- the ceiling

def test_under_the_ceiling_work_is_allowed(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"m": (0.0, 1_000.0)})  # $1 per 1k output tokens
    gate = SpendGate(book, 10.0, FakeMonotonic())
    spend(book, "m", 1_000)  # $1

    state = gate.state()
    assert state["allowed"] is True
    assert state["enforceable"] is True
    assert state["spent_usd"] == pytest.approx(1.0)
    assert state["remaining_usd"] == pytest.approx(9.0)
    assert state["reason"] is None


def test_at_the_ceiling_everything_stops(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    gate = SpendGate(book, 10.0, FakeMonotonic())
    spend(book, "m", 10_000)  # $10

    state = gate.state()
    assert state["allowed"] is False
    assert "daily ceiling" in state["reason"]
    assert gate.allowed() is False
    with pytest.raises(SpendCeilingReached, match="daily ceiling"):
        gate.require("a planning cycle")


def test_a_new_day_starts_the_allowance_again(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    monotonic = FakeMonotonic()
    gate = SpendGate(book, 10.0, monotonic)
    spend(book, "m", 10_000)
    assert gate.allowed() is False

    # Past local midnight, and past the cache.
    clock.advance(days=1)
    monotonic.advance(60)

    assert gate.allowed() is True
    assert gate.state()["spent_usd"] == 0


def test_a_ceiling_of_zero_turns_the_limit_off(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    gate = SpendGate(book, 0.0, FakeMonotonic())
    spend(book, "m", 1_000_000)

    state = gate.state()
    assert state["enabled"] is False
    assert state["allowed"] is True
    assert state["reason"] is None


def test_spend_is_not_re_read_on_every_call(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    monotonic = FakeMonotonic()
    gate = SpendGate(book, 10.0, monotonic)

    first = gate.state()["spent_usd"]
    spend(book, "m", 10_000)
    assert gate.state()["spent_usd"] == first, "cached within the window"

    monotonic.advance(SpendGate.CACHE_SECONDS + 1)
    assert gate.state()["spent_usd"] == pytest.approx(10.0)


# -------------------------------------------------- the honest hard part

def test_an_unpriced_model_is_not_reported_as_safe(gary, clock, prices_file):
    """The live case: the deployment's model has no price, so cost reads zero
    and the ceiling cannot fire. It must say so, not imply the company is
    comfortably within budget."""
    book = ledger(gary, clock, prices_file)  # no prices at all
    gate = SpendGate(book, 10.0, FakeMonotonic())
    spend(book, "gpt-5.6-luna", 500_000)

    state = gate.state()
    assert state["spent_usd"] == 0, "nothing can be costed"
    assert state["unpriced_calls"] == 1
    assert state["enforceable"] is False
    assert "cannot be enforced" in state["reason"]
    # It still allows work: blocking would stop a working deployment the
    # moment a new model appeared.
    assert state["allowed"] is True


def test_pricing_the_model_makes_the_ceiling_real(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"gpt-5.6-luna": (0.0, 1_000.0)})
    gate = SpendGate(book, 10.0, FakeMonotonic())
    spend(book, "gpt-5.6-luna", 20_000)

    state = gate.state()
    assert state["enforceable"] is True
    assert state["allowed"] is False


def test_a_mix_of_priced_and_unpriced_still_stops_on_the_priced_part(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"priced": (0.0, 1_000.0)})
    gate = SpendGate(book, 5.0, FakeMonotonic())
    spend(book, "priced", 6_000)   # $6, over the ceiling on its own
    spend(book, "unpriced", 1_000)

    state = gate.state()
    assert state["allowed"] is False, "what we can see already breaches it"
    assert state["enforceable"] is False, "and there is more we cannot see"


# ------------------------------------------- knowing what it runs on

ROLES = {
    "voice": "gpt-transcribe",
    "planning": "gpt-5.6-luna",
    "specialists": "gpt-5.6-luna",
    "web search": "gpt-5.6-luna",
}


def test_every_configured_model_is_visible_before_it_is_called(gary, clock, prices_file):
    """The ledger only knows models that have already spent money. This is
    what lets you see a missing price before it costs anything."""
    book = ledger(gary, clock, prices_file, {"gpt-5.6-luna": (0.2, 1.2)})

    rows = book.models_in_use(ROLES)
    by_model = {row["model"]: row for row in rows}

    assert set(by_model) == {"gpt-transcribe", "gpt-5.6-luna"}
    # One model, every job it does.
    assert sorted(by_model["gpt-5.6-luna"]["roles"]) == ["planning", "specialists", "web search"]
    assert by_model["gpt-5.6-luna"]["priced"] is True
    assert by_model["gpt-5.6-luna"]["rates"]["input"] == 0.2
    assert by_model["gpt-transcribe"]["priced"] is False
    assert by_model["gpt-transcribe"]["rates"] == {}


def test_unattended_work_stops_while_a_model_has_no_price(gary, clock, prices_file):
    book = ledger(gary, clock, prices_file, {"gpt-5.6-luna": (0.2, 1.2)})
    gate = SpendGate(book, 10.0, FakeMonotonic(), roles=ROLES, require_priced=True)

    state = gate.state()
    assert state["within_ceiling"] is True, "nothing has been spent"
    assert state["allowed"] is False, "but it cannot be measured, so it cannot run"
    assert state["enforceable"] is False
    assert state["unpriced_models"] == ["gpt-transcribe"]
    assert "no price is set" in state["reason"].casefold()
    assert "gpt-transcribe" in state["reason"]


def test_pricing_every_model_lets_the_company_run(gary, clock, prices_file):
    book = ledger(
        gary,
        clock,
        prices_file,
        {m: (0.2, 1.2) for m in ("gpt-transcribe", "gpt-5.6-luna")},
    )
    gate = SpendGate(book, 10.0, FakeMonotonic(), roles=ROLES, require_priced=True)

    state = gate.state()
    assert state["allowed"] is True
    assert state["enforceable"] is True
    assert state["unpriced_models"] == []
    assert state["reason"] is None


def test_requiring_prices_can_be_turned_off(gary, clock, prices_file):
    """The old behaviour, for a deployment that would rather keep running."""
    book = ledger(gary, clock, prices_file)
    gate = SpendGate(book, 10.0, FakeMonotonic(), roles=ROLES, require_priced=False)

    state = gate.state()
    assert state["allowed"] is True
    assert state["enforceable"] is False, "it still refuses to claim it is enforced"
    assert "cannot be enforced" in state["reason"]


def test_the_ceiling_still_wins_over_a_missing_price(gary, clock, prices_file):
    """Both problems at once must report the one that actually stops work."""
    book = ledger(gary, clock, prices_file, {"gpt-5.6-luna": (0.0, 1_000.0)})
    gate = SpendGate(book, 5.0, FakeMonotonic(), roles=ROLES, require_priced=True)
    spend(book, "gpt-5.6-luna", 6_000)  # $6, over the ceiling

    state = gate.state()
    assert state["within_ceiling"] is False
    assert state["allowed"] is False
    assert "daily ceiling" in state["reason"]

"""What GaryCorp's thinking costs: pricing, the ledger, and Catherine's report.

Money is reported from recorded tokens and the deployment's price table. An
unpriced model must be reported as unpriced, never as free.
"""

import datetime as dt
import json

import pytest

from gary.db.repositories import Repositories
from gary.finance.pricing import (
    PriceTable,
    Usage,
    usage_from_openai,
    usage_from_tokens,
)
from gary.finance.usage import UsageLedger

from conftest import START, iso

PRICES = {
    "gpt-test": {"input": 1.0, "cached_input": 0.1, "output": 10.0},
    "gpt-voice": {"input": 2.0, "output": 8.0, "audio_input": 40.0, "audio_output": 80.0},
}


@pytest.fixture
def prices(tmp_path):
    path = tmp_path / "model_prices.json"
    path.write_text(json.dumps(PRICES))
    return PriceTable(path, environ={})


@pytest.fixture
def ledger(gary, prices, clock):
    return UsageLedger(gary.db, prices, gary.timezone, clock)


# ------------------------------------------------------------------ pricing

def test_cost_is_per_million_tokens(prices):
    cost = prices.cost("gpt-test", Usage(input_tokens=1_000_000, output_tokens=100_000))
    assert cost == pytest.approx(1.0 + 1.0)  # $1 input + $10/M * 0.1M output


def test_cached_input_is_billed_at_its_own_rate(prices):
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=900_000)
    # 100k fresh at $1/M plus 900k cached at $0.10/M
    assert prices.cost("gpt-test", usage) == pytest.approx(0.1 + 0.09)


def test_audio_tokens_are_priced_separately(prices):
    usage = Usage(audio_input_tokens=100_000, audio_output_tokens=10_000)
    assert prices.cost("gpt-voice", usage) == pytest.approx(4.0 + 0.8)


def test_an_unpriced_model_costs_none_not_zero(prices):
    assert prices.cost("mystery-model", Usage(input_tokens=1_000)) is None
    assert prices.cost(None, Usage(input_tokens=1_000)) is None


def test_prices_can_be_set_and_reloaded(tmp_path):
    path = tmp_path / "prices.json"
    table = PriceTable(path, environ={})
    assert table.known_models() == []

    table.set_price("gpt-new", input=0.5, output=4.0)
    assert table.cost("gpt-new", Usage(input_tokens=1_000_000)) == pytest.approx(0.5)
    # Saved to disk, so a restart keeps it.
    assert PriceTable(path, environ={}).known_models() == ["gpt-new"]

    with pytest.raises(ValueError):
        table.set_price("gpt-broken", input=1.0)  # no output rate
    assert table.remove("gpt-new") is True
    assert PriceTable(path, environ={}).known_models() == []


def test_environment_overrides_the_file(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"gpt-test": {"input": 1.0, "output": 1.0}}))
    table = PriceTable(path, environ={"GARY_MODEL_PRICES": json.dumps(
        {"gpt-test": {"input": 5.0, "output": 5.0}})})
    assert table.cost("gpt-test", Usage(input_tokens=1_000_000)) == pytest.approx(5.0)


def test_openai_usage_is_read_including_audio_and_cache():
    usage = usage_from_openai({
        "input_tokens": 1000, "output_tokens": 200,
        "input_token_details": {"cached_tokens": 400, "audio_tokens": 600},
        "output_token_details": {"audio_tokens": 150},
    })
    assert usage.input_tokens == 400        # 1000 total minus 600 audio
    assert usage.cached_input_tokens == 400
    assert usage.audio_input_tokens == 600
    assert usage.output_tokens == 50
    assert usage.audio_output_tokens == 150
    assert usage.total_tokens == 1200
    assert usage_from_openai(None).total_tokens == 0


# ------------------------------------------------------------------- ledger

def test_every_department_is_recorded_and_totalled(gary, ledger):
    ledger.record("planning_cycle", "gpt-test", usage_from_tokens(100_000, 10_000),
                  entity_type="planning_run", entity_id="run-1")
    ledger.record("specialist", "gpt-test", usage_from_tokens(200_000, 20_000), detail="susan")
    ledger.record("web_search", "gpt-test", usage_from_tokens(50_000, 5_000))
    ledger.record("voice", "gpt-voice", Usage(audio_input_tokens=10_000, output_tokens=1_000))

    summary = ledger.summary(days=7)
    assert summary["total"]["calls"] == 4
    assert {row["source"] for row in summary["by_source"]} == {
        "planning_cycle", "specialist", "web_search", "voice"}

    planning = next(r for r in summary["by_source"] if r["source"] == "planning_cycle")
    assert planning["cost_usd"] == pytest.approx(0.1 + 0.1)  # 100k in, 10k out
    assert summary["total"]["cost_usd"] > 0
    assert summary["today"]["cost_usd"] == summary["total"]["cost_usd"]
    assert summary["unpriced_models"] == []


def test_unpriced_calls_are_counted_but_not_costed(gary, ledger):
    ledger.record("specialist", "mystery-model", usage_from_tokens(1_000, 100))
    summary = ledger.summary(days=7)

    assert summary["total"]["total_tokens"] == 1_100
    assert summary["total"]["cost_usd"] == 0
    assert summary["total"]["unpriced_calls"] == 1
    assert summary["unpriced_models"][0]["model"] == "mystery-model"
    assert any("set a price" in note for note in summary["notes"])


def test_a_provider_reported_cost_wins_over_the_estimate(gary, ledger):
    ledger.record("specialist", "gpt-test", usage_from_tokens(1_000_000, 0),
                  reported_cost_usd=0.42)
    assert ledger.summary(days=7)["total"]["cost_usd"] == pytest.approx(0.42)


def test_empty_calls_are_not_recorded(gary, ledger):
    assert ledger.record("voice", "gpt-voice", Usage()) is None
    assert ledger.summary(days=7)["total"]["calls"] == 0


def test_recording_never_breaks_the_work_it_measures(gary, prices, clock, caplog):
    class BrokenDb:
        def transaction(self):
            raise RuntimeError("database is locked")

    broken = UsageLedger(BrokenDb(), prices, gary.timezone, clock)
    # Returns the cost it computed, and does not raise.
    assert broken.record("voice", "gpt-test", usage_from_tokens(1_000, 0)) is not None


def test_ease_is_reported_as_unmeasured_not_free(gary, ledger):
    assert any("EASE" in note for note in ledger.summary(days=7)["notes"])


def test_spend_today_ignores_older_days(gary, ledger, clock):
    with gary.db.transaction() as conn:
        Repositories.bind(conn).usage.record(
            "planning_cycle", "gpt-test", {"total_tokens": 1_000, "input_tokens": 1_000},
            cost_usd=5.0, occurred_at=iso(START - dt.timedelta(days=3)))
    ledger.record("planning_cycle", "gpt-test", usage_from_tokens(1_000_000, 0))

    assert ledger.spent_today()["cost_usd"] == pytest.approx(1.0)
    assert ledger.summary(days=7)["total"]["cost_usd"] == pytest.approx(6.0)


# ------------------------------------------------------- Catherine's report

def test_catherine_reads_the_costs(gary, ledger):
    from test_agents import build_team
    from gary.agents.gateway import RunState, ToolGateway

    service = build_team(gary)
    service.runner.services.usage = ledger
    ledger.record("planning_cycle", "gpt-test", usage_from_tokens(100_000, 10_000))
    ledger.record("specialist", "mystery-model", usage_from_tokens(2_000, 500), detail="susan")

    gateway = ToolGateway(service.runner.services, service.registry.get("catherine"),
                          RunState("assign-cfo", "catherine"), service.registry.limits)
    import asyncio

    result = asyncio.run(gateway.call("read_ai_usage", {"days": 7}))

    assert result["total_cost_usd"] > 0
    assert {row["source"] for row in result["by_source"]} == {"planning_cycle", "specialist"}
    assert result["unpriced_models"][0]["model"] == "mystery-model"
    assert any("EASE" in note for note in result["notes"])


def test_without_a_ledger_catherine_says_so(gary):
    from test_agents import build_team
    from gary.agents.gateway import RunState, ToolGateway
    import asyncio

    service = build_team(gary)          # no usage ledger wired
    gateway = ToolGateway(service.runner.services, service.registry.get("catherine"),
                          RunState("assign-cfo", "catherine"), service.registry.limits)
    result = asyncio.run(gateway.call("read_ai_usage", {"days": 7}))
    assert result["costs"] == "not available"


# --------------------------------------------------- billed provider costs

BILLED_PAYLOAD = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": 1758153600,
            "results": [
                {"amount": {"value": 1.25, "currency": "usd"}, "line_item": "gpt-5.6-luna, input"},
                {"amount": {"value": 0.75, "currency": "usd"}, "line_item": "gpt-5.6-luna, output"},
            ],
        },
        {
            "object": "bucket",
            "start_time": 1758240000,
            "results": [
                {"amount": {"value": 2.0, "currency": "usd"}, "line_item": "gpt-realtime-2.1, audio input"}
            ],
        },
    ],
}


def test_billed_costs_are_parsed_into_days_and_line_items():
    from gary.finance.provider_costs import parse_costs

    billed = parse_costs(BILLED_PAYLOAD, days=7)
    assert billed["total_cost"] == pytest.approx(4.0)
    assert [row["cost"] for row in billed["daily"]] == [pytest.approx(2.0), pytest.approx(2.0)]
    assert billed["by_line_item"][0]["cost"] == pytest.approx(2.0)
    assert billed["currency"] == "usd"
    assert "whole organization" in billed["note"]


def test_billed_costs_tolerate_an_empty_or_odd_response():
    from gary.finance.provider_costs import parse_costs

    assert parse_costs({}, days=7)["total_cost"] == 0
    odd = {"data": [{"results": [{"amount": {}}, {"no_amount": True}]}]}
    assert parse_costs(odd, days=7)["total_cost"] == 0


def test_without_an_admin_key_billed_costs_say_what_is_needed():
    from gary.finance.provider_costs import OpenAICosts, ProviderCostsError
    import asyncio

    client = OpenAICosts("")
    assert client.configured is False
    with pytest.raises(ProviderCostsError, match="api.usage.read"):
        asyncio.run(client.daily(7))


def test_an_ordinary_key_is_told_it_needs_an_admin_key(monkeypatch):
    """403 is the exact failure an ordinary API key gets; it must explain."""
    import asyncio
    import urllib.error

    from gary.finance import provider_costs as module

    def forbidden(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {},
            __import__("io").BytesIO(b'{"error":"Missing scopes: api.usage.read"}'))

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    client = module.OpenAICosts("sk-not-an-admin-key")
    with pytest.raises(module.ProviderCostsError, match="admin key"):
        asyncio.run(client.daily(7))

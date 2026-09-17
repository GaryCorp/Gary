"""Catherine, GaryCorp's CFO: her debit card and card purchase requests."""

import asyncio
import datetime as dt
import json
import stat
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from gary import build_gary
from gary.agents.gateway import RunState, ToolDenied, ToolGateway
from gary.agents.models import FinanceReport
from gary.agents.roster import AgentLimits
from gary.agents.service import DelegateRequest, ReviewRequest
from gary.db.repositories import Repositories
from gary.finance import cards
from gary.finance.purchases import SpendingLimits, card_purchase_handler, dollars_to_cents, format_cents
from gary.tools import ToolContext, call_tool

from conftest import fake_handlers
from test_agents import FINANCE, FakeExecutor, build_team

VISA = "4242 4242 4242 4242"
VISA_DIGITS = "4242424242424242"
MASTERCARD = "5555555555554444"
TODAY = dt.date(2026, 9, 16)
LIMITS = SpendingLimits(per_purchase_cents=5000, monthly_cents=12000)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def gary(db_path, clock, external):
    handlers = {
        **fake_handlers(external),
        "card_purchase": card_purchase_handler(LIMITS, ZoneInfo("America/Chicago"), clock),
    }
    return build_gary(db_path, "America/Chicago", action_handlers=handlers, clock=clock)


@pytest.fixture
def vault(tmp_path):
    return cards.CardVault(tmp_path / "secrets" / "card_vault.enc", Fernet.generate_key().decode())


def team(gary, limits=None, executor=None):
    service = build_team(gary, executor, limits=limits)
    service.runner.services.spending_limits = LIMITS
    return service


def catherine_gateway(service, assignment_id="assign-cfo"):
    state = RunState(assignment_id, "catherine")
    agent = service.registry.get("catherine")
    return ToolGateway(service.runner.services, agent, state, service.registry.limits), state


def purchase(amount="12.50", merchant="Acme Microphones", **extra):
    return {"merchant": merchant, "description": "USB microphone for the video",
            "amount_usd": amount, "reason": "The current microphone is too noisy for filming.", **extra}


# ------------------------------------------------------------------- the card

def test_card_validation_and_brand():
    assert cards.normalize_card(VISA, "12", "30", " Alex  Doe ", TODAY) == {
        "number": VISA_DIGITS, "exp_month": 12, "exp_year": 2030, "name_on_card": "Alex Doe",
        "brand": "Visa", "last4": "4242",
    }
    assert cards.card_brand(MASTERCARD) == "Mastercard"
    assert cards.card_brand("378282246310005") == "American Express"
    assert cards.card_brand("6011111111111117") == "Discover"
    for number in ("4242424242424241", "1234", "4242-4242-4242-424x", ""):
        with pytest.raises(ValueError, match="not a valid card number"):
            cards.normalize_card(number, 12, 2030, "", TODAY)
    with pytest.raises(ValueError, match="expired"):
        cards.normalize_card(VISA, 8, 2026, "", TODAY)
    assert cards.normalize_card(VISA, 9, 2026, "", TODAY)["exp_month"] == 9  # valid through its month
    with pytest.raises(ValueError, match="real month"):
        cards.normalize_card(VISA, 13, 2030, "", TODAY)


def test_card_number_is_encrypted_and_never_in_the_database(gary, vault, db_path):
    card = cards.add_card(gary.db, vault, VISA, 12, 2030, "Alex Doe", TODAY)
    assert card == {"brand": "Visa", "last4": "4242", "expires": "12/2030", "status": "active"}

    raw = vault.path.read_bytes()
    assert VISA_DIGITS.encode() not in raw
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    assert vault.load() == {"number": VISA_DIGITS, "exp_month": 12, "exp_year": 2030, "name_on_card": "Alex Doe"}

    with gary.db.read() as conn:
        dump = "\n".join(conn.iterdump())
    assert VISA_DIGITS not in dump and "Alex Doe" not in dump
    assert "4242" in dump
    for path in db_path.parent.glob("gary.db*"):
        assert VISA_DIGITS.encode() not in path.read_bytes()

    with pytest.raises(cards.CardVaultError, match="cannot be decrypted"):
        cards.CardVault(vault.path, Fernet.generate_key().decode()).load()


def test_card_cannot_be_added_without_a_key(gary, tmp_path):
    keyless = cards.CardVault(tmp_path / "card_vault.enc", None)
    assert keyless.configured is False
    with pytest.raises(cards.CardVaultError, match="CARD_ENCRYPTION_KEY"):
        cards.add_card(gary.db, keyless, VISA, 12, 2030, "", TODAY)
    with gary.db.read() as conn:
        assert Repositories.bind(conn).finance.current_card("catherine") is None
    assert not keyless.path.exists()


def test_replace_freeze_and_remove_card(gary, vault):
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    replaced = cards.add_card(gary.db, vault, MASTERCARD, 1, 2031, "", TODAY)
    assert replaced["last4"] == "4444"
    assert vault.load()["number"] == MASTERCARD

    assert cards.set_frozen(gary.db, True)["status"] == "frozen"
    with pytest.raises(ValueError, match="already frozen"):
        cards.set_frozen(gary.db, True)
    assert cards.set_frozen(gary.db, False)["status"] == "active"

    cards.remove_card(gary.db, vault)
    assert not vault.path.exists()
    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        assert repos.finance.current_card("catherine") is None
        events = [r["event_type"] for r in repos.audit.list_recent(20)]
    assert events.count("card_added") == 2
    assert {"card_frozen", "card_unfrozen", "card_removed"} <= set(events)
    with gary.db.read() as conn:
        assert VISA_DIGITS not in "\n".join(conn.iterdump())


# ------------------------------------------------------------------ purchases

def test_amount_parsing():
    assert dollars_to_cents("49.99") == 4999
    assert dollars_to_cents("$1,000") == 100000
    assert dollars_to_cents("7.5") == 750
    for bad in ("0", "12.345", "abc", "-5", ""):
        with pytest.raises(ValueError):
            dollars_to_cents(bad)
    assert format_cents(123456) == "$1,234.56"


def test_purchase_request_waits_for_web_approval_and_is_not_charged(gary, vault):
    service = team(gary)
    gateway, state = catherine_gateway(service)

    no_card = run(gateway.call("request_card_purchase", purchase()))
    assert "no debit card" in no_card["error"]

    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    requested = run(gateway.call("request_card_purchase", purchase()))
    assert requested["status"] == "waiting for Alex's approval"
    assert requested["amount"] == "$12.50"
    assert state.purchase_request_ids == [requested["purchase_id"]]

    pending = gary.approvals.list_pending()
    assert len(pending) == 1
    approval = pending[0]
    assert approval["action_type"] == "card_purchase" and approval["risk_level"] == "yellow"
    assert approval["requested_by"] == "catherine"
    assert approval["summary"] == "Card purchase: $12.50 at Acme Microphones for USB microphone for the video"
    assert VISA_DIGITS not in json.dumps(requested) + approval["payload_json"]

    # Not by voice: neither the service nor Gary's tool can approve it.
    with pytest.raises(ValueError, match="only be approved on the approvals page"):
        run(gary.approvals.resolve(approval["id"], "approved", channel="voice"))
    ctx = ToolContext(gary, {"approval_ids": {approval["id"]}})
    by_voice = run(call_tool("approval_resolve", {"approval_id": approval["id"], "decision": "approved",
                                                  "confirmed": True}, ctx))
    assert by_voice["success"] is False and "approvals page" in by_voice["error"]

    result = run(gary.approvals.resolve(approval["id"], "approved", channel="web"))
    execution = result["execution"]
    assert execution["status"] == "succeeded"
    assert execution["result"]["charged"] is False
    assert "not charged" in execution["result"]["note"]
    with gary.db.read() as conn:
        events = [r["event_type"] for r in Repositories.bind(conn).audit.list_recent(10)]
    assert "card_purchase_approved" in events


def test_purchase_can_be_rejected_by_voice(gary, vault):
    service = team(gary)
    gateway, _ = catherine_gateway(service)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    run(gateway.call("request_card_purchase", purchase()))
    approval = gary.approvals.list_pending()[0]
    ctx = ToolContext(gary, {"approval_ids": {approval["id"]}})
    rejected = run(call_tool("approval_resolve", {"approval_id": approval["id"], "decision": "rejected",
                                                  "confirmed": True}, ctx))
    assert rejected["success"] is True and "execution" not in rejected


def test_spending_limits(gary, vault, clock):
    service = team(gary, limits=AgentLimits(max_execution_seconds=5, max_purchase_requests_per_run=10))
    gateway, _ = catherine_gateway(service)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)

    over = run(gateway.call("request_card_purchase", purchase("50.01")))
    assert "over the per-purchase limit of $50.00" in over["error"]

    first = run(gateway.call("request_card_purchase", purchase("45")))
    run(gateway.call("request_card_purchase", purchase("45")))
    # Pending requests count: 90 committed, 45 more would pass 120.
    third = run(gateway.call("request_card_purchase", purchase("45")))
    assert "would exceed the monthly limit of $120.00" in third["error"]
    assert "$90.00 is already requested or approved" in third["error"]

    # A rejected request frees its amount; an approved one stays committed.
    pending = {json.loads(a["payload_json"])["purchase_id"]: a for a in gary.approvals.list_pending()}
    run(gary.approvals.resolve(pending[first["purchase_id"]]["id"], "rejected", channel="web"))
    assert "purchase_id" in run(gateway.call("request_card_purchase", purchase("30")))

    status = run(gateway.call("read_finance_status", {}))
    assert status["spending"]["committed_this_month"] == "$75.00"
    assert status["spending"]["remaining_this_month"] == "$45.00"
    assert status["purchases_waiting_for_approval"] == 2
    assert status["card"] == {"brand": "Visa", "last4": "4242", "expires": "12/2030", "status": "active"}

    # A new month starts from zero.
    clock.advance(days=16)
    assert "purchase_id" in run(gateway.call("request_card_purchase", purchase("50")))


def test_frozen_card_blocks_approval(gary, vault):
    service = team(gary)
    gateway, _ = catherine_gateway(service)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    run(gateway.call("request_card_purchase", purchase()))
    approval = gary.approvals.list_pending()[0]

    cards.set_frozen(gary.db, True)
    frozen = run(gateway.call("request_card_purchase", purchase()))
    assert "is frozen" in frozen["error"]
    result = run(gary.approvals.resolve(approval["id"], "approved", channel="web"))
    assert result["execution"]["status"] == "failed"
    assert "is frozen" in result["execution"]["error"]


def test_purchase_requests_are_limited_per_assignment(gary, vault):
    service = team(gary)
    gateway, _ = catherine_gateway(service)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    run(gateway.call("request_card_purchase", purchase("1")))
    run(gateway.call("request_card_purchase", purchase("1")))
    with pytest.raises(ToolDenied, match="at most 2 times"):
        run(gateway.call("request_card_purchase", purchase("1")))


def test_purchase_request_validation(gary, vault):
    service = team(gary, limits=AgentLimits(max_execution_seconds=5, max_purchase_requests_per_run=10))
    gateway, _ = catherine_gateway(service)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    assert "https" in run(gateway.call("request_card_purchase", purchase(merchant_url="http://shop.example")))["error"]
    assert "US dollars" in run(gateway.call("request_card_purchase", purchase("ten dollars")))["error"]
    with pytest.raises(ValueError, match="Extra inputs"):
        run(gateway.call("request_card_purchase", {**purchase(), "card_number": VISA_DIGITS}))
    assert gary.approvals.list_pending() == []


def test_nobody_else_can_request_purchases(gary, vault):
    service = team(gary)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    for agent_id in ("susan", "dave", "linda"):
        gateway = ToolGateway(service.runner.services, service.registry.get(agent_id),
                              RunState("assign-x", agent_id), service.registry.limits)
        with pytest.raises(ToolDenied, match="not permitted"):
            run(gateway.call("request_card_purchase", purchase()))

    ctx = ToolContext(gary, {}, {"agents": service})
    proposed = run(call_tool("action_propose", {
        "action_type": "card_purchase", "reason": "Gary wants a microphone.",
        "payload": {"purchase_id": "8f5c9a8e-6f0e-4b8e-9d57-1c1f5a0b0c11", "merchant": "Acme",
                    "description": "Microphone", "amount_cents": 1000}}, ctx))
    assert proposed["success"] is False and "Only Catherine" in proposed["error"]
    assert gary.approvals.list_pending() == []


def test_no_purchases_during_management_review(gary, vault):
    service = team(gary)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)
    started = service._start_review_db(ReviewRequest(topic="Should GaryCorp buy a better microphone?",
                                                     agents=["catherine"]), "gary")
    gateway, _ = catherine_gateway(service, started["assignments"][0]["id"])
    refused = run(gateway.call("request_card_purchase", purchase()))
    assert "during a management review" in refused["error"]
    assert gary.approvals.list_pending() == []


# ------------------------------------------------------------------ Catherine

def test_finance_report_validation():
    assert FinanceReport.model_validate({**FINANCE, "assignment_id": "a"}).purchase_request_ids == []
    with pytest.raises(ValidationError):
        FinanceReport.model_validate({**FINANCE, "assignment_id": "a", "budget_assessment": "cheap"})
    with pytest.raises(ValidationError):
        bad_cost = {"item": "Refund", "amount_usd": -5.0, "frequency": "one_time"}
        FinanceReport.model_validate({**FINANCE, "assignment_id": "a", "costs": [bad_cost]})
    with pytest.raises(ValidationError):
        bad_cost = {"item": "Hosting", "amount_usd": 5.0, "frequency": "weekly"}
        FinanceReport.model_validate({**FINANCE, "assignment_id": "a", "costs": [bad_cost]})


def test_catherine_assignment_requests_a_purchase(gary, vault):
    """Alex: Gary, have Catherine buy a USB microphone under forty dollars."""
    def buys(request):
        tools = {t.name: t for t in request.tools}
        assert "request_card_purchase" in tools
        result = tools["request_card_purchase"].invoke(purchase("39.99"))
        assert result["status"] == "waiting for Alex's approval"
        # The model cannot claim purchases it did not request.
        return {**FINANCE, "purchase_request_ids": ["made-up-id"]}

    executor = FakeExecutor({"catherine": [buys]})
    service = team(gary, executor=executor)
    cards.add_card(gary.db, vault, VISA, 12, 2030, "", TODAY)

    async def scenario():
        assignment = await service.delegate(DelegateRequest(
            agent_id="catherine", objective="Buy a USB microphone for filming, under forty dollars."))
        await service.wait([assignment["id"]], timeout=20)
        return await asyncio.to_thread(service.get_assignment, assignment["id"])
    result = run(scenario())

    assert result["status"] == "completed"
    pending = gary.approvals.list_pending()
    assert len(pending) == 1
    purchase_id = json.loads(pending[0]["payload_json"])["purchase_id"]
    assert result["report"]["purchase_request_ids"] == [purchase_id]
    assert json.loads(pending[0]["payload_json"])["assignment_id"] == result["assignment_id"]

    description = executor.requests[0].task_description
    assert "finance_status" in description and "4242" in description
    assert VISA_DIGITS not in description


def test_ai_usage_report(gary):
    service = team(gary)

    async def scenario():
        assignment = await service.delegate(DelegateRequest(agent_id="dave", objective="Threat-model the finance page."))
        await service.wait([assignment["id"]], timeout=20)
        gateway, _ = catherine_gateway(service)
        return await gateway.call("read_ai_usage", {"days": 7})
    usage = run(scenario())
    dave = next(row for row in usage["specialist_runs"] if row["agent_id"] == "dave")
    assert dave["runs"] == 1 and dave["total_tokens"] == 1234

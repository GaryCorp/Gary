"""Marcus, GaryCorp's CMO: what the company may say about itself.

Marketing is the one department whose output is words said to strangers, so
the report is shaped to keep a claim next to the evidence for it, and Marcus
holds nothing that can publish anything.
"""

import asyncio

import pytest
from pydantic import ValidationError

from gary.agents.models import MarketingReport
from gary.agents.service import DelegateRequest, ReviewRequest
from gary.db.repositories import Repositories

from test_agents import MARKETING, FakeExecutor, build_team


def run(coroutine):
    return asyncio.run(coroutine)


def marcus_report(gary, executor=None):
    """Run one assignment for Marcus and return the stored report."""
    service = build_team(gary, executor or FakeExecutor())

    async def scenario():
        assignment = await service.delegate(
            DelegateRequest(agent_id="marcus", objective="How should we announce the browser capability?")
        )
        await service.wait([assignment["id"]], timeout=20)
        return assignment["id"]

    return service, service.get_assignment(run(scenario()))


# ------------------------------------------------------------------- the role

def test_the_cmo_is_advisory_and_cannot_say_anything_himself(gary):
    service = build_team(gary)
    marcus = service.registry.get("marcus")
    # He can look outward and read the company; he cannot reach anyone.
    assert "web_search" in marcus.allowed_tools
    assert marcus.can_delegate is False
    for tool in marcus.allowed_tools:
        assert not tool.startswith(("send_", "email_", "publish_", "post_", "request_"))
    # His notebook is the only thing he writes to.
    assert marcus.notebook == "Marcus"


def test_the_cmo_reports_to_gary_like_every_other_officer(gary):
    service = build_team(gary)
    assert service.registry.get("marcus").reports_to == "gary"
    assert service.registry.get("marcus").department == "Marketing"


# ----------------------------------------------------------------- the report

def test_a_marketing_report_is_stored_and_typed(gary):
    _, assignment = marcus_report(gary)
    assert assignment["status"] == "completed"
    report = MarketingReport.model_validate(assignment["report"])
    assert report.readiness == "ready_with_changes"
    assert report.positioning.startswith("The assistant that reads the web")


def test_a_claim_keeps_the_evidence_that_makes_it_true(gary):
    _, assignment = marcus_report(gary)
    report = MarketingReport.model_validate(assignment["report"])
    assert report.claims == ["It reads pages without storing personal data"]
    assert report.evidence_for_claims == ["Dave's control: no personal data is stored"]


def test_what_cannot_be_supported_is_kept_rather_than_softened(gary):
    """The field exists so an unsupportable claim is visible, not dropped."""
    _, assignment = marcus_report(gary)
    report = MarketingReport.model_validate(assignment["report"])
    assert report.claims_we_cannot_support == ["That it is faster than a human researcher"]
    # And it never leaked into the claims the company would actually make.
    assert not set(report.claims_we_cannot_support) & set(report.claims)


def test_the_audit_summary_names_the_readiness(gary):
    _, assignment = marcus_report(gary)
    with gary.db.read() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT event_type, summary FROM audit_log WHERE entity_id = ? ORDER BY id",
                (assignment["assignment_id"],),
            )
        ]
    completed = [r for r in rows if r["event_type"] == "agent_assignment_completed"]
    assert completed, rows
    assert "marketing assessment (ready_with_changes)" in completed[-1]["summary"]


def test_a_report_without_evidence_for_its_claims_is_refused():
    """Every list is required: a model that omits one is not a valid report."""
    incomplete = {k: v for k, v in MARKETING.items() if k != "evidence_for_claims"}
    incomplete["assignment_id"] = "assign-1"
    with pytest.raises(ValidationError):
        MarketingReport.model_validate(incomplete)


def test_a_report_cannot_invent_extra_fields():
    payload = {**MARKETING, "assignment_id": "assign-1", "budget_usd": 5000}
    with pytest.raises(ValidationError):
        MarketingReport.model_validate(payload)


@pytest.mark.parametrize("readiness", ["ready", "ready_with_changes", "not_ready", "unknown"])
def test_readiness_is_a_closed_set(readiness):
    report = MarketingReport.model_validate({**MARKETING, "assignment_id": "a", "readiness": readiness})
    assert report.readiness == readiness


def test_an_invented_readiness_is_refused():
    with pytest.raises(ValidationError):
        MarketingReport.model_validate({**MARKETING, "assignment_id": "a", "readiness": "amazing"})


# ------------------------------------------------------------------ in review

def test_the_cmo_has_his_own_place_in_a_management_review(gary):
    """Not the advisory dict a hired employee lands in: a named field."""
    service = build_team(gary, FakeExecutor())

    async def scenario():
        started = await service.start_review(
            ReviewRequest(topic="Should we announce the browser capability?")
        )
        await service.wait([a["id"] for a in started["assignments"]], timeout=20)
        return started["review"]["id"]

    review = service.get_review(run(scenario()))
    assert review.marketing is not None
    assert review.marketing.readiness == "ready_with_changes"
    assert "marcus" not in review.advisory


def test_the_roster_in_the_database_mirrors_the_new_officer(gary):
    service = build_team(gary)
    service.sync_roster()
    with gary.db.read() as conn:
        rows = {row["id"]: row for row in Repositories.bind(conn).agents.list_all()}
    assert rows["marcus"]["name"] == "Marcus"
    assert rows["marcus"]["title"] == "Chief Marketing Officer"

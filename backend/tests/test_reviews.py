"""Performance reviews: the facts, the judgment, and who may write one.

The scorecard is arithmetic over what the company recorded and is the same
whoever reads it; the judgment is one model call, validated before it is
stored. Gary reviews his people and Alex, his people review Gary, and nobody
reviews themselves.
"""

import datetime as dt
import json

import pytest

from conftest import START, iso, make_task, run
from gary.agents.gateway import FORBIDDEN_TOOLS
from gary.agents.roster import AgentRegistry
from gary.db.repositories import Repositories
from gary.models.task import CompleteTaskRequest
from gary.reviewer import instructions_for
from gary.services.common import NotFoundError
from gary.services.review_service import (
    PerformanceReviews,
    ReviewError,
    validate_judgment,
)

JUDGMENT = {
    "summary": "Susan delivers, but her confidence runs ahead of her results.",
    "strengths": ["Four assignments completed out of five"],
    "concerns": ["One confident failure at 0.9 stated confidence"],
    "recommendations": ["State lower confidence when sources are thin"],
    "evidence": ["assignments: 5", "confident_failures: 1"],
    "_usage": {"input_tokens": 900, "output_tokens": 200},
    "_model": "gpt-5.6-luna",
}


class FakeReviewer:
    """Stands in for the model. Records what it was asked to judge."""

    def __init__(self, judgment: dict | None = None, fail: str | None = None):
        self.judgment = judgment or JUDGMENT
        self.fail = fail
        self.calls: list[dict] = []

    async def write_review(self, kind, subject, scorecard, reviewer=None):
        self.calls.append(
            {"kind": kind, "subject": subject, "scorecard": scorecard, "reviewer": reviewer}
        )
        if self.fail:
            raise RuntimeError(self.fail)
        return dict(self.judgment)


def reviews(gary, clock, reviewer=None, registry=None, usage=None) -> PerformanceReviews:
    return PerformanceReviews(
        gary.db,
        registry=registry if registry is not None else AgentRegistry(),
        reviewer=reviewer,
        clock=clock,
        usage=usage,
    )


def give_work(
    gary, agent_id: str, count: int = 1, status: str = "completed", confidence=None, clock=None
):
    """A record for an agent: assignments that were run and finished, at
    whatever the clock says now, so a later month's work lands in the later
    month's window."""
    now = iso(clock()) if clock else iso(START)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        for definition in AgentRegistry().all():
            repos.agents.upsert(
                agent_id=definition.agent_id,
                name=definition.name,
                title=definition.title,
                department=definition.department,
                reports_to=definition.reports_to,
                is_employee=definition.agent_id != "gary",
                active=True,
                now=now,
            )
        for index in range(count):
            assignment = repos.assignments.create(
                assigned_by="gary",
                assigned_to=agent_id,
                objective=f"Look into thing {index}",
                now=now,
            )
            result = {"summary": "Done"}
            if confidence is not None:
                result["confidence"] = confidence
            repos.assignments.transition(
                assignment["id"],
                assignment["status"],
                status,
                completed_at=now,
                result_json=json.dumps(result),
            )


# ------------------------------------------------------------------ facts


def test_the_scorecard_is_arithmetic_not_opinion(gary, clock):
    give_work(gary, "susan", count=2, confidence=0.9)
    give_work(gary, "susan", count=1, status="failed", confidence=0.9)

    card = reviews(gary, clock).scorecard("susan", "employee")

    assert card["assignments"] == 3
    assert card["completed"] == 2 and card["failed"] == 1
    assert card["mean_stated_confidence"] == 0.9
    # Sure of itself and wrong is the number worth reading.
    assert card["confident_failures"] == 1
    assert card["enough_to_review"] is True


def test_alexs_scorecard_counts_what_he_did_with_what_he_was_given(gary, clock):
    task = make_task(gary, title="Video 2: Script")
    gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))
    with gary.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            "gary", "daily_checkin_asked", "Checked in", "assignment", "2026-09-16",
            {"assigned": 3, "done": 1}, now=iso(START),
        )

    card = reviews(gary, clock).scorecard("alex", "principal")

    assert card["tasks_completed"] == 1
    assert card["tasks_assigned"] == 3
    assert card["tasks_done_same_day"] == 1
    assert card["days_checked_in"] == 1


def test_a_thin_record_is_not_reviewed(gary, clock):
    service = reviews(gary, clock, FakeReviewer())
    with pytest.raises(ReviewError, match="not enough of a record"):
        run(service.run("susan"))


# --------------------------------------------------------------- judgment


def test_a_review_stores_the_facts_and_the_judgment_apart(gary, clock):
    give_work(gary, "susan", count=2, confidence=0.9)
    reviewer = FakeReviewer()
    service = reviews(gary, clock, reviewer)

    review = run(service.run("susan"))

    assert review["subject"] == "susan" and review["subject_kind"] == "employee"
    assert review["reviewer"] == "gary"
    assert review["summary"].startswith("Susan delivers")
    assert review["concerns"] == ["One confident failure at 0.9 stated confidence"]
    # The facts are stored as computed, not as the model retold them.
    assert review["scorecard"]["assignments"] == 2
    assert review["model"] == "gpt-5.6-luna"

    # The model was handed the numbers rather than asked to remember them.
    assert reviewer.calls[0]["kind"] == "employee"
    assert reviewer.calls[0]["scorecard"]["assignments"] == 2

    with gary.db.read() as conn:
        events = [
            r[0] for r in conn.execute(
                "SELECT event_type FROM audit_log WHERE entity_id = 'susan' ORDER BY id"
            )
        ]
    assert "performance_review_written" in events


@pytest.mark.parametrize(
    "judgment, reason",
    [
        ({"summary": "Fine.", "evidence": ["assignments: 2"]}, "needs a summary"),
        ({"summary": "A" * 40, "evidence": []}, "quote the figures"),
    ],
)
def test_an_empty_judgment_is_not_stored(judgment, reason):
    with pytest.raises(ReviewError, match=reason):
        validate_judgment(judgment)


def test_lists_are_capped_and_tidied():
    checked = validate_judgment(
        {
            "summary": "A sentence long enough to count as a summary here.",
            "strengths": ["  spaced   out  ", "", "b", "c", "d", "e", "f", "g"],
            "evidence": ["assignments: 2"],
        }
    )
    assert checked["strengths"][0] == "spaced out"
    assert len(checked["strengths"]) == 6


def test_a_review_is_priced_like_any_other_model_call(gary, clock):
    class Ledger:
        def __init__(self):
            self.recorded = []

        def record(self, source, model, usage, **kwargs):
            self.recorded.append((source, model, kwargs.get("entity_id")))

    give_work(gary, "susan", count=2)
    ledger = Ledger()
    run(reviews(gary, clock, FakeReviewer(), usage=ledger).run("susan"))

    assert ledger.recorded == [("performance_review", "gpt-5.6-luna", "susan")]


# ------------------------------------------------------- who reviews whom


def test_gary_cannot_review_himself(gary, clock):
    service = reviews(gary, clock, FakeReviewer())
    with pytest.raises(ReviewError, match="cannot review himself"):
        run(service.run("gary"))


def test_an_employee_cannot_review_a_colleague(gary, clock):
    give_work(gary, "susan", count=2)
    service = reviews(gary, clock, FakeReviewer())
    with pytest.raises(ReviewError, match="Only Gary reviews"):
        run(service.run("susan", reviewer_id="dave"))


def test_an_employee_reviews_gary_and_he_has_to_answer(gary, clock):
    give_work(gary, "susan", count=2)
    reviewer = FakeReviewer(
        {
            "summary": "Gary sends work that is not worth doing and asks Alex too often.",
            "strengths": ["Objectives are clear"],
            "concerns": ["Two of five reports were never used"],
            "recommendations": ["Say what a report is for before commissioning it"],
            "evidence": ["delegations: 5", "things_raised_with_alex: 9"],
        }
    )
    service = reviews(gary, clock, reviewer)

    review = run(service.run("gary", reviewer_id="susan"))

    assert review["subject"] == "gary" and review["subject_kind"] == "manager"
    assert review["reviewer"] == "susan"
    # Written as Susan, on the employees' side of the company.
    assert reviewer.calls[0]["reviewer"] == {"name": "Susan", "title": "Director of Research & Strategy"}
    assert service.unacknowledged_of_manager()[0]["id"] == review["id"]

    answered = service.acknowledge(
        review["id"], "Fair. I will say what each report is for when I commission it."
    )
    assert answered["acknowledgement"].startswith("Fair.")
    assert answered["acknowledged_at"]
    assert service.unacknowledged_of_manager() == []


def test_an_acknowledgement_has_to_say_something(gary, clock):
    give_work(gary, "susan", count=2)
    service = reviews(gary, clock, FakeReviewer())
    review = run(service.run("gary", reviewer_id="susan"))

    with pytest.raises(ReviewError, match="say something"):
        service.acknowledge(review["id"], "noted")
    with pytest.raises(ReviewError, match="already been answered"):
        service.acknowledge(review["id"], "A perfectly reasonable answer to the point raised.")
        service.acknowledge(review["id"], "A perfectly reasonable answer to the point raised.")


def test_only_a_review_of_gary_is_acknowledged(gary, clock):
    give_work(gary, "susan", count=2)
    service = reviews(gary, clock, FakeReviewer())
    review = run(service.run("susan"))

    with pytest.raises(ReviewError, match="Only a review of Gary"):
        service.acknowledge(review["id"], "I would like to answer my own review, please.")
    with pytest.raises(NotFoundError):
        service.acknowledge("not-a-review", "An answer to a review that does not exist.")


def test_specialists_can_never_hold_the_review_tools():
    assert {
        "performance_scorecard",
        "performance_write_review",
        "performance_list_reviews",
        "performance_acknowledge",
    } <= FORBIDDEN_TOOLS


# ------------------------------------------------------------- the round


def test_a_round_reviews_the_people_with_a_record_and_nobody_twice(gary, clock):
    give_work(gary, "susan", count=2)
    give_work(gary, "dave", count=1)
    service = reviews(gary, clock, FakeReviewer())

    due = service.due()
    assert set(due["employees"]) == {"susan", "dave"}
    assert set(due["upward_reviewers"]) == {"susan", "dave"}

    result = run(service.run_all_due())
    written = {(r["subject"], r["reviewer"]) for r in result["written"]}
    assert written == {
        ("susan", "gary"), ("dave", "gary"),
        ("gary", "susan"), ("gary", "dave"),
    }
    assert result["failed"] == []

    # Nobody is due again inside the interval.
    assert run(service.run_all_due())["written"] == []


def test_the_round_is_capped(gary, clock):
    give_work(gary, "susan", count=2)
    give_work(gary, "dave", count=2)
    service = reviews(gary, clock, FakeReviewer())

    result = run(service.run_all_due(limit=2))
    assert len(result["written"]) == 2


def test_a_subject_becomes_due_again_after_the_interval(gary, clock):
    give_work(gary, "susan", count=2)
    service = reviews(gary, clock, FakeReviewer())
    run(service.run("susan"))

    assert "susan" not in service.due()["employees"]

    # A month later, with fresh work: due again. (Without new work the
    # window is empty, and an employee with no record is not reviewed.)
    clock.advance(days=29)
    give_work(gary, "susan", count=2, clock=clock)
    assert "susan" in service.due()["employees"]


def test_one_failure_does_not_stop_the_round(gary, clock):
    give_work(gary, "susan", count=2)
    service = reviews(gary, clock, FakeReviewer(fail="OpenAI is down"))

    result = run(service.run_all_due())
    assert result["written"] == []
    assert result["failed"][0]["error"] == "OpenAI is down"


# -------------------------------------------------------- what is asked of it


def test_the_upward_review_asks_for_criticism_not_manners():
    text = instructions_for("manager", "gary", "Alex", {"name": "Susan", "title": "Head of Research"})
    assert "Susan, Head of Research" in text
    assert "You may be critical" in text
    assert "He will read it and has to answer." in text


def test_every_review_is_told_not_to_invent_figures():
    for kind in ("employee", "principal", "manager"):
        text = instructions_for(kind, "susan", "Alex", {"name": "Susan", "title": "Head"})
        assert "Never invent a figure" in text


def test_a_review_is_a_source_the_ledger_accepts(gary, clock):
    """The first live review was stored but its cost was not: the ledger
    refused 'performance_review' as a source."""
    from gary.db.repositories.usage import SOURCES
    from gary.finance.pricing import PriceTable, usage_from_openai
    from gary.finance.usage import UsageLedger

    assert "performance_review" in SOURCES
    ledger = UsageLedger(gary.db, PriceTable({}), gary.timezone, clock)
    ledger.record(
        "performance_review",
        "gpt-5.6-luna",
        usage_from_openai({"input_tokens": 900, "output_tokens": 200}),
        entity_type="agent",
        entity_id="susan",
    )
    with gary.db.read() as conn:
        rows = [dict(r) for r in conn.execute("SELECT source, model FROM model_usage")]
    assert rows == [{"source": "performance_review", "model": "gpt-5.6-luna"}]

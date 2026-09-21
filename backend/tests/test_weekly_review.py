"""A week of running itself: what broke, and what the week is reported as.

The review makes no model call on purpose, so the week the spend ceiling
stopped everything else is still the week Alex gets a report for. These tests
cover that, and the honesty rule that unpriced spend is never reported as a
number.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from gary.db.repositories import Repositories
from gary.finance.pricing import PriceTable, Usage
from gary.models.task import CompleteTaskRequest
from gary.finance.usage import UsageLedger
from gary.services.planning_service import company_health
from gary.services.weekly_review import WeeklyReview, review_title

from conftest import START, iso, make_task, run

CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture
def prices_file(tmp_path):
    return tmp_path / "model_prices.json"


def book(gary, clock, prices_file, prices=None):
    table = PriceTable(path=prices_file, environ={})
    for model, (inp, out) in (prices or {}).items():
        table.set_price(model, input=inp, output=out)
    return UsageLedger(gary.db, table, CHICAGO, clock)


def review(gary, clock, ledger=None):
    return WeeklyReview(gary.db, CHICAGO, ledger, clock)


def health(gary, now=None):
    with gary.db.read() as conn:
        return company_health(Repositories.bind(conn), now or iso(START))


# ----------------------------------------------------- what is stuck

def test_a_quiet_company_reports_nothing_stuck(gary):
    make_task(gary, title="Film the demo")
    result = health(gary)

    assert result["healthy"] is True
    assert sum(result["counts"].values()) == 0


def test_an_expired_approval_and_question_are_noticed(gary, clock):
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        approval = repos.approvals.create(
            action_type="send_external_email",
            summary="Email the sponsor",
            payload={},
            risk_level="yellow",
            now=iso(START),
        )
        repos.approvals.resolve(approval["id"], "expired", "Not answered in time", iso(START))
        message = repos.spoken.create(
            text="Shall I close the newsletter project?",
            kind="question",
            expects_reply=True,
            now=iso(START),
        )
        repos.spoken.mark_spoken(message["id"], iso(START))
        repos.spoken.expire(message["id"], iso(START))

    result = health(gary)

    assert result["healthy"] is False
    assert result["counts"]["expired_approvals"] == 1
    assert result["counts"]["unanswered_questions"] == 1
    assert "sponsor" in result["expired_approvals"][0]["summary"]
    # Each carries when it became a problem, so a caller can ask what is new.
    assert result["expired_approvals"][0]["at"]


def test_an_overdue_task_is_noticed(gary, clock):
    make_task(gary, title="Ship the thing", deadline=iso(START - dt.timedelta(days=1)))
    result = health(gary)

    assert result["counts"]["overdue_tasks"] == 1
    assert result["overdue_tasks"][0]["title"] == "Ship the thing"


def test_health_reaches_the_planner(gary):
    make_task(gary, title="Ship the thing", deadline=iso(START - dt.timedelta(days=1)))

    snapshot = gary.planning.snapshot()

    assert snapshot["company_health"]["healthy"] is False
    assert snapshot["company_health"]["counts"]["overdue_tasks"] == 1


# ------------------------------------------------------- the review

def test_the_week_is_reported_without_a_model_call(gary, clock, prices_file):
    task = make_task(gary, title="Film the demo")
    # Through the service, so completed_at is set the way the query expects.
    gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))

    data = review(gary, clock, book(gary, clock, prices_file)).collect()
    markdown = review(gary, clock).render_markdown(data)

    assert "# Week to" in markdown
    assert "Film the demo" in markdown
    assert "## What needs you" in markdown
    assert "Nothing is stuck." in markdown


def test_unpriced_spend_is_never_reported_as_a_number(gary, clock, prices_file):
    ledger = book(gary, clock, prices_file)  # no prices
    ledger.record("planning_cycle", "gpt-5.6-luna", Usage(output_tokens=50_000))

    data = review(gary, clock, ledger).collect()
    markdown = review(gary, clock, ledger).render_markdown(data)

    assert data["spend"]["measured"] is False
    assert "unpriced" in markdown
    assert "gpt-5.6-luna" in markdown
    assert "$0.00" not in markdown, "never claim the week was free"


def test_priced_spend_is_reported_as_money(gary, clock, prices_file):
    ledger = book(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    ledger.record("planning_cycle", "m", Usage(output_tokens=2_000))

    data = review(gary, clock, ledger).collect()
    markdown = review(gary, clock, ledger).render_markdown(data)

    assert data["spend"]["measured"] is True
    assert "$2.00" in markdown


def test_the_spoken_summary_says_what_needs_alex(gary, clock, prices_file):
    make_task(gary, title="Ship the thing", deadline=iso(START - dt.timedelta(days=1)))
    ledger = book(gary, clock, prices_file)  # no prices
    ledger.record("planning_cycle", "gpt-5.6-luna", Usage(output_tokens=1_000))
    data = review(gary, clock, ledger).collect()

    spoken = review(gary, clock, ledger).spoken_summary(data)

    assert "need you" in spoken
    assert "no price set" in spoken, "it must not imply it knows the cost"
    # Piper reads this: plain words only.
    assert not any(ch in spoken for ch in "*_#`|")


def test_a_healthy_week_says_nothing_is_stuck(gary, clock, prices_file):
    ledger = book(gary, clock, prices_file, {"m": (0.0, 1_000.0)})
    data = review(gary, clock, ledger).collect()

    assert "Nothing is stuck." in review(gary, clock, ledger).spoken_summary(data)


def test_one_note_per_week(clock):
    friday = dt.date(2026, 9, 18)
    assert review_title(friday) == "Weekly review 2026-09-18"

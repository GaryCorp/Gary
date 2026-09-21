"""The facts a review stands on.

A performance review is only defensible if its numbers are checkable, so
these are arithmetic tests: seed a record, assert the counts. What the
numbers mean is the reviewer's job and is tested elsewhere.
"""

import datetime as dt
import json

import pytest

from gary.db.repositories import Repositories
from gary.services.performance import (
    employee_scorecard,
    manager_scorecard,
    principal_scorecard,
)

from conftest import START, iso, make_task

SINCE = iso(START - dt.timedelta(days=7))
NOW = iso(START)


def card(gary, fn, *args):
    with gary.db.read() as conn:
        return fn(Repositories.bind(conn), *args, SINCE, NOW)


def org_chart(gary):
    """agent_assignments has a foreign key to agents, which the app fills
    from the roster at startup."""
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        repos.agents.upsert("gary", "Gary", "Chief of Staff", "Executive",
                            None, False, True, NOW)
        for agent_id in ("susan", "dave", "linda", "catherine", "lauren"):
            repos.agents.upsert(agent_id, agent_id.title(), "Director", "Dept",
                                "gary", True, True, NOW)


def assignment(gary, agent_id, status="completed", confidence=None, minutes=5, cost=0.01):
    """One piece of work, with the run detail a scorecard reads."""
    org_chart(gary)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        row = repos.assignments.create(
            assigned_by="gary",
            assigned_to=agent_id,
            objective=f"Something useful for {agent_id} to look at",
            now=iso(START - dt.timedelta(days=1)),
        )
        result = {"confidence": confidence} if confidence is not None else {}
        repos.assignments.update(
            row["id"], status=status, result_json=json.dumps(result), completed_at=NOW
        )
        run = repos.agent_runs.start(
            row["id"], agent_id, "gpt-5.6-luna",
            now=iso(START - dt.timedelta(minutes=minutes)),
        )
        # A run's vocabulary is not an assignment's: succeeded, not completed.
        run_status = "succeeded" if status == "completed" else "failed"
        repos.agent_runs.update(
            run["id"], status=run_status, attempts=1, tool_calls=3,
            total_tokens=150, cost_usd=cost, completed_at=NOW,
        )
    return row


# ------------------------------------------------------- an employee

def test_a_specialist_with_no_work_cannot_be_reviewed(gary):
    """Reviewing someone who was never asked to do anything would be
    opinion dressed as assessment."""
    result = card(gary, employee_scorecard, "susan")

    assert result["assignments"] == 0
    assert result["enough_to_review"] is False
    assert result["completion_rate"] is None


def test_a_specialists_record_is_counted_exactly(gary):
    assignment(gary, "susan", "completed", confidence=0.8, cost=0.02)
    assignment(gary, "susan", "completed", confidence=0.6, cost=0.04)
    assignment(gary, "susan", "failed", confidence=0.9, cost=0.01)

    result = card(gary, employee_scorecard, "susan")

    assert result["assignments"] == 3
    assert result["completed"] == 2 and result["failed"] == 1
    assert result["completion_rate"] == 0.67
    assert result["total_cost_usd"] == pytest.approx(0.07)
    assert result["cost_per_completed_report_usd"] == pytest.approx(0.035)
    assert result["mean_stated_confidence"] == pytest.approx(0.77, abs=0.01)
    assert result["enough_to_review"] is True


def test_confident_failure_is_singled_out(gary):
    """Confident and wrong is a different problem from cautious and wrong,
    and counting successes alone would miss it."""
    assignment(gary, "dave", "failed", confidence=0.95)
    assignment(gary, "dave", "failed", confidence=0.3)

    result = card(gary, employee_scorecard, "dave")

    assert result["failed"] == 2
    assert result["confident_failures"] == 1


def test_tool_denials_are_counted(gary):
    assignment(gary, "linda")
    with gary.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            "linda", "agent_tool_denied", "Linda was denied tool spend_money",
            "agent_assignment", "a-1", {"tool": "spend_money"}, now=NOW,
        )

    assert card(gary, employee_scorecard, "linda")["tool_denials"] == 1


def test_one_specialist_is_not_judged_on_another_s_record(gary):
    assignment(gary, "susan", "completed")
    assignment(gary, "dave", "failed")

    assert card(gary, employee_scorecard, "susan")["completed"] == 1
    assert card(gary, employee_scorecard, "susan")["failed"] == 0
    assert card(gary, employee_scorecard, "dave")["failed"] == 1


# --------------------------------------------------------- the principal

def test_alex_is_measured_on_what_he_left_undone(gary, clock):
    from gary.models.task import CompleteTaskRequest

    done = make_task(gary, title="Film the demo")
    gary.tasks.complete_task(CompleteTaskRequest(task_id=done["id"]))
    make_task(gary, title="Ship the thing", deadline=iso(START - dt.timedelta(days=1)))

    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        approval = repos.approvals.create(
            action_type="send_external_email", summary="Email the sponsor",
            payload={}, risk_level="yellow", now=SINCE,
        )
        repos.approvals.resolve(approval["id"], "expired", "Not answered in time", NOW)
        message = repos.spoken.create(
            text="Shall I close the newsletter project?", kind="question",
            expects_reply=True, now=SINCE,
        )
        repos.spoken.mark_spoken(message["id"], SINCE)
        repos.spoken.expire(message["id"], NOW)

    result = card(gary, principal_scorecard)

    assert result["tasks_completed"] == 1
    assert result["tasks_overdue"] == 1
    assert result["approvals_expired_unanswered"] == 1
    assert result["questions_left_unanswered"] == 1
    assert result["enough_to_review"] is True


def test_a_quiet_week_gives_alex_nothing_to_answer_for(gary):
    result = card(gary, principal_scorecard)

    assert result["enough_to_review"] is False
    assert result["tasks_overdue"] == 0


# ----------------------------------------------------------- the manager

def test_gary_is_measured_on_what_he_did_with_the_company(gary):
    assignment(gary, "susan", "completed")
    assignment(gary, "dave", "completed")

    result = card(gary, manager_scorecard)

    assert result["delegations"] == 2
    assert result["reports_received"] == 2
    assert result["enough_to_review"] is True


def test_gary_is_measured_on_whether_alex_answered_him(gary):
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        for i, answer in enumerate([True, False]):
            m = repos.spoken.create(
                text=f"A question about matter number {i}", kind="question",
                expects_reply=True, now=SINCE,
            )
            repos.spoken.mark_spoken(m["id"], SINCE)
            if answer:
                repos.spoken.answer(m["id"], "yes", NOW)

    result = card(gary, manager_scorecard)

    assert result["things_raised_with_alex"] == 2
    assert result["answered_by_alex"] == 1

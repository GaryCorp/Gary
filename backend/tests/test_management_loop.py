"""The continuous loop's cheap decision: is anything worth waking Gary for?

A tick that finds nothing must cost nothing, and a trigger must not fire
twice for the same event, or an unattended week becomes an expensive one.
"""

import datetime as dt

from gary.db.repositories import Repositories
from gary.models.followup import CreateFollowupRequest
from gary.services.management_loop import (
    DailyBudget,
    Triggers,
    completed_since,
    find_triggers,
)

from conftest import START, iso, make_task

NOW = iso(START)
LATER = iso(START + dt.timedelta(hours=2))


def triggers(gary, since=None, reports=0, now=NOW):
    return find_triggers(gary, now=now, since=since, completed_reports=reports)


def test_a_quiet_company_does_not_wake_gary(gary):
    make_task(gary, title="Film the demo")
    result = triggers(gary, since=NOW)
    assert not result
    assert result.describe() == "nothing changed"


def test_a_returned_report_wakes_gary(gary):
    result = triggers(gary, since=NOW, reports=2)
    assert result
    assert result.reports == 2
    assert "2 department reports came back" in result.describe()


def test_a_missed_block_wakes_gary_once(gary):
    task = make_task(gary, title="Film the demo")
    with gary.db.transaction() as conn:
        Repositories.bind(conn).tasks.update(
            task["id"],
            status="scheduled",
            calendar_event_id="event-1",
            scheduled_start=iso(START - dt.timedelta(hours=2)),
            scheduled_end=iso(START - dt.timedelta(hours=1)),
        )
    # The block ended after the last cycle looked, so it is news.
    assert triggers(gary, since=iso(START - dt.timedelta(hours=3)))
    # The next cycle already saw it: not news any more.
    assert not triggers(gary, since=NOW)


def test_a_due_followup_wakes_gary(gary):
    task = make_task(gary, title="Email the sponsor")
    gary.followups.create_followup(
        CreateFollowupRequest(
            title="Check the sponsor replied",
            due_at=iso(START + dt.timedelta(minutes=10)),
            task_id=task["id"],
        )
    )
    assert "1 follow-up due" in triggers(gary, since=NOW).describe()


def test_an_expiring_approval_wakes_gary(gary, clock):
    """Approvals expire by age, so one nearly that old is worth a look."""
    from gary.policy import APPROVAL_EXPIRY_HOURS

    nearly_expired = iso(START - dt.timedelta(hours=APPROVAL_EXPIRY_HOURS - 1))
    with gary.db.transaction() as conn:
        Repositories.bind(conn).approvals.create(
            action_type="send_external_email",
            summary="Email the sponsor",
            payload={"to": "sponsor@example.com"},
            risk_level="yellow",
            now=nearly_expired,
        )
    assert "about to expire" in triggers(gary, since=NOW).describe()


def ask_and_say(gary, text, created_at=NOW):
    """A question Gary has already put to Alex out loud."""
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        message = repos.spoken.create(
            text=text, kind="question", expects_reply=True, now=created_at
        )
        repos.spoken.mark_spoken(message["id"], created_at)
    return message["id"]


def test_an_answered_question_wakes_gary_once(gary):
    """Alex's own answer is the one input Gary cannot get any other way."""
    message_id = ask_and_say(gary, "Shall I close the newsletter project?")
    with gary.db.transaction() as conn:
        Repositories.bind(conn).spoken.answer(message_id, "No, keep it open", LATER)

    assert "1 question answered" in triggers(gary, since=NOW).describe()
    # And not again on the next tick, once the cycle has looked.
    assert not triggers(gary, since=LATER)


def test_a_question_about_to_expire_wakes_gary(gary):
    from gary.policy import APPROVAL_EXPIRY_HOURS

    nearly_expired = iso(START - dt.timedelta(hours=APPROVAL_EXPIRY_HOURS - 1))
    ask_and_say(gary, "Shall I close the newsletter project?", created_at=nearly_expired)

    assert "1 question about to expire" in triggers(gary, since=NOW).describe()


def test_a_notice_nobody_owes_an_answer_to_does_not_wake_gary(gary):
    from gary.policy import APPROVAL_EXPIRY_HOURS

    nearly_expired = iso(START - dt.timedelta(hours=APPROVAL_EXPIRY_HOURS - 1))
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        message = repos.spoken.create(
            text="The engineering sync finished without errors.",
            kind="notice",
            expects_reply=False,
            now=nearly_expired,
        )
        repos.spoken.mark_spoken(message["id"], nearly_expired)

    assert not triggers(gary, since=NOW)


def test_completed_since_counts_only_new_reports():
    finished_early = {"status": "completed", "report": {"summary": "x"},
                      "completed_at": iso(START - dt.timedelta(hours=1))}
    finished_now = {"status": "completed", "report": {"summary": "y"}, "completed_at": LATER}
    still_running = {"status": "running", "report": None, "completed_at": None}

    class Team:
        def list_assignments(self, limit=25):
            return [finished_early, finished_now, still_running]

    assert completed_since(Team(), since=NOW) == 1
    assert completed_since(Team(), since=None) == 2
    assert completed_since(None, since=NOW) == 0


def test_a_broken_team_does_not_stop_the_loop():
    class Broken:
        def list_assignments(self, limit=25):
            raise RuntimeError("agent service down")

    assert completed_since(Broken(), since=NOW) == 0


def test_daily_budget_caps_unattended_thinking_and_resets():
    budget = DailyBudget(max_cycles_per_day=2, timezone=dt.timezone.utc)
    day = dt.datetime(2026, 9, 16, 9, tzinfo=dt.timezone.utc)

    assert budget.take(day) is True
    assert budget.take(day.replace(hour=11)) is True
    assert budget.take(day.replace(hour=13)) is False  # ceiling reached
    assert budget.remaining(day.replace(hour=23)) == 0

    tomorrow = day + dt.timedelta(days=1)
    assert budget.remaining(tomorrow) == 2
    assert budget.take(tomorrow) is True
    assert budget.used_today == 1


def test_skipped_triggers_are_not_actionable():
    result = Triggers(reasons=["a report came back"], skipped="daily ceiling reached")
    assert not result


def test_a_management_cycle_can_be_recorded(gary):
    """The loop's own cycle type must be accepted by the service and the
    database, or every trigger fails at the first step."""
    context = gary.planning.get_planning_context("management")
    run_id = context["planning_run_id"]
    gary.planning.complete_cycle(run_id, {"summary": "Nothing needed doing.", "results": []})

    with gary.db.read() as conn:
        row = conn.execute(
            "SELECT planning_type, status FROM planning_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert (row["planning_type"], row["status"]) == ("management", "completed")

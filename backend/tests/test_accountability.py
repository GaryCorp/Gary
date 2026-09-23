"""Gary managing Alex's day: the morning assignment and the evening check-in.

The clock starts on Wednesday 2026-09-16 at 09:00 Chicago (conftest.START).
"""

import pytest

from conftest import make_project, make_task, run
from gary.models.action import EmailPrincipalPayload, ProposeActionRequest
from gary.models.task import CompleteTaskRequest, DependencyRequest
from gary.policy import ACTION_POLICIES, GREEN
from gary.services.accountability import Accountability


def manager(gary, clock) -> Accountability:
    return Accountability(gary.db, gary.actions, gary.timezone, gary.week, clock)


def events(gary, event_type: str) -> list[dict]:
    with gary.db.read() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM audit_log WHERE event_type = ? ORDER BY id", (event_type,)
            )
        ]


def schedule_today(gary, task, start_hour: int, end_hour: int):
    result = run(gary.actions.propose(ProposeActionRequest(
        action_type="schedule_task",
        payload={
            "task_id": task["id"],
            "start": f"2026-09-16T{start_hour:02d}:00:00-05:00",
            "end": f"2026-09-16T{end_hour:02d}:00:00-05:00",
        },
    )))
    assert result["status"] == "succeeded"


@pytest.fixture
def day(gary):
    """A booked block, three ready tasks (one of them overdue), and one due
    tomorrow that cannot start yet."""
    project = make_project(gary, name="Video 1")
    booked = make_task(gary, title="Video 1: Edit", project_id=project["id"], priority=7)
    schedule_today(gary, booked, 13, 17)
    urgent = make_task(
        gary, title="Video 1: Thumbnail and title", project_id=project["id"], priority=6,
        deadline="2026-09-17T17:00:00-05:00",
    )
    other = make_task(gary, title="Reply to the sponsor", priority=5)
    late = make_task(gary, title="Send the invoice", deadline="2026-09-15T17:00:00-05:00")
    blocked = make_task(
        gary, title="Video 1: Publish", project_id=project["id"],
        deadline="2026-09-17T17:00:00-05:00",
    )
    gary.tasks.add_dependency(DependencyRequest(task_id=blocked["id"], depends_on_task_id=urgent["id"]))
    return {"booked": booked, "urgent": urgent, "other": other, "late": late, "blocked": blocked}


def test_the_morning_assignment_is_spoken_emailed_and_recorded_once(gary, clock, external, day):
    given = run(manager(gary, clock).morning())

    assert given["email_status"] == "succeeded"
    spoken = given["spoken"]
    assert "Video 1: Edit at 1:00 PM" in spoken
    # The overdue invoice is ready, so it is a priority, and said only once.
    assert "Your priorities: Send the invoice" in spoken
    assert "Video 1: Thumbnail and title" in spoken
    assert spoken.count("Send the invoice") == 1
    assert "Already overdue" not in spoken
    assert "Due soon and not started: Video 1: Publish by Thursday 5:00 PM" in spoken

    ((subject, body),) = external.principal_emails
    assert subject == "Today's assignment: Wednesday September 16"
    assert "1:00 PM-5:00 PM  Video 1: Edit" in body
    assert "close its GitHub issue" in body

    assert day["booked"]["id"] in given["assigned"]
    assert day["blocked"]["id"] not in given["assigned"]  # a warning, not an assignment
    assert [e["entity_id"] for e in events(gary, "daily_assignment_given")] == ["2026-09-16"]

    # Once a day: no second email, nothing to say.
    assert run(manager(gary, clock).morning()) is None
    assert len(external.principal_emails) == 1


def test_an_empty_day_says_nothing_and_sends_nothing(gary, clock, external):
    assert run(manager(gary, clock).morning()) is None
    assert external.principal_emails == []
    assert events(gary, "daily_assignment_given") == []


def test_a_failed_email_is_recorded_and_the_assignment_still_spoken(gary, clock, external, day):
    external.fail_next = "Gmail is unavailable"
    given = run(manager(gary, clock).morning())

    assert given["email_status"] == "failed"
    assert given["email_error"] == "Gmail is unavailable"
    assert given["spoken"].startswith("Good morning Alex.")
    with gary.db.read() as conn:
        status = conn.execute(
            "SELECT status FROM actions WHERE action_type = 'email_principal'"
        ).fetchone()[0]
    assert status == "failed"


def test_the_weekend_hands_out_only_what_is_booked(gary, clock, day):
    clock.advance(days=3)  # Saturday the 19th
    assignment = manager(gary, clock).assignment()
    assert assignment["focus"] == [] and assignment["at_risk"] == []


def test_the_evening_check_in_asks_about_what_is_not_done(gary, clock, day):
    boss = manager(gary, clock)
    given = run(boss.morning())
    gary.tasks.complete_task(CompleteTaskRequest(task_id=day["booked"]["id"]))

    clock.advance(hours=9)  # 18:00
    checkin = boss.evening()
    assert checkin["question"] is True
    assert f"1 of {len(given['assigned'])} done today" in checkin["spoken"]
    assert "Video 1: Edit" not in checkin["spoken"]
    assert "Video 1: Thumbnail and title" in checkin["spoken"]
    assert boss.evening() is None  # once a day

    summary = boss.week_summary()
    assert summary == {
        "days_checked_in": 1,
        "assigned": len(given["assigned"]),
        "done_same_day": 1,
        "overdue_now": 1,  # the invoice, still not sent
    }


def test_everything_done_is_a_notice_not_a_question(gary, clock, day):
    boss = manager(gary, clock)
    for task_id in run(boss.morning())["assigned"]:
        gary.tasks.complete_task(CompleteTaskRequest(task_id=task_id))
    checkin = boss.evening()
    assert checkin == {
        "spoken": "Everything I gave you today is done. Good work.",
        "question": False,
        "open": [],
    }


def test_no_assignment_means_no_check_in(gary, clock):
    assert manager(gary, clock).evening() is None


def test_email_to_alex_cannot_be_redirected():
    assert ACTION_POLICIES["email_principal"] == GREEN
    with pytest.raises(ValueError):
        EmailPrincipalPayload(subject="Hi", body="Hello", to="someone@example.com")

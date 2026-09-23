"""Pausing the company by email, and the daily report.

Alex mails "pause" to Gary's own address from anywhere and every unattended
loop stops; "resume" starts them again. Email can do nothing else: the checks
here are the whole reason it is safe to act on a message at all.
"""

import pytest

from gary.db.repositories import Repositories
from gary.models.task import CompleteTaskRequest, CreateTaskRequest
from gary.services.daily_report import DailyReport
from gary.services.operating import (
    OperatingState,
    authenticated_sender,
    parse_command,
)

ALEX = "mainalextaylor@gmail.com"
GMAIL_PASS = "mx.google.com; spf=pass smtp.mailfrom=gmail.com; dkim=pass header.i=@gmail.com"


def message(**overrides) -> dict:
    data = {
        "id": "msg-1",
        "internal_date_ms": 1_800_000_000_000,
        "from": ALEX,
        "subject": "pause",
        "body": "",
        "auth_results": GMAIL_PASS,
    }
    data.update(overrides)
    return data


def state(gary, clock) -> OperatingState:
    return OperatingState(gary.db, clock)


def audit(gary) -> list[str]:
    with gary.db.read() as conn:
        return [
            r[0] for r in conn.execute(
                "SELECT event_type FROM audit_log WHERE entity_id = 'operating_state' ORDER BY id"
            )
        ]


@pytest.mark.parametrize(
    "subject, body, expected",
    [
        ("pause", "", "pause"),
        ("Pause", "", "pause"),
        ("STOP", "", "pause"),
        ("", "resume\n\nsent from my phone", "resume"),
        ("Re: GaryCorp daily report", "Pause.", "pause"),
        ("", "> pause", "pause"),
        # Not commands: the word has to stand alone, on the subject or the
        # first line, or Gary would obey a sentence about pausing.
        ("should we pause the Friday video?", "", None),
        ("", "I had to pause filming today, the light went", None),
        ("", "thanks!\npause", None),
        ("", "", None),
    ],
)
def test_only_the_word_itself_is_a_command(subject, body, expected):
    assert parse_command(subject, body) == expected


@pytest.mark.parametrize(
    "sender, auth, allowed",
    [
        (ALEX, GMAIL_PASS, True),
        (ALEX.upper(), GMAIL_PASS, True),
        # A forged From is not enough: Gmail's own result has to pass.
        (ALEX, "mx.google.com; spf=fail smtp.mailfrom=gmail.com; dkim=fail", False),
        (ALEX, "", False),
        # Passing for some other domain says nothing about this sender.
        (ALEX, "mx.google.com; spf=pass smtp.mailfrom=evil.example", False),
        ("someone.else@gmail.com", GMAIL_PASS, False),
        ("", GMAIL_PASS, False),
    ],
)
def test_a_command_must_come_from_alexs_verified_address(sender, auth, allowed):
    assert authenticated_sender(sender, ALEX, auth) is allowed


def test_pause_and_resume_by_email_are_recorded(gary, clock):
    operating = state(gary, clock)
    assert operating.is_paused() is False

    assert operating.apply_command(message(), ALEX)["applied"] == "pause"
    assert operating.is_paused() is True
    assert operating.state()["changed_by"] == "alex"

    resumed = operating.apply_command(
        message(id="msg-2", internal_date_ms=1_800_000_060_000, subject="resume"), ALEX
    )
    assert resumed["applied"] == "resume"
    assert operating.is_paused() is False
    assert audit(gary) == ["company_paused", "company_resumed"]


def test_the_same_message_cannot_pause_twice(gary, clock):
    operating = state(gary, clock)
    command = message()
    operating.apply_command(command, ALEX)
    operating.set_paused(False, actor="alex")

    # Re-reading the inbox (or a restart) must not re-apply it.
    assert operating.apply_command(command, ALEX)["refused"] == "already read"
    assert operating.is_paused() is False


def test_an_unverified_sender_is_ignored_and_recorded(gary, clock):
    operating = state(gary, clock)
    outcome = operating.apply_command(message(auth_results="spf=fail"), ALEX)

    assert outcome["refused"] == "sender not verified"
    assert operating.is_paused() is False
    assert audit(gary) == ["email_command_refused"]
    # Stepped over, so it is not re-examined every poll.
    assert operating.cursor_ms() == 1_800_000_000_000


def test_ordinary_mail_is_stepped_over_quietly(gary, clock):
    operating = state(gary, clock)
    outcome = operating.apply_command(
        message(subject="Re: sponsorship", body="Are you free Thursday?"), ALEX
    )
    assert outcome["refused"] == "not a command"
    assert audit(gary) == []
    assert operating.cursor_ms() == 1_800_000_000_000


def test_pausing_twice_changes_nothing_the_second_time(gary, clock):
    operating = state(gary, clock)
    assert operating.set_paused(True, reason="first")["changed"] is True
    assert operating.set_paused(True, reason="again")["changed"] is False
    assert audit(gary) == ["company_paused"]


def test_the_state_survives_a_restart(db_path, clock, external):
    from gary import build_gary
    from conftest import fake_handlers

    first = build_gary(db_path, "America/Chicago", action_handlers=fake_handlers(external), clock=clock)
    OperatingState(first.db, clock).set_paused(True, reason="Alex emailed pause")

    second = build_gary(db_path, "America/Chicago", action_handlers=fake_handlers(external), clock=clock)
    assert OperatingState(second.db, clock).is_paused() is True


def test_the_daily_report_says_what_needs_alex(gary, clock, external):
    done = gary.tasks.create_task(CreateTaskRequest(title="Video 1: Script"))
    gary.tasks.complete_task(CompleteTaskRequest(task_id=done["id"]))
    gary.conversation.announce("Should I move the shoot?", expects_reply=True, kind="question")

    report = DailyReport(gary.db, gary.timezone, operating=OperatingState(gary.db, clock), clock=clock)
    data = report.collect()
    email = report.render_email(data)

    assert email["subject"].startswith("GaryCorp daily report: Wednesday September 16")
    assert "Video 1: Script" in email["body"]
    assert "Question unanswered: Should I move the shoot?" in email["body"]
    assert 'Reply "pause"' in email["body"]
    assert "1 task finished" in report.spoken_summary(data)
    assert "One thing needs you" in report.spoken_summary(data)


def test_a_paused_company_says_so_at_the_top_of_the_report(gary, clock):
    operating = OperatingState(gary.db, clock)
    operating.set_paused(True, reason="Alex emailed pause")
    report = DailyReport(gary.db, gary.timezone, operating=operating, clock=clock)

    body = report.render_email(report.collect())["body"]
    assert body.splitlines()[2].startswith("PAUSED.")


def test_the_report_is_sent_once_a_day(gary, clock):
    report = DailyReport(gary.db, gary.timezone, clock=clock)
    assert report.sent_today() is False
    report.record_sent("succeeded")
    assert report.sent_today() is True

    clock.advance(days=1)
    assert report.sent_today() is False


def test_unmeasured_spending_is_never_reported_as_zero(gary, clock):
    class Unpriced:
        def spent_today(self):
            return {"cost_usd": 0, "calls": 3, "unpriced_calls": 3}

    report = DailyReport(gary.db, gary.timezone, usage=Unpriced(), clock=clock)
    body = report.render_email(report.collect())["body"]
    assert "Spending could not be measured today" in body
    assert "$0.00 spent" not in body


def test_mail_already_in_the_inbox_is_never_a_command(gary, clock):
    """Turning the channel on must not act on last week's "stop"."""
    operating = state(gary, clock)
    now_ms = int(clock().timestamp() * 1000)
    assert operating.start_from_now(now_ms) is True

    old = message(internal_date_ms=now_ms - 60_000, subject="stop")
    assert operating.apply_command(old, ALEX)["refused"] == "already read"
    assert operating.is_paused() is False

    # A command sent afterwards still works, and seeding only happens once.
    assert operating.start_from_now(now_ms + 1) is False
    assert operating.apply_command(
        message(id="msg-new", internal_date_ms=now_ms + 1_000), ALEX
    )["applied"] == "pause"

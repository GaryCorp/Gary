import json

import pytest

from gary.models.action import ProposeActionRequest
from gary.models.task import UpdateTaskRequest
from gary.policy import ACTION_POLICIES, GREEN, RED, YELLOW, escalate

from conftest import audit_events, make_task, run

EMAIL = {"to": "sam@example.com", "subject": "Draft", "body": "Here it is."}


def propose(gary, action_type, payload, **extra):
    return run(
        gary.actions.propose(
            ProposeActionRequest(action_type=action_type, payload=payload, reason="test", **extra)
        )
    )


def action_row(gary, action_id):
    with gary.db.read() as conn:
        return dict(conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone())


def test_policy_is_code_and_escalation_only_raises():
    assert ACTION_POLICIES["send_external_email"] == YELLOW
    assert ACTION_POLICIES["spend_money"] == RED
    assert escalate(GREEN, YELLOW) == YELLOW
    assert escalate(YELLOW, GREEN) == YELLOW
    assert escalate(YELLOW, None) == YELLOW


def test_green_action_executes_immediately(gary, external):
    task = make_task(gary)
    result = propose(
        gary,
        "schedule_task",
        {"task_id": task["id"], "start": "2026-09-17T13:00:00-05:00", "end": "2026-09-17T15:00:00-05:00"},
    )
    assert result["status"] == "succeeded"
    assert result["risk_level"] == GREEN
    assert list(external.events) == ["event-1"]

    updated = gary.tasks.get_task(task["id"])
    assert updated["status"] == "scheduled"
    assert updated["calendar_event_id"] == "event-1"
    assert updated["scheduled_start"] == "2026-09-17T18:00:00+00:00"

    row = action_row(gary, result["action_id"])
    assert row["status"] == "succeeded"
    assert json.loads(row["result_json"]) == {"event_id": "event-1"}
    assert audit_events(gary, "action", row["id"]) == ["action_proposed", "action_succeeded"]


def test_internal_green_action(gary):
    task = make_task(gary)
    result = propose(gary, "update_internal_task", {"task_id": task["id"], "priority": 2})
    assert result["status"] == "succeeded"
    assert gary.tasks.get_task(task["id"])["priority"] == 2

    created = propose(gary, "create_internal_task", {"title": "Create thumbnail"})
    assert created["result"]["title"] == "Create thumbnail"


def test_yellow_action_requires_approval_then_executes(gary, external):
    result = propose(gary, "send_external_email", EMAIL)
    assert result["status"] == "awaiting_approval"
    assert result["risk_level"] == YELLOW
    assert external.sent_emails == []  # nothing sent before approval

    pending = gary.approvals.list_pending()
    assert [a["id"] for a in pending] == [result["approval_id"]]
    assert pending[0]["summary"] == "Email sam@example.com: Draft"
    assert pending[0]["action_id"] == result["action_id"]

    resolved = run(gary.approvals.resolve(result["approval_id"], "approved", channel="web"))
    assert resolved["execution"]["status"] == "succeeded"
    assert external.sent_emails == ["sam@example.com"]
    assert gary.approvals.list_pending() == []
    assert action_row(gary, result["action_id"])["status"] == "succeeded"

    events = audit_events(gary, "approval", result["approval_id"])
    assert events == ["approval_requested", "approval_approved"]
    with gary.db.read() as conn:
        actor = conn.execute(
            "SELECT actor FROM audit_log WHERE event_type = 'approval_approved'"
        ).fetchone()["actor"]
    assert actor == "alex"

    with pytest.raises(ValueError, match="already approved"):
        run(gary.approvals.resolve(result["approval_id"], "approved"))
    assert external.sent_emails == ["sam@example.com"]  # never sent twice


def test_rejected_approval_does_not_execute(gary, external):
    result = propose(gary, "send_external_email", EMAIL)
    resolved = run(gary.approvals.resolve(result["approval_id"], "rejected", note="Not yet"))
    assert "execution" not in resolved
    assert external.sent_emails == []
    assert action_row(gary, result["action_id"])["status"] == "rejected"

    with pytest.raises(ValueError, match="not approved"):
        run(gary.actions.execute(result["action_id"]))


def test_red_action_rejected_without_approval(gary):
    result = propose(gary, "spend_money", {"amount": 500})
    assert result["status"] == "rejected"
    assert result["risk_level"] == RED
    assert gary.approvals.list_pending() == []
    assert action_row(gary, result["action_id"])["status"] == "rejected"
    assert audit_events(gary, "action", result["action_id"]) == ["action_rejected_by_policy"]


def test_gary_cannot_choose_risk_level(gary):
    with pytest.raises(ValueError, match="Extra inputs"):
        ProposeActionRequest.model_validate(
            {"action_type": "send_external_email", "payload": EMAIL, "risk_level": "green"}
        )
    # A risk_level smuggled into the payload is rejected by the payload model.
    with pytest.raises(ValueError, match="Extra inputs"):
        propose(gary, "send_external_email", {**EMAIL, "risk_level": "green"})


def test_unknown_and_unimplemented_actions_rejected(gary):
    with pytest.raises(ValueError, match="Unsupported"):
        propose(gary, "delete_database", {})
    with pytest.raises(ValueError, match="Unsupported"):
        propose(gary, "download_file", {"url": "https://example.com"})


def test_invalid_payload_rejected_before_database(gary):
    with pytest.raises(ValueError, match="to"):
        propose(gary, "send_external_email", {**EMAIL, "to": "a@b.com, c@d.com"})
    with pytest.raises(ValueError, match="end must be later"):
        task = make_task(gary)
        propose(
            gary,
            "schedule_task",
            {"task_id": task["id"], "start": "2026-09-17T15:00:00Z", "end": "2026-09-17T13:00:00Z"},
        )
    with gary.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_action_failure_recorded_honestly(gary, external):
    task = make_task(gary)
    external.fail_next = "Google Calendar timeout"
    result = propose(
        gary,
        "schedule_task",
        {"task_id": task["id"], "start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:00:00Z"},
    )
    assert result["status"] == "failed"
    assert "timeout" in result["error"]

    row = action_row(gary, result["action_id"])
    assert row["status"] == "failed"
    assert row["error_message"] == "Google Calendar timeout"
    # SQLite does not pretend the calendar change happened.
    task_after = gary.tasks.get_task(task["id"])
    assert task_after["calendar_event_id"] is None
    assert task_after["status"] == "todo"
    assert audit_events(gary, "action", row["id"])[-1] == "action_failed"


def test_state_change_between_approval_and_execution_fails_safely(gary, external):
    task = make_task(gary)
    handlers = gary.actions.handlers
    # Make schedule_task yellow for this test by classifying it critical.
    from dataclasses import replace

    handlers["schedule_task"] = replace(handlers["schedule_task"], classify=lambda r, p: YELLOW)
    result = propose(
        gary,
        "schedule_task",
        {"task_id": task["id"], "start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:00:00Z"},
    )
    assert result["status"] == "awaiting_approval"

    with gary.db.transaction() as conn:
        conn.execute("UPDATE tasks SET calendar_event_id = 'set-elsewhere' WHERE id = ?", (task["id"],))

    resolved = run(gary.approvals.resolve(result["approval_id"], "approved"))
    assert resolved["execution"]["status"] == "failed"
    assert external.events == {}


def test_recording_failure_after_external_success(gary, external):
    from dataclasses import replace

    def broken_record(repos, payload, result, now):
        raise RuntimeError("disk full")

    handlers = gary.actions.handlers
    handlers["schedule_task"] = replace(handlers["schedule_task"], record=broken_record)
    task = make_task(gary)
    result = propose(
        gary,
        "schedule_task",
        {"task_id": task["id"], "start": "2026-09-17T13:00:00Z", "end": "2026-09-17T14:00:00Z"},
    )
    assert result["status"] == "failed"
    row = action_row(gary, result["action_id"])
    assert "ran but recording" in row["error_message"]
    assert json.loads(row["result_json"]) == {"event_id": "event-1"}


def test_pending_approvals_expire(gary, clock, external):
    result = propose(gary, "send_external_email", EMAIL)
    clock.advance(hours=73)
    assert gary.approvals.list_pending() == []
    assert action_row(gary, result["action_id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="already expired"):
        run(gary.approvals.resolve(result["approval_id"], "approved"))
    assert external.sent_emails == []


def test_update_internal_task_cannot_complete(gary):
    task = make_task(gary)
    with pytest.raises(ValueError, match="status"):
        propose(gary, "update_internal_task", {"task_id": task["id"], "status": "completed"})
    assert UpdateTaskRequest  # imported model is the payload model

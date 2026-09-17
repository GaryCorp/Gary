"""Chief of Staff behavior: working time, briefs, missed blocks, commitment
changes, email and follow-ups in planning, the new tools, and the end-to-end
"publish the video by Friday" scenario."""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from gary import build_gary
from gary.models.action import ProposeActionRequest
from gary.models.commitment import CreateCommitmentRequest
from gary.models.followup import CreateFollowupRequest
from gary.models.task import CompleteTaskRequest, DependencyRequest
from gary.services.calendar_blocks import (
    WorkHours,
    WorkWeek,
    find_free_blocks,
    parse_protected_times,
    protected_intervals,
    working_minutes,
    working_time_problem,
)
from gary.services.planning_service import RecordPlanRequest
from gary.services.planning_cycle import PlanningCycleError
from gary.services.readiness import calculate_task_score
from gary.tools import ToolContext, call_tool

from conftest import audit_events, fake_handlers, make_project, make_task, run
from test_planning_cycle import FakeCalendar, FakeEmail, FakeNotebook, FakePlanner, cycle

CHICAGO = ZoneInfo("America/Chicago")
LUNCH = WorkWeek(WorkHours(9, 17), frozenset({0, 1, 2, 3, 4}), parse_protected_times("12:00-13:00"))
# conftest START: Wednesday 2026-09-16 09:00 in Chicago (14:00 UTC).
WED_9 = "2026-09-16T14:00:00+00:00"


def local(value: str) -> str:
    return dt.datetime.fromisoformat(value).astimezone(CHICAGO).strftime("%a %H:%M")


def call(ctx, tool_name, **arguments):
    return run(call_tool(tool_name, arguments, ctx))


def schedule(gary, task_id, start, end):
    return run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="schedule_task",
                payload={"task_id": task_id, "start": start, "end": end},
                reason="test",
            )
        )
    )


@pytest.fixture
def lunch_gary(db_path, clock, external):
    return build_gary(
        db_path, "America/Chicago", action_handlers=fake_handlers(external), clock=clock, work_week=LUNCH
    )


# ------------------------------------------------------------ working time

def test_protected_times_parse_and_validate():
    assert parse_protected_times("15:00-15:15, 12:00-13:00") == (
        (dt.time(12), dt.time(13)),
        (dt.time(15), dt.time(15, 15)),
    )
    assert parse_protected_times("") == ()
    for bad in ("noon-1pm", "13:00-12:00"):
        with pytest.raises(ValueError):
            parse_protected_times(bad)


def test_free_blocks_skip_busy_lunch_and_weekend():
    busy = [{"start": "2026-09-16T15:00:00+00:00", "end": "2026-09-16T16:30:00+00:00"}]  # 10:00-11:30
    blocks = find_free_blocks(WED_9, "2026-09-19T05:00:00+00:00", busy, LUNCH, CHICAGO, min_minutes=45)
    assert [(local(b["start"]), local(b["end"]), b["minutes"]) for b in blocks] == [
        ("Wed 09:00", "Wed 10:00", 60),
        # 11:30-12:00 is free but shorter than 45 minutes; 12:00-13:00 is lunch.
        ("Wed 13:00", "Wed 17:00", 240),
        ("Thu 09:00", "Thu 12:00", 180),
        ("Thu 13:00", "Thu 17:00", 240),
        ("Fri 09:00", "Fri 12:00", 180),
        ("Fri 13:00", "Fri 17:00", 240),
    ]
    saturday = "2026-09-19T14:00:00+00:00"
    assert find_free_blocks(saturday, "2026-09-20T23:00:00+00:00", [], LUNCH, CHICAGO) == []


def test_working_minutes_until_deadline():
    friday_5pm = "2026-09-18T22:00:00+00:00"
    assert working_minutes(WED_9, friday_5pm, LUNCH, CHICAGO) == 3 * 7 * 60
    assert working_minutes(friday_5pm, WED_9, LUNCH, CHICAGO) == 0
    assert protected_intervals(WED_9, "2026-09-16T23:00:00+00:00", LUNCH, CHICAGO) == [
        ("2026-09-16T17:00:00+00:00", "2026-09-16T18:00:00+00:00")
    ]


def test_working_time_problem():
    def problem(start, end):
        return working_time_problem(start, end, LUNCH, CHICAGO)

    assert problem("2026-09-17T09:00:00-05:00", "2026-09-17T11:00:00-05:00") is None
    assert problem("2026-09-17T10:30:00-05:00", "2026-09-17T13:30:00-05:00") == (
        "it overlaps protected time (12:00-13:00)"
    )
    assert problem("2026-09-17T16:30:00-05:00", "2026-09-17T17:30:00-05:00").startswith(
        "it is outside working hours (09:00-17:00"
    )
    assert problem("2026-09-19T10:00:00-05:00", "2026-09-19T11:00:00-05:00").startswith(
        "it is outside working hours"
    )


def test_payloads_accept_override_flag():
    from gary.models.action import ScheduleTaskPayload
    from gary.models.common import validate_request

    task_id = "7d0f4d1c-7e2b-4c55-9d1c-0b1e7f0e9a11"
    payload = validate_request(ScheduleTaskPayload, {
        "task_id": task_id, "start": "2026-09-19T10:00:00-05:00", "end": "2026-09-19T11:00:00-05:00",
    })
    assert payload.override_working_hours is False


def test_record_plan_without_prior_context(gary):
    run_row = gary.planning.record_plan(RecordPlanRequest(summary="Test Thursday morning, then film."))
    assert run_row["status"] == "completed"
    assert run_row["planning_type"] == "manual"


# ----------------------------------------------------------------- priority

def test_project_priority_is_a_scoring_factor(gary):
    task = {"priority": 5, "deadline": None, "status": "todo"}
    assert calculate_task_score(task, WED_9, False, False, project_priority=10) == 60
    assert calculate_task_score(task, WED_9, False, False, project_priority=5) == 50
    assert calculate_task_score(task, WED_9, False, False, project_priority=1) == 42

    important = make_project(gary, name="Important", priority=10)
    minor = make_project(gary, name="Minor", priority=2)
    make_task(gary, title="Minor task", project_id=minor["id"])
    make_task(gary, title="Important task", project_id=important["id"])
    ready = gary.planning.get_planning_context()["ready_tasks"]
    assert [(t["title"], t["planning_score"]) for t in ready] == [
        ("Important task", 60),
        ("Minor task", 44),
    ]


# --------------------------------------------------- one-call project setup

def test_project_create_with_tasks_in_one_transaction(gary):
    ctx = ToolContext(gary, {})
    result = call(
        ctx, "project_create_with_tasks", name="Publish next video", objective="Video live",
        priority=9, deadline="2026-09-18T17:00:00-05:00",
        tasks=[
            {"title": "Finish software", "estimated_minutes": 180, "priority": 9},
            {"title": "Test Gary", "estimated_minutes": 120, "priority": 9},
            {"title": "Film demo", "estimated_minutes": 120, "depends_on": ["finish software", "Test Gary"]},
            {"title": "Edit video", "estimated_minutes": 480, "depends_on": ["Film demo"]},
            {"title": "Upload", "estimated_minutes": 30, "depends_on": ["Edit video"]},
        ],
    )
    assert result["success"] is True
    assert result["ready_now"] == ["Finish software", "Test Gary"]
    blocked = {t["title"]: t["blocked_by"] for t in result["tasks"] if not t["ready"]}
    assert blocked["Film demo"] == ["Finish software", "Test Gary"]
    project_id = result["project"]["id"]
    assert audit_events(gary, "project", project_id) == ["project_created"]
    with gary.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 4


def test_project_create_with_tasks_rolls_back_on_error(gary):
    ctx = ToolContext(gary, {})
    cyclic = call(
        ctx, "project_create_with_tasks", name="Loop", objective="x",
        tasks=[{"title": "A", "depends_on": ["B"]}, {"title": "B", "depends_on": ["A"]}],
    )
    assert cyclic["success"] is False and "Circular dependency" in cyclic["error"]
    unknown = call(ctx, "project_create_with_tasks", name="Typo", objective="x",
                   tasks=[{"title": "A", "depends_on": ["Filming"]}])
    assert unknown["success"] is False and "unknown task 'Filming'" in unknown["error"]
    duplicate = call(ctx, "project_create_with_tasks", name="Dup", objective="x",
                     tasks=[{"title": "A"}, {"title": " a "}])
    assert duplicate["success"] is False and "unique" in duplicate["error"]
    with gary.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


# ----------------------------------------------------- missed calendar blocks

def test_missed_block_detected_with_downstream_and_reported_once(gary, clock):
    film = make_task(gary, title="Film demo", priority=9)
    edit = make_task(gary, title="Edit video")
    upload = make_task(gary, title="Upload video")
    gary.tasks.add_dependency(DependencyRequest(task_id=edit["id"], depends_on_task_id=film["id"]))
    gary.tasks.add_dependency(DependencyRequest(task_id=upload["id"], depends_on_task_id=edit["id"]))
    schedule(gary, film["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T15:00:00-05:00")

    assert gary.planning.snapshot()["missed_scheduled_blocks"] == []
    clock.advance(hours=6, minutes=30)  # 15:30 local, filming still not done

    missed = gary.planning.snapshot()["missed_scheduled_blocks"]
    assert [(m["title"], m["affects"]) for m in missed] == [("Film demo", ["Edit video", "Upload video"])]

    alerts = gary.planning.collect_new_alerts()
    assert [(b["title"], b["affects"]) for b in alerts["missed_blocks"]] == [
        ("Film demo", ["Edit video", "Upload video"])
    ]
    assert gary.planning.collect_new_alerts()["missed_blocks"] == []

    # Rescheduled and missed again: reported again.
    run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="move_calendar_event",
                payload={
                    "task_id": film["id"],
                    "new_start": "2026-09-16T16:00:00-05:00",
                    "new_end": "2026-09-16T17:00:00-05:00",
                },
                reason="test",
            )
        )
    )
    # Priority 9 makes that move critical, so approve it first.
    approval = gary.approvals.list_pending()[0]
    run(gary.approvals.resolve(approval["id"], "approved"))
    clock.advance(hours=2)
    assert [b["title"] for b in gary.planning.collect_new_alerts()["missed_blocks"]] == ["Film demo"]

    gary.tasks.complete_task(CompleteTaskRequest(task_id=film["id"]))
    assert gary.planning.snapshot()["missed_scheduled_blocks"] == []


# ------------------------------------------------------------------- briefs

def test_morning_brief_facts(lunch_gary):
    gary = lunch_gary
    project = make_project(
        gary, name="Publish video", priority=9, deadline="2026-09-18T17:00:00-05:00"
    )
    test = make_task(gary, title="Test Gary", project_id=project["id"], priority=9, estimated_minutes=120)
    film = make_task(gary, title="Film demo", project_id=project["id"], priority=8, estimated_minutes=120)
    gary.tasks.add_dependency(DependencyRequest(task_id=film["id"], depends_on_task_id=test["id"]))
    schedule(gary, test["id"], "2026-09-16T09:00:00-05:00", "2026-09-16T11:00:00-05:00")
    make_task(gary, title="Late invoice", deadline="2026-09-15T17:00:00-05:00")
    gary.commitments.create_commitment(
        CreateCommitmentRequest(description="Send Sam the draft", committed_to="Sam",
                                deadline="2026-09-17T17:00:00-05:00")
    )
    run(gary.actions.propose(ProposeActionRequest(
        action_type="send_external_email",
        payload={"to": "sam@example.com", "subject": "Draft", "body": "Soon"},
        reason="commitment",
    )))

    brief = gary.briefing.build("morning")
    objective = brief["primary_objective"]
    assert objective["project"]["name"] == "Publish video"
    assert objective["project"]["capacity"]["status"] == "on_track"
    assert objective["project"]["capacity"]["remaining_minutes"] == 240
    assert brief["today"] == [{
        "title": "Test Gary",
        "start": "2026-09-16T14:00:00+00:00",
        "end": "2026-09-16T16:00:00+00:00",
        "status": "scheduled",
    }]
    assert "Film demo is waiting on Test Gary." in brief["risks"]
    assert "Late invoice is overdue (was due Tue 5:00 PM)." in brief["risks"]
    assert "Commitment to Sam: Send Sam the draft is due Thu 5:00 PM." in brief["risks"]
    assert brief["decisions_needed"] == ["Email sam@example.com: Draft"]
    assert "completed_today" not in brief

    markdown = gary.briefing.render_markdown(brief)
    assert markdown.splitlines()[0] == "## Morning brief, 9:00 AM"
    assert "**Primary objective:** Publish video (due Fri 5:00 PM, on track); next: Test Gary" in markdown
    assert "- 9:00 AM-11:00 AM Test Gary" in markdown
    assert len(markdown.splitlines()) < 25  # concise, not a giant report


def test_capacity_at_risk(lunch_gary):
    project = make_project(lunch_gary, name="Big launch", deadline="2026-09-16T17:00:00-05:00")
    for index in range(3):
        make_task(lunch_gary, title=f"Step {index}", project_id=project["id"], estimated_minutes=180)
    brief = lunch_gary.briefing.build("morning")
    assert brief["primary_objective"]["project"]["capacity"] == {
        "status": "at_risk",
        "remaining_minutes": 540,
        "unestimated_tasks": 0,
        "available_working_minutes": 420,
    }
    assert any(r.startswith("Big launch: about 9.0 hours of work left and 7.0 working hours") for r in brief["risks"])


def test_midday_and_evening_reviews(gary, clock):
    done = make_task(gary, title="Integration testing")
    slipping = make_task(gary, title="Fix critical issues")
    optional = make_task(gary, title="Optional research", priority=3)
    for task, start, end in [
        (done, "10:00", "11:00"),
        (slipping, "11:30", "12:30"),
        (optional, "14:00", "15:00"),
    ]:
        schedule(gary, task["id"], f"2026-09-16T{start}:00-05:00", f"2026-09-16T{end}:00-05:00")
    run(cycle(gary, FakePlanner({"summary": "Test, fix, then research.", "briefing": "", "actions": []})).run("morning"))

    clock.advance(hours=4)  # 13:00 local
    gary.tasks.complete_task(CompleteTaskRequest(task_id=done["id"]))
    midday = gary.briefing.build("midday")
    assert midday["completed_today"] == ["Integration testing"]
    assert midday["unfinished_today"] == ["Fix critical issues", "Optional research"]
    assert midday["earlier_plans_today"] == [
        {"planning_type": "morning", "summary": "Test, fix, then research."}
    ]
    assert any("Fix critical issues was scheduled until" in risk for risk in midday["risks"])

    run(gary.actions.propose(ProposeActionRequest(
        action_type="move_calendar_event",
        payload={"task_id": optional["id"], "new_start": "2026-09-17T14:00:00-05:00",
                 "new_end": "2026-09-17T15:00:00-05:00"},
        reason="make room",
    )))
    gary.followups.create_followup(
        CreateFollowupRequest(title="Check fixes landed", due_at="2026-09-17T10:00:00-05:00")
    )

    clock.advance(hours=4)  # 17:00 local
    evening = gary.briefing.build("evening")
    assert evening["completed_today"] == ["Integration testing"]
    assert evening["unfinished_today"] == ["Fix critical issues"]
    assert evening["moved_today"] == ["Optional research"]
    assert evening["new_followups"] == ["Check fixes landed"]
    assert evening["tomorrow"]["scheduled"][0]["title"] == "Optional research"

    markdown = gary.briefing.render_markdown(evening, plan_summary="Finish the fixes first tomorrow.")
    for expected in (
        "## Daily summary, 5:00 PM",
        "**Completed:**\n- Integration testing",
        "**Incomplete:**\n- Fix critical issues",
        "**Moved:**\n- Optional research",
        "**New follow-ups:**\n- Check fixes landed",
        "**Plan:** Finish the fixes first tomorrow.",
        "**Tomorrow:**\n- 2:00 PM-3:00 PM Optional research",
    ):
        assert expected in markdown


# ------------------------------------------------ actions, commitments, audit

def test_create_followup_is_a_green_action(gary):
    task = make_task(gary)
    result = run(gary.actions.propose(ProposeActionRequest(
        action_type="create_followup",
        payload={"title": "Check filming", "due_at": "2026-09-16T15:00:00-05:00", "task_id": task["id"]},
        reason="checkpoint",
    )))
    assert result["status"] == "succeeded"
    assert result["risk_level"] == "green"
    followup_id = result["result"]["followup_id"]
    assert audit_events(gary, "followup", followup_id) == ["followup_created"]


def test_commitment_update_status_direct_and_terms_need_approval(gary):
    ctx = ToolContext(gary, {})
    created = call(ctx, "commitment_create", description="Send Sam the draft", committed_to="Sam",
                   deadline="2026-09-17T17:00:00-05:00")
    commitment_id = created["commitment"]["id"]

    change = call(ctx, "commitment_update", commitment_id=commitment_id,
                  deadline="2026-09-18T12:00:00-05:00", reason="Sam agreed to Friday")
    assert change["status"] == "awaiting_approval"
    assert change["risk_level"] == "yellow"
    assert change["summary"] == 'Change the commitment to Sam "Send Sam the draft" (deadline)'
    listed = call(ctx, "commitment_list")
    assert listed["commitments"][0]["deadline"] == "2026-09-17T17:00:00-05:00"  # unchanged yet

    approved = call(ctx, "approval_resolve", approval_id=change["approval_id"],
                    decision="approved", confirmed=True)
    assert approved["execution"]["status"] == "succeeded"
    assert call(ctx, "commitment_list")["commitments"][0]["deadline"] == "2026-09-18T12:00:00-05:00"
    assert "commitment_changed" in audit_events(gary, "commitment", commitment_id)

    both = call(ctx, "commitment_update", commitment_id=commitment_id, status="fulfilled",
                description="Something else")
    assert both["success"] is False
    fulfilled = call(ctx, "commitment_update", commitment_id=commitment_id, status="fulfilled")
    assert fulfilled["commitment"]["status"] == "fulfilled"
    assert call(ctx, "commitment_list")["count"] == 0
    assert call(ctx, "commitment_list", status="all")["count"] == 1


def test_red_policy_includes_permissions_and_audit_log(gary):
    for action_type in ("modify_permissions", "delete_audit_log", "change_own_permissions"):
        result = run(gary.actions.propose(ProposeActionRequest(
            action_type=action_type, payload={}, reason="please")))
        assert result["status"] == "rejected"
        assert result["risk_level"] == "red"


def test_email_sent_and_calendar_changed_audit_events(gary, external):
    task = make_task(gary)
    scheduled = schedule(gary, task["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T14:00:00-05:00")
    assert "calendar_changed" in audit_events(gary, "action", scheduled["action_id"])
    email = run(gary.actions.propose(ProposeActionRequest(
        action_type="send_external_email",
        payload={"to": "sam@example.com", "subject": "Draft", "body": "Attached"}, reason="x")))
    run(gary.approvals.resolve(email["approval_id"], "approved"))
    assert audit_events(gary, "action", email["action_id"])[-1] == "email_sent"


# ------------------------------------------------------------ planning cycle

def test_cycle_uses_email_and_creates_followups(gary):
    task = make_task(gary, title="Send Sam the draft")
    emails = [{"from": "Sam Lee", "subject": "Draft?", "snippet": "Can you send me the draft by Thursday?"}]
    planner = FakePlanner({"summary": "s", "briefing": "Sam wants the draft by Thursday.", "actions": [
        {"action_type": "create_followup", "title": "Reply to Sam about the draft",
         "due_at": "2026-09-16T11:00:00-05:00", "task_id": task["id"], "reason": "Sam asked"},
        {"action_type": "create_followup", "title": "reply to sam about the  draft",
         "due_at": "2026-09-16T12:00:00-05:00", "task_id": "", "reason": "duplicate"},
        {"action_type": "create_followup", "title": "Way later",
         "due_at": "2026-10-30T12:00:00-05:00", "task_id": "", "reason": "too far"},
    ]})
    result = run(cycle(gary, planner, email=FakeEmail(emails)).run("morning"))

    assert planner.inputs[0]["unread_email"] == emails
    assert planner.inputs[0]["brief"]["kind"] == "morning"
    assert [r["result"]["status"] for r in result["results"]] == ["succeeded"]
    assert [r["reason"] for r in result["rejected"]] == [
        "a pending follow-up with that title already exists",
        "follow-ups must be due within 7 days",
    ]
    pending = gary.followups.list_due()
    assert pending == []  # due at 11:00, not yet
    with gary.db.read() as conn:
        assert conn.execute("SELECT task_id FROM followups").fetchone()["task_id"] == task["id"]


def test_cycle_records_email_failure_and_continues(gary):
    result = run(cycle(gary, email=FakeEmail(fail=True)).run("midday"))
    with gary.db.read() as conn:
        plan = json.loads(conn.execute("SELECT plan_json FROM planning_runs WHERE id = ?",
                                       (result["planning_run_id"],)).fetchone()["plan_json"])
    assert plan["email_error"] == "Gmail access has not been granted"


def test_cycle_rejects_tiny_moves_and_protected_time(lunch_gary):
    gary = lunch_gary
    task = make_task(gary, title="Deep work")
    other = make_task(gary, title="Admin")
    schedule(gary, task["id"], "2026-09-16T14:00:00-05:00", "2026-09-16T15:00:00-05:00")
    planner = FakePlanner({"summary": "s", "briefing": "", "actions": [
        {"action_type": "move_calendar_event", "task_id": task["id"], "reason": "slip",
         "new_start": "2026-09-16T14:15:00-05:00", "new_end": "2026-09-16T15:15:00-05:00"},
        {"action_type": "schedule_task", "task_id": other["id"], "reason": "fits",
         "start": "2026-09-16T11:30:00-05:00", "end": "2026-09-16T12:30:00-05:00"},
    ]})
    result = run(cycle(gary, planner).run("midday"))
    assert [r["reason"] for r in result["rejected"]] == [
        "moves under 30 minutes are not worth changing the calendar",
        "overlaps protected time such as a meal break",
    ]
    assert planner.inputs[0]["protected_times"] == ["12:00-13:00"]


def test_cycle_rate_limit_and_event_triggered_title(gary, clock):
    notebook = FakeNotebook()
    runner = cycle(gary, notebook=notebook)
    run(runner.run("event_triggered", min_gap_minutes=120))
    assert notebook.summaries[0][1].startswith("## Replan after a change, 9:00 AM")
    with pytest.raises(PlanningCycleError, match="ran 0 minutes ago"):
        run(runner.run("event_triggered", min_gap_minutes=120))
    clock.advance(hours=3)
    run(runner.run("event_triggered", min_gap_minutes=120))
    with pytest.raises(ValueError):
        run(runner.run("sometime"))


# -------------------------------------------------------------------- tools

def test_brief_blocks_and_run_cycle_tools(lunch_gary):
    gary = lunch_gary
    ctx = ToolContext(gary, {})
    brief = call(ctx, "planning_get_brief", kind="morning")
    assert brief["success"] and brief["brief"]["kind"] == "morning"
    assert call(ctx, "planning_get_brief", kind="weekly")["success"] is False

    unavailable = call(ctx, "planning_find_work_blocks")
    assert unavailable == {"success": False, "error": "The calendar integration is not available right now"}
    assert call(ctx, "planning_run_cycle", planning_type="manual")["success"] is False

    notebook = FakeNotebook(notes=[{"source": "Planning note", "title": "Preferences",
                                    "text": "Editing usually takes two days."}])
    runner = cycle(gary, notebook=notebook)
    ctx = ToolContext(gary, {}, {"calendar": FakeCalendar(), "notebook": notebook, "planning_cycle": runner})

    context = call(ctx, "planning_get_context")
    assert context["planning_notes"][0]["text"] == "Editing usually takes two days."

    ran = call(ctx, "planning_run_cycle", planning_type="manual")
    assert ran["success"] is True
    assert ran["briefing"] == "Filming is up next."
    assert ran["summary_note_written"] is True
    again = call(ctx, "planning_run_cycle", planning_type="evening")
    assert again["success"] is False and "wait" in again["error"]

    due = call(ctx, "followup_list_due")
    assert due == {"success": True, "count": 0, "followups": []}


def test_find_work_blocks_tool_uses_calendar(lunch_gary, monkeypatch):
    import gary.tools.planning_tools as planning_tools

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 16, 14, 0, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(planning_tools.dt, "datetime", Frozen)
    busy = [{"start": "2026-09-16T14:00:00+00:00", "end": "2026-09-16T16:00:00+00:00", "event_id": "x"}]
    ctx = ToolContext(lunch_gary, {}, {"calendar": FakeCalendar(busy)})
    result = call(ctx, "planning_find_work_blocks", minutes=90)
    assert result["protected_times"] == ["12:00-13:00"]
    assert [(b["start"], b["end"]) for b in result["blocks"]] == [
        ("2026-09-16T13:00:00-05:00", "2026-09-16T17:00:00-05:00"),
    ]
    assert call(ctx, "planning_find_work_blocks", minutes=5)["success"] is False


# ------------------------------------------------------ end-to-end scenario

def test_publish_by_friday_end_to_end(db_path, clock, external, monkeypatch):
    """Alex: "Gary, I need to publish the next video by Friday." Later, filming
    runs over; Gary notices, replans, and everything survives a restart."""
    import gary.tools.planning_tools as planning_tools

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock().astimezone(tz)

    # planning_find_work_blocks reads the wall clock; pin it to the test clock.
    monkeypatch.setattr(planning_tools.dt, "datetime", Frozen)
    gary = build_gary(db_path, "America/Chicago", action_handlers=fake_handlers(external),
                      clock=clock, work_week=LUNCH)
    notebook = FakeNotebook()
    calendar = FakeCalendar()
    ctx = ToolContext(gary, {}, {"calendar": calendar, "notebook": notebook,
                                 "planning_cycle": cycle(gary, notebook=notebook, calendar=calendar)})

    # 1-5: project, tasks, dependencies, deadline, what is actionable.
    project = call(ctx, "project_create", name="Publish next video", objective="Video live on YouTube",
                   priority=9, deadline="2026-09-18T17:00:00-05:00")["project"]
    ids = {}
    for title, minutes, priority in [
        ("Test Gary", 120, 9), ("Film demo", 120, 9), ("Edit video", 240, 8),
        ("Upload video", 30, 8), ("Optional research", 60, 3),
    ]:
        ids[title] = call(ctx, "task_create", project_id=project["id"], title=title,
                          estimated_minutes=minutes, priority=priority)["task"]["id"]
    for task, prerequisite in [("Film demo", "Test Gary"), ("Edit video", "Film demo"),
                               ("Upload video", "Edit video")]:
        assert call(ctx, "task_add_dependency", task_id=ids[task], depends_on_task_id=ids[prerequisite])["success"]

    context = call(ctx, "planning_get_context")["context"]
    assert [t["title"] for t in context["ready_tasks"]] == ["Test Gary", "Optional research"]

    # 6-7: check the calendar and schedule reasonable blocks.
    blocks = call(ctx, "planning_find_work_blocks", minutes=120, start_date="2026-09-17")["blocks"]
    assert blocks[0]["start"] == "2026-09-17T09:00:00-05:00"
    for title, start, end in [("Test Gary", "2026-09-17T09:00:00-05:00", "2026-09-17T11:00:00-05:00"),
                              ("Optional research", "2026-09-16T14:00:00-05:00", "2026-09-16T15:00:00-05:00")]:
        result = call(ctx, "action_propose", action_type="schedule_task", reason="plan",
                      payload={"task_id": ids[title], "start": start, "end": end})
        assert result["status"] == "succeeded"

    # Testing is done early; filming goes on the calendar for this afternoon.
    call(ctx, "task_complete", task_id=ids["Test Gary"], actual_minutes=100)
    filming = call(ctx, "action_propose", action_type="schedule_task", reason="critical path",
                   payload={"task_id": ids["Film demo"], "start": "2026-09-16T13:00:00-05:00",
                            "end": "2026-09-16T15:00:00-05:00"})
    assert filming["status"] == "succeeded"

    # 8: follow-up on filming.
    assert call(ctx, "followup_create", title="Check whether filming finished",
                due_at="2026-09-16T15:00:00-05:00", task_id=ids["Film demo"])["success"]

    # Later: 15:30 and filming is still incomplete.
    clock.advance(hours=6, minutes=30)
    alerts = gary.planning.collect_new_alerts()
    # Optional research (2-3 PM) was not done either; only filming holds anything up.
    assert [(b["title"], b["affects"]) for b in alerts["missed_blocks"]] == [
        ("Film demo", ["Edit video", "Upload video"]),
        ("Optional research", []),
    ]
    assert [f["title"] for f in alerts["due_followups"]] == ["Check whether filming finished"]

    # Event-triggered replan: move optional research out, keep filming, which
    # is critical and needs approval to move.
    calendar.busy = [{"start": "2026-09-16T19:00:00+00:00", "end": "2026-09-16T20:00:00+00:00",
                      "event_id": "event-2"}]
    planner = FakePlanner({
        "summary": "Filming is still incomplete and blocking editing. Move optional research to tomorrow; "
                   "keep a filming block this afternoon.",
        "briefing": "Filming is still incomplete and is blocking editing. I moved optional research to "
                    "tomorrow. Friday is still achievable. No decision is required from you.",
        "actions": [
            {"action_type": "move_calendar_event", "task_id": ids["Optional research"], "reason": "make room",
             "new_start": "2026-09-17T13:00:00-05:00", "new_end": "2026-09-17T14:00:00-05:00"},
            {"action_type": "move_calendar_event", "task_id": ids["Film demo"], "reason": "finish filming",
             "new_start": "2026-09-16T15:45:00-05:00", "new_end": "2026-09-16T17:00:00-05:00"},
        ],
    })
    replan = run(cycle(gary, planner, notebook, calendar).run("event_triggered"))
    statuses = {r["proposal"]["task_title"]: r["result"]["status"] for r in replan["results"]}
    assert statuses == {"Optional research": "succeeded", "Film demo": "awaiting_approval"}
    assert gary.tasks.get_task(ids["Optional research"])["scheduled_start"] == "2026-09-17T18:00:00+00:00"
    assert "Friday is still achievable" in replan["briefing"]
    assert notebook.summaries[-1][1].startswith("## Replan after a change")

    # Restart: a fresh process knows everything from SQLite.
    restarted = build_gary(db_path, "America/Chicago", action_handlers=fake_handlers(external),
                           clock=clock, work_week=LUNCH)
    snapshot = restarted.planning.snapshot()
    assert [p["name"] for p in snapshot["active_projects"]] == ["Publish next video"]
    assert [m["title"] for m in snapshot["missed_scheduled_blocks"]] == ["Film demo"]
    assert {t["title"] for t in snapshot["blocked_tasks"]} == {"Edit video", "Upload video"}
    assert [a["summary"] for a in snapshot["pending_approvals"]] == ["Move Film demo"]
    assert {(a["action_type"], a["status"]) for a in snapshot["recent_actions"]} >= {
        ("schedule_task", "succeeded"),
        ("move_calendar_event", "succeeded"),
        ("move_calendar_event", "awaiting_approval"),
    }
    with restarted.db.read() as conn:
        completed = conn.execute("SELECT title FROM tasks WHERE status = 'completed'").fetchall()
        events = {row["event_type"] for row in conn.execute("SELECT event_type FROM audit_log")}
    assert [row["title"] for row in completed] == ["Test Gary"]
    assert {"project_created", "task_created", "dependency_added", "task_completed", "followup_created",
            "calendar_changed", "scheduled_block_missed", "approval_requested",
            "planning_cycle_completed"} <= events

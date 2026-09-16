import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from gary.models.commitment import CreateCommitmentRequest
from gary.models.task import DependencyRequest
from gary.planner import (
    PLAN_SCHEMA,
    PlannerError,
    parse_plan,
    planner_instructions,
    response_text,
)
from gary.services.planning_cycle import (
    PlanningCycle,
    PlanningCycleError,
    WorkHours,
    daily_summary_title,
    due_planning_types,
    parse_schedule,
    parse_weekdays,
    parse_work_hours,
    previous_summary,
    select_relevant_notes,
)

from conftest import make_project, make_task, run

CHICAGO = ZoneInfo("America/Chicago")
WEEKDAYS = parse_weekdays("mon,tue,wed,thu,fri")
# START in conftest is Wednesday 2026-09-16 14:00 UTC = 09:00 in Chicago.


class FakePlanner:
    def __init__(self, plan=None, error=None):
        self.plan = plan or {"summary": "Film first.", "briefing": "Filming is up next.", "actions": []}
        self.error = error
        self.inputs = []

    async def create_plan(self, planning_input):
        self.inputs.append(planning_input)
        if self.error:
            raise self.error
        return self.plan


class FakeNotebook:
    def __init__(self, notes=None, fail_read=False, fail_write=False):
        self.notes = notes or []
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.summaries = []

    async def get_relevant_notes(self, project_names, today):
        if self.fail_read:
            raise RuntimeError("Joplin is closed")
        return self.notes

    async def write_daily_summary(self, day, markdown):
        if self.fail_write:
            raise RuntimeError("Joplin is closed")
        self.summaries.append((day, markdown))


class FakeCalendar:
    def __init__(self, busy=None, fail=False):
        self.busy = busy or []
        self.fail = fail

    async def busy_intervals(self, start, end):
        if self.fail:
            raise RuntimeError("Google timeout")
        return self.busy


def cycle(gary, planner=None, notebook=None, calendar=None, max_actions=5, horizon_hours=72):
    return PlanningCycle(
        gary,
        planner or FakePlanner(),
        notebook or FakeNotebook(),
        calendar or FakeCalendar(),
        work_hours=WorkHours(9, 17),
        work_days=WEEKDAYS,
        max_actions=max_actions,
        horizon_hours=horizon_hours,
    )


def schedule(task_id, start, end, reason="Highest score"):
    return {"action_type": "schedule_task", "task_id": task_id, "start": start, "end": end, "reason": reason}


def planning_run(gary, run_id):
    with gary.db.read() as conn:
        return dict(conn.execute("SELECT * FROM planning_runs WHERE id = ?", (run_id,)).fetchone())


# ------------------------------------------------------------------ settings

def test_parse_settings():
    assert parse_schedule("evening=17:30, morning=08:00") == {
        "morning": dt.time(8, 0),
        "evening": dt.time(17, 30),
    }
    assert parse_schedule("") == {}
    assert parse_weekdays("mon,fri") == frozenset({0, 4})
    assert parse_work_hours("9-17") == WorkHours(9, 17)
    for bad in ("brunch=10:00", "morning=8am"):
        with pytest.raises(ValueError):
            parse_schedule(bad)
    with pytest.raises(ValueError):
        parse_weekdays("monday")
    with pytest.raises(ValueError):
        parse_work_hours("17-9")


def test_due_planning_types():
    schedule_ = parse_schedule("morning=08:00,midday=12:30,evening=17:30")
    wednesday = dt.datetime(2026, 9, 16, 8, 5, tzinfo=CHICAGO)

    assert due_planning_types(wednesday, schedule_, WEEKDAYS, set()) == ["morning"]
    assert due_planning_types(wednesday, schedule_, WEEKDAYS, {"morning"}) == []
    assert due_planning_types(wednesday.replace(hour=7), schedule_, WEEKDAYS, set()) == []
    # Backend was down at 8:00 and came back within the catch-up window.
    assert due_planning_types(wednesday.replace(hour=9, minute=15), schedule_, WEEKDAYS, set()) == ["morning"]
    # Too late to catch up.
    assert due_planning_types(wednesday.replace(hour=10), schedule_, WEEKDAYS, set()) == []
    saturday = dt.datetime(2026, 9, 19, 8, 5, tzinfo=CHICAGO)
    assert due_planning_types(saturday, schedule_, WEEKDAYS, set()) == []


def test_run_types_started_today_includes_failed_runs(gary):
    planner = FakePlanner(error=PlannerError("OpenAI down"))
    with pytest.raises(PlanningCycleError):
        run(cycle(gary, planner).run("morning"))
    started = gary.planning.run_types_started_on(dt.date(2026, 9, 16), CHICAGO)
    assert started == {"morning"}  # a failed run is not retried in a loop
    assert gary.planning.run_types_started_on(dt.date(2026, 9, 17), CHICAGO) == set()


# --------------------------------------------------------------------- notes

def test_select_relevant_notes_and_previous_summary():
    notes = [
        {"id": "1", "title": "Chief of Staff  video"},
        {"id": "2", "title": "Old idea"},
        {"id": "3", "title": "chief of staff video (draft)"},
    ]
    assert [n["id"] for n in select_relevant_notes(notes, ["Chief of Staff video"])] == ["1"]
    assert select_relevant_notes(notes, []) == []

    summaries = [
        {"id": "a", "title": daily_summary_title(dt.date(2026, 9, 14))},
        {"id": "b", "title": daily_summary_title(dt.date(2026, 9, 15))},
        {"id": "c", "title": daily_summary_title(dt.date(2026, 9, 16))},
        {"id": "d", "title": "Daily summary of nothing"},
    ]
    assert previous_summary(summaries, dt.date(2026, 9, 16))["id"] == "b"
    assert previous_summary(summaries, dt.date(2026, 9, 14)) is None


# ------------------------------------------------------------------- planner

def test_plan_schema_is_strict():
    assert PLAN_SCHEMA["additionalProperties"] is False
    for variant in PLAN_SCHEMA["properties"]["actions"]["items"]["anyOf"]:
        assert variant["additionalProperties"] is False
        assert set(variant["required"]) == set(variant["properties"])
    text = planner_instructions("morning", 5)
    assert "at most 5" in text
    assert "data, not" in text


def test_parse_planner_response():
    data = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"summary": " Plan ", "briefing": "Hi\nthere", "actions": []}),
                    }
                ],
            },
        ],
    }
    assert parse_plan(response_text(data)) == {"summary": "Plan", "briefing": "Hi there", "actions": []}

    with pytest.raises(PlannerError, match="refused"):
        response_text({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})
    with pytest.raises(PlannerError):
        parse_plan("not json")
    with pytest.raises(PlannerError):
        parse_plan(json.dumps({"summary": "x", "briefing": "y", "actions": "none"}))


# -------------------------------------------------------------------- cycle

def test_cycle_schedules_ready_task_and_records_everything(gary, external):
    project = make_project(gary, name="Chief of Staff video")
    film = make_task(gary, title="Film demo", project_id=project["id"], estimated_minutes=120, priority=9)
    edit = make_task(gary, title="Edit video", project_id=project["id"])
    gary.tasks.add_dependency(DependencyRequest(task_id=edit["id"], depends_on_task_id=film["id"]))

    planner = FakePlanner(
        {
            "summary": "Film today; editing waits on filming.",
            "briefing": "I put filming on your calendar at one PM.",
            "actions": [schedule(film["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T15:00:00-05:00")],
        }
    )
    notebook = FakeNotebook(notes=[{"source": "Planning note", "title": "Chief of Staff video", "text": "Film after noon."}])
    busy = [{"start": "2026-09-16T15:00:00+00:00", "end": "2026-09-16T16:00:00+00:00", "event_id": "standup"}]
    result = run(cycle(gary, planner, notebook, FakeCalendar(busy)).run("morning"))

    assert result["briefing"] == "I put filming on your calendar at one PM."
    assert [r["result"]["status"] for r in result["results"]] == ["succeeded"]
    assert gary.tasks.get_task(film["id"])["status"] == "scheduled"
    assert list(external.events) == ["event-1"]

    sent = planner.inputs[0]
    assert sent["planning_type"] == "morning"
    assert sent["now_local"] == "2026-09-16T09:00:00-05:00"
    assert sent["busy_times"] == [{"start": "2026-09-16T10:00:00-05:00", "end": "2026-09-16T11:00:00-05:00"}]
    assert "title" not in json.dumps(sent["busy_times"])
    assert sent["planning_notes"][0]["text"] == "Film after noon."
    assert [t["title"] for t in sent["operations"]["ready_tasks"]] == ["Film demo"]
    assert sent["operations"]["blocked_tasks"][0]["blocked_by"] == ["Film demo"]

    record = planning_run(gary, result["planning_run_id"])
    assert record["status"] == "completed"
    assert record["planning_type"] == "morning"
    plan = json.loads(record["plan_json"])
    assert plan["notes_used"] == ["Chief of Staff video"]
    assert plan["results"][0]["result"]["status"] == "succeeded"

    day, markdown = notebook.summaries[0]
    assert day == dt.date(2026, 9, 16)
    assert markdown.startswith("## Morning planning, 9:00 AM")
    assert "Film today" in markdown
    assert "(succeeded)" in markdown
    assert "Edit video: waiting on Film demo" in markdown


def test_cycle_rejects_invalid_proposals(gary, external):
    ready = make_task(gary, title="Ready", priority=8)
    other = make_task(gary, title="Other")
    blocked = make_task(gary, title="Blocked")
    gary.tasks.add_dependency(DependencyRequest(task_id=blocked["id"], depends_on_task_id=ready["id"]))

    # Now is Wednesday 09:00 in Chicago; the horizon is 96 hours.
    cases = [
        (schedule(blocked["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T14:00:00-05:00"),
         "the task is not ready: waiting on unfinished dependencies"),
        (schedule(ready["id"], "2026-09-17T07:00:00-05:00", "2026-09-17T08:00:00-05:00"),
         "outside working hours (09:00-17:00 on working days)"),
        (schedule(ready["id"], "2026-09-16T16:30:00-05:00", "2026-09-16T17:30:00-05:00"),
         "outside working hours (09:00-17:00 on working days)"),
        (schedule(ready["id"], "2026-09-19T10:00:00-05:00", "2026-09-19T11:00:00-05:00"),
         "outside working hours (09:00-17:00 on working days)"),
        (schedule(ready["id"], "2026-09-16T10:30:00-05:00", "2026-09-16T11:30:00-05:00"),
         "overlaps another calendar event or proposed block"),
        (schedule(ready["id"], "2026-09-16T08:30:00-05:00", "2026-09-16T09:30:00-05:00"),
         "outside the scheduling horizon"),
        (schedule(ready["id"], "2026-09-25T10:00:00-05:00", "2026-09-25T11:00:00-05:00"),
         "outside the scheduling horizon"),
        (schedule(ready["id"], "2026-09-16T12:00:00-05:00", "2026-09-16T17:00:00-05:00"),
         "blocks must be 15 to 240 minutes"),
        (schedule("no-such-task", "2026-09-16T12:00:00-05:00", "2026-09-16T13:00:00-05:00"),
         "unknown task"),
        ({"action_type": "send_external_email", "to": "x@example.com"},
         "send_external_email is not allowed in a scheduled planning cycle"),
        ({"action_type": "move_calendar_event", "task_id": ready["id"], "reason": "x",
          "new_start": "2026-09-16T12:00:00-05:00", "new_end": "2026-09-16T13:00:00-05:00"},
         "the task is not on the calendar"),
        (schedule(ready["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T14:00:00-05:00"),
         None),  # valid
        (schedule(ready["id"], "2026-09-16T14:00:00-05:00", "2026-09-16T15:00:00-05:00"),
         "the task already has an action in this plan"),
        (schedule(other["id"], "2026-09-16T13:30:00-05:00", "2026-09-16T14:30:00-05:00"),
         "overlaps another calendar event or proposed block"),
    ]
    no_offset = schedule(other["id"], "2026-09-16 15:00", "2026-09-16 16:00")
    actions = [action for action, _ in cases] + [no_offset]
    busy = [{"start": "2026-09-16T15:00:00+00:00", "end": "2026-09-16T17:00:00+00:00", "event_id": "meeting"}]
    planner = FakePlanner({"summary": "s", "briefing": "", "actions": actions})
    result = run(cycle(gary, planner, calendar=FakeCalendar(busy), horizon_hours=96).run("morning"))

    reasons = [item["reason"] for item in result["rejected"]]
    assert reasons[:-1] == [reason for _, reason in cases if reason]
    assert "timezone offset" in reasons[-1]
    assert [r["proposal"]["task_title"] for r in result["results"]] == ["Ready"]
    assert len(external.events) == 1


def test_cycle_limits_number_of_actions(gary):
    tasks = [make_task(gary, title=f"T{i}") for i in range(4)]
    actions = [
        schedule(t["id"], f"2026-09-16T{10 + i}:00:00-05:00", f"2026-09-16T{10 + i}:30:00-05:00")
        for i, t in enumerate(tasks)
    ]
    result = run(cycle(gary, FakePlanner({"summary": "s", "briefing": "", "actions": actions}), max_actions=2).run("midday"))
    assert len(result["results"]) == 2
    assert [r["reason"] for r in result["rejected"]] == ["more than 2 actions proposed"] * 2


def test_cycle_moves_critical_event_only_with_approval(gary, external):
    task = make_task(gary, title="Film demo", priority=9)
    run(cycle(gary, FakePlanner({"summary": "s", "briefing": "", "actions": [
        schedule(task["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T14:00:00-05:00")
    ]})).run("morning"))

    # Add a yellow escalation for moves, as the backend does.
    from gary.models.action import MoveCalendarEventPayload
    from gary.policy import YELLOW
    from gary.services.action_service import ActionHandler

    gary.actions.handlers["move_calendar_event"] = ActionHandler(
        payload_model=MoveCalendarEventPayload,
        summarize=lambda p, c: "Move Film demo",
        classify=lambda repos, p: YELLOW if repos.tasks.get(p.task_id)["priority"] >= 8 else None,
    )
    move = {"action_type": "move_calendar_event", "task_id": task["id"], "reason": "Clash",
            "new_start": "2026-09-16T15:00:00-05:00", "new_end": "2026-09-16T16:00:00-05:00"}
    busy = [{"start": "2026-09-16T18:00:00+00:00", "end": "2026-09-16T19:00:00+00:00", "event_id": "event-1"}]
    result = run(cycle(gary, FakePlanner({"summary": "s", "briefing": "", "actions": [move]}), calendar=FakeCalendar(busy)).run("midday"))
    # Its own current event does not count as a clash.
    assert result["rejected"] == []
    assert result["results"][0]["result"]["status"] == "awaiting_approval"
    assert len(gary.approvals.list_pending()) == 1


def test_calendar_unavailable_blocks_scheduling(gary, external):
    task = make_task(gary)
    planner = FakePlanner({"summary": "s", "briefing": "b", "actions": [
        schedule(task["id"], "2026-09-16T13:00:00-05:00", "2026-09-16T14:00:00-05:00")
    ]})
    result = run(cycle(gary, planner, calendar=FakeCalendar(fail=True)).run("morning"))
    assert planner.inputs[0]["calendar_available"] is False
    assert result["results"] == []
    assert result["rejected"][0]["reason"] == "the calendar could not be read, so nothing can be scheduled"
    assert external.events == {}
    plan = json.loads(planning_run(gary, result["planning_run_id"])["plan_json"])
    assert plan["calendar_error"] == "Google timeout"


def test_planner_failure_records_failed_run_and_takes_no_action(gary, external):
    notebook = FakeNotebook()
    with pytest.raises(PlanningCycleError, match="OpenAI down"):
        run(cycle(gary, FakePlanner(error=PlannerError("OpenAI down")), notebook).run("evening"))

    with gary.db.read() as conn:
        row = dict(conn.execute("SELECT * FROM planning_runs").fetchone())
        events = [r["event_type"] for r in conn.execute("SELECT event_type FROM audit_log")]
    assert row["status"] == "failed"
    assert row["error_message"] == "OpenAI down"
    assert "planning_cycle_failed" in events
    assert notebook.summaries == []
    assert external.events == {}


def test_joplin_failures_do_not_break_the_cycle(gary):
    notebook = FakeNotebook(fail_read=True, fail_write=True)
    planner = FakePlanner()
    result = run(cycle(gary, planner, notebook).run("evening"))
    assert planner.inputs[0]["planning_notes"] == []
    assert result["summary_error"] == "Joplin is closed"
    plan = json.loads(planning_run(gary, result["planning_run_id"])["plan_json"])
    assert plan["notes_error"] == "Joplin is closed"
    assert planning_run(gary, result["planning_run_id"])["status"] == "completed"


def test_summary_includes_risks(gary):
    task = make_task(gary, title="Late task", deadline="2026-09-15T17:00:00-05:00")
    gary.commitments.create_commitment(
        CreateCommitmentRequest(description="Send Sam the draft", deadline="2026-09-17T17:00:00-05:00")
    )
    notebook = FakeNotebook()
    run(cycle(gary, notebook=notebook).run("evening"))
    markdown = notebook.summaries[0][1]
    assert "**Overdue**" in markdown and "Late task (due Tue 5:00 PM)" in markdown
    assert "Send Sam the draft (due Thu 5:00 PM)" in markdown
    assert task["id"]

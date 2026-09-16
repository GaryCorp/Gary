import pytest

from gary.models.commitment import CreateCommitmentRequest, ResolveCommitmentRequest
from gary.models.followup import CompleteFollowupRequest, CreateFollowupRequest
from gary.models.task import CompleteTaskRequest, DependencyRequest
from gary.services.planning_service import RecordPlanRequest
from gary.services.readiness import calculate_task_score

from conftest import START, audit_events, make_project, make_task

NOW = "2026-09-16T14:00:00+00:00"


def task_row(**overrides):
    row = {"priority": 5, "deadline": None, "status": "todo"}
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides, blocks, commitment, expected",
    [
        ({}, False, False, 50),
        ({"priority": 9}, False, False, 90),
        ({"deadline": "2026-09-17T10:00:00+00:00"}, False, False, 80),   # <= 24h
        ({"deadline": "2026-09-18T14:00:00+00:00"}, False, False, 70),   # <= 72h
        ({"deadline": "2026-09-21T14:00:00+00:00"}, False, False, 60),   # <= 168h
        ({"deadline": "2026-10-30T14:00:00+00:00"}, False, False, 50),   # far away
        ({"deadline": "2026-09-15T14:00:00+00:00"}, False, False, 120),  # overdue
        ({}, True, False, 65),
        ({}, False, True, 70),
        ({"deadline": "2026-09-15T14:00:00+00:00", "status": "completed"}, False, False, 80),
    ],
)
def test_priority_scoring(overrides, blocks, commitment, expected):
    assert calculate_task_score(task_row(**overrides), NOW, blocks, commitment) == expected


def test_scoring_never_changes_user_priority(gary):
    task = make_task(gary, priority=8, deadline="2026-09-16T18:00:00+00:00")
    context = gary.planning.get_planning_context()
    assert context["ready_tasks"][0]["planning_score"] == 110
    assert gary.tasks.get_task(task["id"])["priority"] == 8


def test_overdue_detection(gary, clock):
    late = make_task(gary, title="Film demo", deadline="2026-09-15T17:00:00-05:00")
    make_task(gary, title="Future", deadline="2026-09-20T17:00:00-05:00")
    done = make_task(gary, title="Done", deadline="2026-09-10T17:00:00-05:00")
    gary.tasks.complete_task(CompleteTaskRequest(task_id=done["id"]))

    assert [t["id"] for t in gary.planning.find_overdue_tasks()] == [late["id"]]

    clock.advance(days=5)
    assert [t["title"] for t in gary.planning.find_overdue_tasks()] == ["Film demo", "Future"]


def test_new_alerts_reported_once(gary, clock):
    task = make_task(gary, deadline="2026-09-16T10:00:00+00:00")
    followup = gary.followups.create_followup(
        CreateFollowupRequest(title="Check filming", due_at="2026-09-16T15:00:00+00:00")
    )

    alerts = gary.planning.collect_new_alerts()
    assert [t["id"] for t in alerts["overdue_tasks"]] == [task["id"]]
    assert alerts["due_followups"] == []

    clock.advance(hours=2)
    alerts = gary.planning.collect_new_alerts()
    assert alerts["overdue_tasks"] == []  # not repeated
    assert [f["id"] for f in alerts["due_followups"]] == [followup["id"]]
    assert gary.planning.collect_new_alerts() == {"due_followups": [], "overdue_tasks": []}
    assert "task_overdue_detected" in audit_events(gary, "task", task["id"])


def test_followup_due_detection_order(gary, clock):
    gary.followups.create_followup(
        CreateFollowupRequest(title="Low", due_at="2026-09-16T10:00:00+00:00", priority=3)
    )
    gary.followups.create_followup(
        CreateFollowupRequest(title="High", due_at="2026-09-16T12:00:00+00:00", priority=9)
    )
    gary.followups.create_followup(
        CreateFollowupRequest(title="Later", due_at="2026-09-17T12:00:00+00:00", priority=10)
    )
    assert [f["title"] for f in gary.followups.list_due()] == ["High", "Low"]

    high = gary.followups.list_due()[0]
    gary.followups.complete_followup(CompleteFollowupRequest(followup_id=high["id"]))
    assert [f["title"] for f in gary.followups.list_due()] == ["Low"]
    with pytest.raises(ValueError, match="already completed"):
        gary.followups.complete_followup(CompleteFollowupRequest(followup_id=high["id"]))


def test_followup_inherits_task_project(gary):
    project = make_project(gary)
    task = make_task(gary, project_id=project["id"])
    followup = gary.followups.create_followup(
        CreateFollowupRequest(title="Check", due_at="2026-09-17T00:00:00Z", task_id=task["id"])
    )
    assert followup["project_id"] == project["id"]


def test_commitment_creation_and_weight(gary):
    task = make_task(gary, title="Send draft")
    plain = make_task(gary, title="Plain")
    commitment = gary.commitments.create_commitment(
        CreateCommitmentRequest(
            description="Send Sam the draft",
            committed_to="Sam",
            deadline="2026-09-17T17:00:00-05:00",
            task_id=task["id"],
        )
    )
    assert commitment["status"] == "open"
    assert audit_events(gary, "commitment", commitment["id"]) == ["commitment_created"]

    context = gary.planning.get_planning_context()
    scores = {t["title"]: t["planning_score"] for t in context["ready_tasks"]}
    assert scores["Send draft"] == scores["Plain"] + 20
    assert context["ready_tasks"][0]["title"] == "Send draft"
    assert [c["description"] for c in context["open_commitments"]] == ["Send Sam the draft"]
    assert context["upcoming_deadlines"][0]["type"] == "commitment"

    gary.commitments.resolve_commitment(
        ResolveCommitmentRequest(commitment_id=commitment["id"], status="fulfilled")
    )
    assert gary.commitments.list_open() == []
    assert plain["id"]


def test_planning_context_definition_of_done_scenario(gary):
    """Section 29 of the spec: publish the video by Friday."""
    project = make_project(
        gary, name="Publish next YouTube video", deadline="2026-09-18T17:00:00-05:00"
    )
    ids = {
        title: make_task(gary, title=title, project_id=project["id"])["id"]
        for title in [
            "Finish software",
            "Test agent",
            "Film demo",
            "Edit video",
            "Upload video",
        ]
    }
    for task, prerequisite in [
        ("Film demo", "Finish software"),
        ("Film demo", "Test agent"),
        ("Edit video", "Film demo"),
        ("Upload video", "Edit video"),
    ]:
        gary.tasks.add_dependency(
            DependencyRequest(task_id=ids[task], depends_on_task_id=ids[prerequisite])
        )

    context = gary.planning.get_planning_context()
    assert {t["title"] for t in context["ready_tasks"]} == {"Finish software", "Test agent"}
    blocked = {t["title"]: t["blocked_by"] for t in context["blocked_tasks"]}
    assert blocked["Film demo"] == ["Finish software", "Test agent"]
    assert blocked["Upload video"] == ["Edit video"]
    assert context["active_projects"][0]["open_tasks"] == 5

    gary.tasks.complete_task(CompleteTaskRequest(task_id=ids["Finish software"]))
    gary.tasks.complete_task(CompleteTaskRequest(task_id=ids["Test agent"]))
    context = gary.planning.get_planning_context()
    assert [t["title"] for t in context["ready_tasks"]] == ["Film demo"]


def test_planning_runs_recorded(gary, clock):
    context = gary.planning.get_planning_context()
    run_id = context["planning_run_id"]
    run = gary.planning.record_plan(
        RecordPlanRequest(planning_run_id=run_id, summary="Film Thursday afternoon.")
    )
    assert run["status"] == "completed"
    assert '"summary": "Film Thursday afternoon."' in run["plan_json"]
    with pytest.raises(ValueError, match="already completed"):
        gary.planning.record_plan(RecordPlanRequest(planning_run_id=run_id, summary="again"))

    stale = gary.planning.get_planning_context()["planning_run_id"]
    clock.advance(hours=3)
    gary.planning.get_planning_context()
    with gary.db.read() as conn:
        status = conn.execute(
            "SELECT status FROM planning_runs WHERE id = ?", (stale,)
        ).fetchone()["status"]
    assert status == "failed"


def test_invalid_planning_type(gary):
    with pytest.raises(ValueError):
        gary.planning.get_planning_context("whenever")


def test_start_constant_matches_now():
    assert START.isoformat() == NOW

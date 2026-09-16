import pytest

from gary.models.common import validate_request
from gary.models.project import CreateProjectRequest, UpdateProjectRequest
from gary.models.task import (
    CompleteTaskRequest,
    CreateTaskRequest,
    DependencyRequest,
    UpdateTaskRequest,
)
from gary.services.common import NotFoundError
from gary.models.followup import CreateFollowupRequest

from conftest import audit_events, make_project, make_task


def test_create_get_update_project(gary, clock):
    project = make_project(gary, priority=9, deadline="2026-09-18T17:00:00-05:00")
    assert project["status"] == "active"
    assert project["deadline"] == "2026-09-18T22:00:00+00:00"  # normalized to UTC

    fetched = gary.projects.get_project(project["id"])
    assert fetched["name"] == "Chief of Staff video"
    assert fetched["tasks"] == []

    clock.advance(hours=1)
    updated = gary.projects.update_project(
        UpdateProjectRequest(project_id=project["id"], priority=10, deadline=None)
    )
    assert updated["priority"] == 10
    assert updated["deadline"] is None
    assert updated["updated_at"] > project["updated_at"]
    assert audit_events(gary, "project", project["id"]) == ["project_created", "project_updated"]


def test_complete_project_and_list_active(gary):
    project = make_project(gary)
    other = make_project(gary, name="Other")
    completed = gary.projects.mark_completed(project["id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert [p["id"] for p in gary.projects.list_projects()] == [other["id"]]
    assert len(gary.projects.list_projects(include_closed=True)) == 2
    assert "project_completed" in audit_events(gary, "project", project["id"])


def test_create_get_update_task(gary):
    project = make_project(gary)
    task = make_task(
        gary,
        project_id=project["id"],
        priority=9,
        estimated_minutes=120,
        deadline="2026-10-10T17:00:00-05:00",
    )
    assert task["status"] == "todo"
    assert task["created_by"] == "gary"

    fetched = gary.tasks.get_task(task["id"])
    assert fetched["title"] == "Film demo"
    assert fetched["ready"] is True

    updated = gary.tasks.update_task(
        UpdateTaskRequest(task_id=task["id"], status="in_progress", description="Record it")
    )
    assert updated["status"] == "in_progress"
    assert updated["started_at"]
    assert updated["description"] == "Record it"
    assert audit_events(gary, "task", task["id"]) == ["task_created", "task_updated"]


def test_task_requires_existing_project(gary):
    with pytest.raises(NotFoundError):
        make_task(gary, project_id="missing")


def test_complete_task_closes_followups_atomically(gary):
    task = make_task(gary)
    gary.followups.create_followup(
        CreateFollowupRequest(
            title="Check filming", due_at="2026-09-17T15:00:00-05:00", task_id=task["id"]
        )
    )
    completed = gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"], actual_minutes=90))
    assert completed["status"] == "completed"
    assert completed["actual_minutes"] == 90
    assert completed["completed_at"]
    assert gary.followups.list_due() == []
    with gary.db.read() as conn:
        assert conn.execute("SELECT status FROM followups").fetchone()["status"] == "completed"
    assert audit_events(gary, "task", task["id"])[-1] == "task_completed"

    again = gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))
    assert again["already_completed"] is True


def test_complete_task_rolls_back_if_audit_fails(gary, monkeypatch):
    task = make_task(gary)
    from gary.db.repositories.audit import AuditRepository

    def broken_write(self, *args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditRepository, "write", broken_write)
    with pytest.raises(RuntimeError):
        gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))
    monkeypatch.undo()

    assert gary.tasks.get_task(task["id"])["status"] == "todo"


def test_closed_task_cannot_be_updated(gary):
    task = make_task(gary)
    gary.tasks.complete_task(CompleteTaskRequest(task_id=task["id"]))
    with pytest.raises(ValueError, match="completed"):
        gary.tasks.update_task(UpdateTaskRequest(task_id=task["id"], priority=3))


def test_dependency_and_completion_unblocks(gary):
    film = make_task(gary, title="Film video")
    edit = make_task(gary, title="Edit video")
    result = gary.tasks.add_dependency(
        DependencyRequest(task_id=edit["id"], depends_on_task_id=film["id"])
    )
    assert result == {"task": "Edit video", "depends_on": "Film video", "already_existed": False}

    assert gary.tasks.is_task_ready(edit["id"]) == {
        "task_id": edit["id"],
        "task": "Edit video",
        "ready": False,
        "blocked_by": ["Film video"],
        "reasons": ["waiting on unfinished dependencies"],
    }

    completed = gary.tasks.complete_task(CompleteTaskRequest(task_id=film["id"]))
    assert completed["unblocked_tasks"] == ["Edit video"]
    assert gary.tasks.is_task_ready(edit["id"])["ready"] is True

    again = gary.tasks.add_dependency(
        DependencyRequest(task_id=edit["id"], depends_on_task_id=film["id"])
    )
    assert again["already_existed"] is True


def test_reject_self_dependency(gary):
    task = make_task(gary)
    with pytest.raises(ValueError, match="itself"):
        gary.tasks.add_dependency(DependencyRequest(task_id=task["id"], depends_on_task_id=task["id"]))


def test_reject_circular_dependency(gary):
    a = make_task(gary, title="A")
    b = make_task(gary, title="B")
    c = make_task(gary, title="C")
    gary.tasks.add_dependency(DependencyRequest(task_id=a["id"], depends_on_task_id=b["id"]))
    with pytest.raises(ValueError, match="Circular"):
        gary.tasks.add_dependency(DependencyRequest(task_id=b["id"], depends_on_task_id=a["id"]))

    gary.tasks.add_dependency(DependencyRequest(task_id=b["id"], depends_on_task_id=c["id"]))
    with pytest.raises(ValueError, match="Circular"):
        gary.tasks.add_dependency(DependencyRequest(task_id=c["id"], depends_on_task_id=a["id"]))


def test_remove_dependency(gary):
    a = make_task(gary, title="A")
    b = make_task(gary, title="B")
    request = DependencyRequest(task_id=a["id"], depends_on_task_id=b["id"])
    gary.tasks.add_dependency(request)
    assert gary.tasks.remove_dependency(request)["removed"] is True
    assert gary.tasks.is_task_ready(a["id"])["ready"] is True


def test_readiness_respects_earliest_start_and_status(gary, clock):
    later = make_task(gary, earliest_start="2026-09-16T12:00:00-05:00")  # 17:00 UTC
    assert gary.tasks.is_task_ready(later["id"])["reasons"] == ["earliest start has not arrived"]
    clock.advance(hours=4)
    assert gary.tasks.is_task_ready(later["id"])["ready"] is True

    waiting = make_task(gary, title="Waiting on Sam")
    gary.tasks.update_task(UpdateTaskRequest(task_id=waiting["id"], status="waiting"))
    assert gary.tasks.is_task_ready(waiting["id"])["ready"] is False


def test_invalid_priority_rejected():
    with pytest.raises(ValueError, match="priority"):
        validate_request(CreateTaskRequest, {"title": "x", "priority": 11})
    with pytest.raises(ValueError, match="priority"):
        validate_request(CreateTaskRequest, {"title": "x", "priority": "high"})
    with pytest.raises(ValueError, match="priority"):
        validate_request(CreateProjectRequest, {"name": "x", "objective": "y", "priority": 0})


def test_invalid_status_rejected():
    with pytest.raises(ValueError, match="status"):
        validate_request(UpdateTaskRequest, {"task_id": "t", "status": "done"})
    with pytest.raises(ValueError, match="status"):
        validate_request(UpdateTaskRequest, {"task_id": "t", "status": "completed"})
    with pytest.raises(ValueError, match="status"):
        validate_request(UpdateProjectRequest, {"project_id": "p", "status": "finished"})


@pytest.mark.parametrize(
    "value",
    ["tomorrow afternoon", "2026-10-10", "2026-10-10T17:00:00", "2026-13-01T00:00:00Z", 5],
)
def test_invalid_timestamp_rejected(value):
    with pytest.raises(ValueError, match="deadline"):
        validate_request(CreateTaskRequest, {"title": "x", "deadline": value})


def test_other_validation_rules():
    with pytest.raises(ValueError, match="title"):
        validate_request(CreateTaskRequest, {"title": ""})
    with pytest.raises(ValueError, match="estimated_minutes"):
        validate_request(CreateTaskRequest, {"title": "x", "estimated_minutes": -5})
    with pytest.raises(ValueError, match="Extra inputs"):
        validate_request(CreateTaskRequest, {"title": "x", "sql": "DROP TABLE"})
    with pytest.raises(ValueError, match="core fields"):
        validate_request(CreateTaskRequest, {"title": "x", "metadata": {"status": "completed"}})
    with pytest.raises(ValueError, match="cannot be cleared"):
        validate_request(UpdateTaskRequest, {"task_id": "t", "title": None})
    with pytest.raises(ValueError, match="no changes"):
        validate_request(UpdateTaskRequest, {"task_id": "t"})

    ok = validate_request(CreateTaskRequest, {"title": "x", "metadata": {"youtube_episode": 3}})
    assert ok.metadata == {"youtube_episode": 3}

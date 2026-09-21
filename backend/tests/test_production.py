"""The weekly video schedule: batches filmed every other Saturday, one episode
published every Friday, deadlines worked back from each publish slot.

The clock starts on Wednesday 2026-09-16 (conftest.START); the first shoot is
Saturday 2026-09-26, so Video 1 publishes Friday 2026-10-02 and Video 2
Friday 2026-10-09.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from conftest import run
from gary.db.repositories import Repositories
from gary.models.project import CreateProjectRequest, UpdateProjectRequest
from gary.models.task import CompleteTaskRequest, CreateTaskRequest
from gary.services.planning_service import collect_operations
from gary.services.production import (
    ProductionSchedule,
    ProductionService,
    batch_plan,
    schedule_from_settings,
)
from gary.timeutil import to_local

ZONE = ZoneInfo("America/Chicago")
SCHEDULE = ProductionSchedule(first_shoot=dt.date(2026, 9, 26))


def production(gary, clock, schedule=SCHEDULE) -> ProductionService:
    return ProductionService(gary.db, gary.actions, schedule, ZONE, clock)


def local(value: str) -> str:
    return to_local(value, ZONE)[:16]


def rows(gary, sql, *args):
    with gary.db.read() as conn:
        return [dict(row) for row in conn.execute(sql, args)]


def tasks_by_title(gary) -> dict:
    return {t["title"]: t for t in rows(gary, "SELECT * FROM tasks")}


def depends_on(gary, title: str) -> set[str]:
    return {
        row["title"]
        for row in rows(
            gary,
            """
            SELECT p.title FROM task_dependencies d
            JOIN tasks t ON t.id = d.task_id JOIN tasks p ON p.id = d.depends_on_task_id
            WHERE t.title = ?
            """,
            title,
        )
    }


def test_two_episodes_per_shoot_keeps_one_a_week():
    assert [SCHEDULE.publish_date(n) for n in range(1, 6)] == [
        dt.date(2026, 10, 2),
        dt.date(2026, 10, 9),
        dt.date(2026, 10, 16),
        dt.date(2026, 10, 23),
        dt.date(2026, 10, 30),
    ]
    assert [SCHEDULE.shoot_date(b) for b in range(3)] == [
        dt.date(2026, 9, 26),
        dt.date(2026, 10, 10),
        dt.date(2026, 10, 24),
    ]
    assert SCHEDULE.batch_episodes(1) == [3, 4]


def test_deadlines_are_worked_back_from_each_publish_slot():
    plan = batch_plan(SCHEDULE, 0, ZONE)
    first, second = plan["episodes"]
    tasks = {t["key"]: t for t in first["tasks"]}

    assert local(plan["film"]["earliest_start"]) == "2026-09-26T10:00"
    assert local(plan["film"]["deadline"]) == "2026-09-26T14:00"  # 2 h per episode
    assert plan["film"]["title"] == "Film Video 1 and Video 2"

    assert local(tasks["script"]["earliest_start"]) == "2026-09-21T00:00"  # the shoot's week
    assert local(tasks["script"]["deadline"]) == "2026-09-25T17:00"  # the day before
    assert local(tasks["edit"]["earliest_start"]) == "2026-09-28T00:00"
    assert local(tasks["edit"]["deadline"]) == "2026-09-30T17:00"  # Wednesday
    assert local(tasks["thumbnail"]["deadline"]) == "2026-10-01T17:00"  # Thursday
    assert local(tasks["publish"]["earliest_start"]) == "2026-10-02T16:30"
    assert local(tasks["publish"]["deadline"]) == "2026-10-02T17:00"

    # The second episode's post-production waits for its own publish week.
    second_tasks = {t["key"]: t for t in second["tasks"]}
    assert local(second_tasks["edit"]["earliest_start"]) == "2026-10-05T00:00"
    assert local(second["publish_at"]) == "2026-10-09T17:00"


def test_plans_the_batch_inside_the_horizon_with_its_dependencies(gary, clock):
    assert production(gary, clock).plan_upcoming() == [1, 2]

    projects = rows(gary, "SELECT name, status, deadline FROM projects ORDER BY name")
    assert [(p["name"], p["status"], local(p["deadline"])) for p in projects] == [
        ("Video 1", "active", "2026-10-02T17:00"),
        ("Video 2", "active", "2026-10-09T17:00"),
    ]
    tasks = tasks_by_title(gary)
    assert sorted(tasks) == sorted(
        ["Film Video 1 and Video 2"]
        + [f"Video {n}: {stage}" for n in (1, 2)
           for stage in ("Script", "Edit", "Thumbnail and title", "Publish")]
    )
    assert all(t["created_by"] == "system" for t in tasks.values())

    assert depends_on(gary, "Film Video 1 and Video 2") == {"Video 1: Script", "Video 2: Script"}
    assert depends_on(gary, "Video 2: Edit") == {"Film Video 1 and Video 2"}
    assert depends_on(gary, "Video 2: Publish") == {"Video 2: Edit", "Video 2: Thumbnail and title"}

    episodes = rows(gary, "SELECT * FROM production_episodes ORDER BY episode_number")
    assert [e["episode_number"] for e in episodes] == [1, 2]
    assert episodes[0]["film_task_id"] == episodes[1]["film_task_id"]


def test_audit_records_the_batch_in_order(gary, clock):
    production(gary, clock).plan_upcoming()
    events = [r["event_type"] for r in rows(gary, "SELECT event_type FROM audit_log ORDER BY id")]
    one_episode = ["project_created"] + ["task_created"] * 4
    assert events == (
        one_episode * 2
        + ["task_created"]  # the shared shoot
        + ["dependency_added"] * 10  # 2 scripts, then 4 per episode
        + ["production_episode_planned"] * 2
    )
    assert {r["actor"] for r in rows(gary, "SELECT actor FROM audit_log")} == {"system"}


def test_planning_again_creates_nothing(gary, clock):
    service = production(gary, clock)
    service.plan_upcoming()
    before = rows(gary, "SELECT COUNT(*) AS n FROM tasks")[0]["n"]

    assert service.plan_upcoming() == []
    assert rows(gary, "SELECT COUNT(*) AS n FROM tasks")[0]["n"] == before


def test_the_next_batch_is_planned_two_weeks_before_its_shoot(gary, clock):
    service = production(gary, clock)
    service.plan_upcoming()

    clock.advance(days=9)  # Friday 25th: the 10th is still 15 days away
    assert service.plan_upcoming() == []
    clock.advance(days=1)
    assert service.plan_upcoming() == [3, 4]
    assert "Film Video 3 and Video 4" in tasks_by_title(gary)


def test_a_shoot_that_has_already_passed_is_skipped_not_backfilled(gary, clock):
    late = ProductionSchedule(first_shoot=dt.date(2026, 9, 12))
    # The 12th is past; numbering carries on, so the 26th films 3 and 4.
    assert production(gary, clock, late).plan_upcoming() == [3, 4]
    assert rows(gary, "SELECT COUNT(*) AS n FROM production_episodes WHERE episode_number < 3")[0]["n"] == 0


def test_work_becomes_ready_in_order(gary, clock):
    production(gary, clock).plan_upcoming()
    tasks = tasks_by_title(gary)

    # Before the shoot's week nothing is ready: scripts wait for Monday.
    assert gary.tasks.is_task_ready(tasks["Video 2: Script"]["id"])["reasons"] == [
        "earliest start has not arrived"
    ]
    clock.advance(days=5)  # Monday 21st
    assert gary.tasks.is_task_ready(tasks["Video 2: Script"]["id"])["ready"]
    film = gary.tasks.is_task_ready(tasks["Film Video 1 and Video 2"]["id"])
    assert set(film["blocked_by"]) == {"Video 1: Script", "Video 2: Script"}

    for title in ("Video 1: Script", "Video 2: Script"):
        gary.tasks.complete_task(CompleteTaskRequest(task_id=tasks[title]["id"]))
    clock.advance(days=5, hours=2)  # Saturday 26th, 11:00 Chicago
    assert gary.tasks.is_task_ready(tasks["Film Video 1 and Video 2"]["id"])["ready"]
    assert not gary.tasks.is_task_ready(tasks["Video 1: Edit"]["id"])["ready"]


def test_shoot_and_publish_slots_go_on_the_calendar_outside_working_hours(gary, clock, external):
    result = run(production(gary, clock).run())

    assert result["created"] == [1, 2]
    assert sorted(result["scheduled"]) == sorted(
        ["Film Video 1 and Video 2", "Video 1: Publish", "Video 2: Publish"]
    )
    assert result["failed"] == []
    assert sorted(external.events.values()) == sorted(
        [
            (t["earliest_start"], t["deadline"])
            for title, t in tasks_by_title(gary).items()
            if title in result["scheduled"]
        ]
    )
    actions = rows(gary, "SELECT action_type, status, payload_json FROM actions")
    assert {a["action_type"] for a in actions} == {"schedule_task"}
    assert all(json.loads(a["payload_json"])["override_working_hours"] for a in actions)
    # The flexible work is left for the planner to fit into the week.
    assert tasks_by_title(gary)["Video 1: Edit"]["calendar_event_id"] is None

    # Nothing is scheduled twice.
    again = run(production(gary, clock).run())
    assert again == {"created": [], "scheduled": [], "failed": []}


def test_a_calendar_failure_is_recorded_and_retried_next_run(gary, clock, external):
    service = production(gary, clock)
    service.plan_upcoming()
    external.fail_next = "Calendar unavailable"

    scheduled, failed = run(service.schedule_fixed_tasks())
    assert len(scheduled) == 2
    assert failed == [{"task": failed[0]["task"], "error": "Calendar unavailable"}]
    statuses = [a["status"] for a in rows(gary, "SELECT status FROM actions ORDER BY rowid")]
    assert statuses.count("failed") == 1

    scheduled, failed = run(service.schedule_fixed_tasks())
    assert scheduled == [failed_task := scheduled[0]] and failed == []
    assert tasks_by_title(gary)[failed_task]["calendar_event_id"]


def test_a_project_that_has_not_started_holds_its_tasks_back(gary):
    project = gary.projects.create_project(
        CreateProjectRequest(name="Video 8", objective="Later", status="active")
    )
    task = gary.tasks.create_task(CreateTaskRequest(project_id=project["id"], title="Film it"))
    assert gary.tasks.is_task_ready(task["id"])["ready"]

    gary.projects.update_project(UpdateProjectRequest(project_id=project["id"], status="planned"))
    readiness = gary.tasks.is_task_ready(task["id"])
    assert not readiness["ready"]
    assert readiness["reasons"] == ["its project has not started"]
    with gary.db.read() as conn:
        operations = collect_operations(Repositories.bind(conn), "2026-09-16T14:00:00+00:00")
    assert task["id"] not in {t["task_id"] for t in operations["ready_tasks"]}
    assert task["id"] in {t["task_id"] for t in operations["blocked_tasks"]}


def test_settings_turn_the_schedule_on_and_reject_nonsense():
    assert schedule_from_settings("") is None
    schedule = schedule_from_settings("2026-09-26", publish_day="Friday", shoot_time="09:30")
    assert schedule.publish_weekday == 4
    assert schedule.shoot_time == dt.time(9, 30)
    with pytest.raises(ValueError, match="PRODUCTION_PUBLISH_DAY"):
        schedule_from_settings("2026-09-26", publish_day="someday")
    with pytest.raises(ValueError, match="PRODUCTION_FIRST_SHOOT"):
        schedule_from_settings("next saturday")
    with pytest.raises(ValueError, match="between 1 and 8"):
        schedule_from_settings("2026-09-26", episodes_per_shoot="0")

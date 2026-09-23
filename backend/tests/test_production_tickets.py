"""The video schedule's stages as GitHub issues.

A stage's issue opens in the week its work can start. Production tickets
differ from engineering ones in one way: there is nothing to review, so
closing the issue finishes the stage, and finishing the stage in Gary closes
the issue.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeClock, FakeExternal, fake_handlers, run
from fake_github import FakeGitHub
from gary import build_gary
from gary.db.repositories import Repositories
from gary.models.task import CompleteTaskRequest
from gary.services.engineering_actions import engineering_action_handlers
from gary.services.production import (
    ProductionSchedule,
    ProductionService,
    stage_of,
    ticket_payload,
)
from test_engineering import build_service

ZONE = ZoneInfo("America/Chicago")
SCHEDULE = ProductionSchedule(first_shoot=dt.date(2026, 9, 26))


@pytest.fixture
def company(tmp_path):
    """Gary with the real engineering action handlers over a fake GitHub,
    and one batch of the schedule planned."""
    clock = FakeClock()
    holder = {}
    gary = build_gary(
        tmp_path / "gary.db",
        "America/Chicago",
        action_handlers={
            **fake_handlers(FakeExternal()),
            **engineering_action_handlers(lambda: holder.get("service")),
        },
        clock=clock,
    )
    service, github = build_service(gary, clock=clock)
    holder["service"] = service
    production = ProductionService(
        gary.db, gary.actions, SCHEDULE, ZONE, clock, engineering=lambda: service
    )
    production.plan_upcoming()
    return gary, production, service, github, clock


def tickets(gary) -> dict:
    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        return {
            repos.tasks.get(row["task_id"])["title"]: row
            for row in repos.engineering.list_all(100)
        }


def task_named(gary, title: str) -> dict:
    with gary.db.read() as conn:
        return dict(conn.execute("SELECT * FROM tasks WHERE title = ?", (title,)).fetchone())


def test_stage_titles_are_recognised():
    assert stage_of("Video 2: Script") == ("script", "Video 2")
    assert stage_of("Video 12: Thumbnail and title") == ("thumbnail", "Video 12")
    assert stage_of("Film Video 1 and Video 2") == ("film", "Video 1 and Video 2")
    assert stage_of("Write the newsletter") is None


def test_only_this_weeks_stages_get_issues(company):
    gary, production, _, github, _ = company
    result = run(production.sync_tickets())

    # Wednesday the 16th: the scripts start Monday the 21st, inside a week;
    # the shoot (26th) and the edits (28th) are further out.
    assert sorted(result["opened"]) == ["Video 1: Script", "Video 2: Script"]
    rows = tickets(gary)
    assert {row["kind"] for row in rows.values()} == {"production"}
    assert all(row["sync_state"] == "synced" for row in rows.values())
    issue = github.issues[1]
    assert "production" in issue["labels"]
    assert "engineering" not in issue["labels"]
    assert "Alex — Creator" in issue["body"]

    # Nothing is opened twice.
    assert run(production.sync_tickets())["opened"] == []


def test_the_board_follows_the_week(company):
    gary, production, _, _, clock = company
    run(production.sync_tickets())
    clock.advance(days=3, hours=2)  # Saturday the 19th, 11:00: the shoot is a week away
    opened = run(production.sync_tickets())["opened"]
    assert opened == ["Film Video 1 and Video 2"]


def test_closing_the_issue_finishes_the_stage(company):
    gary, production, service, github, _ = company
    run(production.sync_tickets())
    ticket = tickets(gary)["Video 2: Script"]

    github.issues[ticket["github_issue_number"]]["state"] = "closed"  # card still Ready
    result = run(service.sync_ticket(ticket["id"]))

    assert result["needs_reconciliation"] is False
    assert result["status"] == "done"
    assert task_named(gary, "Video 2: Script")["status"] == "completed"
    item = github.items[ticket["github_project_item_id"]]
    assert item["fields"]["Status"] == "Done"


def test_finishing_the_stage_in_gary_closes_the_issue(company):
    gary, production, _, github, _ = company
    run(production.sync_tickets())
    ticket = tickets(gary)["Video 1: Script"]

    gary.tasks.complete_task(CompleteTaskRequest(task_id=ticket["task_id"]))
    result = run(production.sync_tickets())

    assert result["closed"] == [ticket["github_issue_number"]]
    assert github.issues[ticket["github_issue_number"]]["state"] == "closed"
    assert github.items[ticket["github_project_item_id"]]["fields"]["Status"] == "Done"
    assert tickets(gary)["Video 1: Script"]["status"] == "done"
    with gary.db.read() as conn:
        events = [
            r[0] for r in conn.execute(
                "SELECT event_type FROM audit_log WHERE entity_id = ? ORDER BY id",
                (ticket["id"],),
            )
        ]
    assert events[-1] == "production_ticket_closed"
    # And it is not closed again.
    assert run(production.sync_tickets())["closed"] == []


def test_engineering_tickets_are_never_closed_from_their_task(company):
    gary, _, service, _, _ = company
    with gary.db.read() as conn:
        row = {"kind": "engineering", "status": "ready"}
    with pytest.raises(Exception, match="Only a production ticket"):
        run(service.close_production_ticket(row))


def test_a_github_failure_backs_off_instead_of_retrying_every_tick(company):
    gary, production, _, github, clock = company
    github.fail["create_issue"] = (502, "Bad gateway")

    first = run(production.sync_tickets())
    assert first["opened"] == [] and len(first["failed"]) == 2
    github.fail.clear()
    clock.advance(minutes=15)
    assert run(production.sync_tickets()) == {"opened": [], "closed": [], "failed": []}

    clock.advance(hours=6)
    assert len(run(production.sync_tickets())["opened"]) == 2


def test_the_payload_is_a_complete_specification(company):
    gary, _, _, _, _ = company
    payload = ticket_payload(task_named(gary, "Video 1: Publish"), ZONE)
    assert payload["kind"] == "production"
    assert payload["priority"] == "P1"
    assert payload["objective"] == "Upload and publish Video 1 at Friday October 2 at 5:00 PM."
    assert payload["acceptance_criteria"] == ["The video is live"]

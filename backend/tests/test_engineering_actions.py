"""Gary running Alex's engineering queue on his own.

Opening a ticket is green, so the interesting tests are not that it works but
that it is bounded: the caps on how much work Gary may assign unattended, the
proposals Python refuses, and what happens when GitHub is not configured.
"""

import datetime as dt

import pytest

from gary import build_gary
from gary.db.repositories import Repositories
from gary.integrations.github.client import GitHubClient
from gary.integrations.github.config import EnvTokenProvider
from gary.integrations.github.privacy import PrivacyGate
from gary.integrations.github.projects import ProjectBoard
from gary.models.action import ProposeActionRequest
from gary.services.engineering_actions import engineering_action_handlers
from gary.services.engineering_service import EngineeringTicketService
from gary.services.planning_cycle import (
    MAX_CYCLE_PRIORITY_CHANGES,
    MAX_CYCLE_TICKETS,
    MAX_DAILY_TICKETS,
    validate_cycle_actions,
)

from conftest import START, iso, make_task, run
from fake_github import FakeGitHub
from test_engineering import PRIORITY_FIELDS, build_config

TOKEN = "ghp_fake_token_for_tests"


async def _no_sleep(_seconds):
    return None


@pytest.fixture
def github():
    fake = FakeGitHub()
    fake.extra_fields = dict(PRIORITY_FIELDS)
    return fake


@pytest.fixture
def engineering(db_path, clock, github):
    """A Gary whose action pipeline can reach GitHub, as the app wires it."""
    holder = {}
    gary = build_gary(
        db_path,
        "America/Chicago",
        action_handlers=engineering_action_handlers(lambda: holder.get("service")),
        clock=clock,
    )
    client = GitHubClient(
        build_config(), EnvTokenProvider(TOKEN), transport=github, sleep=_no_sleep
    )
    holder["service"] = EngineeringTicketService(
        gary, client, board=ProjectBoard(client),
        gate=PrivacyGate(client, cache_seconds=0), clock=clock,
    )
    return gary, holder


def ticket_proposal(task_id, **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "title": "Add read-only research integration",
        "objective": "Susan needs read-only web research so her reports cite current sources.",
        "requirements": ["Read-only web search"],
        "acceptance_criteria": ["Susan can cite sources"],
        "priority": "P1",
    }
    payload.update(overrides)
    return payload


def propose(gary, action_type, payload):
    return run(
        gary.actions.propose(
            ProposeActionRequest(action_type=action_type, payload=payload, reason="Needed this week")
        )
    )


def tickets(gary):
    with gary.db.read() as conn:
        return Repositories.bind(conn).engineering.list_all()


# ---------------------------------------------------------- the action

def test_gary_can_open_a_ticket_without_approval(engineering, github):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")

    result = propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    assert result["risk_level"] == "green", "no approval stands between Gary and a ticket"
    assert result["status"] == "succeeded"
    assert result["result"]["issue_number"] == 1
    row = tickets(gary)[0]
    assert row["priority"] == "P1"
    assert row["sync_state"] == "synced"
    assert github.issues[1]["assignees"] == ["alex"]


def test_a_ticket_for_impossible_work_is_refused(engineering):
    gary, _ = engineering
    done = make_task(gary, title="Already finished")
    with gary.db.transaction() as conn:
        Repositories.bind(conn).tasks.update(done["id"], now=iso(START), status="completed")

    with pytest.raises(ValueError, match="completed"):
        propose(gary, "create_engineering_ticket", ticket_proposal(done["id"]))

    unknown = "7d0f4d1c-7e2b-4c55-9d1c-0b1e7f0e9a11"
    with pytest.raises(ValueError, match="No task"):
        propose(gary, "create_engineering_ticket", ticket_proposal(unknown))


def test_one_ticket_per_task(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    with pytest.raises(ValueError, match="already has engineering ticket"):
        propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    assert len(tickets(gary)) == 1


def test_gary_can_reprioritise(engineering, github):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    created = propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))
    ticket_id = created["result"]["ticket_id"]

    result = propose(
        gary,
        "set_engineering_priority",
        {"ticket_id": ticket_id, "priority": "P0", "reason": "The demo moved up"},
    )

    assert result["status"] == "succeeded"
    assert tickets(gary)[0]["priority"] == "P0"
    assert github.items[tickets(gary)[0]["github_project_item_id"]]["fields"]["Priority"] == "P0"


def test_reprioritising_to_the_same_priority_is_refused(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    created = propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    with pytest.raises(ValueError, match="already P1"):
        propose(
            gary,
            "set_engineering_priority",
            {"ticket_id": created["result"]["ticket_id"], "priority": "P1"},
        )


def test_without_github_the_action_refuses_rather_than_crashing(db_path, clock):
    gary = build_gary(
        db_path,
        "America/Chicago",
        action_handlers=engineering_action_handlers(lambda: None),
        clock=clock,
    )
    task = make_task(gary, title="Research integration")

    with pytest.raises(ValueError, match="not configured"):
        propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))


# ------------------------------------------------------------- the caps

def validate(gary, actions, now=None, day_start=None):
    from gary.services.calendar_blocks import WorkWeek

    return validate_cycle_actions(
        gary,
        actions,
        now=now or iso(START),
        busy=[],
        calendar_available=True,
        week=WorkWeek(),
        horizon_hours=72,
        max_actions=10,
        day_start=day_start or iso(START - dt.timedelta(hours=14)),
    )


def cycle_ticket(task_id, **overrides):
    return {
        "action_type": "create_engineering_ticket",
        **ticket_proposal(task_id, **overrides),
        "kind": "feature",
        "security_review_required": False,
        "reason": "Needed this week",
    }


def test_a_cycle_opens_one_ticket_at_a_time(engineering):
    gary, _ = engineering
    tasks = [make_task(gary, title=f"Engineering work {i}") for i in range(2)]

    accepted, rejected = validate(gary, [cycle_ticket(t["id"]) for t in tasks])

    assert len(accepted) == MAX_CYCLE_TICKETS == 1
    assert rejected[0]["reason"] == "at most 1 engineering ticket per cycle"


def test_a_cycle_will_not_fill_the_backlog_in_a_day(engineering):
    gary, _ = engineering
    for i in range(MAX_DAILY_TICKETS):
        task = make_task(gary, title=f"Engineering work {i}")
        propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    one_more = make_task(gary, title="One more thing")
    accepted, rejected = validate(gary, [cycle_ticket(one_more["id"])])

    assert accepted == []
    assert rejected[0]["reason"] == f"at most {MAX_DAILY_TICKETS} engineering tickets a day"


def test_a_cycle_needs_a_real_specification(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")

    _, rejected = validate(gary, [cycle_ticket(task["id"], requirements=[], acceptance_criteria=[])])
    assert rejected[0]["reason"] == (
        "an engineering ticket needs requirements and acceptance criteria"
    )

    _, rejected = validate(gary, [cycle_ticket(task["id"], title="x", objective="short")])
    assert rejected[0]["reason"] == "an engineering ticket needs a title and an objective"


def test_a_cycle_does_not_ticket_what_is_already_ticketed(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    accepted, rejected = validate(gary, [cycle_ticket(task["id"])])

    assert accepted == []
    assert rejected[0]["reason"] == "that task already has an engineering ticket"


def test_a_cycle_reshuffles_only_so_much(engineering):
    gary, _ = engineering
    ids = []
    for i in range(MAX_CYCLE_PRIORITY_CHANGES + 1):
        task = make_task(gary, title=f"Engineering work {i}")
        created = propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))
        ids.append(created["result"]["ticket_id"])

    changes = [
        {"action_type": "set_engineering_priority", "ticket_id": t, "priority": "P0",
         "reason": "Deadline moved"}
        for t in ids
    ]
    accepted, rejected = validate(gary, changes)

    assert len(accepted) == MAX_CYCLE_PRIORITY_CHANGES
    assert rejected[0]["reason"] == (
        f"at most {MAX_CYCLE_PRIORITY_CHANGES} priority changes per cycle"
    )


def test_a_cycle_will_not_write_a_priority_that_is_already_set(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    created = propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    _, rejected = validate(
        gary,
        [{"action_type": "set_engineering_priority", "ticket_id": created["result"]["ticket_id"],
          "priority": "P1", "reason": "no change"}],
    )

    assert rejected[0]["reason"] == "that ticket is already P1"


def test_the_planner_sees_the_queue(engineering):
    gary, _ = engineering
    task = make_task(gary, title="Research integration")
    propose(gary, "create_engineering_ticket", ticket_proposal(task["id"]))

    queue = gary.planning.snapshot()["engineering_tickets"]

    assert len(queue) == 1
    assert queue[0]["priority"] == "P1"
    assert queue[0]["status"] == "ready"
    assert queue[0]["title"] == "Research integration"
    assert queue[0]["issue_number"] == 1

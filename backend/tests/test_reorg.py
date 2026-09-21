"""Gary proposing a different shape for the company.

The roster in code decides everything and `sync_roster` overwrites the
database from it, so a reorganisation can only ever be a proposal that ends
in Alex editing `roster.py`. These tests are mostly refusals, because what
the feature is really made of is what Gary is not allowed to propose.
"""

import pytest

from gary.db.repositories import Repositories
from gary.models.action import ProposeActionRequest
from gary.services.reorg_actions import ReorganisationError, reorg_action_handler
from gary.services.common import NotFoundError

from conftest import run
from fake_github import FakeGitHub
from test_agents import build_team
from test_hiring import engineering_for


def reorg_ready(gary, github=None, engineering=None):
    """A registry and Gary wired so a reorganisation can be proposed."""
    service = build_team(gary)
    registry = service.registry
    if engineering is None and engineering is not False:
        engineering, github = engineering_for(gary, github)
    gary.actions.handlers.update(
        reorg_action_handler(registry, lambda: engineering or None)
    )
    gary.github_for_tests = github
    return service, registry


def change(agent_id="susan", kind="title", to="Head of Research", reason="Her record supports it"):
    return {"agent_id": agent_id, "change": kind, "to": to, "reason": reason}


def propose(gary, *changes, rationale="The company is organised around old assumptions."):
    return run(
        gary.actions.propose(
            ProposeActionRequest(
                action_type="propose_reorganisation",
                payload={"changes": list(changes) or [change()], "rationale": rationale},
                reason="Reorganising",
            )
        )
    )


def approve(gary, result):
    return run(gary.approvals.resolve(result["approval_id"], "approved", channel="web"))


def tickets(gary):
    with gary.db.read() as conn:
        return Repositories.bind(conn).engineering.list_all()


# --------------------------------------------- what cannot be proposed

def test_there_is_no_field_for_changing_permissions(gary):
    """`modify_permissions` is red, and rather than instruct Gary not to,
    the payload simply has nowhere to put a tool."""
    reorg_ready(gary)

    with pytest.raises(ValueError, match="change"):
        propose(gary, {"agent_id": "susan", "change": "tools",
                       "to": "spend_money", "reason": "She needs it for research"})

    with pytest.raises(ValueError):
        propose(gary, {**change(), "can_delegate": True})


def test_gary_cannot_reorganise_himself(gary):
    reorg_ready(gary)

    with pytest.raises(ReorganisationError, match="cannot reorganise himself"):
        propose(gary, change(agent_id="gary", to="Chief Executive"))


def test_an_unknown_colleague_is_refused(gary):
    reorg_ready(gary)

    with pytest.raises(NotFoundError, match="not a current employee"):
        propose(gary, change(agent_id="nobody"))


def test_a_change_that_changes_nothing_is_refused(gary):
    service, registry = reorg_ready(gary)
    current = registry.get("susan").title

    with pytest.raises(ReorganisationError, match="changes nothing"):
        propose(gary, change(kind="title", to=current))


def test_a_reporting_loop_is_refused(gary):
    """A chart where everyone reports into a circle answers to nobody."""
    reorg_ready(gary)

    with pytest.raises(ReorganisationError, match="loops"):
        propose(
            gary,
            change("susan", "reports_to", "dave"),
            change("dave", "reports_to", "linda"),
            change("linda", "reports_to", "susan"),
        )


def test_nobody_reports_to_themselves(gary):
    reorg_ready(gary)

    with pytest.raises(ReorganisationError, match="report to themselves"):
        propose(gary, change("susan", "reports_to", "susan"))


def test_reporting_to_an_unknown_agent_is_refused(gary):
    reorg_ready(gary)

    with pytest.raises(NotFoundError, match="not a current agent"):
        propose(gary, change("susan", "reports_to", "nobody"))


def test_two_conflicting_changes_to_one_person_are_refused(gary):
    reorg_ready(gary)

    with pytest.raises(ReorganisationError, match="conflicting"):
        propose(gary, change("susan", "title", "Head of Research"),
                change("susan", "title", "Director of Insight"))


def test_without_github_a_reorganisation_cannot_be_proposed(gary):
    reorg_ready(gary, engineering=False)

    with pytest.raises(ReorganisationError, match="not configured"):
        propose(gary)


# ------------------------------------------------- what a proposal does

def test_a_reorganisation_waits_for_alex(gary):
    reorg_ready(gary)

    result = propose(gary)

    assert result["status"] == "awaiting_approval"
    assert result["risk_level"] == "yellow"
    assert tickets(gary) == [], "nothing is filed until Alex approves"


def test_approving_files_one_ticket_with_the_diff(gary):
    service, registry = reorg_ready(gary)
    before = registry.get("susan").title

    result = approve(gary, propose(gary, change("susan", "title", "Head of Research")))

    assert result["execution"]["status"] == "succeeded"
    assert result["execution"]["result"]["applied"] is False
    assert len(tickets(gary)) == 1

    body = gary.github_for_tests.issue_body(1)
    assert before in body and "Head of Research" in body, "the before and after"
    assert "allowed_tools is unchanged" in body
    assert "can_delegate is unchanged" in body
    assert "roster.py" in body


def test_the_roster_does_not_move_until_alex_lands_it(gary):
    """The database cannot be used to route around roster.py: sync_roster
    overwrites it from code on every startup."""
    service, registry = reorg_ready(gary)
    before = {
        d.agent_id: (d.title, d.department, d.reports_to, tuple(d.allowed_tools), d.can_delegate)
        for d in registry.employees()
    }

    approve(gary, propose(gary, change("susan", "title", "Head of Research")))
    service.sync_roster()
    registry.refresh(force=True)

    after = {
        d.agent_id: (d.title, d.department, d.reports_to, tuple(d.allowed_tools), d.can_delegate)
        for d in registry.employees()
    }
    assert after == before


def test_a_github_failure_changes_nothing_and_is_recorded_as_failed(gary):
    github = FakeGitHub()
    github.fail["create_issue"] = (500, "server error")
    service, registry = reorg_ready(gary, github=github)

    result = approve(gary, propose(gary))

    assert result["execution"]["status"] == "failed"
    # The ticket row is reserved before the issue is created, so a retry
    # cannot open a second issue for the same task. What must not exist is
    # an issue, and the row must not claim to be synced.
    assert [t["github_issue_number"] for t in tickets(gary)] == [None]
    assert tickets(gary)[0]["sync_state"] != "synced"
    with gary.db.read() as conn:
        events = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM audit_log WHERE entity_id = 'roster' ORDER BY id"
            )
        ]
    assert "reorganisation_requested" not in events


def test_a_focus_change_is_what_actually_changes_behaviour(gary):
    """Titles and reporting lines move the chart; focus feeds the prompt."""
    reorg_ready(gary)

    result = approve(
        gary,
        propose(gary, change("susan", "focus",
                             "Competitive analysis of other AI assistant channels")),
    )

    assert result["execution"]["status"] == "succeeded"
    assert "Competitive analysis" in gary.github_for_tests.issue_body(1)


# ------------------------------------------------ proposing it unprompted

def cycle_change(agent_id="susan", kind="title", to="Head of Research"):
    return {
        "action_type": "propose_reorganisation",
        "changes": [
            {"agent_id": agent_id, "change": kind, "to": to,
             "reason": "Her record shows the most completed work"}
        ],
        "rationale": "The company is organised around assumptions that no longer hold.",
        "reason": "The record supports a different shape",
    }


def validate(gary, actions, now=None):
    import datetime as dt

    from gary.services.calendar_blocks import WorkWeek
    from gary.services.planning_cycle import validate_cycle_actions

    from conftest import START, iso

    return validate_cycle_actions(
        gary,
        actions,
        now=now or iso(START),
        busy=[],
        calendar_available=True,
        week=WorkWeek(),
        horizon_hours=72,
        max_actions=10,
        day_start=iso(START - dt.timedelta(hours=14)),
    )


def test_a_cycle_reorganises_once_at_most(gary):
    reorg_ready(gary)

    accepted, rejected = validate(gary, [cycle_change("susan"), cycle_change("dave")])

    assert len(accepted) == 1
    assert rejected[0]["reason"] == "at most 1 reorganisation per cycle"


def test_a_cycle_will_not_reshuffle_the_company_twice_in_a_fortnight(gary):
    """A company that reorganises itself weekly is malfunctioning."""
    from gary.services.planning_cycle import REORG_QUIET_DAYS

    reorg_ready(gary)
    approve(gary, propose(gary))

    accepted, rejected = validate(gary, [cycle_change("dave", to="Head of Security")])

    assert accepted == []
    assert rejected[0]["reason"] == (
        f"the company was reorganised in the last {REORG_QUIET_DAYS} days"
    )


def test_a_cycle_cannot_smuggle_in_a_permission_change(gary):
    """The cycle branch keeps only the four allowed kinds, so a proposal
    asking for tools is dropped rather than passed along."""
    reorg_ready(gary)

    accepted, rejected = validate(
        gary,
        [{
            "action_type": "propose_reorganisation",
            "changes": [{"agent_id": "susan", "change": "tools", "to": "spend_money",
                         "reason": "She says she needs it"}],
            "rationale": "Susan should be able to buy her own research tools.",
            "reason": "She asked",
        }],
    )

    assert accepted == []
    assert rejected[0]["reason"] == "no valid changes in that reorganisation"


def test_a_cycle_needs_a_rationale(gary):
    reorg_ready(gary)

    accepted, rejected = validate(
        gary, [{**cycle_change(), "rationale": "because"}]
    )

    assert accepted == []
    assert rejected[0]["reason"] == "a reorganisation needs a rationale"


def test_the_planner_is_given_the_teams_record(gary):
    reorg_ready(gary)

    scorecards = gary.planning.snapshot()["team_scorecards"]

    assert {s["subject"] for s in scorecards} >= {"susan", "dave", "linda"}
    assert all("completion_rate" in s for s in scorecards)

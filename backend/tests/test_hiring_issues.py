"""What Gary can add to the GitHub issues for new colleagues.

Four things: the first issue cites the company's own record, a cycle may
propose a colleague unattended (under caps, still waiting on Alex), Gary can
add to a specification he already filed, and once the colleague exists their
record is posted back to the issue that asked for them.
"""

import datetime as dt

import pytest

from conftest import START, iso, make_task, run
from gary.db.repositories import Repositories
from gary.services.calendar_blocks import WorkWeek
from gary.services.engineering_service import EngineeringError
from gary.services.hiring_actions import hire_spec, hiring_evidence
from gary.services.hiring_followup import follow_up_on_hires, hire_comment
from gary.services.planning_cycle import (
    HIRE_QUIET_DAYS,
    MAX_CYCLE_HIRES,
    validate_cycle_actions,
)
from test_engineering import build_service, create


def hire_proposal(**overrides) -> dict:
    proposal = {
        "action_type": "hire_employee",
        "agent_id": "nina",
        "name": "Nina",
        "title": "Director of Audience Insight",
        "department": "Audience",
        "notebook": "Nina",
        "specialty": "Reads viewer retention and comments, and says what to make next.",
        "capability_gap": "Nobody can read audience data, so every topic decision is a guess.",
        "tools": ["read_projects", "web_search"],
        "reason": "Two assignments failed for want of audience data.",
    }
    proposal.update(overrides)
    return proposal


def validate(gary, actions, now=None):
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


def sync_roster(gary, registry) -> None:
    """The agents table mirrors the roster; an assignment references it."""
    now = iso(START)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        for definition in registry.all():
            repos.agents.upsert(
                agent_id=definition.agent_id,
                name=definition.name,
                title=definition.title,
                department=definition.department,
                reports_to=definition.reports_to,
                is_employee=definition.agent_id != "gary",
                active=True,
                now=now,
            )


# ------------------------------------------------------- the first issue


def test_the_hire_issue_cites_the_companys_own_record(gary):
    gary.conversation.announce("Who should present this?", expects_reply=True, kind="question")

    with gary.db.read() as conn:
        evidence = hiring_evidence(Repositories.bind(conn), since=iso(START - dt.timedelta(days=30)))

    assert "Assignments in the last 30 days: 0, none to anybody" in evidence
    assert "Questions Gary asked and nobody has answered: 1" in evidence


def test_the_evidence_goes_into_the_specification():
    from gary.models.hiring import HireEmployeePayload

    payload = HireEmployeePayload(**{
        k: v for k, v in hire_proposal().items()
        if k not in ("action_type", "reason")
    })
    spec = hire_spec(payload, ["read_projects"], ["Assignments that did not finish: 2"])

    assert spec["title"] == "Hire Nina as Director of Audience Insight"
    assert "What the company's own record shows: Assignments that did not finish: 2." in spec["objective"]
    # Gary's own argument is still there, ahead of the numbers.
    assert "The gap: Nobody can read audience data" in spec["objective"]


# --------------------------------------------------- a cycle may propose


def test_a_cycle_may_propose_one_colleague(gary):
    accepted, rejected = validate(gary, [hire_proposal()])

    assert len(accepted) == 1
    assert accepted[0]["action_type"] == "hire_employee"
    assert accepted[0]["payload"]["agent_id"] == "nina"
    assert accepted[0]["payload"]["tools"] == ["read_projects", "web_search"]
    assert rejected == []


def test_a_cycle_proposes_at_most_one(gary):
    accepted, rejected = validate(
        gary, [hire_proposal(), hire_proposal(agent_id="omar", name="Omar", notebook="Omar")]
    )

    assert len(accepted) == MAX_CYCLE_HIRES
    assert rejected[0]["reason"] == "at most 1 hire proposal per cycle"


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"capability_gap": "we need one"}, "a hire needs the gap it fills, from the record"),
        ({"tools": []}, "a hire needs the tools it would hold"),
        ({"name": ""}, "a hire needs name"),
    ],
)
def test_a_vague_proposal_is_refused(gary, overrides, reason):
    accepted, rejected = validate(gary, [hire_proposal(**overrides)])
    assert accepted == []
    assert rejected[0]["reason"] == reason


def test_a_cycle_will_not_propose_while_one_is_waiting_for_alex(gary, monkeypatch):
    def pending(self):
        return [{"action_type": "hire_employee", "summary": "Hire Nina"}]

    monkeypatch.setattr(
        "gary.db.repositories.approvals.ApprovalRepository.list_pending", pending
    )
    accepted, rejected = validate(gary, [hire_proposal()])

    assert accepted == []
    assert rejected[0]["reason"] == "a hire is already waiting for Alex to decide"


def test_a_cycle_will_not_propose_two_colleagues_in_a_fortnight(gary):
    with gary.db.transaction() as conn:
        Repositories.bind(conn).actions.create(
            action_type="hire_employee",
            payload={"agent_id": "omar"},
            risk_level="yellow",
            status="awaiting_approval",
            now=iso(START - dt.timedelta(days=HIRE_QUIET_DAYS - 1)),
        )

    accepted, rejected = validate(gary, [hire_proposal()])
    assert accepted == []
    assert rejected[0]["reason"] == f"a colleague was proposed in the last {HIRE_QUIET_DAYS} days"


# ------------------------------------------------- adding to an issue


def test_gary_can_add_to_a_specification_he_already_filed(gary):
    service, github = build_service(gary)
    ticket = create(service, make_task(gary, title="Hire Nina as Director of Audience Insight"))
    original = github.issue_body(1)

    result = run(
        service.extend_spec(
            service.resolve(ticket_id=ticket.id),
            requirements=["Nina also needs read_relevant_notes"],
            acceptance_criteria=["Nina can read her own notebook"],
            note="Her first week showed she cannot see the planning notes.",
        )
    )

    assert result == {
        "issue_number": 1,
        "url": ticket.github_url,
        "added_requirements": 1,
        "added_acceptance_criteria": 1,
    }
    body = github.issue_body(1)
    # Appended: what Alex already agreed to is still there, first.
    assert body.startswith(original.rstrip()[:200])
    assert "## Added by Gary" in body
    assert "Nina also needs read_relevant_notes" in body
    assert "Nina can read her own notebook" in body
    assert "Her first week showed she cannot see the planning notes." in body


def test_an_addition_cannot_carry_a_secret(gary):
    service, github = build_service(gary)
    ticket = create(service, make_task(gary, title="Hire Nina"))

    run(
        service.extend_spec(
            service.resolve(ticket_id=ticket.id),
            note="Use token ghp_0123456789abcdefghijklmnopqrstuvwxyz for the API",
        )
    )
    body = github.issue_body(1)
    assert "ghp_0123456789" not in body
    assert "[redacted]" in body


def test_a_finished_ticket_is_not_extended(gary):
    service, github = build_service(gary)
    ticket = create(service, make_task(gary, title="Hire Nina"))
    github.issues[1]["state"] = "closed"

    with pytest.raises(EngineeringError, match="closed"):
        run(service.extend_spec(service.resolve(ticket_id=ticket.id), note="One more thing"))


# --------------------------------------------- following the hire through


def test_the_hire_issue_gets_the_colleagues_record(gary, clock):
    service, github = build_service(gary, clock=clock)
    ticket = create(service, make_task(gary, title="Hire Susan as Head of Research"))

    # The proposal that filed this issue, and the work Susan has since done.
    from gary.agents.roster import AgentRegistry

    registry = AgentRegistry()
    sync_roster(gary, registry)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        repos.audit.write(
            "alex", "employee_requested", "Ticket opened to build Susan", "agent", "susan",
            {"issue_number": ticket.github_issue_number}, now=iso(START),
        )
        assignment = repos.assignments.create(
            assigned_by="gary", assigned_to="susan", objective="Research the topic",
            now=iso(START),
        )
        repos.assignments.transition(
            assignment["id"], assignment["status"], "completed", completed_at=iso(START)
        )

    commented = run(follow_up_on_hires(gary, service, registry, clock))

    assert commented == [{"agent_id": "susan", "issue_number": 1}]
    posted = github.comments[-1]["body"]
    assert "Susan has been working" in posted
    assert "assignments: 1 (1 completed, 0 failed)" in posted
    assert "No model wrote this." in posted

    # Once per hire.
    assert run(follow_up_on_hires(gary, service, registry, clock)) == []


def test_a_colleague_who_does_not_exist_yet_is_not_reported_on(gary, clock):
    service, github = build_service(gary, clock=clock)
    ticket = create(service, make_task(gary, title="Hire Nina as Director of Audience Insight"))
    with gary.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            "alex", "employee_requested", "Ticket opened to build Nina", "agent", "nina",
            {"issue_number": ticket.github_issue_number}, now=iso(START),
        )

    from gary.agents.roster import AgentRegistry

    assert run(follow_up_on_hires(gary, service, AgentRegistry(), clock)) == []
    assert github.comments == []


def test_the_comment_is_arithmetic_not_judgment():
    comment = hire_comment(
        "Nina",
        {
            "assignments": 4, "completed": 3, "failed": 1,
            "mean_stated_confidence": 0.82, "confident_failures": 1,
            "tool_denials": 2, "total_cost_usd": 0.4211,
        },
    )
    assert "- assignments: 4 (3 completed, 1 failed)" in comment
    assert "- confidence they reported, on average: 82%" in comment
    assert "- confident failures: 1" in comment
    assert "- tool calls refused by the gateway: 2" in comment
    assert "- cost of their model calls: $0.42" in comment

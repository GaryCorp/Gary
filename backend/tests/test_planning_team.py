"""The autonomous loop: a scheduled planning cycle using the GaryCorp team.

Between conversations Gary runs on his own, so these check what he may start
unattended — new tasks, assignments, reviews — and the caps and loop
protection that stop a cycle commissioning work all day.
"""

import datetime as dt

import pytest

from gary.db.repositories import Repositories
from gary.services.planning_cycle import (
    MAX_CYCLE_DELEGATIONS,
    MAX_DAILY_DELEGATIONS,
    PlanningCycle,
    looks_like_repeat,
    objective_key,
)
from gary.services.team_actions import team_action_handlers

from conftest import iso, make_project, make_task, run
from test_planning_cycle import FakeCalendar, FakeEmail, FakeNotebook, FakePlanner

EMPLOYEES = (
    ("susan", "Susan", "Director of Research & Strategy", "Research"),
    ("dave", "Dave", "Director of Security", "Security"),
    ("linda", "Linda", "Director of Operations", "Operations"),
    ("catherine", "Catherine", "Chief Financial Officer", "Finance"),
    ("lauren", "Lauren", "Director of Ethics", "Ethics"),
)


class FakeTeam:
    """Stands in for AgentService: the roster, what is running, and reports."""

    def __init__(self, assignments=None):
        self.assignments = assignments or []
        self.delegated = []
        self.reviews = []

    def team(self) -> dict:
        return {
            "members": [
                {"agent_id": agent_id, "name": name, "title": title, "department": department,
                 "can_delegate": False, "status": "active", "assignments": {}}
                for agent_id, name, title, department in EMPLOYEES
            ]
            + [{"agent_id": "gary", "name": "Gary", "title": "Chief of Staff",
                "department": "Executive", "can_delegate": True, "status": "active"}]
        }

    def list_assignments(self, agent_id=None, status=None, limit=10):
        return self.assignments[:limit]

    # The AgentService calls the handlers use.
    @property
    def registry(self):
        return self

    def employee(self, agent_id):
        for known, name, title, _ in EMPLOYEES:
            if known == agent_id:
                return type("Definition", (), {"agent_id": known, "name": name, "title": title})
        from gary.agents.roster import UnknownAgentError

        raise UnknownAgentError(f"Unknown agent {agent_id!r}")

    async def delegate(self, request, assigned_by="gary"):
        self.delegated.append(request)
        assignment = {"id": f"assign-{len(self.delegated)}", "assigned_to": request.agent_id}
        return assignment

    async def start_review(self, request, requested_by="gary"):
        self.reviews.append(request)
        return {
            "review": {"id": f"review-{len(self.reviews)}"},
            "assignments": [{"id": f"assign-r{i}"} for i, _ in enumerate(request.agents)],
        }


def seed_roster(gary):
    """The org chart rows assignments reference."""
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        repos.agents.upsert("gary", "Gary", "Chief of Staff", "Executive", None, False, True)
        for agent_id, name, title, department in EMPLOYEES:
            repos.agents.upsert(agent_id, name, title, department, "gary", True, True)


def seed_assignment(gary, agent_id, objective, status="completed",
                    now="2026-09-16T12:00:00+00:00"):
    """A real assignment row: busy specialists and repeat detection read the
    database, not the caller's word for it."""
    seed_roster(gary)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        assignment = repos.assignments.create(
            assigned_by="gary", assigned_to=agent_id, objective=objective,
            context=None, priority=5, project_id=None, task_id=None,
            review_id=None, review_round=1, now=now)
        if status != "queued":
            repos.assignments.transition(assignment["id"], "queued", status, started_at=now)
        return assignment


def completed(agent="Susan, Director of Research & Strategy", **overrides):
    assignment = {
        "assignment_id": "assign-done",
        "agent": agent,
        "objective": "Research screen recording tools for the next video.",
        "status": "completed",
        "completed_at": iso(dt.datetime(2026, 9, 16, 13, 30, tzinfo=dt.timezone.utc)),
        "report": {
            "summary": "OBS is the strongest free option.",
            "recommendation": "Use OBS with a hardware encoder.",
            "decisions_needed": ["Whether to buy a capture card"],
        },
    }
    assignment.update(overrides)
    return assignment


def team_cycle(gary, plan, team=None, max_actions=5):
    planner = FakePlanner(plan)
    cycle = PlanningCycle(
        gary, planner, FakeNotebook(), FakeCalendar(), FakeEmail(),
        max_actions=max_actions, team=team or FakeTeam(),
    )
    return cycle, planner


def with_team_handlers(gary, team):
    """Register the delegation handlers the way the application does."""
    gary.actions.handlers.update(team_action_handlers(lambda: team))
    return gary


def delegate(agent_id="susan", objective="Research screen recording tools we could use.", **extra):
    return {"action_type": "delegate_to_agent", "agent_id": agent_id, "objective": objective,
            "project_id": extra.get("project_id", ""), "reason": "Their expertise decides this."}


def review(topic="Should GaryCorp add browser automation?", agents=("susan", "dave")):
    return {"action_type": "run_management_review", "topic": topic, "agents": list(agents),
            "project_id": "", "reason": "It needs more than one department."}


def new_task(title="Draft the episode outline", **extra):
    return {"action_type": "create_internal_task", "title": title,
            "project_id": extra.get("project_id", ""), "estimated_minutes": extra.get("minutes", 60),
            "priority": extra.get("priority", 6), "reason": "The work exists but has no task."}


def statuses(result):
    return [r["result"].get("status") for r in result["results"]]


def rejections(result):
    return [item["reason"] for item in result["rejected"]]


# ------------------------------------------------------------- the loop

def test_cycle_delegates_and_creates_work_on_its_own(gary):
    team = FakeTeam()
    with_team_handlers(gary, team)
    project = make_project(gary, name="Chief of Staff video")
    plan = {
        "summary": "Commission the research and write down the outline work.",
        "briefing": "Susan is looking into recording tools.",
        "actions": [new_task(project_id=project["id"]), delegate(project_id=project["id"])],
    }
    cycle, _ = team_cycle(gary, plan, team)
    result = run(cycle.run("morning"))

    assert statuses(result) == ["succeeded", "succeeded"]
    assert [r.agent_id for r in team.delegated] == ["susan"]
    assert team.delegated[0].objective.startswith("Research screen recording")
    with gary.db.read() as conn:
        titles = [t["title"] for t in Repositories.bind(conn).tasks.list_open()]
    assert "Draft the episode outline" in titles


def test_cycle_runs_a_management_review(gary):
    team = FakeTeam()
    with_team_handlers(gary, team)
    cycle, _ = team_cycle(gary, {"summary": "Ask the team.", "briefing": "", "actions": [review()]}, team)
    result = run(cycle.run("morning"))

    assert statuses(result) == ["succeeded"]
    assert team.reviews[0].agents == ["susan", "dave"]


def test_reports_reach_the_next_cycle(gary):
    team = FakeTeam(assignments=[completed()])
    cycle, planner = team_cycle(gary, {"summary": "s", "briefing": "", "actions": []}, team)
    run(cycle.run("morning"))

    sent = planner.inputs[0]
    assert sent["team"]["employee_ids"] == ["susan", "dave", "linda", "catherine", "lauren"]
    report = sent["department_reports"][0]
    assert report["summary"] == "OBS is the strongest free option."
    assert report["recommendation"] == "Use OBS with a hardware encoder."
    assert report["decisions_needed"] == ["Whether to buy a capture card"]


def test_work_in_progress_is_visible_and_not_duplicated(gary):
    running = {"assignment_id": "a1", "agent": "Susan, Director of Research & Strategy",
               "objective": "Research screen recording tools for the next video.",
               "status": "running", "completed_at": None, "report": None}
    team = FakeTeam(assignments=[running])
    with_team_handlers(gary, team)
    seed_assignment(gary, "susan", running["objective"], status="running")
    cycle, planner = team_cycle(gary, {"summary": "s", "briefing": "", "actions": [delegate()]}, team)
    result = run(cycle.run("morning"))

    assert planner.inputs[0]["team"]["working_on"][0]["status"] == "running"
    assert result["results"] == []
    assert "already has an assignment in progress" in rejections(result)[0]
    assert team.delegated == []


def test_the_same_question_is_not_commissioned_twice(gary):
    done = completed(status="completed")
    team = FakeTeam(assignments=[done])
    with_team_handlers(gary, team)
    # Same work, different words.
    repeat = delegate(objective="Research which screen recording tools we should use for videos.")
    cycle, _ = team_cycle(gary, {"summary": "s", "briefing": "", "actions": [repeat]}, team)
    seed_assignment(gary, "susan", done["objective"], status="completed")

    result = run(cycle.run("morning"))
    assert team.delegated == []
    assert "very like this" in rejections(result)[0]


def test_delegation_is_capped_per_cycle(gary):
    team = FakeTeam()
    with_team_handlers(gary, team)
    plan = {"summary": "s", "briefing": "", "actions": [
        delegate("susan"), delegate("dave", "Threat-model the approvals page for us."),
        delegate("linda", "Plan the filming schedule for next week."),
    ]}
    cycle, _ = team_cycle(gary, plan, team)
    result = run(cycle.run("morning"))

    assert len(team.delegated) == MAX_CYCLE_DELEGATIONS
    assert f"at most {MAX_CYCLE_DELEGATIONS} assignments per cycle" in rejections(result)[0]


def test_daily_delegation_cap_counts_earlier_cycles(gary):
    team = FakeTeam()
    with_team_handlers(gary, team)
    for index in range(MAX_DAILY_DELEGATIONS):
        seed_assignment(gary, "catherine", f"Earlier unrelated errand number {index}",
                        status="completed", now="2026-09-16T08:00:00+00:00")
    cycle, _ = team_cycle(gary, {"summary": "s", "briefing": "", "actions": [delegate()]}, team)
    result = run(cycle.run("morning"))

    assert team.delegated == []
    assert f"at most {MAX_DAILY_DELEGATIONS} assignments a day" in rejections(result)[0]


def test_unknown_specialists_and_short_objectives_are_refused(gary):
    team = FakeTeam()
    with_team_handlers(gary, team)
    plan = {"summary": "s", "briefing": "", "actions": [
        delegate("mallory", "Do something useful for the company please."),
        delegate("dave", "help"),
        review(agents=["susan"]),
    ]}
    cycle, _ = team_cycle(gary, plan, team)
    result = run(cycle.run("morning"))

    assert team.delegated == [] and team.reviews == []
    assert rejections(result) == [
        "unknown specialist 'mallory'",
        "an assignment needs an objective",
        "a review needs at least two specialists",
    ]


def test_without_a_team_delegation_is_refused_not_guessed(gary):
    cycle = PlanningCycle(
        gary, FakePlanner({"summary": "s", "briefing": "", "actions": [delegate()]}),
        FakeNotebook(), FakeCalendar(), FakeEmail(), team=None,
    )
    result = run(cycle.run("morning"))
    assert result["results"] == []
    assert "not available in this deployment" in rejections(result)[0]


def test_duplicate_task_titles_are_refused(gary):
    make_task(gary, title="Draft the episode outline")
    team = FakeTeam()
    with_team_handlers(gary, team)
    cycle, _ = team_cycle(gary, {"summary": "s", "briefing": "", "actions": [new_task()]}, team)
    result = run(cycle.run("morning"))
    assert "already exists" in rejections(result)[0]


def test_a_failing_team_does_not_fail_the_cycle(gary):
    class Broken(FakeTeam):
        def team(self):
            raise RuntimeError("agent service down")

    cycle, planner = team_cycle(gary, {"summary": "s", "briefing": "", "actions": []}, Broken())
    result = run(cycle.run("morning"))
    assert result["summary"] == "s"
    assert planner.inputs[0]["team"] is None


@pytest.mark.parametrize(
    "first, second, repeat",
    [
        ("Research screen recording tools", "Research which screen recording tools to use", True),
        ("Research screen recording tools", "Plan the filming schedule for next week", False),
    ],
)
def test_repeat_detection(first, second, repeat):
    assert looks_like_repeat(objective_key(second), [objective_key(first)]) is repeat

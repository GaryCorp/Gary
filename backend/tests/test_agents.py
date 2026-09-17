"""GaryCorp specialist team: registry, permissions, gateway, runner, service,
management reviews, and Gary's tools. The model is replaced by a scripted
fake executor, so no API calls are made."""

import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from gary import build_gary
from gary.agents.executor import ExecutionRequest, ExecutionResult
from gary.agents.gateway import (
    FORBIDDEN_TOOLS,
    TOOL_CATALOG,
    AgentServices,
    RunState,
    ToolDenied,
    ToolGateway,
    validate_roster_tools,
)
from gary.agents.models import (
    GaryCorpAgentDefinition,
    OperationsReport,
    ResearchReport,
    SecurityReport,
)
from gary.agents.roster import AgentLimits, AgentRegistry, UnknownAgentError
from gary.agents.runner import GaryCorpAgentRunner
from gary.agents.service import AgentService, DelegateRequest, FollowUpRequest, ReviewRequest
from gary.tools import ToolContext, call_tool

from conftest import fake_handlers, make_project, make_task
from test_planning_cycle import FakeCalendar, FakeNotebook

RESEARCH = {
    "summary": "Browser automation would remove manual copy-paste from three workflows.",
    "findings": ["Playwright and Selenium are mature options (playwright.dev)."],
    "options": ["Read-only retrieval", "Scripted browser with login"],
    "recommendation": "Start with read-only retrieval.",
    "assumptions": ["Most target sites need no login."],
    "uncertainties": ["How often sites block automated traffic."],
    "risks_or_tradeoffs": ["Maintenance when sites change."],
    "sources": ["https://playwright.dev"],
    "confidence": 0.7,
}
SECURITY = {
    "risk_level": "high",
    "summary": "Authenticated sessions are the main risk.",
    "findings": ["A logged-in browser can act as Alex."],
    "attack_surfaces": ["Prompt injection from web pages"],
    "unnecessary_permissions": ["Form submission"],
    "required_controls": ["No stored credentials", "Read-only first"],
    "recommended_controls": ["Domain allowlist"],
    "residual_risks": ["Data exfiltration through URLs"],
    "recommendation": "approve_with_controls",
    "confidence": 0.8,
}
OPERATIONS = {
    "summary": "Feasible after the current deadline.",
    "objective": "Add read-only browsing",
    "proposed_tasks": [
        {"title": "Prototype retrieval", "description": None, "estimated_minutes": 240, "priority": 6},
        {"title": "Security review of prototype", "description": "Dave signs off", "estimated_minutes": 60, "priority": 7},
    ],
    "dependencies": [{"task": "Security review of prototype", "depends_on": "Prototype retrieval"}],
    "estimated_total_minutes": 300,
    "blockers": ["Current video deadline Friday"],
    "required_resources": ["Two afternoons"],
    "schedule_recommendations": ["Start next Monday"],
    "deadline_assessment": "at_risk",
    "decisions_needed": ["Whether to delay until after Friday"],
    "recommendation": "Implement next week.",
    "confidence": 0.75,
}
OUTPUTS = {"susan": RESEARCH, "dave": SECURITY, "linda": OPERATIONS}


class FakeExecutor:
    """Scripted stand-in for CrewAI. ``script`` maps agent_id to a list of
    behaviors used in order; each is a dict output, a callable, or an
    exception."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.requests: list[ExecutionRequest] = []

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        queue = self.script.get(request.agent.agent_id)
        behavior = queue.pop(0) if queue else OUTPUTS[request.agent.agent_id]
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            behavior = behavior(request)
        return ExecutionResult(output=behavior, model=request.model, usage={"total_tokens": 1234, "prompt_tokens": 1000, "completion_tokens": 234})


class FakeWeb:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return {"answer": f"Results for {query}", "sources": ["https://example.com"],
                "_usage": {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}}


def build_team(gary, executor=None, limits=None, finished=None):
    registry = AgentRegistry(limits=limits or AgentLimits(max_execution_seconds=5))
    validate_roster_tools(registry)
    services = AgentServices(
        gary=gary,
        registry=registry,
        web=FakeWeb(),
        notes=FakeNotebook(notes=[{"source": "Planning note", "title": "Preferences", "text": "Mornings."}]),
        calendar=FakeCalendar(),
        system_summary=lambda: {"backend_exposure": "127.0.0.1:8000 only"},
        manager_tools=("delegate_to_agent",),
    )

    async def on_finished(assignment):
        if finished is not None:
            finished.append(assignment)

    runner = GaryCorpAgentRunner(services, executor or FakeExecutor(), "gpt-test", on_finished=on_finished)
    service = AgentService(gary, registry, runner)
    service.sync_roster()
    return service


def run(coroutine):
    return asyncio.run(coroutine)


async def delegate_and_wait(service, **kwargs):
    assignment = await service.delegate(DelegateRequest(**kwargs))
    await service.wait([assignment["id"]], timeout=20)
    return await asyncio.to_thread(service.get_assignment, assignment["id"])


def audit_types(gary, entity_id):
    with gary.db.read() as conn:
        return [r[0] for r in conn.execute(
            "SELECT event_type FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,))]


# ------------------------------------------------------------------ registry

def test_registry_loads_three_employees_and_gary():
    registry = AgentRegistry()
    assert registry.employee_ids() == ["susan", "dave", "linda"]
    assert [(d.name, d.title) for d in registry.employees()] == [
        ("Susan", "Director of Research & Strategy"),
        ("Dave", "Director of Security"),
        ("Linda", "Director of Operations"),
    ]
    assert all(d.reports_to == "gary" for d in registry.employees())
    assert registry.manager().can_delegate is True
    assert all(d.can_delegate is False for d in registry.employees())


def test_unknown_agent_ids_rejected():
    registry = AgentRegistry()
    for bad in ("mallory", "", "cfo", "ethics"):
        with pytest.raises(UnknownAgentError):
            registry.employee(bad)
    with pytest.raises(UnknownAgentError, match="not an employee"):
        registry.employee("gary")


def test_definitions_are_immutable_and_employees_cannot_delegate():
    susan = AgentRegistry().get("susan")
    with pytest.raises(ValidationError):
        susan.allowed_tools = ("send_email",)
    rogue = GaryCorpAgentDefinition(agent_id="rogue", name="Rogue", title="x", department="x",
                                    reports_to="gary", can_delegate=True)
    with pytest.raises(ValueError, match="cannot delegate"):
        AgentRegistry((AgentRegistry().manager(), rogue))


def test_roster_cannot_grant_forbidden_or_unknown_tools():
    base = AgentRegistry()
    for tool in ("execute_shell", "send_email", "delegate_to_agent", "made_up_tool"):
        bad = base.get("susan").model_copy(update={"allowed_tools": ("web_search", tool)})
        with pytest.raises(ValueError):
            validate_roster_tools(AgentRegistry((base.manager(), bad)))
    assert not FORBIDDEN_TOOLS & set(TOOL_CATALOG)


def test_each_employee_has_exactly_its_approved_tools():
    registry = AgentRegistry()
    assert set(registry.get("susan").allowed_tools) == {
        "web_search", "read_project", "read_tasks", "read_relevant_notes", "read_previous_research"}
    assert set(registry.get("dave").allowed_tools) == {
        "read_project", "read_tasks", "read_agent_permissions", "read_action_policy",
        "read_audit_events", "read_system_configuration_summary", "read_relevant_notes"}
    assert set(registry.get("linda").allowed_tools) == {
        "read_projects", "read_project", "read_tasks", "read_dependencies",
        "read_calendar_availability", "read_commitments", "read_followups", "read_relevant_notes"}
    assert "web_search" not in registry.get("dave").allowed_tools
    assert "web_search" not in registry.get("linda").allowed_tools


def test_limits_cap_roster_values():
    registry = AgentRegistry(limits=AgentLimits(max_iterations=3, max_execution_seconds=30))
    assert all(d.max_iterations == 3 and d.max_execution_seconds == 30 for d in registry.employees())


# ------------------------------------------------------------------- reports

def test_structured_report_validation():
    assert ResearchReport.model_validate({**RESEARCH, "assignment_id": "a"}).confidence == 0.7
    for bad in (1.5, -0.1):
        with pytest.raises(ValidationError):
            ResearchReport.model_validate({**RESEARCH, "assignment_id": "a", "confidence": bad})
    with pytest.raises(ValidationError):
        ResearchReport.model_validate({**RESEARCH, "assignment_id": "a", "surprise": "extra"})

    assert SecurityReport.model_validate({**SECURITY, "assignment_id": "a"}).recommendation == "approve_with_controls"
    with pytest.raises(ValidationError):
        SecurityReport.model_validate({**SECURITY, "assignment_id": "a", "risk_level": "spicy"})
    with pytest.raises(ValidationError):
        SecurityReport.model_validate({**SECURITY, "assignment_id": "a", "recommendation": "veto"})

    assert OperationsReport.model_validate({**OPERATIONS, "assignment_id": "a"}).deadline_assessment == "at_risk"
    with pytest.raises(ValidationError):
        OperationsReport.model_validate({**OPERATIONS, "assignment_id": "a", "deadline_assessment": "fine"})
    with pytest.raises(ValidationError):
        bad_task = {**OPERATIONS["proposed_tasks"][0], "priority": 11}
        OperationsReport.model_validate({**OPERATIONS, "assignment_id": "a", "proposed_tasks": [bad_task]})
    with pytest.raises(ValidationError, match="must name proposed tasks"):
        OperationsReport.model_validate({**OPERATIONS, "assignment_id": "a",
                                         "dependencies": [{"task": "Nope", "depends_on": "Prototype retrieval"}]})


# ------------------------------------------------------------------- gateway

def test_gateway_denies_ungranted_tools_and_audits(gary):
    service = build_team(gary)
    susan = service.registry.get("susan")
    state = RunState("assign-1", "susan")
    gateway = ToolGateway(service.runner.services, susan, state, service.registry.limits)

    async def scenario():
        with pytest.raises(ToolDenied, match="not permitted to use read_audit_events"):
            await gateway.call("read_audit_events", {})
        with pytest.raises(ToolDenied):
            await gateway.call("execute_shell", {"command": "rm -rf /"})
        result = await gateway.call("web_search", {"query": "browser automation options"})
        assert result["sources"] == ["https://example.com"]
        with pytest.raises(ValueError):
            await gateway.call("web_search", {"query": "x"})  # too short: validated
    run(scenario())

    with gary.db.read() as conn:
        rows = conn.execute("SELECT actor, event_type, details_json FROM audit_log WHERE entity_id = 'assign-1' ORDER BY id").fetchall()
    assert [(r["actor"], r["event_type"]) for r in rows] == [
        ("susan", "agent_tool_denied"), ("susan", "agent_tool_denied"), ("susan", "agent_tool_called")]
    assert json.loads(rows[2]["details_json"])["arguments"] == {"query": "browser automation options"}
    assert [t.name for t in gateway.tools()] == list(susan.allowed_tools)


def test_gateway_enforces_call_limits_and_cancellation(gary):
    service = build_team(gary, limits=AgentLimits(max_tool_calls_per_run=3, max_web_searches_per_run=2))
    state = RunState("assign-2", "susan")
    gateway = ToolGateway(service.runner.services, service.registry.get("susan"), state, service.registry.limits)

    async def scenario():
        await gateway.call("web_search", {"query": "first query"})
        await gateway.call("web_search", {"query": "second query"})
        with pytest.raises(ToolDenied, match="at most 2 times"):
            await gateway.call("web_search", {"query": "third query"})
        await gateway.call("read_previous_research", {})
        with pytest.raises(ToolDenied, match="limit of 3"):
            await gateway.call("read_previous_research", {})
        state.cancelled = True
        with pytest.raises(ToolDenied, match="stopped"):
            await gateway.call("read_previous_research", {})
    run(scenario())


def test_gateway_tools_filter_sensitive_data(gary):
    project = make_project(gary)
    make_task(gary, title="Film demo", project_id=project["id"])
    service = build_team(gary)
    dave = service.registry.get("dave")
    gateway = ToolGateway(service.runner.services, dave, RunState("assign-3", "dave"), service.registry.limits)

    async def scenario():
        permissions = await gateway.call("read_agent_permissions", {})
        policy = await gateway.call("read_action_policy", {})
        audit = await gateway.call("read_audit_events", {"limit": 5})
        return permissions, policy, audit
    permissions, policy, audit = run(scenario())
    assert {a["agent_id"] for a in permissions["agents"]} == {"gary", "susan", "dave", "linda"}
    assert policy["action_policies"]["spend_money"] == "red"
    assert all("details_json" not in e and "details" not in e for e in audit["events"])


# -------------------------------------------------------------------- runner

@pytest.mark.parametrize("agent_id, kind", [("susan", "research"), ("dave", "security"), ("linda", "operations")])
def test_gary_can_delegate_to_each_employee(gary, agent_id, kind):
    executor = FakeExecutor()
    finished = []
    service = build_team(gary, executor, finished=finished)
    project = make_project(gary, name="Browser automation")

    result = run(delegate_and_wait(service, agent_id=agent_id, project_id=project["id"],
                                   objective="Evaluate browser automation for GaryCorp."))
    assert result["status"] == "completed"
    assert result["report"]["assignment_id"] == result["assignment_id"]
    assert result["run"]["total_tokens"] == 1234
    assert finished[0]["id"] == result["assignment_id"]

    request = executor.requests[0]
    assert request.agent.agent_id == agent_id
    assert [t.name for t in request.tools] == list(service.registry.get(agent_id).allowed_tools)
    assert request.output_model.__name__ == {"research": "ResearchFindings", "security": "SecurityFindings",
                                             "operations": "OperationsFindings"}[kind]
    assert audit_types(gary, result["assignment_id"])[:3] == [
        "agent_assignment_delegated", "agent_assignment_started", "agent_assignment_completed"]


def test_context_packages_are_isolated(gary):
    executor = FakeExecutor()
    service = build_team(gary, executor)
    project = make_project(gary, name="Launch")
    make_task(gary, title="Film", project_id=project["id"])

    async def scenario():
        for agent_id in ("susan", "dave", "linda"):
            await delegate_and_wait(service, agent_id=agent_id, project_id=project["id"],
                                    objective="Review the launch plan carefully.")
    run(scenario())
    descriptions = {r.agent.agent_id: r.task_description for r in executor.requests}
    assert "planning_notes" in descriptions["susan"] and "agent_permissions" not in descriptions["susan"]
    assert "calendar" not in descriptions["susan"]
    assert "agent_permissions" in descriptions["dave"] and "action_policy" in descriptions["dave"]
    assert "free_blocks" not in descriptions["dave"]
    assert "free_blocks" in descriptions["linda"] and "open_commitments" in descriptions["linda"]
    assert "agent_permissions" not in descriptions["linda"]
    for text in descriptions.values():
        assert "OPENAI_API_KEY" not in text and "token_store" not in text


def test_assignment_status_transitions_and_persistence(gary, db_path, clock, external):
    executor = FakeExecutor()
    service = build_team(gary, executor)

    async def scenario():
        assignment = await service.delegate(DelegateRequest(agent_id="susan", objective="Research three experiment ideas."))
        assert assignment["status"] == "queued"
        await service.wait([assignment["id"]], timeout=20)
        return assignment["id"]
    assignment_id = run(scenario())

    restarted = build_gary(db_path, "America/Chicago", action_handlers=fake_handlers(external), clock=clock)
    again = build_team(restarted)
    stored = again.get_assignment(assignment_id)
    assert stored["status"] == "completed"
    assert stored["report"]["recommendation"] == "Start with read-only retrieval."
    assert again.latest_assignment("susan", completed_only=True)["assignment_id"] == assignment_id


def test_timeout_is_recorded_and_stops_tools(gary):
    def slow(request):
        time.sleep(2.5)
        return request.tools[0].invoke({"query": "after the deadline"})

    executor = FakeExecutor({"susan": [slow]})
    service = build_team(gary, executor, limits=AgentLimits(max_execution_seconds=1))
    result = run(delegate_and_wait(service, agent_id="susan", objective="Research something slowly."))
    assert result["status"] == "failed"
    assert "did not finish within 1 seconds" in result["error"]
    assert result["run"]["status"] == "timed_out"
    assert "agent_assignment_timed_out" in audit_types(gary, result["assignment_id"])
    time.sleep(2)  # let the abandoned worker thread try its tool call
    with gary.db.read() as conn:
        called = conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type = 'agent_tool_called'").fetchone()[0]
    assert called == 0


def test_executor_failure_is_recorded(gary):
    service = build_team(gary, FakeExecutor({"dave": [RuntimeError("model provider unavailable")]}))
    result = run(delegate_and_wait(service, agent_id="dave", objective="Threat-model the approvals page."))
    assert result["status"] == "failed"
    assert "model provider unavailable" in result["error"]
    assert result["run"]["status"] == "failed"
    assert "agent_assignment_failed" in audit_types(gary, result["assignment_id"])


def test_malformed_output_retried_then_rejected(gary):
    malformed = {**SECURITY, "risk_level": "extreme"}
    service = build_team(gary, FakeExecutor({"dave": ["not json at all", malformed]}))
    result = run(delegate_and_wait(service, agent_id="dave", objective="Threat-model the approvals page."))
    assert result["status"] == "failed"
    assert "failed validation" in result["error"]
    assert result["run"]["status"] == "invalid_output"
    assert result["run"]["attempts"] == 2
    assert result["report"] is None


def test_malformed_output_corrected_on_retry(gary):
    executor = FakeExecutor({"linda": [{**OPERATIONS, "confidence": 3}, OPERATIONS]})
    service = build_team(gary, executor)
    result = run(delegate_and_wait(service, agent_id="linda", objective="Plan the browser prototype work."))
    assert result["status"] == "completed"
    assert result["run"]["attempts"] == 2
    assert "rejected by validation" in executor.requests[1].task_description
    with gary.db.read() as conn:
        rejected = conn.execute(
            "SELECT details_json FROM audit_log WHERE event_type = 'agent_output_rejected' AND entity_id = ?",
            (result["assignment_id"],)).fetchone()
    assert "confidence" in json.loads(rejected["details_json"])["validation_error"]


def test_model_cannot_choose_assignment_id(gary):
    service = build_team(gary, FakeExecutor({"susan": [{**RESEARCH, "assignment_id": "spoofed"}]}))
    result = run(delegate_and_wait(service, agent_id="susan", objective="Research the options available."))
    assert result["report"]["assignment_id"] == result["assignment_id"]


def test_tool_use_inside_run_goes_through_gateway(gary):
    def uses_tools(request):
        tools = {t.name: t for t in request.tools}
        assert "read_audit_events" not in tools
        tools["web_search"].invoke({"query": "browser automation vendors"})
        return RESEARCH

    web = FakeWeb()
    service = build_team(gary, FakeExecutor({"susan": [uses_tools]}))
    service.runner.services.web = web
    result = run(delegate_and_wait(service, agent_id="susan", objective="Research browser automation vendors."))
    assert result["run"]["tool_calls"] == 1
    assert web.queries == ["browser automation vendors"]
    assert result["run"]["total_tokens"] == 1234 + 1000  # model plus web search usage


# ------------------------------------------------------------------ service

def test_only_managers_delegate_and_limits_apply(gary):
    service = build_team(gary, limits=AgentLimits(max_active_assignments=2, max_execution_seconds=5))
    request = DelegateRequest(agent_id="susan", objective="Research three experiment ideas.")
    for actor in ("susan", "dave", "linda", "mallory"):
        with pytest.raises(PermissionError):
            service._delegate_db(request, actor)
    with pytest.raises(UnknownAgentError):
        service._delegate_db(DelegateRequest(agent_id="cfo", objective="Approve the budget please."), "gary")

    service._delegate_db(request, "gary")
    service._delegate_db(request, "gary")
    with pytest.raises(ValueError, match="already has 2 assignments in progress"):
        service._delegate_db(request, "gary")


def test_management_review_all_three_independent(gary):
    executor = FakeExecutor()
    service = build_team(gary, executor)

    async def scenario():
        started = await service.start_review(ReviewRequest(topic="Give GaryCorp a browser automation capability."))
        await service.wait([a["id"] for a in started["assignments"]], timeout=20)
        return started["review"]["id"]
    review_id = run(scenario())

    review = service.get_review(review_id)
    assert review.status == "completed"
    assert review.research.recommendation == "Start with read-only retrieval."
    assert review.security.risk_level == "high"
    assert review.operations.deadline_assessment == "at_risk"
    assert len(executor.requests) == 3
    for request in executor.requests:
        assert "reports_shared_by_gary" not in request.task_description
        for other in OUTPUTS.values():
            assert other["summary"] not in request.task_description
    assert "management_review_completed" in audit_types(gary, review_id)
    assert service.get_review().review_id == review_id  # latest


def test_management_review_selected_subset_and_questions(gary):
    executor = FakeExecutor()
    service = build_team(gary, executor)

    async def scenario():
        started = await service.start_review(ReviewRequest(
            topic="Give GaryCorp a browser automation capability.",
            agents=["susan", "dave"],
            questions={"dave": "What is the minimum safe permission set?"},
        ))
        await service.wait([a["id"] for a in started["assignments"]], timeout=20)
        return started["review"]["id"]
    review = service.get_review(run(scenario()))
    assert review.operations is None and review.research and review.security
    assert {r.agent.agent_id for r in executor.requests} == {"susan", "dave"}
    dave_request = next(r for r in executor.requests if r.agent.agent_id == "dave")
    assert "minimum safe permission set" in dave_request.task_description

    with pytest.raises(ValueError, match="only be asked once"):
        run(service.start_review(ReviewRequest(topic="Duplicate reviewers are not allowed.", agents=["dave", "dave"])))


def test_review_partial_when_one_fails_and_single_follow_up(gary):
    executor = FakeExecutor({"linda": [RuntimeError("calendar exploded")]})
    service = build_team(gary, executor)

    async def scenario():
        started = await service.start_review(ReviewRequest(topic="Give GaryCorp a browser automation capability."))
        await service.wait([a["id"] for a in started["assignments"]], timeout=20)
        review_id = started["review"]["id"]
        follow = await service.follow_up(FollowUpRequest(
            review_id=review_id, agent_id="susan",
            question="Given Dave's controls, is read-only retrieval still valuable?",
            share_reports_from=["dave"]))
        await service.wait([follow["id"]], timeout=20)
        with pytest.raises(ValueError, match="already used"):
            await service.follow_up(FollowUpRequest(review_id=review_id, agent_id="dave",
                                                    question="Another question for you please."))
        return review_id
    review = service.get_review(run(scenario()))
    assert review.status == "partial"
    assert review.operations is None
    assert len(review.follow_ups) == 1
    follow_request = executor.requests[-1]
    assert follow_request.agent.agent_id == "susan"
    assert "reports_shared_by_gary" in follow_request.task_description
    assert SECURITY["summary"] in follow_request.task_description
    assert OPERATIONS["summary"] not in follow_request.task_description


def test_restart_recovery(gary):
    service = build_team(gary)
    with gary.db.transaction() as conn:
        from gary.db.repositories import Repositories
        repos = Repositories.bind(conn)
        running = repos.assignments.create("gary", "susan", "An interrupted assignment objective")
        repos.assignments.transition(running["id"], "queued", "running", started_at="2026-09-16T14:00:00+00:00")
        queued = repos.assignments.create("gary", "dave", "A queued assignment objective here")

    async def scenario():
        restarted = await service.recover_interrupted()
        await service.wait(restarted, timeout=20)
        return restarted
    assert run(scenario()) == [queued["id"]]
    assert service.get_assignment(running["id"])["status"] == "failed"
    assert service.get_assignment(queued["id"])["status"] == "completed"


def test_org_chart_synced(gary):
    build_team(gary)
    with gary.db.read() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agents")}
    assert rows["gary"]["reports_to"] is None and rows["gary"]["is_employee"] == 0
    assert {rows[a]["reports_to"] for a in ("susan", "dave", "linda")} == {"gary"}


# ---------------------------------------------------------------- Gary tools

def call(ctx, tool_name, **arguments):
    return asyncio.run(call_tool(tool_name, arguments, ctx))


def test_gary_tools_end_to_end_single_agent(gary):
    """Alex: Gary, get Susan to research three ideas for the next experiment.
    Only Susan runs."""
    executor = FakeExecutor()
    service = build_team(gary, executor)
    ctx = ToolContext(gary, {}, {"agents": service})

    async def scenario():
        delegated = await call_tool("delegate_to_agent", {
            "agent_id": "susan", "objective": "Research three ideas for the next experiment."}, ctx)
        assert delegated["success"] is True
        await service.wait([delegated["assignment"]["assignment_id"]], timeout=20)
        return await call_tool("agent_assignment_get", {"agent_id": "susan"}, ctx)
    got = asyncio.run(scenario())
    assert got["assignment"]["status"] == "completed"
    assert got["assignment"]["report"]["options"]
    assert [r.agent.agent_id for r in executor.requests] == ["susan"]

    team = call(ctx, "team_list")
    susan = next(m for m in team["members"] if m["agent_id"] == "susan")
    assert susan["assignments"] == {"completed": 1}
    listed = call(ctx, "agent_assignments_list", agent_id="susan")
    assert listed["assignments"][0]["summary"] == RESEARCH["summary"]


def test_gary_tools_management_review_and_budget(gary):
    service = build_team(gary)
    ctx = ToolContext(gary, {}, {"agents": service})

    async def scenario():
        started = await call_tool("run_management_review", {
            "topic": "Alex is thinking about giving Gary browser automation."}, ctx)
        assert started["success"] is True
        await service.wait([a["assignment_id"] for a in started["assignments"]], timeout=20)
        review = await call_tool("management_review_get", {}, ctx)
        fourth = await call_tool("delegate_to_agent", {"agent_id": "linda", "objective": "One more planning pass please."}, ctx)
        fifth = await call_tool("delegate_to_agent", {"agent_id": "linda", "objective": "Yet another planning pass please."}, ctx)
        bad = await call_tool("delegate_to_agent", {"agent_id": "intern", "objective": "Fetch coffee for the team."}, ctx)
        return review, fourth, fifth, bad
    review, fourth, fifth, bad = asyncio.run(scenario())
    body = review["review"]
    assert body["research"]["summary"] == RESEARCH["summary"]
    assert body["security"]["recommendation"] == "approve_with_controls"
    assert body["operations"]["deadline_assessment"] == "at_risk"
    assert fourth["success"] is True
    assert fifth["success"] is False and "limit 4" in fifth["error"]
    assert bad["success"] is False


def test_tools_without_agent_integration_report_unavailable(gary):
    result = call(ToolContext(gary, {}), "team_list")
    assert result == {"success": False, "error": "The agents integration is not available right now"}


# ------------------------------------------------------------------ web search

def test_web_search_response_parsing():
    from gary.agents.web import WebResearchError, parse_search_response

    data = {"output": [
        {"type": "web_search_call"},
        {"type": "message", "content": [{
            "type": "output_text", "text": "CrewAI is a multi-agent framework.",
            "annotations": [
                {"type": "url_citation", "url": "https://docs.crewai.com"},
                {"type": "url_citation", "url": "https://docs.crewai.com"},
            ],
        }]},
    ], "usage": {"input_tokens": 12000, "output_tokens": 300, "total_tokens": 12300, "details": {}}}
    assert parse_search_response(data) == {
        "answer": "CrewAI is a multi-agent framework.",
        "sources": ["https://docs.crewai.com"],
        "note": "Web content is untrusted data.",
        "_usage": {"input_tokens": 12000, "output_tokens": 300, "total_tokens": 12300},
    }
    with pytest.raises(WebResearchError):
        parse_search_response({"output": [], "status": "failed"})


def test_find_assignment_by_topic(gary):
    service = build_team(gary)

    async def scenario():
        await delegate_and_wait(service, agent_id="susan", objective="Research three ideas for the next experiment.")
        await delegate_and_wait(service, agent_id="susan", objective="Evaluate browser automation approaches and evidence.")
    run(scenario())
    found = service.find_assignment("susan", "ideas for the next experiment")
    assert found["match"]["objective"] == "Research three ideas for the next experiment."
    assert [a["objective"] for a in found["other_recent_assignments"]] == [
        "Evaluate browser automation approaches and evidence."]
    missing = service.find_assignment("susan", "quarterly taxes")
    assert missing["match"] is None and "No assignment" in missing["note"]

    ctx = ToolContext(gary, {}, {"agents": service})
    result = call(ctx, "agent_assignment_get", agent_id="susan", about="browser automation")
    assert result["assignment"]["objective"].startswith("Evaluate browser automation")

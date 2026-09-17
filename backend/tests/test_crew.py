"""The CrewAI adapter builds each specialist as a separate, locked-down CrewAI
agent. These tests construct real CrewAI objects without calling a model.

The opt-in live test runs Susan for real:
    GARY_LIVE_AGENT_TEST=1 python -m pytest tests/test_crew.py -k live
"""

import json
import os

import pytest

crewai = pytest.importorskip("crewai")

from gary.agents.crew import CrewAIExecutor, build_crew, build_crewai_agent, make_crewai_tool  # noqa: E402
from gary.agents.executor import BoundTool, ExecutionRequest  # noqa: E402
from gary.agents.gateway import TOOL_CATALOG  # noqa: E402
from gary.agents.models import FINDINGS_MODELS  # noqa: E402
from gary.agents.roster import AgentRegistry  # noqa: E402


def request_for(agent_id: str, calls: list | None = None) -> ExecutionRequest:
    agent = AgentRegistry().get(agent_id)

    def bound(name):
        spec = TOOL_CATALOG[name]

        def invoke(arguments, _name=name):
            if calls is not None:
                calls.append((_name, arguments))
            return {"ok": _name}

        return BoundTool(spec.name, spec.description, spec.args_model, invoke)

    return ExecutionRequest(
        agent=agent,
        task_description="Test assignment",
        expected_output="A report",
        output_model=FINDINGS_MODELS[agent.report_kind],
        tools=[bound(name) for name in agent.allowed_tools],
        model="gpt-5.4-mini",
        max_iterations=agent.max_iterations,
        max_execution_seconds=agent.max_execution_seconds,
    )


def llm():
    return crewai.LLM(model="gpt-5.4-mini", api_key="sk-test-not-used")


@pytest.mark.parametrize("agent_id", ["susan", "dave", "linda", "catherine"])
def test_crewai_agent_is_locked_down(agent_id):
    request = request_for(agent_id)
    agent = build_crewai_agent(request, llm())
    definition = request.agent

    assert agent.role == definition.role
    assert agent.goal == definition.goal
    assert agent.backstory == definition.backstory
    assert agent.allow_delegation is False
    assert agent.allow_code_execution is False
    assert agent.max_iter == definition.max_iterations
    assert agent.max_execution_time == definition.max_execution_seconds
    assert sorted(tool.name for tool in agent.tools) == sorted(definition.allowed_tools)


def test_agents_are_separate_with_distinct_identities():
    agents = {agent_id: build_crewai_agent(request_for(agent_id), llm())
              for agent_id in ("susan", "dave", "linda", "catherine")}
    assert len({a.role for a in agents.values()}) == 4
    assert len({a.backstory for a in agents.values()}) == 4
    assert "web_search" in [t.name for t in agents["susan"].tools]
    assert "web_search" not in [t.name for t in agents["dave"].tools]
    assert "read_audit_events" not in [t.name for t in agents["linda"].tools]


def test_crew_uses_structured_output_and_no_memory():
    crew, agent, task = build_crew(request_for("dave"), llm())
    assert task.output_pydantic is FINDINGS_MODELS["security"]
    assert task.agent is agent
    assert crew.memory is False
    assert crew.planning is False
    assert crew.tracing is False
    assert len(crew.agents) == 1 and len(crew.tasks) == 1


def test_tool_wrapper_only_calls_the_gateway():
    calls = []
    request = request_for("linda", calls)
    tool = make_crewai_tool(next(t for t in request.tools if t.name == "read_calendar_availability"))
    assert tool.args_schema is TOOL_CATALOG["read_calendar_availability"].args_model
    output = tool.run(days=3, min_minutes=60)
    assert json.loads(output) == {"ok": "read_calendar_availability"}
    assert calls == [("read_calendar_availability", {"days": 3, "min_minutes": 60})]


def test_telemetry_disabled_by_default():
    from crewai_core.telemetry import Telemetry

    assert os.environ.get("CREWAI_DISABLE_TELEMETRY") == "true"
    assert os.environ.get("OTEL_SDK_DISABLED") == "true"
    assert Telemetry._is_telemetry_disabled() is True


@pytest.mark.skipif(os.environ.get("GARY_LIVE_AGENT_TEST") != "1", reason="set GARY_LIVE_AGENT_TEST=1 to call the model")
def test_live_susan_structured_report():
    request = request_for("susan")
    request.tools = []
    request.task_description = (
        "Objective: list two widely used open-source browser automation libraries and one "
        "tradeoff between them. Keep it short. Return the report."
    )
    request.model = os.environ.get("GARY_EMPLOYEE_MODEL", "gpt-5.4-mini")
    result = CrewAIExecutor(os.environ["OPENAI_API_KEY"]).run(request)
    report = FINDINGS_MODELS["research"].model_validate(
        result.output.model_dump() if hasattr(result.output, "model_dump") else result.output
    )
    assert report.summary and report.options

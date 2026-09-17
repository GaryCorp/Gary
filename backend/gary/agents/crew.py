"""CrewAI executor: each assignment runs as its own single-agent crew.

Every run builds a fresh CrewAI Agent from the roster definition with:
- its own role, goal, and backstory;
- only the tools the gateway granted it (each call re-checked by the gateway);
- allow_delegation=False and allow_code_execution=False;
- bounded max_iter and max_execution_time;
- CrewAI memory, planning, and knowledge disabled, so nothing is stored
  outside GaryCorp's database;
- structured output through Task(output_pydantic=...).
"""

import json
import logging
import os
from typing import Any

from pydantic import BaseModel, PrivateAttr

from gary.agents.executor import BoundTool, ExecutionRequest, ExecutionResult

logger = logging.getLogger("gary.agents.crew")

# CrewAI sends anonymous telemetry and offers hosted tracing by default. GaryCorp
# keeps assignments local, so both are off before crewai is imported.
# Variable names verified against crewai 1.15.22 (crewai_core.telemetry).
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("CREWAI_DISABLE_VERSION_CHECK", "true")


def _crewai():
    import crewai
    from crewai.tools import BaseTool

    return crewai, BaseTool


def make_crewai_tool(bound: BoundTool):
    """Wrap a gateway-bound tool as a CrewAI tool. The wrapper holds no
    capability of its own: it can only call the gateway."""
    _, BaseTool = _crewai()

    class GaryCorpTool(BaseTool):
        name: str = bound.name
        description: str = bound.description
        args_schema: type[BaseModel] = bound.args_model
        _invoke: Any = PrivateAttr()

        def _run(self, **kwargs) -> str:
            return json.dumps(self._invoke(kwargs), default=str)

    tool = GaryCorpTool()
    tool._invoke = bound.invoke
    return tool


def build_crewai_agent(request: ExecutionRequest, llm):
    crewai, _ = _crewai()
    definition = request.agent
    return crewai.Agent(
        role=definition.role,
        goal=definition.goal,
        backstory=definition.backstory,
        tools=[make_crewai_tool(tool) for tool in request.tools],
        llm=llm,
        allow_delegation=False,
        allow_code_execution=False,
        max_iter=request.max_iterations,
        max_execution_time=request.max_execution_seconds,
        verbose=False,
    )


def build_crew(request: ExecutionRequest, llm):
    crewai, _ = _crewai()
    agent = build_crewai_agent(request, llm)
    task = crewai.Task(
        description=request.task_description,
        expected_output=request.expected_output,
        agent=agent,
        output_pydantic=request.output_model,
    )
    crew = crewai.Crew(
        agents=[agent],
        tasks=[task],
        process=crewai.Process.sequential,
        memory=False,
        planning=False,
        tracing=False,
        verbose=False,
    )
    return crew, agent, task


class CrewAIExecutor:
    def __init__(self, api_key: str, temperature: float | None = None):
        # The key comes from the application's environment, never from files.
        self._api_key = api_key
        self._temperature = temperature

    def _llm(self, model: str):
        crewai, _ = _crewai()
        arguments = {"model": model, "api_key": self._api_key}
        if self._temperature is not None:
            arguments["temperature"] = self._temperature
        return crewai.LLM(**arguments)

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        crew, _, _ = build_crew(request, self._llm(request.model))
        result = crew.kickoff()
        output = result.pydantic if result.pydantic is not None else (result.json_dict or result.raw)
        usage = {}
        metrics = getattr(result, "token_usage", None)
        if metrics is not None:
            data = metrics.model_dump() if hasattr(metrics, "model_dump") else dict(metrics)
            usage = {k: v for k, v in data.items() if isinstance(v, (int, float))}
        return ExecutionResult(output=output, model=request.model, usage=usage)

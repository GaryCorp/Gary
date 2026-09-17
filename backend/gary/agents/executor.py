"""The boundary between GaryCorp and the agent framework.

The runner builds an ExecutionRequest (identity, prompt, granted tools,
output model, limits) and an AgentExecutor runs it as a separate model
execution. CrewAIExecutor (gary/agents/crew.py) is the production executor;
tests use fakes so no model is called.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pydantic import BaseModel

from gary.agents.models import GaryCorpAgentDefinition


@dataclass(frozen=True)
class BoundTool:
    """A granted tool, already bound to the gateway for one run. ``invoke``
    is synchronous and safe to call from the executor's worker thread."""

    name: str
    description: str
    args_model: type[BaseModel]
    invoke: Callable[[dict], Any]


@dataclass
class ExecutionRequest:
    agent: GaryCorpAgentDefinition
    task_description: str
    expected_output: str
    output_model: type[BaseModel]
    tools: list[BoundTool]
    model: str
    max_iterations: int
    max_execution_seconds: int


@dataclass
class ExecutionResult:
    # A model instance, dict, or JSON string; validated by the runner.
    output: Any
    model: str | None = None
    usage: dict = field(default_factory=dict)


class AgentExecutor(Protocol):
    def run(self, request: ExecutionRequest) -> ExecutionResult: ...

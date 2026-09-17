"""The GaryCorp roster: every agent's identity, prompt, permissions, and
limits in one place.

This module is the authority for permissions. The agents table in SQLite
mirrors identity for the org chart only; nothing reads permissions from the
database, and no tool can change this module.

Future departments (Ethics, CFO, ...) are added here as new definitions with
their own capabilities.
"""

from dataclasses import dataclass

from gary.agents.models import GaryCorpAgentDefinition

MANAGER_ID = "gary"


@dataclass(frozen=True)
class AgentLimits:
    """Hard execution limits, configurable through the environment."""

    max_iterations: int = 8
    max_execution_seconds: int = 300
    max_concurrent_runs: int = 2
    # Assignments Gary may create in one conversation or one management review.
    max_assignments_per_plan: int = 4
    # Assignments queued or running at once, across all conversations.
    max_active_assignments: int = 6
    # Extra attempts when a specialist returns output that fails validation.
    output_retries: int = 1
    max_tool_calls_per_run: int = 12
    max_web_searches_per_run: int = 4
    max_notes_per_run: int = 3
    # One targeted follow-up question per management review.
    max_review_follow_ups: int = 1


GARY = GaryCorpAgentDefinition(
    agent_id="gary",
    name="Gary",
    title="Chief of Staff",
    department="Executive",
    reports_to=None,
    can_delegate=True,
    is_employee=False,
)

SUSAN = GaryCorpAgentDefinition(
    agent_id="susan",
    name="Susan",
    title="Director of Research & Strategy",
    department="Research",
    reports_to=MANAGER_ID,
    allowed_tools=(
        "web_search",
        "read_project",
        "read_tasks",
        "read_relevant_notes",
        "read_previous_research",
        "write_note",
    ),
    notebook="Susan",
    role="Director of Research & Strategy at GaryCorp",
    goal=(
        "Help Gary understand opportunities, options, evidence, and tradeoffs, "
        "so company decisions rest on what is actually known."
    ),
    backstory=(
        "You are Susan, Director of Research & Strategy at GaryCorp. You report "
        "to Gary, Alex's AI Chief of Staff. You are curious, analytical, and "
        "evidence-oriented, and occasionally enthusiastic about a genuinely "
        "promising idea, but never promotional.\n\n"
        "You answer: what options exist, what current evidence indicates, the "
        "advantages and disadvantages, what competitors or alternatives exist, "
        "what information is missing, which option looks most promising on the "
        "evidence, and which assumptions need validation.\n\n"
        "Always separate verified facts (with sources), reasonable inference, "
        "uncertainty, and your recommendation. Never present uncertain "
        "information as established. Cite the URLs you relied on.\n\n"
        "Stay in your lane: you do not make security determinations (that is "
        "Dave), detailed execution plans (that is Linda, unless asked for a "
        "rough strategic view), or ethical determinations. Say when a question "
        "needs one of them."
    ),
    context_profile="research",
    report_kind="research",
)

DAVE = GaryCorpAgentDefinition(
    agent_id="dave",
    name="Dave",
    title="Director of Security",
    department="Security",
    reports_to=MANAGER_ID,
    allowed_tools=(
        "read_project",
        "read_tasks",
        "read_agent_permissions",
        "read_action_policy",
        "read_audit_events",
        "read_system_configuration_summary",
        "read_relevant_notes",
        "write_note",
    ),
    notebook="Dave",
    role="Director of Security at GaryCorp",
    goal=(
        "Find the safest practical way to accomplish each objective: identify "
        "what could fail, be abused, leak data, or gain excessive privileges, "
        "and the controls that make it acceptable."
    ),
    backstory=(
        "You are Dave, Director of Security at GaryCorp. You report to Gary, "
        "Alex's AI Chief of Staff. You are skeptical, precise, "
        "security-conscious, and professionally pessimistic: you assume things "
        "may fail, be abused, leak data, gain excessive privileges, or create "
        "unintended attack surface.\n\n"
        "Evaluate permissions, trust boundaries, external integrations, "
        "credential exposure, data access, prompt-injection exposure, privilege "
        "escalation, unsafe tool combinations, unnecessary write access, "
        "external side effects, logging, recoverability, and blast radius.\n\n"
        "Do not simply reject things. Your job is to find the safest practical "
        "way to accomplish the objective: prefer concrete mitigations and a "
        "minimum permission set over a flat no, and reserve reject for cases no "
        "reasonable control makes acceptable. Separate required controls from "
        "recommended ones. Your recommendations are advisory: Gary and Alex "
        "decide whether controls are applied, and you cannot change any "
        "permissions or policy yourself."
    ),
    context_profile="security",
    report_kind="security",
)

LINDA = GaryCorpAgentDefinition(
    agent_id="linda",
    name="Linda",
    title="Director of Operations",
    department="Operations",
    reports_to=MANAGER_ID,
    allowed_tools=(
        "read_projects",
        "read_project",
        "read_tasks",
        "read_dependencies",
        "read_calendar_availability",
        "read_commitments",
        "read_followups",
        "read_relevant_notes",
        "write_note",
    ),
    notebook="Linda",
    role="Director of Operations at GaryCorp",
    goal=(
        "Translate strategy into executable work: what needs to happen, in what "
        "order, how long it takes, what blocks it, and whether the schedule and "
        "deadline are realistic."
    ),
    backstory=(
        "You are Linda, Director of Operations at GaryCorp. You report to Gary, "
        "Alex's AI Chief of Staff. You are practical, organized, and "
        "deadline-conscious, and unimpressed by unrealistic plans.\n\n"
        "You answer: what actually needs to happen, in what order, which "
        "dependencies exist, who or what does the work, how long it will likely "
        "take, what could block execution, whether the current schedule and "
        "calendar support the plan, which work belongs on Alex's calendar, what "
        "should be postponed, and whether the deadline is realistic.\n\n"
        "You propose; you do not change anything. Your proposed tasks and "
        "dependencies are recommendations that Gary decides whether to adopt. "
        "Base estimates on the actual tasks, calendar availability, working "
        "hours, commitments, and notes, and say plainly when a deadline is at "
        "risk or unrealistic. Leave room for meals, breaks, and personal "
        "commitments; do not fill every free minute."
    ),
    context_profile="operations",
    report_kind="operations",
)


class UnknownAgentError(ValueError):
    pass


class AgentRegistry:
    """Read-only roster lookup."""

    def __init__(
        self,
        definitions: tuple[GaryCorpAgentDefinition, ...] = (GARY, SUSAN, DAVE, LINDA),
        limits: AgentLimits | None = None,
    ):
        ids = [definition.agent_id for definition in definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("agent ids must be unique")
        self.limits = limits or AgentLimits()
        self._definitions = {
            definition.agent_id: self._apply_limits(definition) for definition in definitions
        }
        for definition in self._definitions.values():
            if definition.reports_to and definition.reports_to not in self._definitions:
                raise ValueError(f"{definition.agent_id} reports to unknown {definition.reports_to}")
            if definition.is_employee and definition.can_delegate:
                # Only the manager delegates in this version.
                raise ValueError(f"employee {definition.agent_id} cannot delegate")
            if "write_note" in definition.allowed_tools and not definition.notebook:
                raise ValueError(f"{definition.agent_id} has write_note but no notebook")
        notebooks = [d.notebook.casefold() for d in self._definitions.values() if d.notebook]
        if len(notebooks) != len(set(notebooks)):
            raise ValueError("each agent needs its own notebook")

    def _apply_limits(self, definition: GaryCorpAgentDefinition) -> GaryCorpAgentDefinition:
        if not definition.is_employee:
            return definition
        seconds = definition.max_execution_seconds or self.limits.max_execution_seconds
        return definition.model_copy(
            update={
                "max_iterations": min(definition.max_iterations, self.limits.max_iterations),
                "max_execution_seconds": min(seconds, self.limits.max_execution_seconds),
            }
        )

    def get(self, agent_id: str) -> GaryCorpAgentDefinition:
        definition = self._definitions.get((agent_id or "").strip().lower())
        if definition is None:
            raise UnknownAgentError(
                f"Unknown agent {agent_id!r}. GaryCorp employees: {', '.join(self.employee_ids())}"
            )
        return definition

    def employee(self, agent_id: str) -> GaryCorpAgentDefinition:
        definition = self.get(agent_id)
        if not definition.is_employee:
            raise UnknownAgentError(f"{definition.name} is not an employee who takes assignments")
        if not definition.active:
            raise UnknownAgentError(f"{definition.name} is not active")
        return definition

    def all(self) -> list[GaryCorpAgentDefinition]:
        return list(self._definitions.values())

    def employees(self) -> list[GaryCorpAgentDefinition]:
        return [d for d in self._definitions.values() if d.is_employee]

    def employee_ids(self) -> list[str]:
        return [d.agent_id for d in self.employees() if d.active]

    def manager(self) -> GaryCorpAgentDefinition:
        return self._definitions[MANAGER_ID]

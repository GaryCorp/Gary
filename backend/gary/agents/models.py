"""Agent definitions and structured reports.

Each report has two layers:
- a *Findings* model the specialist's LLM fills in (plain types, so it maps
  cleanly to structured output), and
- the stored *Report* model, which adds the assignment_id from the
  application (never trusted from the model) and enforces limits.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReportKind = Literal["research", "security", "operations", "finance", "ethics", "advisory"]

LIST_LIMIT = 20
TEXT_LIMIT = 4000
ITEM_LIMIT = 2000


class GaryCorpAgentDefinition(BaseModel):
    """One member of GaryCorp. Frozen: nothing at runtime can change an
    agent's identity or permissions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str
    title: str
    department: str
    reports_to: str | None

    # Capability-based permissions: the exact tool names this agent may call.
    # Authority such as Catherine's request_card_purchase is granted the same
    # way, per agent, never globally.
    allowed_tools: tuple[str, ...] = ()

    can_delegate: bool = False
    is_employee: bool = True

    # The agent's own top-level Joplin notebook. write_note, list_own_notes, and
    # read_own_note always target this notebook; it is never taken from the model.
    notebook: str | None = None

    # Prompt identity (CrewAI role, goal, backstory).
    role: str = ""
    goal: str = ""
    backstory: str = ""

    context_profile: ReportKind | None = None
    report_kind: ReportKind | None = None

    # True for an employee GaryCorp hired for itself, rather than one Alex
    # wrote into the roster. Their capabilities are capped by HIREABLE_TOOLS.
    hired: bool = False

    max_iterations: int = Field(default=8, ge=1, le=25)
    max_execution_seconds: int | None = Field(default=300, ge=1, le=1800)

    active: bool = True


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bounded_items(value: list[str]) -> list[str]:
    if len(value) > LIST_LIMIT:
        raise ValueError(f"at most {LIST_LIMIT} items")
    for item in value:
        if len(item) > ITEM_LIMIT:
            raise ValueError(f"items must be at most {ITEM_LIMIT} characters")
    return value


# ------------------------------------------------------------------ research

class ResearchFindings(ReportModel):
    summary: str
    findings: list[str]
    options: list[str]
    recommendation: str | None
    assumptions: list[str]
    uncertainties: list[str]
    risks_or_tradeoffs: list[str]
    sources: list[str]
    confidence: float


class ResearchReport(ResearchFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)

    @field_validator("findings", "options", "assumptions", "uncertainties",
                     "risks_or_tradeoffs", "sources")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)


# ------------------------------------------------------------------ security

RiskLevel = Literal["low", "medium", "high", "critical"]
SecurityRecommendation = Literal["approve", "approve_with_controls", "revise", "reject"]


class SecurityFindings(ReportModel):
    risk_level: RiskLevel
    summary: str
    findings: list[str]
    attack_surfaces: list[str]
    unnecessary_permissions: list[str]
    required_controls: list[str]
    recommended_controls: list[str]
    residual_risks: list[str]
    recommendation: SecurityRecommendation
    confidence: float


class SecurityReport(SecurityFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)

    @field_validator("findings", "attack_surfaces", "unnecessary_permissions",
                     "required_controls", "recommended_controls", "residual_risks")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)


# ---------------------------------------------------------------- operations

DeadlineAssessment = Literal["comfortable", "achievable", "at_risk", "unrealistic", "unknown"]


class ProposedTask(ReportModel):
    title: str
    description: str | None
    estimated_minutes: int | None
    priority: int


class ProposedDependency(ReportModel):
    task: str
    depends_on: str


class OperationsFindings(ReportModel):
    summary: str
    objective: str
    proposed_tasks: list[ProposedTask]
    dependencies: list[ProposedDependency]
    estimated_total_minutes: int | None
    blockers: list[str]
    required_resources: list[str]
    schedule_recommendations: list[str]
    deadline_assessment: DeadlineAssessment
    decisions_needed: list[str]
    recommendation: str
    confidence: float


class ValidProposedTask(ProposedTask):
    title: str = Field(min_length=1, max_length=300)
    estimated_minutes: int | None = Field(ge=0, le=100_000)
    priority: int = Field(ge=1, le=10)


class OperationsReport(OperationsFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    objective: str = Field(min_length=1, max_length=TEXT_LIMIT)
    proposed_tasks: list[ValidProposedTask] = Field(max_length=30)
    dependencies: list[ProposedDependency] = Field(max_length=60)
    estimated_total_minutes: int | None = Field(ge=0, le=1_000_000)
    recommendation: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)

    @field_validator("blockers", "required_resources", "schedule_recommendations",
                     "decisions_needed")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)

    @field_validator("dependencies")
    @classmethod
    def dependencies_name_proposed_tasks(cls, value, info):
        titles = {task.title.casefold() for task in info.data.get("proposed_tasks", [])}
        if titles:
            for dependency in value:
                if dependency.task.casefold() not in titles or dependency.depends_on.casefold() not in titles:
                    raise ValueError(
                        f"dependency {dependency.task!r} -> {dependency.depends_on!r} "
                        "must name proposed tasks"
                    )
        return value


# ------------------------------------------------------------------- finance

CostFrequency = Literal["one_time", "monthly", "yearly", "usage_based", "unknown"]
BudgetAssessment = Literal["within_budget", "tight", "over_budget", "unknown"]


class CostItem(ReportModel):
    item: str
    amount_usd: float | None
    frequency: CostFrequency


class FinanceFindings(ReportModel):
    summary: str
    costs: list[CostItem]
    estimated_one_time_cost_usd: float | None
    estimated_monthly_cost_usd: float | None
    budget_assessment: BudgetAssessment
    savings_opportunities: list[str]
    risks: list[str]
    decisions_needed: list[str]
    recommendation: str
    confidence: float


class ValidCostItem(CostItem):
    item: str = Field(min_length=1, max_length=300)
    amount_usd: float | None = Field(ge=0, le=10_000_000)


class FinanceReport(FinanceFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    costs: list[ValidCostItem] = Field(max_length=30)
    estimated_one_time_cost_usd: float | None = Field(ge=0, le=100_000_000)
    estimated_monthly_cost_usd: float | None = Field(ge=0, le=100_000_000)
    recommendation: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)
    # Set by the application from the run, never by the model.
    purchase_request_ids: list[str] = Field(default_factory=list)

    @field_validator("savings_opportunities", "risks", "decisions_needed")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)


# -------------------------------------------------------------------- ethics

EthicalAssessment = Literal["acceptable", "acceptable_with_safeguards", "needs_revision", "unacceptable"]


class EthicsFindings(ReportModel):
    summary: str
    ethical_assessment: EthicalAssessment
    stakeholders: list[str]
    ethical_concerns: list[str]
    options_considered: list[str]
    recommended_option: str
    safeguards: list[str]
    where_you_differ_from_ease: list[str]
    value_judgments_for_alex: list[str]
    uncertainties: list[str]
    confidence: float


class EthicsReport(EthicsFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    recommended_option: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)
    # Set by the application from the run, never by the model: how many EASE
    # analyses completed, so a report that skipped the framework is visible.
    ease_analyses: int = Field(default=0, ge=0)

    @field_validator("stakeholders", "ethical_concerns", "options_considered", "safeguards",
                     "where_you_differ_from_ease", "value_judgments_for_alex", "uncertainties")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)


# ------------------------------------------------------------------ advisory
# The report a hired employee returns: general enough for any specialty Gary
# invents, still structured and validated like every other report.


class AdvisoryFindings(ReportModel):
    summary: str
    findings: list[str]
    recommendation: str
    risks: list[str]
    assumptions: list[str]
    uncertainties: list[str]
    decisions_needed: list[str]
    out_of_scope: list[str]
    sources: list[str]
    confidence: float


class AdvisoryReport(AdvisoryFindings):
    assignment_id: str
    summary: str = Field(min_length=1, max_length=TEXT_LIMIT)
    recommendation: str = Field(min_length=1, max_length=TEXT_LIMIT)
    confidence: float = Field(ge=0, le=1)

    @field_validator("findings", "risks", "assumptions", "uncertainties",
                     "decisions_needed", "out_of_scope", "sources")
    @classmethod
    def bounded(cls, value):
        return _bounded_items(value)


FINDINGS_MODELS: dict[str, type[ReportModel]] = {
    "research": ResearchFindings,
    "security": SecurityFindings,
    "operations": OperationsFindings,
    "finance": FinanceFindings,
    "ethics": EthicsFindings,
    "advisory": AdvisoryFindings,
}
REPORT_MODELS: dict[str, type[ReportModel]] = {
    "research": ResearchReport,
    "security": SecurityReport,
    "operations": OperationsReport,
    "finance": FinanceReport,
    "ethics": EthicsReport,
    "advisory": AdvisoryReport,
}


class ManagementReview(BaseModel):
    review_id: str
    topic: str
    status: str
    research: ResearchReport | None = None
    security: SecurityReport | None = None
    operations: OperationsReport | None = None
    finance: FinanceReport | None = None
    ethics: EthicsReport | None = None
    # Reports from hired employees, by agent id: the review model cannot have
    # a field per specialty Gary might invent.
    advisory: dict[str, AdvisoryReport] = Field(default_factory=dict)
    follow_ups: list[dict] = Field(default_factory=list)
    assignments: list[dict] = Field(default_factory=list)

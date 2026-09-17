"""AgentService: how Gary manages the team.

Only Gary (or Alex through the CLI) creates assignments. Every assignment is
validated against the roster and hard limits, persisted before it runs, and
executed by the runner. Management reviews run specialists independently:
none of them sees another's report unless Gary shares it in the single
allowed follow-up.
"""

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable

from pydantic import Field, field_validator

from gary.agents.models import REPORT_MODELS, ManagementReview
from gary.agents.roster import MANAGER_ID, AgentRegistry, UnknownAgentError
from gary.agents.runner import GaryCorpAgentRunner
from gary.container import Gary
from gary.db.repositories import Repositories
from gary.models.common import EntityId, RequestModel
from gary.services.common import NotFoundError, require_project, require_task
from gary.timeutil import format_utc, to_local, utc_now

logger = logging.getLogger("gary.agents.service")

CONTEXT_JSON_LIMIT = 6000
STOPWORDS = frozenset(
    "the and for with about what did does find found from that this into your their have has "
    "was were are our gary susan dave linda catherine lauren report research review".split()
)
REVIEW_FRAMING = {
    "research": "Evaluate its usefulness, the available approaches, the evidence, and the tradeoffs.",
    "security": "Threat-model it and determine the minimum safe permission set and required controls.",
    "operations": "Assess its operational value, what implementing it would require, and whether the timing is realistic.",
    "finance": "Assess what it would cost up front and over time, whether it fits the budget, and cheaper alternatives. Do not request any purchase.",
    "ethics": "Run the EASE framework on it and assess who it affects, its ethical risks, and the safeguards it needs.",
}


class DelegateRequest(RequestModel):
    agent_id: str = Field(min_length=1, max_length=32)
    objective: str = Field(min_length=10, max_length=2000)
    project_id: EntityId | None = None
    task_id: EntityId | None = None
    context: dict[str, str] | None = None
    priority: int = Field(default=5, strict=True, ge=1, le=10)

    @field_validator("context")
    @classmethod
    def bounded_context(cls, value):
        if value is not None and len(json.dumps(value)) > CONTEXT_JSON_LIMIT:
            raise ValueError(f"context cannot exceed {CONTEXT_JSON_LIMIT} characters")
        return value


class ReviewRequest(RequestModel):
    topic: str = Field(min_length=10, max_length=2000)
    agents: list[str] | None = Field(default=None, max_length=5)
    questions: dict[str, str] | None = None
    project_id: EntityId | None = None
    task_id: EntityId | None = None
    context: dict[str, str] | None = None

    @field_validator("context")
    @classmethod
    def bounded_context(cls, value):
        if value is not None and len(json.dumps(value)) > CONTEXT_JSON_LIMIT:
            raise ValueError(f"context cannot exceed {CONTEXT_JSON_LIMIT} characters")
        return value


class FollowUpRequest(RequestModel):
    review_id: EntityId
    agent_id: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=10, max_length=2000)
    share_reports_from: list[str] = Field(default_factory=list, max_length=3)


def _now() -> str:
    return format_utc(utc_now())


class AgentService:
    def __init__(
        self,
        gary: Gary,
        registry: AgentRegistry,
        runner: GaryCorpAgentRunner,
        schedule: Callable[[Awaitable], object] | None = None,
    ):
        self.gary = gary
        self.registry = registry
        self.runner = runner
        # How runs are started: background tasks in the server; tests and the
        # CLI can await them directly.
        self._schedule = schedule or asyncio.ensure_future
        self._pending: dict[str, asyncio.Future] = {}

    # ----------------------------------------------------------------- roster

    def sync_roster(self) -> None:
        """Mirror roster identity into the org chart table."""
        now = _now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            ordered = sorted(self.registry.all(), key=lambda d: d.reports_to is not None)
            for definition in ordered:
                repos.agents.upsert(
                    definition.agent_id, definition.name, definition.title, definition.department,
                    definition.reports_to, definition.is_employee, definition.active, now,
                )
            repos.agents.deactivate_missing([d.agent_id for d in ordered], now)

    async def recover_interrupted(self) -> list[str]:
        """After a restart: running assignments cannot resume, so they fail;
        queued ones are started again."""
        queued = await asyncio.to_thread(self._recover_db)
        for assignment_id in queued:
            self._start(assignment_id)
        return queued

    def _recover_db(self) -> list[str]:
        now = _now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            for assignment in repos.assignments.list_by_status(("running",)):
                repos.assignments.transition(
                    assignment["id"], "running", "failed", completed_at=now,
                    error_message="Interrupted by an application restart",
                )
                repos.audit.write("system", "agent_assignment_failed",
                                  "Assignment interrupted by an application restart",
                                  "agent_assignment", assignment["id"], {"error": "restart"})
                GaryCorpAgentRunner._refresh_review(repos, assignment["review_id"], now)
            repos.agent_runs.fail_running("Interrupted by an application restart", now)
            return [a["id"] for a in repos.assignments.list_by_status(("queued",))]

    # ------------------------------------------------------------- delegation

    def _start(self, assignment_id: str, shared_reports: list[dict] | None = None):
        future = self._schedule(self.runner.run_assignment(assignment_id, shared_reports))
        if isinstance(future, asyncio.Future):
            self._pending[assignment_id] = future
            future.add_done_callback(lambda _: self._pending.pop(assignment_id, None))
        return future

    def _check_capacity(self, repos: Repositories, new_assignments: int) -> None:
        active = repos.assignments.count_active()
        limit = self.registry.limits.max_active_assignments
        if active + new_assignments > limit:
            raise ValueError(
                f"The team already has {active} assignments in progress (limit {limit}). "
                "Wait for them to finish before delegating more."
            )

    def _create_assignment(self, repos: Repositories, agent_id: str, objective: str, *,
                           assigned_by: str, project_id=None, task_id=None, context=None,
                           priority=5, review_id=None, review_round=1, now: str) -> dict:
        agent = self.registry.employee(agent_id)
        require_project(repos, project_id)
        require_task(repos, task_id)
        assignment = repos.assignments.create(
            assigned_by=assigned_by, assigned_to=agent.agent_id, objective=objective,
            context=context, priority=priority, project_id=project_id, task_id=task_id,
            review_id=review_id, review_round=review_round, now=now,
        )
        repos.audit.write(
            assigned_by, "agent_assignment_delegated",
            f"{assigned_by.capitalize()} delegated to {agent.name}: {objective[:120]}",
            "agent_assignment", assignment["id"],
            {"assigned_to": agent.agent_id, "priority": priority, "project_id": project_id,
             "task_id": task_id, "review_id": review_id, "review_round": review_round},
            now=now,
        )
        return assignment

    async def delegate(self, request: DelegateRequest, assigned_by: str = MANAGER_ID) -> dict:
        assignment = await asyncio.to_thread(self._delegate_db, request, assigned_by)
        self._start(assignment["id"])
        return assignment

    def _delegate_db(self, request: DelegateRequest, assigned_by: str) -> dict:
        self._require_manager(assigned_by)
        now = _now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            self._check_capacity(repos, 1)
            assignment = self._create_assignment(
                repos, request.agent_id, request.objective, assigned_by=assigned_by,
                project_id=request.project_id, task_id=request.task_id, context=request.context,
                priority=request.priority, now=now,
            )
        return assignment

    async def start_review(self, request: ReviewRequest, requested_by: str = MANAGER_ID) -> dict:
        result = await asyncio.to_thread(self._start_review_db, request, requested_by)
        # Independent: all first-round assignments start from the same context,
        # and none receives another's report.
        for assignment in result["assignments"]:
            self._start(assignment["id"])
        return result

    def _start_review_db(self, request: ReviewRequest, requested_by: str) -> dict:
        self._require_manager(requested_by)
        agent_ids = [a.strip().lower() for a in (request.agents or self.registry.employee_ids())]
        if len(set(agent_ids)) != len(agent_ids):
            raise ValueError("each employee can only be asked once per review")
        agents = [self.registry.employee(a) for a in agent_ids]
        if len(agents) > self.registry.limits.max_assignments_per_plan:
            raise ValueError(f"a review can include at most {self.registry.limits.max_assignments_per_plan} assignments")
        questions = {k.strip().lower(): v for k, v in (request.questions or {}).items()}
        unknown = set(questions) - set(agent_ids)
        if unknown:
            raise ValueError(f"questions for agents not in this review: {sorted(unknown)}")

        now = _now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            self._check_capacity(repos, len(agents))
            require_project(repos, request.project_id)
            require_task(repos, request.task_id)
            review = repos.reviews.create(request.topic, requested_by, request.project_id, request.task_id, now)
            assignments = []
            for agent in agents:
                question = questions.get(agent.agent_id) or REVIEW_FRAMING[agent.report_kind]
                objective = f"Management review topic: {request.topic}\n\nYour question: {question}"
                assignments.append(self._create_assignment(
                    repos, agent.agent_id, objective[:2000], assigned_by=requested_by,
                    project_id=request.project_id, task_id=request.task_id, context=request.context,
                    review_id=review["id"], review_round=1, now=now,
                ))
            repos.audit.write(requested_by, "management_review_started",
                              f"Management review started with {', '.join(a.name for a in agents)}: {request.topic[:120]}",
                              "management_review", review["id"],
                              {"agents": agent_ids, "assignments": [a["id"] for a in assignments]}, now=now)
        return {"review": review, "assignments": assignments}

    async def follow_up(self, request: FollowUpRequest, requested_by: str = MANAGER_ID) -> dict:
        assignment, shared_reports = await asyncio.to_thread(self._follow_up_db, request, requested_by)
        self._start(assignment["id"], shared_reports)
        return assignment

    def _follow_up_db(self, request: FollowUpRequest, requested_by: str):
        self._require_manager(requested_by)
        agent = self.registry.employee(request.agent_id)
        share = [a.strip().lower() for a in request.share_reports_from]
        now = _now()
        with self.gary.db.transaction() as conn:
            repos = Repositories.bind(conn)
            review = repos.reviews.get(request.review_id)
            if review is None:
                raise NotFoundError(f"No management review with id {request.review_id}")
            first_round = [a for a in repos.assignments.list_for_review(review["id"]) if a["review_round"] == 1]
            if any(a["status"] in ("queued", "running") for a in first_round):
                raise ValueError("Wait for the first round of the review to finish before asking a follow-up")
            shared_reports = []
            for agent_id in share:
                source = next((a for a in first_round if a["assigned_to"] == agent_id and a["status"] == "completed"), None)
                if source is None:
                    raise ValueError(f"{agent_id} has no completed report in this review to share")
                shared_reports.append({"from": self.registry.get(agent_id).name,
                                       "report": json.loads(source["result_json"])})
            self._check_capacity(repos, 1)
            if not repos.reviews.use_follow_up(review["id"], self.registry.limits.max_review_follow_ups):
                raise ValueError(
                    f"This review already used its {self.registry.limits.max_review_follow_ups} follow-up question"
                )
            assignment = self._create_assignment(
                repos, agent.agent_id,
                f"Follow-up on management review: {review['topic'][:500]}\n\nGary's question: {request.question}",
                assigned_by=requested_by, project_id=review["project_id"], task_id=review["task_id"],
                context={"shared_reports_from": ", ".join(share) or "none"},
                review_id=review["id"], review_round=2, now=now,
            )
        return assignment, shared_reports

    def _require_manager(self, actor: str) -> None:
        if actor == "alex":
            return
        try:
            definition = self.registry.get(actor)
        except UnknownAgentError:
            raise PermissionError(f"{actor} may not delegate") from None
        if not definition.can_delegate:
            raise PermissionError(f"{definition.name} may not delegate")

    # ---------------------------------------------------------------- reading

    def _present(self, assignment: dict) -> dict:
        tz = self.gary.timezone
        agent = self.registry.get(assignment["assigned_to"])
        with self.gary.db.read() as conn:
            runs = Repositories.bind(conn).agent_runs.list_for_assignment(assignment["id"])
        run = runs[-1] if runs else None
        return {
            "assignment_id": assignment["id"],
            "agent_id": agent.agent_id,
            "agent": f"{agent.name}, {agent.title}",
            "objective": assignment["objective"],
            "status": assignment["status"],
            "priority": assignment["priority"],
            "project_id": assignment["project_id"],
            "review_id": assignment["review_id"],
            "review_round": assignment["review_round"],
            "created_at": to_local(assignment["created_at"], tz),
            "completed_at": to_local(assignment["completed_at"], tz) if assignment["completed_at"] else None,
            "report": json.loads(assignment["result_json"]) if assignment["result_json"] else None,
            "error": assignment["error_message"],
            "run": {
                "model": run["model"], "attempts": run["attempts"], "tool_calls": run["tool_calls"],
                "total_tokens": run["total_tokens"], "status": run["status"],
            } if run else None,
        }

    def get_assignment(self, assignment_id: str) -> dict:
        with self.gary.db.read() as conn:
            assignment = Repositories.bind(conn).assignments.get(assignment_id)
        if assignment is None:
            raise NotFoundError(f"No assignment with id {assignment_id}")
        return self._present(assignment)

    def latest_assignment(self, agent_id: str, completed_only: bool = False) -> dict | None:
        agent = self.registry.employee(agent_id)
        with self.gary.db.read() as conn:
            rows = Repositories.bind(conn).assignments.list_recent(
                agent.agent_id, "completed" if completed_only else None, limit=1
            )
        return self._present(rows[0]) if rows else None

    def find_assignment(self, agent_id: str, about: str) -> dict:
        """The agent's assignment whose objective best matches ``about``, with
        the agent's other recent objectives so a wrong match is visible."""
        agent = self.registry.employee(agent_id)
        words = {w for w in re.findall(r"[a-z0-9']{3,}", about.casefold()) if w not in STOPWORDS}
        with self.gary.db.read() as conn:
            rows = Repositories.bind(conn).assignments.list_recent(agent.agent_id, None, 30)

        def score(row):
            objective = set(re.findall(r"[a-z0-9']{3,}", row["objective"].casefold()))
            return len(words & objective)

        ranked = sorted(rows, key=lambda row: (score(row), row["created_at"]), reverse=True)
        best = ranked[0] if ranked and score(ranked[0]) > 0 else None
        return {
            "match": self._present(best) if best else None,
            "other_recent_assignments": [
                {"assignment_id": row["id"], "objective": row["objective"][:200], "status": row["status"]}
                for row in rows[:8]
                if best is None or row["id"] != best["id"]
            ],
            "note": None if best else f"No assignment for {agent.name} matches {about!r}.",
        }

    def list_assignments(self, agent_id: str | None = None, status: str | None = None, limit: int = 10) -> list[dict]:
        if agent_id:
            agent_id = self.registry.employee(agent_id).agent_id
        with self.gary.db.read() as conn:
            rows = Repositories.bind(conn).assignments.list_recent(agent_id, status, limit)
        return [self._present(row) for row in rows]

    def get_review(self, review_id: str | None = None) -> ManagementReview:
        with self.gary.db.read() as conn:
            repos = Repositories.bind(conn)
            review = repos.reviews.get(review_id) if review_id else repos.reviews.latest()
            if review is None:
                raise NotFoundError("No management review found" if not review_id
                                    else f"No management review with id {review_id}")
            assignments = repos.assignments.list_for_review(review["id"])
        result = ManagementReview(review_id=review["id"], topic=review["topic"], status=review["status"])
        for assignment in assignments:
            agent = self.registry.get(assignment["assigned_to"])
            presented = self._present(assignment)
            result.assignments.append({k: presented[k] for k in
                                       ("assignment_id", "agent", "status", "review_round", "error")})
            if assignment["review_round"] == 2:
                result.follow_ups.append(presented)
            elif assignment["status"] == "completed":
                setattr(result, agent.report_kind,
                        REPORT_MODELS[agent.report_kind].model_validate_json(assignment["result_json"]))
        return result

    def team(self) -> dict:
        with self.gary.db.read() as conn:
            repos = Repositories.bind(conn)
            counts = repos.assignments.counts_by_agent()
            recent = {d.agent_id: repos.assignments.list_recent(d.agent_id, None, 1) for d in self.registry.employees()}
            reviews = repos.reviews.list_recent(5)
        members = []
        for definition in self.registry.all():
            last = recent.get(definition.agent_id)
            members.append({
                "agent_id": definition.agent_id,
                "name": definition.name,
                "title": definition.title,
                "department": definition.department,
                "reports_to": definition.reports_to,
                "status": "active" if definition.active else "inactive",
                "can_delegate": definition.can_delegate,
                "assignments": counts.get(definition.agent_id, {}),
                "latest_assignment": self._present(last[0]) if last else None,
            })
        return {
            "members": members,
            "recent_reviews": [
                {"review_id": r["id"], "topic": r["topic"], "status": r["status"],
                 "created_at": to_local(r["created_at"], self.gary.timezone)}
                for r in reviews
            ],
            "limits": self.registry.limits.__dict__,
        }

    async def wait(self, assignment_ids: list[str], timeout: float | None = None) -> None:
        pending = [self._pending[a] for a in assignment_ids if a in self._pending]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)

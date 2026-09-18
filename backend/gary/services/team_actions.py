"""Action handlers that put the GaryCorp team to work.

Delegation is an action, not a direct service call, so that scheduled
planning cycles reach the team through the same path as everything else:
policy sets the risk level, the action is recorded and audited, and a failure
is recorded as a failure. The handlers are built by the application, which
owns the AgentService, and the service is resolved lazily because it is wired
after Gary's container exists.

Specialists are advisory: an assignment produces a report and changes nothing
on its own, which is why policy makes these green. The caps that matter live
in the roster (active assignments) and, for unattended cycles, in
planning_cycle.py.
"""

from typing import Callable

from gary.db.repositories import Repositories
from gary.models.action import DelegateToAgentPayload, RunManagementReviewPayload
from gary.services.action_service import ActionHandler


def team_action_handlers(service: Callable[[], object | None]) -> dict[str, ActionHandler]:
    """``service`` returns the AgentService, or None when it is unavailable."""

    def require_service():
        agents = service()
        if agents is None:
            raise ValueError("The GaryCorp team is not available right now")
        return agents

    def _check_delegate(repos: Repositories, payload: DelegateToAgentPayload) -> dict:
        # Fails here rather than after the action is recorded as running.
        agent = require_service().registry.employee(payload.agent_id)
        return {"agent_name": agent.name, "agent_title": agent.title}

    async def _delegate(payload: DelegateToAgentPayload, context: dict) -> dict:
        from gary.agents.service import DelegateRequest

        assignment = await require_service().delegate(
            DelegateRequest(
                agent_id=payload.agent_id,
                objective=payload.objective,
                project_id=payload.project_id,
                task_id=payload.task_id,
                priority=payload.priority,
            )
        )
        return {"assignment_id": assignment["id"], "assigned_to": assignment["assigned_to"]}

    def _record_delegate(repos, payload, result: dict, now: str) -> dict:
        return {"assignment_id": result["assignment_id"], "agent_id": payload.agent_id}

    def _check_review(repos: Repositories, payload: RunManagementReviewPayload) -> dict:
        registry = require_service().registry
        names = [registry.employee(agent_id).name for agent_id in payload.agents]
        return {"agent_names": names}

    async def _review(payload: RunManagementReviewPayload, context: dict) -> dict:
        from gary.agents.service import ReviewRequest

        result = await require_service().start_review(
            ReviewRequest(
                topic=payload.topic,
                agents=list(payload.agents),
                project_id=payload.project_id,
                task_id=payload.task_id,
            )
        )
        return {
            "review_id": result["review"]["id"],
            "assignments": [a["id"] for a in result["assignments"]],
        }

    def _record_review(repos, payload, result: dict, now: str) -> dict:
        return {"review_id": result["review_id"], "agents": list(payload.agents)}

    return {
        "delegate_to_agent": ActionHandler(
            payload_model=DelegateToAgentPayload,
            summarize=lambda payload, context: (
                f"Ask {context.get('agent_name', payload.agent_id)}: {payload.objective[:120]}"
            ),
            check=_check_delegate,
            execute=_delegate,
            record=_record_delegate,
            audit_event="agent_assignment_delegated_by_cycle",
        ),
        "run_management_review": ActionHandler(
            payload_model=RunManagementReviewPayload,
            summarize=lambda payload, context: (
                f"Management review with {', '.join(context.get('agent_names', payload.agents))}: "
                f"{payload.topic[:100]}"
            ),
            check=_check_review,
            execute=_review,
            record=_record_review,
            audit_event="management_review_started_by_cycle",
        ),
    }

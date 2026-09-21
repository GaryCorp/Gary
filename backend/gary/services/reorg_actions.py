"""The reorganisation action: proposal, approval, engineering ticket.

Gary can argue from the company's own record that it is badly organised, and
propose a different shape. He cannot impose one. `sync_roster` mirrors
`roster.py` into the database on every startup, so a change written to the
`agents` table would be reverted on the next restart -- the roster in code is
the only thing that decides anything. A reorganisation is therefore a code
change, and takes the route a hire takes: Alex approves the proposal, an
engineering ticket is filed, Alex edits `roster.py` and deploys.

What Python refuses here is the substance of the feature. Gary may not be the
subject of his own reorganisation, a reporting line may not close a loop, and
nothing may touch what anyone is permitted to do.
"""

import logging

from gary.db.repositories import Repositories
from gary.models.reorg import ReorganisationPayload
from gary.policy import GARY_ACTOR, USER_ACTOR
from gary.services.action_service import ActionHandler
from gary.services.common import NotFoundError

logger = logging.getLogger("gary.reorg")

MANAGER_ID = "gary"

ACCEPTANCE = (
    "Every change below is reflected in backend/gary/agents/roster.py.",
    "validate_roster_tools passes.",
    "allowed_tools is unchanged for every agent: this reorganisation grants "
    "and removes no capability.",
    "can_delegate is unchanged for every agent: Gary remains the only one who "
    "assigns work.",
    "The org chart on /team matches after a rebuild.",
)


class ReorganisationError(ValueError):
    """The proposed shape is not one the company can take."""


def describe(change) -> str:
    return f"{change.agent_id}: {change.change} -> {change.to}"


def validate_changes(changes, employees: dict, manager_id: str = MANAGER_ID) -> list[str]:
    """Every reason a proposed shape is refused. Returns the diff lines.

    ``employees`` maps agent_id to the current definition.
    """
    # The proposed chart starts as the current one and is edited, so a cycle
    # is detected against what the company would actually become.
    reports_to = {
        agent_id: definition.reports_to for agent_id, definition in employees.items()
    }
    seen: set[tuple[str, str]] = set()
    diff = []

    for change in changes:
        agent_id = change.agent_id.strip().lower()
        if agent_id == manager_id:
            raise ReorganisationError(
                "Gary cannot reorganise himself. A change to his own title, "
                "department, reporting line or remit is for Alex to make."
            )
        definition = employees.get(agent_id)
        if definition is None:
            raise NotFoundError(f"{change.agent_id} is not a current employee")
        if (agent_id, change.change) in seen:
            raise ReorganisationError(
                f"{agent_id} has two conflicting {change.change} changes in one proposal"
            )
        seen.add((agent_id, change.change))

        current = {
            "title": definition.title,
            "department": definition.department,
            "reports_to": definition.reports_to,
            "focus": getattr(definition, "specialty", "") or "",
        }[change.change]

        if change.change == "reports_to":
            manager = change.to.strip().lower()
            if manager == agent_id:
                raise ReorganisationError(f"{agent_id} cannot report to themselves")
            if manager != manager_id and manager not in employees:
                raise NotFoundError(f"{change.to} is not a current agent to report to")
            reports_to[agent_id] = manager

        if change.change != "focus" and (current or "").strip() == change.to.strip():
            raise ReorganisationError(
                f"{agent_id} is already {change.change} {change.to!r}; that changes nothing"
            )

        diff.append(f"{agent_id}: {change.change} {current!r} -> {change.to!r} ({change.reason})")

    _refuse_cycles(reports_to, manager_id)
    return diff


def _refuse_cycles(reports_to: dict[str, str | None], manager_id: str) -> None:
    """Nobody may end up reporting into a loop, or to nobody at all."""
    for agent_id in reports_to:
        seen = {agent_id}
        current = reports_to.get(agent_id)
        while current and current != manager_id:
            if current in seen:
                raise ReorganisationError(
                    f"that reporting structure loops: {' -> '.join(sorted(seen))}"
                )
            seen.add(current)
            current = reports_to.get(current)
        if current is None:
            raise ReorganisationError(
                f"{agent_id} would report to nobody; every employee reports to someone"
            )


def reorg_action_handler(registry, engineering=None) -> dict[str, ActionHandler]:
    """``propose_reorganisation``: yellow, and lands as a ticket."""

    def require_engineering():
        service = engineering() if callable(engineering) else engineering
        if service is None:
            raise ReorganisationError(
                "A reorganisation is a change to roster.py, filed as a GitHub "
                "engineering ticket, and the GitHub integration is not configured."
            )
        return service

    def employees() -> dict:
        return {d.agent_id: d for d in registry.employees()}

    def _check(repos: Repositories, payload: ReorganisationPayload) -> dict:
        require_engineering()
        diff = validate_changes(payload.changes, employees())
        return {"diff": diff}

    def _summarize(payload: ReorganisationPayload, context: dict) -> str:
        changes = ", ".join(describe(change) for change in payload.changes)
        return f"Reorganise GaryCorp ({len(payload.changes)}): {changes}"

    async def _file_ticket(payload: ReorganisationPayload, context: dict) -> dict:
        from gary.models.engineering import CreateEngineeringTicketRequest
        from gary.models.task import CreateTaskRequest

        service = require_engineering()
        diff = context.get("diff") or validate_changes(payload.changes, employees())
        summary = ", ".join(describe(change) for change in payload.changes)[:180]

        task = service.gary.tasks.create_task(
            CreateTaskRequest(title=f"Reorganise: {summary}"[:300], priority=6), GARY_ACTOR
        )
        ticket = await service.create_ticket(
            CreateEngineeringTicketRequest(
                task_id=task["id"],
                title=f"Reorganise: {summary}"[:240],
                objective=(
                    f"Gary proposed a reorganisation and Alex approved it. "
                    f"{payload.rationale} This ticket is to make the change in "
                    "roster.py. It grants and removes no capability."
                ),
                requirements=diff,
                acceptance_criteria=list(ACCEPTANCE),
                priority="P2",
                kind="feature",
                security_review_required=True,
            )
        )
        return {
            "task_id": task["id"],
            "ticket_id": ticket.id,
            "issue_number": ticket.github_issue_number,
            "url": ticket.github_url,
            "changes": len(payload.changes),
        }

    def _record(repos: Repositories, payload: ReorganisationPayload, result: dict, now: str):
        repos.audit.write(
            USER_ACTOR,
            "reorganisation_requested",
            f"Engineering ticket #{result.get('issue_number')} opened to "
            f"reorganise {len(payload.changes)} role(s)",
            "agent",
            "roster",
            {
                "changes": [describe(change) for change in payload.changes],
                "rationale": payload.rationale[:500],
                "issue_number": result.get("issue_number"),
                "proposed_by": GARY_ACTOR,
            },
            now=now,
        )
        logger.info(
            "Reorganisation filed as issue #%s; the roster is unchanged until it lands",
            result.get("issue_number"),
        )
        return {
            "issue_number": result.get("issue_number"),
            "url": result.get("url"),
            "applied": False,
        }

    return {
        "propose_reorganisation": ActionHandler(
            payload_model=ReorganisationPayload,
            summarize=_summarize,
            check=_check,
            execute=_file_ticket,
            record=_record,
            audit_event="reorganisation_requested",
        )
    }


__all__ = [
    "ACCEPTANCE",
    "ReorganisationError",
    "reorg_action_handler",
    "validate_changes",
]

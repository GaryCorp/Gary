"""The hire action: proposal, approval, roster entry.

Gary proposes; policy makes it yellow and web-only; Alex approves on the
approvals page; only then does a row appear in ``hired_employees`` and the
registry gain a colleague. Nothing here can widen what an employee may do:
the tools are filtered through ``HIREABLE_TOOLS`` at proposal time, again
when the row is written, and again every time the roster loads.

The checks run twice on purpose. ``check`` runs when the proposal is made and
again at execution, so a name, id or notebook that was free when Gary asked
but taken by the time Alex approves is refused rather than colliding.
"""

import json
import logging

from gary.agents.hiring import (
    GAP_LIMIT,
    NAME_LIMIT,
    PERSONALITY_LIMIT,
    SPECIALTY_LIMIT,
    TITLE_LIMIT,
    HiringError,
    check_proposal,
    normalize_tools,
    scrub,
)
from gary.db.repositories import Repositories
from gary.models.hiring import HireEmployeePayload
from gary.policy import GARY_ACTOR, USER_ACTOR
from gary.services.action_service import ActionHandler

logger = logging.getLogger("gary.hiring")


def hire_action_handler(registry) -> dict[str, ActionHandler]:
    """The ``hire_employee`` handler, bound to the live roster."""

    def _check(repos: Repositories, payload: HireEmployeePayload) -> dict:
        existing = registry.all()
        check_proposal(
            agent_id=payload.agent_id,
            name=payload.name,
            notebook=payload.notebook,
            taken_ids={definition.agent_id for definition in existing},
            taken_names={definition.name.casefold() for definition in existing},
            taken_notebooks={
                definition.notebook.casefold() for definition in existing if definition.notebook
            }
            | repos.hires.taken_notebooks(),
        )
        # Raises when Gary asked for anything above the ceiling.
        tools = normalize_tools(payload.tools)
        if repos.hires.get(payload.agent_id) is not None:
            raise HiringError(f"{payload.agent_id} has already been hired")
        return {"tools": tools}

    def _summarize(payload: HireEmployeePayload, context: dict) -> str:
        tools = context.get("tools") or normalize_tools(payload.tools)
        return (
            f"Hire {payload.name} as {payload.title} ({payload.department}), "
            f"reporting to Gary, with {len(tools)} tools: {', '.join(tools)}. "
            f"Gap: {payload.capability_gap[:200]}"
        )

    def _record(repos: Repositories, payload: HireEmployeePayload, result: dict, now: str) -> dict:
        tools = normalize_tools(payload.tools)
        hire = repos.hires.hire(
            agent_id=payload.agent_id,
            name=scrub(payload.name, NAME_LIMIT),
            title=scrub(payload.title, TITLE_LIMIT),
            department=scrub(payload.department, TITLE_LIMIT),
            notebook=scrub(payload.notebook, NAME_LIMIT),
            specialty=scrub(payload.specialty, SPECIALTY_LIMIT),
            personality=scrub(payload.personality or "", PERSONALITY_LIMIT) or None,
            capability_gap=scrub(payload.capability_gap, GAP_LIMIT),
            allowed_tools=tools,
            proposed_by=GARY_ACTOR,
            approved_by=USER_ACTOR,
            now=now,
        )
        repos.agents.upsert(
            hire["agent_id"], hire["name"], hire["title"], hire["department"],
            GARY_ACTOR, True, True, now,
        )
        repos.audit.write(
            USER_ACTOR,
            "employee_hired",
            f"{hire['name']} joined GaryCorp as {hire['title']}",
            "agent",
            hire["agent_id"],
            {
                "department": hire["department"],
                "tools": list(tools),
                "capability_gap": hire["capability_gap"][:500],
                "proposed_by": GARY_ACTOR,
            },
            now=now,
        )
        # The next roster read includes them.
        registry.refresh(force=True)
        logger.info("Hired %s (%s) with tools %s", hire["name"], hire["agent_id"], list(tools))
        return {
            "agent_id": hire["agent_id"],
            "name": hire["name"],
            "tools": list(tools),
        }

    return {
        "hire_employee": ActionHandler(
            payload_model=HireEmployeePayload,
            summarize=_summarize,
            check=_check,
            record=_record,
            audit_event="employee_hired",
        )
    }


def dismiss(gary, registry, agent_id: str, actor: str = USER_ACTOR) -> dict:
    """Alex removes a hired employee. Gary has no tool for this."""
    from gary.services.common import clock_now

    now = clock_now(gary.planning.clock)
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        hire = repos.hires.get(agent_id)
        if hire is None:
            raise HiringError(f"{agent_id} was not hired by GaryCorp")
        if not repos.hires.deactivate(agent_id, now):
            raise HiringError(f"{hire['name']} is already deactivated")
        repos.agents.upsert(
            hire["agent_id"], hire["name"], hire["title"], hire["department"],
            GARY_ACTOR, True, False, now,
        )
        repos.audit.write(
            actor,
            "employee_deactivated",
            f"{hire['name']} left GaryCorp",
            "agent",
            agent_id,
            {"title": hire["title"]},
            now=now,
        )
    registry.refresh(force=True)
    return {"agent_id": agent_id, "name": hire["name"], "status": "deactivated"}


def proposal_context(registry, repos: Repositories) -> dict:
    """What Gary needs to write a valid proposal."""
    from gary.agents.hiring import HIREABLE_TOOLS

    existing = registry.employees()
    return {
        "existing_employees": [
            {"agent_id": d.agent_id, "name": d.name, "title": d.title, "department": d.department}
            for d in existing
        ],
        "tools_a_new_employee_may_have": sorted(HIREABLE_TOOLS),
        "taken_notebooks": sorted(
            {d.notebook for d in existing if d.notebook} | repos.hires.taken_notebooks()
        ),
    }


__all__ = ["dismiss", "hire_action_handler", "proposal_context", "HiringError", "json"]

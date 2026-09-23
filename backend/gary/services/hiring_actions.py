"""The hire action: proposal, approval, engineering ticket.

Gary proposes a colleague; policy makes it yellow and web-only; Alex approves
on the approvals page. Approving does **not** create the colleague. It files
a private GitHub issue holding Gary's specification, assigned to Alex, who
writes the new employee into ``roster.py`` and deploys.

That split is deliberate. Gary is good at noticing a gap and describing the
role; bringing a new agent with real tools into existence is a code change
that deserves review. Nothing can act until Alex has landed it, so an
approval by itself can no longer put a working agent in the company.

Employees hired under the earlier data-driven flow keep working: the
``hired_employees`` table, ``definition_from_row`` and dismissal are
unchanged, and the roster still loads both kinds. Only new hires take this
route.

The checks run twice on purpose. ``check`` runs when the proposal is made and
again at execution, so a name, id or notebook that was free when Gary asked
but taken by the time Alex approves is refused rather than colliding.
"""

import datetime as dt
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
from gary.services.common import Clock, default_clock
from gary.timeutil import format_utc

logger = logging.getLogger("gary.hiring")


# What "done" means for a hire ticket, written so Alex can work from it and
# so nothing is considered finished until the colleague actually exists.
ACCEPTANCE = (
    "The employee is defined in backend/gary/agents/roster.py and appears in "
    "the roster's definitions tuple.",
    "validate_roster_tools passes: every tool is in TOOL_CATALOG and none is in "
    "FORBIDDEN_TOOLS.",
    "They report to Gary, cannot delegate, and their report is advisory.",
    "They appear on the /team page and in team_list after a rebuild.",
    "Gary can delegate to them and receives a structured report back.",
    "Their Joplin notebook exists, so their own-notebook tools work.",
)


EVIDENCE_DAYS = 30


def hiring_evidence(repos: Repositories, since: str) -> list[str]:
    """What the company's own record says about the gap, in numbers.

    Gary writes the argument for a colleague; this is the part nobody has to
    take his word for. Counted here rather than asked of the model, so the
    ticket cites facts Alex can check against the same database.
    """
    assignments = [
        a for a in repos.assignments.list_recent(None, None, 200)
        if (a["created_at"] or "") >= since
    ]
    per_agent: dict[str, int] = {}
    for assignment in assignments:
        per_agent[assignment["assigned_to"]] = per_agent.get(assignment["assigned_to"], 0) + 1
    failed = [a for a in assignments if a["status"] == "failed"]
    unanswered = [m for m in repos.spoken.list_open() if m["expects_reply"]]

    load = ", ".join(
        f"{agent} {count}" for agent, count in sorted(per_agent.items(), key=lambda i: -i[1])
    )
    evidence = [
        f"Assignments in the last {EVIDENCE_DAYS} days: {len(assignments)}"
        + (f" ({load})" if load else ", none to anybody"),
    ]
    if failed:
        evidence.append(f"Assignments that did not finish: {len(failed)}")
    if unanswered:
        evidence.append(
            f"Questions Gary asked and nobody has answered: {len(unanswered)}"
        )
    return evidence


def hire_spec(
    payload: HireEmployeePayload, tools: list[str], evidence: list[str] | None = None
) -> dict:
    """Gary's proposal, as an engineering specification.

    Everything Alex needs to write the roster entry, stated as requirements
    rather than as code, because how it is built is his decision.
    """
    requirements = [
        f"agent_id: {payload.agent_id}",
        f"name: {payload.name}",
        f"title: {payload.title}",
        f"department: {payload.department}",
        f"Joplin notebook: {payload.notebook}",
        f"specialty: {scrub(payload.specialty, SPECIALTY_LIMIT)}",
    ]
    if payload.personality:
        requirements.append(
            f"personality: {scrub(payload.personality, PERSONALITY_LIMIT)}"
        )
    requirements.append(
        "tools Gary proposed, as the smallest set that does the job: "
        + ", ".join(tools)
    )
    objective = (
        f"GaryCorp needs a {payload.title} in {payload.department}. "
        f"The gap: {scrub(payload.capability_gap, GAP_LIMIT)} "
        "Gary proposed this colleague and Alex approved the proposal; this "
        "ticket is to build them."
    )
    if evidence:
        objective += " What the company's own record shows: " + "; ".join(evidence) + "."
    return {
        "title": f"Hire {payload.name} as {payload.title}",
        "objective": objective,
        "requirements": requirements,
        "acceptance_criteria": list(ACCEPTANCE),
    }


def hire_action_handler(
    registry, engineering=None, clock: Clock = default_clock
) -> dict[str, ActionHandler]:
    """The ``hire_employee`` handler.

    ``engineering`` is a callable returning the engineering ticket service, or
    None where GitHub is not configured; it is resolved lazily because it is
    wired after Gary's container exists.
    """

    def require_engineering():
        service = engineering() if callable(engineering) else engineering
        if service is None:
            raise HiringError(
                "Hiring files a GitHub engineering ticket, and the GitHub "
                "integration is not configured on this deployment."
            )
        return service

    def _check(repos: Repositories, payload: HireEmployeePayload) -> dict:
        require_engineering()
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
        # One ticket per colleague: a retry must not file a second issue.
        since = format_utc(clock() - dt.timedelta(days=EVIDENCE_DAYS))
        spec = hire_spec(payload, tools, hiring_evidence(repos, since))
        for ticket in repos.engineering.list_open(100):
            task = repos.tasks.get(ticket["task_id"])
            if task and task["title"] == spec["title"]:
                raise HiringError(
                    f"{payload.name} already has engineering ticket "
                    f"#{ticket['github_issue_number']}; build that one"
                )
        return {"tools": tools, "spec": spec}

    def _summarize(payload: HireEmployeePayload, context: dict) -> str:
        tools = context.get("tools") or normalize_tools(payload.tools)
        return (
            f"Open an engineering ticket to build {payload.name} as "
            f"{payload.title} ({payload.department}), reporting to Gary, with "
            f"{len(tools)} tools: {', '.join(tools)}. "
            f"Gap: {payload.capability_gap[:200]}"
        )

    async def _file_ticket(payload: HireEmployeePayload, context: dict) -> dict:
        """Create the task and the private issue. No colleague is created.

        Runs with no transaction open, per the external side-effect rule. The
        task has to exist first because an engineering ticket is keyed to one
        (engineering_tickets.task_id is unique, which is also what makes
        creation idempotent).
        """
        from gary.models.engineering import CreateEngineeringTicketRequest
        from gary.models.task import CreateTaskRequest

        service = require_engineering()
        spec = context.get("spec") or hire_spec(payload, normalize_tools(payload.tools))

        task = service.gary.tasks.create_task(
            CreateTaskRequest(title=spec["title"], priority=7), GARY_ACTOR
        )
        ticket = await service.create_ticket(
            CreateEngineeringTicketRequest(
                task_id=task["id"],
                title=spec["title"],
                objective=spec["objective"],
                requirements=spec["requirements"],
                acceptance_criteria=spec["acceptance_criteria"],
                priority="P2",
                kind="feature",
                # A new agent with tools is exactly what Dave's gate is for.
                security_review_required=True,
            )
        )
        return {
            "task_id": task["id"],
            "ticket_id": ticket.id,
            "issue_number": ticket.github_issue_number,
            "url": ticket.github_url,
        }

    def _record(repos: Repositories, payload: HireEmployeePayload, result: dict, now: str) -> dict:
        """Record that the work was commissioned. Nobody joined GaryCorp."""
        repos.audit.write(
            USER_ACTOR,
            "employee_requested",
            f"Engineering ticket #{result.get('issue_number')} opened to build "
            f"{payload.name} as {payload.title}",
            "agent",
            payload.agent_id,
            {
                "department": payload.department,
                "tools": list(normalize_tools(payload.tools)),
                "capability_gap": scrub(payload.capability_gap, GAP_LIMIT)[:500],
                "proposed_by": GARY_ACTOR,
                "issue_number": result.get("issue_number"),
                "ticket_id": result.get("ticket_id"),
            },
            now=now,
        )
        logger.info(
            "Hire of %s (%s) filed as issue #%s; nobody joins until it is built",
            payload.name, payload.agent_id, result.get("issue_number"),
        )
        return {
            "agent_id": payload.agent_id,
            "name": payload.name,
            "issue_number": result.get("issue_number"),
            "url": result.get("url"),
            "joined": False,
        }

    return {
        "hire_employee": ActionHandler(
            payload_model=HireEmployeePayload,
            summarize=_summarize,
            check=_check,
            execute=_file_ticket,
            record=_record,
            audit_event="employee_requested",
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


__all__ = [
    "ACCEPTANCE",
    "dismiss",
    "hire_action_handler",
    "hire_spec",
    "proposal_context",
    "HiringError",
    "json",
]

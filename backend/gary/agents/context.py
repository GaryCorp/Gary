"""Context packages: least privilege for information.

Each specialist gets only what their profile needs for the assignment. Nobody
receives email content, credentials, the card number, the whole notebook, or
another specialist's report unless Gary explicitly shares it in a follow-up.
"""

import asyncio
import datetime as dt
import json
import logging

from gary.agents.gateway import (
    AgentServices,
    AvailabilityArgs,
    NoArgs,
    PurchasesArgs,
    RunState,
    ToolCall,
    _read_action_policy,
    _read_agent_permissions,
    _read_finance_status,
    _read_purchases,
    _read_system_configuration_summary,
    read_calendar_availability,
)
from gary.agents.models import GaryCorpAgentDefinition
from gary.db.repositories import Repositories
from gary.services.common import task_readiness_in
from gary.timeutil import format_utc, to_local

logger = logging.getLogger("gary.agents.context")

SECURITY_AUDIT_EVENTS = (
    "action_rejected_by_policy",
    "action_failed",
    "approval_requested",
    "approval_approved",
    "approval_rejected",
    "agent_tool_denied",
    "email_sent",
    "calendar_changed",
)


def _local(value, services: AgentServices):
    return to_local(value, services.gary.timezone) if value else value


def _project_and_tasks(repos: Repositories, project_id: str | None, services: AgentServices, now: str) -> dict:
    if not project_id:
        return {}
    project = repos.projects.get(project_id)
    if project is None:
        return {}
    tasks = []
    for task in repos.tasks.list_for_project(project_id):
        readiness = task_readiness_in(repos, task, now)
        tasks.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "priority": task["priority"],
                "estimated_minutes": task["estimated_minutes"],
                "deadline": _local(task["deadline"], services),
                "scheduled_start": _local(task["scheduled_start"], services),
                "blocked_by": readiness["blocked_by"],
            }
        )
    return {
        "project": {
            "project_id": project["id"],
            "name": project["name"],
            "objective": project["objective"],
            "status": project["status"],
            "priority": project["priority"],
            "deadline": _local(project["deadline"], services),
        },
        "tasks": tasks,
    }


async def _notes(services: AgentServices, project_name: str | None) -> list[dict] | str:
    if services.notes is None:
        return "not available"
    try:
        today = dt.datetime.now(services.gary.timezone).date()
        return await services.notes.get_relevant_notes([project_name] if project_name else [], today)
    except Exception as exc:
        logger.warning("Planning notes unavailable for agent context: %s", exc)
        return f"not available ({type(exc).__name__})"


async def build_context(
    services: AgentServices,
    agent: GaryCorpAgentDefinition,
    assignment: dict,
    shared_reports: list[dict] | None = None,
) -> dict:
    gary = services.gary
    now = format_utc(gary.planning.clock())
    context: dict = {
        "assignment": {
            "assignment_id": assignment["id"],
            "objective": assignment["objective"],
            "priority": assignment["priority"],
            "from": "Gary, Chief of Staff",
            "gary_context": json.loads(assignment["context_json"]) if assignment["context_json"] else None,
        },
        "now_local": _local(now, services),
        "timezone": str(gary.timezone),
    }

    def read_state():
        with gary.db.read() as conn:
            repos = Repositories.bind(conn)
            data = _project_and_tasks(repos, assignment["project_id"], services, now)
            if agent.context_profile == "operations":
                data["active_projects"] = [
                    {"name": p["name"], "priority": p["priority"], "deadline": _local(p["deadline"], services)}
                    for p in repos.projects.list_active()
                ]
                data["open_commitments"] = [
                    {"description": c["description"], "committed_to": c["committed_to"],
                     "deadline": _local(c["deadline"], services)}
                    for c in repos.commitments.list_open()
                ]
                data["pending_followups"] = [
                    {"title": f["title"], "due_at": _local(f["due_at"], services)}
                    for f in repos.followups.list_pending()
                ]
            if agent.context_profile == "security":
                marks = ", ".join("?" for _ in SECURITY_AUDIT_EVENTS)
                data["recent_security_relevant_events"] = [
                    {**dict(row), "timestamp": _local(row["timestamp"], services)}
                    for row in conn.execute(
                        f"""
                        SELECT timestamp, actor, event_type, summary FROM audit_log
                        WHERE event_type IN ({marks}) ORDER BY id DESC LIMIT 20
                        """,
                        SECURITY_AUDIT_EVENTS,
                    )
                ]
            return data

    context.update(await asyncio.to_thread(read_state))
    project_name = context.get("project", {}).get("name")

    if agent.context_profile in ("research", "operations", "finance", "ethics", "advisory"):
        context["planning_notes"] = await _notes(services, project_name)

    if agent.context_profile == "security":
        state = RunState(assignment["id"], agent.agent_id)
        call = ToolCall(services, agent, state, NoArgs())
        context["agent_permissions"] = await asyncio.to_thread(_read_agent_permissions, call)
        context["action_policy"] = await asyncio.to_thread(_read_action_policy, call)
        context["system_summary"] = await asyncio.to_thread(_read_system_configuration_summary, call)

    if agent.context_profile == "operations":
        state = RunState(assignment["id"], agent.agent_id)
        try:
            context["calendar"] = await read_calendar_availability(
                ToolCall(services, agent, state, AvailabilityArgs(days=5, min_minutes=30))
            )
        except Exception as exc:
            logger.warning("Calendar unavailable for agent context: %s", exc)
            context["calendar"] = f"not available ({type(exc).__name__})"

    if agent.context_profile == "finance":
        state = RunState(assignment["id"], agent.agent_id)
        context["finance_status"] = await asyncio.to_thread(
            _read_finance_status, ToolCall(services, agent, state, NoArgs())
        )
        context["recent_purchases"] = (
            await asyncio.to_thread(_read_purchases, ToolCall(services, agent, state, PurchasesArgs(limit=10)))
        )["purchases"]

    if shared_reports:
        context["reports_shared_by_gary"] = shared_reports

    return context


"""GaryCorp tool gateway: the only way a specialist reaches company data.

    CrewAI agent -> tool wrapper -> ToolGateway.call -> permission check
                 -> limits -> validated arguments -> existing service -> result

Every tool is read-only and returns filtered data: no credentials, no email
content, no notes outside Gary's planning notes, no raw SQL, no shell. A tool
is only callable by agents whose roster entry lists it, and the gateway checks
that on every call, independent of which tools CrewAI was given.
"""

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from pydantic import Field

from gary.agents.models import GaryCorpAgentDefinition
from gary.agents.roster import AgentLimits, AgentRegistry
from gary.container import Gary
from gary.db.repositories import Repositories
from gary.models.common import EntityId, RequestModel, validate_request
from gary.policy import (
    ACTION_POLICIES,
    APPROVAL_EXPIRY_HOURS,
    CRITICAL_TASK_PRIORITY,
)
from gary.services.calendar_blocks import find_free_blocks
from gary.services.readiness import task_readiness
from gary.timeutil import format_utc, to_local, utc_now

logger = logging.getLogger("gary.agents.gateway")

RESULT_CHAR_LIMIT = 12_000


class ToolDenied(PermissionError):
    pass


class WebResearch(Protocol):
    async def search(self, query: str) -> dict: ...


class PlanningNotes(Protocol):
    async def get_relevant_notes(self, project_names: list[str], today: dt.date) -> list[dict]: ...


class BusyCalendar(Protocol):
    async def busy_intervals(self, start: str, end: str) -> list[dict]: ...


class AgentNotebooks(Protocol):
    async def create_note(self, notebook: str, title: str, body: str) -> dict: ...


@dataclass
class AgentServices:
    """What tools may use. Integrations are optional; a tool whose
    integration is missing reports that instead of failing the run."""

    gary: Gary
    registry: AgentRegistry
    web: WebResearch | None = None
    notes: PlanningNotes | None = None
    calendar: BusyCalendar | None = None
    # Writes notes into an agent's own Joplin notebook.
    notebooks: AgentNotebooks | None = None
    # Returns a non-secret summary of the deployment for Dave.
    system_summary: Callable[[], dict] | None = None
    # Names of Gary's own tools, for permission reviews.
    manager_tools: tuple[str, ...] = ()


@dataclass
class RunState:
    assignment_id: str
    agent_id: str
    cancelled: bool = False
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    # Token usage of model calls made by tools (e.g. web search).
    tool_usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[RequestModel]
    handler: Callable[["ToolCall"], Awaitable[Any]]
    # Extra per-run cap for costly tools.
    max_calls_per_run: int | None = None


@dataclass
class ToolCall:
    services: AgentServices
    agent: GaryCorpAgentDefinition
    state: RunState
    args: RequestModel


# ------------------------------------------------------------------ helpers

def _local(value, call: ToolCall):
    return to_local(value, call.services.gary.timezone) if value else value


def _task_brief(task: dict, call: ToolCall, repos: Repositories | None = None, now: str | None = None) -> dict:
    brief = {
        "task_id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "priority": task["priority"],
        "estimated_minutes": task["estimated_minutes"],
        "deadline": _local(task["deadline"], call),
        "scheduled_start": _local(task["scheduled_start"], call),
        "scheduled_end": _local(task["scheduled_end"], call),
    }
    if repos is not None:
        readiness = task_readiness(task, repos.dependencies.list_dependencies(task["id"]), now)
        brief["ready"] = readiness["ready"]
        brief["blocked_by"] = readiness["blocked_by"]
    return brief


def _project_brief(project: dict, call: ToolCall) -> dict:
    return {
        "project_id": project["id"],
        "name": project["name"],
        "objective": project["objective"],
        "status": project["status"],
        "priority": project["priority"],
        "deadline": _local(project["deadline"], call),
    }


def _require_project(repos: Repositories, project_id: str) -> dict:
    project = repos.projects.get(project_id)
    if project is None:
        raise ValueError(f"No project with id {project_id}")
    return project


# ------------------------------------------------------------------ arguments

class NoArgs(RequestModel):
    pass


class WebSearchArgs(RequestModel):
    query: str = Field(min_length=3, max_length=400)


class ProjectArgs(RequestModel):
    project_id: EntityId


class TasksArgs(RequestModel):
    project_id: EntityId | None = None
    status: Literal["open", "all"] = "open"


class NotesArgs(RequestModel):
    project_id: EntityId | None = None


class PreviousResearchArgs(RequestModel):
    keywords: str = Field(default="", max_length=200)


class AuditArgs(RequestModel):
    event_type: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")
    limit: int = Field(default=25, strict=True, ge=1, le=50)


class AvailabilityArgs(RequestModel):
    days: int = Field(default=5, strict=True, ge=1, le=14)
    min_minutes: int = Field(default=60, strict=True, ge=15, le=240)


class CommitmentsArgs(RequestModel):
    status: Literal["open", "all"] = "open"


NOTE_TITLE_LIMIT = 200
NOTE_BODY_LIMIT = 20_000


class WriteNoteArgs(RequestModel):
    title: str = Field(min_length=1, max_length=NOTE_TITLE_LIMIT)
    body: str = Field(min_length=1, max_length=NOTE_BODY_LIMIT)


# ------------------------------------------------------------------ handlers

async def web_search(call: ToolCall):
    if call.services.web is None:
        raise ValueError("Web research is not available right now")
    return await call.services.web.search(call.args.query)


def _read_project(call: ToolCall):
    now = format_utc(call.services.gary.planning.clock())
    with call.services.gary.db.read() as conn:
        repos = Repositories.bind(conn)
        project = _require_project(repos, call.args.project_id)
        tasks = repos.tasks.list_for_project(project["id"])
        return {
            "project": _project_brief(project, call),
            "tasks": [_task_brief(t, call, repos, now) for t in tasks],
        }


def _read_projects(call: ToolCall):
    with call.services.gary.db.read() as conn:
        repos = Repositories.bind(conn)
        return {"projects": [_project_brief(p, call) for p in repos.projects.list_active()]}


def _read_tasks(call: ToolCall):
    now = format_utc(call.services.gary.planning.clock())
    with call.services.gary.db.read() as conn:
        repos = Repositories.bind(conn)
        if call.args.project_id:
            _require_project(repos, call.args.project_id)
            tasks = repos.tasks.list_for_project(call.args.project_id)
        else:
            tasks = repos.tasks.list_open()
        if call.args.status == "open":
            tasks = [t for t in tasks if t["status"] not in ("completed", "cancelled")]
        return {"tasks": [_task_brief(t, call, repos, now) for t in tasks[:60]]}


def _read_dependencies(call: ToolCall):
    with call.services.gary.db.read() as conn:
        repos = Repositories.bind(conn)
        _require_project(repos, call.args.project_id)
        edges = []
        for task in repos.tasks.list_for_project(call.args.project_id):
            for dependency in repos.dependencies.list_dependencies(task["id"]):
                edges.append(
                    {
                        "task": task["title"],
                        "depends_on": dependency["title"],
                        "depends_on_status": dependency["status"],
                    }
                )
        return {"dependencies": edges}


async def read_relevant_notes(call: ToolCall):
    if call.services.notes is None:
        return {"notes": [], "note": "Planning notes are not available right now"}
    names = []
    if call.args.project_id:
        with call.services.gary.db.read() as conn:
            names = [_require_project(Repositories.bind(conn), call.args.project_id)["name"]]
    today = dt.datetime.now(call.services.gary.timezone).date()
    notes = await call.services.notes.get_relevant_notes(names, today)
    return {
        "notes": notes,
        "note": "Only Gary's planning notes for this project, preferences, and the latest daily summary.",
    }


def _read_previous_research(call: ToolCall):
    words = [w for w in re.findall(r"[\w'-]{3,}", call.args.keywords.casefold())][:8]
    with call.services.gary.db.read() as conn:
        rows = Repositories.bind(conn).assignments.search_completed(call.agent.agent_id, words)
    results = []
    for row in rows:
        report = json.loads(row["result_json"] or "{}")
        results.append(
            {
                "objective": row["objective"],
                "completed_at": _local(row["completed_at"], call),
                "summary": report.get("summary"),
                "findings": report.get("findings", [])[:8],
                "recommendation": report.get("recommendation"),
                "sources": report.get("sources", [])[:8],
            }
        )
    return {"previous_research": results}


def _read_agent_permissions(call: ToolCall):
    registry = call.services.registry
    return {
        "agents": [
            {
                "agent_id": d.agent_id,
                "name": d.name,
                "title": d.title,
                "reports_to": d.reports_to,
                "can_delegate": d.can_delegate,
                "allowed_tools": list(d.allowed_tools),
                "max_iterations": d.max_iterations if d.is_employee else None,
                "max_execution_seconds": d.max_execution_seconds if d.is_employee else None,
                "active": d.active,
            }
            for d in registry.all()
        ],
        "manager_tools": list(call.services.manager_tools),
        "limits": registry.limits.__dict__,
        "notes": [
            "Permissions are defined in code (gary/agents/roster.py) and enforced by "
            "the tool gateway on every call; no agent can change them.",
            "All specialist tools are read-only.",
        ],
    }


def _read_action_policy(call: ToolCall):
    return {
        "action_policies": ACTION_POLICIES,
        "critical_task_priority_requires_approval_to_move": CRITICAL_TASK_PRIORITY,
        "approval_expiry_hours": APPROVAL_EXPIRY_HOURS,
        "supported_action_types": call.services.gary.actions.supported_action_types(),
        "rules": [
            "green actions run automatically, yellow wait for Alex's approval, red are refused",
            "the application sets risk; agents cannot",
            "handlers may escalate risk, never lower it",
        ],
    }


def _read_audit_events(call: ToolCall):
    with call.services.gary.db.read() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, actor, event_type, entity_type, summary
            FROM audit_log
            WHERE (? IS NULL OR event_type = ?)
            ORDER BY id DESC LIMIT ?
            """,
            (call.args.event_type, call.args.event_type, call.args.limit),
        ).fetchall()
    # Details are omitted: they can contain email payloads and other content.
    return {
        "events": [
            {**dict(row), "timestamp": _local(row["timestamp"], call)} for row in rows
        ]
    }


def _read_system_configuration_summary(call: ToolCall):
    if call.services.system_summary is None:
        return {"note": "No system summary is available"}
    return call.services.system_summary()


async def read_calendar_availability(call: ToolCall):
    gary = call.services.gary
    now = utc_now()
    end = dt.datetime.combine(
        now.astimezone(gary.timezone).date() + dt.timedelta(days=call.args.days),
        dt.time(),
        gary.timezone,
    )
    start_iso, end_iso = format_utc(now), format_utc(end)
    result: dict = {**gary.week.describe()}
    if call.services.calendar is None:
        result["note"] = "The calendar is not available right now"
        busy = []
    else:
        busy = await call.services.calendar.busy_intervals(start_iso, end_iso)
    blocks = find_free_blocks(start_iso, end_iso, busy, gary.week, gary.timezone, call.args.min_minutes, 25)
    with gary.db.read() as conn:
        scheduled = Repositories.bind(conn).tasks.list_scheduled_between(start_iso, end_iso)
    result.update(
        {
            "free_blocks": [
                {"start": _local(b["start"], call), "end": _local(b["end"], call), "minutes": b["minutes"]}
                for b in blocks
            ],
            "busy_block_count": len(busy),
            "scheduled_gary_tasks": [
                {
                    "title": t["title"],
                    "start": _local(t["scheduled_start"], call),
                    "end": _local(t["scheduled_end"], call),
                    "status": t["status"],
                }
                for t in scheduled
            ],
            "note": "Busy calendar time is shown without titles or attendees.",
        }
    )
    return result


def _read_commitments(call: ToolCall):
    with call.services.gary.db.read() as conn:
        repos = Repositories.bind(conn)
        rows = repos.commitments.list_open() if call.args.status == "open" else repos.commitments.list_all()
    return {
        "commitments": [
            {
                "description": c["description"],
                "committed_to": c["committed_to"],
                "deadline": _local(c["deadline"], call),
                "status": c["status"],
                "task_id": c["task_id"],
            }
            for c in rows
        ]
    }


def _read_followups(call: ToolCall):
    with call.services.gary.db.read() as conn:
        rows = Repositories.bind(conn).followups.list_pending()
    return {
        "followups": [
            {
                "title": f["title"],
                "due_at": _local(f["due_at"], call),
                "priority": f["priority"],
                "task_id": f["task_id"],
            }
            for f in rows
        ]
    }


async def write_note(call: ToolCall):
    agent = call.agent
    if not agent.notebook:
        raise ValueError(f"{agent.name} has no notebook")
    if call.services.notebooks is None:
        raise ValueError("Joplin is not available right now")
    title = " ".join(call.args.title.split())
    stamp = datetime_stamp(call)
    body = (
        f"{call.args.body.rstrip()}\n\n---\n"
        f"Written by {agent.name}, {agent.title}, on {stamp} "
        f"(GaryCorp assignment {call.state.assignment_id})."
    )
    created = await call.services.notebooks.create_note(agent.notebook, title, body)
    return {
        "created": True,
        "notebook": agent.notebook,
        "title": title,
        "note_id": created.get("note_id"),
    }


def datetime_stamp(call: ToolCall) -> str:
    return dt.datetime.now(call.services.gary.timezone).strftime("%Y-%m-%d %H:%M %Z")


def _sync(function):
    import asyncio

    async def handler(call: ToolCall):
        return await asyncio.to_thread(function, call)

    return handler


TOOL_CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "web_search",
            "Search the public web for current information. Returns an answer with source URLs. "
            "Read-only; cannot log in, submit forms, or browse interactively.",
            WebSearchArgs,
            web_search,
        ),
        ToolSpec("read_project", "Read one project and its tasks, with readiness.", ProjectArgs, _sync(_read_project)),
        ToolSpec("read_projects", "List active projects.", NoArgs, _sync(_read_projects)),
        ToolSpec(
            "read_tasks",
            "Read tasks, optionally for one project, with status, estimates, deadlines, scheduling, and blockers.",
            TasksArgs,
            _sync(_read_tasks),
        ),
        ToolSpec("read_dependencies", "Read task dependencies for a project.", ProjectArgs, _sync(_read_dependencies)),
        ToolSpec(
            "read_relevant_notes",
            "Read Gary's planning notes for a project, Alex's preferences note, and the latest daily summary.",
            NotesArgs,
            read_relevant_notes,
        ),
        ToolSpec(
            "read_previous_research",
            "Read your own earlier completed research reports, optionally filtered by keywords.",
            PreviousResearchArgs,
            _sync(_read_previous_research),
        ),
        ToolSpec(
            "read_agent_permissions",
            "Read every GaryCorp agent's tools, delegation rights, and execution limits.",
            NoArgs,
            _sync(_read_agent_permissions),
        ),
        ToolSpec(
            "read_action_policy",
            "Read the action risk policy (green, yellow, red) and approval rules.",
            NoArgs,
            _sync(_read_action_policy),
        ),
        ToolSpec(
            "read_audit_events",
            "Read recent audit events (time, actor, type, summary; no details), optionally by event type.",
            AuditArgs,
            _sync(_read_audit_events),
        ),
        ToolSpec(
            "read_system_configuration_summary",
            "Read a non-secret summary of Gary's deployment: services, network exposure, integration scopes, and limits.",
            NoArgs,
            _sync(_read_system_configuration_summary),
        ),
        ToolSpec(
            "read_calendar_availability",
            "Read free work blocks within working hours (busy time without titles) and Gary's scheduled task blocks.",
            AvailabilityArgs,
            read_calendar_availability,
        ),
        ToolSpec("read_commitments", "Read commitments made to other people.", CommitmentsArgs, _sync(_read_commitments)),
        ToolSpec("read_followups", "Read pending follow-ups.", NoArgs, _sync(_read_followups)),
        ToolSpec(
            "write_note",
            "Create a note in your own Joplin notebook (Markdown body). Use for findings, "
            "decisions, or context worth keeping beyond this report. You cannot read, "
            "edit, or delete notes, or write to any other notebook.",
            WriteNoteArgs,
            write_note,
        ),
    )
}

# Tools that are never granted to any specialist in this version, whatever a
# roster edit says. A roster entry listing one fails at startup.
FORBIDDEN_TOOLS = frozenset(
    {
        "send_email",
        "send_external_email",
        "modify_calendar",
        "spend_money",
        "change_permissions",
        "modify_permissions",
        "execute_shell",
        "execute_sql",
        "modify_security_policy",
        "create_external_accounts",
        "delete_data",
        "delete_audit_log",
        "delegate_to_agent",
    }
)


def validate_roster_tools(registry: AgentRegistry) -> None:
    for definition in registry.employees():
        for tool in definition.allowed_tools:
            if tool in FORBIDDEN_TOOLS:
                raise ValueError(f"{definition.agent_id} may not be granted {tool}")
            if tool not in TOOL_CATALOG:
                raise ValueError(f"{definition.agent_id} lists unknown tool {tool}")


def _audit_arguments(arguments: dict) -> dict:
    """Long text (such as a note body) is shortened in the audit log."""
    return {
        key: (value[:300] + f"... [{len(value)} characters]" if isinstance(value, str) and len(value) > 300 else value)
        for key, value in arguments.items()
    }


def _truncate(result: Any) -> Any:
    text = json.dumps(result, default=str)
    if len(text) <= RESULT_CHAR_LIMIT:
        return result
    return {"truncated": True, "partial_json": text[:RESULT_CHAR_LIMIT]}


class ToolGateway:
    def __init__(
        self,
        services: AgentServices,
        agent: GaryCorpAgentDefinition,
        state: RunState,
        limits: AgentLimits,
    ):
        self.services = services
        self.agent = agent
        self.state = state
        self.limits = limits

    def tools(self) -> list[ToolSpec]:
        """The specs this agent was granted: the only tools handed to CrewAI."""
        return [TOOL_CATALOG[name] for name in self.agent.allowed_tools]

    def _audit(self, event_type: str, summary: str, details: dict) -> None:
        with self.services.gary.db.transaction() as conn:
            Repositories.bind(conn).audit.write(
                self.agent.agent_id,
                event_type,
                summary,
                "agent_assignment",
                self.state.assignment_id,
                details,
            )

    async def call(self, tool_name: str, arguments: dict | None) -> Any:
        import asyncio

        if self.state.cancelled:
            raise ToolDenied("This assignment has been stopped; no further tool calls are allowed")

        if tool_name not in self.agent.allowed_tools or tool_name not in TOOL_CATALOG:
            await asyncio.to_thread(
                self._audit,
                "agent_tool_denied",
                f"{self.agent.name} was denied tool {tool_name}",
                {"tool": tool_name},
            )
            raise ToolDenied(f"{self.agent.name} is not permitted to use {tool_name}")

        spec = TOOL_CATALOG[tool_name]
        if self.state.tool_calls >= self.limits.max_tool_calls_per_run:
            raise ToolDenied(f"Tool call limit of {self.limits.max_tool_calls_per_run} reached for this assignment")
        per_tool_limit = spec.max_calls_per_run
        if tool_name == "web_search":
            per_tool_limit = self.limits.max_web_searches_per_run
        elif tool_name == "write_note":
            per_tool_limit = self.limits.max_notes_per_run
        if per_tool_limit is not None and self.state.calls_by_tool.get(tool_name, 0) >= per_tool_limit:
            raise ToolDenied(f"{tool_name} may be used at most {per_tool_limit} times per assignment")

        args = validate_request(spec.args_model, arguments or {})
        self.state.tool_calls += 1
        self.state.calls_by_tool[tool_name] = self.state.calls_by_tool.get(tool_name, 0) + 1

        try:
            result = await spec.handler(ToolCall(self.services, self.agent, self.state, args))
            if isinstance(result, dict) and isinstance(result.get("_usage"), dict):
                for key, value in result.pop("_usage").items():
                    self.state.tool_usage[key] = self.state.tool_usage.get(key, 0) + value
            ok, error = True, None
        except (ValueError, ToolDenied) as exc:
            result, ok, error = {"error": str(exc)}, False, str(exc)
        except Exception as exc:  # integration failure: report, do not crash the run
            logger.exception("Tool %s failed for %s", tool_name, self.agent.agent_id)
            result, ok, error = {"error": f"{tool_name} failed: {type(exc).__name__}"}, False, str(exc)

        await asyncio.to_thread(
            self._audit,
            "agent_tool_called",
            f"{self.agent.name} used {tool_name}",
            {
                "tool": tool_name,
                "arguments": _audit_arguments(json.loads(args.model_dump_json())),
                "ok": ok,
                "error": error,
            },
        )
        return _truncate(result)

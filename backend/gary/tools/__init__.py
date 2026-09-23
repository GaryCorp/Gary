"""Gary's interface to the operations database.

These narrow tools are the only way the model reaches SQLite. There is
deliberately no tool to run SQL, open a database shell, delete the database,
edit or delete the audit log, or change the approval policy.
"""

import logging
import sqlite3

from gary.integrations.github.exceptions import GitHubError
from gary.services.engineering_service import EngineeringError
from gary.tools import (
    agent_tools,
    approval_tools,
    conversation_tools,
    engineering_tools,
    followup_tools,
    hiring_tools,
    planning_tools,
    project_tools,
    reorg_tools,
    review_tools,
    task_tools,
)
from gary.tools.base import Tool, ToolContext

logger = logging.getLogger("gary.tools")

REGISTRY: dict[str, Tool] = {
    tool.name: tool
    for module in (
        project_tools,
        task_tools,
        followup_tools,
        planning_tools,
        approval_tools,
        agent_tools,
        conversation_tools,
        engineering_tools,
        hiring_tools,
        reorg_tools,
        review_tools,
    )
    for tool in module.TOOLS
}

TOOL_SCHEMAS = [tool.schema() for tool in REGISTRY.values()]
TOOL_NAMES = frozenset(REGISTRY)


async def call_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    tool = REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")

    try:
        result = await tool.handler(arguments or {}, ctx)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except (GitHubError, EngineeringError) as exc:
        # GitHub refused or was unreachable: report the failure, never a
        # success Gary could read out as a created ticket.
        logger.warning("GitHub engineering tool %s failed: %s", name, exc)
        return {"success": False, "error": str(exc)}
    except sqlite3.IntegrityError as exc:
        # Constraints are a second line of defense behind validation.
        logger.warning("Integrity error in %s: %s", name, exc)
        return {"success": False, "error": f"Rejected by the database: {exc}"}

    return {"success": True, **result}


__all__ = ["REGISTRY", "TOOL_NAMES", "TOOL_SCHEMAS", "ToolContext", "call_tool"]

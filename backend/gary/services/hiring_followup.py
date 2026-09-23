"""Closing the loop on a hire.

Gary argued for a colleague, Alex approved it and built them. Whether that
was worth doing is answerable a few weeks later, from the same record any
other performance question is answered from, and the right place to say it is
the issue where the argument was made.

So: once a proposed colleague exists and has done some work, Gary comments on
their hire issue with what they have actually done. Once per hire, and only
with arithmetic -- no model call, nothing that could flatter the decision.
"""

import datetime as dt
import json
import logging

from gary.db.repositories import Repositories
from gary.policy import GARY_ACTOR
from gary.services.common import Clock, clock_now, default_clock
from gary.services.performance import MIN_ASSIGNMENTS, employee_scorecard
from gary.timeutil import format_utc, to_datetime

logger = logging.getLogger("gary.hiring")

REQUESTED_EVENT = "employee_requested"
FOLLOWUP_EVENT = "hire_followed_up"
# Long enough that the first report is not the whole story.
REVIEW_DAYS = 30


def hire_comment(agent_name: str, scorecard: dict) -> str:
    """What the hire has done, in numbers, for the issue that asked for them."""
    lines = [
        f"{agent_name} has been working. Since they were built:",
        "",
        f"- assignments: {scorecard['assignments']}"
        f" ({scorecard['completed']} completed, {scorecard['failed']} failed)",
    ]
    if scorecard.get("mean_stated_confidence") is not None:
        lines.append(
            f"- confidence they reported, on average: "
            f"{scorecard['mean_stated_confidence']:.0%}"
        )
    if scorecard.get("confident_failures"):
        # The number worth reading: sure of itself, and wrong.
        lines.append(f"- confident failures: {scorecard['confident_failures']}")
    if scorecard.get("tool_denials"):
        lines.append(f"- tool calls refused by the gateway: {scorecard['tool_denials']}")
    if scorecard.get("total_cost_usd"):
        lines.append(f"- cost of their model calls: ${scorecard['total_cost_usd']:.2f}")
    lines += ["", "Posted by Gary, from the company's own record. No model wrote this."]
    return "\n".join(lines)


async def follow_up_on_hires(gary, engineering, registry, clock: Clock = default_clock) -> list[dict]:
    """Comment on the hire issue of every proposed colleague who now exists
    and has done some work. Returns what was commented on."""
    if engineering is None:
        return []

    now = clock_now(clock)
    since = format_utc(to_datetime(now) - dt.timedelta(days=REVIEW_DAYS))
    built = {definition.agent_id for definition in registry.all()}

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        requested = repos.audit.list_since(REQUESTED_EVENT, since="")
        done = {event["entity_id"] for event in repos.audit.list_since(FOLLOWUP_EVENT, since="")}

        pending = []
        for event in requested:
            agent_id = event["entity_id"]
            if agent_id in done or agent_id not in built:
                continue
            details = json.loads(event["details_json"] or "{}")
            issue_number = details.get("issue_number")
            if not issue_number:
                continue
            scorecard = employee_scorecard(repos, agent_id, since, now)
            if scorecard["assignments"] < MIN_ASSIGNMENTS:
                continue
            row = repos.engineering.get_by_issue(
                engineering.config.owner, engineering.config.repository, int(issue_number)
            )
            if row is None:
                continue
            definition = registry.get(agent_id)
            pending.append((agent_id, definition.name, row, scorecard))

    commented = []
    for agent_id, name, row, scorecard in pending:
        try:
            await engineering.add_comment(row, hire_comment(name, scorecard), actor=GARY_ACTOR)
        except Exception as exc:  # GitHub down: try again tomorrow
            logger.warning("Could not comment on %s's hire issue: %s", agent_id, exc)
            continue
        _record(gary, agent_id, row, scorecard, clock)
        commented.append({"agent_id": agent_id, "issue_number": row["github_issue_number"]})
    return commented


def _record(gary, agent_id: str, row: dict, scorecard: dict, clock: Clock) -> None:
    with gary.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            GARY_ACTOR,
            FOLLOWUP_EVENT,
            f"Reported {agent_id}'s record on issue #{row['github_issue_number']}",
            "agent",
            agent_id,
            {
                "issue_number": row["github_issue_number"],
                "assignments": scorecard["assignments"],
                "completed": scorecard["completed"],
                "failed": scorecard["failed"],
            },
            now=clock_now(clock),
        )

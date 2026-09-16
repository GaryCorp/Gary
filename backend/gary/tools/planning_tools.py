import datetime as dt
import logging
from typing import Literal

from pydantic import Field

from gary.models.common import RequestModel, validate_request
from gary.services.calendar_blocks import find_free_blocks
from gary.services.planning_service import RecordPlanRequest
from gary.timeutil import format_utc
from gary.tools.base import Tool, ToolContext, integer, obj, present, run_sync, string

logger = logging.getLogger("gary.tools.planning")

# A requested planning cycle makes a model call and may change the calendar,
# so it cannot be repeated in quick succession.
RUN_CYCLE_MIN_GAP_MINUTES = 10


class BriefRequest(RequestModel):
    kind: Literal["morning", "midday", "evening"]


class WorkBlocksRequest(RequestModel):
    start_date: dt.date | None = None
    days: int = Field(default=1, strict=True, ge=1, le=7)
    minutes: int = Field(default=60, strict=True, ge=15, le=240)
    limit: int = Field(default=8, strict=True, ge=1, le=20)


class RunCycleRequest(RequestModel):
    planning_type: Literal["morning", "midday", "evening", "manual"]


async def planning_get_context(args: dict, ctx: ToolContext) -> dict:
    if args:
        raise ValueError("planning_get_context takes no arguments")
    context = await run_sync(ctx.gary.planning.get_planning_context, "manual")
    ctx.approval_ids().update(a["id"] for a in context["pending_approvals"])
    result = {"context": present(context, ctx)}

    notebook = ctx.integrations.get("notebook")
    if notebook is not None:
        today = dt.datetime.now(ctx.gary.timezone).date()
        try:
            result["planning_notes"] = await notebook.get_relevant_notes(
                [p["name"] for p in context["active_projects"]], today
            )
        except Exception as exc:
            logger.warning("Planning notes unavailable: %s", exc)
            result["planning_notes_error"] = str(exc) or type(exc).__name__
    return result


async def planning_get_brief(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(BriefRequest, args)
    brief = await run_sync(ctx.gary.briefing.build, request.kind)
    return {"brief": present(brief, ctx)}


async def planning_find_work_blocks(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(WorkBlocksRequest, args)
    calendar = ctx.integration("calendar")
    timezone = ctx.gary.timezone

    now = dt.datetime.now(dt.timezone.utc)
    if request.start_date is None:
        start = now
    else:
        start = max(now, dt.datetime.combine(request.start_date, dt.time(), timezone))
    first_day = start.astimezone(timezone).date()
    end = dt.datetime.combine(first_day + dt.timedelta(days=request.days), dt.time(), timezone)

    start_iso, end_iso = format_utc(start), format_utc(end)
    busy = await calendar.busy_intervals(start_iso, end_iso)
    blocks = find_free_blocks(
        start_iso, end_iso, busy, ctx.gary.week, timezone, request.minutes, request.limit
    )
    return {
        **ctx.gary.week.describe(),
        "count": len(blocks),
        "blocks": present(blocks, ctx),
    }


async def planning_run_cycle(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(RunCycleRequest, args)
    cycle = ctx.integration("planning_cycle")
    try:
        result = await cycle.run(request.planning_type, min_gap_minutes=RUN_CYCLE_MIN_GAP_MINUTES)
    except Exception as exc:
        raise ValueError(f"Planning cycle did not complete: {exc}") from exc

    for item in result["results"]:
        if item["result"].get("approval_id"):
            ctx.approval_ids().add(item["result"]["approval_id"])
    return {
        "briefing": result["briefing"],
        "summary": result["summary"],
        "brief": present(result["brief"], ctx),
        "actions": [
            {
                "action_type": item["proposal"]["action_type"],
                "task": item["proposal"]["task_title"],
                "status": item["result"].get("status"),
                "summary": item["result"].get("summary"),
                "error": item["result"].get("error"),
            }
            for item in result["results"]
        ],
        "not_accepted": [
            {"action_type": item["proposal"].get("action_type"), "reason": item["reason"]}
            for item in result["rejected"]
        ],
        "summary_note_written": result["summary_error"] is None,
    }


async def planning_record_plan(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(RecordPlanRequest, args)
    run = await run_sync(ctx.gary.planning.record_plan, request)
    return {"planning_run": present(run, ctx)}


TOOLS = [
    Tool(
        "planning_get_brief",
        "Get the facts for a Chief of Staff update without changing anything: "
        "primary objective with deadline capacity, today's scheduled work, risks "
        "(missed blocks, overdue work, blocked critical tasks, commitments due "
        "soon, deadlines at risk), decisions needed, and due follow-ups. midday "
        "adds what finished and what remains; evening adds completed, unfinished, "
        "blocked, moved, new follow-ups, and tomorrow. Use for give me my morning "
        "brief, how is today going, or what am I behind on.",
        obj({"kind": string("Which update.", ["morning", "midday", "evening"])}, ("kind",)),
        planning_get_brief,
    ),
    Tool(
        "planning_find_work_blocks",
        "Find free work blocks in the user's calendar within working hours, "
        "skipping busy time and protected times such as lunch. Use before "
        "scheduling tasks with action_propose schedule_task.",
        obj(
            {
                "start_date": string("First day, YYYY-MM-DD; default today."),
                "days": integer("Number of days to search, 1 to 7; default 1.", 1, 7),
                "minutes": integer("Minimum block length in minutes; default 60.", 15, 240),
                "limit": integer("Most blocks to return; default 8.", 1, 20),
            }
        ),
        planning_find_work_blocks,
    ),
    Tool(
        "planning_run_cycle",
        "Run a full planning cycle now: gathers the brief, calendar, planning "
        "notes, and unread email, lets the planner schedule or move task blocks "
        "and create follow-ups within policy, and writes the daily summary note. "
        "Use when the user asks to replan (manual or midday), for the morning "
        "brief with scheduling (morning), or to close out the day (evening). "
        "Takes a few seconds; it cannot run again within 10 minutes.",
        obj(
            {
                "planning_type": string(
                    "morning, midday, evening, or manual.",
                    ["morning", "midday", "evening", "manual"],
                )
            },
            ("planning_type",),
        ),
        planning_run_cycle,
    ),
    Tool(
        "planning_get_context",
        "Get the full operational picture in one call: active projects, tasks "
        "that are ready (ranked by planning_score), in progress, blocked (and by "
        "what), overdue tasks, upcoming deadlines, due follow-ups, open "
        "commitments, pending approvals, recent actions, missed scheduled "
        "blocks, and relevant planning notes. Use when planning, when the user "
        "asks what to work on, what is blocking something, or what is due. "
        "Starts a planning run.",
        obj({}),
        planning_get_context,
    ),
    Tool(
        "planning_record_plan",
        "After planning with planning_get_context, record the plan you agreed "
        "with the user and the action_ids you proposed, so it can be reviewed "
        "later.",
        obj(
            {
                "planning_run_id": string(
                    "planning_run_id from planning_get_context; omit if you did not call it."
                ),
                "summary": string("The plan and why, in a few sentences."),
                "action_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "action_ids from action_propose for this plan.",
                },
            },
            ("summary",),
        ),
        planning_record_plan,
    ),
]

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    AGENT_LIMITS,
    AGENT_WEB_SEARCH_MODEL,
    CARD_ENCRYPTION_KEY,
    CARD_VAULT_FILE,
    EASE_API_KEY,
    EASE_API_URL,
    EMAIL_CHECK_INTERVAL_MINUTES,
    DAILY_REPORT_TIME,
    EMAIL_COMMAND_POLL_MINUTES,
    EVENING_CHECKIN_TIME,
    EVENT_PLANNING_MIN_GAP_MINUTES,
    GARY_BACKUP_DIR,
    GARY_BACKUP_KEEP,
    GARY_DB_PATH,
    GARY_EMPLOYEE_MODEL,
    GARY_MAILBOX,
    GITHUB_SYNC_INTERVAL_MINUTES,
    GITHUB_TOKEN,
    LOCAL_TIMEZONE,
    MANAGEMENT_TICK_MINUTES,
    MANAGEMENT_WEEKDAYS,
    MAX_DAILY_AI_SPEND_USD,
    MAX_MANAGEMENT_CYCLES_PER_DAY,
    MORNING_ASSIGNMENT_TIME,
    OPENAI_API_KEY,
    OPENAI_REALTIME_MODEL,
    OPS_CHECK_INTERVAL_MINUTES,
    PERPLEXITY_API_KEY,
    PERPLEXITY_PRESET,
    PRODUCT_SEARCH_MAX_ROUNDS,
    PRODUCT_SEARCH_TICK_MINUTES,
    PLANNING_MAX_ACTIONS,
    PLANNING_MODEL,
    PLANNING_SCHEDULE,
    PLANNING_WEEKDAYS,
    MAX_REVIEWS_PER_ROUND,
    PRINCIPAL_NAME,
    PRODUCTION_SCHEDULE,
    REQUIRE_PRICED_MODELS,
    REVIEW_INTERVAL_DAYS,
    REVIEW_PERIOD_DAYS,
    SESSION_SECRET,
    SPENDING_LIMITS,
    SPOKEN_DELIVERY_SECONDS,
    USER_MAILBOX,
    VOICE_BRIDGE_TOKEN,
    VOICE_MODE,
    VOICE_TEXT_MODEL,
    VOICE_TRANSCRIBE_MODEL,
    WAKE_WORD_DISPLAY,
    WEEKLY_REVIEW_DAY,
    WORK_WEEK,
)
from app.announcements import approval_announcement, operations_announcement
from app.calendar_actions import external_action_handlers
from app.gmail import (
    GmailUnreadSummaries,
    check_new_emails,
    fetch_command_emails,
    email_watcher,
    gary_email_watcher,
    in_quiet_hours,
    single_line,
)
from app.google_auth import credentials_for_active_user, gary_mailbox_configured, store
from app.google_calendar import GoogleBusyCalendar
from app.instructions import build_instructions as build_prompt
from app.local_time import spoken_clock
from app.notebooks import JoplinAgentNotebooks, JoplinPlanningNotebook, SpokenNotebook
from app.pages import router as pages_router
from app.realtime_session import RealtimeSession
from app.system_summary import system_configuration_summary
from app.tool_dispatch import run_integration_tool
from app.transient import log_loop_failure
from app.voice_tools import VOICE_TOOLS
from app.voice_turn import VoiceTurn, VoiceTurnError
from gary import build_gary
from gary.agents.ease import EaseFramework
from gary.agents.gateway import AgentServices, validate_roster_tools
from gary.agents.roster import AgentRegistry
from gary.agents.runner import GaryCorpAgentRunner
from gary.agents.perplexity import PerplexityResearch
from gary.agents.service import AgentService
from gary.agents.web import OpenAIWebResearch
from gary.backup import backup_daily
from gary.db.repositories import Repositories
from gary.finance import cards as finance_cards
from gary.finance.pricing import PriceTable, rate_phrase, usage_from_openai
from gary.finance.provider_costs import OpenAICosts, ProviderCostsError
from gary.finance.purchases import card_purchase_handler
from gary.finance.usage import SpendCeilingReached, SpendGate, UsageLedger
from gary.integrations.github import (
    EnvTokenProvider,
    GitHubClient,
    GitHubConfig,
    GitHubError,
    configured as github_configured,
)
from gary.models.action import ProposeActionRequest
from gary.planner import OpenAIPlanner
from gary.reviewer import OpenAIReviewer
from gary.policy import SYSTEM_ACTOR
from gary.services.accountability import Accountability
from gary.services.conversation_service import SpokenDelivery
from gary.services.daily_report import DailyReport
from gary.services.engineering_actions import engineering_action_handlers
from gary.services.engineering_service import EngineeringTicketService
# dismiss_employee and GARY_TOOL_SCHEMAS are read from app.main by
# hiring_cli and ask.
from gary.services.hiring_actions import (
    dismiss as dismiss_employee,
    hire_action_handler,
)
from gary.services.hiring_followup import follow_up_on_hires
from gary.services.management_loop import (
    DailyBudget,
    MIN_GAP_MINUTES,
    completed_since,
    find_triggers,
)
from gary.services.planning_cycle import (
    PlanningCycle,
    PlanningCycleError,
    due_planning_types,
)
from gary.services.operating import OperatingState
from gary.services.product_search import ProductSearchService
from gary.services.production import ProductionService, episode_label
from gary.services.review_service import PerformanceReviews
from gary.services.reorg_actions import reorg_action_handler
from gary.services.team_actions import team_action_handlers
from gary.services.weekly_review import WeeklyReview
from gary.timeutil import format_utc, to_local
from gary.tools import (
    TOOL_NAMES as GARY_TOOL_NAMES,
    TOOL_SCHEMAS as GARY_TOOL_SCHEMAS,
    ToolContext as GaryToolContext,
    call_tool as call_gary_tool,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The org chart mirrors the roster; assignments cut off by a restart are
    # failed, and queued ones start again.
    await asyncio.to_thread(agent_service.sync_roster)
    await agent_service.recover_interrupted()
    # Scheduled planning runs in the backend, whether or not voice is connected.
    scheduler = (
        asyncio.create_task(run_planning_scheduler()) if PLANNING_SCHEDULE else None
    )
    # GitHub is checked in the background: an outage must not stop Gary starting.
    engineering_sync = (
        asyncio.create_task(run_engineering_sync())
        if engineering_service and GITHUB_SYNC_INTERVAL_MINUTES > 0
        else None
    )
    # The company keeps running between the scheduled cycles.
    management = (
        asyncio.create_task(run_management_loop()) if MANAGEMENT_TICK_MINUTES > 0 else None
    )
    # Anything Gary decided to say and could not deliver yet, including the
    # Joplin note for what he already said.
    speaking = asyncio.create_task(run_spoken_delivery())
    # The weekly video schedule, planned ahead and put on the calendar.
    production_loop = (
        asyncio.create_task(run_production_schedule()) if production else None
    )
    # Gary as Alex's manager: the morning assignment, the evening check-in
    # and the daily report.
    managing = (
        asyncio.create_task(run_accountability())
        if MORNING_ASSIGNMENT_TIME or EVENING_CHECKIN_TIME or DAILY_REPORT_TIME
        else None
    )
    # The product search picks its own rounds back up after a restart.
    product_searching = (
        asyncio.create_task(run_product_search_loop())
        if product_search is not None and PRODUCT_SEARCH_TICK_MINUTES > 0
        else None
    )
    # Alex pausing or resuming the company by email, from anywhere.
    commands = (
        asyncio.create_task(run_email_commands())
        if EMAIL_COMMAND_POLL_MINUTES > 0 and gary_mailbox_configured()
        else None
    )
    try:
        yield
    finally:
        for task in (
            scheduler, engineering_sync, management, speaking, production_loop,
            managing, commands, product_searching,
        ):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


app = FastAPI(title="Local AI Calendar Assistant", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,  # localhost only; change for a real HTTPS deployment.
)
app.include_router(pages_router)


async def announce_new_emails(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(EMAIL_CHECK_INTERVAL_MINUTES * 60)

        if in_quiet_hours(dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))):
            continue

        checks = [(USER_MAILBOX, email_watcher)]
        if gary_mailbox_configured():
            checks.append((GARY_MAILBOX, gary_email_watcher))

        # Each inbox is checked on its own, so one that fails (Gary's not yet
        # signed in, say) does not stop the other being announced.
        for mailbox, watcher in checks:
            try:
                announcement = await check_new_emails(watcher, mailbox)
            except Exception as exc:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "bridge.notice",
                            "message": f"New email check skipped ({mailbox}): {exc}",
                        }
                    )
                )
                continue

            if announcement:
                await speak_to_user(announcement, source="email")


# ---------------------------------------------------------------------------
# Chief of Staff operations (SQLite). Business logic lives in the gary
# package; this section supplies the actions that need Google credentials.
# ---------------------------------------------------------------------------

logger = logging.getLogger("gary.backend")


class _LazyRegistry:
    """The registry is built after Gary's container; the hire handler holds
    this and resolves the real one on use."""

    def __getattr__(self, name):
        registry = globals().get("agent_registry")
        if registry is None:
            raise RuntimeError("The GaryCorp roster is not ready yet")
        return getattr(registry, name)


def _lazy_registry() -> "_LazyRegistry":
    return _LazyRegistry()


gary_ops = build_gary(
    GARY_DB_PATH,
    LOCAL_TIMEZONE,
    action_handlers={
        **external_action_handlers(),
        "card_purchase": card_purchase_handler(SPENDING_LIMITS, ZoneInfo(LOCAL_TIMEZONE)),
        # Resolved lazily: the team is wired after Gary's container exists.
        **team_action_handlers(lambda: globals().get("agent_service")),
        # Hiring: approved on the web page only, and approval files an
        # engineering ticket rather than creating the colleague.
        **hire_action_handler(
            _lazy_registry(), lambda: globals().get("engineering_service")
        ),
        # Alex's engineering queue. Resolved lazily and absent in effect when
        # GitHub is not configured: the handlers then refuse.
        **engineering_action_handlers(lambda: globals().get("engineering_service")),
        # Reorganising the company: proposed by Gary, decided by Alex, landed
        # as a roster.py change.
        **reorg_action_handler(
            _lazy_registry(), lambda: globals().get("engineering_service")
        ),
    },
    work_week=WORK_WEEK,
)
card_vault = finance_cards.CardVault(CARD_VAULT_FILE, CARD_ENCRYPTION_KEY)

# What the company's thinking costs. Prices live on the data volume and are
# set with "python -m app.costs set-price"; unpriced models are reported as
# unpriced, never as free.
model_prices = PriceTable()
usage_ledger = UsageLedger(gary_ops.db, model_prices, ZoneInfo(LOCAL_TIMEZONE))
# Which model does which job. The ledger only ever sees models that have
# already been called, so this is what lets Gary say what he is about to use
# and whether it can be costed.
MODEL_ROLES = {
    **(
        {"voice": OPENAI_REALTIME_MODEL}
        if VOICE_MODE == "realtime"
        else {
            "voice transcription": VOICE_TRANSCRIBE_MODEL,
            "voice": VOICE_TEXT_MODEL,
        }
    ),
    "planning": PLANNING_MODEL,
    "specialists": GARY_EMPLOYEE_MODEL,
    "web search": AGENT_WEB_SEARCH_MODEL,
    # Susan's Perplexity search is deliberately absent: Perplexity picks the
    # model and reports what each call cost, so its spend reaches the ledger
    # as the provider's own figure and needs no price here. A model listed
    # here without a price stops unattended work, which would be the wrong
    # answer for a provider that prices itself.
}
# Asked before anything calls a model. See SpendGate: with an unpriced model
# it reports that it cannot be enforced rather than implying safety.
spend_gate = SpendGate(
    usage_ledger,
    MAX_DAILY_AI_SPEND_USD,
    roles=MODEL_ROLES,
    require_priced=REQUIRE_PRICED_MODELS,
)
# Billed costs straight from the provider, when an admin key with the
# api.usage.read scope is configured. Read-only: it can see spend, nothing else.
provider_costs = OpenAICosts(os.getenv("OPENAI_ADMIN_KEY", ""))


def run_daily_backup() -> Path | None:
    return backup_daily(
        GARY_DB_PATH,
        GARY_BACKUP_DIR,
        dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE)).date(),
        keep=GARY_BACKUP_KEEP,
    )


try:
    run_daily_backup()
except Exception:
    logger.exception("Startup backup of %s failed", GARY_DB_PATH)


async def run_engineering_sync() -> None:
    """Pull GitHub state into SQLite on a timer. Webhooks can replace this
    later; nothing else depends on the polling."""
    # A short delay so startup is not spent waiting on GitHub.
    await asyncio.sleep(30)
    while True:
        try:
            result = await engineering_service.sync_all()
            if result["checked"]:
                logger.info(
                    "Engineering sync: %s checked, %s synced, %s need reconciliation",
                    result["checked"], result["synced"], len(result["needs_reconciliation"]),
                )
        except Exception as exc:
            log_loop_failure(logger, exc, "Engineering ticket synchronization")
        await asyncio.sleep(GITHUB_SYNC_INTERVAL_MINUTES * 60)


async def announce_operations(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(OPS_CHECK_INTERVAL_MINUTES * 60)

        try:
            await asyncio.to_thread(run_daily_backup)
        except Exception:
            logger.exception("Daily backup of %s failed", GARY_DB_PATH)

        # Alerts found during quiet hours are announced at the first check after.
        if in_quiet_hours(dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))):
            continue

        try:
            alerts = await asyncio.to_thread(gary_ops.planning.collect_new_alerts)
        except Exception as exc:
            log_loop_failure(logger, exc, "Operations check")
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "bridge.notice",
                        "message": f"Operations check skipped: {exc}",
                    }
                )
            )
            continue

        announcement = operations_announcement(alerts)
        if announcement:
            await speak_to_user(announcement, source="operations")

        if alerts.get("missed_blocks") and PLANNING_SCHEDULE:
            await replan_after_missed_block()


async def replan_after_missed_block() -> None:
    """Event-triggered planning, at most every EVENT_PLANNING_MIN_GAP_MINUTES
    and only during working time."""
    now_local = dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))
    if (
        now_local.weekday() not in WORK_WEEK.days
        or not WORK_WEEK.hours.start <= now_local.hour < WORK_WEEK.hours.end
    ):
        return
    try:
        result = await planning_cycle.run(
            "event_triggered", min_gap_minutes=EVENT_PLANNING_MIN_GAP_MINUTES
        )
    except Exception as exc:
        logger.warning("Event-triggered planning skipped: %s", exc)
        return
    if result["briefing"]:
        await speak_to_user(result["briefing"], source="briefing")


# ---------------------------------------------------------------------------
# Scheduled planning cycle: Joplin planning notes and daily summaries, busy
# calendar times, the planner model call, and the scheduler.
# ---------------------------------------------------------------------------

voice_connections: set[WebSocket] = set()


planning_calendar = GoogleBusyCalendar()
planning_notebook = JoplinPlanningNotebook()
planning_cycle = PlanningCycle(
    gary_ops,
    OpenAIPlanner(OPENAI_API_KEY, PLANNING_MODEL, PRINCIPAL_NAME),
    planning_notebook,
    planning_calendar,
    GmailUnreadSummaries(),
    max_actions=PLANNING_MAX_ACTIONS,
    usage=usage_ledger,
)
def load_hired_employees():
    """Employees GaryCorp hired for itself, re-validated on every load."""
    from gary.agents.hiring import definitions_from_rows

    try:
        with gary_ops.db.read() as conn:
            rows = Repositories.bind(conn).hires.list_active()
    except Exception:
        logger.exception("Could not read hired employees; keeping the static roster")
        return ()
    return definitions_from_rows(rows)


agent_registry = AgentRegistry(limits=AGENT_LIMITS, hired_source=load_hired_employees)
validate_roster_tools(agent_registry)
agent_services = AgentServices(
    gary=gary_ops,
    registry=agent_registry,
    web=OpenAIWebResearch(OPENAI_API_KEY, AGENT_WEB_SEARCH_MODEL),
    research=(
        PerplexityResearch(PERPLEXITY_API_KEY, PERPLEXITY_PRESET) if PERPLEXITY_API_KEY else None
    ),
    notes=planning_notebook,
    calendar=planning_calendar,
    notebooks=JoplinAgentNotebooks(),
    system_summary=lambda: system_configuration_summary(
        card_vault.configured, engineering_service is not None
    ),
    manager_tools=tuple(sorted(GARY_TOOL_NAMES)),
    spending_limits=SPENDING_LIMITS,
    ease=EaseFramework(EASE_API_URL, EASE_API_KEY) if EASE_API_URL else None,
    usage=usage_ledger,
)


def build_engineering_service() -> EngineeringTicketService | None:
    """The engineering integration, or None when it is not configured.

    Gary starts either way: without it the engineering tools report that the
    integration is unavailable, and no ticket is ever claimed to exist.
    """
    if not github_configured():
        logger.info("GitHub engineering integration is not configured; tickets are unavailable")
        return None
    try:
        config = GitHubConfig.from_env()
        client = GitHubClient(config, EnvTokenProvider(GITHUB_TOKEN))
        return EngineeringTicketService(gary_ops, client, clock=gary_ops.planning.clock)
    except GitHubError as exc:
        logger.warning("GitHub engineering integration is misconfigured: %s", exc)
        return None


engineering_service = build_engineering_service()


def build_agent_executor():
    # Imported here so the rest of Gary starts even if CrewAI cannot load.
    from gary.agents.crew import CrewAIExecutor

    return CrewAIExecutor(OPENAI_API_KEY)


def product_search_round_for(assignment_id: str) -> dict | None:
    with gary_ops.db.read() as conn:
        return Repositories.bind(conn).product_search.round_for_assignment(assignment_id)


async def announce_finished_product_searches() -> None:
    """Advance every running search and say which of them ended."""
    if product_search is None:
        return
    for finished in await product_search.advance_all():
        idea = finished["best_idea"]
        if not idea:
            await announce_to_voice(
                f"The product search ended without an idea: {single_line(finished['stop_reason'] or '')}."
            )
            continue
        await announce_to_voice(
            f"Susan has finished the product search after "
            f"{finished['rounds_completed']} rounds. The best idea is {single_line(idea)}, "
            f"scoring {finished['best_score']} out of ten. "
            f"Say {WAKE_WORD_DISPLAY}, what should I build, for the rest."
        )


async def run_product_search_loop() -> None:
    """The safety net for the product search.

    Rounds normally follow each other: a finished assignment advances the
    search. This picks up the ones nothing will wake, after a restart, a
    pause, or a day that hit the spend ceiling. It is one indexed query when
    there is nothing to do.
    """
    while True:
        try:
            await announce_finished_product_searches()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(logger, exc, "Advancing the product search")
        await asyncio.sleep(max(60.0, PRODUCT_SEARCH_TICK_MINUTES * 60))


async def announce_assignment_finished(assignment: dict) -> None:
    if in_quiet_hours(dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))):
        return
    if assignment["review_id"] and assignment["review_round"] == 1:
        # Announce a review once, when its last first-round report is in.
        review = await asyncio.to_thread(agent_service.get_review, assignment["review_id"])
        if review.status != "running":
            await announce_to_voice(
                f"The team's review of {single_line(review.topic)[:80]} is ready. "
                f"Say {WAKE_WORD_DISPLAY}, show me the management review."
            )
        return
    if product_search is not None:
        # A round of the product search is not news by itself: the search
        # itself decides whether to go again, and only its end is announced.
        round_row = await asyncio.to_thread(product_search_round_for, assignment["id"])
        if round_row is not None:
            await announce_finished_product_searches()
            return
    # Gary reads the report on the next management tick, which is now.
    wake_management_loop()
    agent = agent_registry.get(assignment["assigned_to"])
    if assignment["status"] == "completed":
        requested = len(json.loads(assignment["result_json"] or "{}").get("purchase_request_ids", []))
        purchases = (
            f" {agent.name} requested {requested} card purchase{'s' if requested != 1 else ''} "
            "for you to approve on the approvals page."
            if requested else ""
        )
        await announce_to_voice(
            f"{agent.name} has finished: {single_line(assignment['objective'])[:80]}. "
            f"Ask me what {agent.name} found.{purchases}"
        )
    else:
        await announce_to_voice(f"{agent.name}'s assignment did not complete.")


agent_runner = GaryCorpAgentRunner(
    agent_services,
    build_agent_executor(),
    GARY_EMPLOYEE_MODEL,
    on_finished=announce_assignment_finished,
    usage=usage_ledger,
    spend_gate=spend_gate,
)
agent_service = AgentService(gary_ops, agent_registry, agent_runner)
# Scheduled cycles can now see the team and act on the reports that came back.
planning_cycle.team = agent_service
# What the web pages (app.pages) read and change.
app.state.gary_ops = gary_ops
app.state.agent_service = agent_service
app.state.card_vault = card_vault

# Performance reviews: the facts come from SQLite, the judgment from one
# model call. Upward reviews of Gary are written on the employees' model.
performance_reviews = PerformanceReviews(
    gary_ops.db,
    registry=agent_registry,
    reviewer=OpenAIReviewer(
        OPENAI_API_KEY,
        PLANNING_MODEL,
        employee_model=GARY_EMPLOYEE_MODEL,
        principal=PRINCIPAL_NAME,
    ),
    clock=gary_ops.planning.clock,
    usage=usage_ledger,
)

# GaryCorp's search for a product to build: rounds of Susan's research, with
# Python ranking the ideas and deciding when another round would add nothing.
# Absent when the ceiling is 0 rounds, and the tools then say so.
product_search = (
    ProductSearchService(
        gary_ops.db,
        agent_service,
        timezone=ZoneInfo(LOCAL_TIMEZONE),
        clock=gary_ops.planning.clock,
        default_max_rounds=PRODUCT_SEARCH_MAX_ROUNDS,
        # Unattended spending: it stops when Alex pauses the company and when
        # the daily ceiling is reached, and picks up again when they lift.
        paused=lambda: operating.is_paused(),
        spending_allowed=lambda: spend_gate.allowed(),
    )
    if PRODUCT_SEARCH_MAX_ROUNDS > 0
    else None
)

GARY_INTEGRATIONS = {
    "calendar": planning_calendar,
    "notebook": planning_notebook,
    "planning_cycle": planning_cycle,
    "agents": agent_service,
    "reviews": performance_reviews,
    **({"product_search": product_search} if product_search else {}),
    # Absent when GitHub is not configured: the tools then say so.
    **({"engineering": engineering_service} if engineering_service else {}),
}


spoken_notebook = SpokenNotebook()
async def send_announcement(text: str, expects_reply: bool = False) -> bool:
    """Push one line to every connected voice client. True if any took it.

    ``expects_reply`` is part of the VoiceSender protocol and is recorded
    against the message, but it is deliberately not sent to the client: the
    microphone opens on the wake word and nothing else. Gary asks, and Alex
    answers when he chooses to.
    """
    delivered = False
    for websocket in list(voice_connections):
        try:
            await websocket.send_text(
                json.dumps({"type": "bridge.announce", "message": text})
            )
            delivered = True
        except Exception:
            voice_connections.discard(websocket)
    return delivered


spoken_delivery = SpokenDelivery(
    gary_ops.conversation, send_announcement, spoken_notebook
)
# So a repeat Gary reads back in conversation is still recorded and written
# to the Spoken notebook like anything else he says.
GARY_INTEGRATIONS["speech"] = spoken_delivery


async def speak_to_user(text: str, **kwargs) -> dict:
    """Gary saying something Alex did not ask for. Recorded, spoken, written
    down; see SpokenDelivery."""
    return await spoken_delivery.speak(text, **kwargs)


async def raise_approval_with_user(approval: dict) -> None:
    """Every yellow action is put to Alex out loud, instead of waiting to be
    found on a web page. The approval stands whether or not this works."""
    await speak_to_user(
        approval_announcement(approval),
        kind="question",
        source="approval",
        expects_reply=True,
        approval_id=approval["approval_id"],
        action_id=approval["action_id"],
    )


gary_ops.actions.on_approval_requested = raise_approval_with_user


async def raise_unannounced_approvals() -> None:
    """Catch any approval that was never put to Alex.

    The callback above covers approvals made while this process is running.
    This covers the rest: approvals from before this feature existed, and any
    the callback could not deliver. announce() is keyed on approval_id, so an
    approval already raised is not raised twice.
    """
    for approval in await asyncio.to_thread(gary_ops.approvals.list_pending):
        await asyncio.to_thread(
            gary_ops.conversation.announce,
            approval_announcement(
                {
                    "summary": approval["summary"],
                    "action_type": approval["action_type"],
                    "payload": json.loads(approval["payload_json"] or "{}"),
                }
            ),
            kind="question",
            source="approval",
            expects_reply=True,
            approval_id=approval["id"],
            action_id=approval.get("action_id"),
        )


async def announce_to_voice(message: str, source: str = "briefing") -> None:
    """Gary volunteering something. Kept for the callers that had no record
    of what they said; everything now goes through speak_to_user."""
    await speak_to_user(message, source=source)


# Set when something happens that Gary should see promptly, such as a
# specialist's report landing, so the loop does not wait out its interval.
management_wakeup = asyncio.Event()
# Set when something has been queued to say, so the delivery pass does not
# wait out its interval.
spoken_wakeup = asyncio.Event()
management_budget = DailyBudget(MAX_MANAGEMENT_CYCLES_PER_DAY, ZoneInfo(LOCAL_TIMEZONE))


def wake_management_loop() -> None:
    management_wakeup.set()


# The ceiling is announced once a local day, not once a tick.
_spend_stop_announced: dict[str, dt.date | None] = {"day": None}


async def spend_stop(what: str) -> dict | None:
    """The ceiling's answer for one piece of work, announced once a day.

    Returns the gate state when work must stop, or None when it may proceed.
    Telling Alex costs nothing -- local Piper speaks it -- so the one thing
    that still works at the ceiling is Gary explaining why nothing else does.

    """
    state = spend_gate.state()
    today = dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE)).date()

    if state["allowed"]:
        if not state["enforceable"] and _spend_stop_announced["day"] != today:
            # An unpriced model means the ceiling is decoration; say so rather
            # than let a quiet day look like a safe one.
            _spend_stop_announced["day"] = today
            logger.warning("Spend ceiling not enforceable: %s", state["reason"])
            with contextlib.suppress(Exception):
                await speak_to_user(
                    "I cannot tell what the company is spending: the model we are using has "
                    "no price set, so the daily ceiling is not protecting you.",
                    kind="question",
                    source="operations",
                    expects_reply=True,
                )
        return None

    if _spend_stop_announced["day"] != today:
        _spend_stop_announced["day"] = today
        logger.warning("Daily AI spend ceiling reached; stopping %s", what)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                _audit_spend_stop, state, what, format_utc(gary_ops.planning.clock())
            )
            await speak_to_user(state["reason"], source="operations")
    return state


def _audit_spend_stop(state: dict, what: str, now: str) -> None:
    with gary_ops.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            SYSTEM_ACTOR,
            "spend_ceiling_reached",
            f"Daily AI spend ceiling of ${state['ceiling_usd']:.2f} reached; {what} stopped",
            "finance",
            "spend_ceiling",
            {"spent_usd": state["spent_usd"], "ceiling_usd": state["ceiling_usd"]},
            now=now,
        )


async def run_spoken_delivery() -> None:
    """Say what Gary decided to say but could not deliver yet.

    Speaking is separated from deciding to speak precisely so that a voice
    service that was down, or quiet hours, delays a message instead of losing
    it. This pass also retries the Joplin note for anything already spoken,
    so the written record catches up after a Joplin outage.
    """
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    while True:
        try:
            await asyncio.wait_for(
                spoken_wakeup.wait(), timeout=SPOKEN_DELIVERY_SECONDS
            )
        except asyncio.TimeoutError:
            pass
        spoken_wakeup.clear()

        try:
            await spoken_delivery.retry_mirror()
            if not voice_connections or in_quiet_hours(dt.datetime.now(timezone)):
                continue
            await raise_unannounced_approvals()
            await spoken_delivery.deliver_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Delivering what Gary had to say failed")


def record_voice_usage(response: dict) -> None:
    """What a spoken exchange cost. Realtime bills audio input separately
    from text, so the token details are kept apart."""
    usage = response.get("usage")
    if not usage:
        return
    try:
        usage_ledger.record(
            "voice",
            response.get("model") or OPENAI_REALTIME_MODEL,
            usage_from_openai(usage),
            entity_type="realtime_response",
            entity_id=str(response.get("id") or ""),
            detail="voice conversation",
            agent_id="gary",
        )
    except Exception:
        logger.exception("Could not record voice usage")


async def run_management_loop() -> None:
    """Keep the company running between the scheduled cycles.

    Each tick asks a cheap question in SQLite: has anything changed since the
    last cycle? Only then does Gary think. A quiet company costs nothing.
    """
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    interval = MANAGEMENT_TICK_MINUTES * 60
    while True:
        try:
            await asyncio.wait_for(management_wakeup.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        management_wakeup.clear()

        try:
            now_local = dt.datetime.now(timezone)
            if now_local.weekday() not in MANAGEMENT_WEEKDAYS:
                continue
            if await asyncio.to_thread(operating.is_paused):
                continue
            if await spend_stop("the management loop"):
                continue
            since_minutes = await planning_cycle.minutes_since_last_cycle()
            if since_minutes is not None and since_minutes < MIN_GAP_MINUTES:
                continue
            if not management_budget.remaining(now_local):
                continue

            now = format_utc(gary_ops.planning.clock())
            since = await asyncio.to_thread(gary_ops.planning.last_cycle_finished_at)
            reports = await asyncio.to_thread(completed_since, planning_cycle.team, since)
            triggers = await asyncio.to_thread(
                find_triggers, gary_ops, now=now, since=since, completed_reports=reports
            )
            if not triggers:
                logger.debug("Management tick: %s", triggers.describe())
                continue
            if not management_budget.take(now_local):
                logger.warning(
                    "Management loop reached its daily ceiling of %s cycles",
                    MAX_MANAGEMENT_CYCLES_PER_DAY,
                )
                continue

            logger.info("Management cycle: %s", triggers.describe())
            result = await planning_cycle.run("management")
            briefing = result["briefing"]
            if briefing and not in_quiet_hours(dt.datetime.now(timezone)):
                await announce_to_voice(briefing)
        except asyncio.CancelledError:
            raise
        except PlanningCycleError as exc:
            logger.warning("Management cycle failed: %s", exc)
        except Exception as exc:
            log_loop_failure(logger, exc, "Management loop check")


production = (
    ProductionService(
        gary_ops.db,
        gary_ops.actions,
        PRODUCTION_SCHEDULE,
        ZoneInfo(LOCAL_TIMEZONE),
        gary_ops.planning.clock,
        engineering=lambda: globals().get("engineering_service"),
    )
    if PRODUCTION_SCHEDULE
    else None
)


def production_announcement(result: dict) -> str | None:
    """What Gary says when the schedule moved on: a new batch planned, or a
    shoot or publish slot he could not put on the calendar."""
    parts = []
    if result["created"]:
        names = " and ".join(episode_label(PRODUCTION_SCHEDULE, n) for n in result["created"])
        parts.append(
            f"I have planned {names}, with their scripts, shoot, edits and publish "
            "dates on the task list."
        )
    if result["failed"]:
        titles = ", ".join(single_line(item["task"])[:80] for item in result["failed"][:2])
        parts.append(
            f"I could not put {titles} on your calendar today, and I will try again tomorrow."
        )
    return " ".join(parts) or None


# A calendar write that failed is retried on the next tick, up to this many
# attempts a day; a failure is only announced once the day's tries are used.
PRODUCTION_ATTEMPTS_PER_DAY = 4


async def run_production_schedule() -> None:
    """Plan the video schedule at startup and then once a local day.

    Idempotent and makes no model call, so running it costs nothing; once a
    day is enough because a batch is planned two weeks ahead. A failed
    calendar write (often a brief network error) is retried every tick, a
    few times a day, rather than every few minutes forever.
    """
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    done_day, attempts = None, {}
    while True:
        today = dt.datetime.now(timezone).date()
        if await asyncio.to_thread(operating.is_paused):
            await asyncio.sleep(15 * 60)
            continue
        if today != done_day:
            tries = attempts[today] = attempts.get(today, 0) + 1
            attempts = {today: tries}
            try:
                result = await production.run()
                if result["created"] or result["scheduled"] or result["failed"]:
                    logger.warning("Production schedule: %s", result)
                giving_up = tries >= PRODUCTION_ATTEMPTS_PER_DAY
                if not result["failed"] or giving_up:
                    done_day = today
                announcement = production_announcement(
                    {**result, "failed": result["failed"] if giving_up else []}
                )
                if announcement:
                    await speak_to_user(announcement, source="operations")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_loop_failure(logger, exc, "Planning the video schedule")
        # This week's stages as GitHub issues, and issues closed for stages
        # finished in Gary. Every tick, so the board follows within minutes.
        try:
            tickets = await production.sync_tickets()
            if any(tickets.values()):
                logger.warning("Production tickets: %s", tickets)
            if tickets["opened"]:
                count = len(tickets["opened"])
                await speak_to_user(
                    f"I have put {count} new issue{'s' if count != 1 else ''} on your "
                    "GitHub board: "
                    + ", ".join(single_line(t)[:60] for t in tickets["opened"][:4]) + ".",
                    source="operations",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(logger, exc, "Syncing the video schedule's GitHub issues")
        await asyncio.sleep(15 * 60)


operating = OperatingState(gary_ops.db, gary_ops.planning.clock)
daily_report = DailyReport(
    gary_ops.db,
    ZoneInfo(LOCAL_TIMEZONE),
    usage_ledger,
    operating,
    gary_ops.planning.clock,
)


async def email_alex(subject: str, body: str) -> dict:
    """Gary writing to Alex, through the ordinary action pipeline."""
    return await gary_ops.actions.propose(
        ProposeActionRequest(action_type="email_principal", payload={"subject": subject, "body": body}),
        actor=SYSTEM_ACTOR,
    )


async def run_email_commands() -> None:
    """Read Gary's own inbox for Alex's pause and resume emails.

    The only thing email may do is stop the company or start it again; every
    other decision stays on the approvals page. What makes it safe is in
    gary/services/operating.py: Alex's exact address, Gmail's own
    authentication result, the word alone on the subject or first line, and a
    cursor so a message is acted on once.
    """
    interval = EMAIL_COMMAND_POLL_MINUTES * 60
    # Only mail sent after this feature was switched on is ever a command.
    if await asyncio.to_thread(
        operating.start_from_now, int(gary_ops.planning.clock().timestamp() * 1000)
    ):
        logger.info("Email commands: reading Alex's mail from now on")
    while True:
        await asyncio.sleep(interval)
        try:
            principal = await store.active_email(USER_MAILBOX)
            if not principal:
                continue
            messages = await fetch_command_emails(principal, operating.cursor_ms())
            for message in messages:
                outcome = await asyncio.to_thread(operating.apply_command, message, principal)
                if not outcome["applied"]:
                    if outcome["refused"] not in (None, "not a command", "already read"):
                        logger.warning("Email command refused: %s", outcome["refused"])
                    continue

                paused = outcome["applied"] == "pause"
                logger.warning("Alex emailed %s", outcome["applied"])
                said = (
                    "I have paused everything unattended. Nothing will run until you "
                    "tell me to resume. You can still talk to me."
                    if paused
                    else "I have resumed. The company is running again."
                )
                await speak_to_user(said, source="operations")
                await email_alex(
                    "Paused" if paused else "Resumed",
                    said + "\n\nGary",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(logger, exc, "Reading Alex's command email")


accountability = Accountability(
    gary_ops.db,
    gary_ops.actions,
    ZoneInfo(LOCAL_TIMEZONE),
    WORK_WEEK,
    gary_ops.planning.clock,
    principal=PRINCIPAL_NAME,
)
# The assignment is given at the first tick after its time, up until the
# evening check-in: the machine is not always on at 8 am, and a day that
# starts at noon still deserves its assignment. After that the check-in
# covers the day instead.
ASSIGNMENT_LATEST = EVENING_CHECKIN_TIME or dt.time(16, 0)
CHECKIN_LATEST = dt.time(23, 59)


async def run_accountability() -> None:
    """Gary managing Alex's day, with no model call: the assignment each
    morning (spoken and emailed), the check-in each evening. Each happens
    at most once a day; the audit log is what remembers that."""
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    while True:
        try:
            now = dt.datetime.now(timezone).time()
            if await asyncio.to_thread(operating.is_paused):
                await asyncio.sleep(5 * 60)
                continue
            if DAILY_REPORT_TIME and DAILY_REPORT_TIME <= now and not await asyncio.to_thread(
                daily_report.sent_today
            ):
                await run_review_round()
                await send_daily_report()
            if MORNING_ASSIGNMENT_TIME and MORNING_ASSIGNMENT_TIME <= now < ASSIGNMENT_LATEST:
                given = await accountability.morning()
                if given:
                    if given["email_status"] != "succeeded":
                        logger.warning("Morning assignment email: %s", given["email_error"])
                    await speak_to_user(given["spoken"], source="briefing")
            if EVENING_CHECKIN_TIME and EVENING_CHECKIN_TIME <= now < CHECKIN_LATEST:
                checkin = await asyncio.to_thread(accountability.evening)
                if checkin:
                    await speak_to_user(
                        checkin["spoken"],
                        kind="question" if checkin["question"] else "notice",
                        source="operations",
                        expects_reply=checkin["question"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(logger, exc, "The morning assignment or evening check-in")
        await asyncio.sleep(5 * 60)


async def run_review_round() -> None:
    """Gary reviews his people and Alex; his people review him.

    Run daily, but ``due`` is what decides anything happens: a subject with
    too thin a record, or one reviewed inside REVIEW_INTERVAL_DAYS, is not
    reviewed again. Each review is one model call, so the round is capped and
    stops at the spend ceiling like any other thinking.
    """
    if not REVIEW_INTERVAL_DAYS:
        return
    if await spend_stop("performance reviews"):
        return

    result = await performance_reviews.run_all_due(
        days=REVIEW_PERIOD_DAYS,
        interval_days=REVIEW_INTERVAL_DAYS,
        limit=MAX_REVIEWS_PER_ROUND,
    )
    if result["failed"]:
        logger.warning("Performance reviews that failed: %s", result["failed"])
    if not result["written"]:
        return

    logger.warning("Performance reviews written: %s", result["written"])
    owed = await asyncio.to_thread(performance_reviews.unacknowledged_of_manager)
    said = (
        f"{len(result['written'])} performance review"
        f"{'s are' if len(result['written']) != 1 else ' is'} written."
    )
    if owed:
        said += (
            f" {len(owed)} of them {'are' if len(owed) != 1 else 'is'} about me, from the "
            "people who work for me, and I have not answered "
            f"{'them' if len(owed) != 1 else 'it'} yet."
        )
    await speak_to_user(said, source="briefing")


async def send_daily_report() -> None:
    """What the company did today, emailed and said. No model call, so it
    still goes out on a day the spend ceiling stopped everything else."""
    data = await asyncio.to_thread(daily_report.collect)
    email = daily_report.render_email(data)
    outcome = await email_alex(email["subject"], email["body"])
    await asyncio.to_thread(daily_report.record_sent, outcome.get("status", "unknown"))
    if outcome.get("status") != "succeeded":
        logger.warning("Daily report email: %s", outcome.get("error"))
    if not in_quiet_hours(dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))):
        await speak_to_user(daily_report.spoken_summary(data), source="briefing")

    # Once a colleague Gary argued for exists and has done some work, his
    # hire issue gets their record. Once per hire; a GitHub failure retries
    # with tomorrow's report.
    try:
        commented = await follow_up_on_hires(
            gary_ops, engineering_service, agent_registry, gary_ops.planning.clock
        )
        if commented:
            logger.warning("Hire follow-up: %s", commented)
    except Exception as exc:
        log_loop_failure(logger, exc, "Commenting on a hire issue")


weekly_review = WeeklyReview(
    gary_ops.db,
    ZoneInfo(LOCAL_TIMEZONE),
    usage_ledger,
    gary_ops.planning.clock,
    accountability=accountability,
)


async def write_weekly_review() -> None:
    """The week, assembled from SQLite and written down.

    Deliberately makes no model call, so it still runs on the day the spend
    ceiling stopped everything else -- which is exactly the week worth
    reporting.
    """
    now_local = dt.datetime.now(ZoneInfo(LOCAL_TIMEZONE))
    data = await asyncio.to_thread(weekly_review.collect)
    markdown = weekly_review.render_markdown(data)
    try:
        await planning_notebook.write_weekly_review(now_local.date(), markdown)
    except Exception:
        logger.exception("Could not write the weekly review to Joplin")
    await asyncio.to_thread(_audit_weekly_review, now_local)
    if not in_quiet_hours(now_local):
        await speak_to_user(weekly_review.spoken_summary(data), source="briefing")


def _audit_weekly_review(now_local: dt.datetime) -> None:
    with gary_ops.db.transaction() as conn:
        Repositories.bind(conn).audit.write(
            SYSTEM_ACTOR,
            "weekly_review_written",
            f"Weekly review for the week to {now_local.date().isoformat()}",
            "planning",
            now_local.date().isoformat(),
            {},
            now=format_utc(gary_ops.planning.clock()),
        )


def weekly_review_written_today(now_local: dt.datetime) -> bool:
    """One review a week, even if the evening cycle runs twice."""
    with gary_ops.db.read() as conn:
        return Repositories.bind(conn).audit.has_event(
            "weekly_review_written", now_local.date().isoformat()
        )


async def run_planning_scheduler() -> None:
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    while True:
        try:
            now_local = dt.datetime.now(timezone)
            started = await asyncio.to_thread(
                gary_ops.planning.run_types_started_on, now_local.date(), timezone
            )
            if await asyncio.to_thread(operating.is_paused):
                await asyncio.sleep(60)
                continue
            if await spend_stop("scheduled planning"):
                await asyncio.sleep(60)
                continue
            for planning_type in due_planning_types(
                now_local, PLANNING_SCHEDULE, PLANNING_WEEKDAYS, started
            ):
                try:
                    result = await planning_cycle.run(planning_type)
                except Exception:
                    # Recorded as a failed planning run; not retried today.
                    continue
                briefing = result["briefing"]
                if briefing and not in_quiet_hours(dt.datetime.now(timezone)):
                    await announce_to_voice(briefing)
                # The week's review follows the last evening cycle of the
                # configured day, so it sees that cycle's work.
                if (
                    planning_type == "evening"
                    and now_local.weekday() == WEEKLY_REVIEW_DAY
                    and not await asyncio.to_thread(weekly_review_written_today, now_local)
                ):
                    await write_weekly_review()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(logger, exc, "Planning scheduler check")
        await asyncio.sleep(60)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/costs")
async def costs(days: int = 30):
    """What GaryCorp's thinking has cost: our per-call estimate, and the
    provider's billed figure when an admin key is configured. Read-only."""
    days = min(365, max(1, days))
    summary = await asyncio.to_thread(usage_ledger.summary, days)
    # Which model does which job, and whether each can be costed. The ledger
    # only knows models that have already been called; this is the configured
    # picture, which is what tells you a price is missing before it is spent.
    summary["models_in_use"] = await asyncio.to_thread(
        usage_ledger.models_in_use, MODEL_ROLES
    )
    summary["spend_ceiling"] = spend_gate.state()
    if provider_costs.configured:
        try:
            summary["billed"] = await provider_costs.daily(days)
        except ProviderCostsError as exc:
            summary["billed"] = {"available": False, "detail": str(exc)}
    else:
        summary["billed"] = {
            "available": False,
            "detail": "Set OPENAI_ADMIN_KEY (api.usage.read scope) to read billed costs.",
        }
    return summary


@app.get("/management/status")
async def management_status():
    """What the continuous loop is doing: its cadence, what it has spent
    today, and whether anything is waiting for Gary right now. Read-only."""
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    now_local = dt.datetime.now(timezone)
    now = format_utc(gary_ops.planning.clock())
    since = await asyncio.to_thread(gary_ops.planning.last_cycle_finished_at)
    reports = await asyncio.to_thread(completed_since, planning_cycle.team, since)
    triggers = await asyncio.to_thread(
        find_triggers, gary_ops, now=now, since=since, completed_reports=reports
    )
    return {
        "enabled": MANAGEMENT_TICK_MINUTES > 0,
        "tick_minutes": MANAGEMENT_TICK_MINUTES,
        "runs_today": {
            "used": management_budget.used_today,
            "remaining": management_budget.remaining(now_local),
            "ceiling": MAX_MANAGEMENT_CYCLES_PER_DAY,
        },
        "runs_on_weekdays": sorted(MANAGEMENT_WEEKDAYS),
        "running_today": now_local.weekday() in MANAGEMENT_WEEKDAYS,
        "last_cycle_finished": to_local(since, timezone) if since else None,
        "new_reports": reports,
        "team_connected": planning_cycle.team is not None,
        "would_run_now": bool(triggers),
        "triggers": triggers.reasons,
        # The two things that decide whether the company is actually working.
        "spend": spend_gate.state(),
        "company_health": triggers.health,
    }


@app.get("/production/status")
async def production_status():
    """The weekly video schedule: every planned episode and where its work
    stands. Read-only."""
    if production is None:
        return {"enabled": False, "detail": "Set PRODUCTION_FIRST_SHOOT to turn it on."}
    timezone = ZoneInfo(LOCAL_TIMEZONE)
    episodes = await asyncio.to_thread(production.status)
    for episode in episodes:
        for key in ("shoot_at", "publish_at"):
            episode[key] = to_local(episode[key], timezone)
        for task in episode["tasks"]:
            task["deadline"] = to_local(task["deadline"], timezone)
            task["scheduled_start"] = to_local(task["scheduled_start"], timezone)
    return {"enabled": True, "series": PRODUCTION_SCHEDULE.series, "episodes": episodes}


@app.get("/engineering/status")
async def github_engineering_status():
    """Health of the engineering integration, including the privacy audit.

    Read-only: nothing here changes repository or Project visibility.
    """
    if engineering_service is None:
        return {
            "state": "unavailable",
            "detail": "GitHub engineering integration is not configured on this deployment.",
        }
    try:
        return await engineering_service.status()
    except GitHubError as exc:
        return {"state": getattr(exc, "status", "unavailable"), "detail": str(exc)}


def authorized_voice_bridge(websocket: WebSocket) -> bool:
    auth = websocket.headers.get("authorization", "")
    prefix = "Bearer "

    if not auth.startswith(prefix):
        return False

    token = auth[len(prefix):]
    return secrets.compare_digest(token, VOICE_BRIDGE_TOKEN)


async def run_tool_call(name: str, arguments_json: str, session: dict) -> dict:
    """Run one tool call and return its result.

    Shared by both voice paths: the Realtime one writes the result back onto
    its websocket, the transcribe one appends it to the conversation. Keeping
    one implementation is what stops the two paths drifting apart on the
    session rules below.

    session tracks what this voice conversation has seen (see
    app.tool_dispatch), so the model cannot act on guessed IDs.
    """
    try:
        arguments = json.loads(arguments_json or "{}")

        if name in GARY_TOOL_NAMES:
            result = await call_gary_tool(
                name, arguments, GaryToolContext(gary_ops, session, GARY_INTEGRATIONS)
            )
        else:
            result = await run_integration_tool(name, arguments, session)

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

    return result


def describe_models() -> str:
    """What Gary is running on, so he can answer it without delegating."""
    rows = usage_ledger.models_in_use(MODEL_ROLES)
    parts = []
    for row in rows:
        jobs = ", ".join(row["roles"])
        if row["priced"]:
            # Spoken aloud, so the unit has to be the real one: a per-minute
            # rate read as "per million tokens" would be nonsense.
            money = " and ".join(
                rate_phrase(field, value, spoken=True)
                for field, value in row["rates"].items()
            )
            parts.append(f"{row['model']} for {jobs}, costing {money}")
        else:
            parts.append(f"{row['model']} for {jobs}, with no price set")
    return "; ".join(parts)


voice_turn = VoiceTurn(
    OPENAI_API_KEY,
    VOICE_TEXT_MODEL,
    VOICE_TRANSCRIBE_MODEL,
    # A callable, so each turn gets the current date and instructions.
    instructions=lambda: build_instructions(),
    tools=VOICE_TOOLS,
    dispatch=run_tool_call,
    usage=usage_ledger,
)


def build_instructions() -> str:
    return build_prompt(
        describe_models(), usage_ledger.spent_today()["cost_usd"]
    )


async def outstanding_questions(session: dict) -> str | None:
    """What a Realtime session should open knowing is already between them.

    Two kinds of message: questions Gary asked out loud and is still
    waiting on, so a bare "yes" is not a mystery; and messages he
    deliberately held back (urgency next_time) for exactly this moment.
    The held ones count as said once they are handed over, because he is
    being told to raise them now.
    """
    try:
        messages = await asyncio.to_thread(gary_ops.conversation.to_mention)
    except Exception:
        logger.exception("Could not read what Gary has outstanding with the user")
        return None
    if not messages:
        return None

    held = [m for m in messages if m["status"] == "pending"]
    asked = [m for m in messages if m["status"] == "spoken"]
    for message in held:
        await spoken_delivery.mention_in_conversation(message)

    # Only these can be answered or repeated in this conversation.
    session.setdefault("spoken_ids", set()).update(m["id"] for m in messages)

    parts = []
    if asked:
        said = " ".join(
            f"({m['id']}) at {spoken_clock(m['spoken_at'])}: {single_line(m['text'])}"
            for m in asked
        )
        parts.append(
            f"Earlier, without being asked, I said this to {PRINCIPAL_NAME} and am "
            f"still waiting on an answer: {said}. If this turn answers one of them, "
            "record it with question_answer using that message_id."
        )
    if held:
        waiting = " ".join(
            f"({m['id']}) {single_line(m['text'])}" for m in held
        )
        parts.append(
            "I also held these back rather than interrupt him, to raise when we "
            f"next spoke, which is now: {waiting}. Work them into this "
            "conversation once the immediate request is dealt with, and record "
            "any answer with question_answer."
        )
    return " ".join(parts)


@app.websocket("/internal/voice")
async def internal_voice(websocket: WebSocket):
    if not authorized_voice_bridge(websocket):
        await websocket.close(code=1008)
        return

    try:
        await credentials_for_active_user()
    except RuntimeError as exc:
        await websocket.accept()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "bridge.error",
                    "message": str(exc),
                }
            )
        )
        await websocket.close(code=1011)
        return

    await websocket.accept()
    voice_connections.add(websocket)
    # Anything queued while nothing was listening is spoken now.
    spoken_wakeup.set()

    # Set by bridge.utterance just before its audio frame arrives.
    utterance_seconds: list[float] = []

    async def handle_utterance(ws, session: dict, audio: bytes, seconds: float) -> None:
        """One spoken exchange, transcribed and answered."""
        if not VOICE_TRANSCRIBE_MODEL:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "bridge.announce",
                        "message": (
                            "I cannot hear you: no transcription model is configured. "
                            "Set VOICE_TRANSCRIBE_MODEL and restart."
                        ),
                    }
                )
            )
            return
        stopped = await spend_stop("voice")
        if stopped:
            raise SpendCeilingReached(stopped["reason"])
        try:
            said = await voice_turn.transcribe(audio, seconds)
            if not said:
                await ws.send_text(
                    json.dumps({"type": "bridge.notice", "message": "Nothing was heard"})
                )
                return
            await ws.send_text(json.dumps({"type": "bridge.transcript", "text": said}))
            reply = await voice_turn.respond(session, said)
        except VoiceTurnError as exc:
            # Never invent a reply: say plainly that the turn failed.
            logger.warning("Voice turn failed: %s", exc)
            await ws.send_text(
                json.dumps(
                    {"type": "bridge.announce", "message": f"Sorry, that did not work. {exc}"}
                )
            )
            return
        if reply:
            await ws.send_text(json.dumps({"type": "bridge.reply", "text": reply}))

    # Whether the company is stopped is announced once a day by spend_stop,
    # as a recorded message. Connecting sets spoken_wakeup above, so a queued
    # one is spoken now; saying it again here only repeated it.
    await spend_stop("voice")

    session: dict = {
        "event_ids": set(),
        "emails": {},
        "replied": set(),
        "new_emails": set(),
        "note_ids": set(),
        "approval_ids": set(),
    }

    realtime = RealtimeSession(
        websocket,
        session,
        instructions=build_instructions,
        tools=VOICE_TOOLS,
        dispatch=run_tool_call,
        spend_stop=spend_stop,
        on_usage=record_voice_usage,
        opening_context=outstanding_questions,
    )

    email_checker = (
        asyncio.create_task(announce_new_emails(websocket))
        if EMAIL_CHECK_INTERVAL_MINUTES > 0
        else None
    )
    operations_checker = (
        asyncio.create_task(announce_operations(websocket))
        if OPS_CHECK_INTERVAL_MINUTES > 0
        else None
    )

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                # Audio only arrives after local wake-word activation. In
                # transcribe mode it is one complete utterance, already
                # segmented by the voice service; in realtime mode it is a
                # live stream.
                try:
                    if VOICE_MODE == "transcribe":
                        # Duration comes from the bridge.utterance header;
                        # without it the audio is still answered, just not
                        # costed, which is better than dropping what Alex said.
                        seconds = utterance_seconds.pop() if utterance_seconds else 0.0
                        await handle_utterance(websocket, session, message["bytes"], seconds)
                    else:
                        await realtime.send_audio(message["bytes"])
                except SpendCeilingReached as exc:
                    # Audio keeps arriving while Alex talks; say it once and
                    # drop the rest rather than repeating on every chunk.
                    if not session.get("spend_notified"):
                        session["spend_notified"] = True
                        await websocket.send_text(
                            json.dumps({"type": "bridge.announce", "message": str(exc)})
                        )

            elif message.get("text") is not None:
                control = json.loads(message["text"])

                if control.get("type") == "bridge.utterance":
                    # The header for the audio frame that follows, so the
                    # duration is known before it is priced.
                    utterance_seconds.append(float(control.get("seconds") or 0.0))

                elif control.get("type") == "bridge.reset":
                    # Back to sleep. The conversation is forgotten here, which
                    # is the same lifetime the Realtime session had.
                    session["messages"] = []
                    await realtime.close()

    except WebSocketDisconnect:
        pass

    except Exception as exc:
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "bridge.error",
                        "message": str(exc),
                    }
                )
            )
        except Exception:
            pass

    finally:
        voice_connections.discard(websocket)

        for checker in (email_checker, operations_checker):
            if checker is None:
                continue
            checker.cancel()

            try:
                await checker
            except (asyncio.CancelledError, Exception):
                pass

        await realtime.close()

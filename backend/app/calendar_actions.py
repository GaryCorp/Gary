"""The actions whose side effect is Alex's Google account: putting a task on
the calendar, moving it, and sending email Gary initiates.

Each is an ActionHandler for the policy pipeline in ActionService, so it is
checked, run with no transaction open, recorded and audited like any other.
"""

import asyncio
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from app.config import (
    GMAIL_SEND_SCOPE,
    LOCAL_TIMEZONE,
    USER_MAILBOX,
    WAKE_WORD_DISPLAY,
    WORK_WEEK,
)
from app.gmail import gmail_service, require_gmail_scope, send_plain_email
from app.google_auth import (
    credentials_for_active_user,
    credentials_for_mailbox,
    default_send_mailbox,
    store,
)
from app.google_calendar import calendar_service, create_calendar_event
from app.local_time import local_iso, spoken_time
from gary.db.repositories import Repositories
from gary.models.action import (
    EmailPrincipalPayload,
    MoveCalendarEventPayload,
    ScheduleTaskPayload,
    SendExternalEmailPayload,
)
from gary.policy import CRITICAL_TASK_PRIORITY, YELLOW
from gary.services.action_service import ActionHandler
from gary.services.calendar_blocks import working_time_problem
from gary.services.common import require_task


async def move_calendar_event_time(event_id: str, start: str, end: str) -> dict:
    _, credentials = await credentials_for_active_user()
    body = {
        "start": {"dateTime": local_iso(start), "timeZone": LOCAL_TIMEZONE},
        "end": {"dateTime": local_iso(end), "timeZone": LOCAL_TIMEZONE},
    }
    service = calendar_service(credentials)
    try:
        updated = await asyncio.to_thread(
            lambda: service.events()
            .patch(
                calendarId="primary",
                eventId=event_id,
                body=body,
                sendUpdates="none",
            )
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            raise ValueError("That calendar event no longer exists") from exc
        raise
    return {"event_id": updated.get("id"), "html_link": updated.get("htmlLink")}


def open_task_context(repos: Repositories, task_id: str) -> dict:
    task = require_task(repos, task_id)
    if task["status"] in ("completed", "cancelled"):
        raise ValueError(f"Task {task['title']} is {task['status']}")
    return {"task": task}


def check_working_time(start: str, end: str, override: bool) -> None:
    if override:
        return
    problem = working_time_problem(start, end, WORK_WEEK, ZoneInfo(LOCAL_TIMEZONE))
    if problem:
        raise ValueError(
            f"Not scheduled: {problem}. Pick another time with "
            "planning_find_work_blocks, or, only if the user explicitly asked for "
            "this time, call again with override_working_hours set to true."
        )


def check_schedule_task(repos: Repositories, payload: ScheduleTaskPayload) -> dict:
    check_working_time(payload.start, payload.end, payload.override_working_hours)
    context = open_task_context(repos, payload.task_id)
    if context["task"]["calendar_event_id"]:
        raise ValueError(
            "That task is already on the calendar; use move_calendar_event instead"
        )
    return context


async def execute_schedule_task(payload: ScheduleTaskPayload, context: dict) -> dict:
    task = context["task"]
    created = await create_calendar_event(
        title=task["title"],
        start_time=local_iso(payload.start),
        end_time=local_iso(payload.end),
        description=f"Scheduled by {WAKE_WORD_DISPLAY} for the task: {task['title']}",
    )
    return {"event_id": created["event_id"], "html_link": created["html_link"]}


def record_schedule_task(repos, payload: ScheduleTaskPayload, result: dict, now: str):
    task = repos.tasks.get(payload.task_id)
    repos.tasks.update(
        payload.task_id,
        now=now,
        status="scheduled" if task["status"] == "todo" else task["status"],
        scheduled_start=payload.start,
        scheduled_end=payload.end,
        calendar_event_id=result["event_id"],
    )
    return {}


def check_move_event(repos: Repositories, payload: MoveCalendarEventPayload) -> dict:
    check_working_time(payload.new_start, payload.new_end, payload.override_working_hours)
    context = open_task_context(repos, payload.task_id)
    if not context["task"]["calendar_event_id"]:
        raise ValueError("That task is not on the calendar; use schedule_task instead")
    return context


def classify_move_event(repos: Repositories, payload: MoveCalendarEventPayload):
    task = repos.tasks.get(payload.task_id)
    critical = (
        task["priority"] >= CRITICAL_TASK_PRIORITY
        or task["id"] in repos.commitments.open_task_ids()
    )
    return YELLOW if critical else None


async def execute_move_event(payload: MoveCalendarEventPayload, context: dict) -> dict:
    return await move_calendar_event_time(
        context["task"]["calendar_event_id"], payload.new_start, payload.new_end
    )


def record_move_event(repos, payload: MoveCalendarEventPayload, result: dict, now: str):
    repos.tasks.update(
        payload.task_id,
        now=now,
        scheduled_start=payload.new_start,
        scheduled_end=payload.new_end,
    )
    return {}


async def execute_send_email(payload: SendExternalEmailPayload, context: dict) -> dict:
    # Email Gary initiates comes from Gary's own address when there is one.
    mailbox = default_send_mailbox()
    credentials = await credentials_for_mailbox(mailbox)
    require_gmail_scope(credentials, GMAIL_SEND_SCOPE)
    sent = await send_plain_email(
        gmail_service(credentials), payload.to, payload.subject, payload.body
    )
    return {"sent_message_id": sent.get("id"), "to": payload.to, "from_mailbox": mailbox}


async def execute_email_principal(payload: EmailPrincipalPayload, context: dict) -> dict:
    """From Gary's own mailbox when there is one, always to Alex's own
    signed-in address."""
    to = await store.active_email(USER_MAILBOX)
    if not to:
        raise ValueError("No Google account is signed in, so there is nobody to email")
    mailbox = default_send_mailbox()
    credentials = await credentials_for_mailbox(mailbox)
    require_gmail_scope(credentials, GMAIL_SEND_SCOPE)
    sent = await send_plain_email(gmail_service(credentials), to, payload.subject, payload.body)
    return {"sent_message_id": sent.get("id"), "from_mailbox": mailbox}


def external_action_handlers() -> dict[str, ActionHandler]:
    return {
        "schedule_task": ActionHandler(
            payload_model=ScheduleTaskPayload,
            summarize=lambda p, c: (
                f"Schedule {c['task']['title']} on {spoken_time(p.start)}"
                if c.get("task")
                else "Schedule a task"
            ),
            check=check_schedule_task,
            execute=execute_schedule_task,
            record=record_schedule_task,
            audit_event="calendar_changed",
        ),
        "move_calendar_event": ActionHandler(
            payload_model=MoveCalendarEventPayload,
            summarize=lambda p, c: (
                f"Move {c['task']['title']} to {spoken_time(p.new_start)}"
                if c.get("task")
                else "Move a calendar event"
            ),
            check=check_move_event,
            classify=classify_move_event,
            execute=execute_move_event,
            record=record_move_event,
            audit_event="calendar_changed",
        ),
        "send_external_email": ActionHandler(
            payload_model=SendExternalEmailPayload,
            summarize=lambda p, c: f'Email {p.to} with the subject "{p.subject}"',
            execute=execute_send_email,
            audit_event="email_sent",
        ),
        "email_principal": ActionHandler(
            payload_model=EmailPrincipalPayload,
            summarize=lambda p, c: f'Email you "{p.subject}"',
            execute=execute_email_principal,
            audit_event="email_sent",
        ),
    }

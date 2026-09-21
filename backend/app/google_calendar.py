"""Google Calendar calls made on the user's account.
"""

import asyncio
import datetime as dt

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import LOCAL_TIMEZONE
from app.google_auth import credentials_for_active_user


def calendar_service(credentials: Credentials):
    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


async def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
) -> dict:
    _, credentials = await credentials_for_active_user()

    try:
        start_dt = dt.datetime.fromisoformat(start_time)
        end_dt = dt.datetime.fromisoformat(end_time)
    except ValueError as exc:
        raise ValueError(
            "start_time and end_time must be ISO 8601 date-times"
        ) from exc

    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("Calendar date-times must include a timezone offset")

    if end_dt <= start_dt:
        raise ValueError("end_time must be later than start_time")

    title = title.strip()
    if not title:
        raise ValueError("title cannot be empty")

    event_body = {
        "summary": title[:200],
        "description": description.strip()[:4000],
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": LOCAL_TIMEZONE,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": LOCAL_TIMEZONE,
        },
    }

    service = calendar_service(credentials)
    created = await asyncio.to_thread(
        lambda: service.events()
        .insert(calendarId="primary", body=event_body)
        .execute()
    )

    return {
        "success": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "title": event_body["summary"],
        "start": event_body["start"]["dateTime"],
        "end": event_body["end"]["dateTime"],
    }


def parse_aware_datetime(value: str, field_name: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO 8601 date-time"
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone offset")

    return parsed


async def list_calendar_events(
    start_time: str,
    end_time: str,
    max_results: int = 10,
    query: str = "",
) -> dict:
    _, credentials = await credentials_for_active_user()

    start_dt = parse_aware_datetime(start_time, "start_time")
    end_dt = parse_aware_datetime(end_time, "end_time")

    if end_dt <= start_dt:
        raise ValueError("end_time must be later than start_time")

    if end_dt - start_dt > dt.timedelta(days=366):
        raise ValueError("Calendar queries cannot span more than 366 days")

    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")

    max_results = max(1, min(max_results, 25))
    query = query.strip()[:200]

    request_arguments = {
        "calendarId": "primary",
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
        "timeZone": LOCAL_TIMEZONE,
    }

    if query:
        request_arguments["q"] = query

    service = calendar_service(credentials)
    response = await asyncio.to_thread(
        lambda: service.events().list(**request_arguments).execute()
    )

    events = []
    for event in response.get("items", []):
        start = event.get("start", {})
        end = event.get("end", {})
        events.append(
            {
                "event_id": event.get("id"),
                "title": event.get("summary") or "Untitled event",
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "all_day": "date" in start,
                "location": event.get("location", ""),
                "status": event.get("status", "confirmed"),
            }
        )

    return {
        "success": True,
        "timezone": LOCAL_TIMEZONE,
        "range_start": start_dt.isoformat(),
        "range_end": end_dt.isoformat(),
        "count": len(events),
        "events": events,
    }


def parse_date(value: str, field_name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be an ISO 8601 date (YYYY-MM-DD)"
        ) from exc


async def create_all_day_event(
    title: str,
    start_date: str,
    end_date: str = "",
    description: str = "",
) -> dict:
    _, credentials = await credentials_for_active_user()

    first_day = parse_date(start_date, "start_date")
    last_day = parse_date(end_date, "end_date") if end_date else first_day

    if last_day < first_day:
        raise ValueError("end_date cannot be before start_date")

    if (last_day - first_day).days >= 366:
        raise ValueError("All-day events cannot span more than 366 days")

    title = title.strip()
    if not title:
        raise ValueError("title cannot be empty")

    event_body = {
        "summary": title[:200],
        "description": description.strip()[:4000],
        "start": {"date": first_day.isoformat()},
        # Google's all-day end date is exclusive.
        "end": {"date": (last_day + dt.timedelta(days=1)).isoformat()},
    }

    service = calendar_service(credentials)
    created = await asyncio.to_thread(
        lambda: service.events()
        .insert(calendarId="primary", body=event_body)
        .execute()
    )

    return {
        "success": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "title": event_body["summary"],
        "all_day": True,
        "first_day": first_day.isoformat(),
        "last_day": last_day.isoformat(),
        "days": (last_day - first_day).days + 1,
    }


async def delete_calendar_event(
    event_id: str,
    confirmed: bool,
    known_event_ids: set[str],
) -> dict:
    _, credentials = await credentials_for_active_user()

    if confirmed is not True:
        raise ValueError(
            "Deletion not confirmed. Ask the user to confirm this specific "
            "event first, then call again with confirmed set to true."
        )

    event_id = (event_id or "").strip()
    if event_id not in known_event_ids:
        raise ValueError(
            "Unknown event_id. Call list_calendar_events first and use an "
            "event_id it returned in this conversation."
        )

    service = calendar_service(credentials)

    try:
        event = await asyncio.to_thread(
            lambda: service.events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )

        if event.get("status") != "cancelled":
            await asyncio.to_thread(
                lambda: service.events()
                .delete(calendarId="primary", eventId=event_id, sendUpdates="none")
                .execute()
            )
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            known_event_ids.discard(event_id)
            raise ValueError("That event no longer exists") from exc
        raise

    known_event_ids.discard(event_id)
    start = event.get("start", {})

    return {
        "success": True,
        "deleted": True,
        "event_id": event_id,
        "title": event.get("summary") or "Untitled event",
        "start": start.get("dateTime") or start.get("date"),
        "all_day": "date" in start,
        # Only this occurrence is removed for a recurring series.
        "recurring_occurrence": bool(event.get("recurringEventId")),
    }

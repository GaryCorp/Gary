"""The voice tools that reach Google Calendar, Gmail and Joplin.

The company tools (gary.tools) are dispatched by app.main; these are the
rest. session is what this conversation has seen: only listed or created
events can be deleted, and only listed or searched emails can be read or
replied to, so the model cannot act on guessed IDs.
"""

from app.config import USER_MAILBOX
from app.gmail import (
    find_email_contact,
    list_unread_emails,
    read_email,
    search_emails,
    send_email_reply,
    send_new_email,
)
from app.google_calendar import (
    create_all_day_event,
    create_calendar_event,
    delete_calendar_event,
    list_calendar_events,
)
from app.joplin import (
    create_joplin_note,
    create_joplin_notebook,
    delete_joplin_note,
    list_joplin_notebooks,
    list_joplin_notes,
)


async def run_integration_tool(name: str, arguments: dict, session: dict) -> dict:
    """Run one Google or Joplin tool. Raises on an unknown name or failure;
    the caller turns that into the tool's error result."""
    if name == "create_calendar_event":
        result = await create_calendar_event(
            title=arguments["title"],
            start_time=arguments["start_time"],
            end_time=arguments["end_time"],
            description=arguments.get("description", ""),
        )
        session["event_ids"].add(result["event_id"])
    elif name == "create_all_day_event":
        result = await create_all_day_event(
            title=arguments["title"],
            start_date=arguments["start_date"],
            end_date=arguments.get("end_date", ""),
            description=arguments.get("description", ""),
        )
        session["event_ids"].add(result["event_id"])
    elif name == "list_calendar_events":
        result = await list_calendar_events(
            start_time=arguments["start_time"],
            end_time=arguments["end_time"],
            max_results=arguments.get("max_results", 10),
            query=arguments.get("query", ""),
        )
        session["event_ids"].update(
            event["event_id"]
            for event in result["events"]
            if event["event_id"]
        )
    elif name == "delete_calendar_event":
        result = await delete_calendar_event(
            event_id=arguments["event_id"],
            confirmed=arguments.get("confirmed", False),
            known_event_ids=session["event_ids"],
        )
    elif name == "list_unread_emails":
        result = await list_unread_emails(
            max_results=arguments.get("max_results", 5),
            email_session=session,
            mailbox=arguments.get("mailbox", USER_MAILBOX),
        )
    elif name == "search_emails":
        result = await search_emails(
            query=arguments["query"],
            max_results=arguments.get("max_results", 5),
            email_session=session,
            mailbox=arguments.get("mailbox", USER_MAILBOX),
        )
    elif name == "find_email_contact":
        result = await find_email_contact(
            name=arguments["name"],
            mailbox=arguments.get("mailbox", USER_MAILBOX),
        )
    elif name == "read_email":
        result = await read_email(
            email_id=arguments["email_id"],
            email_session=session,
        )
    elif name == "send_email_reply":
        result = await send_email_reply(
            email_id=arguments["email_id"],
            body=arguments["body"],
            confirmed=arguments.get("confirmed", False),
            email_session=session,
        )
    elif name == "list_joplin_notebooks":
        result = await list_joplin_notebooks()
    elif name == "create_joplin_notebook":
        result = await create_joplin_notebook(name=arguments["name"])
    elif name == "create_joplin_note":
        result = await create_joplin_note(
            title=arguments["title"],
            body=arguments.get("body", ""),
            notebook=arguments.get("notebook", ""),
        )
        session["note_ids"].add(result["note_id"])
    elif name == "list_joplin_notes":
        result = await list_joplin_notes(
            notebook=arguments.get("notebook", ""),
            query=arguments.get("query", ""),
        )
        session["note_ids"].update(note["note_id"] for note in result["notes"])
    elif name == "delete_joplin_note":
        result = await delete_joplin_note(
            note_id=arguments["note_id"],
            confirmed=arguments.get("confirmed", False),
            known_note_ids=session["note_ids"],
        )
    elif name == "send_new_email":
        result = await send_new_email(
            to=arguments["to"],
            subject=arguments["subject"],
            body=arguments["body"],
            confirmed=arguments.get("confirmed", False),
            new_recipient_confirmed=arguments.get(
                "new_recipient_confirmed", False
            ),
            email_session=session,
            from_mailbox=arguments.get("from_mailbox", ""),
        )
    else:
        raise ValueError(f"Unknown tool: {name}")

    return result

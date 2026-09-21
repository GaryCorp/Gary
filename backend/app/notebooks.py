"""The Joplin notebooks the company reads and writes outside a conversation.

Gary > Planning and Daily Summaries for the planning cycle and the weekly
review, each specialist's own top-level notebook, and Gary > Spoken, the
readable copy of everything Gary said out loud. The conversational Joplin
tools are in app.joplin.
"""

import datetime as dt
import urllib.parse
from zoneinfo import ZoneInfo

from app.config import (
    JOPLIN_NOTE_BODY_LIMIT,
    JOPLIN_NOTE_TITLE_LIMIT,
    JOPLIN_PLANNING_NOTEBOOK,
    JOPLIN_SPOKEN_NOTEBOOK,
    JOPLIN_SUMMARY_NOTEBOOK,
    JOPLIN_TOKEN,
    LOCAL_TIMEZONE,
    PLANNING_NOTE_CHARS,
    SPOKEN_NOTE_PREFIX,
)
from app.gmail import single_line
from app.joplin import (
    JoplinError,
    create_joplin_notebook,
    gary_notebooks,
    joplin_items,
    joplin_request,
    joplin_time_local,
    notebook_key,
)
from app.local_time import spoken_clock
from gary.services.planning_cycle import (
    daily_summary_title,
    previous_summary,
    select_relevant_notes,
)
from gary.services.weekly_review import review_title
from gary.timeutil import to_datetime


def find_child_notebook(children: list[dict], name: str) -> dict | None:
    return next(
        (f for f in children if notebook_key(f["title"]) == notebook_key(name)),
        None,
    )


class JoplinPlanningNotebook:
    """Reads only Gary > Planning notes titled like an active project, and the
    previous daily summary Gary wrote. Writes one summary note per day."""

    async def _note_text(self, note_id: str) -> str:
        note = await joplin_request(
            "GET", f"/notes/{urllib.parse.quote(note_id)}?fields=body"
        )
        return (note.get("body") or "")[:PLANNING_NOTE_CHARS]

    async def get_relevant_notes(self, project_names: list[str], today: dt.date) -> list[dict]:
        if not JOPLIN_TOKEN:
            return []
        _, children = await gary_notebooks()
        notes = []

        planning = find_child_notebook(children, JOPLIN_PLANNING_NOTEBOOK)
        if planning:
            listed = await joplin_items(f"/folders/{planning['id']}/notes?fields=id,title")
            for note in select_relevant_notes(listed, project_names):
                notes.append(
                    {
                        "source": f"{JOPLIN_PLANNING_NOTEBOOK} note",
                        "title": note["title"],
                        "text": await self._note_text(note["id"]),
                    }
                )

        summaries = find_child_notebook(children, JOPLIN_SUMMARY_NOTEBOOK)
        if summaries:
            listed = await joplin_items(f"/folders/{summaries['id']}/notes?fields=id,title")
            previous = previous_summary(listed, today)
            if previous:
                notes.append(
                    {
                        "source": "previous daily summary",
                        "title": previous["title"],
                        "text": await self._note_text(previous["id"]),
                    }
                )
        return notes

    async def write_weekly_review(self, day: dt.date, markdown: str) -> None:
        await self._write_summary_note(review_title(day), markdown)

    async def write_daily_summary(self, day: dt.date, markdown: str) -> None:
        await self._write_summary_note(daily_summary_title(day), markdown)

    async def _write_summary_note(self, title: str, markdown: str) -> None:
        """Find or create one note in Daily Summaries and append to it."""
        if not JOPLIN_TOKEN:
            return
        await create_joplin_notebook(JOPLIN_SUMMARY_NOTEBOOK)
        _, children = await gary_notebooks()
        folder = find_child_notebook(children, JOPLIN_SUMMARY_NOTEBOOK)

        listed = await joplin_items(f"/folders/{folder['id']}/notes?fields=id,title")
        existing = next((n for n in listed if n["title"] == title), None)
        if existing is None:
            await joplin_request(
                "POST", "/notes", {"title": title, "body": markdown, "parent_id": folder["id"]}
            )
            return

        path = f"/notes/{urllib.parse.quote(existing['id'])}"
        current = await joplin_request("GET", f"{path}?fields=body")
        body = (current.get("body") or "").rstrip()
        await joplin_request("PUT", path, {"body": f"{body}\n\n{markdown}" if body else markdown})


class JoplinAgentNotebooks:
    """Access to each specialist's own top-level notebook: create notes with
    write_note, and list and read them with list_own_notes and read_own_note.
    Only notes directly in that
    notebook are listed or read; sub-notebooks and every other notebook are
    out of reach. The notebook must already exist; it is never created, and
    nested notebooks with the same name are never matched."""

    async def _notebook_id(self, notebook: str) -> str:
        if not JOPLIN_TOKEN:
            raise ValueError("Joplin is not set up")
        folders = await joplin_items("/folders?fields=id,title,parent_id")
        matches = [
            f for f in folders
            if not f.get("parent_id") and notebook_key(f["title"]) == notebook_key(notebook)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"The top-level Joplin notebook {notebook!r} was not found"
                if not matches else f"There are several top-level notebooks named {notebook!r}"
            )
        return matches[0]["id"]

    async def create_note(self, notebook: str, title: str, body: str) -> dict:
        folder_id = await self._notebook_id(notebook)
        created = await joplin_request(
            "POST", "/notes",
            {"title": title[:JOPLIN_NOTE_TITLE_LIMIT], "body": body[:JOPLIN_NOTE_BODY_LIMIT],
             "parent_id": folder_id},
        )
        return {"note_id": created.get("id")}

    async def list_notes(self, notebook: str, query: str) -> list[dict]:
        folder_id = await self._notebook_id(notebook)
        words = notebook_key(single_line(query or "")[:100]).split()
        notes = [
            note for note in await joplin_items(f"/folders/{folder_id}/notes?fields=id,title,updated_time")
            if all(word in (note.get("title") or "").casefold() for word in words)
        ]
        notes.sort(key=lambda note: note.get("updated_time") or 0, reverse=True)
        return [
            {"note_id": note["id"], "title": note.get("title") or "Untitled",
             "updated": joplin_time_local(note.get("updated_time"))}
            for note in notes
        ]

    async def read_note(self, notebook: str, note_id: str) -> dict:
        folder_id = await self._notebook_id(notebook)
        try:
            note = await joplin_request(
                "GET",
                f"/notes/{urllib.parse.quote(note_id)}?fields=id,title,body,parent_id,updated_time,deleted_time",
            )
        except JoplinError as exc:
            if exc.status == 404:
                raise ValueError(f"No note with that note_id in the {notebook} notebook") from exc
            raise
        # The same answer whether the note is elsewhere or missing, so other
        # notebooks cannot be probed.
        if note.get("parent_id") != folder_id or note.get("deleted_time"):
            raise ValueError(f"No note with that note_id in the {notebook} notebook")
        return {
            "note_id": note["id"],
            "title": note.get("title") or "Untitled",
            "updated": joplin_time_local(note.get("updated_time")),
            "body": note.get("body") or "",
        }



class SpokenNotebook:
    """One note a day in Gary > Spoken, holding everything Gary said out loud.

    SQLite is the record; this is the copy Alex can read. It is written after
    the message has been spoken, and a failure here is never fatal: the row
    keeps joplin_written_at NULL and the delivery pass tries again.
    """

    async def append(self, message: dict, again: bool = False) -> str | None:
        if not JOPLIN_TOKEN:
            return None

        await create_joplin_notebook(JOPLIN_SPOKEN_NOTEBOOK)
        _, children = await gary_notebooks()
        folder = find_child_notebook(children, JOPLIN_SPOKEN_NOTEBOOK)
        if folder is None:
            raise JoplinError(f"The {JOPLIN_SPOKEN_NOTEBOOK} notebook is missing")

        said_at = message["last_spoken_at"] or message["spoken_at"]
        day = to_datetime(said_at).astimezone(ZoneInfo(LOCAL_TIMEZONE)).date()
        title = f"{SPOKEN_NOTE_PREFIX}{day.isoformat()}"
        line = spoken_note_line(message, again)

        listed = await joplin_items(f"/folders/{folder['id']}/notes?fields=id,title")
        existing = next((n for n in listed if n["title"] == title), None)
        if existing is None:
            created = await joplin_request(
                "POST",
                "/notes",
                {"title": title, "body": line, "parent_id": folder["id"]},
            )
            return created.get("id")

        path = f"/notes/{urllib.parse.quote(existing['id'])}"
        current = await joplin_request("GET", f"{path}?fields=body")
        body = (current.get("body") or "").rstrip()
        await joplin_request("PUT", path, {"body": f"{body}\n{line}" if body else line})
        return existing["id"]


def spoken_note_line(message: dict, again: bool = False) -> str:
    """One line of the day's note: when he said it, and what he said."""
    said_at = message["last_spoken_at"] or message["spoken_at"]
    prefix = "said again" if again else "said"
    waiting = (
        " _(waiting on your answer)_"
        if message["expects_reply"] and message["status"] == "spoken"
        else ""
    )
    return f"- **{spoken_clock(said_at)}** Gary {prefix}: {single_line(message['text'])}{waiting}"

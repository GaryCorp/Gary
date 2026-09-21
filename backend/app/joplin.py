"""Joplin notes through the local Data API (via joplin-proxy), limited to
Gary's notebooks.
"""

import asyncio
import datetime as dt
import json
import urllib.request
from zoneinfo import ZoneInfo

from app.config import (
    JOPLIN_API_URL,
    JOPLIN_NOTEBOOK,
    JOPLIN_NOTEBOOK_NAME_LIMIT,
    JOPLIN_NOTE_BODY_LIMIT,
    JOPLIN_NOTE_LIST_LIMIT,
    JOPLIN_NOTE_TITLE_LIMIT,
    JOPLIN_PLANNING_NOTEBOOK,
    JOPLIN_TOKEN,
    LOCAL_TIMEZONE,
)
from app.gmail import single_line


class JoplinError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def joplin_request_sync(method: str, path: str, body: dict | None = None) -> dict:
    if not JOPLIN_TOKEN:
        raise JoplinError(
            "Joplin is not set up. Tell the user to add JOPLIN_TOKEN to .env "
            "and restart the backend."
        )

    separator = "&" if "?" in path else "?"
    url = (
        f"{JOPLIN_API_URL}{path}{separator}"
        f"token={urllib.parse.quote(JOPLIN_TOKEN)}"
    )
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise JoplinError(
                "Joplin rejected the API token. Tell the user to copy the token "
                "from Joplin's Web Clipper options into JOPLIN_TOKEN in .env."
            ) from exc
        raise JoplinError(f"Joplin returned HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise JoplinError(
            "Could not reach Joplin. Tell the user to open the Joplin desktop "
            "app and check that the Web Clipper service is enabled."
        ) from exc


async def joplin_request(method: str, path: str, body: dict | None = None) -> dict:
    return await asyncio.to_thread(joplin_request_sync, method, path, body)


def notebook_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def clean_notebook_name(name: str) -> str:
    name = " ".join(single_line(name).split())
    if not name:
        raise ValueError("notebook name cannot be empty")
    if len(name) > JOPLIN_NOTEBOOK_NAME_LIMIT:
        raise ValueError(
            f"notebook name cannot exceed {JOPLIN_NOTEBOOK_NAME_LIMIT} characters"
        )
    return name


async def gary_notebooks() -> tuple[dict, list[dict]]:
    """Return Gary's top-level notebook, creating it if needed, and its
    direct sub-notebooks. Gary cannot write anywhere else in Joplin."""
    folders = []
    page = 1
    while True:
        result = await joplin_request(
            "GET", f"/folders?fields=id,title,parent_id&limit=100&page={page}"
        )
        folders.extend(result.get("items", []))
        if not result.get("has_more"):
            break
        page += 1

    root_key = notebook_key(JOPLIN_NOTEBOOK)
    root = next(
        (
            folder
            for folder in folders
            if not folder.get("parent_id")
            and notebook_key(folder.get("title", "")) == root_key
        ),
        None,
    )
    if root is None:
        root = await joplin_request("POST", "/folders", {"title": JOPLIN_NOTEBOOK})
        return root, []

    children = [
        folder for folder in folders if folder.get("parent_id") == root["id"]
    ]
    return root, sorted(children, key=lambda folder: folder["title"].casefold())


async def list_joplin_notebooks() -> dict:
    root, children = await gary_notebooks()
    return {
        "success": True,
        "main_notebook": root["title"],
        "sub_notebooks": [folder["title"] for folder in children],
    }


async def create_joplin_notebook(name: str) -> dict:
    name = clean_notebook_name(name)
    root, children = await gary_notebooks()

    if notebook_key(name) == notebook_key(root["title"]):
        raise ValueError(
            f"{root['title']} is the main notebook; choose a different name"
        )

    existing = next(
        (
            folder
            for folder in children
            if notebook_key(folder["title"]) == notebook_key(name)
        ),
        None,
    )
    if existing:
        return {
            "success": True,
            "created": False,
            "notebook": existing["title"],
            "inside": root["title"],
            "note": "A notebook with this name already exists.",
        }

    created = await joplin_request(
        "POST", "/folders", {"title": name, "parent_id": root["id"]}
    )
    return {
        "success": True,
        "created": True,
        "notebook": created.get("title", name),
        "inside": root["title"],
    }


async def create_joplin_note(title: str, body: str, notebook: str) -> dict:
    title = single_line(title)
    if not title:
        raise ValueError("title cannot be empty")
    if len(title) > JOPLIN_NOTE_TITLE_LIMIT:
        raise ValueError(f"title cannot exceed {JOPLIN_NOTE_TITLE_LIMIT} characters")

    body = (body or "").strip()
    if len(body) > JOPLIN_NOTE_BODY_LIMIT:
        raise ValueError(f"body cannot exceed {JOPLIN_NOTE_BODY_LIMIT} characters")

    root, children = await gary_notebooks()
    target = root
    notebook = " ".join((notebook or "").split())
    if notebook and notebook_key(notebook) != notebook_key(root["title"]):
        target = next(
            (
                folder
                for folder in children
                if notebook_key(folder["title"]) == notebook_key(notebook)
            ),
            None,
        )
        if target is None and notebook_key(notebook) == notebook_key(JOPLIN_PLANNING_NOTEBOOK):
            # Gary's own planning notebook is created on first use.
            await create_joplin_notebook(JOPLIN_PLANNING_NOTEBOOK)
            _, children = await gary_notebooks()
            target = next(
                (f for f in children if notebook_key(f["title"]) == notebook_key(notebook)),
                None,
            )
        if target is None:
            raise ValueError(
                f"There is no notebook named {notebook} inside {root['title']}. "
                "Ask the user whether to create it with create_joplin_notebook "
                f"or put the note in {root['title']}."
            )

    created = await joplin_request(
        "POST",
        "/notes",
        {"title": title, "body": body, "parent_id": target["id"]},
    )
    return {
        "success": True,
        "created": True,
        "note_id": created["id"],
        "title": title,
        "notebook": target["title"],
        "inside": None if target is root else root["title"],
    }


async def joplin_items(path: str) -> list[dict]:
    items = []
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        result = await joplin_request(
            "GET", f"{path}{separator}limit=100&page={page}"
        )
        items.extend(result.get("items", []))
        if not result.get("has_more"):
            return items
        page += 1


def joplin_time_local(milliseconds) -> str:
    if not milliseconds:
        return ""
    return (
        dt.datetime.fromtimestamp(milliseconds / 1000, ZoneInfo(LOCAL_TIMEZONE))
        .replace(microsecond=0)
        .isoformat()
    )


async def list_joplin_notes(notebook: str, query: str) -> dict:
    root, children = await gary_notebooks()

    notebook = " ".join((notebook or "").split())
    if not notebook:
        folders = [root, *children]
    elif notebook_key(notebook) == notebook_key(root["title"]):
        folders = [root]
    else:
        folders = [
            folder
            for folder in children
            if notebook_key(folder["title"]) == notebook_key(notebook)
        ]
        if not folders:
            raise ValueError(
                f"There is no notebook named {notebook} inside {root['title']}."
            )

    words = notebook_key(single_line(query or "")[:100]).split()

    notes = []
    for folder in folders:
        # Titles and times only: note bodies are never sent to the model.
        for note in await joplin_items(
            f"/folders/{folder['id']}/notes?fields=id,title,updated_time"
        ):
            title = note.get("title") or "Untitled"
            if all(word in title.casefold() for word in words):
                notes.append({**note, "title": title, "notebook": folder["title"]})

    notes.sort(key=lambda note: note.get("updated_time") or 0, reverse=True)

    return {
        "success": True,
        "timezone": LOCAL_TIMEZONE,
        "count": min(len(notes), JOPLIN_NOTE_LIST_LIMIT),
        "total_matches": len(notes),
        "notes": [
            {
                "note_id": note["id"],
                "title": note["title"],
                "notebook": note["notebook"],
                "updated": joplin_time_local(note.get("updated_time")),
            }
            for note in notes[:JOPLIN_NOTE_LIST_LIMIT]
        ],
    }


async def delete_joplin_note(
    note_id: str,
    confirmed: bool,
    known_note_ids: set[str],
) -> dict:
    if confirmed is not True:
        raise ValueError(
            "Deletion not confirmed. Tell the user the note title and notebook, "
            "ask them to confirm, then call again with confirmed set to true."
        )

    note_id = (note_id or "").strip()
    if note_id not in known_note_ids:
        raise ValueError(
            "Unknown note_id. Call list_joplin_notes first and use a note_id it "
            "returned in this conversation."
        )

    # Check the note's current location, since it may have been moved since
    # it was listed.
    root, children = await gary_notebooks()
    allowed = {folder["id"]: folder["title"] for folder in [root, *children]}

    note_path = f"/notes/{urllib.parse.quote(note_id)}"
    try:
        note = await joplin_request(
            "GET", f"{note_path}?fields=id,title,parent_id,deleted_time"
        )
    except JoplinError as exc:
        if exc.status == 404:
            known_note_ids.discard(note_id)
            raise ValueError("That note no longer exists") from exc
        raise

    if note.get("deleted_time"):
        known_note_ids.discard(note_id)
        raise ValueError("That note is already in the Joplin trash")
    if note.get("parent_id") not in allowed:
        known_note_ids.discard(note_id)
        raise ValueError(
            f"That note is no longer in {root['title']}, so it cannot be deleted"
        )

    # Without permanent=1 Joplin moves the note to its trash.
    await joplin_request("DELETE", note_path)
    known_note_ids.discard(note_id)

    return {
        "success": True,
        "deleted": True,
        "title": note.get("title") or "Untitled",
        "notebook": allowed[note["parent_id"]],
        "moved_to_trash": True,
    }

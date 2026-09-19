"""Gary speaking first.

Gary used to be able to answer and never to ask. Everything he said was
broadcast to whatever voice client happened to be connected and then forgotten:
no record that he spoke, no way to say it again, nothing left if Alex was out
of the room.

A ``spoken_messages`` row fixes that by separating two things that used to be
one. Deciding to speak is an action, validated and capped like any other.
Actually speaking is delivery, which happens later, may fail, and is retried.
The row is written first and marked spoken only once a voice client has
received it, so a message is never silently lost and never spoken twice.

What it costs is Alex's attention, so the caps here are the real safeguard:
a few questions open at once, a few a day from the unattended loops, and the
same question never asked twice while one is still open.
"""

import asyncio
import datetime as dt
import logging
from typing import Protocol

from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.conversation import SOURCES, AskUserPayload
from gary.policy import APPROVAL_EXPIRY_HOURS, GARY_ACTOR, SYSTEM_ACTOR
from gary.services.action_service import ActionHandler
from gary.services.common import (
    Clock,
    clock_now,
    default_clock,
    looks_like_repeat,
    objective_key,
    require_project,
    require_task,
)
from gary.timeutil import format_utc, to_datetime

logger = logging.getLogger("gary.conversation")

# At most this many questions may be waiting on Alex at once. Past this Gary
# is not being helpful, he is queueing.
MAX_OPEN_QUESTIONS = 3
# And at most this many raised in a local day by the loops that run with
# nobody watching, mirroring DailyBudget in management_loop.py.
MAX_DAILY_UNATTENDED = 6
UNATTENDED_SOURCES = ("planning_cycle", "management_loop", "approval")
# "Say that again" is a reasonable request; saying it forever is not.
MAX_REPEATS = 3
# Once Alex has answered something, Gary leaves it alone this long. The same
# window as REPEAT_ASSIGNMENT_DAYS in planning_cycle.py, so "Gary already
# covered this" means one thing across the company.
ANSWERED_QUIET_DAYS = 3
# How long Gary keeps looking at what he has already said.
RECENT_HOURS = 12
# An unanswered question goes stale on the same clock as an approval.
MESSAGE_EXPIRY_HOURS = APPROVAL_EXPIRY_HOURS


def topic_key_for(message: str) -> str:
    """Stable content words, so the same question is recognised again."""
    return " ".join(sorted(objective_key(message)))


def expire_stale_messages(repos: Repositories, now: str) -> list[str]:
    """Expire questions Alex never got to. Runs inside the caller's
    transaction, exactly like expire_stale_approvals."""
    cutoff = format_utc(to_datetime(now) - dt.timedelta(hours=MESSAGE_EXPIRY_HOURS))
    expired = []
    for message in repos.spoken.list_open_created_before(cutoff):
        if not repos.spoken.expire(message["id"], now):
            continue
        repos.audit.write(
            SYSTEM_ACTOR,
            "spoken_message_expired",
            f"Gary stopped waiting for an answer: {message['text'][:120]}",
            "spoken_message",
            message["id"],
            {"source": message["source"], "kind": message["kind"]},
            now=now,
        )
        expired.append(message["id"])
    return expired


def quiet_since(now: str) -> str:
    """How far back a settled question still counts as settled."""
    return format_utc(to_datetime(now) - dt.timedelta(days=ANSWERED_QUIET_DAYS))


def close_settled_questions(repos: Repositories, now: str) -> list[str]:
    """Stop waiting on a question whose approval has already been decided.

    Resolving an approval closes its question directly, so this is a safety
    net for anything settled another way. Without it Gary would open the next
    conversation asking about a decision the user has already made.
    """
    closed = []
    for message in repos.spoken.list_open_with_settled_approval():
        decision = message["approval_status"]
        if message["status"] == "pending":
            # Never said, and no longer worth saying.
            if not repos.spoken.expire(message["id"], now):
                continue
        elif not repos.spoken.answer(message["id"], f"{decision} on the approvals page", now):
            continue
        repos.audit.write(
            SYSTEM_ACTOR,
            "spoken_message_settled",
            f"Already {decision}, so Gary stopped waiting: {message['text'][:120]}",
            "spoken_message",
            message["id"],
            {"approval_id": message["approval_id"], "decision": decision},
            now=now,
        )
        closed.append(message["id"])
    return closed


def check_message_caps(
    repos: Repositories,
    payload: AskUserPayload,
    *,
    now: str,
    day_start: str,
) -> None:
    """Every reason Gary may not speak right now. Raises ValueError."""
    open_messages = repos.spoken.list_open()
    questions = [m for m in open_messages if m["expects_reply"]]
    if payload.expects_reply and len(questions) >= MAX_OPEN_QUESTIONS:
        raise ValueError(
            f"{len(questions)} questions are already waiting for Alex; at most "
            f"{MAX_OPEN_QUESTIONS} at a time. Wait for an answer to one of them."
        )

    if payload.source in UNATTENDED_SOURCES:
        raised = repos.spoken.count_created_since(day_start, UNATTENDED_SOURCES)
        if raised >= MAX_DAILY_UNATTENDED:
            raise ValueError(
                f"Gary has already raised {raised} things with Alex today; "
                f"at most {MAX_DAILY_UNATTENDED} unprompted a day."
            )

    key = objective_key(payload.message)
    open_keys = [objective_key(m["topic_key"]) for m in open_messages]
    if looks_like_repeat(key, open_keys):
        raise ValueError("Gary has already asked Alex something very like this; it is still open.")

    settled = [
        objective_key(topic)
        for topic in repos.spoken.answered_topic_keys(quiet_since(now))
    ]
    if looks_like_repeat(key, settled):
        raise ValueError(
            "Alex has already answered something very like this in the last "
            f"{ANSWERED_QUIET_DAYS} days. Act on his answer instead of asking again."
        )



class ConversationService:
    """Reads and state changes for what Gary has said. Delivery lives in the
    application, which is the only part that knows about voice clients."""

    def __init__(self, db: Database, timezone, clock: Clock = default_clock):
        self.db = db
        self.timezone = timezone
        self.clock = clock

    def day_start(self, now: str) -> str:
        """Midnight local, as UTC, for the daily cap."""
        local = to_datetime(now).astimezone(self.timezone)
        return format_utc(
            dt.datetime.combine(local.date(), dt.time.min, tzinfo=self.timezone)
        )

    def announce(
        self,
        text: str,
        *,
        kind: str = "notice",
        source: str = "operations",
        expects_reply: bool = False,
        urgency: str = "now",
        approval_id: str | None = None,
        action_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """Queue something the system decided to say, outside the action
        pipeline: a new email, an alert, an approval that needs Alex.

        Gary did not choose these, so they are not capped like ``ask_user``;
        each already has its own trigger upstream. They are deduplicated by
        approval, so one approval is raised once however often it is seen.
        """
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            if approval_id and repos.spoken.get_by_approval(approval_id) is not None:
                return repos.spoken.get_by_approval(approval_id)
            return repos.spoken.create(
                text=text,
                kind=kind,
                source=source,
                expects_reply=expects_reply,
                urgency=urgency,
                topic_key=topic_key_for(text),
                approval_id=approval_id,
                action_id=action_id,
                project_id=project_id,
                task_id=task_id,
                now=now,
            )

    def pending(self, limit: int = 20) -> list[dict]:
        """Decided but not yet spoken. Expires stale ones first."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            close_settled_questions(repos, now)
            expire_stale_messages(repos, now)
            return repos.spoken.list_pending(limit)

    def to_mention(self) -> list[dict]:
        """What a new conversation should open knowing: messages held for
        exactly this moment, and questions still waiting on an answer."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            close_settled_questions(repos, now)
            expire_stale_messages(repos, now)
            return repos.spoken.list_to_mention()

    def awaiting_answer(self) -> list[dict]:
        with self.db.read() as conn:
            return Repositories.bind(conn).spoken.list_awaiting_answer()

    def recent(self, hours: int = RECENT_HOURS, limit: int = 20) -> list[dict]:
        """What Gary has said lately, so he can read it back."""
        since = format_utc(to_datetime(clock_now(self.clock)) - dt.timedelta(hours=hours))
        with self.db.read() as conn:
            return Repositories.bind(conn).spoken.list_recent(since, limit)

    def get(self, message_id: str) -> dict | None:
        with self.db.read() as conn:
            return Repositories.bind(conn).spoken.get(message_id)

    def mark_spoken(self, message_id: str) -> bool:
        """Called only once a voice client has actually received it."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            if not repos.spoken.mark_spoken(message_id, now):
                return False
            message = repos.spoken.get(message_id)
            repos.audit.write(
                GARY_ACTOR,
                "spoke_to_user",
                f"Gary said: {message['text'][:200]}",
                "spoken_message",
                message_id,
                {
                    "source": message["source"],
                    "kind": message["kind"],
                    "expects_reply": bool(message["expects_reply"]),
                },
                now=now,
            )
            return True

    def repeat(self, message_id: str) -> dict:
        """Alex missed it. Say it again, up to MAX_REPEATS."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            message = repos.spoken.get(message_id)
            if message is None:
                raise ValueError(f"No message with id {message_id}")
            if message["spoken_at"] is None:
                raise ValueError("Gary has not said that yet.")
            if message["repeat_count"] >= MAX_REPEATS:
                raise ValueError(
                    f"Gary has already repeated that {MAX_REPEATS} times. "
                    "It is in the Spoken notebook in Joplin."
                )
            repos.spoken.record_repeat(message_id, now)
            repos.audit.write(
                GARY_ACTOR,
                "spoke_to_user_again",
                f"Gary repeated: {message['text'][:200]}",
                "spoken_message",
                message_id,
                {"repeat_count": message["repeat_count"] + 1},
                now=now,
            )
            return repos.spoken.get(message_id)

    def answer(self, message_id: str, answer: str) -> dict:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            message = repos.spoken.get(message_id)
            if message is None:
                raise ValueError(f"No message with id {message_id}")
            if not repos.spoken.answer(message_id, answer, now):
                raise ValueError(
                    "Gary has not asked Alex that yet."
                    if message["status"] == "pending"
                    else f"That question is already {message['status']}."
                )
            repos.audit.write(
                GARY_ACTOR,
                "user_answered",
                f"Alex answered: {answer[:200]}",
                "spoken_message",
                message_id,
                {"question": message["text"][:200]},
                now=now,
            )
            return repos.spoken.get(message_id)

    # ----------------------------------------------------- the Joplin mirror

    def unmirrored(self, limit: int = 20) -> list[dict]:
        with self.db.read() as conn:
            return Repositories.bind(conn).spoken.list_unmirrored(limit)

    def mark_mirrored(self, message_id: str, note_id: str | None) -> bool:
        with self.db.transaction() as conn:
            return Repositories.bind(conn).spoken.mark_mirrored(
                message_id, note_id, clock_now(self.clock)
            )


class VoiceSender(Protocol):
    """Pushes one line to whatever is listening. True if anything took it."""

    async def __call__(self, text: str, expects_reply: bool) -> bool: ...


class SpokenNotebook(Protocol):
    """Writes one spoken message into the notebook Alex can read."""

    async def append(self, message: dict, again: bool = False) -> str | None: ...


class SpokenDelivery:
    """Turning a decision to speak into words actually said.

    Kept apart from ConversationService because it is the only part that
    talks to the outside: the voice service and Joplin. Both can fail, and
    neither failure may lose a message. A message is marked spoken only once
    a voice client has taken it, and the note is written afterwards and
    retried, so the record catches up rather than going missing.
    """

    def __init__(
        self,
        conversation: ConversationService,
        send: VoiceSender,
        notebook: SpokenNotebook | None = None,
    ):
        self.conversation = conversation
        self.send = send
        self.notebook = notebook

    async def speak(self, text: str, **kwargs) -> dict:
        """Record it, then try to say it now. The one way Gary speaks first."""
        message = await asyncio.to_thread(self.conversation.announce, text, **kwargs)
        await self.deliver(message)
        return self.conversation.get(message["id"]) or message

    async def mention_in_conversation(self, message: dict) -> bool:
        """Hand a held message to a conversation Gary is now in.

        He says it himself, in his own words, so the voice sender is not
        called: broadcasting it would speak it twice. It still counts as said,
        and still goes in the notebook.
        """
        if message["status"] != "pending":
            return False
        if not await asyncio.to_thread(self.conversation.mark_spoken, message["id"]):
            return False
        await self.mirror(self.conversation.get(message["id"]))
        return True

    async def deliver(self, message: dict) -> bool:
        if message["status"] != "pending":
            return False
        if not await self.send(message["text"], bool(message["expects_reply"])):
            # Nobody was listening. It stays pending for the next pass.
            return False
        if not await asyncio.to_thread(self.conversation.mark_spoken, message["id"]):
            return False
        await self.mirror(self.conversation.get(message["id"]))
        return True

    async def deliver_pending(self, limit: int = 20) -> int:
        said = 0
        for message in await asyncio.to_thread(self.conversation.pending, limit):
            if not await self.deliver(message):
                # Nothing is listening; stop rather than burn through the queue.
                break
            said += 1
        return said

    async def repeat(self, message_id: str, speak_aloud: bool = True) -> dict:
        """Say something again for Alex, and note the repeat in the notebook.

        ``speak_aloud`` is false when Gary is already in a conversation and
        will read it back himself; broadcasting it too would say it twice.
        """
        message = await asyncio.to_thread(self.conversation.repeat, message_id)
        if speak_aloud:
            await self.send(message["text"], bool(message["expects_reply"]))
        await self.mirror(message, again=True)
        return message

    async def mirror(self, message: dict, again: bool = False) -> bool:
        """Write it into the notebook. Best effort: a failure is retried."""
        if self.notebook is None:
            return False
        try:
            note_id = await self.notebook.append(message, again)
        except Exception:
            logger.exception("Could not write spoken message %s to the notebook", message["id"])
            return False
        if again:
            # A repeat is an extra line under the message's own note.
            return True
        return await asyncio.to_thread(
            self.conversation.mark_mirrored, message["id"], note_id
        )

    async def retry_mirror(self, limit: int = 20) -> int:
        written = 0
        for message in await asyncio.to_thread(self.conversation.unmirrored, limit):
            if await self.mirror(message):
                written += 1
        return written


def ask_user_handler(conversation: ConversationService) -> dict[str, ActionHandler]:
    """The ``ask_user`` action: green, capped, and recorded but not yet said."""

    def _check(repos: Repositories, payload: AskUserPayload) -> dict:
        now = clock_now(conversation.clock)
        close_settled_questions(repos, now)
        expire_stale_messages(repos, now)
        require_project(repos, payload.project_id)
        require_task(repos, payload.task_id)
        check_message_caps(
            repos, payload, now=now, day_start=conversation.day_start(now)
        )
        return {}

    def _summarize(payload: AskUserPayload, context: dict) -> str:
        verb = "Ask Alex" if payload.expects_reply else "Tell Alex"
        return f"{verb}: {payload.message[:200]}"

    def _record(repos: Repositories, payload: AskUserPayload, result: dict, now: str) -> dict:
        message = repos.spoken.create(
            text=payload.message,
            kind="question" if payload.expects_reply else "notice",
            source=payload.source,
            expects_reply=payload.expects_reply,
            urgency=payload.urgency,
            topic_key=topic_key_for(payload.message),
            approval_id=payload.approval_id,
            project_id=payload.project_id,
            task_id=payload.task_id,
            now=now,
        )
        return {"message_id": message["id"], "status": message["status"]}

    return {
        "ask_user": ActionHandler(
            payload_model=AskUserPayload,
            summarize=_summarize,
            check=_check,
            record=_record,
            audit_event="spoken_message_queued",
        )
    }


__all__ = [
    "ANSWERED_QUIET_DAYS",
    "ConversationService",
    "SpokenDelivery",
    "MAX_DAILY_UNATTENDED",
    "MAX_OPEN_QUESTIONS",
    "MAX_REPEATS",
    "MESSAGE_EXPIRY_HOURS",
    "SOURCES",
    "ask_user_handler",
    "check_message_caps",
    "close_settled_questions",
    "expire_stale_messages",
    "topic_key_for",
]

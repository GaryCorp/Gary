"""Pausing the company, and the emailed commands that do it.

Alex is not always at the microphone. When GaryCorp runs itself for a week,
the one thing he must be able to do from anywhere is stop it, so a mail to
Gary's own address saying "pause" stops every unattended loop, and "resume"
starts them again. Talking to Gary is never paused: the way out of a pause
has to keep working.

What makes this safe enough to act on is that it is narrow and checked:

* only the address Alex is signed in with, compared exactly;
* only when Gmail's own Authentication-Results says the message passed SPF
  or DKIM for that domain, so a forged From is not enough;
* only the words pause, stop, resume or go, read from the subject or the
  first line, never anywhere else in the message;
* only once per message, because the cursor in operating_state moves past it.

Pausing is the *only* thing email can do. Approving an action, spending
money and changing the company all stay on the web page, where the session
cookie proves it is Alex.
"""

import re

from gary.db import Database
from gary.db.repositories import Repositories
from gary.policy import SYSTEM_ACTOR, USER_ACTOR
from gary.services.common import Clock, clock_now, default_clock

PAUSE_WORDS = {"pause", "stop", "halt"}
RESUME_WORDS = {"resume", "go", "start", "continue"}
# The first line only, and nothing else on it: "pause" is a command,
# "should we pause the Friday video?" is not.
_WORD = re.compile(r"^[\s>*_-]*([a-z]+)[\s.!]*$")


def parse_command(subject: str = "", body: str = "") -> str | None:
    """"pause", "resume", or None. The subject wins; otherwise the first
    non-empty line of the body."""
    candidates = [subject or ""]
    for line in (body or "").splitlines():
        if line.strip():
            candidates.append(line)
            break

    for candidate in candidates:
        match = _WORD.match(candidate.strip().casefold())
        if not match:
            continue
        word = match.group(1)
        if word in PAUSE_WORDS:
            return "pause"
        if word in RESUME_WORDS:
            return "resume"
    return None


def authenticated_sender(from_address: str, principal_email: str, auth_results: str) -> bool:
    """Whether this really is Alex's mailbox.

    Gmail writes Authentication-Results on delivery; without a pass for the
    sender's own domain the message is treated as forged and ignored. No
    header at all is also a refusal: this fails closed.
    """
    sender = (from_address or "").strip().casefold()
    expected = (principal_email or "").strip().casefold()
    if not sender or not expected or sender != expected:
        return False

    domain = sender.rpartition("@")[2]
    results = (auth_results or "").casefold()
    # Each result is its own ";"-separated clause, and the domain has to be
    # named in the clause that passed -- a pass for some other domain says
    # nothing about this sender.
    passed = [
        clause for clause in results.split(";")
        if re.search(r"\b(spf|dkim)=pass\b", clause)
    ]
    return any(domain in clause for clause in passed)


class OperatingState:
    """Running or paused, and the cursor into Alex's command emails."""

    def __init__(self, db: Database, clock: Clock = default_clock):
        self.db = db
        self.clock = clock

    def state(self) -> dict:
        with self.db.read() as conn:
            row = Repositories.bind(conn).operating.get()
        return {
            "paused": bool(row["paused"]),
            "reason": row["paused_reason"],
            "changed_at": row["changed_at"],
            "changed_by": row["changed_by"],
        }

    def is_paused(self) -> bool:
        return self.state()["paused"]

    def set_paused(
        self, paused: bool, reason: str | None = None, actor: str = USER_ACTOR
    ) -> dict:
        """Pause or resume. Returns the new state with ``changed`` saying
        whether this call was the one that changed it."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            before = repos.operating.get()
            if bool(before["paused"]) == paused:
                return {"paused": paused, "changed": False, "reason": before["paused_reason"]}
            repos.operating.set_paused(paused, reason, actor, now)
            repos.audit.write(
                actor,
                "company_paused" if paused else "company_resumed",
                (
                    f"Unattended work paused: {reason}" if paused
                    else "Unattended work resumed"
                ),
                "company",
                "operating_state",
                {"reason": reason},
                now=now,
            )
        return {"paused": paused, "changed": True, "reason": reason}

    # ------------------------------------------------------------ commands

    def cursor_ms(self) -> int:
        with self.db.read() as conn:
            return Repositories.bind(conn).operating.get()["commands_cursor_ms"]

    def start_from_now(self, now_ms: int) -> bool:
        """On a first start, skip past everything already in the inbox.

        The cursor begins at zero, and a week-old "stop" that Alex sent about
        something else must not pause the company the moment this is turned
        on. Returns whether the cursor was seeded.
        """
        if self.cursor_ms():
            return False
        with self.db.transaction() as conn:
            Repositories.bind(conn).operating.advance_cursor(now_ms, "start")
        return True

    def apply_command(self, message: dict, principal_email: str) -> dict:
        """Act on one candidate command email.

        ``message`` is {id, internal_date_ms, from, subject, body,
        auth_results}. Returns what happened and why, which is what the
        caller reports back to Alex.
        """
        outcome = {"id": message.get("id"), "applied": None, "refused": None}
        if message.get("internal_date_ms", 0) <= self.cursor_ms():
            outcome["refused"] = "already read"
            return outcome

        command = parse_command(message.get("subject", ""), message.get("body", ""))
        if command is None:
            # Not a command at all: step over it so the next poll is cheap.
            self._advance(message)
            outcome["refused"] = "not a command"
            return outcome

        if not authenticated_sender(
            message.get("from", ""), principal_email, message.get("auth_results", "")
        ):
            self._advance(message)
            outcome["refused"] = "sender not verified"
            self._audit_refusal(message)
            return outcome

        result = self.set_paused(
            command == "pause",
            reason="Alex emailed pause" if command == "pause" else None,
            actor=USER_ACTOR,
        )
        self._advance(message)
        outcome["applied"] = command
        outcome["changed"] = result["changed"]
        return outcome

    def _advance(self, message: dict) -> None:
        with self.db.transaction() as conn:
            Repositories.bind(conn).operating.advance_cursor(
                int(message.get("internal_date_ms", 0)), str(message.get("id", ""))
            )

    def _audit_refusal(self, message: dict) -> None:
        with self.db.transaction() as conn:
            Repositories.bind(conn).audit.write(
                SYSTEM_ACTOR,
                "email_command_refused",
                "An email command did not come from a verified address and was ignored",
                "company",
                "operating_state",
                {"message_id": str(message.get("id", ""))[:64]},
                now=clock_now(self.clock),
            )

"""The continuous management loop.

Between the three scheduled planning cycles, GaryCorp keeps running: reports
come back from specialists, blocks pass unfinished, follow-ups fall due,
approvals expire. This decides, in Python and without a model call, whether
anything has actually changed since the last cycle, and only then does Gary
think.

That check is what makes continuous operation affordable. A tick that finds
nothing new costs nothing: no model call, no tokens, no audit noise. A tick
that finds something runs one ``management`` planning cycle, which is bounded
by the same caps as every other cycle.

The loop is deliberately dumb about time: it does not decide what Gary should
do, only whether there is anything worth waking him for.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from gary.container import Gary
from gary.db.repositories import Repositories
from gary.policy import APPROVAL_EXPIRY_HOURS
from gary.timeutil import format_utc, to_datetime

logger = logging.getLogger("gary.management_loop")

# How soon a due follow-up or expiring approval counts as "now".
FOLLOWUP_WINDOW_MINUTES = 30
APPROVAL_WARNING_HOURS = 6
# A management cycle runs at most this often, whatever wakes the loop.
MIN_GAP_MINUTES = 10


@dataclass
class Triggers:
    """Why Gary should think now, or why he should not."""

    reasons: list[str] = field(default_factory=list)
    reports: int = 0
    skipped: str | None = None

    def __bool__(self) -> bool:
        return bool(self.reasons) and self.skipped is None

    def describe(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "nothing changed"


def find_triggers(
    gary: Gary,
    *,
    now: str,
    since: str | None,
    completed_reports: int = 0,
) -> Triggers:
    """What changed since the last cycle. Pure reads, no model call.

    ``since`` is when the last cycle finished; everything is measured from
    there, so a trigger cannot fire twice for the same event.
    """
    triggers = Triggers(reports=completed_reports)
    if completed_reports:
        triggers.reasons.append(
            f"{completed_reports} department report{'s' if completed_reports != 1 else ''} came back"
        )

    soon = format_utc(to_datetime(now) + dt.timedelta(minutes=FOLLOWUP_WINDOW_MINUTES))
    # Approvals have no expiry column: they expire by age, so an approval
    # created this long ago is within APPROVAL_WARNING_HOURS of lapsing.
    approval_cutoff = format_utc(
        to_datetime(now) - dt.timedelta(hours=APPROVAL_EXPIRY_HOURS - APPROVAL_WARNING_HOURS)
    )

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)

        missed = [
            task
            for task in repos.tasks.list_missed_blocks(now)
            # Only blocks that ended since the last cycle looked.
            if since is None or (task["scheduled_end"] or "") > since
        ]
        if missed:
            triggers.reasons.append(
                f"{len(missed)} scheduled block{'s' if len(missed) != 1 else ''} passed unfinished"
            )

        due = [f for f in repos.followups.list_pending() if (f["due_at"] or "") <= soon]
        if due:
            triggers.reasons.append(f"{len(due)} follow-up{'s' if len(due) != 1 else ''} due")

        overdue = [
            task
            for task in repos.tasks.list_overdue(now)
            if since is None or (task["deadline"] or "") > since
        ]
        if overdue:
            triggers.reasons.append(f"{len(overdue)} task deadline(s) passed")

        expiring = repos.approvals.list_pending_created_before(approval_cutoff)
        if expiring:
            triggers.reasons.append(
                f"{len(expiring)} approval{'s' if len(expiring) != 1 else ''} about to expire"
            )

        # Alex answered something Gary asked. That is new information from the
        # one person whose answer Gary cannot get any other way, so it is
        # worth a cycle rather than waiting for the next scheduled one.
        answered = [
            message
            for message in repos.spoken.list_answered_since(since or "")
            if since is None or (message["answered_at"] or "") > since
        ]
        if answered:
            triggers.reasons.append(
                f"{len(answered)} question{'s' if len(answered) != 1 else ''} answered"
            )

        # And a question of his own about to lapse unanswered, on the same
        # clock as an approval.
        lapsing = [
            message
            for message in repos.spoken.list_open_created_before(approval_cutoff)
            if message["expects_reply"]
        ]
        if lapsing:
            triggers.reasons.append(
                f"{len(lapsing)} question{'s' if len(lapsing) != 1 else ''} about to expire"
            )

    return triggers


def completed_since(team, since: str | None, limit: int = 25) -> int:
    """How many specialist reports finished since the last cycle."""
    if team is None:
        return 0
    try:
        assignments = team.list_assignments(limit=limit)
    except Exception as exc:  # the loop must survive a broken integration
        logger.warning("Could not read assignments for the management loop: %s", exc)
        return 0
    count = 0
    for assignment in assignments:
        if assignment.get("status") != "completed" or not assignment.get("report"):
            continue
        finished = assignment.get("completed_at")
        if since is None or (finished and to_datetime(finished) >= to_datetime(since)):
            count += 1
    return count


class DailyBudget:
    """A hard ceiling on unattended thinking, reset each local day.

    A stuck state (an integration flapping, a report that keeps arriving)
    must not spend all night calling the model.
    """

    def __init__(self, max_cycles_per_day: int, timezone):
        self.max_cycles_per_day = max_cycles_per_day
        self.timezone = timezone
        self._day: dt.date | None = None
        self._used = 0

    def _roll(self, now_local: dt.datetime) -> None:
        if self._day != now_local.date():
            self._day = now_local.date()
            self._used = 0

    def remaining(self, now_local: dt.datetime) -> int:
        self._roll(now_local)
        return max(0, self.max_cycles_per_day - self._used)

    def take(self, now_local: dt.datetime) -> bool:
        self._roll(now_local)
        if self._used >= self.max_cycles_per_day:
            return False
        self._used += 1
        return True

    @property
    def used_today(self) -> int:
        return self._used

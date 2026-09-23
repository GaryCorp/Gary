"""Performance reviews: who is due one, and what is stored when one is written.

The facts come from services/performance.py and the judgment from one model
call (gary/reviewer.py). This is the part in between: it decides who is due,
hands the scorecard over, validates what comes back, and stores the two
halves separately so nobody has to take the judgment on trust.

Nothing here is an action on the world. A review changes no permission, no
assignment and no roster entry; it is a record, and what is done about it is
Alex's decision. The one thing a review does oblige is an answer: a review of
Gary written by someone who works for him is not finished until he has
acknowledged it, and `unacknowledged_of_manager` is what keeps that visible.
"""

import datetime as dt
import json

from gary.db import Database
from gary.db.repositories import Repositories
from gary.policy import GARY_ACTOR, USER_ACTOR
from gary.services.common import Clock, NotFoundError, clock_now, default_clock
from gary.services.performance import (
    MIN_ASSIGNMENTS,
    employee_scorecard,
    manager_scorecard,
    principal_scorecard,
)
from gary.timeutil import format_utc, to_datetime

# The period a review covers, and how long before the same subject is due
# again. Reviewing weekly would say more about the noise than the work.
REVIEW_PERIOD_DAYS = 28
REVIEW_INTERVAL_DAYS = 28
# What one run may spend on judgment: each review is one model call.
MAX_REVIEWS_PER_RUN = 8

SUMMARY_LIMIT = 4000
ITEM_LIMIT = 500
LIST_LIMIT = 6
KINDS = ("employee", "principal", "manager")


class ReviewError(ValueError):
    pass


def present_review(row: dict | None) -> dict | None:
    """One stored review, with its JSON columns read back as lists. The two
    halves stay labelled: ``scorecard`` is what was counted, the rest is what
    one model made of it."""
    if row is None:
        return None
    review = {
        key: value for key, value in row.items() if not key.endswith("_json")
    }
    for column in ("scorecard", "strengths", "concerns", "recommendations", "evidence"):
        raw = row.get(f"{column}_json")
        try:
            review[column] = json.loads(raw) if raw else ([] if column != "scorecard" else {})
        except json.JSONDecodeError:
            review[column] = [] if column != "scorecard" else {}
    return review


def clean_list(values, limit: int = LIST_LIMIT) -> list[str]:
    cleaned = []
    for value in values or []:
        text = " ".join(str(value).split())[:ITEM_LIMIT]
        if text:
            cleaned.append(text)
    return cleaned[:limit]


def validate_judgment(judgment: dict) -> dict:
    """What a model wrote, checked before it is stored.

    A review with no summary or no evidence is not a review, whatever the
    model returned; storing one would put an empty judgment on somebody's
    record.
    """
    summary = " ".join(str(judgment.get("summary") or "").split())[:SUMMARY_LIMIT]
    if len(summary) < 20:
        raise ReviewError("A review needs a summary")
    evidence = clean_list(judgment.get("evidence"))
    if not evidence:
        raise ReviewError("A review has to quote the figures it relied on")
    return {
        "summary": summary,
        "strengths": clean_list(judgment.get("strengths")),
        "concerns": clean_list(judgment.get("concerns")),
        "recommendations": clean_list(judgment.get("recommendations")),
        "evidence": evidence,
    }


class PerformanceReviews:
    def __init__(
        self,
        db: Database,
        registry=None,
        reviewer=None,
        clock: Clock = default_clock,
        usage=None,
    ):
        self.db = db
        # The roster, for who can be reviewed and who reviews Gary.
        self.registry = registry
        # Writes the judgment; None means reviews can be read but not written.
        self.reviewer = reviewer
        self.clock = clock
        self.usage = usage

    # ---------------------------------------------------------------- facts

    def scorecard(self, subject: str, kind: str, days: int = REVIEW_PERIOD_DAYS) -> dict:
        """The arithmetic on its own. No model call, so this is free and is
        what a tool returns when Gary just wants the numbers."""
        if kind not in KINDS:
            raise ReviewError(f"kind must be one of {KINDS}")
        now = clock_now(self.clock)
        since = format_utc(to_datetime(now) - dt.timedelta(days=days))
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            if kind == "employee":
                return employee_scorecard(repos, subject, since, now)
            if kind == "principal":
                return principal_scorecard(repos, since, now)
            return manager_scorecard(repos, since, now)

    def kind_of(self, subject: str) -> str:
        if subject == GARY_ACTOR:
            return "manager"
        if subject == USER_ACTOR:
            return "principal"
        return "employee"

    # --------------------------------------------------------------- write

    async def run(
        self,
        subject: str,
        reviewer_id: str = GARY_ACTOR,
        days: int = REVIEW_PERIOD_DAYS,
        requested_by: str = GARY_ACTOR,
    ) -> dict:
        """One review: facts, then judgment, then a stored record."""
        if self.reviewer is None:
            raise ReviewError("No reviewer is configured on this deployment")
        kind = self.kind_of(subject)
        if kind == "manager" and reviewer_id == GARY_ACTOR:
            raise ReviewError("Gary cannot review himself; an employee reviews him")
        if kind != "manager" and reviewer_id != GARY_ACTOR:
            raise ReviewError("Only Gary reviews employees and the principal")

        scorecard = self.scorecard(subject, kind, days)
        if not scorecard.get("enough_to_review", True):
            raise ReviewError(
                f"There is not enough of a record to review {subject}: "
                f"{MIN_ASSIGNMENTS} assignment(s) at least"
            )

        who = self._reviewer_details(reviewer_id)
        judgment = await self.reviewer.write_review(kind, subject, scorecard, who)
        checked = validate_judgment(judgment)

        if self.usage is not None and judgment.get("_usage"):
            from gary.finance.pricing import usage_from_openai

            self.usage.record(
                "performance_review",
                judgment.get("_model") or "",
                usage_from_openai(judgment["_usage"]),
                entity_type="agent",
                entity_id=subject,
                detail=f"{kind} review of {subject}",
            )

        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            stored = repos.performance.create(
                subject=subject,
                subject_kind=kind,
                reviewer=reviewer_id,
                period_start=scorecard["period_start"],
                period_end=scorecard["period_end"],
                scorecard=scorecard,
                model=judgment.get("_model"),
                requested_by=requested_by,
                now=now,
                **checked,
            )
            review = present_review(stored)
            repos.audit.write(
                reviewer_id,
                "performance_review_written",
                f"{reviewer_id} reviewed {subject}",
                "agent",
                subject,
                {
                    "review_id": review["id"],
                    "kind": kind,
                    "concerns": len(checked["concerns"]),
                    "period_days": days,
                },
                now=now,
            )
        return review

    def _reviewer_details(self, reviewer_id: str) -> dict | None:
        if self.registry is None or reviewer_id == GARY_ACTOR:
            return None
        definition = self.registry.get(reviewer_id)
        return {"name": definition.name, "title": definition.title}

    # ----------------------------------------------------------------- due

    def due(
        self,
        days: int = REVIEW_PERIOD_DAYS,
        interval_days: int = REVIEW_INTERVAL_DAYS,
    ) -> dict:
        """Who is due a review, and which employees owe Gary an upward one.

        Due means: enough of a record to say anything, and not reviewed
        within the interval.
        """
        now = clock_now(self.clock)
        cutoff = format_utc(to_datetime(now) - dt.timedelta(days=interval_days))
        since = format_utc(to_datetime(now) - dt.timedelta(days=days))

        employees, upward = [], []
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            reviewed_recently = {
                (row["subject"], row["reviewer"])
                for row in repos.performance.list_since(cutoff, 200)
            }
            definitions = [] if self.registry is None else [
                definition for definition in self.registry.all()
                if definition.agent_id != GARY_ACTOR
            ]
            for definition in definitions:
                agent_id = definition.agent_id
                card = employee_scorecard(repos, agent_id, since, now)
                if not card["enough_to_review"]:
                    continue
                if (agent_id, GARY_ACTOR) not in reviewed_recently:
                    employees.append(agent_id)
                # Someone with a record of their own can review their manager.
                if (GARY_ACTOR, agent_id) not in reviewed_recently:
                    upward.append(agent_id)

            principal = principal_scorecard(repos, since, now)
            alex_due = (
                principal["enough_to_review"]
                and (USER_ACTOR, GARY_ACTOR) not in reviewed_recently
            )
        return {"employees": employees, "upward_reviewers": upward, "principal": alex_due}

    async def run_all_due(
        self,
        days: int = REVIEW_PERIOD_DAYS,
        interval_days: int = REVIEW_INTERVAL_DAYS,
        limit: int = MAX_REVIEWS_PER_RUN,
    ) -> dict:
        """A review round: Gary reviews his people and Alex, and his people
        review him. Capped, because each one is a model call."""
        due = self.due(days, interval_days)
        written, failed = [], []

        def record(subject, reviewer_id):
            return {"subject": subject, "reviewer": reviewer_id}

        planned = [(agent_id, GARY_ACTOR) for agent_id in due["employees"]]
        if due["principal"]:
            planned.append((USER_ACTOR, GARY_ACTOR))
        planned += [(GARY_ACTOR, agent_id) for agent_id in due["upward_reviewers"]]

        for subject, reviewer_id in planned[:limit]:
            try:
                await self.run(subject, reviewer_id, days)
                written.append(record(subject, reviewer_id))
            except Exception as exc:
                failed.append({**record(subject, reviewer_id), "error": str(exc)[:300]})
        return {"written": written, "failed": failed}

    # --------------------------------------------------------------- read

    def get(self, review_id: str) -> dict:
        with self.db.read() as conn:
            review = present_review(Repositories.bind(conn).performance.get(review_id))
        if review is None:
            raise NotFoundError(f"No review with id {review_id}")
        return review

    def list_for(self, subject: str, limit: int = 10) -> list[dict]:
        with self.db.read() as conn:
            rows = Repositories.bind(conn).performance.list_for(subject, limit)
        return [present_review(row) for row in rows]

    def unacknowledged_of_manager(self) -> list[dict]:
        """Reviews of Gary he has not answered yet."""
        with self.db.read() as conn:
            rows = Repositories.bind(conn).performance.list_unacknowledged_of_manager()
        return [present_review(row) for row in rows]

    def acknowledge(self, review_id: str, response: str, actor: str = GARY_ACTOR) -> dict:
        """Gary answering what someone who works for him wrote about him.

        Stored beside the criticism, so neither can be quietly dropped."""
        answer = " ".join(str(response or "").split())[:SUMMARY_LIMIT]
        if len(answer) < 20:
            raise ReviewError("An acknowledgement has to say something")
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            review = repos.performance.get(review_id)
            if review is None:
                raise NotFoundError(f"No review with id {review_id}")
            if review["subject"] != GARY_ACTOR:
                raise ReviewError("Only a review of Gary is acknowledged")
            if review["acknowledged_at"]:
                raise ReviewError("That review has already been answered")
            repos.performance.acknowledge(review_id, answer, now)
            answered = present_review(repos.performance.get(review_id))
            repos.audit.write(
                actor,
                "performance_review_acknowledged",
                f"{actor} answered {review['reviewer']}'s review",
                "agent",
                GARY_ACTOR,
                {"review_id": review_id, "reviewer": review["reviewer"]},
                now=now,
            )
            return answered

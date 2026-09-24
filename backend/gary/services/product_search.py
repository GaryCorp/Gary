"""Looking for a product to build, one round at a time.

"What startup product should we build?" is not a question one assignment
answers. This service asks it repeatedly: each round is an ordinary
assignment to Susan, carrying the ranked slate from the round before and the
questions she said would most change it, and each returns a slate of ideas
scored on four axes. Between rounds Python does three things the model does
not get to do:

* **it ranks.** The weighted score decides the order, so the leading idea is
  arithmetic over her four scores, not the idea she wrote most warmly about;
* **it decides whether to continue.** Susan may say she is ready; a round
  still happens if her open questions are unanswered, and no round happens
  once the ceiling is reached, the search has converged, spending has stopped
  or the company is paused;
* **it remembers.** The slate, the open questions and every round live in
  SQLite, so a restart continues the search instead of beginning it again.

So "keep thinking until you find the best idea" is real, and bounded: the
search runs until the evidence stops moving or the rounds run out, and it
says which of those happened.
"""

import asyncio
import json
import logging
from typing import Callable

from gary.agents.service import AgentService, DelegateRequest
from gary.db import Database
from gary.db.repositories import Repositories
from gary.models.common import validate_request
from gary.models.product import (
    ProductSearchLookup,
    StartProductSearchRequest,
    StopProductSearchRequest,
)
from gary.policy import SYSTEM_ACTOR, USER_ACTOR
from gary.services.common import Clock, NotFoundError, clock_now, default_clock
from gary.timeutil import to_local

logger = logging.getLogger("gary.product_search")

RESEARCHER = "susan"
REPORT_KIND = "product"

# What the ranking is made of. These weights are the company's judgment, not
# the model's: it scores the four axes, this decides what they are worth.
WEIGHTS = {
    "market_score": 0.30,
    "feasibility_score": 0.25,
    "evidence_score": 0.25,
    "differentiation_score": 0.20,
}
# A leading idea at or above this, with nothing left to check, is good enough
# to stop for.
GOOD_ENOUGH = 7.5
# Two rounds with the same leader and less than this much improvement means
# further rounds are buying nothing.
CONVERGED_GAIN = 0.3
# How many ideas travel into the next round's brief.
SHORTLIST_LIMIT = 5
# How many open questions are carried forward.
QUESTION_LIMIT = 5
# Two failed rounds in a row end the search rather than burning the ceiling.
MAX_CONSECUTIVE_FAILURES = 2
# A round that could not even be delegated is retried, but not for ever: a
# search gives up once it has claimed this many rounds per round it is allowed.
MAX_ROUND_ATTEMPTS = 2

ROUND_ONE_BRIEF = """Find the best startup product for GaryCorp to build.

What Alex is looking for: {brief}

This is round 1 of at most {max_rounds}. Come back with the strongest ideas you
can evidence, not the most ideas: between three and six, each one a product a
very small team could ship, with a named customer, the problem as they
experience it today, and the smallest version worth paying for.

Go and find out what is actually true: who already does this, what it costs
them, what the market looks like now, and what would make each idea fail.
Score every idea on the four axes honestly, drop anything the evidence kills,
and list the questions that would most change your ranking."""

LATER_ROUND_BRIEF = """Continue the search for the best startup product for GaryCorp.

What Alex is looking for: {brief}

This is round {round_number} of at most {max_rounds}. The slate below is where
the search stands, ranked by GaryCorp's own scoring of your last report, with
the questions you said would most change it.

Your job this round is to answer those questions, not to start again. Use your
searches on them. Then: kill any idea the evidence rules out and say so, keep
the ideas that survive with their scores updated to what you now know, and add
a new idea only if it is better than what is already here.

Re-score every idea you return. If the evidence is now good enough to
recommend one, say so and leave open_questions empty; if it is not, say
exactly what you would find out next."""


def score_idea(idea: dict) -> float:
    """One number from the four the model gave, to two decimal places."""
    return round(sum(float(idea.get(field, 0)) * weight for field, weight in WEIGHTS.items()), 2)


def rank_ideas(ideas: list[dict]) -> list[dict]:
    """The slate, best first. Ties keep the order Susan returned them in."""
    ranked = [
        {
            "name": idea["name"],
            "score": score_idea(idea),
            "customer": idea["customer"],
            "wedge": idea["wedge"],
            "what_would_kill_it": idea["what_would_kill_it"],
            "next_validation_step": idea["next_validation_step"],
            "scores": {field: idea[field] for field in WEIGHTS},
        }
        for idea in ideas
    ]
    return sorted(ranked, key=lambda row: -row["score"])


def decide(search: dict, report: dict, ranked: list[dict]) -> tuple[bool, str]:
    """Whether the search has finished, and why. Pure: the caller has already
    counted the round that just ended into ``rounds_completed``."""
    leader = ranked[0] if ranked else None
    questions = report.get("open_questions") or []

    if leader is None:
        return True, "the last round returned no ideas"
    if search["rounds_completed"] >= search["max_rounds"]:
        return True, f"the limit of {search['max_rounds']} rounds was reached"
    if not questions:
        if report.get("ready_to_recommend") and leader["score"] >= GOOD_ENOUGH:
            return True, f"Susan is ready to recommend {leader['name']} and had nothing left to check"
        return True, "Susan had no further questions worth spending a round on"
    if (
        search["rounds_completed"] >= 2
        and search["best_idea"]
        and leader["name"].casefold() == search["best_idea"].casefold()
        and leader["score"] - (search["best_score"] or 0) < CONVERGED_GAIN
    ):
        return True, f"{leader['name']} led two rounds running and stopped improving"
    return False, ""


class ProductSearchError(ValueError):
    pass


class ProductSearchService:
    def __init__(
        self,
        db: Database,
        agents: AgentService,
        timezone=None,
        clock: Clock = default_clock,
        default_max_rounds: int = 5,
        paused: Callable[[], bool] | None = None,
        spending_allowed: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.agents = agents
        self.timezone = timezone or agents.gary.timezone
        self.clock = clock
        self.default_max_rounds = default_max_rounds
        # Both default to "go ahead": a deployment without a pause switch or a
        # spend ceiling still works, and neither is ever assumed from a model.
        self._paused = paused or (lambda: False)
        self._spending_allowed = spending_allowed or (lambda: True)

    # ------------------------------------------------------------- starting

    async def start(self, arguments: dict, started_by: str = "gary") -> dict:
        request = validate_request(StartProductSearchRequest, arguments)
        search = await asyncio.to_thread(self._create, request, started_by)
        await self.advance(search["id"])
        return await asyncio.to_thread(self.get, search["id"])

    def _create(self, request: StartProductSearchRequest, started_by: str) -> dict:
        now = clock_now(self.clock)
        max_rounds = request.max_rounds or self.default_max_rounds
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            active = repos.product_search.active()
            if active is not None:
                raise ProductSearchError(
                    f"A product search is already running: {active['brief'][:120]}. "
                    "Wait for it to finish, or stop it first."
                )
            search = repos.product_search.create(
                brief=request.brief,
                max_rounds=max_rounds,
                started_by=started_by,
                constraints=request.constraints,
                now=now,
            )
            repos.audit.write(
                started_by, "product_search_started",
                f"Product search started: {request.brief[:120]}",
                "product_search", search["id"],
                {"max_rounds": max_rounds, "constraints": request.constraints}, now=now,
            )
        return search

    # -------------------------------------------------------------- driving

    async def advance_all(self) -> list[dict]:
        """Move every running search forward. Returns the searches that
        finished on this pass, for whoever announces them."""
        searches = await asyncio.to_thread(self._active_ids)
        finished = []
        for search_id in searches:
            try:
                result = await self.advance(search_id)
            except Exception:
                logger.exception("Product search %s could not be advanced", search_id)
                continue
            if result is not None:
                finished.append(result)
        return finished

    def _active_ids(self) -> list[str]:
        with self.db.read() as conn:
            return [row["id"] for row in Repositories.bind(conn).product_search.list_active()]

    async def advance(self, search_id: str) -> dict | None:
        """One step: take in a finished round, decide, and start the next one.
        Returns the finished search when this step ended it, else None."""
        step = await asyncio.to_thread(self._step, search_id)
        action = step["action"]

        if action == "restart_assignment":
            # Left queued by the spend ceiling; it can run again now.
            self.agents.restart_queued(step["assignment_id"])
            return None
        if action == "delegate":
            await self._delegate_round(search_id, step["round_id"], step["brief"])
            return None
        if action == "finished":
            return step["search"]
        return None

    def _step(self, search_id: str) -> dict:
        """Everything about one step that touches the database, in one
        transaction: read the state, take in a finished round, and say what
        should happen next."""
        nothing = {"action": "none"}
        now = clock_now(self.clock)
        # Both read the database themselves, so they are asked before the
        # write transaction is opened rather than from inside it.
        can_spend, paused = self._can_spend(), self._paused_now()
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            search = repos.product_search.get(search_id)
            if search is None or search["status"] != "running":
                return nothing

            current = repos.product_search.open_round(search["id"])
            if current is not None:
                assignment = (
                    repos.assignments.get(current["assignment_id"])
                    if current["assignment_id"] else None
                )
                if assignment is None:
                    # Claimed, then the process stopped before the assignment
                    # existed. Nothing ran, so the claim is abandoned and the
                    # next pass claims a fresh round.
                    repos.product_search.update_round(current["id"], status="abandoned", completed_at=now)
                    return nothing
                if assignment["status"] == "queued":
                    return (
                        {"action": "restart_assignment", "assignment_id": assignment["id"]}
                        if can_spend
                        else nothing
                    )
                if assignment["status"] == "running":
                    return nothing
                if assignment["status"] == "completed":
                    search = self._take_in_round(repos, search, current, assignment, now)
                else:
                    search = self._fail_round(repos, search, current, assignment, now)
                if search["status"] != "running":
                    return {"action": "finished", "search": self._present(repos, search)}

            if not can_spend or paused:
                return nothing
            if search["rounds_completed"] >= search["max_rounds"]:
                # Reached without a round to take in (a failure, say): end it
                # here rather than leaving it running for ever.
                self._finish(repos, search, "completed",
                             f"the limit of {search['max_rounds']} rounds was reached", now)
                return {"action": "finished",
                        "search": self._present(repos, repos.product_search.get(search["id"]))}

            # One past the highest round ever claimed, so a failed round is
            # never re-used and UNIQUE (search_id, round_number) holds.
            claimed_rounds = repos.product_search.list_rounds(search["id"])
            if len(claimed_rounds) >= search["max_rounds"] * MAX_ROUND_ATTEMPTS:
                self._finish(repos, search, "failed",
                             "too many rounds could not be completed", now)
                return {"action": "finished",
                        "search": self._present(repos, repos.product_search.get(search["id"]))}
            round_number = max((row["round_number"] for row in claimed_rounds), default=0) + 1
            brief = self._brief(search, round_number)
            claimed = repos.product_search.start_round(search["id"], round_number, brief, now)
            return {"action": "delegate", "round_id": claimed["id"], "brief": brief}

    async def _delegate_round(self, search_id: str, round_id: str, brief: str) -> None:
        context = await asyncio.to_thread(self._round_context, search_id)
        try:
            assignment = await self.agents.delegate(
                DelegateRequest(
                    agent_id=RESEARCHER,
                    objective=brief[:2000],
                    context=context,
                    priority=4,
                    report_kind=REPORT_KIND,
                ),
                assigned_by="gary",
            )
        except Exception as exc:
            logger.warning("Product search round could not be delegated: %s", exc)
            await asyncio.to_thread(self._abandon_round, round_id, str(exc))
            return
        await asyncio.to_thread(self._attach_assignment, search_id, round_id, assignment["id"])

    def _attach_assignment(self, search_id: str, round_id: str, assignment_id: str) -> None:
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            round_row = repos.product_search.update_round(round_id, assignment_id=assignment_id)
            repos.audit.write(
                "gary", "product_search_round_started",
                f"Product search round {round_row['round_number']} started",
                "product_search", search_id,
                {"round": round_row["round_number"], "assignment_id": assignment_id}, now=now,
            )

    def _abandon_round(self, round_id: str, error: str) -> None:
        """A round that could not be delegated at all (the team is full, say)
        is abandoned rather than failed: the next pass claims a new one, and
        only rounds Susan actually ran count towards the failure ceiling."""
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            round_row = repos.product_search.update_round(
                round_id, status="abandoned", completed_at=now
            )
            repos.audit.write(
                SYSTEM_ACTOR, "product_search_round_not_started",
                f"Product search round {round_row['round_number']} could not start: {error[:160]}",
                "product_search", round_row["search_id"],
                {"round": round_row["round_number"], "error": error[:500]}, now=now,
            )

    # ------------------------------------------------------ taking a round in

    def _take_in_round(self, repos: Repositories, search: dict, round_row: dict,
                       assignment: dict, now: str) -> dict:
        report = json.loads(assignment["result_json"] or "{}")
        ranked = rank_ideas(report.get("ideas") or [])
        leader = ranked[0] if ranked else None

        repos.product_search.update_round(
            round_row["id"], status="completed", completed_at=now,
            ideas_considered=len(ranked),
            top_idea=leader["name"] if leader else None,
            top_score=leader["score"] if leader else None,
        )
        questions = (report.get("open_questions") or [])[:QUESTION_LIMIT]
        rounds_completed = search["rounds_completed"] + 1
        stopped, reason = decide({**search, "rounds_completed": rounds_completed}, report, ranked)

        repos.audit.write(
            RESEARCHER, "product_search_round_completed",
            f"Product search round {round_row['round_number']}: "
            f"{len(ranked)} ideas, leading {leader['name'] if leader else 'none'}",
            "product_search", search["id"],
            {
                "round": round_row["round_number"],
                "assignment_id": assignment["id"],
                "ideas": len(ranked),
                "top_idea": leader["name"] if leader else None,
                "top_score": leader["score"] if leader else None,
                "open_questions": len(questions),
                "ready_to_recommend": bool(report.get("ready_to_recommend")),
            },
            now=now,
        )

        search = repos.product_search.update(
            search["id"],
            rounds_completed=rounds_completed,
            shortlist_json=json.dumps(ranked, sort_keys=True),
            open_questions_json=json.dumps(questions),
            best_idea=leader["name"] if leader else search["best_idea"],
            best_score=leader["score"] if leader else search["best_score"],
            updated_at=now,
        )
        if stopped:
            self._finish(repos, search, "completed", reason, now)
            return repos.product_search.get(search["id"])
        return search

    def _fail_round(self, repos: Repositories, search: dict, round_row: dict,
                    assignment: dict, now: str) -> dict:
        repos.product_search.update_round(round_row["id"], status="failed", completed_at=now)
        repos.audit.write(
            SYSTEM_ACTOR, "product_search_round_failed",
            f"Product search round {round_row['round_number']} did not complete",
            "product_search", search["id"],
            {"round": round_row["round_number"], "assignment_id": assignment["id"],
             "error": (assignment["error_message"] or "")[:500]},
            now=now,
        )
        rounds = repos.product_search.list_rounds(search["id"])
        trailing = 0
        for row in reversed(rounds):
            if row["status"] == "abandoned":
                # Never ran, so it neither breaks nor extends a run of failures.
                continue
            if row["status"] != "failed":
                break
            trailing += 1
        if trailing >= MAX_CONSECUTIVE_FAILURES:
            self._finish(repos, search, "failed",
                         f"{trailing} rounds in a row did not complete", now)
            return repos.product_search.get(search["id"])
        return search

    def _finish(self, repos: Repositories, search: dict, status: str, reason: str, now: str) -> None:
        if not repos.product_search.finish(search["id"], status, reason, now):
            return
        repos.audit.write(
            SYSTEM_ACTOR if status != "stopped" else USER_ACTOR,
            f"product_search_{status}",
            f"Product search {status}: {reason}",
            "product_search", search["id"],
            {"rounds": search["rounds_completed"], "best_idea": search["best_idea"],
             "best_score": search["best_score"], "reason": reason},
            now=now,
        )

    # -------------------------------------------------------------- stopping

    def stop(self, arguments: dict, stopped_by: str = USER_ACTOR) -> dict:
        request = validate_request(StopProductSearchRequest, arguments)
        now = clock_now(self.clock)
        with self.db.transaction() as conn:
            repos = Repositories.bind(conn)
            search = (
                repos.product_search.get(request.search_id) if request.search_id
                else repos.product_search.active() or repos.product_search.latest()
            )
            if search is None:
                raise NotFoundError("No product search has been started yet")
            if search["status"] != "running":
                raise ProductSearchError(f"That product search already {search['status']}")
            reason = request.reason.strip() or f"stopped by {stopped_by}"
            self._finish(repos, search, "stopped", reason, now)
            return self._present(repos, repos.product_search.get(search["id"]))

    # --------------------------------------------------------------- reading

    def get(self, search_id: str | None = None) -> dict:
        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            search = repos.product_search.get(search_id) if search_id else repos.product_search.latest()
            if search is None:
                raise NotFoundError("No product search has been started yet")
            return self._present(repos, search)

    def status(self, arguments: dict | None = None) -> dict:
        request = validate_request(ProductSearchLookup, arguments or {})
        return self.get(request.search_id)

    def _present(self, repos: Repositories, search: dict) -> dict:
        shortlist = json.loads(search["shortlist_json"] or "[]")
        rounds = repos.product_search.list_rounds(search["id"])
        return {
            "search_id": search["id"],
            "brief": search["brief"],
            "constraints": json.loads(search["constraints_json"] or "null"),
            "status": search["status"],
            "rounds_completed": search["rounds_completed"],
            "max_rounds": search["max_rounds"],
            "best_idea": search["best_idea"],
            "best_score": search["best_score"],
            "shortlist": shortlist,
            "open_questions": json.loads(search["open_questions_json"] or "[]"),
            "stop_reason": search["stop_reason"],
            "started_at": to_local(search["created_at"], self.timezone),
            "completed_at": to_local(search["completed_at"], self.timezone)
            if search["completed_at"] else None,
            "rounds": [
                {
                    "round": row["round_number"],
                    "status": row["status"],
                    "assignment_id": row["assignment_id"],
                    "ideas_considered": row["ideas_considered"],
                    "top_idea": row["top_idea"],
                    "top_score": row["top_score"],
                }
                for row in rounds
            ],
            "note": (
                "Scores are GaryCorp's weighted ranking of Susan's four scores, not hers. "
                "Each round is one of her assignments: the full ideas are in its report."
            ),
        }

    # --------------------------------------------------------------- helpers

    def _can_spend(self) -> bool:
        try:
            return bool(self._spending_allowed())
        except Exception:
            logger.exception("The spend gate could not be read; not starting a round")
            return False

    def _paused_now(self) -> bool:
        try:
            return bool(self._paused())
        except Exception:
            logger.exception("The pause switch could not be read; not starting a round")
            return True

    def _brief(self, search: dict, round_number: int) -> str:
        template = ROUND_ONE_BRIEF if round_number == 1 else LATER_ROUND_BRIEF
        return template.format(
            brief=search["brief"], round_number=round_number, max_rounds=search["max_rounds"]
        )

    def _round_context(self, search_id: str) -> dict:
        """What the next round is given: the constraints, the slate so far and
        the open questions. Short enough to travel in an assignment."""
        with self.db.read() as conn:
            search = Repositories.bind(conn).product_search.get(search_id)
        if search is None:
            return {}
        context = {"round": f"{search['rounds_completed'] + 1} of at most {search['max_rounds']}"}
        constraints = json.loads(search["constraints_json"] or "null")
        if constraints:
            context["constraints"] = json.dumps(constraints, sort_keys=True)
        shortlist = json.loads(search["shortlist_json"] or "[]")[:SHORTLIST_LIMIT]
        if shortlist:
            context["slate_so_far_ranked"] = json.dumps(
                [
                    {
                        "name": idea["name"],
                        "score": idea["score"],
                        "customer": idea["customer"][:200],
                        "wedge": idea["wedge"][:300],
                        "what_would_kill_it": idea["what_would_kill_it"][:300],
                        "scores": idea["scores"],
                    }
                    for idea in shortlist
                ]
            )
        questions = json.loads(search["open_questions_json"] or "[]")
        if questions:
            context["questions_to_answer_this_round"] = " | ".join(q[:300] for q in questions)
        return context

"""GaryCorp looking for a product to build.

The search is a loop Python drives: Susan researches a round, Python ranks
what came back and decides whether another round would add anything. These
tests are about the deciding, because that is the part a model must not do:
the ranking is arithmetic, the rounds are capped, a paused or broke company
starts none, and a restart continues the search instead of restarting it.
"""

import asyncio
import json

import pytest

from gary.agents.models import ProductReport
from gary.agents.roster import AgentLimits, AgentRegistry
from gary.agents.service import DelegateRequest
from gary.db.repositories import Repositories
from gary.services.common import NotFoundError
from gary.services.product_search import (
    GOOD_ENOUGH,
    ProductSearchError,
    ProductSearchService,
    decide,
    rank_ideas,
    score_idea,
)
from gary.tools import call_tool
from gary.tools.base import ToolContext

from test_agents import FakeExecutor, build_team, run


def idea(name, market=8, feasibility=8, evidence=8, differentiation=8, **extra):
    return {
        "name": name,
        "customer": f"Freelance video editors who need {name}",
        "problem": "They do it by hand every week and hate it.",
        "wedge": "A single-purpose tool that does the worst step.",
        "why_now": "The models only became good enough this year.",
        "evidence": ["Three forum threads asking for exactly this (source: example.com)"],
        "sources": ["https://example.com/thread"],
        "competitors": ["A big suite that does it badly"],
        "risks": ["The platforms could add it themselves"],
        "what_would_kill_it": "The incumbent shipping the same feature free.",
        "next_validation_step": "Post a landing page and count sign-ups.",
        "market_score": market,
        "feasibility_score": feasibility,
        "evidence_score": evidence,
        "differentiation_score": differentiation,
        **extra,
    }


def report(*ideas, questions=("What do they pay today?",), ready=False, recommended=None, summary="A slate."):
    return {
        "summary": summary,
        "ideas": list(ideas),
        "dropped": ["Another idea, killed by an incumbent"],
        "open_questions": list(questions),
        "ready_to_recommend": ready,
        "recommended_idea": recommended,
        "confidence": 0.6,
    }


def build_search(gary, executor=None, max_rounds=3, paused=None, spending_allowed=None, team=None):
    if team is None:
        team = build_team(gary, executor or FakeExecutor())
    elif executor is not None:
        # Reusing a team with a fresh Susan, which is what the next process
        # after a restart amounts to.
        team.runner.executor = executor
    service = ProductSearchService(
        gary.db,
        team,
        timezone=gary.timezone,
        default_max_rounds=max_rounds,
        paused=paused,
        spending_allowed=spending_allowed,
    )
    return service, team


def scripted(*reports):
    """A Susan who returns each of these reports in turn."""
    return FakeExecutor({"susan": list(reports)})


async def drive(service, team, passes=8):
    """Let the search run: in the server a finished assignment advances the
    search, and here each pass does the same thing by hand."""
    for _ in range(passes):
        finished = await service.advance_all()
        if finished:
            return finished
        await team.wait([a["id"] for a in _active_assignments(team)], timeout=20)
    return []


async def search_and_drive(service, team, brief="Something for video people.", passes=8, **extra):
    """Start a search and run it to whatever end it reaches."""
    await service.start({"brief": brief, **extra})
    return await drive(service, team, passes)


def _active_assignments(team):
    with team.gary.db.read() as conn:
        return Repositories.bind(conn).assignments.list_by_status(("queued", "running"))


# ------------------------------------------------------------------ ranking

def test_python_ranks_the_ideas_not_the_model():
    """The leader is arithmetic over Susan's four scores, so an idea cannot
    win by being written about warmly."""
    weak_but_certain = idea("Caption cleaner", market=4, feasibility=9, evidence=9, differentiation=4)
    big_but_vague = idea("AI studio", market=10, feasibility=3, evidence=2, differentiation=9)

    assert score_idea(weak_but_certain) == pytest.approx(4 * 0.3 + 9 * 0.25 + 9 * 0.25 + 4 * 0.2)
    ranked = rank_ideas([big_but_vague, weak_but_certain])
    assert [row["name"] for row in ranked] == ["Caption cleaner", "AI studio"]
    assert ranked[0]["scores"] == {
        "market_score": 4, "feasibility_score": 9, "evidence_score": 9, "differentiation_score": 4,
    }


# ------------------------------------------------------- the stopping rule

def search_row(rounds_completed=1, max_rounds=3, best_idea=None, best_score=None):
    return {
        "rounds_completed": rounds_completed,
        "max_rounds": max_rounds,
        "best_idea": best_idea,
        "best_score": best_score,
    }


def test_open_questions_buy_another_round():
    ranked = rank_ideas([idea("Caption cleaner")])
    stop, reason = decide(search_row(), report(idea("Caption cleaner")), ranked)
    assert (stop, reason) == (False, "")


def test_being_ready_is_not_enough_while_questions_are_open():
    """Susan saying she is ready does not end the search: Python does, and
    only once there is nothing left to check."""
    ideas = [idea("Caption cleaner", market=10, feasibility=10, evidence=10, differentiation=10)]
    stop, _ = decide(search_row(), report(*ideas, ready=True, recommended="Caption cleaner"), rank_ideas(ideas))
    assert stop is False


def test_it_stops_when_she_is_ready_and_has_nothing_left_to_check():
    ideas = [idea("Caption cleaner", market=9, feasibility=9, evidence=8, differentiation=8)]
    ranked = rank_ideas(ideas)
    assert ranked[0]["score"] >= GOOD_ENOUGH
    stop, reason = decide(
        search_row(), report(*ideas, questions=(), ready=True, recommended="Caption cleaner"), ranked
    )
    assert stop is True and "ready to recommend Caption cleaner" in reason


def test_no_questions_and_a_weak_leader_still_stops():
    ideas = [idea("Caption cleaner", market=3, feasibility=4, evidence=3, differentiation=3)]
    stop, reason = decide(search_row(), report(*ideas, questions=()), rank_ideas(ideas))
    assert stop is True and "no further questions" in reason


def test_the_round_limit_is_the_outer_bound():
    ideas = [idea("Caption cleaner")]
    stop, reason = decide(
        search_row(rounds_completed=3, max_rounds=3), report(*ideas), rank_ideas(ideas)
    )
    assert stop is True and "limit of 3 rounds" in reason


def test_a_leader_that_stops_improving_is_converged():
    ideas = [idea("Caption cleaner", market=8, feasibility=8, evidence=8, differentiation=8)]
    ranked = rank_ideas(ideas)
    stop, reason = decide(
        search_row(rounds_completed=2, best_idea="caption cleaner", best_score=ranked[0]["score"] - 0.1),
        report(*ideas),
        ranked,
    )
    assert stop is True and "stopped improving" in reason

    # A real improvement is worth another round.
    keeps_going, _ = decide(
        search_row(rounds_completed=2, best_idea="Caption cleaner", best_score=ranked[0]["score"] - 2),
        report(*ideas),
        ranked,
    )
    assert keeps_going is False


# ------------------------------------------------------------- the whole loop

def test_a_search_runs_rounds_until_the_evidence_settles(gary):
    """Two rounds: the first leaves questions, the second answers them and
    ends the search on its own."""
    first = report(idea("Caption cleaner", market=7, feasibility=8, evidence=6, differentiation=7),
                   idea("Invoice chaser", market=5, feasibility=9, evidence=5, differentiation=4))
    second = report(idea("Caption cleaner", market=9, feasibility=9, evidence=8, differentiation=8),
                    questions=(), ready=True, recommended="Caption cleaner")
    service, team = build_search(gary, scripted(first, second))

    async def scenario():
        result = await service.start({"brief": "Something for video people, built by one person."})
        assert result["status"] == "running" and result["rounds_completed"] == 0
        return await drive(service, team)

    finished = run(scenario())
    assert [s["status"] for s in finished] == ["completed"]

    search = service.get()
    assert search["status"] == "completed"
    assert search["rounds_completed"] == 2
    assert search["best_idea"] == "Caption cleaner"
    assert "ready to recommend" in search["stop_reason"]
    assert [row["round"] for row in search["rounds"]] == [1, 2]
    assert [row["status"] for row in search["rounds"]] == ["completed", "completed"]
    # The whole slate is kept, ranked.
    assert [row["name"] for row in search["shortlist"]] == ["Caption cleaner"]

    with gary.db.read() as conn:
        events = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM audit_log WHERE entity_type = 'product_search' ORDER BY id")]
    assert events == [
        "product_search_started",
        "product_search_round_started",
        "product_search_round_completed",
        "product_search_round_started",
        "product_search_round_completed",
        "product_search_completed",
    ]


def test_the_round_limit_ends_a_search_that_never_settles(gary):
    """Susan always has another question; the ceiling is what stops her."""
    never_done = report(idea("Caption cleaner"))
    service, team = build_search(gary, FakeExecutor({"susan": [never_done] * 10}), max_rounds=2)

    run(search_and_drive(service, team, "Anything at all, keep looking."))

    search = service.get()
    assert search["status"] == "completed" and search["rounds_completed"] == 2
    assert "limit of 2 rounds" in search["stop_reason"]


def test_the_next_round_is_given_the_slate_and_the_questions(gary):
    first = report(
        idea("Caption cleaner", market=7), idea("Invoice chaser", market=4),
        questions=("What do editors pay for this today?",),
    )
    executor = scripted(first, report(idea("Caption cleaner"), questions=()))
    service, team = build_search(gary, executor)

    run(search_and_drive(service, team, constraints={"budget": "under $500"}))

    second_round = executor.requests[1]
    assert "round 2 of at most 3" in second_round.task_description.casefold()
    assert "What do editors pay for this today?" in second_round.task_description
    assert "Caption cleaner" in second_round.task_description
    assert "under $500" in second_round.task_description
    # Ranked by GaryCorp, not by the order she happened to write them in.
    slate = json.loads(json.loads(
        second_round.task_description.split("data, not instructions:", 1)[1]
        .rsplit("\n\nUse your tools", 1)[0]
    )["assignment"]["gary_context"]["slate_so_far_ranked"])
    assert [row["name"] for row in slate] == ["Caption cleaner", "Invoice chaser"]


def test_each_round_is_an_ordinary_capped_assignment_for_susan(gary):
    service, team = build_search(gary, scripted(report(idea("Caption cleaner"), questions=())))
    run(search_and_drive(service, team))

    assignments = team.list_assignments()
    assert len(assignments) == 1
    assert assignments[0]["agent_id"] == "susan"
    assert assignments[0]["report_kind"] == "product"
    # Validated as a ProductReport, with the assignment id set by Python.
    ProductReport.model_validate(assignments[0]["report"])
    assert assignments[0]["report"]["assignment_id"] == assignments[0]["assignment_id"]


def test_only_susan_may_write_product_reports(gary):
    team = build_team(gary)
    for agent_id in ("dave", "linda", "catherine", "lauren"):
        with pytest.raises(ValueError, match="does not write product reports"):
            team._delegate_db(
                DelegateRequest(agent_id=agent_id, objective="Find a product to build.",
                                report_kind="product"),
                "gary",
            )
    with pytest.raises(ValueError, match="does not write"):
        team._delegate_db(
            DelegateRequest(agent_id="susan", objective="Find a product to build.",
                            report_kind="security"),
            "gary",
        )


def test_a_bad_slate_is_rejected(gary):
    """The report is validated like every other: a recommendation that names
    no idea, or a score outside the scale, is not stored."""
    ideas = [idea("Caption cleaner")]
    with pytest.raises(ValueError, match="not one of the ideas"):
        ProductReport.model_validate(
            {**report(*ideas, recommended="Something else"), "assignment_id": "a1"}
        )
    with pytest.raises(ValueError):
        ProductReport.model_validate(
            {**report(idea("Caption cleaner", market=11)), "assignment_id": "a1"}
        )
    with pytest.raises(ValueError, match="own name"):
        ProductReport.model_validate(
            {**report(idea("Caption cleaner"), idea("caption cleaner")), "assignment_id": "a1"}
        )
    with pytest.raises(ValueError):
        ProductReport.model_validate({**report(), "assignment_id": "a1"})


def test_a_failed_round_is_retried_and_two_end_the_search(gary):
    executor = FakeExecutor({"susan": [RuntimeError("model provider unavailable")] * 2})
    service, team = build_search(gary, executor)

    run(search_and_drive(service, team))

    search = service.get()
    assert search["status"] == "failed"
    assert [row["status"] for row in search["rounds"]] == ["failed", "failed"]
    assert "did not complete" in search["stop_reason"]


def test_a_round_that_could_not_start_is_retried_not_counted_as_failed(gary):
    """A full team is not a failed round: the claim is given back and the
    next pass tries again, rather than burning one of the search's rounds."""
    service, team = build_search(gary, scripted(report(idea("Caption cleaner"), questions=())),
                                 max_rounds=2)

    async def scenario():
        # Every seat taken, so the round cannot be delegated.
        for _ in range(team.registry.limits.max_active_assignments):
            team._delegate_db(DelegateRequest(agent_id="dave", objective="Threat-model the page."), "gary")
        await service.start({"brief": "Something for video people."})
        assert [row["status"] for row in service.get()["rounds"]] == ["abandoned"]

        with gary.db.transaction() as conn:
            conn.execute("UPDATE agent_assignments SET status = 'cancelled' WHERE assigned_to = 'dave'")
        await drive(service, team)

    run(scenario())
    search = service.get()
    assert search["status"] == "completed"
    # The abandoned claim is kept for the trail; the round that ran is the next one.
    assert [(row["round"], row["status"]) for row in search["rounds"]] == [
        (1, "abandoned"), (2, "completed")]
    assert search["rounds_completed"] == 1

    with gary.db.read() as conn:
        events = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM audit_log WHERE entity_type = 'product_search' ORDER BY id")]
    assert "product_search_round_not_started" in events


def test_a_search_that_can_never_start_a_round_gives_up(gary):
    service, team = build_search(gary, FakeExecutor(), max_rounds=1)

    async def scenario():
        for _ in range(team.registry.limits.max_active_assignments):
            team._delegate_db(DelegateRequest(agent_id="dave", objective="Threat-model the page."), "gary")
        await service.start({"brief": "Something for video people."})
        for _ in range(4):
            await service.advance_all()

    run(scenario())
    search = service.get()
    assert search["status"] == "failed"
    assert "could not be completed" in search["stop_reason"]


def test_a_paused_company_starts_no_round(gary):
    paused = {"value": True}
    service, team = build_search(gary, scripted(report(idea("Caption cleaner"), questions=())),
                                 paused=lambda: paused["value"])

    async def scenario():
        await service.start({"brief": "Something for video people."})
        assert service.get()["rounds"] == []
        paused["value"] = False
        await drive(service, team)

    run(scenario())
    assert service.get()["status"] == "completed"


def test_the_spend_ceiling_starts_no_round(gary):
    allowed = {"value": False}
    service, team = build_search(gary, scripted(report(idea("Caption cleaner"), questions=())),
                                 spending_allowed=lambda: allowed["value"])

    async def scenario():
        await service.start({"brief": "Something for video people."})
        assert service.get()["rounds"] == []
        allowed["value"] = True
        await drive(service, team)

    run(scenario())
    assert service.get()["rounds_completed"] == 1


def test_a_restart_continues_the_search_it_did_not_finish(gary):
    """The rounds live in SQLite, so a new process picks the search up where
    it was instead of starting again."""
    first = report(idea("Caption cleaner", market=7))
    service, team = build_search(gary, scripted(first))

    async def first_process():
        """Starts the search and gets round one's report back, then stops
        before anything takes it in."""
        await service.start({"brief": "Something for video people."})
        await team.wait([a["id"] for a in _active_assignments(team)], timeout=20)

    run(first_process())
    assert service.get()["rounds_completed"] == 0
    assert [row["status"] for row in service.get()["rounds"]] == ["running"]

    # A new service over the same database, as after a restart.
    later, later_team = build_search(
        gary, scripted(report(idea("Caption cleaner", market=9), questions=())), team=team
    )
    run(drive(later, later_team))

    search = later.get()
    assert search["status"] == "completed"
    assert search["rounds_completed"] == 2
    assert [row["round"] for row in search["rounds"]] == [1, 2]


def test_one_search_at_a_time(gary):
    service, _ = build_search(gary, scripted(report(idea("Caption cleaner"))))

    async def scenario():
        await service.start({"brief": "Something for video people."})
        with pytest.raises(ProductSearchError, match="already running"):
            await service.start({"brief": "Something else entirely."})

    run(scenario())


def test_alex_can_stop_a_search_and_keep_what_it_found(gary):
    service, team = build_search(gary, scripted(report(idea("Caption cleaner", market=7))))
    run(search_and_drive(service, team, passes=2))

    stopped = service.stop({"reason": "I have decided already"})
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "I have decided already"
    assert stopped["best_idea"] == "Caption cleaner"

    # Nothing more runs, and stopping twice is refused rather than pretended.
    assert run(service.advance_all()) == []
    with pytest.raises(ProductSearchError, match="already stopped"):
        service.stop({})


def test_reading_a_search_before_one_exists(gary):
    service, _ = build_search(gary, FakeExecutor())
    with pytest.raises(NotFoundError, match="No product search"):
        service.get()


# ------------------------------------------------------------------- tools

def test_garys_tools_start_and_read_the_search(gary):
    service, team = build_search(gary, scripted(
        report(idea("Caption cleaner", market=9, feasibility=9, evidence=8, differentiation=8),
               questions=(), ready=True, recommended="Caption cleaner")
    ))
    ctx = ToolContext(gary, {}, {"product_search": service})

    async def scenario():
        started = await call_tool("product_search_start", {"brief": "Something for video people."}, ctx)
        assert started["success"] is True
        assert started["search"]["status"] == "running"
        await drive(service, team)

    run(scenario())

    status = run(call_tool("product_search_status", {}, ctx))
    assert status["search"]["best_idea"] == "Caption cleaner"
    assert status["search"]["rounds"] == "1 of at most 3"
    assert status["search"]["leading_ideas"][0]["for"].startswith("Freelance video editors")

    full = run(call_tool("product_search_get", {}, ctx))
    assert full["search"]["shortlist"][0]["next_validation_step"].startswith("Post a landing page")

    # Stopping a finished search says so rather than pretending.
    stopped = run(call_tool("product_search_stop", {}, ctx))
    assert stopped["success"] is False and "already completed" in stopped["error"]


def test_without_the_service_the_tools_say_so(gary):
    result = run(call_tool("product_search_status", {}, ToolContext(gary, {})))
    assert result == {"success": False,
                      "error": "The product_search integration is not available right now"}

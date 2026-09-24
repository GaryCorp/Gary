"""Gary's tools for GaryCorp's search for a product to build.

Starting one commissions repeated background work, so it is capped like
everything else: one search at a time, a fixed ceiling of rounds, and each
round is an ordinary assignment to Susan under the usual limits. Gary starts
and reads a search; the rounds themselves are driven by Python
(gary/services/product_search.py), never by a model deciding to go again.
"""

from gary.models.product import ProductSearchLookup
from gary.models.common import validate_request
from gary.tools.base import Tool, ToolContext, integer, obj, run_sync, string

SPOKEN_IDEAS = 3


def _service(ctx: ToolContext):
    return ctx.integration("product_search")


def _spoken(search: dict) -> dict:
    """What Gary says out loud: the leader, the case for it, and where the
    search is. The whole slate is in product_search_get."""
    top = search["shortlist"][:SPOKEN_IDEAS]
    return {
        "search_id": search["search_id"],
        "status": search["status"],
        "rounds": f"{search['rounds_completed']} of at most {search['max_rounds']}",
        "best_idea": search["best_idea"],
        "best_score": search["best_score"],
        "leading_ideas": [
            {"name": idea["name"], "score": idea["score"], "for": idea["customer"],
             "wedge": idea["wedge"]}
            for idea in top
        ],
        "still_to_find_out": search["open_questions"],
        "stop_reason": search["stop_reason"],
        "note": search["note"],
    }


async def product_search_start(args: dict, ctx: ToolContext) -> dict:
    search = await _service(ctx).start(args, started_by="gary")
    return {
        "search": _spoken(search),
        "note": "Susan is researching in the background, one round at a time. Each round "
                "narrows the slate; the search stops when the evidence stops changing the "
                "ranking or the rounds run out. Ask for the product search to hear where it is.",
    }


async def product_search_get(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ProductSearchLookup, args)
    search = await run_sync(_service(ctx).get, request.search_id)
    return {"search": search}


async def product_search_status(args: dict, ctx: ToolContext) -> dict:
    request = validate_request(ProductSearchLookup, args)
    search = await run_sync(_service(ctx).get, request.search_id)
    return {"search": _spoken(search)}


async def product_search_stop(args: dict, ctx: ToolContext) -> dict:
    search = await run_sync(_service(ctx).stop, args, "alex")
    return {
        "search": _spoken(search),
        "note": "No further rounds will run. The ideas found so far are kept.",
    }


TOOLS = [
    Tool(
        "product_search_start",
        "Start GaryCorp's search for a startup product to build. Susan researches it over "
        "several rounds in the background: each round scores a slate of ideas on market, "
        "feasibility, evidence and differentiation, answers the questions left over from the "
        "round before, and drops what the evidence kills. It stops by itself when a further "
        "round would not change the ranking, or when the round limit is reached. Only one "
        "search runs at a time. Use it when Alex asks what he should build or wants product "
        "ideas worked out properly, not for a single quick question: that is delegate_to_agent.",
        obj(
            {
                "brief": string(
                    "What Alex is looking for, in his words: the kind of product, the market, "
                    "who it is for, what he wants out of it."
                ),
                "constraints": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Short named limits the ideas must respect, e.g. "
                                   "{\"budget\": \"under $500 to start\", \"time\": \"evenings\"}.",
                },
                "max_rounds": integer("How many rounds at most (1-10); omit for the default.", 1, 10),
            },
            ("brief",),
        ),
        product_search_start,
    ),
    Tool(
        "product_search_status",
        "Where the product search has got to: the leading idea and its score, the ideas behind "
        "it, what is still being checked, and whether it has finished and why. Use this for "
        "what should I build, what did Susan come up with, how is the product search going.",
        obj({"search_id": string("A specific search_id; omit for the latest.")}),
        product_search_status,
    ),
    Tool(
        "product_search_get",
        "The full product search: every idea on the slate with its scores, what would kill it, "
        "the cheapest next test, and each round's assignment. Use when Alex wants the detail "
        "behind one idea rather than the summary.",
        obj({"search_id": string("A specific search_id; omit for the latest.")}),
        product_search_get,
    ),
    Tool(
        "product_search_stop",
        "Stop the running product search. Only when Alex says to stop it; the ideas found so "
        "far are kept and can still be read.",
        obj(
            {
                "search_id": string("A specific search_id; omit for the running one."),
                "reason": string("Why Alex stopped it, if he said."),
            }
        ),
        product_search_stop,
    ),
]

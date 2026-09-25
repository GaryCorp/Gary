"""Run GaryCorp's search for a product to build, by hand, in the container.

    docker compose exec backend python -m app.product_cli start \\
        "A tool one person can ship, for video editors" \\
        --constraint budget="under $500 to start" --rounds 3
    docker compose exec backend python -m app.product_cli status
    docker compose exec backend python -m app.product_cli show
    docker compose exec backend python -m app.product_cli stop --reason "I have decided"

``start`` drives the whole search here and waits: it prints each round as it
lands and the ranked slate at the end, so the terminal is a way in without a
microphone. That matters because the process that starts a round runs it: with
``--detach`` the search is created and left to the backend's own loop instead,
which is the right choice only when the backend is running this code.

One round of a specialist's work takes a minute or two, so a five-round search
takes ten. Interrupting is safe: the rounds are in SQLite, and the backend
picks the search up on its next tick.
"""

import argparse
import asyncio
import json
import logging
import sys

# Waiting on the search as a whole, not one round: each round is a specialist
# run with its own hard timeout, and this only has to outlast the last one.
STALL_CHECKS = 2


def print_json(value) -> None:
    print(json.dumps(value, indent=2, default=str))


def note(text: str) -> None:
    print(text, file=sys.stderr)


def _signature(search: dict) -> tuple:
    return tuple((row["round"], row["status"]) for row in search["rounds"])


def _stall_reason(app_main) -> str:
    if app_main.operating.is_paused():
        return "the company is paused; resume it and the search continues"
    state = app_main.spend_gate.state()
    if not state["allowed"]:
        return state["reason"] or "the daily AI spend ceiling has been reached"
    return "no round could be started; see the backend log"


async def drive(app_main, service, agents, search_id: str, timeout: float) -> dict:
    """Take the search from where it is to wherever it ends."""
    reported, stalled = 0, 0
    while True:
        search = await asyncio.to_thread(service.get, search_id)
        if search["status"] != "running":
            return search

        completed = [row for row in search["rounds"] if row["status"] == "completed"]
        for row in completed[reported:]:
            note(f"  round {row['round']}: {row['ideas_considered']} ideas, "
                 f"leading {row['top_idea']} at {row['top_score']} out of 10")
        reported = max(reported, len(completed))

        running = [row["assignment_id"] for row in search["rounds"]
                   if row["status"] == "running" and row["assignment_id"]]
        if running:
            note(f"  round {search['rounds'][-1]['round']} is with Susan; waiting...")
            await agents.wait(running, timeout)

        finished = await service.advance(search_id)
        if finished is not None:
            return finished

        after = await asyncio.to_thread(service.get, search_id)
        if not running and _signature(after) == _signature(search):
            stalled += 1
            if stalled >= STALL_CHECKS:
                note(f"Stopped waiting: {_stall_reason(app_main)}.")
                return after
            await asyncio.sleep(2)
        else:
            stalled = 0


def summarise(search: dict) -> None:
    note("")
    note(f"{search['status']} after {search['rounds_completed']} of at most "
         f"{search['max_rounds']} rounds: {search['stop_reason'] or 'still running'}")
    for position, row in enumerate(search["shortlist"], start=1):
        note(f"  {position}. {row['name']} — {row['score']} out of 10, for {row['customer']}")
    if search["open_questions"]:
        note(f"  still open: {'; '.join(search['open_questions'])}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.product_cli")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a product search and run it to the end")
    start.add_argument("brief", help="what you are looking for, in your own words")
    start.add_argument("--rounds", type=int, help="round limit for this search (default: the deployment's)")
    start.add_argument("--constraint", action="append", default=[], metavar="NAME=TEXT",
                       help="a limit the ideas must respect, e.g. budget='under $500'")
    start.add_argument("--detach", action="store_true",
                       help="create the search and leave the backend's loop to run it")

    commands.add_parser("status", help="the leading idea and where the search has got to")
    show = commands.add_parser("show", help="the whole search: every idea, its scores and its rounds")
    show.add_argument("search_id", nargs="?")
    stop = commands.add_parser("stop", help="stop the running search, keeping what it found")
    stop.add_argument("--reason", default="")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    from app import main as app_main

    service = app_main.product_search
    if service is None:
        note("Product searches are turned off on this deployment "
             "(PRODUCT_SEARCH_MAX_ROUNDS is 0).")
        return 1

    agents = app_main.agent_service
    # This process runs its own rounds; do not announce them to voice.
    agents.runner.on_finished = None
    await asyncio.to_thread(agents.sync_roster)
    timeout = agents.registry.limits.max_execution_seconds * 3 + 60

    if args.command == "status":
        summarise(await asyncio.to_thread(service.get))
        return 0
    if args.command == "show":
        print_json(await asyncio.to_thread(service.get, args.search_id))
        return 0
    if args.command == "stop":
        stopped = await asyncio.to_thread(service.stop, {"reason": args.reason}, "alex")
        summarise(stopped)
        return 0

    constraints = dict(item.split("=", 1) for item in args.constraint) or None
    search = await service.start(
        {"brief": args.brief, "constraints": constraints,
         **({"max_rounds": args.rounds} if args.rounds else {})},
        started_by="alex",
    )
    note(f"Search {search['search_id']} started, at most {search['max_rounds']} rounds.")
    if args.detach:
        note("Left running: the backend picks it up on its next tick.")
        return 0

    finished = await drive(app_main, service, agents, search["search_id"], timeout)
    summarise(finished)
    print_json(finished)
    return 0 if finished["status"] in ("completed", "stopped") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

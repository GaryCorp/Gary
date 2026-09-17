"""Run GaryCorp specialist work by hand, inside the backend container.

    docker compose exec backend python -m app.team_cli team
    docker compose exec backend python -m app.team_cli assign susan "Research three ideas for the next experiment"
    docker compose exec backend python -m app.team_cli review "Give GaryCorp browser automation" --agents susan,dave,linda,catherine,lauren
    docker compose exec backend python -m app.team_cli show <assignment_id>
    docker compose exec backend python -m app.team_cli show-review [review_id]

assign and review wait for the reports and print them. Work is recorded as
assigned by "alex" in the audit log, and uses the same roster, permissions,
limits, and database as Gary.
"""

import argparse
import asyncio
import json
import logging
import sys


def print_json(value) -> None:
    print(json.dumps(value, indent=2, default=str))


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.team_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("team", help="show the team and recent work")

    assign = commands.add_parser("assign", help="give one specialist an assignment and wait")
    assign.add_argument("agent", choices=["susan", "dave", "linda", "catherine", "lauren"])
    assign.add_argument("objective")
    assign.add_argument("--project", help="project_id")
    assign.add_argument("--priority", type=int, default=5)
    assign.add_argument("--context", action="append", default=[], metavar="KEY=TEXT")

    review = commands.add_parser("review", help="run an independent management review and wait")
    review.add_argument("topic")
    review.add_argument("--agents", default="susan,dave,linda,catherine,lauren")
    review.add_argument("--project", help="project_id")

    show = commands.add_parser("show", help="show an assignment and its report")
    show.add_argument("assignment_id")
    show_review = commands.add_parser("show-review", help="show a management review")
    show_review.add_argument("review_id", nargs="?")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    from app import main as app_main
    from gary.agents.service import DelegateRequest, ReviewRequest

    service = app_main.agent_service
    # This process runs its own assignments; do not announce them to voice.
    service.runner.on_finished = None
    await asyncio.to_thread(service.sync_roster)
    timeout = service.registry.limits.max_execution_seconds * 3 + 60

    if args.command == "team":
        print_json(await asyncio.to_thread(service.team))
    elif args.command == "assign":
        context = dict(item.split("=", 1) for item in args.context) or None
        assignment = await service.delegate(
            DelegateRequest(agent_id=args.agent, objective=args.objective, project_id=args.project,
                            priority=args.priority, context=context),
            assigned_by="alex",
        )
        print(f"Assignment {assignment['id']} queued for {args.agent}; waiting...", file=sys.stderr)
        await service.wait([assignment["id"]], timeout)
        print_json(await asyncio.to_thread(service.get_assignment, assignment["id"]))
    elif args.command == "review":
        agents = [a.strip() for a in args.agents.split(",") if a.strip()]
        started = await service.start_review(
            ReviewRequest(topic=args.topic, agents=agents, project_id=args.project), requested_by="alex"
        )
        print(f"Review {started['review']['id']} started with {', '.join(agents)}; waiting...", file=sys.stderr)
        await service.wait([a["id"] for a in started["assignments"]], timeout)
        print_json((await asyncio.to_thread(service.get_review, started["review"]["id"])).model_dump(mode="json"))
    elif args.command == "show":
        print_json(await asyncio.to_thread(service.get_assignment, args.assignment_id))
    elif args.command == "show-review":
        print_json((await asyncio.to_thread(service.get_review, args.review_id)).model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

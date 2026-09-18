"""GaryCorp's hired employees: who Gary hired, and letting one go.

    docker compose exec backend python -m app.hiring_cli list
    docker compose exec backend python -m app.hiring_cli show nina
    docker compose exec backend python -m app.hiring_cli dismiss nina

Hiring happens through Gary's proposal and your approval on the web page.
Dismissal is yours alone: Gary has no tool for it.
"""

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.hiring_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every employee GaryCorp hired")
    show = commands.add_parser("show", help="one hire in full, including their prompt")
    show.add_argument("agent_id")
    dismiss = commands.add_parser("dismiss", help="deactivate a hired employee")
    dismiss.add_argument("agent_id")
    args = parser.parse_args(argv)

    from app import main as app_main
    from gary.agents.hiring import HiringError, definition_from_row
    from gary.db.repositories import Repositories

    with app_main.gary_ops.db.read() as conn:
        rows = Repositories.bind(conn).hires.list_all()

    if args.command == "list":
        if not rows:
            print("GaryCorp has not hired anyone yet. Gary proposes; you approve at /approvals.")
            return 0
        for row in rows:
            tools = ", ".join(json.loads(row["allowed_tools"]))
            print(f"{row['agent_id']:<12} {row['name']:<12} {row['title']:<34} "
                  f"{row['status']:<12} {row['created_at'][:10]}")
            print(f"             tools: {tools}")
        return 0

    row = next((r for r in rows if r["agent_id"] == args.agent_id), None)
    if row is None:
        print(f"No hired employee called {args.agent_id}.")
        return 1

    if args.command == "show":
        print(f"{row['name']} — {row['title']} ({row['department']}), {row['status']}")
        print(f"notebook: {row['notebook']}")
        print(f"gap they fill: {row['capability_gap']}")
        print(f"tools: {', '.join(json.loads(row['allowed_tools']))}")
        try:
            print(f"\n--- prompt ---\n{definition_from_row(row).backstory}")
        except HiringError as exc:
            print(f"\nThis row does not load as a valid employee: {exc}")
        return 0

    try:
        result = dismiss_or_fail(app_main, args.agent_id)
    except HiringError as exc:
        print(f"Could not dismiss {args.agent_id}: {exc}")
        return 1
    print(f"{result['name']} has left GaryCorp. Their notes and reports stay in the record.")
    return 0


def dismiss_or_fail(app_main, agent_id: str) -> dict:
    return app_main.dismiss_employee(app_main.gary_ops, app_main.agent_registry, agent_id)


if __name__ == "__main__":
    sys.exit(main())

"""Watch Gary run the company, without letting him change anything.

    docker compose exec backend python -m app.dry_run
    docker compose exec backend python -m app.dry_run --cycles 3 --type morning
    docker compose exec backend python -m app.dry_run --act          # really do it

Observe-only by default: the real model, the real company data, and a real
planning cycle, but ``max_actions=0`` so every proposal fails validation.
Nothing reaches the calendar, the team, or GitHub, and no daily summary is
written to Joplin. The proposals are printed so you can judge whether Gary's
decisions are any good before he makes them unattended.

``--act`` removes the guard and runs a normal cycle, which can schedule work,
delegate to specialists, and spend model tokens.
"""

import argparse
import asyncio
import sys

from gary.services.planning_cycle import PlanningCycle, PlanningCycleError

OBSERVED_FIELDS = ("title", "objective", "topic", "task_id")


class SilentNotebook:
    """Planning notes are skipped and no daily summary is written."""

    async def get_relevant_notes(self, project_names, today):
        return []

    async def write_daily_summary(self, day, markdown):
        return None


def describe(proposal: dict) -> str:
    for field in OBSERVED_FIELDS:
        value = proposal.get(field)
        if value:
            return str(value)[:100]
    return ""


def print_cycle(index: int, planning_type: str, result: dict, observed: bool) -> None:
    print(f"\n{'=' * 70}\ncycle {index}: {planning_type}\n{'=' * 70}")
    print(f"summary : {result['summary']}")
    print(f"briefing: {result['briefing'] or '(nothing worth interrupting for)'}")

    if observed:
        proposals = [item["proposal"] for item in result["rejected"]]
        print(f"\nwould have done ({len(proposals)}):")
        for proposal in proposals:
            print(f"  - {proposal.get('action_type')}: {describe(proposal)}")
            print(f"      why: {str(proposal.get('reason', ''))[:110]}")
        if not proposals:
            print("  (nothing: an empty plan is often the right answer)")
        return

    print(f"\ndid ({len(result['results'])}):")
    for item in result["results"]:
        status = item["result"].get("status", "error")
        print(f"  - {item['proposal']['action_type']}: {item['proposal']['task_title']} [{status}]")
    for item in result["rejected"]:
        print(f"  x {item['proposal'].get('action_type')}: {item['reason']}")


async def run(planning_type: str, cycles: int, act: bool) -> int:
    from app import main as app_main

    live = app_main.planning_cycle
    cycle = PlanningCycle(
        app_main.gary_ops,
        live.planner,
        live.notebook if act else SilentNotebook(),
        app_main.planning_calendar,
        live.email,
        # 0 means every proposed action is rejected: Gary plans, nothing happens.
        max_actions=live.max_actions if act else 0,
        horizon_hours=live.horizon_hours,
        team=live.team,
        # An observe-only run still calls the model, so it is still costed.
        usage=live.usage,
    )
    if act:
        print("RUNNING FOR REAL: this can change your calendar and commission work.\n")
    else:
        print("Observe-only: the real model on real data, but nothing will change.\n")
    print(f"team connected: {cycle.team is not None}")

    for index in range(1, cycles + 1):
        try:
            result = await cycle.run(planning_type)
        except PlanningCycleError as exc:
            print(f"\ncycle {index} failed: {exc}")
            return 1
        print_cycle(index, planning_type, result, observed=not act)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.dry_run")
    parser.add_argument(
        "--type",
        default="management",
        choices=["management", "morning", "midday", "evening", "manual"],
        help="which cycle to run (default: the continuous loop's cycle)",
    )
    parser.add_argument("--cycles", type=int, default=1, help="how many in a row")
    parser.add_argument(
        "--act",
        action="store_true",
        help="let the actions actually run (calendar, specialists, tokens)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args.type, max(1, args.cycles), args.act))


if __name__ == "__main__":
    sys.exit(main())

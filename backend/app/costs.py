"""Model prices and what GaryCorp's thinking has cost.

    docker compose exec backend python -m app.costs report --days 7
    docker compose exec backend python -m app.costs prices
    docker compose exec backend python -m app.costs set-price gpt-5.4-mini --input 0.25 --output 2
    docker compose exec backend python -m app.costs remove-price gpt-4o-mini

Prices are US dollars per MILLION tokens, as providers publish them, and are
stored on the data volume so they survive a rebuild. A model with no price is
reported as unpriced: its tokens are counted but no cost is claimed.
"""

import argparse
import sys


def money(value) -> str:
    return f"${value:,.4f}" if value else "$0.0000"


def print_report(summary: dict) -> None:
    total = summary["total"]
    print(f"\nGaryCorp AI spend, last {summary['days']} days")
    print(f"  total     : {money(total['cost_usd'])} over {total['calls']} calls "
          f"({total['total_tokens']:,} tokens)")
    print(f"  per day   : {money(summary['average_per_day_usd'])}")
    print(f"  today     : {money(summary['today']['cost_usd'])} "
          f"({summary['today']['calls']} calls)")

    if summary["by_source"]:
        print("\n  by department")
        for row in summary["by_source"]:
            print(f"    {row['source']:<16} {money(row['cost_usd']):>12}  "
                  f"{row['total_tokens']:>12,} tokens  {row['calls']:>4} calls")
    if summary["by_model"]:
        print("\n  by model")
        for row in summary["by_model"]:
            unpriced = "  (unpriced)" if row["unpriced_calls"] else ""
            print(f"    {row['model']:<24} {money(row['cost_usd']):>12}  "
                  f"{row['total_tokens']:>12,} tokens{unpriced}")
    if summary["daily"]:
        print("\n  by day")
        for row in summary["daily"][-14:]:
            print(f"    {row['day']}  {money(row['cost_usd']):>12}  {row['total_tokens']:>12,} tokens")
    if summary["unpriced_models"]:
        print("\n  unpriced models (tokens counted, no cost claimed)")
        for row in summary["unpriced_models"]:
            print(f"    {row['model']:<24} {row['total_tokens']:>12,} tokens  {row['calls']} calls")
    for note in summary["notes"]:
        print(f"\n  note: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.costs")
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="what the company has spent")
    report.add_argument("--days", type=int, default=30)

    billed = commands.add_parser(
        "billed", help="what the provider actually billed (needs OPENAI_ADMIN_KEY)"
    )
    billed.add_argument("--days", type=int, default=7)

    commands.add_parser("prices", help="show the price table")

    price = commands.add_parser("set-price", help="set a model's price, per million tokens")
    price.add_argument("model")
    price.add_argument("--input", type=float, required=True, help="USD per 1M input tokens")
    price.add_argument("--output", type=float, required=True, help="USD per 1M output tokens")
    price.add_argument("--cached-input", type=float, help="USD per 1M cached input tokens")
    price.add_argument("--audio-input", type=float, help="USD per 1M audio input tokens")
    price.add_argument("--audio-output", type=float, help="USD per 1M audio output tokens")

    remove = commands.add_parser("remove-price", help="forget a model's price")
    remove.add_argument("model")

    args = parser.parse_args(argv)
    from app import main as app_main

    prices = app_main.model_prices

    if args.command == "report":
        print_report(app_main.usage_ledger.summary(max(1, args.days)))
        return 0

    if args.command == "billed":
        from gary.finance.provider_costs import ProviderCostsError
        import asyncio

        try:
            billed = asyncio.run(app_main.provider_costs.daily(max(1, args.days)))
        except ProviderCostsError as exc:
            print(f"Billed costs are not available: {exc}")
            return 1
        print(f"\nBilled by OpenAI, last {billed['days']} days: "
              f"{money(billed['total_cost'])} {billed['currency'].upper()}")
        for row in billed["daily"]:
            print(f"    {row['day']}  {money(row['cost']):>12}")
        if billed["by_line_item"]:
            print("\n  by line item")
            for row in billed["by_line_item"]:
                print(f"    {row['line_item']:<34} {money(row['cost']):>12}")
        print(f"\n  note: {billed['note']}")
        return 0

    if args.command == "prices":
        table = prices.as_dict()
        if not table:
            print("No prices set. Costs will be reported as unpriced.")
            print("Set one with: python -m app.costs set-price <model> --input X --output Y")
            return 0
        print(f"\nPrices in {prices.path} (USD per million tokens)")
        for model, rates in table.items():
            detail = "  ".join(f"{field}={value}" for field, value in rates.items())
            print(f"  {model:<24} {detail}")
        return 0

    if args.command == "set-price":
        rates = {"input": args.input, "output": args.output}
        for field, value in (
            ("cached_input", args.cached_input),
            ("audio_input", args.audio_input),
            ("audio_output", args.audio_output),
        ):
            if value is not None:
                rates[field] = value
        price = prices.set_price(args.model, **rates)
        print(f"{args.model}: {price}")
        print(f"saved to {prices.path}")
        print("Existing ledger rows keep the cost they were recorded with; "
              "new calls use this price.")
        return 0

    if prices.remove(args.model):
        print(f"removed the price for {args.model}")
        return 0
    print(f"no price was set for {args.model}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Model prices and what GaryCorp's thinking has cost.

    docker compose exec backend python -m app.costs report --days 7
    docker compose exec backend python -m app.costs prices
    docker compose exec backend python -m app.costs set-price gpt-5.6-luna --input 0.2 --output 1.2
    docker compose exec backend python -m app.costs remove-price gpt-4o-mini

Token prices are US dollars per MILLION tokens, as providers publish them.
Transcription models bill by the minute instead, so they take --per-minute;
a model may have both. Prices are stored on the data volume so they survive a
rebuild. A model with no price is reported as unpriced: its tokens and audio
are counted but no cost is claimed.
"""

import argparse
import sys


def money(value) -> str:
    return f"${value:,.4f}" if value else "$0.0000"


def print_report(summary: dict) -> None:
    total = summary["total"]
    print(f"\nGaryCorp AI spend, last {summary['days']} days")
    audio = total.get("audio_seconds") or 0
    minutes = f", {audio / 60:,.1f} min audio" if audio else ""
    print(f"  total     : {money(total['cost_usd'])} over {total['calls']} calls "
          f"({total['total_tokens']:,} tokens{minutes})")
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
    commands.add_parser(
        "models", help="which model does which job, and whether it can be costed"
    )

    price = commands.add_parser("set-price", help="set a model's price, per million tokens")
    price.add_argument("model")
    price.add_argument("--input", type=float, help="USD per 1M input tokens")
    price.add_argument("--output", type=float, help="USD per 1M output tokens")
    price.add_argument("--cached-input", type=float, help="USD per 1M cached input tokens")
    price.add_argument("--audio-input", type=float, help="USD per 1M audio input tokens")
    price.add_argument("--audio-output", type=float, help="USD per 1M audio output tokens")
    price.add_argument(
        "--per-minute", type=float, help="USD per minute of audio (transcription models)"
    )

    remove = commands.add_parser("remove-price", help="forget a model's price")
    remove.add_argument("model")

    args = parser.parse_args(argv)
    from app import main as app_main
    from gary.finance.pricing import rate_phrase

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

    if args.command == "models":
        rows = app_main.usage_ledger.models_in_use(app_main.MODEL_ROLES)
        print("\nModels GaryCorp is configured to use")
        for row in rows:
            jobs = ", ".join(row["roles"])
            if row["priced"]:
                status = "  ".join(
                    rate_phrase(field, value) for field, value in row["rates"].items()
                )
            else:
                status = "NO PRICE SET - this spend cannot be measured"
            print(f"  {row['model']:<22} {jobs:<24} {status}")

        state = app_main.spend_gate.state(force=True)
        print(f"\n  daily ceiling  : {money(state['ceiling_usd'])}")
        print(f"  spent today    : {money(state['spent_usd'])}")
        print(f"  enforceable    : {state['enforceable']}")
        print(f"  work allowed   : {state['allowed']}")
        if state["reason"]:
            print(f"\n  {state['reason']}")
        if state["unpriced_models"]:
            print("\n  set the missing prices with one of:")
            for model in state["unpriced_models"]:
                # Which rate applies depends on how the model bills, and that
                # is the provider's choice, so both forms are offered.
                print(f"    python -m app.costs set-price {model} --input X --output Y")
                print(f"    python -m app.costs set-price {model} --per-minute X   (audio)")
        return 0

    if args.command == "prices":
        table = prices.as_dict()
        if not table:
            print("No prices set. Costs will be reported as unpriced.")
            print("Set one with: python -m app.costs set-price <model> --input X --output Y")
            return 0
        print(f"\nPrices in {prices.path}")
        for model, rates in table.items():
            # A zero rate charges nothing, so printing it only suggests the
            # model bills for something it does not.
            detail = "  ".join(
                rate_phrase(field, value) for field, value in rates.items() if value
            )
            print(f"  {model:<24} {detail}")
        return 0

    if args.command == "set-price":
        rates = {}
        for field, value in (
            ("input", args.input),
            ("output", args.output),
            ("cached_input", args.cached_input),
            ("audio_input", args.audio_input),
            ("audio_output", args.audio_output),
            ("per_minute", args.per_minute),
        ):
            if value is not None:
                rates[field] = value
        if not rates:
            print("Give at least one rate: --input/--output, or --per-minute for audio.")
            return 1
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

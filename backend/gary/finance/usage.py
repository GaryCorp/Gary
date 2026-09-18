"""The model-usage ledger: what GaryCorp's thinking costs.

Every model call the company makes is recorded here — Gary's planning cycles,
the specialists' runs, their web searches, and voice conversations — priced
from the deployment's price table. Recording never fails a caller: if the
ledger cannot be written, the work still happened and the failure is logged.

What is *not* measured is stated rather than assumed: EASE runs inside its own
container against its own provider and reports no tokens back, so its cost is
reported as unmeasured, never as zero.
"""

import datetime as dt
import logging

from gary.db import Database
from gary.db.repositories import Repositories
from gary.finance.pricing import PriceTable, Usage
from gary.services.common import Clock, clock_now, default_clock
from gary.timeutil import format_utc, to_datetime

logger = logging.getLogger("gary.usage")

UNMEASURED = (
    "EASE analyses run in the ease-api container on its own provider and report "
    "no token counts, so they are not included."
)


class UsageLedger:
    def __init__(self, db: Database, prices: PriceTable, timezone, clock: Clock = default_clock):
        self.db = db
        self.prices = prices
        self.timezone = timezone
        self.clock = clock

    # ------------------------------------------------------------ recording

    def record(
        self,
        source: str,
        model: str,
        usage: Usage,
        *,
        reported_cost_usd: float | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        detail: str | None = None,
    ) -> float | None:
        """Record one model call. Returns its cost, or None when unpriced."""
        if usage.total_tokens <= 0 and reported_cost_usd is None:
            return None
        cost = self.prices.cost(model, usage)
        try:
            now = clock_now(self.clock)
            with self.db.transaction() as conn:
                Repositories.bind(conn).usage.record(
                    source=source,
                    model=model,
                    tokens=usage.as_row(),
                    cost_usd=cost,
                    reported_cost_usd=reported_cost_usd,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    detail=detail,
                    now=now,
                )
        except Exception:
            # Accounting must never break the work it is accounting for.
            logger.exception("Could not record model usage for %s/%s", source, model)
        return cost

    # ------------------------------------------------------------ reporting

    def _since(self, days: int) -> str:
        return format_utc(clock_now_dt(self.clock) - dt.timedelta(days=days))

    def spent_today(self) -> dict:
        """Cost since local midnight: what a daily ceiling is measured against."""
        start = dt.datetime.combine(
            clock_now_dt(self.clock).astimezone(self.timezone).date(),
            dt.time(),
            self.timezone,
        )
        with self.db.read() as conn:
            totals = Repositories.bind(conn).usage.totals_since(format_utc(start))
        return {
            "since": format_utc(start),
            "cost_usd": round(totals["cost_usd"], 4),
            "total_tokens": totals["total_tokens"],
            "calls": totals["calls"],
            "unpriced_calls": totals["unpriced_calls"] or 0,
        }

    def summary(self, days: int = 30) -> dict:
        """What the company spent, by department, by model, and by day."""
        since = self._since(days)
        offset = clock_now_dt(self.clock).astimezone(self.timezone).strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"

        with self.db.read() as conn:
            repos = Repositories.bind(conn)
            totals = repos.usage.totals_since(since)
            by_source = repos.usage.by_source_since(since)
            by_model = repos.usage.by_model_since(since)
            daily = repos.usage.daily_since(since, offset)
            unpriced = repos.usage.unpriced_models_since(since)

        def money(row: dict) -> dict:
            return {**row, "cost_usd": round(row.get("cost_usd") or 0, 4)}

        priced_models = self.prices.known_models()
        return {
            "days": days,
            "since": since,
            "total": money(totals),
            "by_source": [money(row) for row in by_source],
            "by_model": [money(row) for row in by_model],
            "daily": [money(row) for row in daily],
            "today": self.spent_today(),
            "average_per_day_usd": round((totals["cost_usd"] or 0) / max(1, days), 4),
            "unpriced_models": unpriced,
            "priced_models": priced_models,
            "notes": [
                UNMEASURED,
                (
                    "Some calls are unpriced: set a price with "
                    "'python -m app.costs set-price <model> --input X --output Y' "
                    "(US dollars per million tokens) so they are costed."
                    if unpriced
                    else (
                        "No model calls have been recorded in this period."
                        if not totals["calls"]
                        else "Every recorded call is priced."
                    )
                ),
            ],
        }


def clock_now_dt(clock: Clock) -> dt.datetime:
    return to_datetime(clock_now(clock))

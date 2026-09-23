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
import time

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
        agent_id: str | None = None,
    ) -> float | None:
        """Record one model call. Returns its cost, or None when unpriced."""
        # A transcription call has no tokens at all, only a duration, so
        # tokens alone are not enough to decide there is nothing to record.
        if (
            usage.total_tokens <= 0
            and usage.audio_seconds <= 0
            and reported_cost_usd is None
        ):
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
                    agent_id=agent_id,
                    now=now,
                )
        except Exception:
            # Accounting must never break the work it is accounting for.
            logger.exception("Could not record model usage for %s/%s", source, model)
        return cost

    # ------------------------------------------------------------ reporting

    def models_in_use(self, roles: dict[str, str]) -> list[dict]:
        """Which model does which job, and what each one costs.

        ``roles`` maps a job ("voice", "planning", ...) to the model the
        deployment is configured to use. Until a model has actually been
        called it appears nowhere in the ledger, so this is the only way to
        see what is configured -- and the only way to notice that the thing
        about to spend money has no price.
        """
        seen: dict[str, dict] = {}
        for role, model in roles.items():
            if not model:
                continue
            entry = seen.setdefault(
                model, {**self.prices.describe(model), "roles": []}
            )
            entry["roles"].append(role)
        return sorted(seen.values(), key=lambda row: row["model"])

    def unpriced_in_use(self, roles: dict[str, str]) -> list[str]:
        """Configured models with no price. These are what make a spend
        ceiling unenforceable, whether or not they have been called yet."""
        return [row["model"] for row in self.models_in_use(roles) if not row["priced"]]

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
            by_agent = repos.usage.by_agent_since(since)
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
            # What each person's thinking cost: the CFO's view of the team.
            "by_agent": [money(row) for row in by_agent],
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


class SpendGate:
    """A hard daily ceiling on what GaryCorp may spend on thinking.

    The company runs itself for days at a time, so the thing that must not be
    possible is a stuck state quietly spending all week. Every path that can
    call a model asks this first: planning cycles, specialist runs, and the
    voice session.

    The ceiling is measured against priced calls only, which is the honest
    limit of what it can promise. An unpriced model contributes tokens but no
    cost, so while any call today is unpriced the ceiling **cannot** be
    enforced -- ``state()`` says so rather than reporting the company as
    comfortably within budget. It does not block on unpriced calls, because
    that would stop a working deployment the moment a new model appeared;
    setting a price is what makes the ceiling real.
    """

    # Spend is read this often at most; a 15-minute loop and a chatty voice
    # session must not turn the ceiling into a query storm.
    CACHE_SECONDS = 5.0

    def __init__(
        self,
        ledger: "UsageLedger",
        ceiling_usd: float,
        monotonic=None,
        roles: dict[str, str] | None = None,
        require_priced: bool = False,
    ):
        self.ledger = ledger
        self.ceiling_usd = float(ceiling_usd)
        self._monotonic = monotonic or time.monotonic
        # Which model does which job. Checked even before a model has been
        # called, so a deployment that cannot be costed is caught at startup
        # rather than after it has spent something.
        self.roles = roles or {}
        # When true, work that spends money is refused while a model it would
        # use has no price: an unmeasurable spend cannot be capped.
        self.require_priced = require_priced
        self._cached: dict | None = None
        self._cached_at = 0.0

    def state(self, force: bool = False) -> dict:
        now = self._monotonic()
        if not force and self._cached is not None and now - self._cached_at < self.CACHE_SECONDS:
            return self._cached

        today = self.ledger.spent_today()
        spent = today["cost_usd"]
        unpriced = today.get("unpriced_calls") or 0
        unpriced_models = self.ledger.unpriced_in_use(self.roles)
        enabled = self.ceiling_usd > 0
        within = not enabled or spent < self.ceiling_usd
        state = {
            "enabled": enabled,
            "ceiling_usd": round(self.ceiling_usd, 4),
            "spent_usd": spent,
            "remaining_usd": round(max(0.0, self.ceiling_usd - spent), 4) if enabled else None,
            "unpriced_calls": unpriced,
            "unpriced_models": unpriced_models,
            # What the ceiling can actually promise right now.
            "enforceable": enabled and unpriced == 0 and not unpriced_models,
            "within_ceiling": within,
            "allowed": within and not (self.require_priced and unpriced_models),
            "since": today["since"],
        }
        state["reason"] = self._reason(state)
        self._cached, self._cached_at = state, now
        return state

    @staticmethod
    def _reason(state: dict) -> str | None:
        if not state["within_ceiling"]:
            return (
                f"GaryCorp has spent ${state['spent_usd']:.2f} on AI today, which is its "
                f"daily ceiling of ${state['ceiling_usd']:.2f}. Work stops until midnight."
            )
        if not state["allowed"]:
            names = ", ".join(state["unpriced_models"])
            return (
                f"No price is set for {names}, so what the company spends cannot be "
                "measured and the daily ceiling cannot hold. Unattended work stops until "
                "a price is set."
            )
        if not state["enabled"]:
            return None
        if not state["enforceable"]:
            missing = state["unpriced_models"]
            what = ", ".join(missing) if missing else f"{state['unpriced_calls']} call(s) today"
            return (
                f"{what} has no price, so the ${state['ceiling_usd']:.2f} daily ceiling "
                "cannot be enforced. Set a price with the costs CLI."
            )
        return None

    def allowed(self) -> bool:
        return self.state()["allowed"]

    def require(self, what: str = "this") -> None:
        """Raise when the company may not spend any more today."""
        state = self.state()
        if not state["allowed"]:
            raise SpendCeilingReached(state["reason"] or f"The daily AI spend ceiling stops {what}")


class SpendCeilingReached(RuntimeError):
    """The company has spent its daily allowance. Not an error in the work."""


def clock_now_dt(clock: Clock) -> dt.datetime:
    return to_datetime(clock_now(clock))

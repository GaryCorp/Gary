"""What the provider says GaryCorp actually spent.

The price table estimates a cost per call, which is what lets Catherine
attribute spend to a department. This asks OpenAI for the billed figure
instead: authoritative, but only available per day and per line item, with no
idea which part of the company spent it.

Both are reported. When they disagree, the provider is right and the price
table needs correcting.

It needs an **admin key** (``OPENAI_ADMIN_KEY``) with the ``api.usage.read``
scope, created at platform.openai.com under Settings, Organization, Admin
keys. An ordinary API key returns 403, and that is reported plainly rather
than as a zero.
"""

import asyncio
import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("gary.provider_costs")

COSTS_URL = "https://api.openai.com/v1/organization/costs"
# One bucket per day is the finest granularity the costs endpoint offers.
BUCKET_WIDTH = "1d"
MAX_BUCKETS = 180


class ProviderCostsError(RuntimeError):
    """The provider could not be asked. Never a zero."""


class OpenAICosts:
    def __init__(self, admin_key: str, url: str = COSTS_URL, timeout: float = 30.0):
        self._admin_key = (admin_key or "").strip()
        self.url = url
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._admin_key)

    async def daily(self, days: int = 7) -> dict:
        return await asyncio.to_thread(self._daily, days)

    def _daily(self, days: int) -> dict:
        if not self.configured:
            raise ProviderCostsError(
                "No admin key is configured. Set OPENAI_ADMIN_KEY to a key with the "
                "api.usage.read scope (platform.openai.com, Settings, Organization, "
                "Admin keys) to read billed costs."
            )
        days = max(1, min(days, MAX_BUCKETS))
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        query = urllib.parse.urlencode(
            {
                "start_time": int(start.timestamp()),
                "bucket_width": BUCKET_WIDTH,
                "limit": days,
            }
        )
        # group_by repeats as an array parameter.
        request = urllib.request.Request(
            f"{self.url}?{query}&group_by[]=line_item",
            headers={
                "Authorization": f"Bearer {self._admin_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:200]
            if exc.code == 401:
                raise ProviderCostsError(
                    "OpenAI rejected the admin key. Check OPENAI_ADMIN_KEY."
                ) from exc
            if exc.code == 403:
                raise ProviderCostsError(
                    "The key cannot read usage. It needs the api.usage.read scope, "
                    "which ordinary API keys do not have: create an admin key at "
                    "platform.openai.com, Settings, Organization, Admin keys."
                ) from exc
            raise ProviderCostsError(f"OpenAI returned HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderCostsError(f"Could not reach OpenAI: {exc}") from exc

        return parse_costs(payload, days)


def parse_costs(payload: dict, days: int) -> dict:
    """Turn the bucketed response into daily totals and line items."""
    daily: list[dict] = []
    line_items: dict[str, float] = {}
    total = 0.0
    currency = "usd"

    for bucket in (payload or {}).get("data") or []:
        start = bucket.get("start_time")
        day = (
            dt.datetime.fromtimestamp(start, dt.timezone.utc).date().isoformat()
            if isinstance(start, (int, float))
            else None
        )
        amount = 0.0
        for result in bucket.get("results") or []:
            value = (result.get("amount") or {}).get("value")
            if not isinstance(value, (int, float)):
                continue
            amount += value
            currency = (result.get("amount") or {}).get("currency") or currency
            name = result.get("line_item") or "unattributed"
            line_items[name] = round(line_items.get(name, 0.0) + value, 6)
        total += amount
        if day:
            daily.append({"day": day, "cost": round(amount, 6)})

    return {
        "source": "OpenAI organization costs API (billed)",
        "days": days,
        "currency": currency,
        "total_cost": round(total, 4),
        "daily": daily,
        "by_line_item": [
            {"line_item": name, "cost": cost}
            for name, cost in sorted(line_items.items(), key=lambda item: -item[1])
        ],
        "note": (
            "Billed by the provider for the whole organization, so it includes any "
            "usage outside GaryCorp and cannot be split by department."
        ),
    }

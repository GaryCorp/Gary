"""Card purchase requests.

Catherine can only *request* a purchase. A request becomes a card_purchase
action, which policy makes yellow: it waits for Alex, and can only be
approved on the /approvals web page. Limits are checked when the request is
made and again when it is approved, against everything requested or approved
this calendar month.

No payment channel is connected yet, so an approved purchase is recorded as
approved and the card is not charged. A payment channel would plug in as the
handler's ``execute`` step.
"""

import datetime as dt
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from gary.db.repositories import Repositories
from gary.finance.cards import is_expired, public_card
from gary.models.common import EntityId, RequestModel
from gary.policy import CFO_ACTOR
from gary.services.action_service import ActionHandler
from gary.services.common import Clock, default_clock
from gary.timeutil import format_utc, to_local

MAX_AMOUNT_CENTS = 10_000_000
NOT_CHARGED_NOTE = "Approved. No payment channel is connected yet, so the card was not charged."


@dataclass(frozen=True)
class SpendingLimits:
    """Hard caps from the environment. No tool can read or change them except
    as numbers in Catherine's status."""

    per_purchase_cents: int
    monthly_cents: int

    def __post_init__(self):
        if not 0 < self.per_purchase_cents <= self.monthly_cents:
            raise ValueError("the per-purchase limit must be positive and no more than the monthly limit")


def dollars_to_cents(value: str) -> int:
    text = str(value).strip().removeprefix("$").replace(",", "")
    if not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", text):
        raise ValueError("amount must be US dollars such as 49.99")
    cents = int(Decimal(text) * 100)
    if not 0 < cents <= MAX_AMOUNT_CENTS:
        raise ValueError("amount must be more than zero")
    return cents


def format_cents(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def month_start(now: dt.datetime, timezone: ZoneInfo) -> str:
    local = now.astimezone(timezone)
    return format_utc(dt.datetime(local.year, local.month, 1, tzinfo=timezone))


class CardPurchasePayload(RequestModel):
    purchase_id: EntityId
    merchant: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    amount_cents: int = Field(strict=True, ge=1, le=MAX_AMOUNT_CENTS)
    currency: Literal["USD"] = "USD"
    merchant_url: str | None = Field(default=None, max_length=500)
    requested_by: Literal["catherine"] = CFO_ACTOR
    assignment_id: str | None = Field(default=None, max_length=64)

    @field_validator("merchant", "description")
    @classmethod
    def single_line(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("merchant_url")
    @classmethod
    def https_only(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"https://[^\s]+", value):
            raise ValueError("merchant_url must be an https:// address")
        return value


def purchase_summary(payload: CardPurchasePayload) -> str:
    return f"Card purchase: {format_cents(payload.amount_cents)} at {payload.merchant} for {payload.description[:100]}"


def spending_status(repos: Repositories, limits: SpendingLimits, timezone: ZoneInfo, now: dt.datetime) -> dict:
    committed = repos.finance.committed_cents(month_start(now, timezone))
    return {
        "per_purchase_limit": format_cents(limits.per_purchase_cents),
        "monthly_limit": format_cents(limits.monthly_cents),
        "committed_this_month": format_cents(committed),
        "remaining_this_month": format_cents(max(0, limits.monthly_cents - committed)),
        "note": "Committed means requested and waiting for Alex, or approved.",
    }


PURCHASE_STATUS = {
    "awaiting_approval": "waiting for Alex's approval",
    "approved": "approved",
    "executing": "approved",
    "succeeded": "approved, not charged (no payment channel yet)",
    "failed": "failed",
    "rejected": "rejected by Alex",
    "cancelled": "expired without an answer",
}


def purchase_brief(action: dict, timezone: ZoneInfo) -> dict:
    payload = json.loads(action["payload_json"])
    return {
        "purchase_id": payload.get("purchase_id"),
        "merchant": payload.get("merchant"),
        "description": payload.get("description"),
        "amount": format_cents(payload.get("amount_cents", 0)),
        "status": PURCHASE_STATUS.get(action["status"], action["status"]),
        "error": action["error_message"],
        "requested_at": to_local(action["created_at"], timezone),
    }


def card_purchase_handler(limits: SpendingLimits, timezone: ZoneInfo, clock: Clock = default_clock) -> ActionHandler:
    def check(repos: Repositories, payload: CardPurchasePayload) -> dict:
        card = repos.finance.current_card(CFO_ACTOR)
        if card is None:
            raise ValueError("Catherine has no debit card yet. Alex can add one at http://localhost:8000/finance")
        if card["status"] != "active":
            raise ValueError(f"Catherine's card ending {card['last4']} is {card['status']}")
        now = clock()
        if is_expired(card["exp_month"], card["exp_year"], now.astimezone(timezone).date()):
            raise ValueError(f"Catherine's card ending {card['last4']} has expired")
        if payload.amount_cents > limits.per_purchase_cents:
            raise ValueError(
                f"{format_cents(payload.amount_cents)} is over the per-purchase limit of "
                f"{format_cents(limits.per_purchase_cents)}"
            )
        committed = repos.finance.committed_cents(month_start(now, timezone), payload.purchase_id)
        if committed + payload.amount_cents > limits.monthly_cents:
            raise ValueError(
                f"{format_cents(payload.amount_cents)} would exceed the monthly limit of "
                f"{format_cents(limits.monthly_cents)}: {format_cents(committed)} is already "
                "requested or approved this month"
            )
        return {"card": public_card(card)}

    def record(repos: Repositories, payload: CardPurchasePayload, result: dict, now: str) -> dict:
        card = repos.finance.current_card(CFO_ACTOR)
        return {
            "charged": False,
            "card": f"{card['brand']} ending {card['last4']}" if card else None,
            "note": NOT_CHARGED_NOTE,
        }

    return ActionHandler(
        payload_model=CardPurchasePayload,
        summarize=lambda payload, context: purchase_summary(payload),
        check=check,
        execute=None,
        record=record,
        audit_event="card_purchase_approved",
    )

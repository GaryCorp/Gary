"""Catherine's debit card.

The card number never enters the operations database, a tool result, a
prompt, or the audit log. It is encrypted with Fernet into its own vault file
(separate key, owner-only file). SQLite keeps only brand, last four digits,
expiry, and status, which is all any agent can see.
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from gary.db import Database
from gary.db.repositories import Repositories
from gary.policy import CFO_ACTOR, USER_ACTOR
from gary.timeutil import format_utc, utc_now

NAME_LIMIT = 100


class CardVaultError(RuntimeError):
    pass


def luhn_valid(number: str) -> bool:
    total = 0
    for index, digit in enumerate(reversed(number)):
        value = int(digit)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def card_brand(number: str) -> str:
    if number.startswith("4"):
        return "Visa"
    if number[:2] in ("34", "37"):
        return "American Express"
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return "Mastercard"
    if number.startswith(("6011", "65")) or 644 <= int(number[:3]) <= 649:
        return "Discover"
    return "Card"


def is_expired(exp_month: int, exp_year: int, today: dt.date) -> bool:
    # A card is valid through the last day of its expiry month.
    return (exp_year, exp_month) < (today.year, today.month)


def normalize_card(number: str, exp_month, exp_year, name_on_card: str, today: dt.date) -> dict:
    digits = re.sub(r"[\s-]", "", number or "")
    if not re.fullmatch(r"\d{13,19}", digits) or not luhn_valid(digits):
        raise ValueError("That is not a valid card number")
    try:
        month, year = int(exp_month), int(exp_year)
    except (TypeError, ValueError):
        raise ValueError("Expiry month and year must be numbers") from None
    if year < 100:
        year += 2000
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        raise ValueError("Expiry must be a real month and year")
    if is_expired(month, year, today):
        raise ValueError("That card has expired")
    name = " ".join((name_on_card or "").split())
    if len(name) > NAME_LIMIT:
        raise ValueError(f"Name on card can be at most {NAME_LIMIT} characters")
    return {
        "number": digits,
        "exp_month": month,
        "exp_year": year,
        "name_on_card": name,
        "brand": card_brand(digits),
        "last4": digits[-4:],
    }


class CardVault:
    """Encrypted, owner-only storage for the full card details."""

    def __init__(self, path: Path, key: str | None):
        self.path = Path(path)
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def _require_key(self) -> Fernet:
        if self._fernet is None:
            raise CardVaultError("CARD_ENCRYPTION_KEY is not set, so a card cannot be stored")
        return self._fernet

    def save(self, details: dict) -> None:
        token = self._require_key().encrypt(json.dumps(details, sort_keys=True).encode())
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(token)
        os.replace(temporary, self.path)

    def load(self) -> dict | None:
        """For a future payment channel; nothing calls this yet."""
        if not self.path.exists():
            return None
        try:
            return json.loads(self._require_key().decrypt(self.path.read_bytes()))
        except InvalidToken:
            raise CardVaultError("The card vault cannot be decrypted with CARD_ENCRYPTION_KEY") from None

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def public_card(card: dict | None) -> dict | None:
    """Everything an agent or page may show about the card."""
    if card is None:
        return None
    return {
        "brand": card["brand"],
        "last4": card["last4"],
        "expires": f"{card['exp_month']:02d}/{card['exp_year']}",
        "status": card["status"],
    }


def add_card(db: Database, vault: CardVault, number: str, exp_month, exp_year,
             name_on_card: str, today: dt.date, holder: str = CFO_ACTOR) -> dict:
    card = normalize_card(number, exp_month, exp_year, name_on_card, today)
    vault._require_key()
    now = format_utc(utc_now())
    with db.transaction() as conn:
        repos = Repositories.bind(conn)
        row = repos.finance.add_card(holder, card["brand"], card["last4"], card["exp_month"], card["exp_year"], now)
        repos.audit.write(
            USER_ACTOR, "card_added",
            f"Gave {holder.capitalize()} a {card['brand']} debit card ending {card['last4']}",
            "payment_card", row["id"],
            {"brand": card["brand"], "last4": card["last4"]}, now=now,
        )
        # Written inside the transaction: if the vault cannot be written, the
        # card is not recorded either.
        vault.save({key: card[key] for key in ("number", "exp_month", "exp_year", "name_on_card")})
    return public_card(row)


def set_frozen(db: Database, frozen: bool, holder: str = CFO_ACTOR) -> dict:
    now = format_utc(utc_now())
    from_status, to_status = ("active", "frozen") if frozen else ("frozen", "active")
    with db.transaction() as conn:
        repos = Repositories.bind(conn)
        card = repos.finance.current_card(holder)
        if card is None:
            raise ValueError("There is no card to change")
        if not repos.finance.set_status(holder, from_status, to_status, now):
            raise ValueError(f"The card is already {card['status']}")
        repos.audit.write(
            USER_ACTOR, "card_frozen" if frozen else "card_unfrozen",
            f"{'Froze' if frozen else 'Unfroze'} the card ending {card['last4']}",
            "payment_card", card["id"], {"last4": card["last4"]}, now=now,
        )
        return public_card(repos.finance.current_card(holder))


def remove_card(db: Database, vault: CardVault, holder: str = CFO_ACTOR) -> None:
    now = format_utc(utc_now())
    with db.transaction() as conn:
        repos = Repositories.bind(conn)
        card = repos.finance.current_card(holder)
        if card is None:
            raise ValueError("There is no card to remove")
        repos.finance.remove_card(holder, now)
        repos.audit.write(
            USER_ACTOR, "card_removed", f"Removed the card ending {card['last4']}",
            "payment_card", card["id"], {"last4": card["last4"]}, now=now,
        )
    vault.delete()

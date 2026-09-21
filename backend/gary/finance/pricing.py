"""What a model call costs.

Prices are deployment data, not code: providers change them, and a wrong
number quietly misreports the company's spend. They live in a JSON file on
the data volume (``GARY_MODEL_PRICES_FILE``, default ``/data/model_prices.json``)
and can be set with ``python -m app.costs set-price``.

A model with no price is reported as **unpriced**, with its tokens counted and
its cost left null. Nothing here guesses a price, and an unpriced call is
never reported as free.

Prices are US dollars per one million tokens, the unit providers publish.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("gary.pricing")

DEFAULT_PRICES_FILE = "/data/model_prices.json"
PER_TOKENS = 1_000_000
# Token rates are US dollars per million tokens; per_minute is dollars per
# minute of audio, which is how transcription models bill. A model may have
# both: whichever rates are set are the ones that apply.
TOKEN_RATE_FIELDS = ("input", "cached_input", "output", "audio_input", "audio_output")
RATE_FIELDS = (*TOKEN_RATE_FIELDS, "per_minute")


def rate_phrase(field: str, value: float, spoken: bool = False) -> str:
    """One rate, in the unit it is actually charged in.

    ``spoken`` puts the currency where a person says it, because Piper reads
    this aloud and "$" is not a word.
    """
    money = f"{value} dollars" if spoken else f"${value}"
    if field == "per_minute":
        return f"{money} per minute of audio"
    return f"{money} per million {field.replace('_', ' ')} tokens"


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million tokens, except per_minute: dollars per minute
    of audio, which is how transcription models bill."""

    input: float = 0.0
    cached_input: float | None = None
    output: float = 0.0
    audio_input: float | None = None
    audio_output: float | None = None
    per_minute: float | None = None

    def rate(self, field: str) -> float:
        value = getattr(self, field, None)
        if value is not None:
            return value
        # Fall back to the text rate when a provider bills audio or cached
        # tokens at the same price, rather than silently charging zero.
        if field == "cached_input":
            return self.input
        if field == "audio_input":
            return self.input
        if field == "audio_output":
            return self.output
        # per_minute has no text equivalent: a model that does not bill by
        # the minute simply has no audio charge.
        return 0.0


@dataclass(frozen=True)
class Usage:
    """What one model call consumed: tokens, and audio duration when the
    model bills by the minute."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.audio_input_tokens
            + self.audio_output_tokens
        )

    def as_row(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "audio_input_tokens": self.audio_input_tokens,
            "audio_output_tokens": self.audio_output_tokens,
            "total_tokens": self.total_tokens,
            "audio_seconds": self.audio_seconds,
        }


def _number(value) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def usage_from_openai(payload: dict | None) -> Usage:
    """Read a Responses or Realtime API usage object.

    Both report ``input_tokens``/``output_tokens`` with optional details for
    cached and audio tokens; uncounted fields stay zero rather than being
    inferred.
    """
    payload = payload or {}
    input_details = payload.get("input_token_details") or {}
    output_details = payload.get("output_token_details") or {}
    cached = _number(input_details.get("cached_tokens"))
    audio_in = _number(input_details.get("audio_tokens"))
    audio_out = _number(output_details.get("audio_tokens"))
    text_in = max(0, _number(payload.get("input_tokens")) - audio_in)
    text_out = max(0, _number(payload.get("output_tokens")) - audio_out)
    return Usage(
        input_tokens=text_in,
        cached_input_tokens=min(cached, text_in),
        output_tokens=text_out,
        audio_input_tokens=audio_in,
        audio_output_tokens=audio_out,
    )


def usage_from_tokens(prompt: int = 0, completion: int = 0) -> Usage:
    """Usage from a framework that only reports prompt and completion counts."""
    return Usage(input_tokens=_number(prompt), output_tokens=_number(completion))


class PriceTable:
    """The deployment's model prices, read from disk and overridable by env."""

    def __init__(self, path: str | Path | None = None, environ: dict | None = None):
        env = environ if environ is not None else os.environ
        self.path = Path(path or env.get("GARY_MODEL_PRICES_FILE") or DEFAULT_PRICES_FILE)
        self._prices: dict[str, ModelPrice] = {}
        self._load(env)

    def _load(self, env: dict) -> None:
        raw: dict = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text()) or {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read model prices from %s: %s", self.path, exc)
        override = (env.get("GARY_MODEL_PRICES") or "").strip()
        if override:
            try:
                raw.update(json.loads(override))
            except json.JSONDecodeError as exc:
                logger.warning("GARY_MODEL_PRICES is not valid JSON: %s", exc)

        for model, values in raw.items():
            if not isinstance(values, dict):
                continue
            rates = {
                field: float(values[field])
                for field in RATE_FIELDS
                if isinstance(values.get(field), (int, float))
            }
            if rates:
                self._prices[model.strip().casefold()] = ModelPrice(**rates)

    # ------------------------------------------------------------- lookups

    def get(self, model: str | None) -> ModelPrice | None:
        if not model:
            return None
        return self._prices.get(model.strip().casefold())

    def known_models(self) -> list[str]:
        return sorted(self._prices)

    def describe(self, model: str | None) -> dict:
        """One model's price, in the units a person reads: dollars per
        million tokens. ``priced`` is false when nothing is known, which is
        reported rather than treated as free."""
        price = self.get(model)
        if price is None:
            return {"model": model, "priced": False, "rates": {}}
        return {
            "model": model,
            "priced": True,
            # A zero rate costs nothing, so showing it would only suggest
            # the model charges for something it does not.
            "rates": {
                field: getattr(price, field)
                for field in RATE_FIELDS
                if getattr(price, field, None)
            },
        }

    def cost(self, model: str | None, usage: Usage) -> float | None:
        """Dollars for this call, or None when the model has no price."""
        price = self.get(model)
        if price is None:
            return None
        billable_input = max(0, usage.input_tokens - usage.cached_input_tokens)
        dollars = (
            billable_input * price.rate("input")
            + usage.cached_input_tokens * price.rate("cached_input")
            + usage.output_tokens * price.rate("output")
            + usage.audio_input_tokens * price.rate("audio_input")
            + usage.audio_output_tokens * price.rate("audio_output")
        ) / PER_TOKENS
        # Audio is billed by the minute. A model with only a per-minute rate
        # contributes nothing above; one with only token rates nothing here.
        dollars += usage.audio_seconds / 60.0 * price.rate("per_minute")
        return round(dollars, 6)

    # -------------------------------------------------------------- writing

    def set_price(self, model: str, **rates: float) -> ModelPrice:
        """Set or update one model's price and save the table."""
        unknown = set(rates) - set(RATE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown price fields: {sorted(unknown)}")
        current = self.get(model)
        merged = {
            field: rates.get(field, getattr(current, field, None) if current else None)
            for field in RATE_FIELDS
        }
        merged = {field: value for field, value in merged.items() if value is not None}
        # A transcription model bills only by the minute and has no token
        # rates at all, so either kind of rate is enough to be priced.
        has_tokens = "input" in merged and "output" in merged
        if not has_tokens and "per_minute" not in merged:
            raise ValueError(
                "a price needs input and output rates, or per_minute for audio"
            )
        price = ModelPrice(**merged)
        self._prices[model.strip().casefold()] = price
        self._save()
        return price

    def remove(self, model: str) -> bool:
        removed = self._prices.pop(model.strip().casefold(), None) is not None
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        payload = {
            model: {
                field: getattr(price, field)
                for field in RATE_FIELDS
                if getattr(price, field) is not None
            }
            for model, price in sorted(self._prices.items())
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def as_dict(self) -> dict:
        return {
            model: {
                field: getattr(price, field)
                for field in RATE_FIELDS
                if getattr(price, field) is not None
            }
            for model, price in sorted(self._prices.items())
        }

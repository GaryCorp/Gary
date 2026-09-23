import sqlite3

from gary.db.repositories.base import insert_row, new_id, now_utc, rows_to_dicts

SOURCES = (
    "planning_cycle",
    "specialist",
    "web_search",
    "voice",
    # Turning what Alex said into words. Billed by the minute, not by token,
    # so it is kept apart from the conversation it belongs to.
    "voice_transcription",
    # Judging how someone is doing. One call per review.
    "performance_review",
    "other",
)


class ModelUsageRepository:
    """The model-usage ledger: one row per model call, priced where known."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(
        self,
        source: str,
        model: str,
        tokens: dict,
        cost_usd: float | None = None,
        reported_cost_usd: float | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        detail: str | None = None,
        occurred_at: str | None = None,
        now: str | None = None,
    ) -> str:
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        now = now or now_utc()
        usage_id = new_id()
        insert_row(
            self.conn,
            "model_usage",
            {
                "id": usage_id,
                "occurred_at": occurred_at or now,
                "source": source,
                "model": model or "unknown",
                "audio_seconds": tokens.get("audio_seconds", 0) or 0,
                "input_tokens": tokens.get("input_tokens", 0),
                "cached_input_tokens": tokens.get("cached_input_tokens", 0),
                "output_tokens": tokens.get("output_tokens", 0),
                "audio_input_tokens": tokens.get("audio_input_tokens", 0),
                "audio_output_tokens": tokens.get("audio_output_tokens", 0),
                "total_tokens": tokens.get("total_tokens", 0),
                "cost_usd": cost_usd,
                "reported_cost_usd": reported_cost_usd,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "detail": detail,
                "created_at": now,
            },
        )
        return usage_id

    _TOTALS = """
        COUNT(*) AS calls,
        COALESCE(SUM(input_tokens), 0) AS input_tokens,
        COALESCE(SUM(output_tokens), 0) AS output_tokens,
        COALESCE(SUM(audio_input_tokens), 0) AS audio_input_tokens,
        COALESCE(SUM(audio_output_tokens), 0) AS audio_output_tokens,
        COALESCE(SUM(total_tokens), 0) AS total_tokens,
        COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
        COALESCE(SUM(COALESCE(reported_cost_usd, cost_usd)), 0) AS cost_usd,
        SUM(CASE WHEN cost_usd IS NULL AND reported_cost_usd IS NULL THEN 1 ELSE 0 END)
            AS unpriced_calls
    """

    def totals_since(self, since: str) -> dict:
        row = self.conn.execute(
            f"SELECT {self._TOTALS} FROM model_usage WHERE occurred_at >= ?", (since,)
        ).fetchone()
        return dict(row)

    def by_source_since(self, since: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT source, {self._TOTALS} FROM model_usage
                WHERE occurred_at >= ?
                GROUP BY source ORDER BY cost_usd DESC, total_tokens DESC
                """,
                (since,),
            )
        )

    def by_model_since(self, since: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT model, {self._TOTALS} FROM model_usage
                WHERE occurred_at >= ?
                GROUP BY model ORDER BY cost_usd DESC, total_tokens DESC
                """,
                (since,),
            )
        )

    def daily_since(self, since: str, timezone_offset: str = "+00:00") -> list[dict]:
        """Spend per local day. SQLite has no timezone support, so the stored
        UTC instant is shifted by the local offset before the date is taken."""
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT date(datetime(occurred_at, ?)) AS day, {self._TOTALS}
                FROM model_usage WHERE occurred_at >= ?
                GROUP BY day ORDER BY day
                """,
                (_sqlite_offset(timezone_offset), since),
            )
        )

    def unpriced_models_since(self, since: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT model, COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM model_usage
                WHERE occurred_at >= ? AND cost_usd IS NULL AND reported_cost_usd IS NULL
                GROUP BY model ORDER BY total_tokens DESC
                """,
                (since,),
            )
        )

    def recent(self, limit: int = 20) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM model_usage ORDER BY occurred_at DESC LIMIT ?", (limit,)
            )
        )


def _sqlite_offset(offset: str) -> str:
    """``-05:00`` -> ``-5 hours``, for SQLite's datetime modifier."""
    try:
        sign = -1 if offset.startswith("-") else 1
        hours, _, minutes = offset.lstrip("+-").partition(":")
        total = sign * (int(hours) * 60 + int(minutes or 0))
        return f"{total} minutes"
    except (ValueError, AttributeError):
        return "0 minutes"

import sqlite3

from gary.db.repositories.base import insert_row, new_id, now_utc, row_to_dict, rows_to_dicts

# Purchase statuses that count against the spending limits: requested and
# not yet refused, or approved.
COMMITTED_PURCHASE_STATUSES = ("awaiting_approval", "approved", "executing", "succeeded")


class FinanceRepository:
    """Card facts (never the card number) and the card purchase ledger, which
    is the card_purchase rows of the actions table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def current_card(self, holder_agent_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                """
                SELECT * FROM payment_cards
                WHERE holder_agent_id = ? AND status != 'removed'
                """,
                (holder_agent_id,),
            ).fetchone()
        )

    def add_card(
        self,
        holder_agent_id: str,
        brand: str,
        last4: str,
        exp_month: int,
        exp_year: int,
        now: str | None = None,
    ) -> dict:
        """Replaces any card the holder already has."""
        now = now or now_utc()
        self.remove_card(holder_agent_id, now)
        card_id = new_id()
        insert_row(
            self.conn,
            "payment_cards",
            {
                "id": card_id,
                "holder_agent_id": holder_agent_id,
                "brand": brand,
                "last4": last4,
                "exp_month": exp_month,
                "exp_year": exp_year,
                "status": "active",
                "added_at": now,
                "updated_at": now,
            },
        )
        return row_to_dict(
            self.conn.execute("SELECT * FROM payment_cards WHERE id = ?", (card_id,)).fetchone()
        )

    def set_status(self, holder_agent_id: str, from_status: str, to_status: str, now: str | None = None) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE payment_cards SET status = ?, updated_at = ?
            WHERE holder_agent_id = ? AND status = ?
            """,
            (to_status, now or now_utc(), holder_agent_id, from_status),
        )
        return cursor.rowcount == 1

    def remove_card(self, holder_agent_id: str, now: str | None = None) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE payment_cards SET status = 'removed', updated_at = ?
            WHERE holder_agent_id = ? AND status != 'removed'
            """,
            (now or now_utc(), holder_agent_id),
        )
        return cursor.rowcount == 1

    def committed_cents(self, since: str, exclude_purchase_id: str | None = None) -> int:
        """Total of card purchases requested or approved since ``since``,
        leaving out one purchase (the one being checked)."""
        marks = ", ".join("?" for _ in COMMITTED_PURCHASE_STATUSES)
        row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM(json_extract(payload_json, '$.amount_cents')), 0) AS total
            FROM actions
            WHERE action_type = 'card_purchase'
              AND created_at >= ?
              AND status IN ({marks})
              AND (? IS NULL OR json_extract(payload_json, '$.purchase_id') != ?)
            """,
            (since, *COMMITTED_PURCHASE_STATUSES, exclude_purchase_id, exclude_purchase_id),
        ).fetchone()
        return int(row["total"])

    def list_purchases(self, since: str | None = None, limit: int = 20) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM actions
                WHERE action_type = 'card_purchase'
                  AND (? IS NULL OR created_at >= ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since, since, limit),
            )
        )

    def agent_usage_since(self, since: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT agent_id, model, COUNT(*) AS runs,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       SUM(cost_usd) AS reported_cost_usd
                FROM agent_runs
                WHERE started_at >= ?
                GROUP BY agent_id, model
                ORDER BY total_tokens DESC
                """,
                (since,),
            )
        )

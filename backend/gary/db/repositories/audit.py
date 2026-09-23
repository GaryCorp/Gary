import sqlite3

from gary.db.repositories.base import now_utc, rows_to_dicts, to_json


class AuditRepository:
    """Append-only. There is deliberately no update or delete method, and
    database triggers reject UPDATE and DELETE on audit_log."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def write(
        self,
        actor: str,
        event_type: str,
        summary: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict | None = None,
        now: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO audit_log
                (timestamp, actor, event_type, entity_type, entity_id,
                 summary, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now or now_utc(),
                actor,
                event_type,
                entity_type,
                entity_id,
                summary,
                to_json(details),
            ),
        )
        return cursor.lastrowid

    def count_events_for(self, event_type: str, actor: str, since: str) -> int:
        """How many times one actor caused one kind of event. Used by
        performance scorecards, where a count is the whole point."""
        return self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM audit_log
            WHERE event_type = ? AND actor = ? AND timestamp >= ?
            """,
            (event_type, actor, since),
        ).fetchone()["n"]

    def list_recent(self, limit: int = 50) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def list_for_entity(self, entity_type: str, entity_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM audit_log
                WHERE entity_type = ? AND entity_id = ?
                ORDER BY id
                """,
                (entity_type, entity_id),
            )
        )

    def latest_timestamp(self, event_types: tuple[str, ...]) -> str | None:
        marks = ", ".join("?" for _ in event_types)
        row = self.conn.execute(
            f"SELECT MAX(timestamp) AS latest FROM audit_log WHERE event_type IN ({marks})",
            event_types,
        ).fetchone()
        return row["latest"]

    def list_since(self, event_type: str, since: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM audit_log WHERE event_type = ? AND timestamp >= ?
                ORDER BY id
                """,
                (event_type, since),
            )
        )

    def has_event(self, event_type: str, entity_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM audit_log WHERE event_type = ? AND entity_id = ? LIMIT 1",
                (event_type, entity_id),
            ).fetchone()
            is not None
        )

import sqlite3

from gary.db.repositories.base import now_utc, row_to_dict


class OperatingStateRepository:
    """The single operating_state row: paused or running, and how far Alex's
    command emails have been read."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self) -> dict:
        return row_to_dict(
            self.conn.execute("SELECT * FROM operating_state WHERE id = 1").fetchone()
        )

    def set_paused(
        self, paused: bool, reason: str | None, actor: str, now: str | None = None
    ) -> dict:
        self.conn.execute(
            """
            UPDATE operating_state
            SET paused = ?, paused_reason = ?, changed_at = ?, changed_by = ?
            WHERE id = 1
            """,
            (int(paused), reason, now or now_utc(), actor),
        )
        return self.get()

    def advance_cursor(self, internal_date_ms: int, message_id: str) -> dict:
        """Only ever moves forward, so a message is acted on once."""
        self.conn.execute(
            """
            UPDATE operating_state
            SET commands_cursor_ms = ?, last_command_id = ?
            WHERE id = 1 AND commands_cursor_ms < ?
            """,
            (internal_date_ms, message_id, internal_date_ms),
        )
        return self.get()

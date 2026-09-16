import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
    to_json,
    update_columns,
)


class ActionRepository:
    UPDATABLE = frozenset(
        {
            "status",
            "approval_id",
            "executed_at",
            "result_json",
            "error_message",
        }
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        action_type: str,
        payload: dict,
        risk_level: str,
        status: str,
        reason: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        approval_id: str | None = None,
        error_message: str | None = None,
        now: str | None = None,
    ) -> dict:
        action_id = new_id()
        insert_row(
            self.conn,
            "actions",
            {
                "id": action_id,
                "action_type": action_type,
                "project_id": project_id,
                "task_id": task_id,
                "payload_json": to_json(payload),
                "reason": reason,
                "risk_level": risk_level,
                "status": status,
                "approval_id": approval_id,
                "error_message": error_message,
                "created_at": now or now_utc(),
            },
        )
        return self.get(action_id)

    def get(self, action_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        )

    def get_by_approval(self, approval_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM actions WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        )

    def update(self, action_id: str, **changes) -> dict:
        update_columns(self.conn, "actions", action_id, changes, self.UPDATABLE)
        return self.get(action_id)

    def transition(self, action_id: str, from_status: str, to_status: str) -> bool:
        """Compare-and-set status change, so an action cannot run twice."""
        cursor = self.conn.execute(
            "UPDATE actions SET status = ? WHERE id = ? AND status = ?",
            (to_status, action_id, from_status),
        )
        return cursor.rowcount == 1

    def list_executed_between(
        self, start: str, end: str, action_type: str | None = None
    ) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM actions
                WHERE status = 'succeeded'
                  AND executed_at >= ? AND executed_at < ?
                  AND (? IS NULL OR action_type = ?)
                ORDER BY executed_at
                """,
                (start, end, action_type, action_type),
            )
        )

    def list_recent(self, since: str, limit: int = 10) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM actions
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since, limit),
            )
        )

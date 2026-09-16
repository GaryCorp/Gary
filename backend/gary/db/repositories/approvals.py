import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
    to_json,
)


class ApprovalRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        action_type: str,
        summary: str,
        payload: dict,
        risk_level: str,
        reason: str | None = None,
        requested_by: str = "gary",
        now: str | None = None,
    ) -> dict:
        approval_id = new_id()
        insert_row(
            self.conn,
            "approvals",
            {
                "id": approval_id,
                "action_type": action_type,
                "summary": summary,
                "payload_json": to_json(payload),
                "reason": reason,
                "risk_level": risk_level,
                "requested_by": requested_by,
                "created_at": now or now_utc(),
            },
        )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        )

    def list_pending(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"
            )
        )

    def list_recent_resolved(self, limit: int = 20) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM approvals
                WHERE status != 'pending'
                ORDER BY resolved_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def resolve(
        self,
        approval_id: str,
        status: str,
        note: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Resolve a pending approval. Returns False if it was not pending, so
        two people or channels cannot both resolve it."""
        cursor = self.conn.execute(
            """
            UPDATE approvals
            SET status = ?, resolved_at = ?, resolution_note = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, now or now_utc(), note, approval_id),
        )
        return cursor.rowcount == 1

    def list_pending_created_before(self, cutoff: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM approvals
                WHERE status = 'pending' AND created_at < ?
                """,
                (cutoff,),
            )
        )

import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
)


class CommitmentRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        description: str,
        committed_to: str | None = None,
        deadline: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        commitment_id = new_id()
        insert_row(
            self.conn,
            "commitments",
            {
                "id": commitment_id,
                "project_id": project_id,
                "task_id": task_id,
                "description": description,
                "committed_to": committed_to,
                "deadline": deadline,
                "created_at": now,
                "updated_at": now,
            },
        )
        return self.get(commitment_id)

    def get(self, commitment_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
            ).fetchone()
        )

    def set_status(self, commitment_id: str, status: str, now: str | None = None) -> dict:
        self.conn.execute(
            "UPDATE commitments SET status = ?, updated_at = ? WHERE id = ?",
            (status, now or now_utc(), commitment_id),
        )
        return self.get(commitment_id)

    def list_open(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM commitments
                WHERE status = 'open'
                ORDER BY deadline IS NULL, deadline, created_at
                """
            )
        )

    def open_task_ids(self) -> set[str]:
        return {
            row["task_id"]
            for row in self.conn.execute(
                """
                SELECT DISTINCT task_id FROM commitments
                WHERE status = 'open' AND task_id IS NOT NULL
                """
            )
        }

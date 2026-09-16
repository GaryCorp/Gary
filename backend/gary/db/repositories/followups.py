import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
)


class FollowupRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        title: str,
        due_at: str,
        description: str | None = None,
        priority: int = 5,
        project_id: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        followup_id = new_id()
        insert_row(
            self.conn,
            "followups",
            {
                "id": followup_id,
                "project_id": project_id,
                "task_id": task_id,
                "title": title,
                "description": description,
                "due_at": due_at,
                "priority": priority,
                "created_at": now or now_utc(),
            },
        )
        return self.get(followup_id)

    def get(self, followup_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM followups WHERE id = ?", (followup_id,)
            ).fetchone()
        )

    def set_status(self, followup_id: str, status: str, now: str | None = None) -> dict:
        self.conn.execute(
            "UPDATE followups SET status = ?, completed_at = ? WHERE id = ?",
            (status, now or now_utc(), followup_id),
        )
        return self.get(followup_id)

    def list_due(self, now: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT *
                FROM followups
                WHERE status = 'pending'
                AND due_at <= ?
                ORDER BY priority DESC, due_at ASC
                """,
                (now,),
            )
        )

    def list_created_between(self, start: str, end: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM followups
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at
                """,
                (start, end),
            )
        )

    def list_pending(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM followups WHERE status = 'pending' ORDER BY due_at"
            )
        )

    def list_pending_for_project(self, project_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM followups
                WHERE status = 'pending' AND project_id = ?
                ORDER BY due_at, priority DESC
                """,
                (project_id,),
            )
        )

    def list_pending_between(self, start: str, end: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM followups
                WHERE status = 'pending' AND due_at > ? AND due_at <= ?
                ORDER BY due_at, priority DESC
                """,
                (start, end),
            )
        )

    def close_pending_for_task(
        self, task_id: str, status: str, now: str | None = None
    ) -> list[str]:
        ids = [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM followups WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
        ]
        self.conn.execute(
            """
            UPDATE followups SET status = ?, completed_at = ?
            WHERE task_id = ? AND status = 'pending'
            """,
            (status, now or now_utc(), task_id),
        )
        return ids

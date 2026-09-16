import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    placeholders,
    row_to_dict,
    rows_to_dicts,
    to_json,
    update_columns,
)

CLOSED_TASK_STATUSES = ("completed", "cancelled")


class TaskRepository:
    UPDATABLE = frozenset(
        {
            "project_id",
            "title",
            "description",
            "status",
            "priority",
            "estimated_minutes",
            "actual_minutes",
            "deadline",
            "earliest_start",
            "scheduled_start",
            "scheduled_end",
            "calendar_event_id",
            "started_at",
            "completed_at",
            "metadata_json",
        }
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        title: str,
        project_id: str | None = None,
        description: str | None = None,
        priority: int = 5,
        estimated_minutes: int | None = None,
        deadline: str | None = None,
        earliest_start: str | None = None,
        metadata: dict | None = None,
        created_by: str = "gary",
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        task_id = new_id()
        insert_row(
            self.conn,
            "tasks",
            {
                "id": task_id,
                "project_id": project_id,
                "title": title,
                "description": description,
                "priority": priority,
                "estimated_minutes": estimated_minutes,
                "deadline": deadline,
                "earliest_start": earliest_start,
                "created_at": now,
                "updated_at": now,
                "created_by": created_by,
                "metadata_json": to_json(metadata),
            },
        )
        return self.get(task_id)

    def get(self, task_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    def get_many(self, task_ids) -> list[dict]:
        task_ids = list(task_ids)
        if not task_ids:
            return []
        return rows_to_dicts(
            self.conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders(task_ids)})",
                task_ids,
            )
        )

    def update(self, task_id: str, now: str | None = None, **changes) -> dict:
        changes["updated_at"] = now or now_utc()
        update_columns(
            self.conn, "tasks", task_id, changes, self.UPDATABLE | {"updated_at"}
        )
        return self.get(task_id)

    def list_for_project(self, project_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM tasks
                WHERE project_id = ?
                ORDER BY status IN ('completed', 'cancelled'),
                         priority DESC, deadline IS NULL, deadline, created_at
                """,
                (project_id,),
            )
        )

    def list_open(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM tasks
                WHERE status NOT IN ('completed', 'cancelled')
                ORDER BY priority DESC, deadline IS NULL, deadline, created_at
                """
            )
        )

    def list_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE status IN ({placeholders(statuses)})
                ORDER BY priority DESC, deadline IS NULL, deadline, created_at
                """,
                statuses,
            )
        )

    def list_overdue(self, now: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM tasks
                WHERE deadline IS NOT NULL
                  AND deadline < ?
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY deadline, priority DESC
                """,
                (now,),
            )
        )

    def list_deadlines_between(self, start: str, end: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM tasks
                WHERE deadline >= ? AND deadline <= ?
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY deadline
                """,
                (start, end),
            )
        )

    def mark_started(self, task_id: str, now: str | None = None) -> dict:
        now = now or now_utc()
        task = self.get(task_id)
        return self.update(
            task_id,
            now=now,
            status="in_progress",
            started_at=task["started_at"] or now,
        )

    def mark_completed(
        self,
        task_id: str,
        actual_minutes: int | None = None,
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        changes = {"status": "completed", "completed_at": now}
        if actual_minutes is not None:
            changes["actual_minutes"] = actual_minutes
        return self.update(task_id, now=now, **changes)

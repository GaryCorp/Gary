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

ACTIVE_PROJECT_STATUSES = ("planned", "active", "blocked")


class ProjectRepository:
    UPDATABLE = frozenset(
        {
            "name",
            "objective",
            "status",
            "priority",
            "deadline",
            "completed_at",
            "metadata_json",
        }
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        name: str,
        objective: str,
        priority: int = 5,
        deadline: str | None = None,
        status: str = "active",
        metadata: dict | None = None,
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        project_id = new_id()
        insert_row(
            self.conn,
            "projects",
            {
                "id": project_id,
                "name": name,
                "objective": objective,
                "status": status,
                "priority": priority,
                "deadline": deadline,
                "created_at": now,
                "updated_at": now,
                "metadata_json": to_json(metadata),
            },
        )
        return self.get(project_id)

    def get(self, project_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        )

    def list_active(self) -> list[dict]:
        return self.list_by_status(ACTIVE_PROJECT_STATUSES)

    def list_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM projects
                WHERE status IN ({marks})
                ORDER BY priority DESC, deadline IS NULL, deadline, name
                """,
                statuses,
            )
        )

    def list_all(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM projects ORDER BY priority DESC, updated_at DESC"
            )
        )

    def update(self, project_id: str, now: str | None = None, **changes) -> dict:
        changes["updated_at"] = now or now_utc()
        update_columns(
            self.conn,
            "projects",
            project_id,
            changes,
            self.UPDATABLE | {"updated_at"},
        )
        return self.get(project_id)

    def mark_completed(self, project_id: str, now: str | None = None) -> dict:
        now = now or now_utc()
        return self.update(project_id, now=now, status="completed", completed_at=now)

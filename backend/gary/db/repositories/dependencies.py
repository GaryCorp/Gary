import sqlite3

from gary.db.repositories.base import now_utc, rows_to_dicts


class DependencyRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, task_id: str, depends_on_task_id: str, now: str | None = None) -> bool:
        """Returns False if the dependency already existed."""
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO task_dependencies
                (task_id, depends_on_task_id, created_at)
            VALUES (?, ?, ?)
            """,
            (task_id, depends_on_task_id, now or now_utc()),
        )
        return cursor.rowcount == 1

    def remove(self, task_id: str, depends_on_task_id: str) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM task_dependencies
            WHERE task_id = ? AND depends_on_task_id = ?
            """,
            (task_id, depends_on_task_id),
        )
        return cursor.rowcount == 1

    def list_dependencies(self, task_id: str) -> list[dict]:
        """Tasks that ``task_id`` depends on."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT t.id, t.title, t.status
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id = ?
                ORDER BY t.title
                """,
                (task_id,),
            )
        )

    def list_dependents(self, task_id: str) -> list[dict]:
        """Tasks that depend on ``task_id``."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT t.id, t.title, t.status
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.task_id
                WHERE d.depends_on_task_id = ?
                ORDER BY t.title
                """,
                (task_id,),
            )
        )

    def all_edges(self) -> list[tuple[str, str]]:
        return [
            (row["task_id"], row["depends_on_task_id"])
            for row in self.conn.execute(
                "SELECT task_id, depends_on_task_id FROM task_dependencies"
            )
        ]

    def depends_on_transitively(self, start_task_id: str, target_task_id: str) -> bool:
        """True if ``start_task_id`` already depends, directly or indirectly,
        on ``target_task_id``."""
        row = self.conn.execute(
            """
            WITH RECURSIVE upstream(id) AS (
                SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?
                UNION
                SELECT d.depends_on_task_id
                FROM task_dependencies d
                JOIN upstream u ON d.task_id = u.id
            )
            SELECT 1 FROM upstream WHERE id = ? LIMIT 1
            """,
            (start_task_id, target_task_id),
        ).fetchone()
        return row is not None

    def open_downstream_of(self, task_id: str) -> list[dict]:
        """Unfinished tasks that depend, directly or indirectly, on ``task_id``."""
        return rows_to_dicts(
            self.conn.execute(
                """
                WITH RECURSIVE downstream(id) AS (
                    SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ?
                    UNION
                    SELECT d.task_id
                    FROM task_dependencies d
                    JOIN downstream s ON d.depends_on_task_id = s.id
                )
                SELECT t.id, t.title, t.status, t.deadline
                FROM downstream s JOIN tasks t ON t.id = s.id
                WHERE t.status NOT IN ('completed', 'cancelled')
                ORDER BY t.title
                """,
                (task_id,),
            )
        )

    def task_ids_blocking_open_tasks(self) -> set[str]:
        """Tasks that at least one unfinished task depends on."""
        return {
            row["depends_on_task_id"]
            for row in self.conn.execute(
                """
                SELECT DISTINCT d.depends_on_task_id
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.task_id
                WHERE t.status NOT IN ('completed', 'cancelled')
                """
            )
        }

import sqlite3

from gary.db.repositories.base import insert_row, now_utc, row_to_dict, rows_to_dicts


class ProductionEpisodeRepository:
    """Which project is which episode of the weekly video schedule."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        episode_number: int,
        project_id: str,
        film_task_id: str,
        publish_task_id: str,
        shoot_at: str,
        publish_at: str,
        now: str | None = None,
    ) -> dict:
        insert_row(
            self.conn,
            "production_episodes",
            {
                "episode_number": episode_number,
                "project_id": project_id,
                "film_task_id": film_task_id,
                "publish_task_id": publish_task_id,
                "shoot_at": shoot_at,
                "publish_at": publish_at,
                "created_at": now or now_utc(),
            },
        )
        return self.get(episode_number)

    def get(self, episode_number: int) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM production_episodes WHERE episode_number = ?",
                (episode_number,),
            ).fetchone()
        )

    def list_all(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT e.*, p.name AS project_name, p.status AS project_status
                FROM production_episodes e JOIN projects p ON p.id = e.project_id
                ORDER BY e.episode_number
                """
            )
        )

    def list_tasks_needing_ticket(self, starts_before: str) -> list[dict]:
        """Open schedule tasks whose work starts before the cutoff and that
        have no finished GitHub issue yet. The shoot lives in the first
        episode's project, so it is included like any other stage."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT t.*, p.name AS project_name
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                JOIN production_episodes e ON e.project_id = t.project_id
                LEFT JOIN engineering_tickets k ON k.task_id = t.id
                WHERE t.status NOT IN ('completed', 'cancelled')
                  AND t.earliest_start <= ?
                  AND (k.id IS NULL OR k.sync_state != 'synced')
                ORDER BY t.earliest_start, t.deadline
                """,
                (starts_before,),
            )
        )

    def list_open_tickets_for_finished_tasks(self) -> list[dict]:
        """Production issues still open on GitHub for stages Alex already
        finished in Gary."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT k.* FROM engineering_tickets k
                JOIN tasks t ON t.id = k.task_id
                WHERE k.kind = 'production'
                  AND k.status != 'done'
                  AND k.github_issue_number IS NOT NULL
                  AND t.status = 'completed'
                """
            )
        )

    def list_unscheduled_fixed_tasks(self, now: str) -> list[dict]:
        """Filming and publishing tasks still to come that are not on the
        calendar yet. Filming is shared by a batch, so it appears once."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT DISTINCT t.*
                FROM production_episodes e
                JOIN tasks t ON t.id IN (e.film_task_id, e.publish_task_id)
                WHERE t.calendar_event_id IS NULL
                  AND t.status NOT IN ('completed', 'cancelled')
                  AND t.earliest_start > ?
                ORDER BY t.earliest_start
                """,
                (now,),
            )
        )

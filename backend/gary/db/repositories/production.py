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

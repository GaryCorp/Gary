import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
    to_json,
)


class PlanningRunRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start(
        self,
        planning_type: str,
        input_summary: str | None = None,
        now: str | None = None,
    ) -> dict:
        run_id = new_id()
        insert_row(
            self.conn,
            "planning_runs",
            {
                "id": run_id,
                "started_at": now or now_utc(),
                "planning_type": planning_type,
                "input_summary": input_summary,
                "status": "running",
            },
        )
        return self.get(run_id)

    def get(self, run_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM planning_runs WHERE id = ?", (run_id,)
            ).fetchone()
        )

    def complete(self, run_id: str, plan: dict, now: str | None = None) -> dict:
        self.conn.execute(
            """
            UPDATE planning_runs
            SET status = 'completed', plan_json = ?, completed_at = ?
            WHERE id = ?
            """,
            (to_json(plan), now or now_utc(), run_id),
        )
        return self.get(run_id)

    def fail_stale(self, started_before: str, now: str | None = None) -> int:
        cursor = self.conn.execute(
            """
            UPDATE planning_runs
            SET status = 'failed', completed_at = ?,
                error_message = 'No plan was recorded for this planning run'
            WHERE status = 'running' AND started_at < ?
            """,
            (now or now_utc(), started_before),
        )
        return cursor.rowcount

    def list_recent(self, limit: int = 10) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM planning_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        )

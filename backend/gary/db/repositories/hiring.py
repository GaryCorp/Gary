import json
import sqlite3

from gary.db.repositories.base import insert_row, now_utc, row_to_dict, rows_to_dicts


class HiredEmployeeRepository:
    """Employees GaryCorp hired for itself. Identity only: what they may do
    is re-derived from code every time the roster loads."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def hire(
        self,
        agent_id: str,
        name: str,
        title: str,
        department: str,
        notebook: str,
        specialty: str,
        capability_gap: str,
        allowed_tools: tuple[str, ...],
        personality: str | None = None,
        proposed_by: str = "gary",
        action_id: str | None = None,
        approval_id: str | None = None,
        approved_by: str | None = None,
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        insert_row(
            self.conn,
            "hired_employees",
            {
                "agent_id": agent_id,
                "name": name,
                "title": title,
                "department": department,
                "notebook": notebook,
                "specialty": specialty,
                "personality": personality,
                "capability_gap": capability_gap,
                "allowed_tools": json.dumps(list(allowed_tools)),
                "status": "active",
                "proposed_by": proposed_by,
                "action_id": action_id,
                "approval_id": approval_id,
                "approved_by": approved_by,
                "created_at": now,
                "updated_at": now,
            },
        )
        return self.get(agent_id)

    def get(self, agent_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM hired_employees WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        )

    def list_active(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM hired_employees WHERE status = 'active' ORDER BY created_at"
            )
        )

    def list_all(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute("SELECT * FROM hired_employees ORDER BY created_at")
        )

    def taken_notebooks(self) -> set[str]:
        return {
            row["notebook"].casefold()
            for row in self.conn.execute(
                "SELECT notebook FROM hired_employees WHERE status = 'active'"
            )
        }

    def deactivate(self, agent_id: str, now: str | None = None) -> bool:
        now = now or now_utc()
        cursor = self.conn.execute(
            """
            UPDATE hired_employees
            SET status = 'deactivated', deactivated_at = ?, updated_at = ?
            WHERE agent_id = ? AND status = 'active'
            """,
            (now, now, agent_id),
        )
        return cursor.rowcount > 0

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

TERMINAL_ASSIGNMENT_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_ASSIGNMENT_STATUSES = ("queued", "running")


class AgentRepository:
    """Org chart mirror of the code registry (identity only, not permissions)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(
        self,
        agent_id: str,
        name: str,
        title: str,
        department: str,
        reports_to: str | None,
        is_employee: bool,
        active: bool,
        now: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO agents
                (id, name, title, department, reports_to, is_employee, active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                title = excluded.title,
                department = excluded.department,
                reports_to = excluded.reports_to,
                is_employee = excluded.is_employee,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                agent_id,
                name,
                title,
                department,
                reports_to,
                int(is_employee),
                int(active),
                now or now_utc(),
            ),
        )

    def deactivate_missing(self, keep_ids: list[str], now: str | None = None) -> None:
        self.conn.execute(
            f"UPDATE agents SET active = 0, updated_at = ? WHERE id NOT IN ({placeholders(keep_ids)})",
            (now or now_utc(), *keep_ids),
        )

    def list_all(self) -> list[dict]:
        return rows_to_dicts(self.conn.execute("SELECT * FROM agents ORDER BY is_employee, name"))


class AssignmentRepository:
    UPDATABLE = frozenset(
        {"status", "started_at", "completed_at", "result_json", "error_message"}
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        assigned_by: str,
        assigned_to: str,
        objective: str,
        context: dict | None = None,
        priority: int = 5,
        project_id: str | None = None,
        task_id: str | None = None,
        review_id: str | None = None,
        review_round: int = 1,
        now: str | None = None,
    ) -> dict:
        assignment_id = new_id()
        insert_row(
            self.conn,
            "agent_assignments",
            {
                "id": assignment_id,
                "assigned_by": assigned_by,
                "assigned_to": assigned_to,
                "project_id": project_id,
                "task_id": task_id,
                "review_id": review_id,
                "review_round": review_round,
                "objective": objective,
                "context_json": to_json(context),
                "priority": priority,
                "created_at": now or now_utc(),
            },
        )
        return self.get(assignment_id)

    def get(self, assignment_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM agent_assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        )

    def transition(self, assignment_id: str, from_status: str, to_status: str, **changes) -> bool:
        """Compare-and-set status change so an assignment cannot run twice."""
        unknown = set(changes) - self.UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update agent_assignments columns: {sorted(unknown)}")
        assignments = ", ".join(f"{column} = ?" for column in ("status", *changes))
        cursor = self.conn.execute(
            f"UPDATE agent_assignments SET {assignments} WHERE id = ? AND status = ?",
            (to_status, *changes.values(), assignment_id, from_status),
        )
        return cursor.rowcount == 1

    def update(self, assignment_id: str, **changes) -> dict:
        update_columns(self.conn, "agent_assignments", assignment_id, changes, self.UPDATABLE)
        return self.get(assignment_id)

    def list_recent(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM agent_assignments
                WHERE (? IS NULL OR assigned_to = ?)
                  AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (agent_id, agent_id, status, status, limit),
            )
        )

    def list_for_review(self, review_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM agent_assignments
                WHERE review_id = ?
                ORDER BY review_round, created_at
                """,
                (review_id,),
            )
        )

    def list_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM agent_assignments
                WHERE status IN ({placeholders(statuses)})
                ORDER BY created_at
                """,
                statuses,
            )
        )

    def count_active(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM agent_assignments WHERE status IN ('queued', 'running')"
        ).fetchone()[0]

    def search_completed(self, agent_id: str, words: list[str], limit: int = 5) -> list[dict]:
        rows = rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM agent_assignments
                WHERE assigned_to = ? AND status = 'completed'
                ORDER BY completed_at DESC
                LIMIT 50
                """,
                (agent_id,),
            )
        )
        if words:
            rows = [
                row
                for row in rows
                if any(word in row["objective"].casefold() for word in words)
            ]
        return rows[:limit]

    def counts_by_agent(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(
            "SELECT assigned_to, status, COUNT(*) AS n FROM agent_assignments GROUP BY assigned_to, status"
        ):
            counts.setdefault(row["assigned_to"], {})[row["status"]] = row["n"]
        return counts


class ManagementReviewRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        topic: str,
        requested_by: str,
        project_id: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        review_id = new_id()
        insert_row(
            self.conn,
            "management_reviews",
            {
                "id": review_id,
                "topic": topic,
                "requested_by": requested_by,
                "project_id": project_id,
                "task_id": task_id,
                "created_at": now or now_utc(),
            },
        )
        return self.get(review_id)

    def get(self, review_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM management_reviews WHERE id = ?", (review_id,)
            ).fetchone()
        )

    def latest(self) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM management_reviews ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        )

    def list_recent(self, limit: int = 10) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM management_reviews ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        )

    def use_follow_up(self, review_id: str, limit: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE management_reviews SET follow_ups_used = follow_ups_used + 1
            WHERE id = ? AND follow_ups_used < ?
            """,
            (review_id, limit),
        )
        return cursor.rowcount == 1

    def set_status(self, review_id: str, status: str, completed_at: str | None) -> None:
        self.conn.execute(
            "UPDATE management_reviews SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, review_id),
        )


class AgentRunRepository:
    UPDATABLE = frozenset(
        {
            "completed_at",
            "status",
            "model",
            "attempts",
            "tool_calls",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "result_summary",
            "error_message",
        }
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start(self, assignment_id: str, agent_id: str, model: str | None, now: str | None = None) -> dict:
        run_id = new_id()
        insert_row(
            self.conn,
            "agent_runs",
            {
                "id": run_id,
                "assignment_id": assignment_id,
                "agent_id": agent_id,
                "started_at": now or now_utc(),
                "model": model,
            },
        )
        return self.get(run_id)

    def get(self, run_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        )

    def update(self, run_id: str, **changes) -> dict:
        update_columns(self.conn, "agent_runs", run_id, changes, self.UPDATABLE)
        return self.get(run_id)

    def list_for_assignment(self, assignment_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM agent_runs WHERE assignment_id = ? ORDER BY started_at",
                (assignment_id,),
            )
        )

    def fail_running(self, error: str, now: str | None = None) -> int:
        cursor = self.conn.execute(
            """
            UPDATE agent_runs SET status = 'failed', completed_at = ?, error_message = ?
            WHERE status = 'running'
            """,
            (now or now_utc(), error),
        )
        return cursor.rowcount

import json
import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    placeholders,
    row_to_dict,
    rows_to_dicts,
    update_columns,
)

OPEN_TICKET_STATUSES = ("backlog", "ready", "in_progress", "review", "security_review", "blocked")


class EngineeringTicketRepository:
    """Engineering tickets: one per Gary task, holding the GitHub identifiers."""

    UPDATABLE = frozenset(
        {
            "github_issue_number",
            "github_issue_node_id",
            "github_url",
            "github_project_id",
            "github_project_item_id",
            "assigned_to",
            "assignment_confirmed",
            "priority",
            "status",
            "security_review_required",
            "security_reviewed_at",
            "sync_state",
            "sync_error",
            "last_synced_at",
        }
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        task_id: str,
        owner: str,
        repository: str,
        assigned_to: str,
        priority: str,
        status: str = "backlog",
        security_review_required: bool = False,
        kind: str = "engineering",
        now: str | None = None,
    ) -> dict:
        now = now or now_utc()
        ticket_id = new_id()
        insert_row(
            self.conn,
            "engineering_tickets",
            {
                "id": ticket_id,
                "task_id": task_id,
                "github_owner": owner,
                "github_repository": repository,
                "assigned_to": assigned_to,
                "priority": priority,
                "status": status,
                "security_review_required": int(security_review_required),
                "kind": kind,
                "sync_state": "pending",
                "created_at": now,
                "updated_at": now,
            },
        )
        return self.get(ticket_id)

    def get(self, ticket_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM engineering_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        )

    def get_for_task(self, task_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM engineering_tickets WHERE task_id = ?", (task_id,)
            ).fetchone()
        )

    def get_by_issue(self, owner: str, repository: str, number: int) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                """
                SELECT * FROM engineering_tickets
                WHERE github_owner = ? AND github_repository = ? AND github_issue_number = ?
                """,
                (owner, repository, number),
            ).fetchone()
        )

    def update(self, ticket_id: str, now: str | None = None, **changes) -> dict:
        changes["updated_at"] = now or now_utc()
        update_columns(
            self.conn, "engineering_tickets", ticket_id, changes, self.UPDATABLE | {"updated_at"}
        )
        return self.get(ticket_id)

    def list_all(self, limit: int = 50) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM engineering_tickets ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        )

    def list_by_status(self, statuses: tuple[str, ...], limit: int = 50) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM engineering_tickets
                WHERE status IN ({placeholders(statuses)})
                ORDER BY created_at DESC LIMIT ?
                """,
                (*statuses, limit),
            )
        )

    def list_open(self, limit: int = 50) -> list[dict]:
        return self.list_by_status(OPEN_TICKET_STATUSES, limit)

    def count_created_since(self, since: str) -> int:
        """How many tickets have been opened since a point in time, for the
        cap on how much work Gary may assign unattended."""
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM engineering_tickets WHERE created_at >= ?",
            (since,),
        ).fetchone()["n"]

    def list_syncable(self, limit: int = 100) -> list[dict]:
        """Tickets with a GitHub issue that are not finished: what a sync run
        needs to look at."""
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM engineering_tickets
                WHERE github_issue_number IS NOT NULL
                  AND (status IN ({placeholders(OPEN_TICKET_STATUSES)})
                       OR sync_state IN ('pending', 'degraded', 'needs_reconciliation'))
                ORDER BY COALESCE(last_synced_at, created_at) LIMIT ?
                """,
                (*OPEN_TICKET_STATUSES, limit),
            )
        )

    def list_incomplete(self, limit: int = 50) -> list[dict]:
        """Tickets whose GitHub setup did not finish, for retry."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM engineering_tickets
                WHERE sync_state IN ('pending', 'degraded')
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            )
        )


class GitHubProjectFieldRepository:
    """Cached, non-secret Project field and option node ids."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self, project_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT fields_json FROM github_project_fields WHERE project_id = ?", (project_id,)
        ).fetchone()
        return json.loads(row["fields_json"]) if row else None

    def save(self, project_id: str, fields: dict, now: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO github_project_fields (project_id, fields_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                fields_json = excluded.fields_json,
                updated_at = excluded.updated_at
            """,
            (project_id, json.dumps(fields, sort_keys=True), now or now_utc()),
        )

    def clear(self, project_id: str) -> None:
        self.conn.execute("DELETE FROM github_project_fields WHERE project_id = ?", (project_id,))

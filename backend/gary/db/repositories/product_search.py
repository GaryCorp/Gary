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

ACTIVE_SEARCH_STATUS = "running"
TERMINAL_SEARCH_STATUSES = ("completed", "stopped", "failed")


class ProductSearchRepository:
    """The multi-round hunt for a product to build, and its rounds."""

    UPDATABLE = frozenset(
        {
            "status",
            "rounds_completed",
            "shortlist_json",
            "open_questions_json",
            "best_idea",
            "best_score",
            "stop_reason",
            "updated_at",
            "completed_at",
        }
    )
    ROUND_UPDATABLE = frozenset(
        {"assignment_id", "status", "ideas_considered", "top_idea", "top_score", "completed_at"}
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------------------------------------------------------- search

    def create(
        self,
        brief: str,
        max_rounds: int,
        started_by: str,
        constraints: dict | None = None,
        now: str | None = None,
    ) -> dict:
        search_id = new_id()
        stamp = now or now_utc()
        insert_row(
            self.conn,
            "product_searches",
            {
                "id": search_id,
                "brief": brief,
                "constraints_json": to_json(constraints),
                "max_rounds": max_rounds,
                "started_by": started_by,
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        return self.get(search_id)

    def get(self, search_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute("SELECT * FROM product_searches WHERE id = ?", (search_id,)).fetchone()
        )

    def latest(self) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM product_searches ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        )

    def active(self) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM product_searches WHERE status = ? ORDER BY created_at LIMIT 1",
                (ACTIVE_SEARCH_STATUS,),
            ).fetchone()
        )

    def list_active(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM product_searches WHERE status = ? ORDER BY created_at",
                (ACTIVE_SEARCH_STATUS,),
            )
        )

    def list_recent(self, limit: int = 5) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM product_searches ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        )

    def update(self, search_id: str, **changes) -> dict:
        changes.setdefault("updated_at", now_utc())
        update_columns(self.conn, "product_searches", search_id, changes, self.UPDATABLE)
        return self.get(search_id)

    def finish(self, search_id: str, status: str, stop_reason: str, now: str | None = None) -> bool:
        """Compare-and-set, so a search is only ended once however many
        callers notice at the same moment."""
        stamp = now or now_utc()
        cursor = self.conn.execute(
            """
            UPDATE product_searches
            SET status = ?, stop_reason = ?, completed_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (status, stop_reason, stamp, stamp, search_id, ACTIVE_SEARCH_STATUS),
        )
        return cursor.rowcount == 1

    # ---------------------------------------------------------------- rounds

    def start_round(self, search_id: str, round_number: int, brief: str, now: str | None = None) -> dict:
        """Claim a round number. The UNIQUE constraint is what stops a
        restart, or two callers, running the same round twice."""
        round_id = new_id()
        insert_row(
            self.conn,
            "product_search_rounds",
            {
                "id": round_id,
                "search_id": search_id,
                "round_number": round_number,
                "brief": brief,
                "created_at": now or now_utc(),
            },
        )
        return self.get_round(round_id)

    def get_round(self, round_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM product_search_rounds WHERE id = ?", (round_id,)
            ).fetchone()
        )

    def round_for_assignment(self, assignment_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM product_search_rounds WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
        )

    def list_rounds(self, search_id: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                "SELECT * FROM product_search_rounds WHERE search_id = ? ORDER BY round_number",
                (search_id,),
            )
        )

    def open_round(self, search_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                """
                SELECT * FROM product_search_rounds
                WHERE search_id = ? AND status = 'running'
                ORDER BY round_number DESC LIMIT 1
                """,
                (search_id,),
            ).fetchone()
        )

    def update_round(self, round_id: str, **changes) -> dict:
        update_columns(
            self.conn, "product_search_rounds", round_id, changes, self.ROUND_UPDATABLE
        )
        return self.get_round(round_id)

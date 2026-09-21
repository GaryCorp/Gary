import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
    to_json,
)


class PerformanceReviewRepository:
    """Reviews of employees, of the principal, and of Gary himself.

    A review of Gary is not finished when it is written: he has to answer it,
    and ``acknowledge`` is what records that he did.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        subject: str,
        subject_kind: str,
        reviewer: str,
        period_start: str,
        period_end: str,
        scorecard: dict,
        summary: str,
        strengths: list[str] | None = None,
        concerns: list[str] | None = None,
        recommendations: list[str] | None = None,
        evidence: list[str] | None = None,
        model: str | None = None,
        requested_by: str = "gary",
        now: str | None = None,
    ) -> dict:
        review_id = new_id()
        insert_row(
            self.conn,
            "performance_reviews",
            {
                "id": review_id,
                "subject": subject,
                "subject_kind": subject_kind,
                "reviewer": reviewer,
                "period_start": period_start,
                "period_end": period_end,
                "scorecard_json": to_json(scorecard),
                "summary": summary,
                "strengths_json": to_json(strengths or []),
                "concerns_json": to_json(concerns or []),
                "recommendations_json": to_json(recommendations or []),
                "evidence_json": to_json(evidence or []),
                "model": model,
                "requested_by": requested_by,
                "created_at": now or now_utc(),
            },
        )
        return self.get(review_id)

    def get(self, review_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM performance_reviews WHERE id = ?", (review_id,)
            ).fetchone()
        )

    def latest_for(self, subject: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                """
                SELECT * FROM performance_reviews
                WHERE subject = ? ORDER BY created_at DESC LIMIT 1
                """,
                (subject,),
            ).fetchone()
        )

    def list_for(self, subject: str, limit: int = 10) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM performance_reviews
                WHERE subject = ? ORDER BY created_at DESC LIMIT ?
                """,
                (subject, limit),
            )
        )

    def list_since(self, since: str, limit: int = 50) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM performance_reviews
                WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?
                """,
                (since, limit),
            )
        )

    def list_unacknowledged_of_manager(self) -> list[dict]:
        """What the team said about Gary that he has not answered."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM performance_reviews
                WHERE subject_kind = 'manager' AND acknowledged_at IS NULL
                ORDER BY created_at
                """
            )
        )

    def acknowledge(self, review_id: str, response: str, now: str | None = None) -> bool:
        return (
            self.conn.execute(
                """
                UPDATE performance_reviews
                SET acknowledged_at = ?, acknowledgement = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (now or now_utc(), response, review_id),
            ).rowcount
            > 0
        )

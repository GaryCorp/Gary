import sqlite3

from gary.db.repositories.base import (
    insert_row,
    new_id,
    now_utc,
    row_to_dict,
    rows_to_dicts,
)

# Statuses where Gary has decided to speak but Alex has not settled it yet.
OPEN_STATUSES = ("pending", "spoken")


class SpokenMessageRepository:
    """What Gary said out loud, and what he is still waiting to hear back.

    A row exists before anything is spoken, so nothing is lost when the voice
    service is down; ``mark_spoken`` is only called once a client received it.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        text: str,
        kind: str = "notice",
        source: str = "voice",
        expects_reply: bool = False,
        topic_key: str = "",
        approval_id: str | None = None,
        action_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        message_id = new_id()
        insert_row(
            self.conn,
            "spoken_messages",
            {
                "id": message_id,
                "kind": kind,
                "text": text,
                "expects_reply": 1 if expects_reply else 0,
                "source": source,
                "topic_key": topic_key,
                "approval_id": approval_id,
                "action_id": action_id,
                "project_id": project_id,
                "task_id": task_id,
                "created_at": now or now_utc(),
            },
        )
        return self.get(message_id)

    def get(self, message_id: str) -> dict | None:
        return row_to_dict(
            self.conn.execute(
                "SELECT * FROM spoken_messages WHERE id = ?", (message_id,)
            ).fetchone()
        )

    def get_by_approval(self, approval_id: str) -> dict | None:
        """One approval is raised with Alex once, however often it is seen."""
        return row_to_dict(
            self.conn.execute(
                """
                SELECT * FROM spoken_messages
                WHERE approval_id = ?
                ORDER BY created_at
                LIMIT 1
                """,
                (approval_id,),
            ).fetchone()
        )

    def list_pending(self, limit: int = 20) -> list[dict]:
        """Decided but not yet said: what the delivery pass speaks next."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM spoken_messages
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT ?
                """,
                (limit,),
            )
        )

    def list_awaiting_answer(self) -> list[dict]:
        """Questions already asked that Alex has not answered."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM spoken_messages
                WHERE status = 'spoken' AND expects_reply = 1
                ORDER BY created_at
                """
            )
        )

    def list_open(self) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM spoken_messages
                WHERE status IN ({", ".join("?" for _ in OPEN_STATUSES)})
                ORDER BY created_at
                """,
                OPEN_STATUSES,
            )
        )

    def list_recent(self, since: str, limit: int = 20) -> list[dict]:
        """What Gary has said lately, newest first, for reading back."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM spoken_messages
                WHERE spoken_at IS NOT NULL AND spoken_at >= ?
                ORDER BY spoken_at DESC
                LIMIT ?
                """,
                (since, limit),
            )
        )

    def count_created_since(self, since: str, sources: tuple[str, ...]) -> int:
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM spoken_messages
            WHERE created_at >= ?
            AND source IN ({", ".join("?" for _ in sources)})
            """,
            (since, *sources),
        ).fetchone()
        return row["n"]

    def open_topic_keys(self) -> list[str]:
        return [
            row["topic_key"]
            for row in self.conn.execute(
                f"""
                SELECT topic_key FROM spoken_messages
                WHERE status IN ({", ".join("?" for _ in OPEN_STATUSES)})
                AND topic_key != ''
                """,
                OPEN_STATUSES,
            )
        ]

    def mark_spoken(self, message_id: str, now: str | None = None) -> bool:
        """Only after a voice client actually received it."""
        at = now or now_utc()
        return (
            self.conn.execute(
                """
                UPDATE spoken_messages
                SET status = 'spoken', spoken_at = ?, last_spoken_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (at, at, message_id),
            ).rowcount
            > 0
        )

    def record_repeat(self, message_id: str, now: str | None = None) -> bool:
        """Alex asked to hear it again."""
        return (
            self.conn.execute(
                """
                UPDATE spoken_messages
                SET repeat_count = repeat_count + 1, last_spoken_at = ?
                WHERE id = ? AND spoken_at IS NOT NULL
                """,
                (now or now_utc(), message_id),
            ).rowcount
            > 0
        )

    def answer(self, message_id: str, answer: str, now: str | None = None) -> bool:
        return (
            self.conn.execute(
                """
                UPDATE spoken_messages
                SET status = 'answered', answered_at = ?, answer = ?
                WHERE id = ? AND status = 'spoken'
                """,
                (now or now_utc(), answer, message_id),
            ).rowcount
            > 0
        )

    def list_unmirrored(self, limit: int = 20) -> list[dict]:
        """Said out loud but not yet written to the Joplin note."""
        return rows_to_dicts(
            self.conn.execute(
                """
                SELECT * FROM spoken_messages
                WHERE spoken_at IS NOT NULL AND joplin_written_at IS NULL
                ORDER BY spoken_at
                LIMIT ?
                """,
                (limit,),
            )
        )

    def mark_mirrored(
        self, message_id: str, note_id: str | None, now: str | None = None
    ) -> bool:
        return (
            self.conn.execute(
                """
                UPDATE spoken_messages
                SET joplin_note_id = ?, joplin_written_at = ?
                WHERE id = ? AND joplin_written_at IS NULL
                """,
                (note_id, now or now_utc(), message_id),
            ).rowcount
            > 0
        )

    def list_open_created_before(self, cutoff: str) -> list[dict]:
        return rows_to_dicts(
            self.conn.execute(
                f"""
                SELECT * FROM spoken_messages
                WHERE status IN ({", ".join("?" for _ in OPEN_STATUSES)})
                AND created_at < ?
                ORDER BY created_at
                """,
                (*OPEN_STATUSES, cutoff),
            )
        )

    def expire(self, message_id: str, now: str | None = None) -> bool:
        return (
            self.conn.execute(
                f"""
                UPDATE spoken_messages
                SET status = 'expired'
                WHERE id = ? AND status IN ({", ".join("?" for _ in OPEN_STATUSES)})
                """,
                (message_id, *OPEN_STATUSES),
            ).rowcount
            > 0
        )

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=5,
        check_same_thread=False,
        # Autocommit mode: transactions are always explicit (BEGIN ... COMMIT),
        # so the sqlite3 module never opens or commits one implicitly.
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    return conn


class Database:
    """Opens a short-lived connection per unit of work.

    Never hold a transaction open across an LLM call or HTTP request: read
    state, close the transaction, act, then record the result in a new one.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            # Owner-only, like the Google token store. SQLite gives the -wal
            # and -shm files the same permissions as the database file.
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        return get_connection(self.path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write: everything inside commits together or not at all.

        BEGIN IMMEDIATE takes the write lock up front, so checks made inside
        the transaction (e.g. dependency cycles) cannot race another writer.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Consistent snapshot for several related reads."""
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("ROLLBACK")
        finally:
            conn.close()

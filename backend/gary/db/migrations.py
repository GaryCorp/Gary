"""Versioned schema migrations.

Migrations live in ``gary/db/migrations/NNN_name.sql`` and are applied in
order, each in its own transaction together with its ``schema_migrations``
row. A failing migration rolls back completely. Startup never recreates or
drops the database.
"""

import re
import sqlite3
from pathlib import Path

from gary.db.connection import Database
from gary.timeutil import format_utc, utc_now

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def split_statements(sql: str) -> list[str]:
    # executescript() commits implicitly, which would break per-migration
    # atomicity, so statements are executed one at a time instead.
    statements, current = [], ""
    for line in sql.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError("Migration ends with an incomplete SQL statement")
    return statements


def available_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.match(path.name)
        if not match:
            raise ValueError(f"Badly named migration file: {path.name}")
        migrations.append((int(match.group(1)), path))

    versions = [version for version, _ in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Two migrations share a version number")
    return migrations


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def get_schema_version(db: Database) -> int:
    conn = db.connect()
    try:
        _ensure_migrations_table(conn)
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return row["version"]
    finally:
        conn.close()


def apply_migrations(db: Database, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply unapplied migrations in order. Returns the versions applied."""
    current_version = get_schema_version(db)
    applied = []

    for version, path in available_migrations(directory):
        if version <= current_version:
            continue

        statements = split_statements(path.read_text())
        with db.transaction() as conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, format_utc(utc_now())),
            )
        applied.append(version)

    return applied

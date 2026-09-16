"""Shared repository helpers.

Repositories hold SQL only: no business rules and no LLM logic. Every value is
bound as a parameter. Table and column names are code constants, never input:
``update_columns`` only accepts names from a repository's allowlist.
"""

import json
import sqlite3
from uuid import uuid4

from gary.timeutil import format_utc, utc_now


def new_id() -> str:
    return str(uuid4())


def now_utc() -> str:
    return format_utc(utc_now())


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def to_json(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def insert_row(conn: sqlite3.Connection, table: str, values: dict) -> None:
    columns = ", ".join(values)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders(values)})",
        tuple(values.values()),
    )


def update_columns(
    conn: sqlite3.Connection,
    table: str,
    row_id: str,
    changes: dict,
    allowed: frozenset[str],
) -> None:
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Cannot update {table} columns: {sorted(unknown)}")
    if not changes:
        return

    assignments = ", ".join(f"{column} = ?" for column in changes)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        (*changes.values(), row_id),
    )

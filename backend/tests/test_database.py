import sqlite3
import threading

import pytest

from gary.backup import backup_daily, backup_database
from gary.db import Database, apply_migrations, get_schema_version
from gary.db.migrations import split_statements
from gary.db.repositories import Repositories

from conftest import START, make_project, make_task


def test_migrations_apply_once_and_record_version(db_path):
    db = Database(db_path)
    assert apply_migrations(db) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert get_schema_version(db) == 15
    assert apply_migrations(db) == []

    conn = db.connect()
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {
        "projects", "tasks", "task_dependencies", "followups", "commitments",
        "approvals", "actions", "audit_log", "planning_runs", "schema_migrations",
        "agents", "agent_assignments", "agent_runs", "management_reviews", "payment_cards",
        "engineering_tickets", "github_project_fields", "model_usage", "hired_employees",
        "spoken_messages", "performance_reviews",
    } <= tables


def test_failed_migration_rolls_back_completely(db_path, tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_good.sql").write_text("CREATE TABLE a (id TEXT PRIMARY KEY);")
    (migrations / "002_bad.sql").write_text(
        "CREATE TABLE b (id TEXT PRIMARY KEY);\nINSERT INTO missing_table VALUES (1);"
    )
    db = Database(db_path)

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db, migrations)

    assert get_schema_version(db) == 1
    conn = db.connect()
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "a" in tables
    assert "b" not in tables  # table from the failed migration was rolled back


def test_split_statements_keeps_triggers_whole():
    sql = """
    CREATE TABLE t (id INTEGER);
    CREATE TRIGGER x BEFORE DELETE ON t
    BEGIN
        SELECT RAISE(ABORT, 'no');
    END;
    """
    assert len(split_statements(sql)) == 2


def test_database_and_backups_are_owner_only(gary, db_path, tmp_path):
    make_task(gary)
    assert db_path.stat().st_mode & 0o777 == 0o600
    backup = backup_daily(db_path, tmp_path / "backups", START.date())
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700


def test_connection_pragmas(gary):
    conn = gary.db.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_foreign_keys_are_enforced(gary):
    with pytest.raises(sqlite3.IntegrityError):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).tasks.create("Orphan", project_id="no-such-project")


def test_foreign_key_on_delete_behavior(gary):
    # Hard deletes are admin-only; this checks the schema's ON DELETE rules.
    project = make_project(gary)
    task = make_task(gary, project_id=project["id"])
    other = make_task(gary, title="Edit")
    with gary.db.transaction() as conn:
        repos = Repositories.bind(conn)
        repos.dependencies.add(other["id"], task["id"])
        repos.followups.create("Check", "2026-09-17T00:00:00+00:00", task_id=task["id"])
        conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))

    with gary.db.read() as conn:
        repos = Repositories.bind(conn)
        assert repos.tasks.get(task["id"])["project_id"] is None  # SET NULL

    with gary.db.transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))

    with gary.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 0  # CASCADE


def test_transaction_rolls_back_on_error(gary):
    with pytest.raises(RuntimeError):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).tasks.create("Should not persist")
            raise RuntimeError("boom")

    with gary.db.read() as conn:
        assert Repositories.bind(conn).tasks.list_open() == []


def test_check_constraints_reject_invalid_values(gary):
    with pytest.raises(sqlite3.IntegrityError):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).tasks.create("Bad", priority=11)
    task = make_task(gary)
    with pytest.raises(sqlite3.IntegrityError):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).tasks.update(task["id"], status="done")
    with pytest.raises(sqlite3.IntegrityError):
        with gary.db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_dependencies VALUES (?, ?, ?)",
                (task["id"], task["id"], "2026-01-01T00:00:00+00:00"),
            )


def test_audit_log_is_append_only(gary):
    task = make_task(gary)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with gary.db.transaction() as conn:
            conn.execute("UPDATE audit_log SET summary = 'changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with gary.db.transaction() as conn:
            conn.execute("DELETE FROM audit_log")

    with gary.db.read() as conn:
        rows = Repositories.bind(conn).audit.list_for_entity("task", task["id"])
    assert [row["event_type"] for row in rows] == ["task_created"]


def test_update_rejects_columns_outside_allowlist(gary):
    task = make_task(gary)
    with pytest.raises(ValueError, match="Cannot update"):
        with gary.db.transaction() as conn:
            Repositories.bind(conn).tasks.update(task["id"], **{"title = 'x', id": "y"})


def test_sql_injection_text_is_stored_literally(gary):
    title = "x'); DROP TABLE tasks; --"
    task = make_task(gary, title=title)
    assert gary.tasks.get_task(task["id"])["title"] == title


def test_concurrent_reads_and_writes(gary):
    errors = []

    def writer(n):
        try:
            for i in range(20):
                make_task(gary, title=f"writer {n} task {i}")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def reader():
        try:
            for _ in range(40):
                gary.tasks.list_tasks()
                gary.planning.find_overdue_tasks()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with gary.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 80
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'task_created'"
        ).fetchone()[0] == 80


def test_state_survives_restart(db_path, clock, external):
    from conftest import fake_handlers
    from gary import build_gary

    first = build_gary(db_path, action_handlers=fake_handlers(external), clock=clock)
    project = make_project(first)
    make_task(first, project_id=project["id"])

    second = build_gary(db_path, action_handlers=fake_handlers(external), clock=clock)
    assert [p["name"] for p in second.projects.list_projects()] == ["Chief of Staff video"]
    assert len(second.tasks.list_tasks(project_id=project["id"])) == 1


def test_backup_uses_backup_api(gary, db_path, tmp_path):
    make_task(gary)
    target = tmp_path / "copy.db"
    backup_database(db_path, target)
    conn = sqlite3.connect(target)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    conn.close()


def test_daily_backup_runs_once_and_prunes(gary, db_path, tmp_path):
    backups = tmp_path / "backups"
    today = START.date()
    assert backup_daily(db_path, backups, today, keep=2) is not None
    assert backup_daily(db_path, backups, today, keep=2) is None
    for day in (1, 2, 3):
        backup_daily(db_path, backups, today.replace(day=today.day + day), keep=2)
    assert len(list(backups.glob("gary-*.db"))) == 2

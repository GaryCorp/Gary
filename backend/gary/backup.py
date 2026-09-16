"""SQLite backups using the online backup API, which is safe while the
database is in use (unlike copying the file)."""

import datetime as dt
import os
import sqlite3
from pathlib import Path

BACKUP_PREFIX = "gary-"


def backup_database(source_path: str | Path, backup_path: str | Path) -> None:
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(backup_path)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()


def backup_daily(
    source_path: str | Path,
    backup_dir: str | Path,
    today: dt.date,
    keep: int = 14,
) -> Path | None:
    """Write today's backup if it does not exist yet, keeping the newest
    ``keep`` backups. Returns the new backup path, or None if already done."""
    source_path, backup_dir = Path(source_path), Path(backup_dir)
    if not source_path.exists():
        return None

    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backup_dir / f"{BACKUP_PREFIX}{today.isoformat()}.db"
    if target.exists():
        return None

    partial = target.with_suffix(".db.partial")
    os.close(os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    backup_database(source_path, partial)
    partial.replace(target)

    backups = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()

    return target

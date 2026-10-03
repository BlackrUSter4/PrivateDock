from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config.config import current as get_config
from src.db.dialect import current_dialect
from src.db.store import sqlite_path_from_dsn
from src.logger.logger import log_event, LOG_LEVEL_ERROR, LOG_LEVEL_INFO


MAX_DATABASE_BACKUPS = 10
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_backup_lock: asyncio.Lock | None = None
_backup_tasks: set[asyncio.Task] = set()


def _resolve_sqlite_path() -> Path | None:
    if current_dialect().name != "sqlite":
        return None

    cfg = get_config()
    raw_path = (cfg.database.dsn or cfg.database.path).strip()
    if not raw_path:
        return None

    path = Path(sqlite_path_from_dsn(raw_path))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def _prune_backups(directory: Path, stem: str, keep: int) -> None:
    digit = "[0-9]"
    pattern = f"{stem}_{digit * 8}_{digit * 6}_{digit * 6}.db"
    backups = sorted(directory.glob(pattern), key=lambda path: path.name, reverse=True)
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError as exc:
            log_event("DB", "Backup", f"failed to remove old backup {stale}: {exc}", LOG_LEVEL_ERROR)


def _create_sqlite_backup(
    source: Path,
    backup_dir: Path,
    max_backups: int,
    now: datetime | None = None,
) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir / f"{source.stem}_{timestamp}.db"
    temporary = backup_dir / f".{destination.name}.{os.getpid()}.tmp"

    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(str(source), timeout=30.0)
        source_conn.execute("PRAGMA busy_timeout=30000")
        source_conn.execute("PRAGMA query_only=ON")

        temporary.unlink(missing_ok=True)
        destination_conn = sqlite3.connect(str(temporary), timeout=30.0)
        source_conn.backup(destination_conn)

        check = destination_conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {check!r}")
        destination_conn.close()
        destination_conn = None
        source_conn.close()
        source_conn = None
        os.replace(temporary, destination)
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()
        temporary.unlink(missing_ok=True)

    _prune_backups(backup_dir, source.stem, max(1, max_backups))
    return destination


async def create_database_backup(reason: str, max_backups: int = MAX_DATABASE_BACKUPS) -> Path | None:
    """Create a consistent SQLite snapshot and retain only the newest backups."""
    global _backup_lock

    source = _resolve_sqlite_path()
    if source is None:
        return None

    if _backup_lock is None:
        _backup_lock = asyncio.Lock()

    async with _backup_lock:
        destination = await asyncio.to_thread(
            _create_sqlite_backup,
            source,
            source.parent / "backups",
            max_backups,
        )

    log_event("DB", "Backup", f"created database backup for {reason}: {destination}", LOG_LEVEL_INFO)
    return destination


def schedule_database_backup(reason: str) -> asyncio.Task:
    """Run a login backup without blocking packet handling."""

    async def run() -> None:
        try:
            await create_database_backup(reason)
        except Exception as exc:
            log_event("DB", "Backup", f"database backup failed for {reason}: {exc}", LOG_LEVEL_ERROR)

    task = asyncio.create_task(run())
    _backup_tasks.add(task)
    task.add_done_callback(_backup_tasks.discard)
    return task

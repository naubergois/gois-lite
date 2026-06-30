"""Harden RuFlo ``memory.db`` against corruption (lock, disk guard, WAL)."""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator, Optional

from .ruflo_memory_repair import memory_db_integrity_ok, repair_memory_db

log = logging.getLogger(__name__)

_LOCK_SUFFIX = ".lock"
_BACKUP_SUFFIX = ".bak"
_DEFAULT_MIN_FREE_MB = 512


class MemoryDbDiskLowError(RuntimeError):
    """Raised when free disk space is below the configured threshold."""


def memory_db_lock_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}{_LOCK_SUFFIX}")


def free_disk_mb(path: Path) -> float:
    if path.exists() and path.is_file():
        target = path.parent
    else:
        target = path if path.exists() else path.parent
    target.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(target).free / (1024 * 1024)


def disk_space_ok(path: Path, min_free_mb: int) -> bool:
    if min_free_mb <= 0:
        return True
    return free_disk_mb(path) >= float(min_free_mb)


@contextmanager
def memory_db_lock(db_path: Path, *, enabled: bool = True) -> Iterator[None]:
    """Cross-process exclusive lock for all readers/writers of ``memory.db``."""
    if not enabled:
        yield
        return
    db_path = db_path.expanduser().resolve()
    lock_path = memory_db_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _apply_safe_pragmas(conn: sqlite3.Connection, db_path: Path, min_free_mb: int) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    sync = "FULL" if not disk_space_ok(db_path, min_free_mb) else "NORMAL"
    conn.execute(f"PRAGMA synchronous={sync}")


@contextmanager
def memory_db_session(
    db_path: Path,
    *,
    min_free_mb: int = _DEFAULT_MIN_FREE_MB,
    lock_enabled: bool = True,
    row_factory: Any = None,
) -> Iterator[sqlite3.Connection]:
    """Locked SQLite session with safe pragmas and passive WAL checkpoint on exit."""
    db_path = db_path.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if min_free_mb > 0 and not disk_space_ok(db_path, min_free_mb):
        free = free_disk_mb(db_path)
        raise MemoryDbDiskLowError(
            f"free disk {free:.0f}MB < minimum {min_free_mb}MB for {db_path}"
        )
    with memory_db_lock(db_path, enabled=lock_enabled):
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        if row_factory is not None:
            conn.row_factory = row_factory
        try:
            _apply_safe_pragmas(conn, db_path, min_free_mb)
            yield conn
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as exc:
                log.debug("memory db wal_checkpoint skipped: %s", exc)
        finally:
            conn.close()


def checkpoint_memory_db(db_path: Path) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        return {"ok": True, "skipped": "missing"}
    try:
        with memory_db_session(db_path, min_free_mb=0) as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"ok": True, "checkpoint": list(row) if row else None}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)[:200]}


def rotate_memory_db_backup(db_path: Path, *, keep: int = 3) -> Optional[str]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file() or keep <= 0:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}{_BACKUP_SUFFIX}-{stamp}")
    shutil.copy2(db_path, backup)
    pattern = f"{db_path.name}{_BACKUP_SUFFIX}-*"
    backups = sorted(
        db_path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("failed to prune memory db backup %s: %s", old, exc)
    return str(backup)


def ensure_memory_db_ready(
    db_path: Path,
    *,
    min_free_mb: int = _DEFAULT_MIN_FREE_MB,
    auto_repair: bool = True,
) -> dict[str, Any]:
    """Verify disk + integrity; repair automatically when corrupted."""
    db_path = db_path.expanduser().resolve()
    result: dict[str, Any] = {
        "ok": True,
        "path": str(db_path),
        "free_mb": round(free_disk_mb(db_path), 1),
    }
    if min_free_mb > 0 and not disk_space_ok(db_path, min_free_mb):
        result["ok"] = False
        result["error"] = "disk_low"
        result["min_free_mb"] = min_free_mb
        log.warning(
            "ruflo memory db blocked: %.0fMB free < %dMB minimum (%s)",
            result["free_mb"],
            min_free_mb,
            db_path,
        )
        return result
    if not db_path.is_file():
        result["skipped"] = "missing"
        return result
    if memory_db_integrity_ok(db_path):
        return result
    if not auto_repair:
        result["ok"] = False
        result["error"] = "integrity_failed"
        return result
    repair = repair_memory_db(db_path)
    result["repair"] = repair
    if repair.get("repaired"):
        result["repaired"] = True
    result["ok"] = bool(repair.get("ok"))
    if not result["ok"]:
        result["error"] = repair.get("error") or "repair_failed"
    return result


def maintain_memory_db(
    db_path: Path,
    *,
    min_free_mb: int = _DEFAULT_MIN_FREE_MB,
    backup_keep: int = 3,
    auto_repair: bool = True,
) -> dict[str, Any]:
    """Periodic maintenance: ensure healthy, checkpoint WAL, rotate backup."""
    db_path = db_path.expanduser().resolve()
    result = ensure_memory_db_ready(
        db_path, min_free_mb=min_free_mb, auto_repair=auto_repair
    )
    if not result.get("ok") or not db_path.is_file():
        return result
    checkpoint = checkpoint_memory_db(db_path)
    result["checkpoint"] = checkpoint
    if checkpoint.get("ok"):
        try:
            backup = rotate_memory_db_backup(db_path, keep=backup_keep)
            if backup:
                result["backup_path"] = backup
        except OSError as exc:
            result["backup_error"] = str(exc)[:200]
            log.warning("ruflo memory db backup failed: %s", exc)
    else:
        result["ok"] = False
        result["error"] = checkpoint.get("error") or "checkpoint_failed"
    return result


def memory_cli_lock_context(
    db_path: Optional[Path],
    subargs: list[str],
    *,
    lock_enabled: bool = True,
):
    """Hold the memory db lock while RuFlo CLI touches ``memory`` subcommands."""
    if not lock_enabled or not db_path or not subargs or subargs[0] != "memory":
        return nullcontext()
    return memory_db_lock(db_path, enabled=True)

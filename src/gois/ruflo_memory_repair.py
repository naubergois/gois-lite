"""Repair corrupted RuFlo AgentDB (``.swarm/memory.db``)."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def memory_db_integrity_ok(path: Path) -> bool:
    """Return True when the DB is missing or passes ``PRAGMA integrity_check``."""
    if not path.is_file():
        return True
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row and str(row[0]).strip().lower() == "ok")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("ruflo memory integrity check failed for %s: %s", path, exc)
        return False


def repair_memory_db(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Rebuild a corrupted SQLite DB via ``.recover`` and swap in place.

    Creates a timestamped backup of the broken file before replacing it.
    """
    path = path.expanduser().resolve()
    result: dict[str, Any] = {
        "ok": False,
        "path": str(path),
        "dry_run": dry_run,
        "repaired": False,
        "backup_path": None,
        "entries_before": None,
        "entries_after": None,
        "error": None,
    }
    if not path.is_file():
        result["ok"] = True
        result["skipped"] = "missing"
        return result
    if memory_db_integrity_ok(path):
        result["ok"] = True
        result["skipped"] = "integrity_ok"
        return result

    result["entries_before"] = _count_memory_entries(path)
    if dry_run:
        result["ok"] = True
        result["would_repair"] = True
        return result

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}.bak")
    recovered = path.with_name(f"{path.name}.recovered")
    sql_dump = path.with_name(f"{path.name}.recovered.sql")

    try:
        path.rename(backup)
        result["backup_path"] = str(backup)
        for sidecar in (
            path.with_suffix(path.suffix + "-wal"),
            path.with_suffix(path.suffix + "-shm"),
        ):
            if sidecar.is_file():
                sidecar.unlink(missing_ok=True)

        sqlite3_bin = shutil.which("sqlite3")
        if not sqlite3_bin:
            raise RuntimeError("sqlite3 binary not found on PATH")

        proc = subprocess.run(
            [sqlite3_bin, str(backup), ".recover"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(
                (proc.stderr or proc.stdout or f"sqlite3 recover exit {proc.returncode}").strip()[:400]
            )
        sql_dump.write_text(proc.stdout, encoding="utf-8")

        if recovered.is_file():
            recovered.unlink()
        import_proc = subprocess.run(
            [sqlite3_bin, str(recovered)],
            input=sql_dump.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            check=False,
        )
        if import_proc.returncode != 0:
            raise RuntimeError(
                (import_proc.stderr or import_proc.stdout or f"sqlite3 import exit {import_proc.returncode}").strip()[:400]
            )
        if not recovered.is_file():
            raise RuntimeError("sqlite3 recover did not create database file")

        conn_dst = sqlite3.connect(recovered)
        try:
            row = conn_dst.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).strip().lower() != "ok":
                raise sqlite3.DatabaseError(f"recovered db failed integrity: {row}")
        finally:
            conn_dst.close()

        recovered.rename(path)
        result["entries_after"] = _count_memory_entries(path)
        result["repaired"] = True
        result["ok"] = True
        log.info(
            "repaired ruflo memory db %s (entries %s → %s, backup %s)",
            path,
            result["entries_before"],
            result["entries_after"],
            backup,
        )
    except Exception as exc:
        result["error"] = str(exc)[:400]
        log.warning("ruflo memory repair failed for %s: %s", path, exc)
        if backup.is_file() and not path.is_file():
            backup.rename(path)
    finally:
        sql_dump.unlink(missing_ok=True)
        recovered.unlink(missing_ok=True)

    return result


def _count_memory_entries(path: Path) -> int | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memory_entries'"
            ).fetchone()
            if not row or int(row[0]) == 0:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def resolve_ruflo_memory_db_path(
    *,
    swarm_memory_cfg: Any = None,
    ruflo_chat_cfg: Any = None,
    repo_root: Path | None = None,
) -> Path | None:
    """Pick the AgentDB path from swarm_memory / ruflo engine config."""
    root = (repo_root or Path.cwd()).resolve()
    raw: str | None = None
    if swarm_memory_cfg is not None and getattr(swarm_memory_cfg, "enabled", False):
        raw = getattr(swarm_memory_cfg, "agentdb_path", None)
    if not raw and ruflo_chat_cfg is not None and getattr(ruflo_chat_cfg, "enabled", False):
        raw = "./.swarm/memory.db"
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return path

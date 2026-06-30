"""Shared helpers for one-shot SQLite → MongoDB imports (non-destructive)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from .mongo import get_collection

log = logging.getLogger(__name__)


def scope_from_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def resolve_scope_path(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
    default: Callable[[], Path] | None = None,
) -> Path:
    """Resolve the legacy SQLite path used as MongoDB ``_scope`` key."""
    chosen = scope_path if scope_path is not None else db_path
    if chosen is None:
        if default is None:
            raise TypeError("scope_path or db_path required")
        chosen = default()
    return Path(chosen).expanduser().resolve()


def has_scope(collection: str, scope: str) -> bool:
    return get_collection(collection).count_documents({"_scope": scope}, limit=1) > 0


def import_sqlite_rows(
    db_path: Path,
    *,
    table: str,
    collection: str,
    scope: str,
    row_to_doc: Callable[[sqlite3.Row], dict[str, Any]],
    batch_size: int = 500,
    merge: bool = False,
    unique_fields: Optional[list[str]] = None,
    preserve_fields: Optional[list[str]] = None,
) -> int:
    """Copy rows from a SQLite table into a Mongo collection. Returns count.

    When ``merge`` is False (default), skips import if any document exists for
    ``scope``. When ``merge`` is True, upserts each row using ``unique_fields``
    (defaults to ``["id"]`` when the column exists, else all non-None columns).
    ``preserve_fields`` keeps existing Mongo values on update (e.g. ``id`` when
    upserting chat sessions by ``session_key``).
    """
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_file():
        return 0
    col = get_collection(collection)
    if not merge and col.count_documents({"_scope": scope}, limit=1) > 0:
        return 0

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()

    if not rows:
        return 0

    keys = rows[0].keys()
    fields = unique_fields
    if merge and not fields:
        if "id" in keys:
            fields = ["id"]
        elif "jid" in keys:
            fields = ["jid"]
        else:
            fields = [k for k in keys]

    if merge:
        count = 0
        for row in rows:
            doc = row_to_doc(row)
            doc["_scope"] = scope
            filt: dict[str, Any] = {"_scope": scope}
            for f in fields:
                if f not in doc:
                    break
                filt[f] = doc[f]
            else:
                if preserve_fields:
                    existing = col.find_one(filt)
                    if existing:
                        for pf in preserve_fields:
                            if pf in existing:
                                doc[pf] = existing[pf]
                col.replace_one(filt, doc, upsert=True)
                count += 1
        if count:
            log.info(
                "Merged %d row(s) from %s.%s → MongoDB %s",
                count,
                db_path.name,
                table,
                collection,
            )
        return count

    docs: list[dict[str, Any]] = []
    for row in rows:
        doc = row_to_doc(row)
        doc["_scope"] = scope
        docs.append(doc)

    for i in range(0, len(docs), batch_size):
        col.insert_many(docs[i : i + batch_size])
    log.info(
        "Imported %d row(s) from %s.%s → MongoDB %s",
        len(docs),
        db_path.name,
        table,
        collection,
    )
    return len(docs)


def import_sqlite_tables(
    db_path: Path,
    *,
    scope: str,
    tables: list[tuple[str, str, Callable[[sqlite3.Row], dict[str, Any]]]],
) -> dict[str, int]:
    """Import multiple tables; returns {table: count}."""
    out: dict[str, int] = {}
    for table, collection, transform in tables:
        out[table] = import_sqlite_rows(
            db_path,
            table=table,
            collection=collection,
            scope=scope,
            row_to_doc=transform,
        )
    return out


def mongo_regex_search(
    query: str,
    fields: list[str],
) -> dict[str, Any]:
    """Build a case-insensitive $or regex filter (FTS5 replacement)."""
    import re

    text = (query or "").strip()
    if not text:
        return {}
    pattern = re.escape(text)
    return {"$or": [{f: {"$regex": pattern, "$options": "i"}} for f in fields]}

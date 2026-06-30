"""Central MongoDB access for gois.

Single source of truth for the Mongo connection. Everything that used to live
in SQLite (.db files) or in the YAML config file now goes through here.

Connection is configured via environment:

    MONGODB_URI   connection string (default: mongodb://localhost:27017)
    MONGODB_DB    database name      (default: gois)

The client is a lazy, process-wide singleton with a short server-selection
timeout so a missing/dead mongod fails fast with a clear message instead of
hanging. ``ping()`` lets callers (tests, health checks) probe availability
without raising.

Collection and database handles returned by :func:`get_db` / :func:`get_collection`
automatically retry once on transient connection errors (same pattern as Redis).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger(__name__)

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "gois"
_MAX_MONGO_ATTEMPTS = 2

_lock = threading.Lock()
_orphan_lock = threading.Lock()
_client = None  # type: ignore[var-annotated]
_db_cache: dict[str, "_RetryDatabase"] = {}
_orphan_clients: list[Any] = []
_ORPHAN_CLOSE_DELAY_SECONDS = 30.0

T = TypeVar("T")


def mongo_uri() -> str:
    return os.environ.get("MONGODB_URI", DEFAULT_URI).strip() or DEFAULT_URI


def mongo_db_name() -> str:
    return os.environ.get("MONGODB_DB", DEFAULT_DB).strip() or DEFAULT_DB


def _mongo_connection_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)
    try:
        from pymongo.errors import (
            AutoReconnect,
            ConnectionFailure,
            InvalidOperation,
            NetworkTimeout,
            NotPrimaryError,
            ServerSelectionTimeoutError,
        )

        errors = (
            *errors,
            AutoReconnect,
            ConnectionFailure,
            InvalidOperation,
            NetworkTimeout,
            NotPrimaryError,
            ServerSelectionTimeoutError,
        )
    except ImportError:  # pragma: no cover - dependency missing
        pass
    return errors


_MONGO_CONN_ERRORS = _mongo_connection_errors()


def _client_is_open(client: Any) -> bool:
    """Return False when pymongo has already closed this client handle."""
    try:
        return not client._topology._closed
    except Exception:
        return True


def _close_orphan_client(client: Any) -> None:
    with _orphan_lock:
        try:
            _orphan_clients.remove(client)
        except ValueError:
            pass
    try:
        client.close()
    except Exception:
        pass


def _schedule_orphan_close(client: Any) -> None:
    """Close a replaced MongoClient after a grace period (avoids FD leaks)."""
    with _orphan_lock:
        _orphan_clients.append(client)
    timer = threading.Timer(
        _ORPHAN_CLOSE_DELAY_SECONDS,
        _close_orphan_client,
        args=(client,),
    )
    timer.daemon = True
    timer.start()


def _invalidate_client(*, close: bool) -> None:
    """Drop cached client/db handles.

    On transient transport errors we orphan the client without closing it so
    concurrent request threads that already hold a reference can finish. Orphans
    are closed after :data:`_ORPHAN_CLOSE_DELAY_SECONDS` to avoid socket leaks.
    Test teardown passes ``close=True`` to release sockets promptly.
    """
    global _client
    with _lock:
        old = _client
        _client = None
        _db_cache.clear()
    if old is None:
        return
    if close:
        try:
            old.close()
        except Exception:
            pass
    else:
        _schedule_orphan_close(old)


def reset_client_for_tests() -> None:
    """Drop cached client/db handles after errors or when tests swap MONGODB_URI."""
    _invalidate_client(close=True)


def _reset_client_on_error(exc: BaseException) -> bool:
    if isinstance(exc, _MONGO_CONN_ERRORS):
        log.debug("mongodb connection error — resetting client: %s", exc)
        _invalidate_client(close=False)
        _try_autostart_mongod()
        return True
    return False


def _try_autostart_mongod() -> None:
    """Best-effort: restart a local mongod so the single retry can succeed.

    Isolated in its own try/except so a failure here never masks the original
    connection error the caller is about to re-raise.
    """
    try:
        from .mongo_autostart import restart_mongod

        restart_mongod()
    except Exception:  # pragma: no cover - autostart must never raise
        log.debug("mongo autostart hook failed", exc_info=True)


def with_mongo_retry(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a Mongo operation; invalidate the cached client and retry once on transport errors."""
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_MONGO_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except _MONGO_CONN_ERRORS as exc:
            last_exc = exc
            if attempt == 0 and _reset_client_on_error(exc):
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_mongo_retry exhausted without result")


class _RetryDatabase:
    """Database proxy that retries transient errors and always uses a fresh client."""

    def __init__(self, db_name: str) -> None:
        self._db_name = db_name

    def _underlying(self):
        return get_client()[self._db_name]

    def __getitem__(self, name: str) -> "_RetryCollection":
        return _RetryCollection(name, db=self._db_name)

    def __getattr__(self, name: str) -> Any:
        if not callable(getattr(self._underlying(), name)):
            return getattr(self._underlying(), name)

        # Re-resolve against a fresh client on each call so that a retry after
        # reset_client_for_tests() does not reuse a closed MongoClient handle.
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return with_mongo_retry(
                lambda: getattr(self._underlying(), name)(*args, **kwargs)
            )

        return wrapper


class _RetryCollection:
    """Collection proxy that retries transient errors and always uses a fresh client."""

    def __init__(self, name: str, *, db: Optional[str] = None) -> None:
        self._name = name
        self._db_name = db or mongo_db_name()

    def _underlying(self):
        return get_client()[self._db_name][self._name]

    def __getattr__(self, name: str) -> Any:
        if not callable(getattr(self._underlying(), name)):
            return getattr(self._underlying(), name)

        # Re-resolve against a fresh client on each call so that a retry after
        # reset_client_for_tests() does not reuse a closed MongoClient handle.
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return with_mongo_retry(
                lambda: getattr(self._underlying(), name)(*args, **kwargs)
            )

        return wrapper


def get_client():
    """Return the process-wide MongoClient, creating it on first use.

    Uses a short serverSelectionTimeoutMS so callers fail fast instead of
    blocking for the pymongo default (30s) when mongod is down.
    """
    global _client
    with _lock:
        if _client is not None:
            if _client_is_open(_client):
                return _client
            _client = None
            _db_cache.clear()
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise RuntimeError(
                "pymongo is required for MongoDB storage. Install with "
                "`pip install pymongo` (it is in pyproject dependencies)."
            ) from exc
        uri = mongo_uri()
        _client = MongoClient(
            uri,
            serverSelectionTimeoutMS=int(
                os.environ.get("MONGODB_TIMEOUT_MS", "10000")
            ),
            connectTimeoutMS=int(os.environ.get("MONGODB_CONNECT_TIMEOUT_MS", "10000")),
            socketTimeoutMS=int(os.environ.get("MONGODB_SOCKET_TIMEOUT_MS", "60000")),
            maxPoolSize=50,
            tz_aware=False,
        )
        log.debug("created MongoClient for %s", uri)
        return _client


def get_db(name: Optional[str] = None) -> _RetryDatabase:
    """Return a retry-aware database handle (cached wrapper per name)."""
    db_name = name or mongo_db_name()
    cached = _db_cache.get(db_name)
    if cached is not None:
        return cached
    db = _RetryDatabase(db_name)
    _db_cache[db_name] = db
    return db


def get_collection(name: str, *, db: Optional[str] = None) -> _RetryCollection:
    """Return a retry-aware collection handle."""
    return get_db(db)[name]


def ping(timeout_ms: int = 1500) -> bool:
    """Return True if mongod answers a ping; never raises.

    Uses a short-lived client so a failed probe cannot close the process-wide
    singleton used by concurrent request handlers.
    """
    try:
        from pymongo import MongoClient
    except ImportError:
        return False
    for attempt in range(_MAX_MONGO_ATTEMPTS):
        client = None
        try:
            client = MongoClient(
                mongo_uri(), serverSelectionTimeoutMS=timeout_ms, tz_aware=False
            )
            client.admin.command("ping")
            return True
        except _MONGO_CONN_ERRORS:
            if attempt == 0:
                _try_autostart_mongod()
                continue
            return False
        except Exception:
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
    return False


def mongo_status(
    *,
    include_collections: bool = False,
    include_persistence: bool = False,
) -> dict[str, Any]:
    """Connectivity snapshot for /status and health checks.

    By default only pings MongoDB (fast). Pass ``include_collections`` /
    ``include_persistence`` for a full inventory (slow on large DBs).
    """
    out: dict[str, Any] = {
        "ok": False,
        "uri": mongo_uri(),
        "db": mongo_db_name(),
        "collections": {},
        "error": None,
    }
    try:
        db = get_db()
        db.command("ping")
        out["ok"] = True
        if include_collections:
            for name in sorted(db.list_collection_names()):
                try:
                    out["collections"][name] = db[name].estimated_document_count()
                except Exception:
                    out["collections"][name] = None
        if include_persistence:
            try:
                from .mongo_persistence import build_mongo_persistence_snapshot

                out["persistence"] = build_mongo_persistence_snapshot()
            except Exception as exc:
                out["persistence"] = {"error": str(exc)}
    except Exception as exc:
        out["error"] = str(exc)
    return out

"""Central Redis access for gois runtime state.

Runtime JSON blobs (monitor state, priority queue, swarm checkpoints, skill
suggestions) are stored here when Redis is reachable.

Environment:

    REDIS_URL              default redis://localhost:6379/0
    REDIS_KEY_PREFIX       default gois
    REDIS_TIMEOUT_SEC      socket/connect timeout (default 2)
    QCLAW_RUNTIME_REDIS=0    force file fallback (tests / offline)
    QCLAW_RUNTIME_JSON_MIRROR=1  mirror runtime JSON to disk when Redis is up (default off)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_URL = "redis://localhost:6379/0"
DEFAULT_PREFIX = "gois"

_lock = threading.Lock()
_client = None  # type: ignore[var-annotated]
_disabled_logged = False


def _connection_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)
    try:
        import redis.exceptions as rex

        errors = (*errors, rex.ConnectionError, rex.TimeoutError)
    except ImportError:  # pragma: no cover
        pass
    return errors


_REDIS_CONN_ERRORS = _connection_errors()


def _runtime_redis_requested() -> bool:
    flag = os.environ.get("QCLAW_RUNTIME_REDIS", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def redis_url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_URL).strip() or DEFAULT_URL


def redis_key_prefix() -> str:
    return os.environ.get("REDIS_KEY_PREFIX", DEFAULT_PREFIX).strip() or DEFAULT_PREFIX


def redis_key(suffix: str) -> str:
    suffix = suffix.strip().lstrip(":")
    return f"{redis_key_prefix()}:{suffix}"


def get_client():
    """Return the process-wide Redis client, creating it on first use."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        try:
            import redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "redis package is required for Redis runtime state. "
                "Install with `pip install redis`."
            ) from exc
        timeout = float(os.environ.get("REDIS_TIMEOUT_SEC", "2"))
        _client = redis.Redis.from_url(
            redis_url(),
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        log.debug("created Redis client for %s", redis_url())
        return _client


def reset_client_for_tests() -> None:
    """Drop cached client (pytest isolation and reconnect after outages)."""
    global _client, _disabled_logged
    with _lock:
        stale = _client
        _client = None
        _disabled_logged = False
    if stale is not None:
        try:
            stale.close()
        except Exception:
            pass


def _reset_client_on_error(exc: BaseException) -> bool:
    """Drop stale client after transport errors so the next call reconnects."""
    if isinstance(exc, _REDIS_CONN_ERRORS):
        log.debug("redis connection error — resetting client: %s", exc)
        reset_client_for_tests()
        return True
    return False


def ping() -> bool:
    """Return True when Redis is configured, enabled, and replying to PING."""
    if not _runtime_redis_requested():
        return False
    for attempt in range(2):
        try:
            return bool(get_client().ping())
        except _REDIS_CONN_ERRORS as exc:
            _reset_client_on_error(exc)
            if attempt == 0:
                continue
            return False
        except Exception:
            return False
    return False


def json_get(key_suffix: str) -> Optional[Any]:
    """Load JSON value from Redis. Returns None when missing or on error."""
    if not _runtime_redis_requested():
        return None
    for attempt in range(2):
        if not ping():
            return None
        try:
            raw = get_client().get(redis_key(key_suffix))
        except _REDIS_CONN_ERRORS as exc:
            _reset_client_on_error(exc)
            if attempt == 0:
                continue
            log.debug("redis json_get failed for %s: %s", key_suffix, exc)
            return None
        except Exception as exc:
            log.debug("redis json_get failed for %s: %s", key_suffix, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("invalid JSON in redis key %s", redis_key(key_suffix))
            return None
    return None


def json_set(key_suffix: str, value: Any, *, ttl_seconds: Optional[int] = None) -> bool:
    """Persist JSON value to Redis. Returns True on success."""
    if not _runtime_redis_requested():
        return False
    payload = json.dumps(value, ensure_ascii=False, default=str)
    key = redis_key(key_suffix)
    for attempt in range(2):
        if not ping():
            return False
        try:
            if ttl_seconds is not None and ttl_seconds > 0:
                get_client().setex(key, int(ttl_seconds), payload)
            else:
                get_client().set(key, payload)
            return True
        except _REDIS_CONN_ERRORS as exc:
            _reset_client_on_error(exc)
            if attempt == 0:
                continue
            log.warning("redis json_set failed for %s: %s", key_suffix, exc)
            return False
        except Exception as exc:
            log.warning("redis json_set failed for %s: %s", key_suffix, exc)
            return False
    return False


def delete(key_suffix: str) -> bool:
    if not _runtime_redis_requested():
        return False
    for attempt in range(2):
        if not ping():
            return False
        try:
            get_client().delete(redis_key(key_suffix))
            return True
        except _REDIS_CONN_ERRORS as exc:
            _reset_client_on_error(exc)
            if attempt == 0:
                continue
            log.debug("redis delete failed for %s: %s", key_suffix, exc)
            return False
        except Exception as exc:
            log.debug("redis delete failed for %s: %s", key_suffix, exc)
            return False
    return False


def scan_key_suffixes(suffix_glob: str) -> list[str]:
    """Return Redis keys matching ``{prefix}:{suffix_glob}`` (``*`` allowed)."""
    if not _runtime_redis_requested():
        return []
    pattern = redis_key(suffix_glob)
    for attempt in range(2):
        if not ping():
            return []
        try:
            return sorted(get_client().scan_iter(match=pattern))
        except _REDIS_CONN_ERRORS as exc:
            _reset_client_on_error(exc)
            if attempt == 0:
                continue
            log.debug("redis scan failed for %s: %s", suffix_glob, exc)
            return []
        except Exception as exc:
            log.debug("redis scan failed for %s: %s", suffix_glob, exc)
            return []
    return []


def redis_status() -> dict[str, Any]:
    ok = ping()
    out: dict[str, Any] = {
        "ok": ok,
        "url": redis_url(),
        "prefix": redis_key_prefix(),
        "enabled": _runtime_redis_requested(),
    }
    if ok:
        try:
            info = get_client().info(section="server")
            out["redis_version"] = info.get("redis_version")
        except Exception:
            pass
    return out

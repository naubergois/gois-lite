"""Single-leader election for gois background workers.

Uses a Redis lease when available; falls back to an advisory file lock on the
same host. HTTP handlers may run on every instance; mutating loops should gate
on :meth:`MonitorLeaderLock.is_leader`.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
else
    return 0
end
"""


def monitor_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class MonitorLeaderLock:
    """Best-effort leader lock with Redis lease or local file fallback."""

    def __init__(
        self,
        *,
        enabled: bool,
        instance_id: str,
        lock_key_suffix: str = "monitor:leader",
        lease_seconds: float = 30.0,
        file_lock_path: Optional[Path] = None,
    ) -> None:
        self.enabled = enabled
        self.instance_id = instance_id
        self.lock_key_suffix = lock_key_suffix
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.file_lock_path = file_lock_path
        self._is_leader = not enabled
        self._backend = "disabled" if not enabled else "none"
        self._holder: Optional[str] = None
        self._file_handle = None

    @property
    def is_leader(self) -> bool:
        if not self.enabled:
            return True
        return self._is_leader

    def try_acquire(self) -> bool:
        if not self.enabled:
            self._is_leader = True
            self._backend = "disabled"
            self._holder = self.instance_id
            return True
        if self._try_acquire_redis():
            return True
        if self._try_acquire_file():
            return True
        self._is_leader = False
        if self._backend == "none":
            self._backend = "unavailable"
        return False

    def renew(self) -> bool:
        if not self.enabled:
            return True
        if self._backend == "redis":
            return self._renew_redis()
        if self._backend == "file":
            return self._is_leader
        return self.try_acquire()

    def release(self) -> None:
        if self._backend == "redis":
            self._release_redis()
        self._release_file()
        self._is_leader = False
        self._backend = "none"
        self._holder = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "is_leader": self.is_leader,
            "instance_id": self.instance_id,
            "holder": self._holder,
            "backend": self._backend,
            "lock_key": self.lock_key_suffix,
            "lease_seconds": self.lease_seconds,
        }

    def _try_acquire_redis(self) -> bool:
        try:
            from .redis_store import get_client, ping, redis_key
        except ImportError:
            return False
        if not ping():
            return False
        key = redis_key(self.lock_key_suffix)
        ttl = max(1, int(self.lease_seconds))
        try:
            acquired = bool(
                get_client().set(key, self.instance_id, nx=True, ex=ttl)
            )
        except Exception as exc:
            log.debug("leader redis acquire failed: %s", exc)
            return False
        if acquired:
            self._release_file()
            self._is_leader = True
            self._backend = "redis"
            self._holder = self.instance_id
            log.info("leader acquired via redis (%s)", self.instance_id)
            return True
        try:
            holder = get_client().get(key)
            if isinstance(holder, bytes):
                holder = holder.decode("utf-8", errors="replace")
            self._holder = str(holder or "")
        except Exception:
            self._holder = None
        return False

    def _renew_redis(self) -> bool:
        try:
            from .redis_store import get_client, redis_key
        except ImportError:
            return False
        key = redis_key(self.lock_key_suffix)
        ttl = max(1, int(self.lease_seconds))
        try:
            renewed = get_client().eval(
                _RENEW_LUA, 1, key, self.instance_id, str(ttl)
            )
        except Exception as exc:
            log.debug("leader redis renew failed: %s", exc)
            self._is_leader = False
            return False
        if renewed:
            self._is_leader = True
            self._holder = self.instance_id
            return True
        self._is_leader = False
        return False

    def _release_redis(self) -> None:
        try:
            from .redis_store import get_client, redis_key
        except ImportError:
            return
        key = redis_key(self.lock_key_suffix)
        try:
            holder = get_client().get(key)
            if isinstance(holder, bytes):
                holder = holder.decode("utf-8", errors="replace")
            if holder == self.instance_id:
                get_client().delete(key)
        except Exception as exc:
            log.debug("leader redis release failed: %s", exc)

    def _try_acquire_file(self) -> bool:
        if self.file_lock_path is None:
            return False
        try:
            import fcntl
        except ImportError:
            return False
        path = self.file_lock_path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            try:
                self._holder = path.read_text(encoding="utf-8").strip() or None
            except OSError:
                self._holder = None
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(self.instance_id)
        handle.flush()
        self._release_file()
        self._file_handle = handle
        self._is_leader = True
        self._backend = "file"
        self._holder = self.instance_id
        log.info("leader acquired via file lock (%s)", path)
        return True

    def _release_file(self) -> None:
        handle = self._file_handle
        self._file_handle = None
        if handle is None:
            return
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

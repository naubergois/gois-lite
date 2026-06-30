"""Per-conversation chat send queue — one worker thread per UI session key.

Each dashboard conversation (``display_key``) gets its own daemon thread and
deque. Jobs in the same conversation run serially; different conversations run
in parallel without blocking each other.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)

_lock = threading.Lock()
_queues: dict[str, deque[str]] = {}
_payloads: dict[str, dict[str, Any]] = {}
_active: set[str] = set()
_global_sem: threading.Semaphore | None = None
_global_sem_limit = 4


def configure_global_chat_send_limit(limit: int) -> None:
    """Limit concurrent chat LLM workers so HTTP handlers stay responsive."""
    global _global_sem, _global_sem_limit
    capped = max(1, int(limit))
    _global_sem_limit = capped
    _global_sem = threading.Semaphore(capped)
    log.info("chat send global concurrency cap=%d", capped)


def _global_chat_send_sem() -> threading.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = threading.Semaphore(_global_sem_limit)
    return _global_sem


def enqueue_session_chat_send(
    queue_key: str,
    job_id: str,
    run_kwargs: dict[str, Any],
    runner: Callable[..., None],
) -> None:
    """Queue a chat send job; only one runner executes per queue_key at a time."""
    key = (queue_key or "").strip()
    jid = (job_id or "").strip()
    if not key or not jid:
        log.warning("enqueue_session_chat_send: dropped (key=%r, jid=%r)", queue_key, job_id)
        return
    start_worker = False
    with _lock:
        _payloads[jid] = dict(run_kwargs)
        q = _queues.setdefault(key, deque(maxlen=50))
        q.append(jid)
        if key not in _active:
            _active.add(key)
            start_worker = True
    if start_worker:
        threading.Thread(
            target=_drain_queue,
            args=(key, runner),
            name=f"chat-send-q-{key[-28:]}",
            daemon=True,
        ).start()


def _drain_queue(queue_key: str, runner: Callable[..., None]) -> None:
    try:
        while True:
            with _lock:
                q = _queues.get(queue_key)
                if not q:
                    break
                job_id = q[0]
                kwargs = _payloads.get(job_id)
            if not kwargs:
                log.warning("chat send queue: job %s has no payload — failing", job_id)
                with _lock:
                    if _queues.get(queue_key) and _queues[queue_key][0] == job_id:
                        _queues[queue_key].popleft()
                    _payloads.pop(job_id, None)
                    if not _queues.get(queue_key):
                        break
                try:
                    from .chat_jobs import fail_job
                    fail_job(job_id, "payload perdido na fila de envio (possível race condition)")
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                with _global_chat_send_sem():
                    runner(**kwargs)
            except Exception as exc:
                log.exception("chat send queue runner failed job=%s", job_id)
                try:
                    from .chat_jobs import fail_job
                    fail_job(job_id, f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            with _lock:
                if _queues.get(queue_key) and _queues[queue_key][0] == job_id:
                    _queues[queue_key].popleft()
                _payloads.pop(job_id, None)
                if not _queues.get(queue_key):
                    break
    finally:
        with _lock:
            _queues.pop(queue_key, None)
            _active.discard(queue_key)


def queued_job_count(queue_key: str) -> int:
    key = (queue_key or "").strip()
    with _lock:
        q = _queues.get(key)
        return len(q) if q else 0


def cancel_queued_jobs_for_keys(queue_keys: set[str]) -> list[str]:
    """Drop queued send jobs for unwatched conversations (running job included)."""
    keys = {k.strip() for k in queue_keys if (k or "").strip()}
    if not keys:
        return []
    dropped: list[str] = []
    with _lock:
        for queue_key in list(_queues.keys()):
            if queue_key not in keys:
                continue
            q = _queues.get(queue_key)
            if not q:
                continue
            for jid in list(q):
                dropped.append(jid)
                _payloads.pop(jid, None)
            q.clear()
    return dropped


def queue_snapshot() -> dict[str, list[dict[str, Any]]]:
    """Per-thread queue state: {queue_key: [{jobId, position, active, hasPayload}]}.

    Used by the message queue panel to show the buffer contents for each thread.
    """
    with _lock:
        result: dict[str, list[dict[str, Any]]] = {}
        for key, q in _queues.items():
            items: list[dict[str, Any]] = []
            for idx, jid in enumerate(q):
                payload = _payloads.get(jid)
                preview = ""
                if payload:
                    raw = str(payload.get("text") or payload.get("message_text") or "")
                    preview = raw[:120] if raw else ""
                items.append({
                    "jobId": jid,
                    "position": idx,
                    "active": idx == 0 and key in _active,
                    "hasPayload": jid in _payloads,
                    "preview": preview,
                    "sessionKey": str(
                        (payload or {}).get("display_key") or key
                    ).strip() or key,
                })
            result[key] = items
        return result

"""Roteiro Viral local warmup — background worker removed; books run inline via chat."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

_INLINE_MSG = "execução inline via chat (worker removido)"


def worker_running() -> bool:
    return False


def _auto_start_disabled() -> bool:
    return True


def ensure_embedded_worker(*, reason: str = "embedded_api") -> dict[str, Any]:
    return {"attempted": False, "skipped": "worker_removed", "detail": _INLINE_MSG}


def start_rv_worker(*, reason: str = "manual", force: bool = False) -> dict[str, Any]:
    return {"attempted": False, "skipped": "worker_removed", "detail": _INLINE_MSG}


def stop_rv_worker(*, reason: str = "manual") -> dict[str, Any]:
    return {"stopped": False, "skipped": "worker_removed", "reason": reason}


def stop_embedded_worker() -> None:
    pass


async def warmup_embedded_roteiro_viral() -> None:
    """Warm embedded RV API only — no background worker."""
    from .embedded_api import embedded_available, get_client

    if not embedded_available():
        log.debug("roteiro viral runtime not available — skip RV warmup")
        return

    def _sync() -> None:
        get_client()

    await asyncio.to_thread(_sync)

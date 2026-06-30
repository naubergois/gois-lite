"""Periodic progress updates while blocking chat operations run."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .tool_progress import with_model_prefix


def format_waiting_progress(
    label: str,
    elapsed_seconds: int,
    *,
    model_label: str = "",
) -> str:
    base = (label or "A processar").strip()
    elapsed = max(0, int(elapsed_seconds))
    if elapsed > 0:
        body = f"{base} ({elapsed}s)…"
    else:
        body = f"{base}…"
    return with_model_prefix(body, model_label)


class ProgressHeartbeat:
    """Emit progress on an interval until the context exits."""

    def __init__(
        self,
        *,
        interval_seconds: float = 8.0,
        label: str = "A processar",
        model_label: str = "",
        on_update: Callable[[str], None],
        first_immediate: bool = True,
    ) -> None:
        self._interval = max(3.0, float(interval_seconds))
        self._label = (label or "A processar").strip()
        self._model_label = (model_label or "").strip()
        self._on_update = on_update
        self._first_immediate = first_immediate
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = 0.0

    def _emit(self, elapsed: int) -> None:
        msg = format_waiting_progress(
            self._label,
            elapsed,
            model_label=self._model_label,
        )
        try:
            self._on_update(msg)
        except Exception:
            logging.getLogger(__name__).debug("progress heartbeat callback failed", exc_info=True)

    def _loop(self) -> None:
        if self._first_immediate:
            self._emit(0)
        while not self._stop.wait(self._interval):
            elapsed = int(time.time() - self._started)
            self._emit(elapsed)

    def __enter__(self) -> ProgressHeartbeat:
        self._started = time.time()
        self._thread = threading.Thread(
            target=self._loop,
            name="chat-progress-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.5)

"""Local Roteiro Viral — no external API."""

from .bootstrap import ensure_runtime_on_path, resolve_runtime_root
from .config import local_output_root, use_local_rv
from .dispatch import dispatch_local, is_local_path, local_mode_label
from .embedded_api import client_mode, embedded_available, get_client, request_embedded, reset_client
from .embedded_worker import (
    ensure_embedded_worker,
    start_rv_worker,
    stop_embedded_worker,
    stop_rv_worker,
    warmup_embedded_roteiro_viral,
    worker_running,
)

__all__ = [
    "client_mode",
    "dispatch_local",
    "embedded_available",
    "ensure_embedded_worker",
    "ensure_runtime_on_path",
    "get_client",
    "is_local_path",
    "local_mode_label",
    "local_output_root",
    "request_embedded",
    "reset_client",
    "resolve_runtime_root",
    "start_rv_worker",
    "stop_embedded_worker",
    "stop_rv_worker",
    "use_local_rv",
    "warmup_embedded_roteiro_viral",
    "worker_running",
]

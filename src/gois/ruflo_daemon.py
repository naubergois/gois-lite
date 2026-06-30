"""RuFlo background daemon helpers (status probes, startup)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .config import RufloChatConfig
from .openclaw_chat import QclawRuntime
from .ruflo_chat import _project_dir, run_ruflo

log = logging.getLogger(__name__)


def daemon_pid_file(workspace: Path) -> Path:
    return workspace / ".claude-flow" / "daemon.pid"


def daemon_running(workspace: Path) -> bool:
    pid_file = daemon_pid_file(workspace)
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_ruflo_daemon(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    *,
    swarm_memory_cfg: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Start RuFlo daemon if missing; no-op when already running."""
    workspace = _project_dir(cfg, runtime)
    if daemon_running(workspace):
        return {"ok": True, "already_running": True, "workspace": str(workspace)}
    code, stdout, stderr = run_ruflo(
        cfg,
        runtime,
        ["daemon", "start"],
        timeout=timeout,
        swarm_memory_cfg=swarm_memory_cfg,
    )
    running = daemon_running(workspace)
    ok = running or code == 0
    if ok:
        log.info("ruflo daemon started workspace=%s", workspace)
    else:
        log.warning(
            "ruflo daemon start failed workspace=%s code=%s stderr=%s",
            workspace,
            code,
            (stderr or stdout or "")[:300],
        )
    return {
        "ok": ok,
        "workspace": str(workspace),
        "daemon_running": running,
        "exit_code": code,
        "stderr_tail": (stderr or "")[-400:],
    }

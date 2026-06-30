"""Resolved OpenClaw/QClaw filesystem paths for chat bridge code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import QclawConfig
from .local_paths import openclaw_state_dir
from .recovery import _resolve_live_gateway_port


@dataclass(frozen=True)
class QclawRuntime:
    state_dir: Path
    config_path: Path
    gateway_port: Optional[int]
    control_url: Optional[str]


def resolve_qclaw_runtime(qclaw: QclawConfig) -> QclawRuntime:
    """Resolve QClaw state directory and live gateway port from qclaw.json."""
    state_dir = openclaw_state_dir()
    config_path = state_dir / "openclaw.json"
    port: Optional[int] = None

    port_file = (
        Path(qclaw.gateway_port_file).expanduser()
        if qclaw.gateway_port_file
        else None
    )
    if port_file and port_file.is_file():
        try:
            data = json.loads(port_file.read_text())
            if data.get("stateDir"):
                state_dir = Path(data["stateDir"]).expanduser()
            if data.get("configPath"):
                config_path = Path(data["configPath"]).expanduser()
        except (OSError, json.JSONDecodeError):
            pass

    port = _resolve_live_gateway_port(qclaw, sync_file=True)

    control_url = f"http://127.0.0.1:{port}/" if port else None
    return QclawRuntime(
        state_dir=state_dir,
        config_path=config_path,
        gateway_port=port,
        control_url=control_url,
    )

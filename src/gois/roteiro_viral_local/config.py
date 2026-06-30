"""Local Roteiro Viral mode — no external RV API required."""

from __future__ import annotations

import os
from pathlib import Path


def use_local_rv() -> bool:
    """Default: local. Set QCLAW_RV_USE_API=1 to call ROTEIRO_VIRAL_API over HTTP."""
    raw = (os.environ.get("QCLAW_RV_USE_API") or "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def local_output_root() -> Path:
    root = Path(__file__).resolve().parents[3] / ".stack" / "roteiro-viral-local"
    root.mkdir(parents=True, exist_ok=True)
    return root

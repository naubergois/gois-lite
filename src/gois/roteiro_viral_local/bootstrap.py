"""Bootstrap environment for embedded Roteiro Viral runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def qclaw_root() -> Path:
    return Path(__file__).resolve().parents[3]


def runtime_candidates() -> list[Path]:
    """Prefer in-repo runtime; external ROTEIRO_VIRAL_PATH is optional fallback."""
    out: list[Path] = []
    in_repo = qclaw_root() / "src" / "gois" / "roteiro_viral"
    if in_repo.is_dir():
        out.append(in_repo)
    vendor = qclaw_root() / "vendor" / "roteiroviral-runtime"
    if vendor.is_dir():
        out.append(vendor)
    runtime_copy = qclaw_root() / "src" / "gois" / "roteiroviral-runtime"
    if runtime_copy.is_dir():
        out.append(runtime_copy)
    explicit = (os.environ.get("ROTEIRO_VIRAL_PATH") or "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
    return out


def resolve_runtime_root() -> Path:
    for candidate in runtime_candidates():
        if candidate.is_dir() and (candidate / "api_v2" / "main.py").is_file():
            return candidate.resolve()
    tried = ", ".join(str(p) for p in runtime_candidates())
    raise RuntimeError(
        "Runtime Roteiro Viral não encontrado. "
        f"Execute ./scripts/sync_roteiroviral_vendor.sh (tentou: {tried})"
    )


def _apply_env_defaults() -> None:
    """Map QClaw env → RV env without clobbering explicit overrides."""
    try:
        from gois.mongo import mongo_db_name, mongo_uri
    except Exception:
        mongo_db_name = lambda: os.environ.get("MONGODB_DB", "gois")  # type: ignore[assignment]
        mongo_uri = lambda: os.environ.get("MONGODB_URI", "mongodb://localhost:27017")  # type: ignore[assignment]

    uri = mongo_uri()
    rv_db = (os.environ.get("QCLAW_RV_MONGO_DB") or "viralscript").strip() or "viralscript"

    defaults = {
        "MONGODB_URL": uri,
        "MONGODB_URI": uri,
        "MONGODB_DB_NAME": rv_db,
        "WHATSAPP_SYNC_ENABLED": "false",
        "WHATSAPP_SYNC_EMBEDDED_IN_API": "false",
        "WHATSAPP_AGENT_ENABLED": "false",
        "MONGO_AUTO_RESTART_LOCAL": "0",
        "QCLAW_EMBEDDED_RV": "1",
        "THUMBNAIL_API_FALLBACK_SEC": "0",
        "WORKER_POLL_IDLE_SEC": "15",
        "WORKER_HEARTBEAT_FULL_SEC": "60",
        "WORKER_IDLE_EXIT_SEC": "300",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)

    # Propagate Gemini key if present in QClaw env
    for src, dst in (
        ("GEMINI_API_KEY", "GEMINI_API_KEY"),
        ("GOOGLE_API_KEY", "GOOGLE_API_KEY"),
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ):
        val = os.environ.get(src)
        if val and not os.environ.get(dst):
            os.environ[dst] = val


def frontend_dist_candidates(*, runtime_root: Path | None = None) -> list[Path]:
    """Directories that may contain a built RV SPA (index.html)."""
    candidates: list[Path] = []
    env_dist = (os.environ.get("ROTEIRO_VIRAL_FRONTEND_DIST") or "").strip()
    if env_dist:
        candidates.append(Path(env_dist).expanduser())
    rv_path = (os.environ.get("ROTEIRO_VIRAL_PATH") or "").strip()
    if rv_path:
        candidates.append(Path(rv_path).expanduser() / "frontend" / "dist")
    default_external = Path("/Volumes/NAUBER/roteiroviral")
    if default_external.is_dir():
        candidates.append(default_external / "frontend" / "dist")
    for extra_root in (
        qclaw_root() / "vendor" / "roteiroviral-runtime",
        qclaw_root() / "src" / "gois" / "roteiroviral-runtime",
    ):
        if extra_root.is_dir():
            candidates.append(extra_root / "frontend" / "dist")
    if runtime_root is not None:
        candidates.append(runtime_root / "frontend" / "dist")
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        key = str(candidate.expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def resolve_frontend_dist(*, runtime_root: Path | None = None) -> str | None:
    """Return absolute path to frontend/dist when index.html exists."""
    for candidate in frontend_dist_candidates(runtime_root=runtime_root):
        index_path = candidate / "index.html"
        if index_path.is_file():
            return str(candidate.resolve())
    return None


def ensure_runtime_on_path() -> Path:
    root = resolve_runtime_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    _apply_env_defaults()
    if not (os.environ.get("ROTEIRO_VIRAL_PATH") or "").strip():
        dist = resolve_frontend_dist(runtime_root=root)
        if dist:
            external = Path(dist).parent.parent
            if external.is_dir():
                os.environ.setdefault("ROTEIRO_VIRAL_PATH", str(external.resolve()))
    return root

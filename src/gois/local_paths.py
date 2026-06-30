"""Resolve Hermes/OpenClaw roots for a self-contained single-repo setup.

Two roots per service:

  * ``*_source()``: the vendored source code (git submodule under ``vendor/``).
    Ships with the project; same on every install.
  * ``*_home()`` / ``*_state_dir()`` / ``*_bundle_base()``: the writable runtime
    state directory. Lives under ``.stack/`` by default (gitignored).

Both can be overridden by env vars for unusual layouts. Legacy paths in ``$HOME``
remain as a last-resort fallback so older installs keep working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _prefer_existing(*candidates: Path) -> Path:
    for path in candidates:
        if path.exists():
            return path
    return candidates[-1]


# ---------------------------------------------------------------------------
# Vendored source (committed via git submodules under ``vendor/``)
# ---------------------------------------------------------------------------

def vendor_root() -> Path:
    raw = os.environ.get("GOIS_VENDOR_ROOT")
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "vendor"


def hermes_source() -> Path:
    """Path to the Hermes agent source tree (git submodule)."""
    raw = os.environ.get("GOIS_HERMES_SOURCE")
    if raw:
        return Path(raw).expanduser()
    return vendor_root() / "hermes-agent"


def openclaw_source() -> Path:
    """Path to the OpenClaw source tree (git submodule)."""
    raw = os.environ.get("GOIS_OPENCLAW_SOURCE")
    if raw:
        return Path(raw).expanduser()
    return vendor_root() / "openclaw"


# ---------------------------------------------------------------------------
# Runtime state roots (writable; gitignored)
# ---------------------------------------------------------------------------

def project_stack_root() -> Path:
    env = os.environ.get("GOIS_STACK_ROOT")
    if env:
        return Path(env).expanduser()
    return _repo_root() / ".stack"


def hermes_home() -> Path:
    """Hermes runtime state directory (logs, profiles, sessions, state.db).

    Prefers the in-project ``.stack/hermes`` so installs are self-contained.
    Falls back to ``~/.hermes`` for legacy setups.
    """
    for key in ("GOIS_HERMES_HOME", "HERMES_HOME"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser()
    return _prefer_existing(
        project_stack_root() / "hermes",
        Path.home() / ".hermes",
    )


def openclaw_state_dir() -> Path:
    raw = os.environ.get("GOIS_OPENCLAW_STATE_DIR")
    if raw:
        return Path(raw).expanduser()
    return _prefer_existing(
        project_stack_root() / "openclaw" / "state",
        Path("~/.qclaw-oversea").expanduser(),
    )


def _config_yaml_path() -> Optional[Path]:
    env = os.environ.get("QCLAW_MONITOR_CONFIG", "").strip()
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_file():
            return candidate
    candidate = _repo_root() / "config.yaml"
    return candidate if candidate.is_file() else None


def _load_media_storage_config():
    from .config import MediaStorageConfig

    env_root = os.environ.get("GOIS_MEDIA_ROOT", "").strip()
    if env_root:
        return MediaStorageConfig(enabled=True, root_dir=env_root)

    cfg_path = _config_yaml_path()
    if cfg_path is None:
        try:
            from .config import Config

            cfg = Config.from_mongo()
            if cfg is not None:
                return cfg.media_storage
        except Exception:
            pass
        return MediaStorageConfig()

    try:
        from .config import Config

        return Config.load(cfg_path, auto_import=False).media_storage
    except Exception as exc:
        log.debug("could not load media_storage config: %s", exc)
        return MediaStorageConfig()


def _volume_mounted(path: Path) -> bool:
    """Return False when *path* sits on an unmounted external volume."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    parts = resolved.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        mount = Path("/") / "Volumes" / parts[2]
        return mount.is_dir()
    return True


def media_storage_root() -> Optional[Path]:
    """External media root when configured and available; else None (use ``.stack``)."""
    ms = _load_media_storage_config()
    if not ms.enabled or not str(ms.root_dir or "").strip():
        return None

    root = Path(ms.root_dir).expanduser()
    if not root.is_absolute():
        root = (_repo_root() / root).resolve()

    if not _volume_mounted(root):
        if ms.fallback_to_stack:
            log.warning(
                "media storage volume not mounted at %s; using .stack/chat",
                root,
            )
            return None
        root.mkdir(parents=True, exist_ok=True)
        return root

    root.mkdir(parents=True, exist_ok=True)
    return root


def media_chat_subdir(name: str) -> Path:
    """Resolve a chat media subdirectory (artifacts, previews, attachments)."""
    external = media_storage_root()
    if external is not None:
        dest = external / "chat" / name
    else:
        dest = project_stack_root() / "chat" / name
    dest.mkdir(parents=True, exist_ok=True)
    return dest.resolve()


def openclaw_bundle_base() -> Path:
    """OpenClaw runtime bundle (``node``, ``openclaw`` binaries + node_modules).

    Order of preference:
      1. ``GOIS_OPENCLAW_BASE`` env var
      2. ``.stack/openclaw/bundle`` (if a bootstrap has populated it)
      3. ``vendor/openclaw`` (the source submodule, after ``npm install``)
      4. ``~/Library/Application Support/QClaw/openclaw`` (QClaw.app bundle)
    """
    raw = os.environ.get("GOIS_OPENCLAW_BASE")
    if raw:
        return Path(raw).expanduser()
    return _prefer_existing(
        project_stack_root() / "openclaw" / "bundle",
        openclaw_source(),
        Path("~/Library/Application Support/QClaw/openclaw").expanduser(),
    )

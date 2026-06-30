"""Cross-platform user paths for logs, data, config — no admin required.

The legacy code hard-coded macOS locations like
``~/Library/Logs/Gois`` and ``~/Library/Application Support/...``.
These helpers pick the correct location per OS so the same code works on
Linux, Windows and macOS as a regular (non-admin) user.

Resolution order (matches platform conventions):

* macOS   → ``~/Library/Logs/Gois``   /  ``~/Library/Application Support/Gois``
* Windows → ``%LOCALAPPDATA%/Gois/Logs`` / ``%APPDATA%/Gois``
* Linux   → ``$XDG_STATE_HOME/gois/logs`` (fallback ``~/.local/state/...``)
            and ``$XDG_DATA_HOME/gois`` (fallback ``~/.local/share/...``)

Any override via the ``GOIS_LOG_DIR`` / ``GOIS_DATA_DIR`` env
vars wins. Directories are created lazily on first read with ``parents=True``
so user installs never need elevation.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

APP_NAME = "Gois"


def _expand(p: Path) -> Path:
    return p.expanduser().resolve()


def user_log_dir() -> Path:
    """Return a writable, per-user log directory. Never raises."""
    override = os.environ.get("GOIS_LOG_DIR")
    if override:
        path = _expand(Path(override))
    elif platform.system() == "Darwin":
        path = _expand(Path.home() / "Library" / "Logs" / APP_NAME)
    elif platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            path = _expand(Path(base) / APP_NAME / "Logs")
        else:
            path = _expand(Path.home() / "AppData" / "Local" / APP_NAME / "Logs")
    else:  # Linux + BSDs
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        path = _expand(Path(base) / "gois" / "logs")

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback to a tempdir-like location we know is writable
        fallback = _expand(Path.home() / ".gois" / "logs")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return path


def user_data_dir() -> Path:
    """Return a writable, per-user data directory. Never raises."""
    override = os.environ.get("GOIS_DATA_DIR")
    if override:
        path = _expand(Path(override))
    elif platform.system() == "Darwin":
        path = _expand(Path.home() / "Library" / "Application Support" / APP_NAME)
    elif platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            path = _expand(Path(base) / APP_NAME)
        else:
            path = _expand(Path.home() / "AppData" / "Roaming" / APP_NAME)
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = _expand(Path(base) / "gois")

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = _expand(Path.home() / ".gois" / "data")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return path


def venv_python(project_dir: Path) -> Path:
    """Return the venv Python executable for the project, cross-OS.

    Windows venvs put the interpreter at ``Scripts/python.exe`` while POSIX
    uses ``bin/python``.
    """
    project_dir = _expand(Path(project_dir))
    if platform.system() == "Windows":
        return project_dir / ".venv" / "Scripts" / "python.exe"
    return project_dir / ".venv" / "bin" / "python"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_linux() -> bool:
    return platform.system() == "Linux"

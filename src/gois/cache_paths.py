"""Resolve cache directories (npm, temp, pip, ruflo) on external volume or .stack."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CACHE_KINDS = ("npm", "tmp", "ruflo", "pip", "npx", "xdg")


def _load_cache_storage_config():
    from .config import CacheStorageConfig

    env_root = os.environ.get("GOIS_CACHE_ROOT", "").strip()
    if env_root:
        return CacheStorageConfig(enabled=True, root_dir=env_root)

    from .local_paths import _config_yaml_path

    cfg_path = _config_yaml_path()
    if cfg_path is None:
        try:
            from .config import Config

            cfg = Config.from_mongo()
            if cfg is not None:
                return cfg.cache_storage
        except Exception:
            pass
        return CacheStorageConfig()

    try:
        from .config import Config

        return Config.load(cfg_path, auto_import=False).cache_storage
    except Exception as exc:
        log.debug("could not load cache_storage config: %s", exc)
        return CacheStorageConfig()


def _repo_root() -> Path:
    from .local_paths import _repo_root as root

    return root()


def _stack_cache_root() -> Path:
    from .local_paths import project_stack_root

    return project_stack_root() / "cache"


def cache_storage_root() -> Optional[Path]:
    """External cache root when configured and mounted; else None (use ``.stack/cache``)."""
    cs = _load_cache_storage_config()
    if not cs.enabled or not str(cs.root_dir or "").strip():
        return None

    root = Path(cs.root_dir).expanduser()
    if not root.is_absolute():
        root = (_repo_root() / root).resolve()

    from .local_paths import _volume_mounted

    if not _volume_mounted(root):
        if cs.fallback_to_stack:
            log.warning(
                "cache storage volume not mounted at %s; using .stack/cache",
                root,
            )
            return None
        root.mkdir(parents=True, exist_ok=True)
        return root

    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_storage_active() -> bool:
    """True when caches are stored on the configured external volume."""
    return cache_storage_root() is not None


def _resolved_cache_root() -> Path:
    external = cache_storage_root()
    if external is not None:
        return external
    return _stack_cache_root()


def cache_subdir(kind: str) -> Path:
    """Return (and create) the cache directory for *kind* (npm, tmp, ruflo, pip, npx, xdg)."""
    name = str(kind or "").strip().lower()
    if name not in _CACHE_KINDS:
        raise ValueError(f"unknown cache kind: {kind!r} (expected one of {_CACHE_KINDS})")
    dest = _resolved_cache_root() / name
    dest.mkdir(parents=True, exist_ok=True)
    return dest.resolve()


def cache_subprocess_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Environment for child processes — npm/temp/pip caches on configured volume."""
    env = dict(base if base is not None else os.environ)
    npm = cache_subdir("npm")
    tmp = cache_subdir("tmp")
    pip = cache_subdir("pip")
    xdg = cache_subdir("xdg")
    root = _resolved_cache_root()
    env["GOIS_CACHE_ROOT"] = str(root)
    env["NPM_CONFIG_CACHE"] = str(npm)
    env["npm_config_cache"] = str(npm)
    env["TMPDIR"] = str(tmp)
    env["XDG_CACHE_HOME"] = str(xdg)
    env["PIP_CACHE_DIR"] = str(pip)
    return env


def apply_cache_env_to_process() -> Path:
    """Apply cache env vars to the current process (monitor startup). Returns cache root."""
    env = cache_subprocess_env()
    for key in (
        "GOIS_CACHE_ROOT",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
    ):
        os.environ[key] = env[key]
    return Path(env["GOIS_CACHE_ROOT"])


def launchd_cache_env_xml() -> str:
    """Plist XML fragment for LaunchAgent ``EnvironmentVariables``."""
    env = cache_subprocess_env()
    lines: list[str] = []
    for key in (
        "GOIS_CACHE_ROOT",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
    ):
        value = env.get(key)
        if not value:
            continue
        escaped = (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        lines.append(f"        <key>{key}</key>")
        lines.append(f"        <string>{escaped}</string>")
    return "\n".join(lines)

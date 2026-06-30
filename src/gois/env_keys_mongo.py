"""MongoDB-backed store for API keys and other secret env vars.

Single document per scope in the ``env_keys`` collection::

    { "_id": "default", "keys": { "DEEPSEEK_API_KEY": "sk-...", ... }, "_updated_at": ... }

After migration, ``secrets_fallback`` reads keys from here (MongoDB is the source of
truth; ``.env`` and sibling projects remain fallbacks for seeding).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from .mongo import get_collection

log = logging.getLogger(__name__)

ENV_KEYS_COLLECTION = "env_keys"
ENV_KEYS_DEFAULT_ID = "default"

# Process-local cache: doc_id → normalized keys (reloaded on startup / refresh).
_env_keys_cache: dict[str, dict[str, str]] = {}


def _normalize_keys(raw: dict[Any, Any]) -> dict[str, str]:
    from .secrets_fallback import is_placeholder

    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        val = str(value or "").strip()
        if name and val and not is_placeholder(val):
            out[name] = val
    return out


def _read_keys_from_mongo(doc_id: str) -> dict[str, str]:
    doc = get_collection(ENV_KEYS_COLLECTION).find_one({"_id": doc_id})
    if not doc:
        return {}
    raw = doc.get("keys")
    if not isinstance(raw, dict):
        return {}
    return _normalize_keys(raw)


def _set_env_keys_cache(doc_id: str, keys: dict[str, str]) -> None:
    _env_keys_cache[doc_id] = dict(keys)


def load_env_keys_cache(
    doc_id: str = ENV_KEYS_DEFAULT_ID,
    *,
    force: bool = False,
) -> dict[str, str]:
    """Load keys from MongoDB into memory; return a copy of the cache."""
    if not force and doc_id in _env_keys_cache:
        return dict(_env_keys_cache[doc_id])
    keys = _read_keys_from_mongo(doc_id)
    _set_env_keys_cache(doc_id, keys)
    log.info("env keys cache loaded (%d key(s), doc=%s)", len(keys), doc_id)
    return dict(keys)


def get_cached_env_keys(doc_id: str = ENV_KEYS_DEFAULT_ID) -> dict[str, str]:
    """Return cached keys, loading from MongoDB on first access in this process."""
    return load_env_keys_cache(doc_id)


def refresh_env_keys_cache(doc_id: str = ENV_KEYS_DEFAULT_ID) -> dict[str, str]:
    """Force reload from MongoDB (e.g. after external DB edits)."""
    return load_env_keys_cache(doc_id, force=True)


def clear_env_keys_cache(doc_id: str | None = None) -> None:
    """Clear in-memory cache (tests and process restart)."""
    if doc_id is None:
        _env_keys_cache.clear()
    else:
        _env_keys_cache.pop(doc_id, None)


def apply_env_keys_cache_to_environ(doc_id: str = ENV_KEYS_DEFAULT_ID) -> int:
    """Apply all cached keys to ``os.environ``. Returns count applied."""
    import os

    from .secrets_fallback import (
        is_instance_env_key,
        is_placeholder,
        is_storable_env_key,
    )

    applied = 0
    for key, val in get_cached_env_keys(doc_id).items():
        if (
            not is_storable_env_key(key)
            or is_instance_env_key(key)
            or is_placeholder(val)
        ):
            continue
        os.environ[key] = val
        applied += 1
    return applied


class EnvKeysStore:
    """Persistent store for environment variables (secrets and config)."""

    def __init__(self, doc_id: str = ENV_KEYS_DEFAULT_ID) -> None:
        self.doc_id = doc_id

    def _collection(self):
        return get_collection(ENV_KEYS_COLLECTION)

    def get_all(self, *, use_cache: bool = True) -> dict[str, str]:
        if use_cache:
            return get_cached_env_keys(self.doc_id)
        return _read_keys_from_mongo(self.doc_id)

    def get(self, name: str) -> Optional[str]:
        return self.get_all().get(name)

    def upsert(
        self,
        keys: dict[str, str],
        *,
        source_path: Optional[str] = None,
        merge: bool = True,
        allow_all: bool = False,
    ) -> int:
        """Upsert key/value pairs. Returns count of keys written."""
        from .secrets_fallback import (
            is_instance_env_key,
            is_managed_env_key,
            is_placeholder,
            is_storable_env_key,
        )

        if not keys:
            return 0
        col = self._collection()
        existing = col.find_one({"_id": self.doc_id}) or {}
        merged: dict[str, str] = {}
        if merge:
            prev = existing.get("keys")
            if isinstance(prev, dict):
                merged = {str(k): str(v) for k, v in prev.items()}
        written = 0
        for key, value in keys.items():
            name = str(key or "").strip()
            val = str(value or "").strip()
            if not name or not val or is_placeholder(val):
                continue
            if is_instance_env_key(name):
                continue
            if allow_all:
                if not is_storable_env_key(name):
                    continue
            elif not is_managed_env_key(name):
                continue
            merged[name] = val
            written += 1
        sources = list(existing.get("_source_paths") or [])
        if source_path and source_path not in sources:
            sources.append(source_path)
        col.replace_one(
            {"_id": self.doc_id},
            {
                "_id": self.doc_id,
                "keys": merged,
                "_updated_at": time.time(),
                "_source_paths": sources,
            },
            upsert=True,
        )
        _set_env_keys_cache(self.doc_id, _normalize_keys(merged))
        return written

    def set(self, name: str, value: str) -> None:
        self.upsert({name: value}, merge=True, allow_all=True)

    def delete(self, names: list[str]) -> int:
        """Remove keys from the stored document. Returns count removed."""
        if not names:
            return 0
        col = self._collection()
        doc = col.find_one({"_id": self.doc_id}) or {}
        keys = doc.get("keys")
        if not isinstance(keys, dict):
            return 0
        merged = {str(k): str(v) for k, v in keys.items()}
        removed = 0
        for raw in names:
            name = str(raw or "").strip()
            if name in merged:
                del merged[name]
                removed += 1
        if not removed:
            return 0
        col.replace_one(
            {"_id": self.doc_id},
            {
                "_id": self.doc_id,
                "keys": merged,
                "_updated_at": time.time(),
                "_source_paths": list(doc.get("_source_paths") or []),
            },
            upsert=True,
        )
        _set_env_keys_cache(self.doc_id, _normalize_keys(merged))
        return removed


def collect_all_env_keys_from_file(local_env: Path) -> dict[str, str]:
    """Import every valid variable from a local .env file."""
    from .secrets_fallback import (
        is_instance_env_key,
        is_placeholder,
        is_storable_env_key,
        parse_env_file,
    )

    local_env = local_env.expanduser().resolve()
    if not local_env.is_file():
        return {}
    out: dict[str, str] = {}
    for key, val in parse_env_file(local_env).items():
        if (
            is_storable_env_key(key)
            and not is_instance_env_key(key)
            and val
            and not is_placeholder(val)
        ):
            out[key] = val
    return out


def collect_env_keys_from_files(
    *,
    local_env: Optional[Path] = None,
    sibling_paths: Optional[tuple[Path, ...]] = None,
) -> dict[str, str]:
    """Merge managed keys from sibling .env files, then local .env (local wins)."""
    from .secrets_fallback import (
        SIBLING_ENV_FILES,
        is_managed_env_key,
        is_placeholder,
        parse_env_file,
    )

    merged: dict[str, str] = {}
    paths = sibling_paths if sibling_paths is not None else SIBLING_ENV_FILES
    for path in paths:
        if not path.is_file():
            continue
        for key, val in parse_env_file(path).items():
            if key in merged:
                continue
            if is_managed_env_key(key) and val and not is_placeholder(val):
                merged[key] = val
    if local_env is None:
        local_env = Path.cwd() / ".env"
    if local_env.is_file():
        for key, val in parse_env_file(local_env).items():
            if is_managed_env_key(key) and val and not is_placeholder(val):
                merged[key] = val
    return merged


def migrate_env_keys_to_mongo(
    *,
    local_env: Optional[Path] = None,
    doc_id: str = ENV_KEYS_DEFAULT_ID,
    sibling_paths: Optional[tuple[Path, ...]] = None,
    import_all: bool = True,
) -> dict[str, Any]:
    """Import keys from .env into MongoDB (all vars by default)."""
    if local_env is None:
        local_env = Path.cwd() / ".env"
    local_env = Path(local_env).expanduser()
    if import_all and local_env.is_file():
        keys = collect_all_env_keys_from_file(local_env)
    else:
        keys = collect_env_keys_from_files(
            local_env=local_env,
            sibling_paths=sibling_paths,
        )
    store = EnvKeysStore(doc_id)
    written = store.upsert(
        keys,
        source_path=str(local_env.resolve()) if local_env.is_file() else None,
        merge=True,
        allow_all=import_all,
    )
    return {
        "doc_id": doc_id,
        "imported": written,
        "total": len(store.get_all()),
        "keys": sorted(store.get_all().keys()),
        "import_all": import_all,
    }


def prune_managed_keys_from_env(
    env_path: Path,
    *,
    doc_id: str = ENV_KEYS_DEFAULT_ID,
    stored_keys: Optional[dict[str, str]] = None,
    all_stored: bool = True,
) -> dict[str, Any]:
    """Remove from ``env_path`` keys already stored in MongoDB."""
    from .secrets_fallback import is_managed_env_key

    env_path = env_path.expanduser().resolve()
    if stored_keys is None:
        stored_keys = EnvKeysStore(doc_id).get_all()
    if all_stored:
        to_remove = set(stored_keys.keys())
    else:
        to_remove = {
            name
            for name, val in stored_keys.items()
            if is_managed_env_key(name) and val
        }
    if not to_remove or not env_path.is_file():
        return {
            "ok": True,
            "removed": [],
            "path": str(env_path),
            "skipped": not env_path.is_file(),
        }

    removed: list[str] = []
    kept: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            kept.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in to_remove:
            removed.append(key)
            continue
        kept.append(line)

    if removed:
        env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        log.info("pruned %d key(s) from %s", len(removed), env_path)

    return {
        "ok": True,
        "removed": sorted(set(removed)),
        "path": str(env_path),
    }

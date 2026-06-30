"""Runtime JSON persistence — Redis cache, MongoDB durable store, file fallback.

Used by monitor state, priority queue, swarm checkpoints, skill discovery, and
swarm definitions. When MongoDB is up, blobs are persisted in ``runtime_blobs``.
Redis serves as a fast cache when available. Legacy JSON files are imported once
on first read (non-destructive). Disk mirroring while Redis is up is opt-in via
``QCLAW_RUNTIME_JSON_MIRROR=1`` (default off).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .redis_store import (
    delete as redis_delete,
    json_get,
    json_set,
    ping as redis_ping,
    scan_key_suffixes,
)
from .runtime_swarm_paths import swarm_def_key

log = logging.getLogger(__name__)

_stats_lock = threading.Lock()
_stats: dict[str, int] = {
    "redis_reads": 0,
    "redis_writes": 0,
    "mongo_reads": 0,
    "mongo_writes": 0,
    "file_reads": 0,
    "file_writes": 0,
    "redis_seeds": 0,
    "mongo_seeds": 0,
    "redis_write_failures": 0,
    "mongo_write_failures": 0,
    "file_mirror_writes": 0,
}
_file_fallback_logged = False


def _inc(name: str, amount: int = 1) -> None:
    with _stats_lock:
        _stats[name] = _stats.get(name, 0) + amount


def reset_runtime_stats_for_tests() -> None:
    """Reset runtime persistence counters (pytest isolation)."""
    global _file_fallback_logged
    from .runtime_integrity import reset_integrity_state_for_tests

    reset_integrity_state_for_tests()
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0
    _file_fallback_logged = False


def runtime_stats() -> dict[str, Any]:
    """Counters and flags for runtime persistence (Redis vs file fallback)."""
    from .runtime_blobs_mongo import runtime_mongo_enabled

    with _stats_lock:
        counters = dict(_stats)
    from .runtime_integrity import integrity_redis_write_failures

    counters["redis_write_failures"] = integrity_redis_write_failures()
    backend = "file"
    if runtime_mongo_enabled():
        backend = "mongo+redis" if runtime_redis_enabled() else "mongo"
    elif runtime_redis_enabled():
        backend = "redis"
    return {
        "backend": backend,
        "mongo_enabled": runtime_mongo_enabled(),
        "json_mirror_enabled": _json_mirror_enabled(),
        "auto_mirror_active": _mirror_enabled(),
        **counters,
    }


def _json_mirror_enabled() -> bool:
    """When Redis is up, mirror writes to legacy JSON files (default off)."""
    flag = os.environ.get("QCLAW_RUNTIME_JSON_MIRROR", "0").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _mirror_enabled() -> bool:
    from .runtime_integrity import degraded_mirror_active

    return _json_mirror_enabled() or degraded_mirror_active()


def _log_file_fallback_once(reason: str) -> None:
    global _file_fallback_logged
    if _file_fallback_logged:
        return
    _file_fallback_logged = True
    log.warning(
        "runtime state using JSON file fallback (%s) — "
        "check Redis (REDIS_URL) or set QCLAW_RUNTIME_REDIS=0 for offline mode",
        reason,
    )


def runtime_redis_enabled() -> bool:
    return redis_ping()


def _read_file(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("could not read runtime state file %s: %s", path, exc)
        return None


def _json_fingerprint(data: Any) -> str:
    import hashlib

    try:
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _warm_redis_cache(key_suffix: str, data: Any) -> bool:
    """Populate Redis from the authoritative store (best-effort cache warm)."""
    if not runtime_redis_enabled():
        return False
    if json_set(key_suffix, data):
        _inc("redis_writes")
        _inc("redis_seeds")
        return True
    log.debug("could not warm redis cache for %s", key_suffix)
    return False


def _write_file(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not write runtime state file %s: %s", path, exc)


def load_json(key_suffix: str, legacy_path: Optional[Path] = None) -> Optional[Any]:
    """Load JSON blob — Mongo authoritative, Redis cache, file legacy seed."""
    from .runtime_blobs_mongo import blob_get, blob_set, runtime_mongo_enabled

    resolved = legacy_path.expanduser().resolve() if legacy_path else None
    mongo_data: Any = None
    redis_data: Any = None

    if runtime_mongo_enabled():
        mongo_data = blob_get(key_suffix)
        if mongo_data is not None:
            _inc("mongo_reads")

    if runtime_redis_enabled():
        redis_data = json_get(key_suffix)
        if redis_data is not None:
            _inc("redis_reads")

    if mongo_data is not None:
        if redis_data is not None:
            if _json_fingerprint(mongo_data) != _json_fingerprint(redis_data):
                log.warning(
                    "runtime cache drift for %s — refreshing redis from mongo",
                    key_suffix,
                )
                _warm_redis_cache(key_suffix, mongo_data)
        else:
            _warm_redis_cache(key_suffix, mongo_data)
        return mongo_data

    if redis_data is not None:
        if runtime_mongo_enabled() and blob_set(key_suffix, redis_data):
            _inc("mongo_writes")
            _inc("mongo_seeds")
            log.info("seeded mongo runtime key %s from redis cache", key_suffix)
        return redis_data

    if resolved and resolved.is_file():
        seeded = _read_file(resolved)
        if seeded is not None:
            _inc("file_reads")
            mongo_seeded = False
            if runtime_mongo_enabled():
                mongo_seeded = blob_set(key_suffix, seeded)
                if mongo_seeded:
                    _inc("mongo_writes")
                    _inc("mongo_seeds")
            if runtime_redis_enabled():
                if _warm_redis_cache(key_suffix, seeded):
                    log.info("seeded redis runtime key %s from %s", key_suffix, resolved)
                else:
                    from .runtime_integrity import record_write_failure

                    record_write_failure(
                        key_suffix,
                        reason=f"could not seed redis from {resolved}",
                        critical=not mongo_seeded,
                    )
                    log.warning(
                        "could not seed redis runtime key %s from %s; "
                        "serving from file",
                        key_suffix,
                        resolved,
                    )
        return seeded

    _log_file_fallback_once("redis/mongo unavailable")
    return None


def save_json(
    key_suffix: str,
    data: Any,
    legacy_path: Optional[Path] = None,
    *,
    indent_file: bool = True,
) -> None:
    """Persist JSON — Mongo durable store, Redis cache, file fallback/mirror."""
    from .runtime_blobs_mongo import blob_set, runtime_mongo_enabled

    mongo_ok = not runtime_mongo_enabled()
    if runtime_mongo_enabled():
        mongo_ok = blob_set(key_suffix, data)
        if mongo_ok:
            _inc("mongo_writes")
        else:
            from .runtime_integrity import record_write_failure

            record_write_failure(key_suffix, reason="mongo blob_set failed")
            _inc("mongo_write_failures")

    if runtime_redis_enabled():
        if json_set(key_suffix, data):
            _inc("redis_writes")
            if not runtime_mongo_enabled():
                from .runtime_integrity import note_redis_write_success

                note_redis_write_success()
        else:
            if runtime_mongo_enabled() and mongo_ok:
                log.warning(
                    "redis cache refresh failed for %s (mongo persisted)",
                    key_suffix,
                )
            else:
                from .runtime_integrity import record_write_failure

                record_write_failure(key_suffix)
                log.warning(
                    "redis json_set failed for %s; falling back to file",
                    key_suffix,
                )
                if legacy_path is not None:
                    _write_file(legacy_path.expanduser(), data)
                    _inc("file_writes")
                return

        if legacy_path is None or not _mirror_enabled():
            return

        resolved = legacy_path.expanduser()
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            text = (
                json.dumps(data, indent=2, ensure_ascii=False, default=str)
                if indent_file
                else json.dumps(data, ensure_ascii=False, default=str)
            )
            if indent_file and not text.endswith("\n"):
                text += "\n"
            tmp = resolved.with_suffix(resolved.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(resolved)
            _inc("file_mirror_writes")
        except OSError as exc:
            log.warning("could not mirror runtime state to %s: %s", resolved, exc)
        return

    if runtime_mongo_enabled() and mongo_ok:
        return

    _log_file_fallback_once("redis/mongo unavailable")
    if legacy_path is None:
        return
    _write_file(legacy_path.expanduser(), data)
    _inc("file_writes")


def delete_json(key_suffix: str, legacy_path: Optional[Path] = None) -> None:
    """Remove JSON blob from Redis/Mongo and optionally delete the legacy file."""
    from .runtime_blobs_mongo import blob_delete, runtime_mongo_enabled

    if runtime_mongo_enabled():
        blob_delete(key_suffix)
    if runtime_redis_enabled():
        redis_delete(key_suffix)
    if legacy_path is None:
        return
    try:
        legacy_path.expanduser().unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not delete runtime state file %s: %s", legacy_path, exc)



def _disk_swarm_definitions_dir() -> Path:
    """On-disk swarm definitions dir (respects ``openai_swarm._swarm_state_dir`` monkeypatches)."""
    try:
        from .openai_swarm import _swarm_state_dir

        return _swarm_state_dir()
    except Exception:
        from .local_paths import project_stack_root
        from .runtime_swarm_paths import swarm_definitions_dir

        return swarm_definitions_dir(project_stack_root())


def list_swarm_def_slugs() -> list[str]:
    """List swarm definition slugs from Mongo, Redis, and legacy disk."""
    from .runtime_blobs_mongo import blob_list_ids, runtime_mongo_enabled

    slugs: set[str] = set()

    if runtime_mongo_enabled():
        for key in blob_list_ids("swarm:def:"):
            parts = key.split(":")
            if len(parts) >= 4 and parts[-1] == "state" and parts[-2]:
                slugs.add(parts[-2])

    if runtime_redis_enabled():
        for key in scan_key_suffixes("swarm:def:*:state"):
            parts = key.split(":")
            if len(parts) >= 2 and parts[-1] == "state" and parts[-2]:
                slugs.add(parts[-2])

    definitions_dir = _disk_swarm_definitions_dir()
    if definitions_dir.is_dir():
        for state_file in sorted(definitions_dir.glob("*.json")):
            if not state_file.name.startswith("."):
                slugs.add(state_file.stem)

    return sorted(slugs)


def list_swarm_checkpoint_slugs() -> list[str]:
    """List swarm checkpoint slugs from Mongo, Redis, and legacy disk."""
    from .runtime_blobs_mongo import blob_list_ids, runtime_mongo_enabled
    from .runtime_swarm_paths import SWARM_CHECKPOINT_FILENAME

    slugs: set[str] = set()

    if runtime_mongo_enabled():
        for key in blob_list_ids("swarm:"):
            parts = key.split(":")
            if len(parts) >= 3 and parts[-1] == "checkpoint" and parts[-2]:
                if parts[-3] != "def":
                    slugs.add(parts[-2])

    if runtime_redis_enabled():
        for key in scan_key_suffixes("swarm:*:checkpoint"):
            parts = key.split(":")
            if len(parts) >= 2 and parts[-1] == "checkpoint" and parts[-2]:
                slugs.add(parts[-2])

    definitions_dir = _disk_swarm_definitions_dir()
    if definitions_dir.is_dir():
        for swarm_subdir in sorted(definitions_dir.iterdir()):
            if not swarm_subdir.is_dir():
                continue
            if (swarm_subdir / SWARM_CHECKPOINT_FILENAME).is_file():
                slugs.add(swarm_subdir.name)

    return sorted(slugs)


def migrate_file_to_redis(key_suffix: str, legacy_path: Path) -> bool:
    """Import a legacy JSON file into Redis if the key is empty."""
    path = legacy_path.expanduser().resolve()
    if not path.is_file() or not runtime_redis_enabled():
        return False
    if json_get(key_suffix) is not None:
        return False
    data = _read_file(path)
    if data is None:
        return False
    return json_set(key_suffix, data)


def migrate_all_runtime_to_redis(stack_root: Path | None = None) -> dict[str, int]:
    """One-shot import of known runtime JSON files into Redis."""
    from .config import Config
    from .local_paths import project_stack_root

    stack = (stack_root or project_stack_root()).expanduser().resolve()
    repo = stack.parent
    counts: dict[str, int] = {
        "monitor": 0,
        "priority_queue": 0,
        "skill_suggestions": 0,
        "swarm_checkpoints": 0,
        "swarm_definitions": 0,
    }
    if not runtime_redis_enabled():
        return counts

    cfg_path = repo / "config.yaml"
    cfg = None
    if cfg_path.is_file():
        try:
            cfg = Config.load(cfg_path)
        except Exception:
            cfg = None

    monitor_path = Path(
        (cfg.state.path if cfg and cfg.state.path else "./gois.state.json")
    ).expanduser()
    if migrate_file_to_redis("monitor:state", monitor_path):
        counts["monitor"] = 1

    pq_path = stack / "priority_queue" / "state.json"
    if migrate_file_to_redis("priority_queue:state", pq_path):
        counts["priority_queue"] = 1

    skill_path = Path(
        (cfg.skill_discovery.state_path if cfg else "./.stack/skill_suggestions/state.json")
    ).expanduser()
    if migrate_file_to_redis("skill_suggestions:state", skill_path):
        counts["skill_suggestions"] = 1

    from .runtime_swarm_paths import migrate_swarm_files

    swarm_counts = migrate_swarm_files(stack, migrate_file_to_redis)
    counts["swarm_definitions"] = swarm_counts["swarm_definitions"]
    counts["swarm_checkpoints"] = swarm_counts["swarm_checkpoints"]

    return counts

"""Deleted swarm robot tombstones — hide profiles from UI listings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .runtime_swarm_paths import swarm_definitions_dir
from .swarm_robots_slugs import _norm_slug

log = logging.getLogger(__name__)

ROBOT_TOMBSTONES_KEY = "swarm:robot_tombstones"


def _robot_tombstone_path() -> Path:
    from .local_paths import project_stack_root

    d = swarm_definitions_dir(project_stack_root())
    d.mkdir(parents=True, exist_ok=True)
    return d / ".deleted_robots.json"


def _load_tombstone_list() -> list[str]:
    from .runtime_state import load_json

    legacy = _robot_tombstone_path()
    data = load_json(ROBOT_TOMBSTONES_KEY, legacy)
    if isinstance(data, list):
        return [str(item) for item in data if str(item).strip()]
    return []


def load_robot_tombstones() -> set[str]:
    """Slugs explicitly deleted by the user; hidden from kanban/team listings."""
    return {_norm_slug(s) for s in _load_tombstone_list() if _norm_slug(str(s))}


def _save_robot_tombstones(slugs: set[str]) -> None:
    from .runtime_state import save_json

    payload = sorted(slugs)
    try:
        save_json(ROBOT_TOMBSTONES_KEY, payload, _robot_tombstone_path())
    except Exception as exc:
        log.warning("swarm robot: failed to save tombstones: %s", exc)


def add_robot_tombstone(slug: str) -> None:
    key = _norm_slug(slug)
    if not key:
        return
    tombstones = load_robot_tombstones()
    if key not in tombstones:
        tombstones.add(key)
        _save_robot_tombstones(tombstones)


def clear_robot_tombstone(slug: str) -> None:
    key = _norm_slug(slug)
    tombstones = load_robot_tombstones()
    if key in tombstones:
        tombstones.discard(key)
        _save_robot_tombstones(tombstones)


def filter_tombstoned_profiles(
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide robots deleted from the swarm UI from monitor/profile listings."""
    tombstones = load_robot_tombstones()
    if not tombstones:
        return list(profiles)
    return [
        row
        for row in profiles
        if isinstance(row, dict)
        and _norm_slug(str(row.get("name") or "")) not in tombstones
    ]


def filter_tombstoned_cron_jobs(
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop cron rows for profiles explicitly deleted as swarm robots."""
    tombstones = load_robot_tombstones()
    if not tombstones:
        return list(jobs)
    kept: list[dict[str, Any]] = []
    for row in jobs:
        if not isinstance(row, dict):
            continue
        profile = _norm_slug(str(row.get("profile") or ""))
        if profile and profile in tombstones:
            continue
        kept.append(row)
    return kept


def apply_tombstone_filters_to_cron_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Refresh cron summary counts after hiding deleted robot profiles."""
    if not isinstance(snapshot, dict):
        return snapshot
    jobs = filter_tombstoned_cron_jobs(list(snapshot.get("jobs") or []))
    running = filter_tombstoned_cron_jobs(list(snapshot.get("running") or []))
    out = dict(snapshot)
    out["jobs"] = jobs
    out["running"] = running
    out["total"] = len(jobs)
    out["active_count"] = sum(1 for row in jobs if row.get("active"))
    out["paused_count"] = len(jobs) - out["active_count"]
    out["error_count"] = sum(
        1 for row in jobs if row.get("active") and row.get("last_status") == "error"
    )
    out["running_count"] = len(running)
    return out

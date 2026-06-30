"""RuFlo engines (Motores) dashboard API."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import RufloChatConfig, SwarmMemoryConfig, SwarmRufloEngineConfig
from .local_paths import _repo_root
from .openclaw_chat import QclawRuntime
from .ruflo_chat import run_ruflo
from .ruflo_swarm_hooks import _parse_json_stdout

log = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"initialized", "ready", "running", "active"})
_CLI_STATUS_CACHE_SECONDS = 30.0
_cli_status_cache: dict[str, Any] = {"expires_at": 0.0}


def _motores_root(engine_cfg: SwarmRufloEngineConfig) -> Path:
    """RuFlo swarm state (.swarm/) lives in the gois repo, not OpenClaw workspace."""
    raw = getattr(engine_cfg, "project_dir", None)
    if raw:
        return Path(str(raw)).expanduser().resolve()
    return _repo_root()


def _cfg_at_root(cfg: RufloChatConfig, root: Path) -> RufloChatConfig:
    return cfg.model_copy(update={"project_dir": str(root)})


def _swarm_cli_status(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    root: Path,
    engine_cfg: SwarmRufloEngineConfig,
    *,
    timeout: Optional[float] = None,
) -> tuple[Optional[dict[str, Any]], bool, Optional[str]]:
    now = time.time()
    if now < float(_cli_status_cache.get("expires_at") or 0):
        cached = _cli_status_cache.get("payload")
        if isinstance(cached, tuple) and len(cached) == 3:
            return cached

    lim = float(
        timeout
        if timeout is not None
        else getattr(engine_cfg, "cli_timeout_seconds", 20.0) or 20.0
    )
    cfg_root = _cfg_at_root(cfg, root)
    code, stdout, stderr = run_ruflo(
        cfg_root,
        runtime,
        ["swarm", "status", "--format", "json"],
        timeout=lim,
    )
    data = _parse_json_stdout(stdout)
    ok = code == 0 and isinstance(data, dict)
    err = None if ok else (stderr or stdout or f"exit {code}").strip()[:400] or None
    payload = (data if isinstance(data, dict) else None, ok, err)
    _cli_status_cache["payload"] = payload
    _cli_status_cache["expires_at"] = now + _CLI_STATUS_CACHE_SECONDS
    return payload


def _apply_engine_defaults(
    state: dict[str, Any],
    engine_cfg: SwarmRufloEngineConfig,
) -> dict[str, Any]:
    topo = str(state.get("topology") or "").strip()
    if not topo or topo == "—":
        state["topology"] = str(
            getattr(engine_cfg, "topology", None) or "hierarchical-mesh"
        )
    return state


def _resolve_db_path(
    memory_cfg: SwarmMemoryConfig,
    engine_cfg: SwarmRufloEngineConfig,
    root: Path,
) -> Path:
    raw = (
        getattr(memory_cfg, "agentdb_path", None)
        or getattr(engine_cfg, "agentdb_path", None)
        or "./.swarm/memory.db"
    )
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _read_state_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("motores: failed to read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def normalize_swarm_state(
    raw: dict[str, Any],
    *,
    swarm_cli: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Map RuFlo state.json / CLI status to the Motores UI shape."""
    status = str(raw.get("status") or "").strip().lower()
    if isinstance(swarm_cli, dict):
        if swarm_cli.get("hasActiveSwarm"):
            status = "running"
        cli_status = swarm_cli.get("status")
        if cli_status:
            status = str(cli_status).strip().lower()

    max_agents = (
        raw.get("maxAgents")
        or raw.get("agents")
        or (swarm_cli or {}).get("maxAgents")
        or (swarm_cli or {}).get("agents")
        or 0
    )
    try:
        max_agents = int(max_agents)
    except (TypeError, ValueError):
        max_agents = 0

    active = status in _ACTIVE_STATUSES or bool(
        isinstance(swarm_cli, dict) and swarm_cli.get("hasActiveSwarm")
    )
    return {
        "id": raw.get("id") or raw.get("swarmId") or (swarm_cli or {}).get("swarmId") or "—",
        "topology": raw.get("topology") or (swarm_cli or {}).get("topology") or "—",
        "maxAgents": max_agents,
        "status": status or "stopped",
        "active": active,
        "objective": raw.get("objective") or (swarm_cli or {}).get("objective") or "",
        "strategy": raw.get("strategy") or (swarm_cli or {}).get("strategy") or "",
    }


def _agents_from_state(raw: dict[str, Any]) -> list[dict[str, Any]]:
    plan = raw.get("agentPlan")
    if not isinstance(plan, list):
        return []
    agents: list[dict[str, Any]] = []
    for row in plan:
        if not isinstance(row, dict):
            continue
        agent_type = str(row.get("type") or row.get("role") or "agent").strip()
        count = int(row.get("count") or 1)
        for idx in range(max(1, count)):
            suffix = f"-{idx + 1}" if count > 1 else ""
            agents.append(
                {
                    "type": f"{agent_type}{suffix}",
                    "model": "inherit",
                    "consensus": "raft",
                    "status": "idle",
                }
            )
    return agents


def _agents_from_ruflo_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    for worker in status.get("hive_workers") or []:
        if not isinstance(worker, dict):
            continue
        agents.append(
            {
                "type": str(worker.get("type") or worker.get("name") or "worker"),
                "model": "inherit",
                "consensus": "raft",
                "status": str(worker.get("status") or "unknown"),
            }
        )
    return agents


def _query_db_stats(db_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    db_stats = {
        "memory_entries": 0,
        "patterns": 0,
        "trajectories": 0,
        "db_size": 0,
    }
    patterns_list: list[dict[str, Any]] = []
    indexes_list: list[dict[str, Any]] = []
    if not db_path.is_file():
        return db_stats, patterns_list, indexes_list

    try:
        db_stats["db_size"] = db_path.stat().st_size
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        for table, key in (
            ("memory_entries", "memory_entries"),
            ("patterns", "patterns"),
            ("trajectories", "trajectories"),
        ):
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                db_stats[key] = cursor.fetchone()[0]
            except sqlite3.Error:
                pass

        try:
            cursor.execute(
                "SELECT pattern_type, source, confidence, updated_at "
                "FROM patterns ORDER BY confidence DESC LIMIT 50"
            )
            for row in cursor.fetchall():
                patterns_list.append(
                    {
                        "type": row[0],
                        "namespace": row[1] or "default",
                        "confidence": row[2],
                        "updated_at": row[3],
                    }
                )
        except sqlite3.Error:
            pass

        try:
            cursor.execute("SELECT id, name, dimensions FROM vector_indexes")
            for row in cursor.fetchall():
                indexes_list.append(
                    {"id": row[0], "name": row[1], "dimensions": row[2]}
                )
        except sqlite3.Error:
            pass

        conn.close()
    except Exception as exc:
        log.warning("motores: failed to query %s: %s", db_path, exc)

    return db_stats, patterns_list, indexes_list


def motores_status(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    *,
    memory_cfg: SwarmMemoryConfig,
    engine_cfg: SwarmRufloEngineConfig,
) -> dict[str, Any]:
    root = _motores_root(engine_cfg)
    raw_state = _read_state_file(root / ".swarm" / "state.json")
    local_state = _apply_engine_defaults(
        normalize_swarm_state(raw_state),
        engine_cfg,
    )

    swarm_cli: Optional[dict[str, Any]] = None
    swarm_ok = False
    swarm_error: Optional[str] = None
    status_source = "none"

    if local_state.get("active"):
        state = local_state
        swarm_ok = True
        status_source = "local"
    else:
        swarm_cli, swarm_ok, swarm_error = _swarm_cli_status(
            cfg, runtime, root, engine_cfg
        )
        state = _apply_engine_defaults(
            normalize_swarm_state(raw_state, swarm_cli=swarm_cli),
            engine_cfg,
        )
        status_source = "cli" if swarm_ok else "cli_error"

    agents = _agents_from_state(raw_state)

    db_path = _resolve_db_path(memory_cfg, engine_cfg, root)
    db_stats, patterns_list, indexes_list = _query_db_stats(db_path)

    return {
        "ok": True,
        "state": state,
        "db_stats": db_stats,
        "patterns_list": patterns_list,
        "indexes_list": indexes_list,
        "agents": agents,
        "swarm_ok": swarm_ok,
        "swarm_error": swarm_error if not state.get("active") else None,
        "status_source": status_source,
        "project_dir": str(root),
        "db_path": str(db_path),
    }


def invalidate_motores_cli_cache() -> None:
    _cli_status_cache["expires_at"] = 0.0


def motores_start(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    engine_cfg: SwarmRufloEngineConfig,
    *,
    objective: str = "Continuous integration",
) -> dict[str, Any]:
    text = str(objective or "Continuous integration").strip() or "Continuous integration"
    result: dict[str, Any] = {"ok": False, "objective": text}
    cfg_root = _cfg_at_root(cfg, _motores_root(engine_cfg))

    if getattr(engine_cfg, "init_before_run", True):
        code, stdout, stderr = run_ruflo(
            cfg_root,
            runtime,
            [
                "swarm",
                "init",
                "--v3-mode",
                "--topology",
                str(getattr(engine_cfg, "topology", "hierarchical-mesh") or "hierarchical-mesh"),
                "--max-agents",
                str(int(getattr(engine_cfg, "max_agents", 15) or 15)),
                "--strategy",
                str(getattr(engine_cfg, "strategy", "specialized") or "specialized"),
            ],
            timeout=float(getattr(engine_cfg, "cli_timeout_seconds", 45.0) or 45.0),
        )
        init_data = _parse_json_stdout(stdout)
        init_ok = code == 0 or init_data is not None
        result["init"] = {
            "ok": init_ok,
            "exit_code": code,
            "error": None if init_ok else (stderr or stdout or f"exit {code}")[:400],
        }
        if not init_ok:
            result["error"] = result["init"]["error"]
            return result

    strategy = str(getattr(engine_cfg, "task_type", "development") or "development")
    start_args = ["swarm", "start", "-o", text[:500], "-s", strategy]
    if getattr(engine_cfg, "parallel_start", True):
        start_args.append("--parallel")

    code, stdout, stderr = run_ruflo(
        cfg_root,
        runtime,
        start_args,
        timeout=float(getattr(engine_cfg, "cli_timeout_seconds", 45.0) or 45.0),
    )
    start_data = _parse_json_stdout(stdout)
    start_ok = code == 0 or start_data is not None
    result["start"] = {
        "ok": start_ok,
        "exit_code": code,
        "error": None if start_ok else (stderr or stdout or f"exit {code}")[:400],
    }
    result["ok"] = start_ok
    if not start_ok:
        result["error"] = result["start"]["error"]
    else:
        invalidate_motores_cli_cache()
    return result


def motores_stop(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    engine_cfg: SwarmRufloEngineConfig,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    cfg_root = _cfg_at_root(cfg, _motores_root(engine_cfg))
    code, stdout, stderr = run_ruflo(
        cfg_root,
        runtime,
        ["swarm", "stop"],
        timeout=timeout,
    )
    ok = code == 0
    if ok:
        invalidate_motores_cli_cache()
    return {
        "ok": ok,
        "exit_code": code,
        "error": None if ok else (stderr or stdout or f"exit {code}")[:400],
    }


def motores_rebuild(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    *,
    memory_cfg: SwarmMemoryConfig,
    engine_cfg: SwarmRufloEngineConfig,
    timeout: float = 15.0,
) -> dict[str, Any]:
    root = _motores_root(engine_cfg)
    cfg_root = _cfg_at_root(cfg, root)
    db_path = _resolve_db_path(memory_cfg, engine_cfg, root)
    rel = os.path.relpath(db_path, root)
    code, stdout, stderr = run_ruflo(
        cfg_root,
        runtime,
        ["memory", "init", "-f", "-p", rel],
        timeout=timeout,
    )
    ok = code == 0
    return {
        "ok": ok,
        "exit_code": code,
        "db_path": str(db_path),
        "error": None if ok else (stderr or stdout or f"exit {code}")[:400],
    }

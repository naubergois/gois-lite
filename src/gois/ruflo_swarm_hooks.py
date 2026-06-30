"""RuFlo hooks bridge for Hermes swarm graph nodes (integration phase 2).

After each graph node completes, optionally:
* ``memory_store`` — persist handoff output (AgentDB + RuFlo CLI)
* ``hooks_post-task`` — record learned patterns in AgentDB + RuFlo CLI
* ``hooks_route`` — suggest routing before / during execution (RuFlo CLI)

Non-disruptive: when disabled, the graph behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .ruflo_memory_guard import memory_db_session
from .ruflo_transport import RufloCommandRunner

log = logging.getLogger(__name__)

_PATTERNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  pattern_type TEXT NOT NULL,
  condition TEXT NOT NULL,
  action TEXT NOT NULL,
  description TEXT,
  confidence REAL DEFAULT 0.5,
  success_count INTEGER DEFAULT 0,
  failure_count INTEGER DEFAULT 0,
  decay_rate REAL DEFAULT 0.01,
  half_life_days INTEGER DEFAULT 30,
  embedding TEXT,
  embedding_dimensions INTEGER,
  version INTEGER DEFAULT 1,
  parent_id TEXT,
  tags TEXT,
  metadata TEXT,
  source TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_matched_at INTEGER,
  last_success_at INTEGER,
  last_failure_at INTEGER,
  status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_patterns_source ON patterns(source);
"""


def _slug(swarm: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", str(swarm or "").strip().lower()).strip("-")
    return slug[:64] or "swarm"


def _parse_json_stdout(stdout: str) -> Optional[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


class RufloSwarmHooks:
    """Best-effort RuFlo hooks invoked by the Hermes swarm graph."""

    backend = "ruflo_hooks"

    def __init__(self, cfg: Any, *, light_override: Optional[bool] = None) -> None:
        self._cfg = cfg
        self._light = (
            bool(light_override)
            if light_override is not None
            else bool(getattr(cfg, "light", False))
        )
        self._db_path = str(Path(getattr(cfg, "agentdb_path", "./.swarm/memory.db")).expanduser())
        self._project_dir = Path(
            getattr(cfg, "project_dir", None) or Path.cwd()
        ).expanduser().resolve()
        self._timeout = float(getattr(cfg, "cli_timeout_seconds", 15.0) or 15.0)
        self._use_cli = bool(getattr(cfg, "use_cli", True))
        self._runner = RufloCommandRunner(cfg)
        self._last_routing: Optional[dict[str, Any]] = None
        self._ensure_patterns_schema()

    def _cli_available(self) -> bool:
        return self._runner.available()

    def _run_cli(
        self, subargs: list[str], *, timeout: Optional[float] = None
    ) -> tuple[int, str, str]:
        return self._runner.run(subargs, timeout=timeout)

    def close_transport(self) -> None:
        self._runner.close()

    def _session(self):
        min_free_mb = int(getattr(self._cfg, "db_min_free_mb", 512) or 512)
        lock_enabled = bool(getattr(self._cfg, "db_lock_enabled", True))
        return memory_db_session(
            Path(self._db_path),
            min_free_mb=min_free_mb,
            lock_enabled=lock_enabled,
        )

    def _ensure_patterns_schema(self) -> None:
        try:
            with self._session() as conn:
                conn.executescript(_PATTERNS_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            log.warning("ruflo_hooks: patterns schema init failed: %s", exc)

    def _light_mode(self) -> bool:
        return self._light

    def route_task(self, task: str) -> dict[str, Any]:
        """``hooks_route`` — suggest agents/topology for a swarm objective."""
        if self._light_mode() or not getattr(self._cfg, "route", True):
            return {"ok": False, "skipped": True, "reason": "light mode"}
        text = str(task or "").strip()
        if not text:
            return {"ok": False, "error": "empty task"}
        if not self._cli_available():
            return {"ok": False, "error": "ruflo CLI unavailable"}
        code, stdout, stderr = self._run_cli(
            ["hooks", "route", "-t", text, "--format", "json"]
        )
        data = _parse_json_stdout(stdout)
        if data is None:
            return {
                "ok": False,
                "error": (stderr or stdout or f"exit {code}").strip()[:400],
            }
        self._last_routing = data
        return {"ok": True, "routing": data}

    def suggest_next(
        self,
        objective: str,
        current_node: str,
        candidates: list[str],
    ) -> Optional[str]:
        """Use ``hooks_route`` to pick among dynamic handoff candidates."""
        if not candidates or self._light_mode() or not getattr(self._cfg, "route", True):
            return None
        if not self._cli_available():
            return None
        task = (
            f"Swarm objective: {objective}\n"
            f"Current node: {current_node}\n"
            f"Choose next agent from: {', '.join(candidates)}"
        )
        routed = self.route_task(task)
        if not routed.get("ok"):
            return None
        routing = routed.get("routing") or {}
        primary = (
            routing.get("primaryAgent")
            or routing.get("agent")
            or routing.get("recommendedAgent")
        )
        pick = ""
        if isinstance(primary, dict):
            pick = str(
                primary.get("type") or primary.get("name") or primary.get("id") or ""
            ).strip()
        elif isinstance(primary, str):
            pick = primary.strip()
        if not pick:
            agents = routing.get("agents") or routing.get("recommendedAgents") or []
            if isinstance(agents, list) and agents:
                first = agents[0]
                if isinstance(first, dict):
                    pick = str(first.get("type") or first.get("name") or "").strip()
                elif isinstance(first, str):
                    pick = first.strip()
        if not pick:
            return None
        low_pick = pick.lower()
        for cand in candidates:
            if cand.lower() == low_pick or _slug(cand) == _slug(pick):
                return cand
        return None

    def on_node_complete(
        self,
        *,
        swarm_name: str,
        node_name: str,
        role: str,
        output: str,
        objective: str,
        success: bool = True,
    ) -> dict[str, Any]:
        """``memory_store`` + ``hooks_post-task`` after a Hermes node finishes."""
        result: dict[str, Any] = {"ok": True, "hooks": []}
        text = str(output or "").strip()
        if not text:
            return {"ok": True, "skipped": True, "reason": "empty output"}

        if self._light_mode():
            if getattr(self._cfg, "post_task", True):
                result["hooks"].append(
                    self._post_task(
                        swarm_name,
                        node_name,
                        role,
                        text,
                        objective,
                        success,
                        cli=False,
                    )
                )
            return result

        if getattr(self._cfg, "memory_store", True):
            result["hooks"].append(self._memory_store(swarm_name, node_name, text))
        if getattr(self._cfg, "post_task", True):
            result["hooks"].append(
                self._post_task(
                    swarm_name, node_name, role, text, objective, success, cli=True
                )
            )
        return result

    def _memory_store(
        self, swarm_name: str, node_name: str, content: str
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"hook": "memory_store"}
        if self._cli_available():
            ns = f"swarm:{_slug(swarm_name)}"
            key = f"{node_name}-{int(time.time() * 1000)}"
            preview = content[:4000]
            code, stdout, stderr = self._run_cli(
                [
                    "memory",
                    "store",
                    "--namespace",
                    ns,
                    "--key",
                    key,
                    "--value",
                    preview,
                ]
            )
            out["cli_ok"] = code == 0
            if code != 0:
                out["cli_error"] = (stderr or stdout or f"exit {code}").strip()[:300]
        else:
            out["cli_ok"] = False
            out["cli_error"] = "ruflo CLI unavailable"
        return out

    def _post_task(
        self,
        swarm_name: str,
        node_name: str,
        role: str,
        output: str,
        objective: str,
        success: bool,
        *,
        cli: bool = True,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"hook": "hooks_post-task"}
        if self._light_mode():
            out["light"] = True
        task_id = f"{_slug(swarm_name)}:{node_name}:{int(time.time() * 1000)}"
        out["task_id"] = task_id
        out["db_ok"] = self._write_pattern(
            swarm_name, node_name, role, output, objective, success
        )
        if cli and not self._light_mode():
            if self._cli_available():
                code, stdout, stderr = self._run_cli(
                    [
                        "hooks",
                        "post-task",
                        "--task-id",
                        task_id,
                        "--success",
                        "true" if success else "false",
                        "--store-results",
                        "true",
                    ]
                )
                out["cli_ok"] = code == 0
                if code != 0:
                    out["cli_error"] = (stderr or stdout or f"exit {code}").strip()[:300]
            else:
                out["cli_ok"] = False
                out["cli_error"] = "ruflo CLI unavailable"
        return out

    def _write_pattern(
        self,
        swarm_name: str,
        node_name: str,
        role: str,
        output: str,
        objective: str,
        success: bool,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        pattern_id = str(uuid.uuid4())
        source = f"swarm:{_slug(swarm_name)}"
        metadata = {
            "swarm": swarm_name,
            "node": node_name,
            "role": role,
            "objective": objective[:500],
            "success": success,
            "source": "gois",
        }
        condition = json.dumps(
            {"swarm": swarm_name, "node": node_name, "role": role},
            ensure_ascii=False,
        )
        action = json.dumps(
            {"handoff": "complete", "output_chars": len(output)},
            ensure_ascii=False,
        )
        confidence = 0.75 if success else 0.35
        try:
            with self._session() as conn:
                conn.execute(
                    """
                    INSERT INTO patterns (
                      id, name, pattern_type, condition, action, description,
                      confidence, success_count, failure_count, tags, metadata,
                      source, created_at, updated_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        pattern_id,
                        f"{swarm_name}:{node_name}",
                        "workflow",
                        condition,
                        action,
                        output[:800],
                        confidence,
                        1 if success else 0,
                        0 if success else 1,
                        json.dumps(["swarm", "hermes", node_name], ensure_ascii=False),
                        json.dumps(metadata, ensure_ascii=False),
                        source,
                        now_ms,
                        now_ms,
                    ),
                )
            return True
        except sqlite3.Error as exc:
            log.warning("ruflo_hooks: pattern write failed: %s", exc)
            return False

    @property
    def last_routing(self) -> Optional[dict[str, Any]]:
        return self._last_routing


def build_ruflo_swarm_hooks(
    cfg: Any, *, light: Optional[bool] = None
) -> Optional[RufloSwarmHooks]:
    """Build hooks from ``SwarmRufloHooksConfig`` (or return None)."""
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    return RufloSwarmHooks(cfg, light_override=light)


def swarm_run_light_mode(cfg: Any, payload: Optional[dict[str, Any]] = None) -> bool:
    """True when a graph/team run should skip heavy RuFlo CLI hooks."""
    if payload:
        flag = str(payload.get("light") or payload.get("fast") or "").strip().lower()
        if flag in ("1", "true", "yes", "on"):
            return True
    hooks_cfg = getattr(cfg, "swarm_ruflo_hooks", None)
    return bool(getattr(hooks_cfg, "light", False))


def wrap_router_with_ruflo(
    base_router: Any,
    hooks: Optional[RufloSwarmHooks],
) -> Any:
    """Prefer RuFlo ``hooks_route`` suggestion when it matches a candidate."""
    if hooks is None or base_router is None:
        return base_router

    def _router(node: Any, run: Any, candidates: list[str]) -> Optional[str]:
        pick = hooks.suggest_next(
            str(getattr(run, "objective", "") or ""),
            str(getattr(node, "name", "") or ""),
            list(candidates or []),
        )
        if pick:
            return pick
        return base_router(node, run, candidates)

    return _router

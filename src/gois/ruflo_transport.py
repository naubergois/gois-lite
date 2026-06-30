"""RuFlo command transport — MCP with CLI fallback (integration phase 6)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from .local_paths import _repo_root
from .cache_paths import cache_subprocess_env
from .ruflo_mcp_client import RufloMcpSession
from .ruflo_memory_guard import ensure_memory_db_ready, memory_cli_lock_context
from .ruflo_memory_repair import resolve_ruflo_memory_db_path

log = logging.getLogger(__name__)


def _parse_flag(subargs: list[str], flag: str, default: str = "") -> str:
    if flag not in subargs:
        return default
    idx = subargs.index(flag)
    if idx + 1 >= len(subargs):
        return default
    return str(subargs[idx + 1])


def cli_subargs_to_mcp(subargs: list[str]) -> Optional[tuple[str, dict[str, Any]]]:
    """Map RuFlo CLI argv to an MCP tool call."""
    if len(subargs) < 2:
        return None
    head, cmd = subargs[0], subargs[1]
    if head == "hooks" and cmd == "route":
        return "hooks_route", {"task": _parse_flag(subargs, "-t")}
    if head == "hooks" and cmd == "post-task":
        return "hooks_post-task", {
            "taskId": _parse_flag(subargs, "--task-id"),
            "success": _parse_flag(subargs, "--success", "true").lower() == "true",
            "result": _parse_flag(subargs, "--result"),
        }
    if head == "memory" and cmd == "store":
        return "memory_store", {
            "namespace": _parse_flag(subargs, "--namespace", "patterns"),
            "key": _parse_flag(subargs, "--key"),
            "value": _parse_flag(subargs, "--value"),
        }
    if head == "swarm" and cmd == "init":
        return "swarm_init", {
            "topology": _parse_flag(subargs, "--topology", "hierarchical-mesh"),
            "maxAgents": int(_parse_flag(subargs, "--max-agents", "15") or 15),
            "strategy": _parse_flag(subargs, "--strategy", "specialized"),
            "v3Mode": "--v3-mode" in subargs,
        }
    if head == "swarm" and cmd == "start":
        return "swarm_start", {
            "objective": _parse_flag(subargs, "-o"),
            "strategy": _parse_flag(subargs, "-s", "development"),
            "parallel": "--parallel" in subargs,
        }
    if head == "swarm" and cmd == "stop":
        return "swarm_stop", {}
    if head == "swarm" and cmd == "health":
        return "swarm_health", {}
    if head == "agent" and cmd == "spawn":
        return "agent_spawn", {
            "type": _parse_flag(subargs, "--type"),
            "task": _parse_flag(subargs, "--task"),
        }
    return None


class RufloCommandRunner:
    """Execute RuFlo operations via MCP (preferred) or CLI."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self._project_dir = Path(
            getattr(cfg, "project_dir", None) or Path.cwd()
        ).expanduser().resolve()
        self._timeout = float(getattr(cfg, "cli_timeout_seconds", 45.0) or 45.0)
        self._mcp_timeout = float(
            getattr(cfg, "mcp_timeout_seconds", self._timeout) or self._timeout
        )
        self._transport = str(getattr(cfg, "transport", "auto") or "auto").lower()
        self._use_cli = bool(getattr(cfg, "use_cli", True))
        self._mcp: Optional[RufloMcpSession] = None
        self._last_backend = "none"

    @property
    def last_backend(self) -> str:
        return self._last_backend

    def _cli_available(self) -> bool:
        if not self._use_cli:
            return False
        return shutil.which(str(getattr(self._cfg, "ruflo_bin", "npx") or "npx")) is not None

    def available(self) -> bool:
        from .ruflo_mcp_client import mcp_available as _mcp_available

        mode = self._transport
        if mode == "mcp":
            return _mcp_available(ruflo_bin=str(getattr(self._cfg, "ruflo_bin", "npx")))
        if mode == "cli":
            return self._cli_available()
        return self._cli_available() or _mcp_available(
            ruflo_bin=str(getattr(self._cfg, "ruflo_bin", "npx"))
        )

    def _ensure_mcp(self) -> Optional[RufloMcpSession]:
        from .ruflo_mcp_client import RufloMcpSession as _RufloMcpSession

        if self._mcp is not None and self._mcp.ok:
            return self._mcp
        if self._mcp is not None:
            self._mcp.close()
        env = cache_subprocess_env()
        for key in (
            "CLAUDE_FLOW_MODE",
            "CLAUDE_FLOW_HOOKS_ENABLED",
            "CLAUDE_FLOW_TOPOLOGY",
            "CLAUDE_FLOW_MAX_AGENTS",
            "CLAUDE_FLOW_MEMORY_BACKEND",
        ):
            import os

            val = os.environ.get(key)
            if val:
                env[key] = val
        self._mcp = _RufloMcpSession(
            ruflo_bin=str(getattr(self._cfg, "ruflo_bin", "npx") or "npx"),
            ruflo_args=list(getattr(self._cfg, "ruflo_args", None) or ["-y", "ruflo@latest"]),
            project_dir=str(self._project_dir),
            timeout=self._mcp_timeout,
            env=env,
        )
        return self._mcp if self._mcp.ok else None

    def _memory_db_path(self) -> Optional[Path]:
        return resolve_ruflo_memory_db_path(
            swarm_memory_cfg=getattr(self._cfg, "swarm_memory_cfg", None),
            ruflo_chat_cfg=getattr(self._cfg, "ruflo_chat_cfg", None),
            repo_root=_repo_root(),
        )

    def _run_cli(
        self, subargs: list[str], *, timeout: Optional[float] = None
    ) -> tuple[int, str, str]:
        cmd = [
            str(getattr(self._cfg, "ruflo_bin", "npx") or "npx"),
            *list(getattr(self._cfg, "ruflo_args", None) or ["-y", "ruflo@latest"]),
            *subargs,
        ]
        lim = float(timeout if timeout is not None else self._timeout)
        db_path = self._memory_db_path()
        lock_enabled = bool(getattr(self._cfg, "db_lock_enabled", True))
        min_free_mb = int(getattr(self._cfg, "db_min_free_mb", 512) or 512)
        try:
            with memory_cli_lock_context(db_path, subargs, lock_enabled=lock_enabled):
                if db_path and subargs and subargs[0] == "memory":
                    ready = ensure_memory_db_ready(
                        db_path,
                        min_free_mb=min_free_mb,
                        auto_repair=True,
                    )
                    if not ready.get("ok"):
                        err = ready.get("error") or "memory_db_not_ready"
                        return 1, "", f"[ERROR] {err}"
                proc = subprocess.run(
                    cmd,
                    cwd=str(self._project_dir),
                    capture_output=True,
                    text=True,
                    timeout=max(3.0, lim),
                    env=cache_subprocess_env(),
                )
                return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {lim:.0f}s"
        except OSError as exc:
            return 127, "", str(exc)

    def run(
        self, subargs: list[str], *, timeout: Optional[float] = None
    ) -> tuple[int, str, str]:
        """CLI-compatible runner (exit, stdout, stderr)."""
        mode = self._transport
        mapped = cli_subargs_to_mcp(subargs)
        try_mcp = mode in ("mcp", "auto") and mapped is not None
        if try_mcp:
            try:
                session = self._ensure_mcp()
                if session is not None:
                    tool, args = mapped
                    data = session.call_tool(tool, args)
                    self._last_backend = "mcp"
                    text = json.dumps(data, ensure_ascii=False)
                    code = 0 if data.get("ok", True) else 1
                    err = "" if code == 0 else str(data.get("error") or text)[:400]
                    return code, text, err
            except Exception as exc:
                log.warning(
                    "RuFlo MCP path failed (%s: %s); falling back to CLI",
                    type(exc).__name__,
                    exc,
                )
            if mode == "mcp":
                self._last_backend = "mcp"
                return 127, "", "mcp session unavailable"

        if not self._cli_available():
            self._last_backend = "none"
            return 127, "", "ruflo transport unavailable"
        self._last_backend = "cli"
        return self._run_cli(subargs, timeout=timeout)

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None

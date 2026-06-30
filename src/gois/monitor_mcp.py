"""MCP server config discovery and process health for the monitor dashboard."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

MCP_CONFIG_PATHS = (
    Path(".mcp.json"),
    Path(".kiro/settings/mcp.json"),
    Path(".cursor/mcp.json"),
)


def load_mcp_config() -> dict[str, Any]:
    """Read merged MCP server config from known locations (later files override)."""
    merged: dict[str, Any] = {}
    for rel in MCP_CONFIG_PATHS:
        path = Path.cwd() / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or {}
            merged.update(servers)
        except Exception:
            pass
    from .gois_lite import filter_lite_mcp_servers

    return filter_lite_mcp_servers(merged)


@dataclass(frozen=True)
class _McpProcess:
    pid: int
    command: str


def _list_mcp_candidate_processes() -> list[_McpProcess]:
    """Scan the host for MCP server processes (not limited to QClaw catalogue)."""
    try:
        result = subprocess.run(
            ["ps", "-axwwo", "pid,command"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("mcp process scan failed: %s", exc)
        return []
    if result.returncode != 0:
        return []

    procs: list[_McpProcess] = []
    for raw in result.stdout.splitlines()[1:]:
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            procs.append(_McpProcess(pid=int(parts[0]), command=parts[1]))
        except ValueError:
            continue
    return procs


def _process_matches_mcp(proc_command: str, cfg: dict[str, Any]) -> bool:
    """Return True when *proc_command* looks like the configured MCP server."""
    cmd_lower = proc_command.lower()
    command = str(cfg.get("command") or "")
    args = [str(a) for a in (cfg.get("args") or [])]

    signature_parts = [command, *args]
    signature = " ".join(signature_parts).lower()
    if signature and signature in cmd_lower:
        return True

    # Python MCP: Cursor may spawn a different interpreter than config.command.
    if len(args) >= 2 and args[0] == "-m":
        module = args[1].lower()
        if module in cmd_lower and (" -m " in f" {cmd_lower} " or f" {module}" in cmd_lower):
            return True

    if len(args) >= 1:
        key_arg = args[-1].lower()
        if key_arg and key_arg in cmd_lower and command.lower() in cmd_lower:
            return True

    return False


def mcp_servers_status(processes_cache: list | None = None) -> list[dict[str, Any]]:
    """Check which configured MCP servers have matching running processes."""
    servers_cfg = load_mcp_config()
    if not servers_cfg:
        return []

    if processes_cache is not None:
        procs = [
            _McpProcess(pid=p.pid, command=p.command)
            for p in processes_cache
            if getattr(p, "command", None) and getattr(p, "pid", None) is not None
        ]
    else:
        procs = _list_mcp_candidate_processes()

    results: list[dict[str, Any]] = []
    for name, cfg in servers_cfg.items():
        if not isinstance(cfg, dict):
            continue
        disabled = cfg.get("disabled", False)
        command = cfg.get("command", "")

        up = False
        pid: Optional[int] = None
        for proc in procs:
            if _process_matches_mcp(proc.command, cfg):
                up = True
                pid = proc.pid
                break

        results.append({
            "name": name,
            "up": up and not disabled,
            "disabled": disabled,
            "command": command,
            "pid": pid,
        })
    return results

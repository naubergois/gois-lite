"""OpenClaw native tool catalog counts for the monitor dashboard."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from .config import OpenclawDoctorConfig
from .openclaw_chat import QclawRuntime, _openclaw_cli_env

log = logging.getLogger(__name__)

_TOOL_ALIASES = {"bash": "exec", "apply-patch": "apply_patch"}

# Mirrors vendor/openclaw/src/agents/tool-catalog.ts CORE_TOOL_GROUPS (subset).
_TOOL_GROUPS: dict[str, list[str]] = {
    "group:openclaw": [
        "read",
        "write",
        "edit",
        "apply_patch",
        "exec",
        "process",
        "code_execution",
        "web_search",
        "web_fetch",
        "x_search",
        "memory_search",
        "memory_get",
        "sessions_list",
        "sessions_history",
        "sessions_send",
        "sessions_spawn",
        "sessions_yield",
        "subagents",
        "session_status",
        "browser",
        "message",
        "heartbeat_respond",
        "cron",
        "gateway",
        "nodes",
        "agents_list",
        "get_goal",
        "create_goal",
        "update_goal",
        "update_plan",
        "skill_workshop",
        "image",
        "image_generate",
        "music_generate",
        "video_generate",
        "tts",
    ],
    "group:fs": ["read", "write", "edit", "apply_patch"],
    "group:runtime": ["exec", "process", "code_execution"],
    "group:web": ["web_search", "web_fetch", "x_search"],
    "group:memory": ["memory_search", "memory_get"],
    "group:sessions": [
        "sessions_list",
        "sessions_history",
        "sessions_send",
        "sessions_spawn",
        "sessions_yield",
        "subagents",
        "session_status",
    ],
    "group:messaging": ["message"],
    "group:automation": ["heartbeat_respond", "cron", "gateway"],
    "group:nodes": ["nodes"],
    "group:agents": [
        "agents_list",
        "get_goal",
        "create_goal",
        "update_goal",
        "update_plan",
        "skill_workshop",
    ],
    "group:media": ["image", "image_generate", "music_generate", "video_generate", "tts"],
    "bundle-mcp": ["group:plugins"],
    "group:plugins": [],
}

_PROFILE_ALLOW: dict[str, list[str]] = {
    "minimal": ["session_status"],
    "coding": [
        "read",
        "write",
        "edit",
        "apply_patch",
        "exec",
        "process",
        "code_execution",
        "web_search",
        "web_fetch",
        "x_search",
        "memory_search",
        "memory_get",
        "sessions_list",
        "sessions_history",
        "sessions_send",
        "sessions_spawn",
        "sessions_yield",
        "subagents",
        "session_status",
        "cron",
        "get_goal",
        "create_goal",
        "update_goal",
        "update_plan",
        "skill_workshop",
        "image",
        "image_generate",
        "music_generate",
        "video_generate",
        "tts",
        "bundle-mcp",
    ],
    "messaging": [
        "sessions_list",
        "sessions_history",
        "sessions_send",
        "session_status",
        "message",
        "bundle-mcp",
    ],
    "full": ["*"],
}


def _normalize_tool_name(name: str) -> str:
    key = (name or "").strip().lower()
    return _TOOL_ALIASES.get(key, key)


def _expand_tool_groups(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = _normalize_tool_name(raw)
        if not value:
            continue
        expanded = _TOOL_GROUPS.get(value)
        if expanded:
            for entry in _expand_tool_groups(expanded):
                if entry not in seen:
                    seen.add(entry)
                    out.append(entry)
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _compile_patterns(patterns: Optional[list[str]]) -> list[tuple[str, str | re.Pattern[str]]]:
    compiled: list[tuple[str, str | re.Pattern[str]]] = []
    if not patterns:
        return compiled
    for pattern in _expand_tool_groups([str(p) for p in patterns if str(p).strip()]):
        if pattern == "*":
            compiled.append(("all", "*"))
        elif "*" not in pattern:
            compiled.append(("exact", pattern))
        else:
            escaped = re.escape(pattern).replace(r"\*", ".*")
            compiled.append(("regex", re.compile(f"^{escaped}$")))
    return compiled


def _matches_any(name: str, patterns: list[tuple[str, str | re.Pattern[str]]]) -> bool:
    for kind, value in patterns:
        if kind == "all":
            return True
        if kind == "exact" and name == value:
            return True
        if kind == "regex" and isinstance(value, re.Pattern) and value.match(name):
            return True
    return False


def _is_allowed_by_policy(name: str, policy: Optional[dict[str, list[str]]]) -> bool:
    if not policy:
        return True
    normalized = _normalize_tool_name(name)
    deny = _compile_patterns(policy.get("deny"))
    if _matches_any(normalized, deny):
        return False
    allow = _compile_patterns(policy.get("allow"))
    if not allow:
        return True
    if _matches_any(normalized, allow):
        return True
    if normalized == "apply_patch" and _matches_any("exec", allow):
        return True
    return False


def _matches_list(name: str, items: Optional[list[str]]) -> bool:
    if not items:
        return False
    return _is_allowed_by_policy(name, {"allow": list(items)})


def _resolve_profile_policy(profile: str) -> Optional[dict[str, list[str]]]:
    allow = _PROFILE_ALLOW.get(profile.strip().lower())
    if not allow:
        return None
    return {"allow": list(allow)}


def _load_openclaw_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_agent_tools_profile(cfg: dict[str, Any], agent_id: str) -> tuple[str, str]:
    """Return (profile_id, profile_source_label)."""
    global_tools = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    entries = agents.get("list") if isinstance(agents.get("list"), list) else []
    entry: dict[str, Any] = {}
    for row in entries:
        if isinstance(row, dict) and str(row.get("id") or "") == agent_id:
            entry = row
            break
    agent_tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
    if agent_tools.get("profile"):
        return str(agent_tools["profile"]).strip().lower(), "agente"
    if isinstance(global_tools, dict) and global_tools.get("profile"):
        return str(global_tools["profile"]).strip().lower(), "global"
    return "full", "padrão"


def _resolve_base_policy(cfg: dict[str, Any], agent_id: str) -> dict[str, Any]:
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    entries = agents.get("list") if isinstance(agents.get("list"), list) else []
    entry: dict[str, Any] = {}
    for row in entries:
        if isinstance(row, dict) and str(row.get("id") or "") == agent_id:
            entry = row
            break
    agent_tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
    global_tools = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
    allow = agent_tools.get("allow")
    if isinstance(allow, list) and allow:
        deny = agent_tools.get("deny") if isinstance(agent_tools.get("deny"), list) else []
        return {"allow": [str(x) for x in allow], "deny": [str(x) for x in deny]}
    profile, _source = _resolve_agent_tools_profile(cfg, agent_id)
    policy = _resolve_profile_policy(profile) or {}
    also_allow = agent_tools.get("alsoAllow")
    if isinstance(also_allow, list) and also_allow:
        merged = list(policy.get("allow") or [])
        merged.extend(str(x) for x in also_allow)
        policy = {**policy, "allow": _expand_tool_groups(merged)}
    deny = agent_tools.get("deny")
    if isinstance(deny, list) and deny:
        policy = {**policy, "deny": [str(x) for x in deny]}
    return policy


def _catalog_tool_ids(catalog: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for group in catalog.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for tool in group.get("tools") or []:
            if isinstance(tool, dict) and tool.get("id"):
                ids.append(str(tool["id"]))
    return ids


def _count_enabled_tools(tool_ids: list[str], policy: dict[str, Any]) -> int:
    return sum(1 for tool_id in tool_ids if _is_allowed_by_policy(tool_id, policy))


def _gateway_call_tools_catalog(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    *,
    agent_id: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    bin_path, env = _openclaw_cli_env(runtime, doctor_cfg)
    if not bin_path:
        raise RuntimeError("openclaw CLI not found (configure openclaw_doctor paths)")
    cmd = [
        str(bin_path),
        "gateway",
        "call",
        "tools.catalog",
        "--json",
        "--params",
        json.dumps({"agentId": agent_id, "includePlugins": True}),
        "--timeout",
        str(max(1000, int(timeout_seconds * 1000))),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 2.0,
        env=env or None,
        cwd=str(runtime.state_dir) if runtime.state_dir.is_dir() else None,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(detail)
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError("empty gateway response")
    payload = json.loads(raw)
    if isinstance(payload, dict) and payload.get("ok") is False:
        err = payload.get("error") or payload.get("message") or payload
        raise RuntimeError(str(err))
    if isinstance(payload, dict) and "payload" in payload:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            return inner
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("unexpected tools.catalog response shape")


def openclaw_native_tools_status(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    *,
    agent_id: str = "main",
    gateway_timeout_seconds: float = 2.5,
) -> dict[str, Any]:
    """Count OpenClaw tools available for an agent (catalog + profile policy)."""
    cfg = _load_openclaw_config(runtime.config_path)
    profile, profile_source = _resolve_agent_tools_profile(cfg, agent_id)
    policy = _resolve_base_policy(cfg, agent_id)
    result: dict[str, Any] = {
        "agent_id": agent_id,
        "tools_profile": profile,
        "tools_profile_source": profile_source,
        "catalog_total": 0,
        "tools_enabled": 0,
        "tools_available": 0,
        "gateway_catalog": False,
        "scan_error": None,
    }
    catalog: Optional[dict[str, Any]] = None
    try:
        catalog = _gateway_call_tools_catalog(
            runtime,
            doctor_cfg,
            agent_id=agent_id,
            timeout_seconds=gateway_timeout_seconds,
        )
        result["gateway_catalog"] = True
    except Exception as e:
        result["scan_error"] = f"{type(e).__name__}: {e}"
        log.debug("tools.catalog via gateway failed: %s", e)

    if catalog:
        tool_ids = _catalog_tool_ids(catalog)
        resolved_profile = str(catalog.get("agentId") or agent_id)
        result["agent_id"] = resolved_profile
    else:
        # Static core catalog when gateway is down (plugins omitted).
        tool_ids = list(_TOOL_GROUPS["group:openclaw"])

    result["catalog_total"] = len(tool_ids)
    enabled = _count_enabled_tools(tool_ids, policy)
    result["tools_enabled"] = enabled
    result["tools_available"] = enabled
    return result


def list_openclaw_native_tools(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    *,
    agent_id: str = "main",
    gateway_timeout_seconds: float = 2.5,
) -> dict[str, Any]:
    """List OpenClaw native tools with enablement for UI/API."""
    cfg = _load_openclaw_config(runtime.config_path)
    profile, profile_source = _resolve_agent_tools_profile(cfg, agent_id)
    policy = _resolve_base_policy(cfg, agent_id)
    result: dict[str, Any] = {
        "ok": True,
        "agent_id": agent_id,
        "tools_profile": profile,
        "tools_profile_source": profile_source,
        "catalog_total": 0,
        "tools_enabled": 0,
        "gateway_catalog": False,
        "scan_error": None,
        "tools": [],
    }
    catalog: Optional[dict[str, Any]] = None
    try:
        catalog = _gateway_call_tools_catalog(
            runtime,
            doctor_cfg,
            agent_id=agent_id,
            timeout_seconds=gateway_timeout_seconds,
        )
        result["gateway_catalog"] = True
    except Exception as e:
        result["scan_error"] = f"{type(e).__name__}: {e}"
        log.debug("tools.catalog via gateway failed: %s", e)

    tool_rows: list[dict[str, Any]] = []
    if catalog:
        result["agent_id"] = str(catalog.get("agentId") or agent_id)
        for group in catalog.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id") or group.get("name") or "")
            for tool in group.get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                tool_id = str(tool.get("id") or tool.get("name") or "").strip()
                if not tool_id:
                    continue
                tool_rows.append(
                    {
                        "id": tool_id,
                        "name": str(tool.get("name") or tool_id),
                        "description": str(tool.get("description") or "").strip(),
                        "group": group_id,
                        "source": "openclaw",
                        "enabled": _is_allowed_by_policy(tool_id, policy),
                    }
                )
    else:
        for tool_id in _TOOL_GROUPS["group:openclaw"]:
            tool_rows.append(
                {
                    "id": tool_id,
                    "name": tool_id,
                    "description": "",
                    "group": "core",
                    "source": "openclaw-static",
                    "enabled": _is_allowed_by_policy(tool_id, policy),
                }
            )

    result["tools"] = tool_rows
    result["catalog_total"] = len(tool_rows)
    result["tools_enabled"] = sum(1 for row in tool_rows if row.get("enabled"))
    return result

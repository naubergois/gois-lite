"""gois-lite mode: Chat + Kanban only (no OpenClaw, QClaw, Swarm, or Monitor UI)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .config import Config

_FLAG = False

LITE_MCP_SERVERS = frozenset({"gois-cards"})

LITE_SYSTEM_PROMPT = (
    "You are the Gois Lite assistant on the dashboard. "
    "Help the user manage Kanban boards and development cards. "
    "Use gois_cards_* tools to list, create, move, and update cards; "
    "gois_kanban_ide_handoff to prepare a card and open Cursor, Kiro, or VS Code. "
    "Stay within Gois Kanban tools only. "
    "Answer in the user's language; be concise and actionable."
)

# HTML pages allowed in lite (everything else redirects to /chat).
LITE_HTML_PATHS = frozenset(
    {
        "/",
        "/chat",
        "/kanban",
        "/mcp-cards",
        "/login",
        "/auth/login",
        "/auth/register",
        "/auth/bootstrap",
    }
)

LITE_BLOCKED_HTML_PATHS = frozenset(
    {
        "/ui",
        "/saude",
        "/modelos",
        "/modelos/custos",
        "/agentes",
        "/swarm",
        "/swarm/perfis",
        "/chat/ruflo",
        "/chat/perguntas",
        "/ruflo",
        "/ruflo/motores",
        "/erros",
        "/roles",
        "/agents/novo",
        "/jobs/novo",
        "/ide",
        "/conhecimento",
        "/entidades",
        "/memoria",
        "/times",
        "/projetos",
        "/mcp",
        "/fila-prioridades",
        "/skills",
        "/allowlist",
        "/chaves",
        "/users",
        "/agenda",
        "/latex",
        "/backups",
        "/artigos/qualidade",
        "/cron/slots",
        "/cron/custos",
        "/gerenciar/apagar",
        "/calendario",
        "/metrics/ui",
        "/status/painel",
    }
)

LITE_TABS = (
    ("chat", "/chat", "Chat", "message"),
    ("kanban", "/kanban", "Kanban", "kanban"),
    ("mcp_cards", "/mcp-cards", "MCP IDE", "plug"),
)

# MCP qclaw-cards tools kept in gois-lite (kanban demandas + handoff IDE).
LITE_CARDS_MCP_TOOLS = frozenset(
    {
        "list_kanban_boards",
        "list_teams",
        "get_cards",
        "get_card_detail",
        "get_my_cards",
        "get_cards_todo",
        "create_card",
        "move_card",
        "update_card",
        "kanban_ide_handoff",
    }
)

# MCP tools exposed in lite — kanban/cards only (MCP server: gois-cards).
LITE_MCP_TOOL_EXACT = frozenset(LITE_CARDS_MCP_TOOLS)

LITE_MCP_TOOL_PREFIXES = (
    "kanban_",
)

# Chat tools for updating kanban from the Chat UI (gois_* only — no qclaw/openclaw).
LITE_CHAT_TOOL_SUBSTRINGS = ()
LITE_CHAT_TOOL_PREFIXES = (
    "gois_cards_",
    "gois_kanban_",
)
LITE_CHAT_TOOL_EXACT = frozenset()

LITE_CHAT_TOOL_BLOCK_SUBSTRINGS = (
    "qclaw",
    "openclaw",
    "skills_",
    "heygen",
    "suno",
    "seedance",
    "whatsapp",
    "shell",
    "desktop",
    "swarm",
    "ruflo",
    "latex",
    "roteiro",
)

LITE_SKIP_STARTUP_TASKS = frozenset(
    {
        "bootstrap_skills_mcp",
        "cleanup_rv_orphans",
        "repair_ruflo_memory",
        "ensure_ruflo_daemon",
        "verify_ruflo_cli",
        "warmup_embedded_roteiro_viral",
        "ensure_hermes_dashboard",
        "enforce_swarm_only_cron",
    }
)


def configure_gois_lite(cfg: Optional["Config"]) -> None:
    """Call once at startup after Config.load()."""
    global _FLAG
    enabled = False
    if cfg is not None:
        lite = getattr(cfg, "gois_lite", None)
        enabled = bool(getattr(lite, "enabled", False))
    _FLAG = enabled
    from .gois_lite_storage import configure_lite_storage

    configure_lite_storage(cfg)


def is_gois_lite() -> bool:
    raw = os.environ.get("GOIS_LITE", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if _FLAG:
        return True
    return raw in {"1", "true", "yes"}


def lite_startup_task_enabled(task: str) -> bool:
    if not is_gois_lite():
        return True
    return task not in LITE_SKIP_STARTUP_TASKS


def lite_redirect_for_html_path(path: str, *, method: str = "GET") -> Optional[str]:
    """Return redirect target when lite blocks a dashboard HTML route."""
    if method != "GET" or not is_gois_lite():
        return None
    if path in LITE_HTML_PATHS or path.startswith("/auth/"):
        return None
    if path in LITE_BLOCKED_HTML_PATHS or path.startswith("/swarm/") or path.startswith("/ruflo/"):
        return "/chat"
    return None


def _norm_tool_name(name: str) -> str:
    bare = (name or "").strip().lower()
    for prefix in ("qclaw_", "gois_"):
        if bare.startswith(prefix):
            return bare[len(prefix) :]
    return bare


def lite_resolve_chat_tool_name(name: str) -> str:
    """Map gois_* chat tool names to internal qclaw_* handlers."""
    if not is_gois_lite():
        return (name or "").strip()
    raw = (name or "").strip()
    lower = raw.lower()
    if lower.startswith("gois_cards_"):
        return "qclaw_cards_" + raw[len("gois_cards_") :]
    aliases = {
        "gois_kanban_ide_handoff": "qclaw_kanban_ide_handoff",
    }
    return aliases.get(lower, raw)


def _lite_rename_chat_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    fn = out.get("function")
    if not isinstance(fn, dict):
        return out
    fn_out = dict(fn)
    name = str(fn_out.get("name") or "")
    lower = name.lower()
    if lower.startswith("qclaw_cards_"):
        fn_out["name"] = "gois_cards_" + name[len("qclaw_cards_") :]
    elif lower == "qclaw_kanban_ide_handoff":
        fn_out["name"] = "gois_kanban_ide_handoff"
    desc = str(fn_out.get("description") or "")
    if desc:
        fn_out["description"] = (
            desc.replace("qclaw-cards", "gois-cards")
            .replace("QClaw", "Gois")
            .replace("qclaw_", "gois_")
        )
    out["function"] = fn_out
    return out


def lite_mcp_tool_allowed(name: str) -> bool:
    bare = _norm_tool_name(name)
    if bare in LITE_MCP_TOOL_EXACT:
        return True
    if "kanban" in bare:
        return True
    return bare.startswith(LITE_MCP_TOOL_PREFIXES)


def filter_lite_mcp_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if lite_mcp_tool_allowed(str(tool.get("name") or "")):
            out.append(tool)
    return out


def lite_chat_tool_allowed(name: str) -> bool:
    raw = (name or "").strip().lower()
    if any(block in raw for block in LITE_CHAT_TOOL_BLOCK_SUBSTRINGS):
        return False
    if raw in LITE_CHAT_TOOL_EXACT:
        return True
    if raw.startswith(LITE_CHAT_TOOL_PREFIXES):
        return True
    bare = _norm_tool_name(raw)
    if bare.startswith(LITE_MCP_TOOL_PREFIXES):
        return True
    if bare in LITE_MCP_TOOL_EXACT:
        return True
    return False


def filter_lite_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in tools:
        if not isinstance(spec, dict):
            continue
        fn = spec.get("function")
        name = ""
        if isinstance(fn, dict):
            name = str(fn.get("name") or "")
        if not name:
            name = str(spec.get("name") or "")
        if lite_chat_tool_allowed(name):
            out.append(_lite_rename_chat_tool_spec(spec))
    return out


def filter_lite_mcp_servers(servers: dict[str, Any]) -> dict[str, Any]:
    if not is_gois_lite():
        return servers
    return {
        k: v
        for k, v in servers.items()
        if k in LITE_MCP_SERVERS and isinstance(v, dict)
    }


def lite_cards_mcp_tool_allowed(name: str) -> bool:
    bare = _norm_tool_name(name)
    return bare in LITE_CARDS_MCP_TOOLS or lite_mcp_tool_allowed(name)


def filter_lite_cards_mcp_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_gois_lite():
        return tools
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if lite_cards_mcp_tool_allowed(str(tool.get("name") or "")):
            out.append(tool)
    return out

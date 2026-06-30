"""Cap LLM tool schemas for providers with a hard tools-array limit."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .chat_models import ResolvedChatModel

log = logging.getLogger(__name__)

# OpenAI Chat Completions rejects tools arrays longer than 128.
OPENAI_MAX_TOOLS = 128

# xAI Grok rejects tools arrays longer than 200.
XAI_MAX_TOOLS = 200

_TOOLS_LIMIT_RE = re.compile(
    r"(?:maximum|max(?:imum)?)\s+is\s+(\d+)",
    re.IGNORECASE,
)

# Always keep: skill discovery, cards/kanban MCP, jobs, core ops.
_PRIORITY_0_PREFIXES = ("qclaw_skills_", "qclaw_cards_", "qclaw_jobs_")
_PRIORITY_0_EXACT = frozenset(
    {
        "qclaw_health_check",
        "qclaw_process_status",
        "qclaw_read_log_tail",
        "qclaw_monitor_snapshot",
        "qclaw_list_teams",
        "qclaw_create_team",
        "qclaw_team_kanban",
        "qclaw_run_shell",
        "ask_qclaw_agent",
        "qclaw_monitor_update",
        "qclaw_chat_failures",
        "qclaw_team_files_search",
        "qclaw_team_files_download",
        "qclaw_create_kanban_card",
        "qclaw_kanban_list_attachments",
        "qclaw_team_context_search",
    }
)

# Drop first when over limit: niche UI / media / templates.
_PRIORITY_3_PREFIXES = (
    "qclaw_google_photos_",
    "qclaw_local_photos_",
    "qclaw_overleaf_",
    "qclaw_visual_memory_",
    "qclaw_thumbnail_style_",
    "qclaw_user_data_",
    "qclaw_identidade_",
    "qclaw_curriculum_",
    "qclaw_chat_personality_",
    "qclaw_tool_learning_",
    "qclaw_teams_calendar_",
    "qclaw_calendar_notion_",
    "qclaw_notion_",
    "qclaw_desktop_",
    "qclaw_evaluate_agents",
    "qclaw_fix_agents",
)

# Drop before P1: heavy content-generation batches.
_PRIORITY_2_PREFIXES = (
    "qclaw_virtual_band_",
    "qclaw_thumbnail_",
    "qclaw_slides_",
    "qclaw_grok_imagine_",
    "qclaw_imagen_",
    "qclaw_nano_banana_",
    "qclaw_elevenlabs_",
    "qclaw_gemini_music_",
    "qclaw_gemini_computer_use",
    "qclaw_curso_",
    "qclaw_roteiro_",
    "qclaw_modulo_portal",
    "qclaw_heygen_",
    "qclaw_suno_",
    "qclaw_seedance_",
)


def _tool_name(spec: dict[str, Any]) -> str:
    fn = spec.get("function") if isinstance(spec.get("function"), dict) else {}
    return str(fn.get("name") or spec.get("name") or "")


def tool_priority_score(name: str) -> int:
    if name in _PRIORITY_0_EXACT or name.startswith(_PRIORITY_0_PREFIXES):
        return 0
    if name.startswith(_PRIORITY_3_PREFIXES):
        return 3
    if name.startswith(_PRIORITY_2_PREFIXES):
        return 2
    return 1


def provider_default_tool_limit(resolved: ResolvedChatModel) -> Optional[int]:
    """Built-in provider defaults when nothing is stored in the database."""
    base = (resolved.entry.base_url or "").lower()
    if "api.openai.com" in base:
        return OPENAI_MAX_TOOLS
    if "api.x.ai" in base:
        return XAI_MAX_TOOLS
    return None


def model_llm_tool_limit(resolved: ResolvedChatModel) -> Optional[int]:
    """Return max tools for this model (DB override, then provider default)."""
    return resolve_model_tool_limit(resolved)


def resolve_model_tool_limit(resolved: ResolvedChatModel) -> Optional[int]:
    """Mongo-stored limit for ``model_id``, else provider default, else uncapped."""
    from .model_tool_limits_store import get_model_tool_limit

    stored = get_model_tool_limit(resolved.entry.id)
    if stored is not None:
        return stored
    return provider_default_tool_limit(resolved)


def parse_tools_limit_from_error(message: str) -> Optional[int]:
    """Extract provider max-tools from API error text (xAI, OpenAI, …)."""
    text = (message or "").strip()
    if not text:
        return None
    lower = text.lower()
    if "maximum tools" not in lower and "tools limit" not in lower:
        if "tools have been provided" not in lower or "maximum" not in lower:
            return None
    match = _TOOLS_LIMIT_RE.search(text)
    if not match:
        return None
    try:
        limit = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def is_tools_limit_error(exc: BaseException) -> bool:
    return parse_tools_limit_from_error(str(exc)) is not None


def record_learned_tool_limit(
    model_id: str,
    max_tools: int,
    *,
    base_url: str = "",
) -> None:
    from .model_tool_limits_store import set_model_tool_limit

    result = set_model_tool_limit(
        model_id,
        max_tools,
        source="learned",
        base_url=base_url,
    )
    if result.get("ok") and result.get("updated"):
        log.info(
            "learned model tool limit: %s -> %d (base_url=%s)",
            model_id,
            max_tools,
            base_url or "-",
        )


def tools_limit_failover(
    exc: BaseException,
    *,
    model_id: str,
    base_url: str,
    tools_full: list[dict[str, Any]],
    tools_current: list[dict[str, Any]],
) -> Optional[tuple[list[dict[str, Any]], list[str], int]]:
    """On tools-limit API error: persist limit and return a smaller tool list."""
    limit = parse_tools_limit_from_error(str(exc))
    if limit is None:
        return None
    record_learned_tool_limit(model_id, limit, base_url=base_url)
    capped, dropped = cap_llm_tools(tools_full, limit=limit)
    if len(capped) >= len(tools_current):
        return None
    return capped, dropped, limit


def cap_llm_tools(
    tools: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep at most ``limit`` tools, preferring operational over content-batch tools."""
    if limit <= 0 or len(tools) <= limit:
        return list(tools), []

    indexed = list(enumerate(tools))
    indexed.sort(key=lambda pair: (tool_priority_score(_tool_name(pair[1])), pair[0]))
    kept_pairs = indexed[:limit]
    kept_pairs.sort(key=lambda pair: pair[0])
    kept = [spec for _, spec in kept_pairs]
    kept_names = {_tool_name(spec) for spec in kept}
    dropped = [_tool_name(spec) for spec in tools if _tool_name(spec) not in kept_names]
    return kept, dropped


def tools_cap_system_note(dropped: list[str], *, limit: int) -> str:
    if not dropped:
        return ""
    sample = ", ".join(dropped[:6])
    more = len(dropped) - 6
    suffix = f" (+{more} mais)" if more > 0 else ""
    return (
        f"\n\n[Nota: este modelo aceita no máximo {limit} ferramentas; "
        f"{len(dropped)} ficaram de fora neste turno ({sample}{suffix}). "
        "Use `qclaw_skills_search` / `qclaw_skills_tools_for_skill` para descobrir "
        "ferramentas omitidas, ou troque para um modelo sem limite (ex.: DeepSeek) "
        "se precisar do catálogo completo.]"
    )

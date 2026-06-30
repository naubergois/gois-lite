"""Heuristic LLM routing — pick the best available model per task."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from .chat_models import (
    _model_entries,
    effective_chat_default_model_id,
    model_entry_available,
    model_supports_vision,
    resolve_llm_api_key,
)
from .config import OpenclawChatConfig

AUTO_MODEL_ID = "auto"
AUTO_MODEL_LABEL = "Auto (melhor modelo)"

_TASK_VISION = "vision"
_TASK_CODING = "coding"
_TASK_RESEARCH = "research"
_TASK_REASONING = "reasoning"
_TASK_FAST = "fast"
_TASK_GENERAL = "general"

_CODING_RE = re.compile(
    r"\b("
    r"c[oó]digo|code|implement|bug|fix|refactor|patch|diff|commit|pull request|pr\b|"
    r"pytest|unittest|typescript|javascript|python|rust|golang|java\b|sql\b|api\b|"
    r"endpoint|deploy|docker|kubernetes|ci/cd|lint|compile|debug|stack trace|"
    r"fun[cç][aã]o|classe|m[oó]dulo|script|shell|bash|git\b|merge|rebase"
    r")\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b("
    r"pesquis|research|buscar na web|web search|literatura|artigo|citar|cita[cç][aã]o|"
    r"refer[eê]ncia|bibliografia|paper|arxiv|not[ií]cia|news|fonte|evid[eê]ncia|"
    r"comparar pre[cç]os|benchmark|mercado|concorrent"
    r")\b",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(
    r"\b("
    r"arquitetura|architecture|design|estrat[eé]gia|planej|planear|roadmap|"
    r"seguran[cç]a|security|audit|an[aá]lise profunda|trade-off|decis[aã]o|"
    r"orquestr|swarm|multi-agent|sistem[aá] distribu|consenso|root cause|"
    r"investigar|diagn[oó]stico|complexo|cr[ií]tico"
    r")\b",
    re.IGNORECASE,
)
_FAST_RE = re.compile(
    r"\b("
    r"resum|traduz|translate|format|corrigir texto|revis[aã]o leve|"
    r"lista|bullet|t[oó]picos|s[ií]ntese|r[aá]pido|breve|curto"
    r")\b",
    re.IGNORECASE,
)
_ROLE_CODING_RE = re.compile(
    r"\b(coder|developer|dev|tester|engineer|programador|desenvolvedor|revisor.?c[oó]digo)\b",
    re.IGNORECASE,
)
_ROLE_RESEARCH_RE = re.compile(
    r"\b(researcher|pesquisador|analyst|analista|scout|investigador)\b",
    re.IGNORECASE,
)
_ROLE_REASONING_RE = re.compile(
    r"\b(architect|arquiteto|planner|planejador|orchestr|coordenador|lead|supervisor|security)\b",
    re.IGNORECASE,
)

_PREFERENCES: dict[str, list[str]] = {
    _TASK_VISION: [
        "claude-sonnet",
        "gpt-4o",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gpt-4.1",
    ],
    _TASK_CODING: [
        "codex-cli",
        "claude-sonnet",
        "deepseek-v4-pro",
        "gpt-4.1",
        "openai-codex",
        "gpt-5",
        "deepseek-chat",
    ],
    _TASK_RESEARCH: [
        "perplexity-sonar",
        "gemini-2.5-pro",
        "claude-sonnet",
        "gpt-4o",
        "deepseek-chat",
    ],
    _TASK_REASONING: [
        "claude-opus-4-8",
        "o3",
        "gpt-5",
        "claude-opus-4-7",
        "grok-4",
        "claude-sonnet",
    ],
    _TASK_FAST: [
        "claude-haiku-4-5",
        "gpt-4o-mini",
        "deepseek-chat",
        "gemini-2.0-flash-lite",
        "gemini-2.5-flash",
    ],
    _TASK_GENERAL: [
        "deepseek-chat",
        "gpt-4o-mini",
        "claude-sonnet",
        "gemini-2.5-flash",
    ],
}

_REASONS: dict[str, str] = {
    _TASK_VISION: "anexo visual — modelo com visão",
    _TASK_CODING: "tarefa de programação",
    _TASK_RESEARCH: "pesquisa e síntese",
    _TASK_REASONING: "raciocínio / arquitetura",
    _TASK_FAST: "tarefa curta ou operacional",
    _TASK_GENERAL: "uso geral equilibrado",
}


@dataclass(frozen=True)
class ModelRouteResult:
    model_id: str
    label: str
    task_type: str
    reason: str


def is_auto_model_id(model_id: Optional[str]) -> bool:
    mid = str(model_id or "").strip().lower()
    return mid in {AUTO_MODEL_ID, "__auto__", "auto-model", "automatic"}


def auto_model_catalog_entry() -> dict[str, Any]:
    return {
        "id": AUTO_MODEL_ID,
        "label": AUTO_MODEL_LABEL,
        "provider": "router",
        "model": AUTO_MODEL_ID,
        "api_key_env": "",
        "available": True,
        "supports_attachments": True,
        "tools_enabled": True,
        "coding": False,
        "group": "geral",
        "auto": True,
    }


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in folded if unicodedata.category(c) != "Mn").lower()


def _has_image_attachments(attachments: Optional[list[Any]]) -> bool:
    if not attachments:
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "").lower()
        name = str(item.get("name") or item.get("filename") or "").lower()
        if mime.startswith("image/"):
            return True
        if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic")):
            return True
    return False


def classify_task_type(
    *,
    message: str = "",
    attachments: Optional[list[Any]] = None,
    role: str = "",
    skills: Optional[list[str]] = None,
    title: str = "",
) -> str:
    if _has_image_attachments(attachments):
        return _TASK_VISION

    parts = [message, title, role, " ".join(skills or [])]
    blob = _norm("\n".join(p for p in parts if p))

    role_norm = _norm(role)
    if _ROLE_CODING_RE.search(role_norm):
        return _TASK_CODING
    if _ROLE_RESEARCH_RE.search(role_norm):
        return _TASK_RESEARCH
    if _ROLE_REASONING_RE.search(role_norm):
        return _TASK_REASONING

    if _CODING_RE.search(blob):
        return _TASK_CODING
    if _RESEARCH_RE.search(blob):
        return _TASK_RESEARCH
    if _REASONING_RE.search(blob):
        return _TASK_REASONING

    msg = (message or title or "").strip()
    if msg and len(msg) < 120 and _FAST_RE.search(blob):
        return _TASK_FAST
    if msg and len(msg) < 48 and not _CODING_RE.search(blob):
        return _TASK_FAST

    return _TASK_GENERAL


def _pick_available(
    chat_cfg: OpenclawChatConfig,
    preferences: list[str],
    *,
    require_tools: bool,
    require_vision: bool,
) -> Optional[tuple[str, str]]:
    entries = _model_entries(chat_cfg)
    by_id = {e.id: e for e in entries}
    for mid in preferences:
        entry = by_id.get(mid)
        if entry is None:
            continue
        if require_tools and not entry.tools_enabled:
            continue
        if require_vision and not model_supports_vision(entry):
            continue
        if not model_entry_available(entry):
            continue
        return entry.id, entry.label
    for entry in entries:
        if require_tools and not entry.tools_enabled:
            continue
        if require_vision and not model_supports_vision(entry):
            continue
        if model_entry_available(entry):
            return entry.id, entry.label
    fallback = effective_chat_default_model_id(chat_cfg.default_model_id)
    entry = by_id.get(fallback) or (entries[0] if entries else None)
    if entry is None:
        return fallback, fallback
    return entry.id, entry.label


def route_model_for_task(
    chat_cfg: OpenclawChatConfig,
    *,
    message: str = "",
    attachments: Optional[list[Any]] = None,
    role: str = "",
    skills: Optional[list[str]] = None,
    title: str = "",
    require_tools: bool = True,
    force_task_type: Optional[str] = None,
) -> ModelRouteResult:
    task_type = force_task_type or classify_task_type(
        message=message,
        attachments=attachments,
        role=role,
        skills=skills,
        title=title,
    )
    prefs = list(_PREFERENCES.get(task_type, _PREFERENCES[_TASK_GENERAL]))
    if require_tools and task_type == _TASK_RESEARCH:
        prefs = [p for p in prefs if p != "perplexity-sonar"] + ["perplexity-sonar"]

    picked = _pick_available(
        chat_cfg,
        prefs,
        require_tools=require_tools,
        require_vision=task_type == _TASK_VISION,
    )
    if picked is None:
        mid = effective_chat_default_model_id(chat_cfg.default_model_id)
        return ModelRouteResult(
            model_id=mid,
            label=mid,
            task_type=task_type,
            reason=_REASONS.get(task_type, _REASONS[_TASK_GENERAL]),
        )
    model_id, label = picked
    return ModelRouteResult(
        model_id=model_id,
        label=label,
        task_type=task_type,
        reason=_REASONS.get(task_type, _REASONS[_TASK_GENERAL]),
    )


def resolve_effective_model_id(
    chat_cfg: OpenclawChatConfig,
    model_id: Optional[str],
    *,
    message: str = "",
    attachments: Optional[list[Any]] = None,
    role: str = "",
    skills: Optional[list[str]] = None,
    title: str = "",
    require_tools: bool = True,
    force_task_type: Optional[str] = None,
) -> tuple[str, Optional[ModelRouteResult]]:
    """Return concrete model id; route when ``model_id`` is auto."""
    explicit = str(model_id or "").strip()
    if not is_auto_model_id(explicit):
        if explicit:
            return explicit, None
        return effective_chat_default_model_id(chat_cfg.default_model_id), None

    route = route_model_for_task(
        chat_cfg,
        message=message,
        attachments=attachments,
        role=role,
        skills=skills,
        title=title,
        require_tools=require_tools,
        force_task_type=force_task_type,
    )
    return route.model_id, route

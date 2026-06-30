"""Token-usage modes for dashboard chat (lean vs full prompt/tools)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from .config import OpenclawChatConfig
from .llm_tool_limits import _tool_name

log = logging.getLogger(__name__)

TOKEN_USAGE_MODES: tuple[str, ...] = ("economy", "balanced", "full", "debug")

TOKEN_USAGE_MODE_CATALOG: list[dict[str, str]] = [
    {
        "id": "economy",
        "label": "Econômico",
        "hint": "Mínimo de contexto; skills e ferramentas sob demanda (qclaw_skills_*).",
    },
    {
        "id": "balanced",
        "label": "Balanceado",
        "hint": "Padrão recomendado — contexto moderado com expansão por intenção.",
    },
    {
        "id": "full",
        "label": "Completo",
        "hint": "Catálogo amplo de skills e ferramentas (mais tokens).",
    },
    {
        "id": "debug",
        "label": "Debug",
        "hint": "Igual ao completo; regista tamanhos de prompt no log do monitor.",
    },
]

_LEAN_SYSTEM_PROMPT = (
    "You are a QClaw assistant on the gois dashboard. "
    "Discover skills with qclaw_skills_search / qclaw_skills_get / qclaw_skills_run; "
    "use native qclaw_* tools for ops, kanban (qclaw_cards_*), and shell (qclaw_run_shell). "
    "NEVER ask the user to run terminal commands — execute with qclaw_run_shell. "
    "NEVER say you will create, send, list, or run something later — call the qclaw_* tool "
    "in this turn, then summarize the result. "
    "Before long operations (batch slides/images, swarm, video, music, LaTeX), "
    "warn briefly that it may take minutes, then proceed. "
    "When using skills, start the reply with <!--skills:name-->. "
    "Answer in the user's language; be concise and actionable."
)

# Model replied with text like "Vou criar…" / "Aguarde…" instead of tool_calls.
_DEFERRED_ACTION_RE = re.compile(
    r"\b("
    r"vou|irei|vamos|i will|i'll|let me|agora vou"
    r")\s+.{0,40}?\b("
    r"criar|adicionar|enviar|executar|preparar|gerar|listar|buscar|atualizar|"
    r"sincronizar|rodar|correr|criar|add|create|send|run|generate|fetch|sync"
    r")\b",
    re.IGNORECASE,
)
_WAIT_RE = re.compile(r"\b(aguarde|wait|moment|um momento)\b", re.IGNORECASE)
_EXECUTING_STALL_RE = re.compile(r"\bexecutando agora\b", re.IGNORECASE)
_VERIFYING_STALL_RE = re.compile(
    r"\b(verificando|a procurar|a baixar)\b.{0,50}?\b("
    r"email|gmail|anexo|trabalho|ferramenta"
    r")\b",
    re.IGNORECASE,
)
_DURATION_STALL_RE = re.compile(
    r"\b(pode levar|demora|demorar)\b.{0,40}?\d",
    re.IGNORECASE,
)
_RETURN_PROMISE_RE = re.compile(r"\bvou retornar\s*:", re.IGNORECASE)
_IMMEDIATE_TOOL_INTENT_RE = re.compile(
    r"\b("
    r"email|gmail|anexo|anexos|trabalho|trabalhos|entrega|entregas|"
    r"corrigir|correção|correcao|baixar|listar|procurar|buscar|"
    r"verificar|pipeline|avaliar|turma"
    r")\b",
    re.IGNORECASE,
)
_PRESENT_WIDGET_ACTION_RE = re.compile(
    r"\b(abrindo|opening|a abrir)\b.{0,60}?\b("
    r"editor|gerador|painel|widget|imagem|image|latex|roteiro|storyboard|thumbnail"
    r")\b",
    re.IGNORECASE,
)
_WIDGET_OPEN_CLAIM_RE = re.compile(
    r"\b(editor|gerador|painel|widget).{0,40}\b("
    r"aberto|aberta|no chat|inline|interativo"
    r")\b",
    re.IGNORECASE,
)

TOOL_ACTION_NUDGE = (
    "Pare de descrever ações futuras. Execute AGORA com as ferramentas qclaw_* "
    "(qclaw_cards_*, qclaw_create_kanban_card, qclaw_run_shell, qclaw_skills_run, …). "
    "Não responda só com texto se uma ferramenta resolve o pedido."
)

MAX_TOOL_ACTION_NUDGES = 2

_LEAN_SKILLS_NOTE = (
    "## Skills e ferramentas (modo econômico)\n"
    "O catálogo completo **não** está no prompt. Fluxo: "
    "`qclaw_skills_search` → `qclaw_skills_get` → `qclaw_skills_run` ou ferramenta qclaw_* nativa. "
    "Kanban: `qclaw_cards_*`. Operações: `qclaw_health_check`, `qclaw_run_shell`, "
    "`qclaw_monitor_update`, `qclaw_jobs_health`. "
    "Modos de consumo: `qclaw_token_mode` (status/set/compare/cron_heavy/cron_repair/cron_stale).\n"
)

_BALANCED_SKILLS_NOTE = (
    "## Skills (modo balanceado)\n"
    "Skills atribuídas aparecem abaixo; para outras use `qclaw_skills_search` / `qclaw_skills_get`. "
    "Kanban: `qclaw_cards_*`. Shell: `qclaw_run_shell`. "
    "Tokens: `qclaw_token_mode` ou seletor **Tokens** no chat.\n"
)

_LAZY_TOOL_PREFIXES: tuple[str, ...] = (
    "qclaw_skills_",
    "qclaw_cards_",
    "qclaw_jobs_",
    "qclaw_mcp__",
)

_LAZY_TOOL_EXACT: frozenset[str] = frozenset(
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
        "qclaw_app_passwords_list",
        "qclaw_app_passwords_store",
        "qclaw_app_passwords_delete",
        "qclaw_chat_failures",
        "qclaw_team_files_search",
        "qclaw_team_files_download",
        "qclaw_team_files_send",
        "qclaw_create_kanban_card",
        "qclaw_kanban_list_attachments",
        "qclaw_team_context_search",
        "qclaw_show_media",
        "qclaw_save_project_note",
        "qclaw_team_contacts",
        "qclaw_token_mode",
        "qclaw_mcp_register",
        "qclaw_whatsapp_contacts_search",
        "qclaw_article_images",
    }
)

_TOOL_SLOW_ON_CONTACT_SEARCH: frozenset[str] = frozenset({
    "qclaw_whatsapp_agenda_sync",
    "qclaw_whatsapp_memoria_index",
    "qclaw_whatsapp_memoria_list",
    "qclaw_whatsapp_memoria_summary",
})

_CONTACT_SEARCH_INTENT_RE = re.compile(
    r"\b(procurar|buscar|achar|encontrar|qual).{0,30}\bcontat|"
    r"contat.{0,30}\b(whatsapp|zap|wacli)\b|"
    r"\btelefone\b.{0,20}\b(whatsapp|zap)\b",
    re.I,
)

_INTENT_TOOL_RULES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"\bslides?|pptx?|apresenta|deck\b", re.I), ("qclaw_slides_", "qclaw_show_slides")),
    (re.compile(r"\baws|ec2|finops|custo\b", re.I), ("qclaw_aws_",)),
    (
        re.compile(
            r"\b(procurar|buscar|achar|encontrar|qual).{0,30}\bcontat|"
            r"contat.{0,30}\b(whatsapp|zap|wacli)\b|"
            r"\btelefone\b.{0,20}\b(whatsapp|zap)\b",
            re.I,
        ),
        ("qclaw_whatsapp_contacts_search", "qclaw_whatsapp_agenda_list"),
    ),
    (
        re.compile(r"\bwhatsapp|wacli\b", re.I),
        ("qclaw_wacli_", "qclaw_whatsapp_", "qclaw_send_whatsapp", "qclaw_team_whatsapp"),
    ),
    (
        re.compile(r"\b(cpf|rg|nis|pis|pasep|t[ií]tulo de eleitor|identidade civil|documento pessoal)\b", re.I),
        ("qclaw_identidade_",),
    ),
    (
        re.compile(r"\b(curr[ií]culo|cv\b|resume|lattes|diploma|certificado)\b", re.I),
        ("qclaw_curriculum_", "qclaw_user_data_"),
    ),
    (
        re.compile(r"\b(endere[cç]o|cep|logradouro)\b", re.I),
        ("qclaw_identidade_", "qclaw_user_data_"),
    ),
    (
        re.compile(r"\b(agendar reuni|google meet|marcar (call|reuni)|meeting link|videoconfer)\b", re.I),
        ("qclaw_calendar_meet_",),
    ),
    (
        re.compile(
            r"\b(montar cena|plano de cenas|breakdown.{0,12}cena|dividir.{0,12}cenas?|"
            r"shot list|cen[aá]s? do (ato|roteiro))\b",
            re.I,
        ),
        ("qclaw_roteiro_scene_setup",),
    ),
    (
        re.compile(
            r"\b(google flights|buscar voo|passagem a[eé]rea|voos? de .+ para|"
            r"pre[cç]o do voo|tarifa a[eé]rea|ida e volta)\b",
            re.I,
        ),
        ("qclaw_google_flights_", "qclaw_flights_"),
    ),
    (
        re.compile(
            r"\b(metadados? (do )?youtube|t[ií]tulo do v[ií]deo|descri[cç][aã]o (do )?youtube|"
            r"tags? (do )?v[ií]deo|seo youtube|publicar no youtube|upload youtube)\b",
            re.I,
        ),
        ("qclaw_youtube_metadata_",),
    ),
    (
        re.compile(r"\bcalend[aá]rio|agenda|compromisso|reuni[aã]o\b", re.I),
        ("qclaw_calendar_",),
    ),
    (re.compile(r"\boverleaf|latex|artigo\b", re.I), ("qclaw_overleaf_", "qclaw_article_")),
    (re.compile(r"\bheygen\b", re.I), ("qclaw_heygen_",)),
    (
        re.compile(r"\bseedance|v[ií]deo (com )?ia|text-to-video|image-to-video\b", re.I),
        ("qclaw_seedance_",),
    ),
    (re.compile(r"\bsuno|música|lyria\b", re.I), ("qclaw_suno_", "qclaw_gemini_music", "qclaw_virtual_band")),
    (re.compile(r"\bthumbnail|thumb\b", re.I), ("qclaw_thumbnail_",)),
    (
        re.compile(
            r"\bgrok|imagen|nano.?banana|gerar imagem|criar imagem|editor de imagem|"
            r"ilustra[cç][aã]o|thumbnail visual\b",
            re.I,
        ),
        (
            "qclaw_chat_widget_",
            "qclaw_grok_imagine",
            "qclaw_imagen",
            "qclaw_nano_banana",
            "qclaw_openrouter_image",
        ),
    ),
    (re.compile(r"\bdesktop|screenshot|ecr[aã]\b", re.I), ("qclaw_desktop_",)),
    (
        re.compile(
            r"\b(swarm|time|enxame).{0,40}\b(vscode|vs code|cursor|kiro|antigravity|ide)\b|"
            r"\b(rodar|executar|correr).{0,40}\bswarm.{0,40}\b(vscode|vs code|cursor|kiro|ide)\b|"
            r"\bswarm.{0,40}\b(na ide|numa ide|no vscode|no cursor|no kiro)\b",
            re.I,
        ),
        (
            "qclaw_run_swarm",
            "qclaw_run_team_swarm",
            "qclaw_kanban_ide_handoff",
            "qclaw_swarm",
        ),
    ),
    (
        re.compile(r"\bswarm|ruflo\b", re.I),
        ("qclaw_swarm", "qclaw_ruflo", "qclaw_run_swarm", "qclaw_run_team_swarm"),
    ),
    (re.compile(r"\bemail|gmail\b", re.I), ("qclaw_gmail", "qclaw_email_", "qclaw_app_passwords_")),
    (
        re.compile(
            r"\b(trabalho|trabalhos|entrega|entregas|corrigir|correção|correcao|"
            r"avaliar|alunos?|turma|prova|anexo|anexos)\b",
            re.I,
        ),
        ("qclaw_gmail", "qclaw_email_", "qclaw_skills_"),
    ),
    (
        re.compile(r"\bsenha de app|app password|application password\b", re.I),
        ("qclaw_app_passwords_",),
    ),
    (re.compile(r"\bfoto|google.?photos\b", re.I), ("qclaw_google_photos_", "qclaw_local_photos_")),
    (
        re.compile(
            r"\b(?:extrair|aprend(?:a|er)|guard(?:a|ar)|cadastr(?:a|ar)|registr(?:a|ar)|"
            r"caracter[ií]sticas?|tra[çc]os?|apar[eê]ncia|rosto|fisionomia|mem[oó]ria\s+visual|"
            r"perfil\s+(?:visual|foto)|como\s+(?:eu\s+)?(?:sou|pareço))\b",
            re.I,
        ),
        ("qclaw_profile_from_photo", "qclaw_visual_memory_"),
    ),
    (
        re.compile(r"\btoken|consumo|modo econ|economizar\b", re.I),
        ("qclaw_token_mode",),
    ),
    (
        re.compile(
            r"\b(reiniciar|restart|git pull|atualizar git|atualizar monitor|monitor update|deploy local)\b",
            re.I,
        ),
        ("qclaw_monitor_update",),
    ),
    (
        re.compile(r"\b(cadastrar|registrar|instalar|adicionar).{0,40}\bmcp\b|\bmcp\b.{0,40}\b(cadastr|registr|instal|configur)\b", re.I),
        ("qclaw_mcp_register", "qclaw_mcp__"),
    ),
    (
        re.compile(
            r"\b(humaniz|humanis|deslopp|de-?ai|anti-?ai-?slop|un-?chatgpt|"
            r"tirar (?:a )?cara de ia|texto (?:com )?(?:cara|tom) de ia|"
            r"soar (?:mais )?natural|naturaliz|flu[ií]dez|legibilidade|"
            r"reescrev.{0,20}natural|rob[oó]tic)\b",
            re.I,
        ),
        (
            "qclaw_mcp__humantext__",
            "qclaw_mcp__ai-humanizer__",
            "qclaw_skills_",
        ),
    ),
    (
        re.compile(
            r"\b(detect(?:ar)? (?:texto )?ia|copyleaks|hemingway|text2go|"
            r"texto gerado por ia|parece (?:de )?chatgpt|score (?:de )?ia|"
            r"humantext|cr[eé]ditos humantext)\b",
            re.I,
        ),
        (
            "qclaw_mcp__ai-humanizer__",
            "qclaw_mcp__humantext__",
        ),
    ),
    (
        re.compile(r"\b(humanizer pro|texthumanizer|humanizer-pro)\b", re.I),
        ("qclaw_mcp__humanizer-pro__",),
    ),
]


@dataclass(frozen=True)
class ChatTokenLimits:
    mode: str
    max_skills_in_prompt: int
    max_skill_body_chars: int
    include_verbose_skill_notes: bool
    tools_selection: str
    history_limit: int
    project_memory_max_chars: int
    context_retrieval_limit: int
    max_tool_iterations: int
    max_context_chars: int
    max_tools_cap: int
    trim_tool_loop: bool
    lean_system_prompt: bool
    include_team_contacts_rule: bool
    log_prompt_sizes: bool


_MODE_PRESETS: dict[str, dict[str, Any]] = {
    "economy": {
        "max_skills_in_prompt": 12,
        "max_skill_body_chars": 0,
        "include_verbose_skill_notes": False,
        "tools_selection": "lazy",
        "history_limit": 12,
        "project_memory_max_chars": 2000,
        "context_retrieval_limit": 4,
        "max_tool_iterations": 12,
        "max_context_chars": 150_000,
        "max_tools_cap": 48,
        "trim_tool_loop": True,
        "lean_system_prompt": True,
        "include_team_contacts_rule": False,
        "log_prompt_sizes": False,
    },
    "balanced": {
        "max_skills_in_prompt": 30,
        "max_skill_body_chars": 3000,
        "include_verbose_skill_notes": False,
        "tools_selection": "intent",
        "history_limit": 20,
        "project_memory_max_chars": 4000,
        "context_retrieval_limit": 6,
        "max_tool_iterations": 20,
        "max_context_chars": 400_000,
        "max_tools_cap": 96,
        "trim_tool_loop": True,
        "lean_system_prompt": False,
        "include_team_contacts_rule": True,
        "log_prompt_sizes": False,
    },
    "full": {
        "max_skills_in_prompt": 0,
        "max_skill_body_chars": 12000,
        "include_verbose_skill_notes": True,
        "tools_selection": "full",
        "history_limit": 40,
        "project_memory_max_chars": 12000,
        "context_retrieval_limit": 8,
        "max_tool_iterations": 0,
        "max_context_chars": 0,
        "max_tools_cap": 0,
        "trim_tool_loop": True,
        "lean_system_prompt": False,
        "include_team_contacts_rule": True,
        "log_prompt_sizes": False,
    },
    "debug": {
        "max_skills_in_prompt": 0,
        "max_skill_body_chars": 12000,
        "include_verbose_skill_notes": True,
        "tools_selection": "full",
        "history_limit": 40,
        "project_memory_max_chars": 12000,
        "context_retrieval_limit": 8,
        "max_tool_iterations": 0,
        "max_context_chars": 0,
        "max_tools_cap": 0,
        "trim_tool_loop": True,
        "lean_system_prompt": False,
        "include_team_contacts_rule": True,
        "log_prompt_sizes": True,
    },
}

TOOL_ITERATIONS_MIN = 20
TOOL_ITERATIONS_MAX = 2000

TOOLS_CAP_MIN = 8
TOOLS_CAP_MAX = 256


def mode_preset_tools_cap(mode: str) -> int:
    preset = _MODE_PRESETS.get(normalize_token_usage_mode(mode), _MODE_PRESETS["balanced"])
    return int(preset.get("max_tools_cap") or 0)


def clamp_tools_cap_override(value: object, *, default: int = TOOLS_CAP_MIN) -> int:
    """Clamp user override; 0 means unlimited."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        return 0
    return max(TOOLS_CAP_MIN, min(TOOLS_CAP_MAX, n))


def resolve_effective_tools_cap(chat_cfg: OpenclawChatConfig) -> int:
    """Return max tool schemas for LLM (0 = unlimited by mode/override)."""
    override = getattr(chat_cfg, "tools_cap_override", None)
    if override is not None:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    env_raw = os.environ.get("QCLAW_CHAT_TOKEN_MODE", "").strip().lower()
    mode = normalize_token_usage_mode(env_raw or getattr(chat_cfg, "token_usage_mode", "balanced"))
    return mode_preset_tools_cap(mode)


def clamp_tool_iterations(value: object, *, default: int = TOOL_ITERATIONS_MIN) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(TOOL_ITERATIONS_MIN, min(TOOL_ITERATIONS_MAX, n))


def normalize_token_usage_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    if mode in TOKEN_USAGE_MODES:
        return mode
    return "balanced"


def resolve_token_limits(chat_cfg: OpenclawChatConfig) -> ChatTokenLimits:
    env_raw = os.environ.get("QCLAW_CHAT_TOKEN_MODE", "").strip().lower()
    mode = normalize_token_usage_mode(env_raw or getattr(chat_cfg, "token_usage_mode", "balanced"))
    preset = dict(_MODE_PRESETS[mode])
    preset.pop("max_tool_iterations", None)
    max_iters = clamp_tool_iterations(getattr(chat_cfg, "max_tool_iterations", TOOL_ITERATIONS_MIN))
    preset["max_tools_cap"] = resolve_effective_tools_cap(chat_cfg)
    return ChatTokenLimits(mode=mode, max_tool_iterations=max_iters, **preset)


_FALLBACK_MAX_CONTEXT_CHARS = 800_000


def effective_max_context_chars(
    chat_cfg: OpenclawChatConfig,
    *,
    model_max_context_chars: int = 0,
) -> int:
    """Resolve per-request context char budget (model > mode > global config)."""
    if model_max_context_chars and model_max_context_chars > 0:
        return int(model_max_context_chars)
    limits = resolve_token_limits(chat_cfg)
    if limits.max_context_chars and limits.max_context_chars > 0:
        return limits.max_context_chars
    global_cap = int(chat_cfg.max_context_chars or 0)
    if global_cap > 0:
        return global_cap
    return _FALLBACK_MAX_CONTEXT_CHARS


def count_conversation_messages(messages: list[dict[str, Any]]) -> int:
    """User + assistant rows (excludes status/tool noise)."""
    return sum(
        1
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    )


def history_nudge_fields(
    messages: list[dict[str, Any]],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    """Suggest a fresh conversation when the transcript nears the prompt window."""
    limits = resolve_token_limits(chat_cfg)
    limit = max(1, int(limits.history_limit))
    count = count_conversation_messages(messages)
    base = {
        "conversationMessageCount": count,
        "historyPromptLimit": limit,
        "historyNudge": False,
    }
    if not getattr(chat_cfg, "history_nudge_enabled", True):
        return base
    threshold = max(4, int(limit * 0.85))
    if count < threshold:
        return base
    mode_label = next(
        (m["label"] for m in TOKEN_USAGE_MODE_CATALOG if m["id"] == limits.mode),
        limits.mode,
    )
    base["historyNudge"] = True
    base["historyNudgeText"] = (
        f"Esta conversa tem {count} mensagens; o modo {mode_label} usa até {limit} no prompt. "
        "Mensagens antigas deixam de entrar no contexto — inicie uma nova conversa para respostas mais rápidas."
    )
    return base


def token_mode_status(chat_cfg: OpenclawChatConfig) -> dict[str, Any]:
    limits = resolve_token_limits(chat_cfg)
    return {
        "ok": True,
        "mode": limits.mode,
        "modes": list(TOKEN_USAGE_MODE_CATALOG),
        "limits": {
            "max_skills_in_prompt": limits.max_skills_in_prompt,
            "max_skill_body_chars": limits.max_skill_body_chars,
            "tools_selection": limits.tools_selection,
            "history_limit": limits.history_limit,
            "max_tool_iterations": limits.max_tool_iterations,
            "project_memory_max_chars": limits.project_memory_max_chars,
            "context_retrieval_limit": limits.context_retrieval_limit,
            "max_context_chars": effective_max_context_chars(chat_cfg),
            "max_tools_cap": limits.max_tools_cap or None,
            "trim_tool_loop": limits.trim_tool_loop,
        },
    }


def effective_system_prompt(chat_cfg: OpenclawChatConfig) -> str:
    from .gois_lite import LITE_SYSTEM_PROMPT, is_gois_lite

    if is_gois_lite():
        return LITE_SYSTEM_PROMPT
    limits = resolve_token_limits(chat_cfg)
    if limits.lean_system_prompt:
        return _LEAN_SYSTEM_PROMPT
    return chat_cfg.system_prompt.strip()


def build_skill_notes_prefix(
    chat_cfg: OpenclawChatConfig,
    *,
    runtime: Any,
    qclaw_tools: Any,
) -> str:
    """Return notes to prepend to the skills block (may be empty in full mode)."""
    limits = resolve_token_limits(chat_cfg)
    if not chat_cfg.qclaw_tools_enabled or qclaw_tools is None:
        return ""
    if limits.include_verbose_skill_notes:
        return _build_verbose_skill_notes(chat_cfg, runtime=runtime, qclaw_tools=qclaw_tools)
    if limits.mode == "economy":
        return _LEAN_SKILLS_NOTE
    return _BALANCED_SKILLS_NOTE


def _build_verbose_skill_notes(
    chat_cfg: OpenclawChatConfig,
    *,
    runtime: Any,
    qclaw_tools: Any,
) -> str:
    from .openclaw_chat import (
        _CHAT_ARTICLE_IMAGES_SKILLS_NOTE,
        _CHAT_AULAS_MEMORIA_SKILLS_NOTE,
        _CHAT_MEMCLAW_MEMORIA_SKILLS_NOTE,
        _CHAT_AWS_SKILLS_NOTE,
        _CHAT_CARDS_MCP_NOTE,
        _CHAT_DESKTOP_SKILLS_NOTE,
        _CHAT_FALHAS_SKILLS_NOTE,
        _CHAT_GOOGLE_FOTOS_SKILLS_NOTE,
        _CHAT_GROK_IMAGINE_SKILLS_NOTE,
        _CHAT_HEYGEN_MCP_SKILLS_NOTE,
        _CHAT_MCP_REGISTER_NOTE,
        _CHAT_LOCAL_FOTOS_SKILLS_NOTE,
        _CHAT_MEDIA_SKILLS_NOTE,
        _CHAT_MONITOR_UPDATE_NOTE,
        _CHAT_NOTION_SKILLS_NOTE,
        _CHAT_OVERLEAF_TEMPLATE_SKILLS_NOTE,
        _CHAT_ROTEIRO_LAB_SKILLS_NOTE,
        _CHAT_RV_SCREENS_NOTE,
        _CHAT_ROTEIRO_THUMBNAILS_SKILLS_NOTE,
        _CHAT_ROTEIRO_MONGO_SKILLS_NOTE,
        _CHAT_ROTEIRO_API_SKILLS_NOTE,
        _CHAT_SHELL_SKILLS_NOTE,
        _CHAT_SKILLS_MCP_NOTE,
        _CHAT_SLIDES_BATCH_IMAGES_SKILLS_NOTE,
        _CHAT_SLIDES_CORNER_DECOR_SKILLS_NOTE,
        _CHAT_SLIDES_PDF_SKILLS_NOTE,
        _CHAT_SLIDES_REPLACE_DIDACTIC_SKILLS_NOTE,
        _CHAT_SUNO_MCP_SKILLS_NOTE,
        _CHAT_SEEDANCE_MCP_SKILLS_NOTE,
        _CHAT_PHOTO_TO_VIDEO_SKILLS_NOTE,
        _CHAT_RUNWAY_MCP_SKILLS_NOTE,
        _CHAT_TEXT_HUMANIZER_MCP_NOTE,
        _CHAT_TEAM_FILES_SEARCH_NOTE,
        _CHAT_TEAM_SWARM_SKILLS_NOTE,
        _CHAT_VIRTUAL_BAND_SKILLS_NOTE,
        _CHAT_WHATSAPP_BUSCA_SKILLS_NOTE,
        _openclaw_cli_env,
        _desktop_control_enabled_for_host,
    )

    notes = [
        _CHAT_SKILLS_MCP_NOTE,
        _CHAT_ROTEIRO_LAB_SKILLS_NOTE,
        _CHAT_RV_SCREENS_NOTE,
        _CHAT_CARDS_MCP_NOTE,
        _CHAT_FALHAS_SKILLS_NOTE,
        _CHAT_AWS_SKILLS_NOTE,
        _CHAT_TEAM_FILES_SEARCH_NOTE,
        _CHAT_TEAM_SWARM_SKILLS_NOTE,
        _CHAT_AULAS_MEMORIA_SKILLS_NOTE,
        _CHAT_MEMCLAW_MEMORIA_SKILLS_NOTE,
        _CHAT_NOTION_SKILLS_NOTE,
        _CHAT_OVERLEAF_TEMPLATE_SKILLS_NOTE,
        _CHAT_MEDIA_SKILLS_NOTE,
        _CHAT_ARTICLE_IMAGES_SKILLS_NOTE,
        _CHAT_SLIDES_PDF_SKILLS_NOTE,
        _CHAT_SLIDES_CORNER_DECOR_SKILLS_NOTE,
        _CHAT_SLIDES_REPLACE_DIDACTIC_SKILLS_NOTE,
        _CHAT_SLIDES_BATCH_IMAGES_SKILLS_NOTE,
        _CHAT_GROK_IMAGINE_SKILLS_NOTE,
        _CHAT_LOCAL_FOTOS_SKILLS_NOTE,
        _CHAT_GOOGLE_FOTOS_SKILLS_NOTE,
        _CHAT_WHATSAPP_BUSCA_SKILLS_NOTE,
    ]
    notes.append(_CHAT_MONITOR_UPDATE_NOTE)
    if chat_cfg.shell_enabled:
        notes.append(_CHAT_SHELL_SKILLS_NOTE)
    if _desktop_control_enabled_for_host(chat_cfg):
        notes.append(_CHAT_DESKTOP_SKILLS_NOTE)
    if chat_cfg.heygen_mcp_enabled:
        bin_path, _env = _openclaw_cli_env(runtime, qclaw_tools.doctor_cfg)
        if bin_path:
            notes.append(_CHAT_HEYGEN_MCP_SKILLS_NOTE)
    if chat_cfg.suno_mcp_enabled:
        bin_path, _env = _openclaw_cli_env(runtime, qclaw_tools.doctor_cfg)
        if bin_path:
            notes.append(_CHAT_SUNO_MCP_SKILLS_NOTE)
    if chat_cfg.seedance_mcp_enabled:
        bin_path, _env = _openclaw_cli_env(runtime, qclaw_tools.doctor_cfg)
        if bin_path:
            notes.append(_CHAT_SEEDANCE_MCP_SKILLS_NOTE)
    if getattr(chat_cfg, "photo_to_video_enabled", True):
        notes.append(_CHAT_PHOTO_TO_VIDEO_SKILLS_NOTE)
    if getattr(chat_cfg, "runway_mcp_enabled", True):
        bin_path, _env = _openclaw_cli_env(runtime, qclaw_tools.doctor_cfg)
        if bin_path:
            notes.append(_CHAT_RUNWAY_MCP_SKILLS_NOTE)
    if chat_cfg.virtual_band_enabled:
        notes.append(_CHAT_VIRTUAL_BAND_SKILLS_NOTE)
    if chat_cfg.roteiro_thumbnails_enabled:
        notes.append(_CHAT_ROTEIRO_THUMBNAILS_SKILLS_NOTE)
    if chat_cfg.roteiro_mongo_enabled:
        notes.append(_CHAT_ROTEIRO_MONGO_SKILLS_NOTE)
    if chat_cfg.roteiro_api_enabled:
        notes.append(_CHAT_ROTEIRO_API_SKILLS_NOTE)
    if chat_cfg.external_mcp_enabled:
        notes.append(_CHAT_MCP_REGISTER_NOTE)
        notes.append(_CHAT_TEXT_HUMANIZER_MCP_NOTE)
    return "\n\n".join(n for n in notes if n)


def reply_promises_deferred_action(text: str) -> bool:
    """True when the assistant text claims a future action without tool_calls."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _DEFERRED_ACTION_RE.search(raw):
        return True
    if _EXECUTING_STALL_RE.search(raw):
        return True
    if _VERIFYING_STALL_RE.search(raw):
        return True
    if _DURATION_STALL_RE.search(raw) and _WAIT_RE.search(raw):
        return True
    if _RETURN_PROMISE_RE.search(raw) and len(raw) < 600:
        return True
    if _PRESENT_WIDGET_ACTION_RE.search(raw):
        return True
    if _WIDGET_OPEN_CLAIM_RE.search(raw):
        return True
    # "Aguarde um momento" alone is a common stall without tools.
    return bool(_WAIT_RE.search(raw) and len(raw) < 280)


def requires_immediate_tool_call(user_text: str, history_text: str = "") -> bool:
    """True when the user message expects a qclaw_* tool on the first model turn."""
    corpus = f"{user_text}\n{history_text}".strip()
    if not corpus:
        return False
    return bool(_IMMEDIATE_TOOL_INTENT_RE.search(corpus))


def select_chat_tools(
    tools_full: list[dict[str, Any]],
    *,
    chat_cfg: OpenclawChatConfig,
    user_text: str = "",
    history_text: str = "",
) -> list[dict[str, Any]]:
    limits = resolve_token_limits(chat_cfg)
    if limits.tools_selection == "full":
        return list(tools_full)

    prefixes: set[str] = set(_LAZY_TOOL_PREFIXES)
    exact = set(_LAZY_TOOL_EXACT)
    corpus = f"{user_text}\n{history_text}"
    if limits.tools_selection in ("intent", "lazy") and corpus.strip():
        for pattern, pfxs in _INTENT_TOOL_RULES:
            if pattern.search(corpus):
                prefixes.update(pfxs)

    selected: list[dict[str, Any]] = []
    contact_search = bool(_CONTACT_SEARCH_INTENT_RE.search(corpus))
    for spec in tools_full:
        name = _tool_name(spec)
        if name in exact or name in prefixes or any(name.startswith(p) for p in prefixes):
            if contact_search and name in _TOOL_SLOW_ON_CONTACT_SEARCH:
                continue
            selected.append(spec)
    return selected


def strip_stale_screenshots_from_messages(messages: list[dict[str, Any]]) -> None:
    """Keep only the latest desktop screenshot block in a tool-loop transcript."""
    indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            indices.append(i)
    for i in indices[:-1]:
        messages[i] = {
            "role": "user",
            "content": "[captura de ecrã anterior omitida para poupar tokens]",
        }


def maybe_log_prompt_sizes(
    chat_cfg: OpenclawChatConfig,
    *,
    session_key: str,
    system_chars: int,
    tools_count: int,
    tools_chars: int,
    history_count: int,
) -> None:
    limits = resolve_token_limits(chat_cfg)
    if not limits.log_prompt_sizes:
        return
    log.info(
        "chat token-mode=%s key=%s system=%d tools=%d/%d history_msgs=%d",
        limits.mode,
        session_key,
        system_chars,
        tools_count,
        tools_chars,
        history_count,
    )


def team_contacts_rule_block(chat_cfg: OpenclawChatConfig) -> str:
    limits = resolve_token_limits(chat_cfg)
    if not limits.include_team_contacts_rule:
        return ""
    return (
        "\n\n## Extração automática de contatos\n\n"
        "REGRA OBRIGATÓRIA: sempre que o utilizador mencionar dados de uma pessoa "
        "(nome, email, telefone, cargo, whatsapp, departamento) em qualquer mensagem, "
        "chama IMEDIATAMENTE a ferramenta `qclaw_team_contacts` com action=upsert "
        "para persistir o contato no time selecionado. "
        "Não perguntes confirmação — extrai e salva de forma silenciosa e automática. "
        "Se houver múltiplas pessoas na mesma mensagem, chama a ferramenta uma vez por pessoa."
    )

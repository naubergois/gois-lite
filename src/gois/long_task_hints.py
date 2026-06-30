"""Detect long-running chat operations and emit duration hints for users."""

from __future__ import annotations

import re
from typing import Any, Optional

from .openclaw_chat_tool_dispatch import _SHELL_TOOL_NAMES

# Tools that typically run minutes (sync or background).
_LONG_TOOL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("qclaw_slides_batch", "Gerar slides em lote costuma demorar alguns minutos."),
    ("qclaw_slides_narration", "Narração por slide pode demorar vários minutos."),
    ("qclaw_slides_replace_didactic", "Substituir slides didáticos pode demorar alguns minutos."),
    ("qclaw_slides_corner_decor", "Decorar slides pode demorar alguns minutos."),
    ("qclaw_grok_imagine", "Gerar imagens com Grok pode demorar 1–3 min por imagem."),
    ("qclaw_imagen", "Gerar imagens com Imagen pode demorar 1–3 min por imagem."),
    ("qclaw_nano_banana", "Gerar imagens pode demorar 1–2 min por imagem."),
    ("qclaw_openrouter_image", "Gerar imagens via OpenRouter pode demorar 1–3 min por imagem."),
    ("qclaw_virtual_band", "Banda virtual em batch pode demorar 5–15 min."),
    ("qclaw_thumbnail", "Thumbnails em lote podem demorar alguns minutos."),
    ("qclaw_heygen", "Vídeo HeyGen costuma demorar 3–10 min."),
    ("qclaw_seedance", "Vídeo Seedance costuma demorar 1–3 min."),
    ("qclaw_suno", "Música Suno costuma demorar 2–5 min."),
    ("qclaw_gemini_music", "Música Gemini Lyria costuma demorar 1–3 min."),
    ("qclaw_elevenlabs", "Narração ElevenLabs pode demorar 1–3 min."),
    ("qclaw_roteiro_book", "Gerar livro completo pode demorar 10–30 min."),
    ("qclaw_modulo_portal", "Montar módulo com mídia pode demorar vários minutos."),
    ("qclaw_curso_notebooks", "Gerar notebooks e Docker pode demorar alguns minutos."),
    ("qclaw_run_team_swarm", "Swarm do time pode demorar 2–10 min."),
    ("qclaw_run_swarm", "Execução de swarm pode demorar 2–10 min."),
    ("qclaw_create_openai_swarm", "Criar swarm pode demorar 1–3 min."),
    ("qclaw_team_article_pdf", "Compilar PDF LaTeX pode demorar 1–5 min."),
    ("qclaw_article_images", "Processar figuras do artigo pode demorar alguns minutos."),
    ("qclaw_kanban_attach_project_zip", "Compactar projeto pode demorar 1–3 min."),
    ("qclaw_kanban_attach_latex_zip", "Reunir fontes LaTeX pode demorar 1–3 min."),
    ("qclaw_monitor_update", "Atualizar git e reiniciar o monitor pode demorar ~30–60 s."),
    ("qclaw_email_memoria_index", "Indexar emails pode demorar vários minutos."),
    ("qclaw_whatsapp_memoria_index", "Indexar WhatsApp pode demorar vários minutos."),
    ("qclaw_whatsapp_agenda_sync", "Sincronizar agenda WhatsApp pode demorar 30s–3 min."),
    ("qclaw_whatsapp_messages_search", "Busca ao vivo no WhatsApp pode demorar 15–45 s."),
    ("qclaw_aulas_memoria", "Operações de memória de aulas em lote podem demorar."),
    ("qclaw_index_team_articles", "Indexar artigos do time pode demorar alguns minutos."),
)

_LONG_TOOL_EXACT: dict[str, str] = {
    "qclaw_show_slides_pdf": "Renderizar PDF de slides pode demorar 1–3 min.",
    "ask_qclaw_agent": "Consultar o agente OpenClaw pode demorar 1–3 min.",
}

# User message patterns (before any tool call).
_USER_LONG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(\d{2,})\s*(slides?|imagens?|figuras?|p[aá]ginas?)\b", re.I),
        "Pedidos com muitos itens costumam demorar vários minutos.",
    ),
    (
        re.compile(r"\b(batch|lote|em massa|todos os slides|deck completo)\b", re.I),
        "Operações em lote costumam demorar alguns minutos.",
    ),
    (
        re.compile(r"\b(swarm|enxame|multi[- ]?agente)\b", re.I),
        "Swarms e orquestração multi-agente podem demorar alguns minutos.",
    ),
    (
        re.compile(r"\b(compilar|pdf|latex|overleaf|artigo completo)\b", re.I),
        "Compilar LaTeX/PDF pode demorar 1–5 min.",
    ),
    (
        re.compile(r"\b(heygen|v[ií]deo|narra[cç][aã]o|elevenlabs)\b", re.I),
        "Geração de vídeo/narração costuma demorar alguns minutos.",
    ),
    (
        re.compile(r"\b(suno|m[uú]sica|lyria|banda virtual)\b", re.I),
        "Geração de música pode demorar 2–5 min.",
    ),
    (
        re.compile(r"\b(livro|cap[ií]tulos?|roteiro completo)\b", re.I),
        "Gerar livro ou roteiro longo pode demorar 10+ min.",
    ),
    (
        re.compile(r"\b(reiniciar|restart|monitor update|atualizar monitor)\b", re.I),
        "Reiniciar o monitor pode demorar ~30–60 s.",
    ),
)

_SLIDE_COUNT_RE = re.compile(r"\b(\d+)\s*slides?\b", re.I)
_IMAGE_COUNT_RE = re.compile(r"\b(\d+)\s*imagens?\b", re.I)


def _count_hint(args: dict[str, Any], *, unit: str, seconds_each: int) -> Optional[str]:
    for key in ("slides", "count", "num_slides", "limit", "total", "images"):
        raw = args.get(key)
        if raw in (None, ""):
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n < 4:
            return None
        low = max(1, (n * seconds_each) // 60)
        high = max(low + 1, (n * seconds_each * 2) // 60)
        return f"Gerar {n} {unit} pode demorar cerca de {low}–{high} min."
    return None


def long_task_hint_for_tool(tool_name: str, args: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return a short duration estimate for a tool about to run."""
    name = (tool_name or "").strip()
    if not name:
        return None
    targs = args if isinstance(args, dict) else {}

    if name.startswith("qclaw_slides"):
        slide_hint = _count_hint(targs, unit="slides", seconds_each=45)
        if slide_hint:
            return slide_hint
    if name.startswith(("qclaw_grok_imagine", "qclaw_imagen", "qclaw_nano_banana", "qclaw_openrouter_image")):
        img_hint = _count_hint(targs, unit="imagens", seconds_each=60)
        if img_hint:
            return img_hint

    exact = _LONG_TOOL_EXACT.get(name)
    if exact:
        return exact
    for prefix, hint in _LONG_TOOL_PREFIXES:
        if name.startswith(prefix):
            return hint
    if name in _SHELL_TOOL_NAMES:
        cmd = str(targs.get("command") or targs.get("cmd") or targs.get("script") or "")
        if len(cmd) > 200 or any(
            tok in cmd.lower()
            for tok in ("docker compose", "npm run build", "pytest", "compile", "latexmk", "make ")
        ):
            return "Comando shell longo pode demorar 1–5 min."
    return None


def long_task_hint_for_user_message(text: str) -> Optional[str]:
    """Heuristic on the user request before the model picks tools."""
    msg = (text or "").strip()
    if not msg:
        return None
    m = _SLIDE_COUNT_RE.search(msg)
    if m:
        try:
            n = int(m.group(1))
            if n >= 8:
                return f"Pedido com {n} slides — costuma demorar {max(3, n // 3)}–{max(5, n // 2)} min."
        except ValueError:
            pass
    m = _IMAGE_COUNT_RE.search(msg)
    if m:
        try:
            n = int(m.group(1))
            if n >= 6:
                return f"Pedido com {n} imagens — costuma demorar {max(2, n // 2)}–{n} min."
        except ValueError:
            pass
    for pattern, hint in _USER_LONG_PATTERNS:
        if pattern.search(msg):
            return hint
    return None


def format_long_task_warning(hint: str) -> str:
    text = (hint or "").strip()
    if not text:
        return ""
    if text.startswith("⏳"):
        return text
    return f"⏳ {text} O chat continua livre — avisamos aqui do progresso."


_LONG_TASK_SYSTEM_RULE = (
    "\n\n## Tarefas longas\n"
    "Antes de chamar ferramentas que podem demorar minutos (slides/imagens em lote, "
    "vídeo HeyGen, música Suno/Gemini, swarm, livro, LaTeX/PDF, indexação grande, "
    "reinício do monitor), avise o usuário em **uma frase curta** com estimativa "
    "(ex.: «vai levar 2–5 min», «pode demorar ~10 min»). Depois execute sem pedir "
    "confirmação extra — o chat corre em background e mostra progresso."
)


def long_task_rule_block() -> str:
    return _LONG_TASK_SYSTEM_RULE

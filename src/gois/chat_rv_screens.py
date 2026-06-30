"""Roteiro Viral UI screens as chat dialog widgets (iframe embed ?embed=1&qclaw=1)."""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

from .roteiro_viral_keys import roteiro_viral_api_base

# Screens mirrored from src/gois/roteiroviral/frontend/src/components/Layout.tsx
RV_SCREEN_CATALOG: list[dict[str, str]] = [
    # Persistent
    {"path": "/", "name": "Dashboard", "category": "Início"},
    {"path": "/meme-video", "name": "Meme Vídeo", "category": "Início"},
    {"path": "/script-act-instrumental", "name": "Geração de música", "category": "Início"},
    {"path": "/history", "name": "Histórico", "category": "Início"},
    {"path": "/search", "name": "Busca", "category": "Início"},
    # Conteúdo
    {"path": "/script", "name": "Roteiro Viral", "category": "Conteúdo"},
    {"path": "/roteiros-do-dia", "name": "Roteiros do Dia", "category": "Conteúdo"},
    {"path": "/roteiros-distopicos", "name": "Roteiros distópicos", "category": "Conteúdo"},
    {"path": "/storyboard", "name": "Storyboard", "category": "Conteúdo"},
    {"path": "/heygen-script-video", "name": "Vídeo HeyGen", "category": "Conteúdo"},
    {"path": "/instagram-storyboard", "name": "Instagram Stories", "category": "Conteúdo"},
    {"path": "/social-promo-pack", "name": "Thumbnail + texto (IG / TikTok)", "category": "Conteúdo"},
    {"path": "/image-script", "name": "Roteiro por Imagens", "category": "Conteúdo"},
    {"path": "/comic/characters", "name": "Personagens", "category": "Conteúdo"},
    {"path": "/character-battle-kling", "name": "Batalha Kling", "category": "Conteúdo"},
    {"path": "/mascot-studio", "name": "Mascot Studio", "category": "Conteúdo"},
    {"path": "/comic", "name": "Quadrinhos", "category": "Conteúdo"},
    {"path": "/image-studio", "name": "Editor de Fotos", "category": "Conteúdo"},
    {"path": "/thumbnails", "name": "Thumbnails", "category": "Conteúdo"},
    {"path": "/image-generator", "name": "Gerador de Imagens", "category": "Conteúdo"},
    {"path": "/ads-campaign-agent", "name": "Agente Google Ads", "category": "Conteúdo"},
    {"path": "/youtube-ads-factory", "name": "Fábrica YouTube Ads (roteiro)", "category": "Conteúdo"},
    {"path": "/editorial-articles", "name": "Artigos IA (linha editorial)", "category": "Conteúdo"},
    {"path": "/video-runway", "name": "Vídeo Runway", "category": "Conteúdo"},
    {"path": "/storyboard", "name": "Vídeo FAL", "category": "Conteúdo", "slug": "video-fal", "tab": "video-kling-o3"},
    {"path": "/videos-continuados", "name": "Vídeos contínuos", "category": "Conteúdo"},
    {"path": "/motion-library", "name": "Biblioteca de Movimento", "category": "Conteúdo"},
    {"path": "/sora-sequence", "name": "Sequência Sora", "category": "Conteúdo"},
    {"path": "/flow", "name": "Flow", "category": "Conteúdo"},
    {"path": "/comfy-animatediff", "name": "ComfyUI (Vídeo local)", "category": "Conteúdo"},
    {"path": "/image-to-3d", "name": "Animação 3D (parallax)", "category": "Conteúdo"},
    {"path": "/video-catalog", "name": "Catálogo de Vídeo", "category": "Conteúdo"},
    {"path": "/virtual-band", "name": "Banda virtual", "category": "Conteúdo"},
    {"path": "/music-catalog", "name": "Cadastro musical", "category": "Conteúdo"},
    # Produção avançada
    {"path": "/book", "name": "Criador de Livros", "category": "Produção"},
    {"path": "/books", "name": "Biblioteca de Livros", "category": "Produção"},
    {"path": "/course", "name": "Criador de Cursos", "category": "Produção"},
    {"path": "/courses", "name": "Biblioteca de Cursos", "category": "Produção"},
    {"path": "/slide-studio", "name": "Studio de Slides", "category": "Produção"},
    {"path": "/sites", "name": "Cadastro de Sites", "category": "Produção"},
    # Ciência
    {"path": "/research", "name": "Pesquisa Científica", "category": "Ciência"},
    {"path": "/articles", "name": "Artigos Científicos", "category": "Ciência"},
    {"path": "/article-strategies", "name": "Estratégias de Artigo", "category": "Ciência"},
    {"path": "/authors", "name": "Editar autores", "category": "Ciência"},
    {"path": "/science/journal-formats", "name": "Formatos de revista", "category": "Ciência"},
    {"path": "/research-projects", "name": "Projetos de Pesquisa", "category": "Ciência"},
    {"path": "/sota", "name": "SOTA", "category": "Ciência"},
    {"path": "/awards", "name": "Awards", "category": "Ciência"},
    # Concurso
    {"path": "/concursos", "name": "Concursos", "category": "Concurso"},
    {"path": "/concurso-study", "name": "Planejamento", "category": "Concurso"},
    {"path": "/concursos/editais", "name": "Editais", "category": "Concurso"},
    {"path": "/concursos/planos", "name": "Planos de Estudo", "category": "Concurso"},
    {"path": "/concursos/questoes", "name": "Banco de Questões", "category": "Concurso"},
    {"path": "/concursos/jogos", "name": "Jogos", "category": "Concurso"},
    {"path": "/aprovatudo", "name": "AprovaTudo", "category": "Concurso"},
    # Comunicação
    {"path": "/telegram", "name": "Telegram", "category": "Comunicação"},
    {"path": "/gmail", "name": "Gmail", "category": "Comunicação"},
    # Vida pessoal
    {"path": "/financial-agent", "name": "Agente Financeiro", "category": "Vida Pessoal"},
    {"path": "/travel", "name": "Planejador de Viagens", "category": "Vida Pessoal"},
    {"path": "/travel/api-test", "name": "Teste API Viagem", "category": "Vida Pessoal"},
    # Gestão
    {"path": "/kanban", "name": "Kanban", "category": "Gestão"},
    {"path": "/schedule", "name": "Agendamento", "category": "Gestão"},
    {"path": "/jobs", "name": "Jobs", "category": "Gestão"},
    {"path": "/scripts", "name": "Gestão de Roteiros", "category": "Gestão"},
    {"path": "/costs", "name": "Custos", "category": "Gestão"},
    {"path": "/costs/generators", "name": "Custos por Gerador", "category": "Gestão"},
    {"path": "/workers", "name": "Workers", "category": "Gestão"},
    {"path": "/agent-monitor", "name": "Monitor do Agente", "category": "Gestão"},
    {"path": "/langgraph", "name": "Fluxo LangGraph", "category": "Gestão"},
    # Laboratório
    {"path": "/script-lab", "name": "Lab de Roteiros", "category": "Laboratório"},
    {"path": "/lesson-text-lab", "name": "Lab de Texto de Aulas", "category": "Laboratório"},
    {"path": "/book-section-text-lab", "name": "Lab de Texto de Seções", "category": "Laboratório"},
    {"path": "/metadata-study", "name": "Estudo de Metadados", "category": "Laboratório"},
    {"path": "/code-studio", "name": "Studio de Código", "category": "Laboratório"},
    {"path": "/code-review", "name": "Code Review", "category": "Laboratório"},
    {"path": "/software-spec", "name": "Especificação de Software", "category": "Laboratório"},
    {"path": "/video-tester", "name": "Testador de Vídeo", "category": "Laboratório"},
    {"path": "/image-transformer", "name": "Transformador 16:9", "category": "Laboratório"},
    {"path": "/live-portrait", "name": "Live Portrait", "category": "Laboratório"},
    {"path": "/fal-kontext", "name": "FAL Kontext", "category": "Laboratório"},
    {"path": "/youtube-download", "name": "Download YouTube", "category": "Laboratório"},
    {"path": "/youtube-paraphrase", "name": "YouTube Paráfrase", "category": "Laboratório"},
    {"path": "/ideias-propostas", "name": "Ideias e propostas", "category": "Laboratório"},
    # Configuração
    {"path": "/providers", "name": "Provedores de Imagem", "category": "Configuração"},
    {"path": "/text-providers", "name": "Provedores de Texto", "category": "Configuração"},
    {"path": "/video-providers", "name": "Provedores de Vídeo", "category": "Configuração"},
    {"path": "/travel-providers", "name": "Provedores de Viagem", "category": "Configuração"},
    {"path": "/models", "name": "Teste de Modelos", "category": "Configuração"},
    {"path": "/models-manager", "name": "Gerenciador de Modelos", "category": "Configuração"},
    {"path": "/execution-modes", "name": "Modos de Execução", "category": "Configuração"},
    {"path": "/styles", "name": "Estilos de Imagem", "category": "Configuração"},
    {"path": "/prompts", "name": "Manutenção de Prompts", "category": "Configuração"},
    {"path": "/prerequisites", "name": "Pré-requisitos", "category": "Configuração"},
    {"path": "/world-template-taxonomy", "name": "Taxonomia World Template", "category": "Configuração"},
    {"path": "/mock-library", "name": "Mock Library", "category": "Configuração"},
    {"path": "/settings", "name": "Configurações", "category": "Configuração"},
    {"path": "/api-keys", "name": "API Keys", "category": "Configuração"},
    # Sistema
    {"path": "/mcp-skills", "name": "Servidor MCP", "category": "Sistema"},
    {"path": "/redis", "name": "Redis", "category": "Sistema"},
    {"path": "/logs", "name": "Logs", "category": "Sistema"},
    {"path": "/maintenance", "name": "Manutenção", "category": "Sistema"},
    {"path": "/admin/endpoints", "name": "Saúde dos endpoints", "category": "Sistema"},
    {"path": "/create", "name": "Criar Conteúdo", "category": "Início"},
    {"path": "/profile", "name": "Perfil", "category": "Sistema"},
    {"path": "/exam-grader", "name": "Corretor de Provas", "category": "Professor"},
]

# Prefer native QClaw chat widgets when available (better UX than iframe).
NATIVE_WIDGET_BY_PATH: dict[str, tuple[str, dict[str, Any]]] = {
    "/script-lab": ("qclaw_chat_widget_script_acts_editor", {}),
    "/script": ("qclaw_chat_widget_script_acts_editor", {}),
    "/image-generator": ("qclaw_chat_widget_image_generate", {}),
    "/image-studio": ("qclaw_chat_widget_image_editor", {}),
    "/storyboard": ("qclaw_chat_widget_storyboard_editor", {}),
    "/storyboard_image_editor": ("qclaw_chat_widget_storyboard_image_editor", {}),
    "/book": ("qclaw_chat_widget_book_editor", {}),
    "/books": ("qclaw_chat_widget_book_editor", {"title": "Biblioteca de Livros", "tab": "library"}),
    "/course": ("qclaw_chat_widget_course_editor", {}),
    "/courses": ("qclaw_chat_widget_course_manager", {"title": "Gerenciador de Cursos"}),
    "/research-projects": ("qclaw_chat_widget_research_project_editor", {}),
    "/ideias-propostas": ("qclaw_chat_widget_research_ideas_editor", {}),
    "/comic": ("qclaw_chat_widget_comic_editor", {}),
    "/comic/characters": ("qclaw_chat_widget_character_editor", {}),
    "/code-studio": ("qclaw_chat_widget_visual_advanced", {"tab": "code"}),
    "/thumbnails": ("qclaw_chat_widget_visual_advanced", {"tab": "thumbnail"}),
    "/word-editor": ("qclaw_chat_widget_word_editor", {}),
}

_PATH_INDEX: dict[str, dict[str, str]] = {}
_SLUG_INDEX: dict[str, str] = {}


def _build_indexes() -> None:
    if _PATH_INDEX:
        return
    for row in RV_SCREEN_CATALOG:
        path = row["path"]
        _PATH_INDEX[path] = row
        slug = (row.get("slug") or path.strip("/").replace("/", "-") or "dashboard").lower()
        _SLUG_INDEX[slug] = path
        name_key = row["name"].lower().replace(" ", "-")
        _SLUG_INDEX.setdefault(name_key, path)
    # Common aliases
    for alias, path in (
        ("dashboard", "/"),
        ("app", "/"),
        ("home", "/"),
        ("inicio", "/"),
        ("aplicacao", "/"),
        ("aplicação", "/"),
        ("lab-roteiros", "/script-lab"),
        ("lab", "/script-lab"),
        ("roteiro", "/script"),
        ("roteiro-viral", "/"),
        ("gerador-imagem", "/image-generator"),
        ("editor-fotos", "/image-studio"),
        ("storyboard", "/storyboard"),
        ("livros", "/books"),
        ("word", "/word-editor"),
        ("word-editor", "/word-editor"),
        ("curso", "/course"),
        ("cursos", "/courses"),
        ("quadrinhos", "/comic"),
        ("hq", "/comic"),
        ("comic", "/comic"),
        ("personagens", "/comic/characters"),
        ("characters", "/comic/characters"),
        ("code-studio", "/code-studio"),
        ("studio-codigo", "/code-studio"),
        ("thumbnails", "/thumbnails"),
        ("campanha-visual", "/comic"),
        ("pesquisa", "/research-projects"),
        ("projeto-pesquisa", "/research-projects"),
        ("projeto-de-pesquisa", "/research-projects"),
        ("research-projects", "/research-projects"),
        ("ideias-propostas", "/ideias-propostas"),
        ("ideias-pesquisa", "/ideias-propostas"),
        ("ideias-proposta", "/ideias-propostas"),
        ("workers", "/workers"),
        ("worker", "/workers"),
        ("monitor-workers", "/workers"),
    ):
        _SLUG_INDEX[alias] = path


_CACHED_RV_FRONTEND_BASE: str | None = None

# Safe SPA routes for availability probes (must return HTML, not FastAPI JSON handlers).
_RV_DEFAULT_PROBE_PATHS = ("/book", "/course", "/comic", "/")
# First path segment when GET is handled by api_v2 before SPA fallback (see api_v2/app.py).
_RV_API_ROUTE_HEADS = frozenset(
    {
        "api",
        "output",
        "uploads",
        "health",
        "jobs",
        "status",
        "generate",
        "tools",
        "acts",
        "config",
        "runway",
        "video",
        "diagnostics",
        "workers",
        "agents",
        "flows",
        "system",
        "options",
        "maintenance",
        "costs",
        "system_logs",
        "video-catalog",
        "api-keys",
        "storyboard",
        "job_types",
        "repair_job",
        "cancel_job",
        "retry_job",
        "jobs_list",
        "styles",
        "acts_plan",
        "sites",
        "financial-agent",
        "events",
        "roteiros-do-dia",
        "virtual-band",
        "integrations",
        "editorial-articles",
        "elsevier-checklist",
    }
)

_RV_FRONTEND_UNAVAILABLE_MSG = (
    "UI Roteiro Viral indisponível. Confirme frontend/dist em ROTEIRO_VIRAL_PATH, "
    "ou instale dependências com: uv sync --extra roteiro-viral"
)


def _rv_embed_page_ok(base: str, path: str = "/book") -> bool:
    """True when GET path returns HTML (SPA/Vite), not FastAPI JSON 404."""
    base = (base or "").strip().rstrip("/")
    if not base or base.startswith("http://roteiro-viral."):
        return False
    route = normalize_screen_path(path) or "/book"
    url = f"{base}{route}?embed=1&qclaw=1"
    try:
        import httpx

        response = httpx.get(
            url,
            timeout=0.9,
            follow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except Exception:
        return False
    if response.status_code >= 400:
        return False
    body = (response.text or "")[:600].lower()
    if '"detail"' in body and "not found" in body:
        return False
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type and "text/html" not in content_type:
        return False
    return "text/html" in content_type or "<!doctype" in body or "<html" in body


def _rv_probe_path(require_path: str = "") -> str:
    """Pick a GET route that serves SPA HTML, not a JSON API handler on the same prefix."""
    route = normalize_screen_path(require_path) if require_path else ""
    if not route or route == "/":
        return "/book"
    head = route.strip("/").split("/")[0].lower()
    if head in _RV_API_ROUTE_HEADS:
        return "/book"
    return route


def _rv_ui_base_ok(base: str, *, require_path: str = "") -> bool:
    """Probe SPA routes used by embed dialogs."""
    if require_path:
        return _rv_embed_page_ok(base, _rv_probe_path(require_path))
    return any(_rv_embed_page_ok(base, path) for path in _RV_DEFAULT_PROBE_PATHS)


def _rv_frontend_fallback(*, require_path: str = "") -> str:
    probe = _rv_probe_path(require_path) if require_path else "/comic"
    try:
        from .roteiro_viral_local.embedded_api import get_browser_base_url

        base = get_browser_base_url()
        if base and _rv_embed_page_ok(base, probe):
            return base
    except Exception:
        pass
    for raw in (
        os.environ.get("ROTEIRO_VIRAL_UI") or "",
        os.environ.get("ROTEIRO_VIRAL_FRONTEND") or "",
        os.environ.get("ROTEIRO_VIRAL_API") or "",
        roteiro_viral_api_base(),
    ):
        base = (raw or "").strip().rstrip("/")
        if base and _rv_embed_page_ok(base, probe):
            return base
    return ""


def _rv_frontend_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        base = (raw or "").strip().rstrip("/")
        if not base or base in seen:
            return
        seen.add(base)
        out.append(base)

    add(os.environ.get("ROTEIRO_VIRAL_UI") or "")
    add(os.environ.get("ROTEIRO_VIRAL_FRONTEND") or "")
    try:
        from .roteiro_viral_local.embedded_api import get_browser_base_url

        add(get_browser_base_url())
    except Exception:
        pass
    add("http://127.0.0.1:3000")
    add("http://127.0.0.1:5173")
    add("http://127.0.0.1:5174")
    add(os.environ.get("ROTEIRO_VIRAL_API") or "")
    add(roteiro_viral_api_base())
    return out


def _rv_frontend_base_fast() -> str:
    """Return configured RV UI base without network probes or subprocess startup."""
    global _CACHED_RV_FRONTEND_BASE
    if _CACHED_RV_FRONTEND_BASE:
        return _CACHED_RV_FRONTEND_BASE
    for raw in (
        os.environ.get("ROTEIRO_VIRAL_UI") or "",
        os.environ.get("ROTEIRO_VIRAL_FRONTEND") or "",
        os.environ.get("ROTEIRO_VIRAL_API") or "",
        roteiro_viral_api_base(),
    ):
        base = (raw or "").strip().rstrip("/")
        if base:
            return base
    return ""


def rv_frontend_base(*, refresh: bool = False, require_path: str = "", fast: bool = False) -> str:
    global _CACHED_RV_FRONTEND_BASE
    if fast and not require_path:
        if refresh:
            _CACHED_RV_FRONTEND_BASE = None
        return _rv_frontend_base_fast()
    probe = normalize_screen_path(require_path) if require_path else ""
    if probe:
        for candidate in _rv_frontend_candidates():
            if _rv_ui_base_ok(candidate, require_path=probe):
                return candidate
        return _rv_frontend_fallback(require_path=probe)
    if _CACHED_RV_FRONTEND_BASE and not refresh:
        return _CACHED_RV_FRONTEND_BASE
    for candidate in _rv_frontend_candidates():
        if _rv_ui_base_ok(candidate):
            _CACHED_RV_FRONTEND_BASE = candidate
            return candidate
    fallback = _rv_frontend_fallback()
    if fallback:
        _CACHED_RV_FRONTEND_BASE = fallback
    else:
        _CACHED_RV_FRONTEND_BASE = None
    return fallback


def normalize_screen_path(raw: str) -> str:
    _build_indexes()
    text = (raw or "").strip()
    if not text:
        return ""
    lower = text.lower().strip()
    if lower in _SLUG_INDEX:
        return _SLUG_INDEX[lower]
    if text.startswith("http://") or text.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(text)
        return parsed.path or "/"
    for row in RV_SCREEN_CATALOG:
        if lower == row["name"].lower():
            return row["path"]
    path = text if text.startswith("/") else f"/{text}"
    if path in _PATH_INDEX:
        return path
    # longest prefix match
    candidates = sorted(_PATH_INDEX.keys(), key=len, reverse=True)
    for candidate in candidates:
        if path == candidate or path.startswith(f"{candidate}/"):
            return path
    return path


def resolve_screen(*, path: str = "", screen: str = "", name: str = "") -> Optional[dict[str, str]]:
    _build_indexes()
    raw = path or screen or name
    normalized = normalize_screen_path(raw)
    if normalized in _PATH_INDEX:
        return dict(_PATH_INDEX[normalized])
    q = (raw or "").strip().lower()
    if not q:
        return None
    for row in RV_SCREEN_CATALOG:
        if q in row["name"].lower() or q in row["path"].lower():
            return dict(row)
    return None


def list_screens(
    *,
    query: str = "",
    category: str = "",
    limit: int = 40,
) -> list[dict[str, str]]:
    _build_indexes()
    q = (query or "").strip().lower()
    cat = (category or "").strip().lower()
    cap = max(1, min(int(limit or 40), 120))
    out: list[dict[str, str]] = []
    for row in RV_SCREEN_CATALOG:
        if cat and cat not in row["category"].lower():
            continue
        hay = f"{row['path']} {row['name']} {row['category']}".lower()
        if q and q not in hay:
            continue
        slug = row.get("slug") or row["path"].strip("/").replace("/", "-") or "dashboard"
        out.append(
            {
                "path": row["path"],
                "name": row["name"],
                "category": row["category"],
                "slug": slug,
            }
        )
        if len(out) >= cap:
            break
    return out


def list_screen_categories() -> list[str]:
    _build_indexes()
    seen: list[str] = []
    for row in RV_SCREEN_CATALOG:
        cat = row["category"]
        if cat not in seen:
            seen.append(cat)
    return seen


def apply_rv_embed_fields(
    widget: dict[str, Any],
    *,
    path: str,
    category: str = "",
    extra_query: Optional[dict[str, str]] = None,
    auto_open: Optional[bool] = None,
) -> dict[str, Any]:
    """Attach iframe embed URLs so chat opens the RV UI in the dialog."""
    _build_indexes()
    route = normalize_screen_path(path) or path or "/"
    row = _PATH_INDEX.get(route)
    cat = category or (row or {}).get("category") or ""
    frontend_base = rv_frontend_base(require_path=route)
    embed_url = build_embed_url(route, extra_query=extra_query)
    widget["path"] = route
    widget["category"] = cat
    widget["frontend_base"] = frontend_base
    if embed_url:
        widget["embed_url"] = embed_url
        widget["external_url"] = f"{frontend_base}{route}"
        if auto_open is not None:
            widget["auto_open"] = bool(auto_open)
        elif widget.get("auto_open") is None:
            widget["auto_open"] = True
    else:
        widget.pop("embed_url", None)
        widget.pop("external_url", None)
        optional_embed = auto_open is False
        if not optional_embed:
            widget["error"] = widget.get("error") or _RV_FRONTEND_UNAVAILABLE_MSG
            if widget.get("status") not in ("error", "draft", "empty", "running", "done"):
                widget["status"] = "error"
        widget["auto_open"] = False
    return widget


def build_embed_url(path: str, *, extra_query: Optional[dict[str, str]] = None) -> str:
    route = normalize_screen_path(path) or "/"
    base = rv_frontend_base(require_path=route)
    if not base:
        return ""
    params: dict[str, str] = {"embed": "1", "qclaw": "1"}
    if extra_query:
        for key, val in extra_query.items():
            if val is not None and str(val).strip():
                params[str(key)] = str(val).strip()
    qs = urlencode(params)
    return f"{base}{route}?{qs}"


def _widget_id() -> str:
    return uuid.uuid4().hex[:12]


def build_rv_screen_widget(
    *,
    path: str = "",
    screen: str = "",
    name: str = "",
    title: str = "",
    auto_open: bool = True,
    extra_query: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    row = resolve_screen(path=path, screen=screen, name=name)
    route = normalize_screen_path(path or screen or name)
    if row:
        route = row["path"]
    display = (title or (row or {}).get("name") or route or "Roteiro Viral").strip()
    frontend_base = rv_frontend_base(require_path=route) if route else rv_frontend_base()
    widget: dict[str, Any] = {
        "type": "rv_screen",
        "id": _widget_id(),
        "title": display,
        "path": route or "/",
        "category": (row or {}).get("category") or "",
        "status": "ready",
        "auto_open": bool(auto_open),
        "frontend_base": frontend_base,
    }
    if not route:
        widget["status"] = "error"
        widget["error"] = "Tela não encontrada — use qclaw_rv_screens_list para ver opções."
        return widget
    widget["embed_url"] = build_embed_url(route, extra_query=extra_query)
    if not widget["embed_url"]:
        widget["status"] = "error"
        widget["error"] = _RV_FRONTEND_UNAVAILABLE_MSG
        widget["auto_open"] = False
        return widget
    widget["external_url"] = f"{frontend_base}{route}"
    tab = (row or {}).get("tab")
    if tab and "embed_url" in widget:
        sep = "&" if "?" in widget["embed_url"] else "?"
        widget["embed_url"] = f"{widget['embed_url']}{sep}tab={tab}"
        widget["external_url"] = f"{widget['external_url']}?tab={tab}"
    return widget


def dispatch_show_rv_screen(args: dict[str, Any]) -> dict[str, Any]:
    route = normalize_screen_path(
        str(args.get("path") or args.get("screen") or args.get("name") or "")
    )
    native = None if args.get("force_iframe") else NATIVE_WIDGET_BY_PATH.get(route)
    if native:
        tool_name, defaults = native
        from .chat_creative_widgets import dispatch_show_widget

        merged = {**defaults, **{k: v for k, v in args.items() if v is not None}}
        return dispatch_show_widget(tool_name, merged)

    widget = build_rv_screen_widget(
        path=str(args.get("path") or ""),
        screen=str(args.get("screen") or ""),
        name=str(args.get("name") or ""),
        title=str(args.get("title") or ""),
        auto_open=not bool(args.get("no_auto_open")),
        extra_query=args.get("query") if isinstance(args.get("query"), dict) else None,
    )
    if widget.get("status") == "error":
        err = str(widget.get("error") or "tela não encontrada")
        return {"ok": False, "error": err, "message": err, "creative_widget": widget}
    msg = f"Editor «{widget.get('title')}» pronto — abra o diálogo no chat."
    return {"ok": True, "message": msg, "creative_widget": widget}


def dispatch_list_rv_screens(args: dict[str, Any]) -> dict[str, Any]:
    screens = list_screens(
        query=str(args.get("query") or args.get("q") or ""),
        category=str(args.get("category") or ""),
        limit=int(args.get("limit") or 40),
    )
    return {
        "ok": True,
        "count": len(screens),
        "total": len(RV_SCREEN_CATALOG),
        "categories": list_screen_categories(),
        "frontend_base": rv_frontend_base(),
        "screens": screens,
        "message": f"{len(screens)} tela(s) — use qclaw_chat_widget_rv_screen com path ou name.",
    }


def chat_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_widget_rv_screen",
                "description": (
                    "Abre no chat um editor em diálogo de uma tela do Roteiro Viral "
                    f"({len(RV_SCREEN_CATALOG)} telas: storyboard, labs, livros, cursos, vídeo, etc.). "
                    "Use path (/script-lab), slug (script-lab) ou name (Lab de Roteiros). "
                    "Telas com widget nativo no chat (ex.: /script-lab, /storyboard) abrem inline "
                    "por defeito; force_iframe=true força iframe RV (?embed=1&qclaw=1)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Rota RV, ex. /script-lab"},
                        "screen": {"type": "string", "description": "Alias de path (slug ou rota)"},
                        "name": {"type": "string", "description": "Nome da tela, ex. Storyboard"},
                        "title": {"type": "string", "description": "Título no painel do chat"},
                        "force_iframe": {
                            "type": "boolean",
                            "description": "Usar iframe RV mesmo quando há widget nativo",
                        },
                        "native_widget": {
                            "type": "boolean",
                            "description": "Usar widget nativo inline em vez do iframe RV",
                        },
                        "no_auto_open": {
                            "type": "boolean",
                            "description": "Não abrir o diálogo automaticamente",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_rv_screens_list",
                "description": (
                    "Lista telas disponíveis do Roteiro Viral para abrir via "
                    "qclaw_chat_widget_rv_screen (busca por nome, path ou categoria)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Filtrar por texto"},
                        "category": {
                            "type": "string",
                            "description": "Filtrar categoria (Conteúdo, Laboratório, …)",
                        },
                        "limit": {"type": "integer", "description": "Máximo de resultados (padrão 40)"},
                    },
                    "required": [],
                },
            },
        },
    ]

"""User-area (persona) taxonomy shared by nav, skills catalog and MCP tools."""

from __future__ import annotations

from typing import Any, Iterable

# Ordered labels — same taxonomy as dashboard sidebar groups.
USER_AREAS: tuple[str, ...] = (
    "Uso diário",
    "Operação & Infra",
    "Orquestração IA",
    "Equipes & Projetos",
    "Comunicação",
    "Conhecimento",
    "Conteúdo",
    "Plataforma & Dev",
    "Admin & FinOps",
)

_NAV_ITEM_AREAS: dict[str, str] = {
    "chat": "Uso diário",
    "kanban": "Uso diário",
    "swarm_robots": "Uso diário",
    "monitor": "Operação & Infra",
    "health": "Operação & Infra",
    "errors": "Operação & Infra",
    "active_agents": "Orquestração IA",
    "swarm_profiles": "Orquestração IA",
    "ruflo_chat": "Orquestração IA",
    "ruflo_results": "Orquestração IA",
    "ruflo_engines": "Orquestração IA",
    "agent_create": "Orquestração IA",
    "cron_create": "Orquestração IA",
    "teams": "Equipes & Projetos",
    "roles": "Equipes & Projetos",
    "projects": "Equipes & Projetos",
    "priority_queue": "Equipes & Projetos",
    "calendario": "Equipes & Projetos",
    "perguntas_chat": "Comunicação",
    "agenda": "Comunicação",
    "allowlist": "Comunicação",
    "knowledge": "Conhecimento",
    "entity_db": "Conhecimento",
    "project_memory": "Conhecimento",
    "latex": "Conteúdo",
    "backup_manager": "Conteúdo",
    "article_quality": "Conteúdo",
    "ide": "Plataforma & Dev",
    "skills": "Plataforma & Dev",
    "mcp_cards": "Plataforma & Dev",
    "mcp_servers": "Plataforma & Dev",
    "model_quotas": "Admin & FinOps",
    "model_costs": "Admin & FinOps",
    "cron_slots": "Admin & FinOps",
    "cron_costs": "Admin & FinOps",
    "env_keys": "Admin & FinOps",
    "users": "Admin & FinOps",
    "manage_delete": "Admin & FinOps",
}

# Sidebar *display* groups. The three daily workspaces (chat / kanban /
# swarm) and Monitor live in the top tab bar, so they are intentionally
# omitted here. This is presentation only — the persona taxonomy used for
# skill/MCP routing lives in ``USER_AREAS`` / ``_NAV_ITEM_AREAS`` above.
_NAV_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("operacao", "Operação", ("health", "errors")),
    (
        "orquestracao",
        "IA & Agentes",
        (
            "active_agents",
            "swarm_profiles",
            "ruflo_chat",
            "ruflo_results",
            "ruflo_engines",
            "agent_create",
            "cron_create",
        ),
    ),
    (
        "gestao",
        "Times & Projetos",
        ("teams", "roles", "projects", "priority_queue", "calendario"),
    ),
    ("comunicacao", "Comunicação", ("perguntas_chat", "agenda", "allowlist")),
    (
        "saber",
        "Conhecimento & Conteúdo",
        ("knowledge", "entity_db", "project_memory", "latex", "backup_manager", "article_quality"),
    ),
    ("plataforma", "Plataforma & Dev", ("ide", "skills", "mcp_cards", "mcp_servers")),
    (
        "admin",
        "Admin & FinOps",
        (
            "model_quotas",
            "model_costs",
            "cron_slots",
            "cron_costs",
            "env_keys",
            "users",
            "manage_delete",
        ),
    ),
)

# (area, substrings) — first match wins; keep specific rules before broad ones.
_SKILL_AREA_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Admin & FinOps", (
        "aws-manage", "api-credits", "custo-modelo", "custo-agente", "consumo-modo", "llm-quotas",
        "chat-falhas", "chat-failures", "error-log", "autoaprimorar", "ec2-monitor",
        "cron-health", "cron-cost", "chaves", "cleanup-all", "db-backup",
    )),
    ("Operação & Infra", (
        "monitor-improver", "monitor-update", "chat-monitor", "chat-errors",
        "chat-jobs", "jobs-health", "jobs-manage", "hermes-cron", "wacli-saude",
        "wacli-auth", "wacli-unlock", "wacli-group", "wacli-groups",
    )),
    ("Orquestração IA", (
        "swarm-manage", "swarm-topology", "team-swarm", "ruflo-swarm", "ruflo-cron",
        "agent-evaluate", "agent-fix", "agent-langchain", "agent-langgraph",
        "agent-pydantic", "claude-flow", "hermes-agent", "create-hermes",
    )),
    ("Equipes & Projetos", (
        "team-files", "team-swarm", "team-context", "team-articles", "team-whatsapp",
        "kanban", "requisitos", "card-release", "teams-calendar", "teams-hygiene",
        "git-baseline", "priority-queue", "projetos",
        "estrategia-gartner", "estrategia-corporativa", "estrategia-organizacional",
        "operating-model", "business-capability",
    )),
    ("Uso diário", (
        "viagem-", "viagem ", "travel-plan", "roteiro-viagem", "planejamento viagem",
        "orcamento-domestico", "orcamento", "financas-pessoais",
    )),
    ("Comunicação", (
        "whatsapp", "wacli", "gmail", "send_whatsapp", "allowlist", "perguntas",
        "email-time", "email-pessoas",
    )),
    ("Conhecimento", (
        "memoria", "memory", "memclaw", "email-memoria", "aulas-memoria", "ai-engineer",
        "ai-kb", "notion", "calendar", "google-calendar", "team-context-search",
        "personalidade", "memoria-visual", "dados-pessoais", "chat-dados-pessoais", "conhecimento",
    )),
    ("Conteúdo", (
        "slides", "roteiro", "thumbnail", "imagen", "grok-imagine", "heygen",
        "suno", "elevenlabs", "nano-banana", "image-models", "curso", "livro",
        "modulo-portal", "hotmart", "overleaf", "latex", "article", "midia",
        "virtual-band", "pdf-preview", "pdf-fill", "browser-canva", "browser-heygen",
        "browser-youtube", "browser-udemy", "course-mcp",
    )),
    ("Plataforma & Dev", (
        "run-shell", "macos-desktop", "mcp-search", "github", "kiro-dev",
        "skills_list", "skills_get", "skills_run", "skills_search",
        "skills_tools", "skills_reference", "claude-flow-mcp", "langchain",
    )),
)

_MCP_TOOL_AREAS: dict[str, str] = {
    # qclaw-skills — discovery
    "skills_list": "Plataforma & Dev",
    "skills_get": "Plataforma & Dev",
    "skills_search": "Plataforma & Dev",
    "skills_run": "Plataforma & Dev",
    "skills_tools_for_skill": "Plataforma & Dev",
    "skills_reference_get": "Plataforma & Dev",
    # Operação & Infra
    "monitor_update": "Operação & Infra",
    "calendar_meet_create": "Comunicação",
    "google_flights_search": "Uso diário",
    "flights_search": "Uso diário",
    "flights_providers_status": "Uso diário",
    "travel_stack_status": "Uso diário",
    "travel_weather_forecast": "Uso diário",
    "travel_currency_convert": "Uso diário",
    "travel_places_search": "Uso diário",
    "travel_hotels_search": "Uso diário",
    "travel_itinerary_build": "Uso diário",
    "travel_stack_plan": "Uso diário",
    "social_stack_status": "Conteúdo",
    "social_stack_list_layers": "Conteúdo",
    "social_stack_key_status": "Conteúdo",
    "social_stack_plan_pipeline": "Conteúdo",
    "social_post_brief": "Conteúdo",
    "social_calendar_build": "Conteúdo",
    "social_stack_plan": "Conteúdo",
    "software_planning_list_stages": "Equipes & Projetos",
    "software_planning_status": "Equipes & Projetos",
    "software_planning_key_status": "Equipes & Projetos",
    "software_planning_plan_pipeline": "Equipes & Projetos",
    "software_planning_prd_brief": "Equipes & Projetos",
    "software_planning_backlog_brief": "Equipes & Projetos",
    "software_planning_architecture_brief": "Equipes & Projetos",
    "software_planning_traceability_matrix": "Equipes & Projetos",
    "software_planning_plan": "Equipes & Projetos",
    "roteiro_scene_setup": "Conteúdo",
    "youtube_metadata_generate": "Conteúdo",
    "kanban_ide_handoff": "Uso diário",
    "chat_failures": "Admin & FinOps",
    "aws_overview": "Admin & FinOps",
    "aws_cost": "Admin & FinOps",
    "aws_ce_get_cost_and_usage": "Admin & FinOps",
    "aws_machines": "Admin & FinOps",
    "aws_waste": "Admin & FinOps",
    "aws_env": "Admin & FinOps",
    "jobs_health": "Operação & Infra",
    "jobs_list_running": "Operação & Infra",
    "jobs_get_running": "Operação & Infra",
    "jobs_cancel": "Operação & Infra",
    "jobs_cancel_all_batches": "Operação & Infra",
    "jobs_cron_list": "Orquestração IA",
    "jobs_cron_get": "Orquestração IA",
    "jobs_cron_action": "Orquestração IA",
    "jobs_cron_create": "Orquestração IA",
    "jobs_cron_edit": "Orquestração IA",
    # Uso diário / equipes
    "team_files_search": "Equipes & Projetos",
    "team_files_download": "Equipes & Projetos",
    "team_files_send": "Equipes & Projetos",
    "team_rule_store": "Equipes & Projetos",
    "team_rule_list": "Equipes & Projetos",
    "team_rule_search": "Equipes & Projetos",
    "team_rule_delete": "Equipes & Projetos",
    "team_rules_summary": "Equipes & Projetos",
    "team_fact_store": "Equipes & Projetos",
    "team_fact_import": "Equipes & Projetos",
    "team_fact_list": "Equipes & Projetos",
    "team_fact_search": "Equipes & Projetos",
    "team_fact_delete": "Equipes & Projetos",
    "team_facts_summary": "Equipes & Projetos",
    "team_facts_for_roteiro": "Conteúdo",
    "email_team_pdf_list": "Comunicação",
    "email_team_pdf_save": "Equipes & Projetos",
    "gmail_attachments_list": "Comunicação",
    "gmail_attachments_download": "Comunicação",
    "trello_connect": "Equipes & Projetos",
    "trello_boards": "Equipes & Projetos",
    "trello_board_detail": "Equipes & Projetos",
    "trello_card_create": "Equipes & Projetos",
    "trello_card_move": "Equipes & Projetos",
    "trello_kanban_sync": "Equipes & Projetos",
    "legal_pdf_extract": "Conhecimento",
    "legal_normas_list": "Equipes & Projetos",
    "legal_norma_extract": "Conhecimento",
    "legal_evaluation_save": "Conhecimento",
    "legal_evaluation_get": "Conhecimento",
    "legal_evaluation_list": "Conhecimento",
    "legal_evaluation_search": "Conhecimento",
    "budget_get": "Uso diário",
    "budget_save": "Uso diário",
    "budget_summary": "Uso diário",
    "budget_analyze_csv": "Uso diário",
    "team_payment_save": "Equipes & Projetos",
    "team_payment_get": "Equipes & Projetos",
    "team_payment_list": "Equipes & Projetos",
    "team_payment_search": "Equipes & Projetos",
    "team_payment_delete": "Equipes & Projetos",
    "team_payments_summary": "Equipes & Projetos",
    "list_kanban_boards": "Uso diário",
    "get_cards": "Uso diário",
    "get_card_detail": "Uso diário",
    "get_my_cards": "Uso diário",
    "get_cards_todo": "Uso diário",
    "list_teams": "Equipes & Projetos",
    "team_swarm_status": "Orquestração IA",
    "run_team_swarm": "Orquestração IA",
    "move_card": "Uso diário",
    "get_errors": "Operação & Infra",
    "errors_to_cards": "Operação & Infra",
    # Conteúdo
    "grok_imagine_generate": "Conteúdo",
    "imagen_generate": "Conteúdo",
    "slides_batch_images": "Conteúdo",
    "slides_batch_artifacts": "Conteúdo",
    "slides_narration": "Conteúdo",
    "slides_corner_decor": "Conteúdo",
    "slides_replace_didactic": "Conteúdo",
    "elevenlabs_narrate": "Conteúdo",
    "gemini_music_generate": "Conteúdo",
    "gemini_computer_use": "Plataforma & Dev",
    "curso_notebooks_docker": "Conteúdo",
    "roteiro_book_generate": "Conteúdo",
    "roteiro_book_sync": "Conteúdo",
    "roteiro_course_sync": "Conteúdo",
    "roteiro_book_latex": "Conteúdo",
    "epub_editor": "Conteúdo",
    "chapter_image_epub": "Conteúdo",
    "modulo_portal": "Conteúdo",
    "thumbnail_prompt": "Conteúdo",
    "thumbnail_generate": "Conteúdo",
    "thumbnail_concepts": "Conteúdo",
    "thumbnail_job_status": "Conteúdo",
    "virtual_band_create": "Conteúdo",
    "virtual_band_portrait": "Conteúdo",
    "virtual_band_job_status": "Conteúdo",
    "virtual_band_list_bands": "Conteúdo",
    "virtual_band_suggest_members": "Conteúdo",
    "rv_mongo_collections": "Conhecimento",
    "rv_mongo_find": "Conhecimento",
    "rv_mongo_count": "Conhecimento",
    "memclaw_recall": "Conhecimento",
    "memclaw_list": "Conhecimento",
    "memclaw_stats": "Conhecimento",
    "memclaw_keystones": "Conhecimento",
    "memclaw_write": "Conhecimento",
    "memclaw_manage": "Conhecimento",
    "memclaw_evolve": "Conhecimento",
    "memclaw_doc": "Conhecimento",
    "article_references_verify": "Conhecimento",
    "crossref_get_doi": "Conhecimento",
    "crossref_search_title": "Conhecimento",
    "crossref_search_author": "Conhecimento",
    "orcid_get_profile": "Conhecimento",
    "orcid_search_researchers": "Conhecimento",
    "orcid_resolve_researcher": "Conhecimento",
    "orcid_get_works": "Conhecimento",
    "orcid_get_work_detail": "Conhecimento",
    "orcid_get_affiliations": "Conhecimento",
    "literature_search": "Conhecimento",
    "research_start": "Conhecimento",
    "humanize_text": "Conteúdo",
    "detect_ai_text": "Conteúdo",
    "humanizer_providers_status": "Plataforma & Dev",
    "paper_orchestra_status": "Conteúdo",
    "paper_orchestra_get": "Conteúdo",
    "paper_orchestra_init": "Conteúdo",
    "paper_orchestra_validate": "Conteúdo",
    "paper_orchestra_build_pdf": "Conteúdo",
    "paper_orchestra_run_script": "Conteúdo",
    "rv_api_health": "Conteúdo",
    "rv_api_get": "Conteúdo",
    "rv_api_post": "Conteúdo",
    "rv_api_patch": "Conteúdo",
    "rv_api_delete": "Conteúdo",
    "rv_job_status": "Conteúdo",
    "rv_jobs_list": "Conteúdo",
    "roteiro_book_cover_portrait": "Conteúdo",
    "book_cover_studio_brief": "Conteúdo",
    "book_cover_studio_concepts": "Conteúdo",
    "book_cover_studio_review": "Conteúdo",
    "book_cover_studio_export": "Conteúdo",
    "book_cover_studio_pipeline": "Conteúdo",
    "list_article_workspaces": "Conteúdo",
    "list_articles": "Conteúdo",
    "read_article": "Conteúdo",
    "search_articles": "Conteúdo",
    "write_article": "Conteúdo",
    "compile_article": "Conteúdo",
    "edit_article_tex": "Conteúdo",
    "qclaw_team_article_pdf": "Conteúdo",
    # Swarm
    "swarm_list": "Orquestração IA",
    "swarm_get": "Orquestração IA",
    "swarm_create": "Orquestração IA",
    "swarm_update": "Orquestração IA",
    "swarm_delete": "Orquestração IA",
    "swarm_health": "Orquestração IA",
    "swarm_topology": "Orquestração IA",
    "swarm_design": "Orquestração IA",
}


def nav_item_area(key: str) -> str:
    return _NAV_ITEM_AREAS.get(key, "Plataforma & Dev")


def _norm_key(*parts: str) -> str:
    return " ".join(str(p or "").strip().lower() for p in parts if p)


def classify_skill(*, name: str = "", slug: str = "", description: str = "") -> str:
    """Infer user area from skill slug/name/description."""
    key = _norm_key(slug, name, description)
    if not key:
        return "Plataforma & Dev"
    for area, needles in _SKILL_AREA_RULES:
        if any(needle in key for needle in needles):
            return area
    if key.startswith("qclaw-chat-"):
        return "Uso diário"
    return "Plataforma & Dev"


def classify_mcp_tool(name: str) -> str:
    """Infer user area for an MCP tool name (with or without qclaw_ prefix)."""
    raw = str(name or "").strip()
    if not raw:
        return "Plataforma & Dev"
    if raw in _MCP_TOOL_AREAS:
        return _MCP_TOOL_AREAS[raw]
    bare = raw.removeprefix("qclaw_")
    if bare in _MCP_TOOL_AREAS:
        return _MCP_TOOL_AREAS[bare]
    return classify_skill(slug=bare.replace("_", "-"), name=bare)


def area_matches_filter(area: str, area_filter: str) -> bool:
    filt = str(area_filter or "").strip()
    if not filt or filt.lower() in {"all", "todas", ""}:
        return True
    return area.lower() == filt.lower() or area.replace(" & ", " ").lower() == filt.lower()


def prefix_description_with_area(description: str, area: str) -> str:
    """Prefix MCP tool description with area tag for agent routing."""
    desc = (description or "").strip()
    tag = f"[{area}]"
    if desc.startswith("["):
        return desc
    return f"{tag} {desc}".strip()


def annotate_mcp_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Return tool dict with user_area and area-prefixed description."""
    name = str(tool.get("name") or "")
    area = classify_mcp_tool(name)
    out = dict(tool)
    out["user_area"] = area
    out["description"] = prefix_description_with_area(str(tool.get("description") or ""), area)
    return out


def annotate_mcp_tools(tools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [annotate_mcp_tool(t) for t in tools if isinstance(t, dict)]


def summarize_tool_with_area(tool: dict[str, Any]) -> dict[str, Any]:
    """Compact tool row for HTTP catalog UI."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    name = str(tool.get("name") or "")
    area = str(tool.get("user_area") or classify_mcp_tool(name))
    return {
        "name": name,
        "description": (tool.get("description") or "").strip(),
        "user_area": area,
        "params": list(props.keys()),
        "required": list(required),
    }

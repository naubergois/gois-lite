"""Ready-made team templates and shared Kanban column definitions."""

from __future__ import annotations

from typing import Any

# Shared across per-team kanban (accounts) and Hermes project kanban.
DEFAULT_KANBAN_COLUMNS: list[dict[str, str]] = [
    {"id": "backlog", "title": "Backlog"},
    {"id": "todo", "title": "A fazer"},
    {"id": "doing", "title": "Em progresso"},
    {"id": "testes-usabilidade", "title": "Testes e usabilidade"},
    {"id": "review", "title": "Em revisão"},
    {"id": "done", "title": "Concluído"},
]

# Default board layout (Trello-style lists).
TRELLO_KANBAN_COLUMNS: list[dict[str, str]] = [
    {"id": "todo", "title": "A fazer"},
    {"id": "doing", "title": "Em progresso"},
    {"id": "testes-usabilidade", "title": "Testes e usabilidade"},
    {"id": "done", "title": "Concluído"},
]

DEFAULT_PROJECT_TEAM_ID = "projeto-padrao"
DEFAULT_PROJECT_TEAM_NAME = "Projeto padrão"

SCHEDULING_TEAM_ID = "agendamentos"
SCHEDULING_TEAM_NAME = "Agendamentos"
SCHEDULING_TEAM_DESCRIPTION = (
    "Time de agendamentos — cards gerados automaticamente para jobs cron "
    "que não possuem card em nenhum outro time."
)

SCHEDULING_KANBAN_COLUMNS: list[dict[str, str]] = [
    {"id": "agendado", "title": "Agendado"},
    {"id": "doing", "title": "Em execução"},
    {"id": "done", "title": "Concluído"},
    {"id": "error", "title": "Com erro"},
]


def is_default_project_team_id(team_id: str) -> bool:
    tid = str(team_id or "").strip()
    return tid == DEFAULT_PROJECT_TEAM_ID or tid.startswith(f"{DEFAULT_PROJECT_TEAM_ID}-")

# Each preset: suggested Hermes profile slugs (match TEAM_ROLE_PRESETS ids when possible).
TEAM_PRESETS: list[dict[str, Any]] = [
    {
        "id": "squad-produto",
        "name": "Squad Produto",
        "description": (
            "Discovery, priorização e entrega de valor — product, design e liderança técnica."
        ),
        "role_ids": ["product-manager", "ux-designer", "tech-lead"],
        "profile_slugs": ["product-manager", "ux-designer", "tech-lead"],
    },
    {
        "id": "squad-backend",
        "name": "Squad Backend",
        "description": "APIs, dados persistentes, testes e operação do serviço backend.",
        "role_ids": ["backend-dev", "qa-engineer", "devops"],
        "profile_slugs": ["backend-dev", "qa-engineer", "devops"],
    },
    {
        "id": "squad-frontend",
        "name": "Squad Frontend",
        "description": "Interface web, design system, acessibilidade e integração com APIs.",
        "role_ids": ["frontend-dev", "ux-designer", "qa-engineer"],
        "profile_slugs": ["frontend-dev", "ux-designer", "qa-engineer"],
    },
    {
        "id": "squad-fullstack",
        "name": "Squad Full-stack",
        "description": "Entrega ponta a ponta: backend, frontend, CI e deploy.",
        "role_ids": ["fullstack-dev", "tech-lead", "qa-engineer"],
        "profile_slugs": ["fullstack-dev", "tech-lead", "qa-engineer"],
    },
    {
        "id": "squad-mobile",
        "name": "Squad Mobile",
        "description": "Apps nativos ou híbridos, UX mobile e publicação nas lojas.",
        "role_ids": ["mobile-dev", "ux-designer", "qa-engineer"],
        "profile_slugs": ["mobile-dev", "ux-designer", "qa-engineer"],
    },
    {
        "id": "squad-dados",
        "name": "Squad Dados",
        "description": "Pipelines, modelagem analítica e qualidade de dados.",
        "role_ids": ["data-engineer", "backend-dev", "devops"],
        "profile_slugs": ["data-engineer", "backend-dev", "devops"],
    },
    {
        "id": "squad-plataforma",
        "name": "Squad Plataforma",
        "description": "Infraestrutura, observabilidade, segurança e confiabilidade.",
        "role_ids": ["devops", "security", "tech-lead"],
        "profile_slugs": ["devops", "security", "tech-lead"],
    },
    {
        "id": "squad-suporte",
        "name": "Squad Suporte",
        "description": "Triagem, diagnóstico e escalonamento para engenharia.",
        "role_ids": ["support", "devops", "backend-dev"],
        "profile_slugs": ["support", "devops", "backend-dev"],
    },
    {
        "id": "squad-curso-completo",
        "name": "Squad Curso Online",
        "description": (
            "Pipeline de curso: design instrucional, redação, slides, vídeo HeyGen e publicação."
        ),
        "role_ids": [
            "product-manager",
            "instructional-designer",
            "technical-writer",
            "ux-designer",
            "video-producer",
            "course-publisher",
            "qa-engineer",
        ],
        "profile_slugs": [
            "product-manager",
            "instructional-designer",
            "technical-writer",
            "ux-designer",
            "video-producer",
            "course-publisher",
            "qa-engineer",
        ],
    },
    {
        "id": "squad-infoproduto",
        "name": "Squad Infoproduto",
        "description": (
            "Ebook ou guia digital: pesquisa, redação, design de capa e revisão editorial."
        ),
        "role_ids": [
            "product-manager",
            "ai-engineer",
            "technical-writer",
            "ux-designer",
            "qa-engineer",
        ],
        "profile_slugs": [
            "product-manager",
            "ai-engineer",
            "technical-writer",
            "ux-designer",
            "qa-engineer",
        ],
    },
    {
        "id": "squad-lancamento",
        "name": "Squad Lançamento",
        "description": (
            "Lançamento de infoproduto: copy de vendas, design de oferta e publicação."
        ),
        "role_ids": [
            "product-manager",
            "sales-copywriter",
            "ux-designer",
            "course-publisher",
        ],
        "profile_slugs": [
            "product-manager",
            "sales-copywriter",
            "ux-designer",
            "course-publisher",
        ],
    },
]

_TEAM_PRESET_BY_ID = {str(p["id"]): p for p in TEAM_PRESETS}


def get_team_preset(preset_id: str) -> dict[str, Any]:
    key = str(preset_id or "").strip()
    preset = _TEAM_PRESET_BY_ID.get(key)
    if not preset:
        raise ValueError(f"preset de time desconhecido: {key!r}")
    return preset


def default_project_starter_tasks() -> list[dict[str, Any]]:
    """Seed cards for the built-in default project board."""
    return [
        {
            "id": "TASK-001",
            "title": "Explorar o quadro Kanban",
            "column": "todo",
            "priority": 2,
            "assignees": [],
            "description": "Arraste cartões entre listas ou use os botões de mover.",
        },
        {
            "id": "TASK-002",
            "title": "Criar agentes em Papéis",
            "column": "todo",
            "priority": 3,
            "assignees": [],
            "description": "Defina perfis Hermes e associe ao time em Times.",
        },
        {
            "id": "TASK-003",
            "title": "Primeira entrega",
            "column": "doing",
            "priority": 1,
            "assignees": [],
            "description": "Mova para Concluído quando terminar (comentário de resultado).",
        },
    ]


def starter_kanban_tasks(preset_id: str) -> list[dict[str, Any]]:
    """Optional seed tasks when creating a team from a preset."""
    preset = get_team_preset(preset_id)
    name = str(preset.get("name") or preset_id)
    slugs = [str(s).strip() for s in (preset.get("profile_slugs") or []) if str(s).strip()]
    if not slugs:
        slugs = [""]

    def _assignee_for(index: int) -> list[str]:
        slug = slugs[index % len(slugs)]
        return [slug] if slug else []

    return [
        {
            "id": "TASK-001",
            "title": f"Alinhar backlog do {name}",
            "column": "backlog",
            "priority": 1,
            "assignees": _assignee_for(0),
            "description": "Revisar prioridades, critérios de aceite e dependências entre papéis.",
        },
        {
            "id": "TASK-002",
            "title": "Configurar ambiente e repositório",
            "column": "todo",
            "priority": 2,
            "assignees": _assignee_for(1),
            "description": "Clonar repo ou apontar pasta local; validar build e testes iniciais.",
        },
    ]

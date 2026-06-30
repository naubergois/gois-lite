"""Create Hermes profiles and scheduled dev jobs from natural language."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import httpx

from pathlib import Path

from .config import AgentConfig, HermesAgentCreateConfig
from .hermes_cron import create_hermes_cron_job
from .hermes_skills import (
    DEFAULT_DEV_SKILL_SLUGS,
    format_skills_for_prompt,
    format_skills_for_user,
    list_development_skills,
    normalize_skill_names,
)
from .role_presets_catalog import EXTENDED_ROLE_PRESETS

log = logging.getLogger(__name__)

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_PARSE_SYSTEM_BASE = """\
You turn a user's natural-language request into a Hermes **development agent** spec.
Return ONLY a JSON object (no markdown fences) with exactly these keys:
- "name": lowercase slug, 2-32 chars, [a-z0-9][a-z0-9_-]* (no spaces)
- "description": one or two sentences describing what this agent programs
- "soul": markdown for SOUL.md — role, stack, constraints, tone (same language as user)
- "requirement_prompt": detailed cron prompt the agent runs on each schedule — concrete
  steps to implement the user's requirement (repos, tests, commits, deliverables)
- "schedule": Hermes schedule string (e.g. "every 24h", "every 90m", "0 9 * * *") or null
- "skills": array of skill slugs from the AVAILABLE SKILLS list below (pick 3-8 relevant)
- "workdir": absolute project path if the user named one, else null

Pick a short slug (e.g. "api-billing-dev", "whatsapp-coder").
Prefer software-development skills: plan, writing-plans, test-driven-development,
systematic-debugging, requesting-code-review, subagent-driven-development.
Write soul and requirement_prompt in the user's language when possible.
"""

_PARSE_SYSTEM_ROLE = """\
You turn a user's natural-language request into a Hermes **team role / professional profile** spec.
The role can be anyone on a software team or any other profession (developer, QA, designer,
product manager, DevOps, data engineer, tech lead, support, etc.).
Return ONLY a JSON object (no markdown fences) with exactly these keys:
- "name": lowercase slug, 2-32 chars, [a-z0-9][a-z0-9_-]* (no spaces)
- "description": one or two sentences describing this role's responsibilities
- "soul": markdown for SOUL.md — expertise, scope, deliverables, collaboration style,
  constraints, tone (same language as the user)

Pick a short slug derived from the role (e.g. "backend-dev", "qa-engineer", "ux-designer").
Write soul in the user's language when possible. Focus on what this professional does,
not on cron schedules or automated jobs.
"""

# Papéis clássicos de time de software (catálogo estendido em role_presets_catalog).
_SOFTWARE_TEAM_ROLE_PRESETS: list[dict[str, str]] = [
    {
        "id": "backend-dev",
        "label": "Desenvolvedor backend",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de desenvolvedor backend sênior: APIs REST, Python/FastAPI, "
            "PostgreSQL, testes automatizados, revisão de código e documentação técnica."
        ),
    },
    {
        "id": "frontend-dev",
        "label": "Desenvolvedor frontend",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de desenvolvedor frontend: React/TypeScript, acessibilidade, "
            "design system, testes de componente e integração com APIs."
        ),
    },
    {
        "id": "fullstack-dev",
        "label": "Desenvolvedor full-stack",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de desenvolvedor full-stack: backend + frontend, entrega end-to-end, "
            "CI/CD básico, testes e deploy seguro."
        ),
    },
    {
        "id": "mobile-dev",
        "label": "Desenvolvedor mobile",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de desenvolvedor mobile: apps nativos ou React Native, "
            "performance, UX mobile e publicação nas lojas."
        ),
    },
    {
        "id": "qa-engineer",
        "label": "QA / testes",
        "category": "qualidade",
        "prompt": (
            "Crie um papel de engenheiro de QA: planos de teste, automação, regressão, "
            "critérios de aceite e reporte de bugs com reprodução clara."
        ),
    },
    {
        "id": "devops",
        "label": "DevOps / SRE",
        "category": "operações",
        "prompt": (
            "Crie um papel de DevOps/SRE: infraestrutura, observabilidade, deploy, "
            "incident response e automação de pipelines."
        ),
    },
    {
        "id": "hermes-recovery",
        "label": "Agente de recuperação Hermes",
        "category": "operacoes-ti",
        "prompt": (
            "operador SRE do Hermes: gateway (`hermes gateway`), dashboard web, "
            "perfis de agentes, crons agendados e logs"
        ),
    },
    {
        "id": "hermes-cron-recovery",
        "label": "Agente de recuperação cron Hermes",
        "category": "operacoes-ti",
        "prompt": (
            "especialista em crons Hermes: gateway do scheduler (hermes gateway run), "
            "jobs.json, falhas do cron.scheduler e re-disparo de jobs com hermes cron run"
        ),
    },
    {
        "id": "hermes-cron-diagnostic",
        "label": "Agente de diagnóstico cron Hermes",
        "category": "operacoes-ti",
        "prompt": (
            "analista de falhas em crons Hermes: scheduler, jobs.json, logs errors.log, "
            "outputs em cron/output — apenas diagnóstico, sem reiniciar serviços"
        ),
    },
    {
        "id": "tech-lead",
        "label": "Tech lead",
        "category": "gestão",
        "prompt": (
            "Crie um papel de tech lead: arquitetura, mentoria, code review, "
            "desbloqueio técnico e alinhamento com produto."
        ),
    },
    {
        "id": "product-manager",
        "label": "Product manager",
        "category": "produto",
        "prompt": (
            "Crie um papel de product manager: discovery, priorização, user stories, "
            "métricas de produto e comunicação com engenharia e design."
        ),
    },
    {
        "id": "ux-designer",
        "label": "Designer UX/UI",
        "category": "design",
        "prompt": (
            "Crie um papel de designer UX/UI: pesquisa, wireframes, protótipos, "
            "design system e handoff para desenvolvimento."
        ),
    },
    {
        "id": "data-engineer",
        "label": "Engenheiro de dados",
        "category": "dados",
        "prompt": (
            "Crie um papel de engenheiro de dados: pipelines ETL, modelagem, "
            "qualidade de dados e integração analítica."
        ),
    },
    {
        "id": "security",
        "label": "Segurança",
        "category": "segurança",
        "prompt": (
            "Crie um papel de analista de segurança: threat modeling, revisão de código "
            "focada em riscos, hardening e resposta a vulnerabilidades."
        ),
    },
    {
        "id": "support",
        "label": "Suporte técnico",
        "category": "operações",
        "prompt": (
            "Crie um papel de suporte técnico: triagem de tickets, diagnóstico, "
            "documentação de soluções e escalonamento para engenharia."
        ),
    },
    {
        "id": "scrum-master",
        "label": "Scrum master",
        "category": "gestão",
        "prompt": (
            "Crie um papel de scrum master: facilitação de cerimônias, remoção de "
            "impedimentos, métricas de fluxo e melhoria contínua do time."
        ),
    },
    {
        "id": "architect",
        "label": "Arquiteto de software",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de arquiteto de software: decisões de arquitetura, "
            "padrões, ADRs, revisão de desenhos técnicos e alinhamento entre squads."
        ),
    },
    {
        "id": "technical-writer",
        "label": "Documentação técnica",
        "category": "produto",
        "prompt": (
            "Crie um papel de technical writer: documentação de APIs, guias de "
            "onboarding, changelogs e manuais para desenvolvedores e usuários."
        ),
    },
    {
        "id": "platform-engineer",
        "label": "Engenheiro de plataforma",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de engenheiro de plataforma: SDKs internos, templates, "
            "golden paths, self-service para squads e padronização de stacks."
        ),
    },
    {
        "id": "cloud-engineer",
        "label": "Engenheiro cloud",
        "category": "operações",
        "prompt": (
            "Crie um papel de engenheiro cloud: AWS/GCP/Azure, IaC (Terraform), "
            "rede, identidade, custos e boas práticas de segurança na nuvem."
        ),
    },
    {
        "id": "ml-engineer",
        "label": "Engenheiro de ML",
        "category": "dados",
        "prompt": (
            "Crie um papel de engenheiro de machine learning: pipelines de treino, "
            "feature stores, avaliação de modelos, MLOps e deploy de inferência."
        ),
    },
    {
        "id": "ai-engineer",
        "label": "Engenheiro de IA",
        "category": "dados",
        "prompt": (
            "Crie um papel de engenheiro de inteligência artificial e arquiteto de "
            "sistemas de IA: LLMs, RAG, agentes, orquestração, MLOps, avaliação de "
            "modelos, pesquisa de técnicas na web e banco de conhecimento persistente."
        ),
    },
    {
        "id": "release-engineer",
        "label": "Release engineer",
        "category": "operações",
        "prompt": (
            "Crie um papel de release engineer: versionamento, changelogs, "
            "estratégias de deploy (blue/green, canary), feature flags e rollback."
        ),
    },
    {
        "id": "database-engineer",
        "label": "Engenheiro de banco de dados",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de engenheiro de banco de dados: modelagem, migrações, "
            "índices, tuning de queries, backups e alta disponibilidade."
        ),
    },
    {
        "id": "performance-engineer",
        "label": "Engenheiro de performance",
        "category": "qualidade",
        "prompt": (
            "Crie um papel de engenheiro de performance: profiling, benchmarks, "
            "gargalos de CPU/memória/IO e metas de latência em produção."
        ),
    },
    {
        "id": "engineering-manager",
        "label": "Engineering manager",
        "category": "gestão",
        "prompt": (
            "Crie um papel de engineering manager: contratação, 1:1s, planejamento "
            "de capacidade, remoção de bloqueios e saúde do time de engenharia."
        ),
    },
    {
        "id": "api-engineer",
        "label": "Engenheiro de integrações / API",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de engenheiro de APIs e integrações: contratos OpenAPI, "
            "webhooks, idempotência, rate limits e compatibilidade entre serviços."
        ),
    },
    {
        "id": "embedded-engineer",
        "label": "Engenheiro embedded",
        "category": "desenvolvimento",
        "prompt": (
            "Crie um papel de engenheiro embedded: firmware, RTOS, drivers, "
            "restrições de hardware e testes em dispositivo."
        ),
    },
    {
        "id": "instructional-designer",
        "label": "Designer instrucional",
        "category": "infoprodutos",
        "prompt": (
            "Crie um papel de designer instrucional: módulos, objetivos de aprendizagem, "
            "sequência pedagógica, exercícios e avaliações para cursos online."
        ),
    },
    {
        "id": "video-producer",
        "label": "Produtor de vídeo",
        "category": "infoprodutos",
        "prompt": (
            "Crie um papel de produtor de vídeo educacional: roteiro por aula, "
            "vídeos avatar HeyGen, revisão de takes e entrega pronta para plataforma."
        ),
    },
    {
        "id": "sales-copywriter",
        "label": "Copywriter de vendas",
        "category": "infoprodutos",
        "prompt": (
            "Crie um papel de copywriter de infoprodutos: página de vendas, promessa, "
            "bônus, prova social, urgência e sequência de e-mails de lançamento."
        ),
    },
    {
        "id": "course-publisher",
        "label": "Publicador de curso",
        "category": "infoprodutos",
        "prompt": (
            "Crie um papel de publicador de cursos digitais: Hotmart, KDP, HTML offline, "
            "pacotes ZIP, checklist de publicação e configuração de preço."
        ),
    },
]

_ROLE_PRESET_DEFAULT_SKILLS: dict[str, list[str]] = {
    "ai-engineer": [
        "qclaw-chat-ai-engineer",
        "qclaw-mcp-search",
        "qclaw-agent-langchain",
        "qclaw-llm-models",
    ],
    "instructional-designer": [
        "qclaw-roteiro-curso-completo",
        "qclaw-roteiro-cursos",
        "qclaw-roteiro-lab-index",
        "qclaw-roteiro-lab-texto-aula",
        "qclaw-roteiro-thumbnails",
        "qclaw-chat-roteiro-thumbnails",
    ],
    "video-producer": [
        "qclaw-roteiro-heygen",
        "qclaw-roteiro-curso-completo",
        "qclaw-roteiro-lab-index",
        "qclaw-roteiro-lab-atos",
        "qclaw-roteiro-lab-refino",
        "qclaw-roteiro-lab-limpa-fala",
        "qclaw-roteiro-thumbnails",
        "qclaw-chat-roteiro-thumbnails",
    ],
    "course-publisher": [
        "qclaw-roteiro-curso-completo",
        "qclaw-roteiro-cursos",
        "qclaw-roteiro-thumbnails",
        "qclaw-chat-roteiro-thumbnails",
    ],
    "ux-designer": [
        "qclaw-roteiro-curso-completo",
        "qclaw-roteiro-gamma-storyboard",
        "qclaw-roteiro-thumbnails",
        "qclaw-chat-roteiro-thumbnails",
    ],
    "technical-writer": [
        "qclaw-roteiro-lab-index",
        "qclaw-roteiro-lab-texto-aula",
        "qclaw-roteiro-lab-refino",
        "qclaw-article-references-verify",
        "qclaw-roteiro-thumbnails",
        "qclaw-chat-roteiro-thumbnails",
    ],
    "sales-copywriter": [
        "qclaw-roteiro-editorial",
        "qclaw-roteiro-capas",
    ],
    "scrum-master": [
        "qclaw-kanban-weekly-summary",
        "qclaw-kanban-weekly-review",
        "qclaw-report-cards",
        "qclaw-kanban-list-all",
        "qclaw-kanban-whatsapp-status",
    ],
    "tech-lead": [
        "qclaw-kanban-weekly-summary",
        "qclaw-kanban-list-all",
        "qclaw-report-cards",
    ],
    "engineering-manager": [
        "qclaw-kanban-weekly-summary",
        "qclaw-kanban-weekly-review",
        "qclaw-report-cards",
        "qclaw-kanban-list-all",
        "qclaw-kanban-whatsapp-status",
    ],
    "product-manager": [
        "qclaw-kanban-weekly-summary",
        "qclaw-report-cards",
        "qclaw-kanban-list-all",
    ],
}


def preset_default_skills(preset_id: str) -> list[str]:
    """Optional Hermes/OpenClaw skills bundled with a role preset."""
    return list(_ROLE_PRESET_DEFAULT_SKILLS.get(str(preset_id or "").strip(), ()))


_ROLE_DOMAIN_CONTEXT: dict[str, tuple[str, list[str]]] = {
    "desenvolvimento": (
        "time de desenvolvimento de software",
        [
            "Priorize qualidade, segurança e entregas incrementais.",
            "Documente decisões relevantes e peça contexto quando faltar informação.",
            "Colabore com produto, QA, DevOps, design e outros engenheiros de forma pragmática.",
        ],
    ),
    "qualidade": ("time de desenvolvimento de software", []),
    "operações": ("time de desenvolvimento de software", []),
    "gestão": ("time de desenvolvimento de software", []),
    "produto": ("time de desenvolvimento de software", []),
    "design": ("time de desenvolvimento de software", []),
    "dados": ("time de desenvolvimento de software", []),
    "segurança": ("time de desenvolvimento de software", []),
    "operacoes-ti": (
        "operações de tecnologia da informação (TI)",
        [
            "Priorize disponibilidade, segurança e experiência do usuário interno.",
            "Siga processos ITIL quando aplicável e documente incidentes e mudanças.",
            "Escale com contexto claro (impacto, timeline, logs) para o próximo nível.",
        ],
    ),
    "pesquisa-cientifica": (
        "pesquisa científica e ambiente acadêmico",
        [
            "Priorize rigor metodológico, ética e reprodutibilidade.",
            "Cite fontes, descreva limitações e separe hipótese de evidência.",
            "Alinhe entregas a editais, comitês de ética e prazos de publicação.",
        ],
    ),
    "youtube": (
        "criação de conteúdo para YouTube e ecossistema de creators",
        [
            "Priorize retenção, clareza e consistência com a identidade do canal.",
            "Respeite diretrizes da plataforma, direitos autorais e disclosure de patrocínio.",
            "Use métricas (CTR, retenção, RPM) para iterar sem sacrificar autenticidade.",
            "Para roteiros por atos, refino e TTS use skills qclaw-roteiro-lab-* (índice qclaw-roteiro-lab-index).",
        ],
    ),
    "infoprodutos": (
        "criação de cursos online, ebooks e infoprodutos digitais",
        [
            "Priorize clareza pedagógica, entregáveis verificáveis e pipeline reprodutível.",
            "Use skills qclaw-roteiro-curso-completo, qclaw-roteiro-cursos, qclaw-roteiro-heygen "
            "e qclaw-roteiro-thumbnails (qclaw-chat-roteiro-thumbnails no chat) quando aplicável.",
            "Para labs de texto/roteiro na API RV use qclaw-roteiro-lab-texto-aula, qclaw-roteiro-lab-atos e qclaw-roteiro-lab-index.",
            "Para ebooks, artigos e materiais com bibliografia use qclaw-article-references-verify para confirmar se as referências existem de fato.",
            "Alinhe conteúdo, mídia e publicação (Hotmart/KDP/HTML) antes de considerar o produto pronto.",
        ],
    ),
}

_DEFAULT_DOMAIN = (
    "organização profissional",
    [
        "Priorize qualidade e comunicação clara.",
        "Documente decisões relevantes e peça contexto quando faltar informação.",
        "Colabore de forma pragmática com as partes interessadas.",
    ],
)


def _merge_team_role_presets() -> list[dict[str, str]]:
    seen: set[str] = set()
    merged: list[dict[str, str]] = []
    for preset in list(_SOFTWARE_TEAM_ROLE_PRESETS) + list(EXTENDED_ROLE_PRESETS):
        pid = str(preset.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        merged.append(preset)
    return merged


def _domain_for_preset(preset: dict[str, str]) -> tuple[str, list[str]]:
    category = str(preset.get("category") or "").strip().lower()
    if category in _ROLE_DOMAIN_CONTEXT:
        org, bullets = _ROLE_DOMAIN_CONTEXT[category]
        if bullets:
            return org, bullets
        _, default_bullets = _ROLE_DOMAIN_CONTEXT["desenvolvimento"]
        return org, default_bullets
    return _DEFAULT_DOMAIN


def preset_agent_spec(preset: dict[str, str]) -> AgentSpec:
    """Build a Hermes profile spec from a TEAM_ROLE_PRESETS entry (no LLM)."""
    pid = str(preset.get("id") or "").strip()
    if pid == "hermes-cron-recovery":
        label = str(preset.get("label") or "Agente de recuperação cron Hermes").strip()
        soul = (
            f"# {label}\n\n"
            "Você recupera o **cron scheduler** do Hermes: jobs em `cron/jobs.json` só "
            "disparam com gateway ativo no mesmo `HERMES_HOME`.\n\n"
            "## Escopo\n"
            "- `hermes cron status` / gateway para o home dos jobs.\n"
            "- `hermes cron run` para re-disparar jobs após falha do scheduler.\n"
            "- Logs: `cron.scheduler`, `gateway.log`, `errors.log`.\n\n"
            "## Como atuar\n"
            "1. Confirmar scheduler parado (`Gateway is not running`).\n"
            "2. Subir gateway (`hermes gateway start` ou ensure via gois).\n"
            "3. Validar `Gateway is running` e próximas execuções.\n"
            "4. Re-run de jobs com `last_status=error` se aplicável.\n"
        )
        return AgentSpec(name=pid, description=label, soul=soul)
    if pid == "hermes-cron-diagnostic":
        label = str(preset.get("label") or "Agente de diagnóstico cron Hermes").strip()
        soul = (
            f"# {label}\n\n"
            "Você **diagnostica** falhas de cron Hermes sem aplicar correções automáticas.\n\n"
            "## Escopo\n"
            "- Scheduler: `hermes cron status`, HERMES_HOME vs `jobs.json`.\n"
            "- Jobs com `last_status=error` e ficheiros em `cron/output/<job_id>/`.\n"
            "- Linhas `cron.scheduler: Job '…' failed` em `errors.log`.\n"
            "- Skill/chat: `qclaw-hermes-cron-health` no OpenClaw.\n\n"
            "## Como atuar\n"
            "1. Relatório estruturado (issues + recomendações).\n"
            "2. Evidência em logs e último output do job.\n"
            "3. Separar: scheduler parado vs falha de negócio no agente.\n"
            "4. Indicar recuperação (gateway/re-run) só como recomendação ao operador.\n"
        )
        return AgentSpec(name=pid, description=label, soul=soul)
    if pid == "ai-engineer":
        label = str(preset.get("label") or "Engenheiro de IA").strip()
        soul = (
            f"# {label}\n\n"
            "Você é **engenheiro de inteligência artificial e arquiteto de sistemas de IA** "
            "em contexto de pesquisa aplicada e engenharia de produto.\n\n"
            "## Escopo\n"
            "- Arquitetura de LLMs, RAG, agentes e orquestração multi-agente.\n"
            "- MLOps, avaliação, observabilidade, custo de inferência e routing de modelos.\n"
            "- Padrões de produção: guardrails, safety, caching e memória.\n"
            "- Pesquisa contínua na web e **banco de conhecimento local** "
            "(skill `qclaw-chat-ai-engineer`).\n\n"
            "## Como atuar\n"
            "1. Consulte o banco (`qclaw_ai_kb_search`) antes de pesquisar na web.\n"
            "2. Use `web_search` para técnicas, papers e benchmarks recentes.\n"
            "3. Sintetize trade-offs e recomende arquitetura pragmática.\n"
            "4. Persista aprendizados com `qclaw_ai_kb_store` (título, categoria, fonte).\n"
            "5. Cite fontes (URL ou entrada do banco); não invente papers.\n\n"
            "## Skills OpenClaw (chat)\n"
            "- `qclaw-chat-ai-engineer` — banco de conhecimento e persona de IA.\n"
            "- `qclaw-mcp-search` — descobrir MCPs e ferramentas.\n"
            "- `qclaw-agent-langchain` / `qclaw-agent-langgraph` — bootstrap de agentes.\n"
            "- `qclaw-llm-models` — modelos disponíveis no gois.\n"
        )
        return AgentSpec(name=pid, description=label, soul=soul)
    if pid == "hermes-recovery":
        label = str(preset.get("label") or "Agente de recuperação Hermes").strip()
        soul = (
            f"# {label}\n\n"
            "Você é o **agente de recuperação do Hermes** em contexto de operações de TI.\n\n"
            "## Escopo\n"
            "- Diagnosticar e recuperar o **gateway** Hermes (`hermes gateway start|stop`).\n"
            "- Verificar o **dashboard** web (porta padrão 9119) e perfis em `profiles/`.\n"
            "- Inspecionar **crons** (`hermes cron list`, jobs com erro) e logs em "
            "`logs/gateway.log`, `errors.log`, `agent.log`.\n"
            "- Coordenar com o **gois** (health, recovery automático, skill "
            "`qclaw-hermes-cron-health`) quando estiver no chat OpenClaw.\n\n"
            "## Como atuar\n"
            "- Priorize evidência: processo, HTTP do dashboard, últimas linhas de log.\n"
            "- Reinicie gateway ou dashboard só quando o modo de falha exigir.\n"
            "- Documente causa provável, ação tomada e estado final.\n"
            "- Escale ao operador se faltar permissão ou a falha persistir após restart.\n"
        )
        return AgentSpec(name=pid, description=label, soul=soul)

    label = str(preset.get("label") or preset.get("id") or "Papel").strip()
    scope = str(preset.get("prompt") or "").strip()
    if scope.lower().startswith("crie um papel de "):
        scope = scope[len("crie um papel de ") :].strip()
    if scope.endswith("."):
        scope = scope[:-1].strip()
    org, bullets = _domain_for_preset(preset)
    how = "\n".join(f"- {b}" for b in bullets)
    soul = (
        f"# {label}\n\n"
        f"Você é **{label}** em contexto de {org}.\n\n"
        f"## Escopo\n{scope}.\n\n"
        "## Como atuar\n"
        f"{how}\n"
    )
    return AgentSpec(
        name=str(preset["id"]),
        description=label,
        soul=soul,
    )


# Presets shown on the /roles dashboard (software + operações TI + pesquisa + YouTube).
TEAM_ROLE_PRESETS: list[dict[str, str]] = _merge_team_role_presets()


def _seed_error_skippable(exc: Exception) -> bool:
    """Profile already on disk / API — treat as skipped, not failure."""
    return "already exists" in str(exc).lower()


def _seed_error_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "connection refused",
            "timed out",
            "timeout",
            "401",
            "unauthorized",
            "502",
            "503",
            "remote protocol error",
        )
    )


def _profile_matches_preset(profile_name: str, preset_id: str) -> bool:
    name = profile_name.strip().lower()
    pid = preset_id.strip().lower()
    if not name or not pid:
        return False
    return name == pid or name.startswith(f"{pid}-")


def hermes_profiles_root() -> Path:
    """Writable Hermes profiles directory (``~/.hermes/profiles`` or ``.stack/hermes``)."""
    from .local_paths import hermes_home

    return hermes_home() / "profiles"


def _hermes_profile_dir_names() -> set[str]:
    """Profile slugs on disk under ~/.hermes/profiles (fallback when API list lags)."""
    root = hermes_profiles_root()
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


_PRESET_BY_ID: dict[str, dict[str, str]] = {
    str(p["id"]): p for p in TEAM_ROLE_PRESETS if p.get("id")
}


def role_catalog_status(*, profiles_root: Optional[Path] = None) -> dict[str, Any]:
    """Counts for the built-in role catalog vs profiles installed on disk."""
    from collections import Counter

    preset_ids = set(_PRESET_BY_ID)
    disk_rows = list_hermes_profiles_filesystem(profiles_root=profiles_root)
    disk_names = {
        str(r.get("name") or "").strip()
        for r in disk_rows
        if str(r.get("name") or "").strip() and r.get("name") != "default"
    }
    installed = {n for n in disk_names if n in preset_ids}
    missing = sorted(preset_ids - installed)
    by_cat = Counter(
        str(p.get("category") or "outros") for p in TEAM_ROLE_PRESETS
    )
    return {
        "catalog_total": len(TEAM_ROLE_PRESETS),
        "installed_total": len(installed),
        "missing_count": len(missing),
        "profiles_on_disk": len(disk_rows),
        "categories": dict(sorted(by_cat.items())),
        "profiles_root": str((profiles_root or hermes_profiles_root()).expanduser()),
    }


def list_hermes_profiles_filesystem(
    *,
    profiles_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Fast profile list from on-disk directories (no Hermes dashboard HTTP call)."""
    root = (profiles_root or hermes_profiles_root()).expanduser()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        preset = _PRESET_BY_ID.get(path.name)
        label = str(preset.get("label") or "") if preset else ""
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "display_name": label or path.name.replace("-", " ").replace("_", " "),
                "description": label,
                "category": str(preset.get("category") or "") if preset else "",
            }
        )
    return rows


_FS_COPY_IGNORE = shutil.ignore_patterns(
    "sessions",
    "logs",
    "memories",
    "workspace",
    "plans",
    "cron",
    "home",
)


def _resolve_seed_template_dir(
    profiles_root: Path, template_profile: Optional[str] = None
) -> Path:
    if template_profile:
        path = profiles_root / template_profile.strip()
        if path.is_dir():
            return path
        raise RuntimeError(f"template profile not found: {path}")
    for name in ("default", "backend-dev", "orchestrator", "whatsapp-coder"):
        path = profiles_root / name
        if path.is_dir() and (path / "config.yaml").is_file():
            return path
    for path in sorted(profiles_root.iterdir()):
        if path.is_dir() and (path / "config.yaml").is_file():
            return path
    raise RuntimeError(f"no template profile under {profiles_root}")


def seed_team_role_presets_filesystem(
    *,
    only_missing: bool = True,
    preset_ids: Optional[list[str]] = None,
    categories: Optional[list[str]] = None,
    profiles_root: Optional[Path] = None,
    template_profile: Optional[str] = None,
    progress_every: int = 50,
) -> dict[str, Any]:
    """Clone preset profiles on disk (fast; Hermes picks them up like API-created ones)."""
    root = (profiles_root or Path.home() / ".hermes" / "profiles").expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"profiles root missing: {root}", "created": [], "skipped": []}

    try:
        template = _resolve_seed_template_dir(root, template_profile)
    except Exception as e:
        return {"ok": False, "error": str(e), "created": [], "skipped": []}

    wanted = {str(x).strip() for x in (preset_ids or []) if str(x).strip()}
    cat_wanted = {str(c).strip().lower() for c in (categories or []) if str(c).strip()}
    presets = [
        p
        for p in TEAM_ROLE_PRESETS
        if (not wanted or str(p.get("id") or "") in wanted)
        and (not cat_wanted or str(p.get("category") or "").strip().lower() in cat_wanted)
    ]
    if not presets:
        return {"ok": False, "error": "nenhum preset correspondente", "created": [], "skipped": []}

    existing_names = _hermes_profile_dir_names()
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    total = len(presets)

    for idx, preset in enumerate(presets, start=1):
        pid = str(preset.get("id") or "")
        target = root / pid
        if only_missing and (
            pid in existing_names
            or target.is_dir()
            or any(_profile_matches_preset(n, pid) for n in existing_names)
        ):
            skipped.append(pid)
            continue
        if progress_every > 0 and idx % progress_every == 0:
            log.info(
                "filesystem role seed %d/%d (created=%d skipped=%d)",
                idx,
                total,
                len(created),
                len(skipped),
            )
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(
                template, target, ignore=_FS_COPY_IGNORE, dirs_exist_ok=False
            )
            spec = preset_agent_spec(preset)
            (target / "SOUL.md").write_text(spec.soul, encoding="utf-8")
            existing_names.add(pid)
            created.append({"preset_id": pid, "name": pid, "renamed": False})
        except Exception as e:
            log.warning("filesystem seed %s failed: %s", pid, e)
            errors.append({"preset_id": pid, "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": not errors,
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "total_presets": total,
        "mode": "filesystem",
        "template": template.name,
        "profiles_root": str(root),
    }


def _unique_filesystem_profile_name(base_name: str) -> str:
    existing = _hermes_profile_dir_names()
    base = _normalize_name(base_name)
    if base not in existing:
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"
        if len(candidate) > 64:
            candidate = f"{base[:58]}-{i}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"could not find unused name near {base_name!r}")


def create_hermes_profile_filesystem(
    spec: AgentSpec,
    *,
    profile_meta: Optional[dict[str, Any]] = None,
    profiles_root: Optional[Path] = None,
    template_profile: Optional[str] = None,
    model_id: Optional[str] = None,
    chat_cfg: Optional[Any] = None,
) -> dict[str, Any]:
    """Clone a template profile on disk and write SOUL/meta (no dashboard HTTP)."""
    import yaml

    root = (profiles_root or hermes_profiles_root()).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"profiles root missing: {root}")

    template = _resolve_seed_template_dir(root, template_profile)
    profile_name = _unique_filesystem_profile_name(spec.name)
    target = root / profile_name
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(template, target, ignore=_FS_COPY_IGNORE, dirs_exist_ok=False)
    (target / "SOUL.md").write_text(spec.soul, encoding="utf-8")

    meta_path = target / "profile.yaml"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, yaml.YAMLError):
            meta = {}
    meta["description"] = spec.description
    meta["description_auto"] = False
    if profile_meta:
        meta.update(profile_meta)
    meta_path.write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    if model_id:
        from .hermes_profile_model import write_profile_model_default

        write_profile_model_default(
            profile_name,
            model_id,
            chat_cfg=chat_cfg,
            profiles_root=root,
        )

    return {
        "ok": True,
        "name": profile_name,
        "path": str(target),
        "description": spec.description,
        "requested_name": spec.name,
        "renamed": profile_name != spec.name,
        "mode": "filesystem",
    }


def seed_team_role_presets(
    dashboard_url: str,
    *,
    clone_from_default: bool = True,
    only_missing: bool = True,
    preset_ids: Optional[list[str]] = None,
    categories: Optional[list[str]] = None,
    timeout: float = 180.0,
    progress_every: int = 25,
    max_retries: int = 5,
    retry_delay_seconds: float = 2.0,
    pause_between_seconds: float = 0.15,
    use_filesystem: bool = False,
    profiles_root: Optional[Path] = None,
    template_profile: Optional[str] = None,
) -> dict[str, Any]:
    """Create Hermes profiles from TEAM_ROLE_PRESETS (optionally filtered)."""
    if use_filesystem:
        return seed_team_role_presets_filesystem(
            only_missing=only_missing,
            preset_ids=preset_ids,
            categories=categories,
            profiles_root=profiles_root,
            template_profile=template_profile,
            progress_every=progress_every,
        )

    wanted = {str(x).strip() for x in (preset_ids or []) if str(x).strip()}
    cat_wanted = {str(c).strip().lower() for c in (categories or []) if str(c).strip()}
    presets = [
        p
        for p in TEAM_ROLE_PRESETS
        if (not wanted or str(p.get("id") or "") in wanted)
        and (not cat_wanted or str(p.get("category") or "").strip().lower() in cat_wanted)
    ]
    if not presets:
        return {"ok": False, "error": "nenhum preset correspondente", "created": [], "skipped": []}

    existing_rows: list[dict] = []
    try:
        existing_rows = list_hermes_profiles(dashboard_url, timeout=timeout)
    except Exception as e:
        log.warning(
            "Hermes list failed (%s: %s); using ~/.hermes/profiles on disk",
            type(e).__name__,
            e,
        )

    existing_names = _hermes_profile_dir_names()
    existing_names |= {
        str(p.get("name") or "").strip()
        for p in existing_rows
        if isinstance(p, dict) and p.get("name")
    }

    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []

    total = len(presets)
    for idx, preset in enumerate(presets, start=1):
        pid = str(preset.get("id") or "")
        if only_missing and (
            pid in existing_names
            or any(_profile_matches_preset(n, pid) for n in existing_names)
        ):
            skipped.append(pid)
            continue
        if progress_every > 0 and idx % progress_every == 0:
            log.info(
                "role catalog seed %d/%d (created=%d skipped=%d errors=%d)",
                idx,
                total,
                len(created),
                len(skipped),
                len(errors),
            )
        result: Optional[dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for attempt in range(max(1, max_retries)):
            try:
                result = create_hermes_agent(
                    dashboard_url,
                    preset_agent_spec(preset),
                    clone_from_default=clone_from_default,
                    timeout=timeout,
                )
                break
            except Exception as e:
                last_err = e
                if _seed_error_skippable(e):
                    skipped.append(pid)
                    existing_names.add(pid)
                    result = None
                    break
                if _seed_error_retriable(e) and attempt + 1 < max_retries:
                    delay = retry_delay_seconds * (attempt + 1)
                    log.info(
                        "seed preset %s retry %d/%d after %s (sleep %.1fs)",
                        pid,
                        attempt + 2,
                        max_retries,
                        type(e).__name__,
                        delay,
                    )
                    time.sleep(delay)
                    try:
                        existing_rows = list_hermes_profiles(
                            dashboard_url, timeout=timeout
                        )
                        existing_names = {
                            str(p.get("name") or "").strip()
                            for p in existing_rows
                            if isinstance(p, dict) and p.get("name")
                        }
                    except Exception:
                        pass
                    continue
                break
        if result is None:
            if last_err and not _seed_error_skippable(last_err):
                log.warning("seed preset %s failed: %s", pid, last_err)
                errors.append(
                    {"preset_id": pid, "error": f"{type(last_err).__name__}: {last_err}"}
                )
            continue
        profile_name = str(result.get("name") or pid)
        existing_names.add(profile_name)
        created.append(
            {
                "preset_id": pid,
                "name": profile_name,
                "renamed": bool(result.get("renamed")),
            }
        )
        if pause_between_seconds > 0:
            time.sleep(pause_between_seconds)

    return {
        "ok": not errors,
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "total_presets": len(presets),
    }


@dataclass
class AgentSpec:
    name: str
    description: str
    soul: str


@dataclass
class DevAgentSpec(AgentSpec):
    requirement_prompt: str
    schedule: str
    skills: list[str] = field(default_factory=list)
    workdir: Optional[str] = None


def _normalize_name(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    if not slug or not slug[0].isalnum():
        slug = f"agent-{slug}" if slug else "agent"
    return slug[:64]


def _parse_legacy_spec_json(text: str) -> AgentSpec:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("model returned non-object JSON")
    name = _normalize_name(str(data.get("name") or ""))
    description = str(data.get("description") or "").strip()
    soul = str(data.get("soul") or "").strip()
    if not PROFILE_NAME_RE.match(name):
        raise ValueError(f"invalid profile name: {name!r}")
    if not description:
        raise ValueError("description is required")
    if not soul:
        raise ValueError("soul is required")
    return AgentSpec(name=name, description=description, soul=soul)


def _parse_dev_spec_json(
    text: str,
    *,
    known_skills: set[str],
    default_schedule: str,
    default_skills: list[str],
) -> DevAgentSpec:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("model returned non-object JSON")
    name = _normalize_name(str(data.get("name") or ""))
    description = str(data.get("description") or "").strip()
    soul = str(data.get("soul") or "").strip()
    requirement = str(data.get("requirement_prompt") or data.get("prompt") or "").strip()
    schedule = str(data.get("schedule") or "").strip() or default_schedule
    workdir_raw = data.get("workdir")
    workdir = str(workdir_raw).strip() if workdir_raw else None
    if workdir == "":
        workdir = None

    raw_skills = data.get("skills")
    requested: list[str] = []
    if isinstance(raw_skills, list):
        requested = [str(s) for s in raw_skills]
    elif isinstance(raw_skills, str) and raw_skills.strip():
        requested = [raw_skills.strip()]

    if not PROFILE_NAME_RE.match(name):
        raise ValueError(f"invalid profile name: {name!r}")
    if not description:
        raise ValueError("description is required")
    if not soul:
        raise ValueError("soul is required")
    if not requirement:
        raise ValueError("requirement_prompt is required")

    skills = normalize_skill_names(
        requested,
        known_skills,
        defaults=default_skills,
    )
    soul = _append_skills_to_soul(soul, skills)
    return DevAgentSpec(
        name=name,
        description=description,
        soul=soul,
        requirement_prompt=requirement,
        schedule=schedule,
        skills=skills,
        workdir=workdir,
    )


def _append_skills_to_soul(soul: str, skills: list[str]) -> str:
    from .skills_mcp_bootstrap import ensure_soul_has_mcp_block

    if skills:
        block = (
            "\n\n## Hermes skills (cron)\n"
            "Este perfil usa estas skills nas execuções agendadas:\n"
            + "\n".join(f"- `{s}`" for s in skills)
        )
        if "## Hermes skills" not in soul:
            soul = soul.rstrip() + block
    return ensure_soul_has_mcp_block(soul)


def _dev_skills_catalog(create_cfg: HermesAgentCreateConfig) -> dict[str, Any]:
    return list_development_skills(categories=create_cfg.skill_categories)


def resolve_role_spec(
    role_text: str,
    role_preset: Optional[str],
    agent_cfg: AgentConfig,
) -> AgentSpec:
    """Use built-in preset SOUL when available; otherwise parse via LLM."""
    pid = str(role_preset or "").strip()
    if pid:
        preset = _PRESET_BY_ID.get(pid)
        if preset:
            return preset_agent_spec(preset)
    return parse_agent_from_text(role_text, agent_cfg)


def generate_personality_from_prompt(
    prompt: str,
    *,
    agent_cfg: AgentConfig,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    role_preset: Optional[str] = None,
) -> dict[str, Any]:
    """Generate SOUL.md content from natural language without creating a profile."""
    user_prompt = str(prompt or "").strip()
    if not user_prompt:
        raise ValueError("prompt is required")

    parts: list[str] = []
    if display_name and str(display_name).strip():
        parts.append(f"Nome de exibição do agente: {str(display_name).strip()}")
    if role and str(role).strip():
        parts.append(f"Papel resumido: {str(role).strip()}")
    pid = str(role_preset or "").strip()
    if pid:
        preset = _PRESET_BY_ID.get(pid)
        if preset:
            label = str(preset.get("label") or pid)
            parts.append(f"Baseie-se no preset de papel «{label}».")
            preset_prompt = str(preset.get("prompt") or "").strip()
            if preset_prompt:
                parts.append(preset_prompt)
    parts.append(f"Pedido do usuário:\n{user_prompt}")
    combined = "\n\n".join(p for p in parts if p)

    spec = parse_agent_from_text(combined, agent_cfg)
    return {
        "ok": True,
        "soul": spec.soul,
        "description": spec.description,
        "suggested_name": spec.name,
    }


def parse_agent_from_text(text: str, agent_cfg: AgentConfig) -> AgentSpec:
    """Legacy: profile only (no cron)."""
    prompt = text.strip()
    if not prompt:
        raise ValueError("text is required")

    api_key = os.environ.get(agent_cfg.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"{agent_cfg.api_key_env} not set; cannot parse agent description"
        )

    from .llm_gateway import make_client, trace_context

    client = make_client(
        api_key=api_key,
        base_url=agent_cfg.base_url,
        timeout=agent_cfg.timeout_seconds,
    )
    with trace_context(name="hermes.parse_legacy_spec", model_label=agent_cfg.model):
        resp = client.chat.completions.create(
            model=agent_cfg.model,
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM_ROLE},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raise RuntimeError("model returned empty response")
    return _parse_legacy_spec_json(raw)


def parse_dev_agent_from_text(
    text: str,
    agent_cfg: AgentConfig,
    create_cfg: HermesAgentCreateConfig,
    *,
    schedule_override: Optional[str] = None,
    skills_override: Optional[list[str]] = None,
    workdir_override: Optional[str] = None,
) -> DevAgentSpec:
    """Parse a dev agent + cron spec using the configured LLM."""
    prompt = text.strip()
    if not prompt:
        raise ValueError("text is required")

    catalog = _dev_skills_catalog(create_cfg)
    skill_rows = catalog.get("skills") or []
    known = {str(r["name"]) for r in skill_rows if r.get("name")}
    defaults = list(create_cfg.default_skills or DEFAULT_DEV_SKILL_SLUGS)

    system = (
        _PARSE_SYSTEM_BASE
        + "\n\nAVAILABLE SKILLS (use exact slug in \"skills\" array):\n"
        + format_skills_for_prompt(skill_rows)
    )

    api_key = os.environ.get(agent_cfg.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"{agent_cfg.api_key_env} not set; cannot parse agent description"
        )

    from .llm_gateway import make_client, trace_context

    client = make_client(
        api_key=api_key,
        base_url=agent_cfg.base_url,
        timeout=agent_cfg.timeout_seconds,
    )
    with trace_context(name="hermes.parse_dev_agent", model_label=agent_cfg.model):
        resp = client.chat.completions.create(
            model=agent_cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raise RuntimeError("model returned empty response")

    spec = _parse_dev_spec_json(
        raw,
        known_skills=known,
        default_schedule=create_cfg.default_schedule,
        default_skills=defaults,
    )
    if schedule_override:
        spec.schedule = schedule_override.strip()
    if skills_override:
        spec.skills = normalize_skill_names(
            skills_override,
            known,
            defaults=defaults,
        )
        spec.soul = _append_skills_to_soul(
            re.sub(r"\n\n## Hermes skills.*", "", spec.soul, flags=re.DOTALL).strip(),
            spec.skills,
        )
    if workdir_override:
        spec.workdir = workdir_override.strip() or None
    elif create_cfg.default_workdir and not spec.workdir:
        spec.workdir = create_cfg.default_workdir.strip() or None
    return spec


def hermes_http_timeout(seconds: float) -> httpx.Timeout:
    """Short connect, long read — profile clone via the dashboard API is often slow."""
    return httpx.Timeout(connect=10.0, read=seconds, write=seconds, pool=10.0)


def hermes_dashboard_http_up(dashboard_url: str, *, timeout: float = 3.0) -> bool:
    """True when the Hermes web UI accepts HTTP (same check as recovery loop)."""
    url = dashboard_url.rstrip("/") + "/"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=2.0)) as client:
            resp = client.get(url)
            return resp.status_code < 500
    except Exception:
        return False


def _api_base(dashboard_url: str) -> str:
    base = dashboard_url.rstrip("/") + "/"
    return urljoin(base, "api/")


_SESSION_TOKEN_RE = re.compile(
    r'window\.__HERMES_SESSION_TOKEN__="([^"]+)"',
)
_SESSION_HEADER = "X-Hermes-Session-Token"
_TOKEN_CACHE: dict[str, str] = {}


def fetch_hermes_session_token(
    dashboard_url: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Return the ephemeral Hermes dashboard API token.

    Hermes injects a per-process token into index.html on loopback binds.
    Set ``HERMES_DASHBOARD_SESSION_TOKEN`` to override (e.g. for scripting).
    """
    override = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "").strip()
    if override:
        return override
    root = dashboard_url.rstrip("/") + "/"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(root)
    if resp.status_code >= 400:
        detail = resp.text.strip() or resp.reason_phrase
        raise RuntimeError(
            f"Hermes dashboard token fetch failed ({resp.status_code}): {detail}"
        )
    match = _SESSION_TOKEN_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Hermes dashboard session token not found in index.html "
            "(OAuth-gated dashboard requires browser login)"
        )
    return match.group(1)


def clear_hermes_session_token_cache(dashboard_url: Optional[str] = None) -> None:
    """Drop cached session tokens (tests or after dashboard restart)."""
    if dashboard_url is None:
        _TOKEN_CACHE.clear()
        return
    _TOKEN_CACHE.pop(dashboard_url.rstrip("/"), None)


def _hermes_session_token(
    dashboard_url: str,
    *,
    refresh: bool = False,
    timeout: float = 10.0,
) -> str:
    key = dashboard_url.rstrip("/")
    if not refresh and key in _TOKEN_CACHE:
        return _TOKEN_CACHE[key]
    token = fetch_hermes_session_token(dashboard_url, timeout=timeout)
    _TOKEN_CACHE[key] = token
    return token


def _hermes_auth_headers(
    dashboard_url: str,
    *,
    refresh: bool = False,
    timeout: float = 10.0,
) -> dict[str, str]:
    token = _hermes_session_token(dashboard_url, refresh=refresh, timeout=timeout)
    return {_SESSION_HEADER: token}


def _hermes_api_call(
    client: httpx.Client,
    dashboard_url: str,
    method: str,
    url: str,
    *,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> httpx.Response:
    """Authenticated Hermes dashboard API request; refreshes token once on 401."""
    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(_hermes_auth_headers(dashboard_url))
    resp = client.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if resp.status_code != 401:
        return resp
    headers.update(_hermes_auth_headers(dashboard_url, refresh=True))
    return client.request(method, url, headers=headers, timeout=timeout, **kwargs)


_PROFILE_YAML_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_PROFILE_META_KEYS = (
    "display_name",
    "mascot",
    "description",
    "description_auto",
    "kanban_file",
    "project",
    "github",
)


def _load_profile_yaml_meta(meta_path: Path) -> dict[str, Any]:
    """Read profile.yaml with a simple mtime cache (hot path for large catalogs)."""
    key = str(meta_path)
    try:
        mtime = meta_path.stat().st_mtime
    except OSError:
        return {}
    cached = _PROFILE_YAML_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        import yaml  # local import — yaml is optional at module import time
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    _PROFILE_YAML_CACHE[key] = (mtime, loaded)
    if len(_PROFILE_YAML_CACHE) > 4000:
        for old_key in list(_PROFILE_YAML_CACHE.keys())[:500]:
            _PROFILE_YAML_CACHE.pop(old_key, None)
    return loaded


def _enrich_profile_with_local_meta(profile: dict[str, Any]) -> dict[str, Any]:
    """Merge fields from the profile's local profile.yaml (display_name, mascot,
    project workdir, kanban_file, …) into the slim row returned by the Hermes API."""
    path_raw = profile.get("path")
    if not path_raw:
        return profile
    meta_path = Path(str(path_raw)) / "profile.yaml"
    if not meta_path.is_file():
        return profile
    loaded = _load_profile_yaml_meta(meta_path)
    if not loaded:
        return profile
    if not isinstance(loaded, dict):
        return profile
    for key in _PROFILE_META_KEYS:
        if key not in loaded:
            continue
        if profile.get(key) in (None, "", []):
            profile[key] = loaded[key]
    return profile


def list_hermes_profiles(
    dashboard_url: str,
    *,
    timeout: float = 10.0,
    enrich_local_meta: bool = False,
) -> list[dict[str, Any]]:
    """Return profile rows from the Hermes dashboard API.

    When ``enrich_local_meta`` is True, merge each profile's local profile.yaml
    (slow for hundreds of profiles — use only when project/kanban metadata is needed).
    """
    api_base = _api_base(dashboard_url)
    with httpx.Client(timeout=timeout) as client:
        resp = _hermes_api_call(client, dashboard_url, "GET", f"{api_base}profiles")
        if resp.status_code >= 400:
            detail = resp.text.strip() or resp.reason_phrase
            raise RuntimeError(f"Hermes list failed ({resp.status_code}): {detail}")
        payload = resp.json()
        profiles = payload.get("profiles", [])
        if not isinstance(profiles, list):
            raise RuntimeError("Hermes list returned unexpected payload")
        rows = [p for p in profiles if isinstance(p, dict)]
        if not enrich_local_meta:
            return rows
        return [_enrich_profile_with_local_meta(p) for p in rows]


def _unique_name(
    client: httpx.Client,
    dashboard_url: str,
    api_base: str,
    base_name: str,
) -> str:
    resp = _hermes_api_call(
        client, dashboard_url, "GET", f"{api_base}profiles", timeout=10.0
    )
    resp.raise_for_status()
    existing = {
        p.get("name", "")
        for p in resp.json().get("profiles", [])
        if isinstance(p, dict)
    }
    if base_name not in existing:
        return base_name
    for i in range(2, 100):
        candidate = f"{base_name}-{i}"
        if len(candidate) > 64:
            candidate = f"{base_name[:58]}-{i}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"could not find unused name near {base_name!r}")


def create_hermes_agent(
    dashboard_url: str,
    spec: AgentSpec,
    *,
    clone_from_default: bool = True,
    profile_meta: Optional[dict[str, Any]] = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Create profile + SOUL via the Hermes dashboard HTTP API."""
    api_base = _api_base(dashboard_url)
    client_timeout = hermes_http_timeout(timeout)
    try:
        return _create_hermes_agent_impl(
            dashboard_url,
            spec,
            api_base=api_base,
            client_timeout=client_timeout,
            clone_from_default=clone_from_default,
            profile_meta=profile_meta,
            request_timeout=client_timeout,
        )
    except httpx.ReadTimeout as e:
        raise RuntimeError(
            f"Dashboard Hermes não respondeu a tempo após {timeout:.0f}s "
            f"(clone_from_default={clone_from_default}). "
            "Aumente hermes_agent_create.dashboard_api_timeout_seconds no config."
        ) from e


def _create_hermes_agent_impl(
    dashboard_url: str,
    spec: AgentSpec,
    *,
    api_base: str,
    client_timeout: httpx.Timeout,
    clone_from_default: bool,
    profile_meta: Optional[dict[str, Any]],
    request_timeout: httpx.Timeout,
) -> dict[str, Any]:
    with httpx.Client(timeout=client_timeout) as client:
        name = _unique_name(client, dashboard_url, api_base, spec.name)
        create_resp = _hermes_api_call(
            client,
            dashboard_url,
            "POST",
            f"{api_base}profiles",
            json={
                "name": name,
                "clone_from_default": clone_from_default,
            },
            timeout=request_timeout,
        )
        if create_resp.status_code >= 400:
            detail = create_resp.text.strip() or create_resp.reason_phrase
            raise RuntimeError(f"Hermes create failed ({create_resp.status_code}): {detail}")

        created = create_resp.json()
        profile_name = created.get("name", name)

        from .skills_mcp_bootstrap import ensure_soul_has_mcp_block

        soul_content = ensure_soul_has_mcp_block(spec.soul)
        soul_resp = _hermes_api_call(
            client,
            dashboard_url,
            "PUT",
            f"{api_base}profiles/{profile_name}/soul",
            json={"content": soul_content},
            timeout=request_timeout,
        )
        if soul_resp.status_code >= 400:
            detail = soul_resp.text.strip() or soul_resp.reason_phrase
            raise RuntimeError(f"Hermes SOUL update failed ({soul_resp.status_code}): {detail}")

        profile_path = created.get("path")
        if profile_path:
            try:
                from pathlib import Path

                import yaml

                from .skills_mcp_bootstrap import ensure_profile_tools_md

                ensure_profile_tools_md(Path(profile_path))

                meta_path = Path(profile_path) / "profile.yaml"
                meta: dict[str, Any] = {}
                if meta_path.is_file():
                    loaded = yaml.safe_load(meta_path.read_text()) or {}
                    if isinstance(loaded, dict):
                        meta = loaded
                meta["description"] = spec.description
                meta["description_auto"] = False
                if profile_meta:
                    meta.update(profile_meta)
                meta_path.write_text(
                    yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("could not write profile description for %s: %s", profile_name, e)

        return {
            "ok": True,
            "name": profile_name,
            "path": profile_path,
            "description": spec.description,
            "requested_name": spec.name,
            "renamed": profile_name != spec.name,
        }


def create_dev_agent_from_text(
    text: str,
    *,
    dashboard_url: str,
    agent_cfg: AgentConfig,
    create_cfg: HermesAgentCreateConfig,
    schedule: Optional[str] = None,
    skills: Optional[list[str]] = None,
    workdir: Optional[str] = None,
    schedule_enabled: Optional[bool] = None,
    stagger_fn: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """Create Hermes profile + optional cron job for a programming requirement."""
    catalog = _dev_skills_catalog(create_cfg)
    spec = parse_dev_agent_from_text(
        text,
        agent_cfg,
        create_cfg,
        schedule_override=schedule,
        skills_override=skills,
        workdir_override=workdir,
    )

    api_timeout = create_cfg.dashboard_api_timeout_seconds
    profile_result = create_hermes_agent(
        dashboard_url,
        spec,
        clone_from_default=create_cfg.clone_from_default,
        timeout=api_timeout,
    )
    profile_name = profile_result["name"]

    do_schedule = (
        create_cfg.schedule_enabled
        if schedule_enabled is None
        else schedule_enabled
    )
    cron_result: Optional[dict[str, Any]] = None
    if do_schedule:
        scheduled_schedule = spec.schedule
        if stagger_fn is not None:
            try:
                staggered = stagger_fn(scheduled_schedule)
            except Exception:
                staggered = scheduled_schedule
            if staggered:
                scheduled_schedule = staggered
                spec.schedule = staggered
        cron_result = create_hermes_cron_job(
            scheduled_schedule,
            spec.requirement_prompt,
            name=spec.description[:80] if spec.description else profile_name,
            profile=profile_name,
            skills=spec.skills,
            workdir=spec.workdir,
            accept_hooks=create_cfg.cron_accept_hooks,
            timeout_seconds=create_cfg.cron_timeout_seconds,
        )
        if not cron_result.get("ok"):
            profile_result["cron_error"] = cron_result.get("reason") or cron_result.get(
                "summary"
            )
            profile_result["ok"] = False
            profile_result["error"] = (
                f"Perfil {profile_name} criado, mas o cron falhou: "
                f"{profile_result.get('cron_error')}"
            )
            return profile_result

    skills_summary = format_skills_for_user(
        catalog.get("skills") or [],
        catalog.get("recommended") or list(DEFAULT_DEV_SKILL_SLUGS),
    )

    out: dict[str, Any] = {
        **profile_result,
        "description": spec.description,
        "soul_preview": spec.soul[:240] + ("…" if len(spec.soul) > 240 else ""),
        "requirement_preview": spec.requirement_prompt[:320]
        + ("…" if len(spec.requirement_prompt) > 320 else ""),
        "schedule": spec.schedule,
        "skills": spec.skills,
        "workdir": spec.workdir,
        "skills_catalog_summary": skills_summary,
        "skills_used": spec.skills,
    }
    if cron_result:
        out["cron"] = {
            "job_id": cron_result.get("job_id"),
            "schedule": spec.schedule,
            "profile": profile_name,
            "skills": spec.skills,
            "summary": cron_result.get("summary"),
        }
    return out


def create_role_from_text(
    text: str,
    *,
    dashboard_url: str,
    agent_cfg: AgentConfig,
    clone_from_default: bool = True,
    api_timeout_seconds: float = 180.0,
    role_preset: Optional[str] = None,
) -> dict[str, Any]:
    """Create a Hermes profile for a team role or any professional (no cron)."""
    spec = resolve_role_spec(text, role_preset, agent_cfg)
    result = create_hermes_agent(
        dashboard_url,
        spec,
        clone_from_default=clone_from_default,
        timeout=api_timeout_seconds,
    )
    result["description"] = spec.description
    result["soul_preview"] = spec.soul[:240] + ("…" if len(spec.soul) > 240 else "")
    result["mode"] = "role"
    return result


def create_agent_from_text(
    text: str,
    *,
    dashboard_url: str,
    agent_cfg: AgentConfig,
    create_cfg: Optional[HermesAgentCreateConfig] = None,
    clone_from_default: bool = True,
    schedule: Optional[str] = None,
    skills: Optional[list[str]] = None,
    workdir: Optional[str] = None,
    schedule_enabled: Optional[bool] = None,
    mode: str = "dev",
    stagger_fn: Optional[Callable[[str], str]] = None,
    role_preset: Optional[str] = None,
) -> dict[str, Any]:
    """Create agent; uses dev+cron flow when create_cfg is provided and mode is dev."""
    if mode == "role" or create_cfg is None:
        api_timeout = (
            create_cfg.dashboard_api_timeout_seconds if create_cfg is not None else 180.0
        )
        return create_role_from_text(
            text,
            dashboard_url=dashboard_url,
            agent_cfg=agent_cfg,
            role_preset=role_preset,
            clone_from_default=(
                clone_from_default
                if create_cfg is None
                else create_cfg.clone_from_default
            ),
            api_timeout_seconds=api_timeout,
        )
    if create_cfg is not None:
        return create_dev_agent_from_text(
            text,
            dashboard_url=dashboard_url,
            agent_cfg=agent_cfg,
            create_cfg=create_cfg,
            schedule=schedule,
            skills=skills,
            workdir=workdir,
            schedule_enabled=schedule_enabled,
            stagger_fn=stagger_fn,
        )

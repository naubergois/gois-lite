"""OpenAI Agents SDK (successor to Swarm) integration for gois.

Creates and runs multi-agent swarms on Hermes using the OpenAI Agents SDK.
Agents can be orchestrated with handoffs, tools, and guardrails, all running
through the DeepSeek (or any OpenAI-compatible) backend already configured.

Usage from the dashboard chat:
  "Cria um swarm com 3 agentes: pesquisador, redator e revisor"
  → Creates Hermes profiles for each agent with handoff wiring.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .agent_role_skills import (
    infer_role_preset_id,
    persist_swarm_profile_fields,
    role_skill_slugs,
    swarm_profile_meta_extras,
    verify_swarm_profile_on_disk,
)
from .config import AgentConfig, HermesAgentCreateConfig
from .hermes_cron import create_hermes_cron_job
from .hermes_profiles import (
    _PRESET_BY_ID,
    TEAM_ROLE_PRESETS,
    DevAgentSpec,
    _append_skills_to_soul,
    _normalize_name,
    create_hermes_agent,
    create_hermes_profile_filesystem,
    preset_agent_spec,
)
from .hermes_skills import (
    DEFAULT_DEV_SKILL_SLUGS,
    list_development_skills,
    normalize_skill_names,
    swarm_known_skill_names,
)
from .secrets_fallback import resolve_llm_api_key

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SwarmAgentSpec:
    """Specification for a single agent in the swarm."""

    name: str
    role: str
    instructions: str
    skills: list[str] = field(default_factory=list)
    handoff_to: list[str] = field(default_factory=list)
    role_preset_id: str = ""


@dataclass
class SwarmSpec:
    """Full swarm specification with multiple agents and topology."""

    name: str
    description: str
    agents: list[SwarmAgentSpec]
    entry_agent: str  # name of the triage/entry agent
    topology: str = "handoff"  # handoff | pipeline | broadcast


# ---------------------------------------------------------------------------
# LLM-assisted swarm design
# ---------------------------------------------------------------------------

_SWARM_DESIGN_SYSTEM = """\
Você é um arquiteto de swarms de agentes. O usuário vai descrever o que precisa e \
você deve devolver um JSON com a especificação do swarm.

Formato de saída (JSON puro, sem markdown):
{
  "name": "nome-do-swarm",
  "description": "descrição curta",
  "topology": "handoff",
  "entry_agent": "nome do agente principal/triagem",
  "agents": [
    {
      "name": "nome-slug",
      "role": "papel curto",
      "instructions": "instruções detalhadas para o agente",
      "skills": ["skill1", "skill2"],
      "handoff_to": ["outro-agente"]
    }
  ]
}

Regras:
- Nomes de agentes devem ser slugs (lowercase, hífens, sem espaços).
- O entry_agent faz a triagem e faz handoff para os especialistas.
- Cada agente deve ter instruções claras e em português.
- Se topology=pipeline, handoff_to é o próximo da sequência.
- Se topology=handoff, cada agente pode fazer handoff para qualquer outro listado em handoff_to.
- Máximo de 8 agentes por swarm.
- Sempre inclua um agente de triagem/coordenação como entry_agent.
- Cada agente DEVE ter "instructions" com passos concretos e entregáveis verificáveis.
- Cada agente DEVE listar 3–6 "skills" relevantes (slugs reais, ex.: plan, test-driven-development).
- Nunca crie agentes decorativos — todos executam trabalho real a cada cron.
"""


def _design_swarm_with_llm(
    text: str,
    *,
    agent_cfg: AgentConfig,
) -> SwarmSpec:
    """Use the configured LLM to design a swarm from natural language."""
    from .llm_gateway import make_client, trace_context

    api_key = resolve_llm_api_key(agent_cfg.api_key_env)
    if not api_key:
        raise ValueError(
            f"API key not found (env: {agent_cfg.api_key_env}). "
            "Configure DEEPSEEK_API_KEY ou OPENAI_API_KEY no .env."
        )

    client = make_client(
        api_key=api_key,
        base_url=getattr(agent_cfg, "base_url", None)
        or getattr(agent_cfg, "api_base", None),
        timeout=getattr(agent_cfg, "timeout_seconds", 90.0) or 90.0,
    )

    with trace_context(
        name="swarm.design",
        task_type="reasoning",
        model_label=agent_cfg.model,
        tags=["swarm", "design"],
    ):
        resp = client.chat.completions.create(
            model=agent_cfg.model,
            messages=[
                {"role": "system", "content": _SWARM_DESIGN_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=4096,
        )

    raw = (resp.choices[0].message.content or "").strip()
    # Strip markdown fences if the LLM wraps the JSON
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM não retornou JSON válido: {e}\n\nResposta:\n{raw[:500]}")

    agents_raw = data.get("agents") or []
    if not agents_raw:
        raise ValueError("O LLM não definiu nenhum agente no swarm.")
    if len(agents_raw) > 8:
        agents_raw = agents_raw[:8]

    agents = [
        SwarmAgentSpec(
            name=str(a.get("name") or f"agent-{i}").strip(),
            role=str(a.get("role") or "").strip(),
            instructions=str(a.get("instructions") or "").strip(),
            skills=[str(s).strip() for s in (a.get("skills") or [])],
            handoff_to=[str(h).strip() for h in (a.get("handoff_to") or [])],
        )
        for i, a in enumerate(agents_raw)
    ]

    entry = str(data.get("entry_agent") or agents[0].name).strip()

    return SwarmSpec(
        name=str(data.get("name") or "swarm").strip(),
        description=str(data.get("description") or text[:100]).strip(),
        agents=agents,
        entry_agent=entry,
        topology=str(data.get("topology") or "handoff").strip(),
    )


# Deterministic fallback swarm templates (used when the LLM design is
# unavailable, so swarm creation never hard-fails with "profile error").
_FALLBACK_KEYWORD_ROLES: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("backend", "api", "servidor", "banco"), "backend-dev", "Desenvolvedor backend",
     "Implementar e manter APIs, lógica de servidor e integração com banco de dados."),
    (("frontend", "ui", "interface", "react", "tela"), "frontend-dev", "Desenvolvedor frontend",
     "Implementar interface, componentes e integração com as APIs."),
    (("test", "qa", "qualidade", "revis"), "qa-engineer", "QA / Revisor",
     "Escrever testes, validar entregas e revisar qualidade do código."),
    (("seguran", "security", "vulnerab"), "security", "Segurança",
     "Avaliar riscos, revisar código por segurança e propor hardening."),
    (("dados", "data", "etl", "analytics"), "data-engineer", "Engenheiro de dados",
     "Construir pipelines de dados, modelar e garantir qualidade analítica."),
    (("pesquis", "research", "investig", "paper"), "ai-engineer", "Pesquisador",
     "Pesquisar fontes confiáveis, sintetizar e documentar achados."),
    (("deploy", "infra", "devops", "pipeline"), "devops", "DevOps",
     "Automatizar deploy, infraestrutura e observabilidade."),
)


def _fallback_swarm_design(text: str) -> SwarmSpec:
    """Build a sensible swarm without an LLM (coordinator + role specialists)."""
    blob = _norm_text(text)
    specialists: list[SwarmAgentSpec] = []
    seen: set[str] = set()
    for keywords, slug, role, instructions in _FALLBACK_KEYWORD_ROLES:
        if slug in seen:
            continue
        if any(kw in blob for kw in keywords):
            seen.add(slug)
            specialists.append(
                SwarmAgentSpec(name=slug, role=role, instructions=instructions)
            )
    if not specialists:
        # Generic software squad.
        specialists = [
            SwarmAgentSpec(
                name="backend-dev",
                role="Desenvolvedor",
                instructions="Implementar a solução pedida com código funcional e testes.",
            ),
            SwarmAgentSpec(
                name="qa-engineer",
                role="QA / Revisor",
                instructions="Testar, revisar e validar a entrega do desenvolvedor.",
            ),
        ]
    coordinator = SwarmAgentSpec(
        name="coordenador",
        role="Coordenador",
        instructions=(
            "Triar o objetivo do swarm, dividir em tarefas e delegar para os "
            "especialistas, consolidando as entregas."
        ),
        handoff_to=[a.name for a in specialists],
    )
    name = _slugify_swarm_name(text[:40] or "swarm") or "swarm"
    return SwarmSpec(
        name=name,
        description=text[:120] or "Swarm gerado automaticamente",
        agents=[coordinator, *specialists],
        entry_agent=coordinator.name,
        topology="handoff",
    )


# ---------------------------------------------------------------------------
# Swarm persistence (state file in .stack/)
# ---------------------------------------------------------------------------


def _swarm_state_dir() -> Path:
    from .local_paths import project_stack_root

    d = project_stack_root() / "swarms"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_swarm_name_for_team(
    team_id: str,
    *,
    hint_name: str = "",
) -> Optional[str]:
    """Resolve a swarm slug linked to *team_id* (file team_id, name match, or hint)."""
    tid = str(team_id or "").strip()
    if not tid:
        return None
    tid_lower = tid.lower()
    hint = str(hint_name or "").strip().lower()
    name_match: Optional[str] = None
    for swarm in load_swarms_full():
        name = str(swarm.get("name") or "").strip()
        if not name:
            continue
        if str(swarm.get("team_id") or "").strip() == tid:
            return name
        name_lower = name.lower()
        if hint and name_lower == hint:
            return name
        if tid_lower in name_lower and name_match is None:
            name_match = name
    return name_match


def _finalize_swarm_state(
    spec: SwarmSpec,
    agent_results: list[dict[str, Any]],
    hermes_profiles: list[str],
    *,
    team_id: str = "",
) -> Path:
    """Persist swarm state using actual Hermes profile slugs and role metadata."""
    name_map = _swarm_agent_name_map(spec)
    results_by_requested = {
        str(r.get("requested_name") or ""): r
        for r in agent_results
        if str(r.get("requested_name") or "").strip()
    }
    agents: list[dict[str, Any]] = []
    for agent_spec in spec.agents:
        result = results_by_requested.get(agent_spec.name, {})
        profile_name = str(
            result.get("profile_name")
            or name_map.get(agent_spec.name)
            or agent_spec.name
        ).strip()
        agents.append(
            {
                "name": profile_name,
                "role": agent_spec.role,
                "instructions": agent_spec.instructions,
                "skills": list(agent_spec.skills),
                "handoff_to": _map_handoff_targets(agent_spec.handoff_to, name_map),
                "display_name": agent_spec.role or agent_spec.name,
                "role_preset": str(result.get("role_preset") or agent_spec.role_preset_id or ""),
                "role_preset_label": str(result.get("role_preset_label") or ""),
                "role_category": str(result.get("role_category") or ""),
            }
        )
    entry_profile = name_map.get(spec.entry_agent, spec.entry_agent)
    entry_result = results_by_requested.get(spec.entry_agent)
    if entry_result:
        entry_profile = str(entry_result.get("profile_name") or entry_profile).strip()
    state: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "topology": spec.topology,
        "entry_agent": entry_profile,
        "agents": agents,
        "hermes_profiles": list(hermes_profiles),
        "created_at": time.time(),
    }
    if str(team_id or "").strip():
        state["team_id"] = str(team_id).strip()
    state = ensure_swarm_handoffs(state)
    state_file = _swarm_state_dir() / f"{spec.name}.json"
    _write_swarm_state_file(state_file, state)
    log.info("swarm state saved to %s (%d agents)", state_file, len(agents))
    return state_file


def list_swarms() -> list[dict[str, Any]]:
    """List all saved swarm definitions."""
    return [summarize_swarm(data) for data in load_swarms_full()]


def summarize_swarm(data: dict[str, Any]) -> dict[str, Any]:
    """Compact swarm summary for list endpoints."""
    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "topology": data.get("topology"),
        "agents_count": len(data.get("agents") or []),
        "entry_agent": data.get("entry_agent"),
        "team_id": data.get("team_id") or "",
        "created_at": data.get("created_at"),
    }


_swarms_full_cache: tuple[float, list[dict[str, Any]]] | None = None
_SWARMS_FULL_CACHE_TTL = 20.0  # seconds — matches swarm robots snapshot cache


def invalidate_swarms_full_cache() -> None:
    global _swarms_full_cache
    _swarms_full_cache = None


def load_swarms_full() -> list[dict[str, Any]]:
    """Load full swarm state from Mongo/Redis (authoritative) with disk legacy seed."""
    global _swarms_full_cache
    now = time.time()
    if _swarms_full_cache is not None and now - _swarms_full_cache[0] < _SWARMS_FULL_CACHE_TTL:
        return _swarms_full_cache[1]

    from .runtime_state import list_swarm_def_slugs

    state_dir = _swarm_state_dir()
    results: list[dict[str, Any]] = []

    def _consume(raw: dict[str, Any], slug: str, state_file: Path) -> None:
        raw = dict(raw)
        raw.setdefault("name", slug)
        before_agents = json.dumps(raw.get("agents") or [], sort_keys=True)
        data = ensure_swarm_handoffs(raw)
        after_agents = json.dumps(data.get("agents") or [], sort_keys=True)
        if before_agents != after_agents:
            _write_swarm_state_file(state_file, data)
        results.append(data)

    for slug in list_swarm_def_slugs():
        state_file = state_dir / f"{slug}.json"
        raw = _read_swarm_state_file(state_file)
        if isinstance(raw, dict):
            _consume(raw, slug, state_file)

    _swarms_full_cache = (now, results)
    return results


def load_swarm_state_by_name(swarm_name: str) -> Optional[dict[str, Any]]:
    """Load one swarm definition by slug (Mongo authoritative, disk legacy seed)."""
    slug = _slugify_swarm_name(str(swarm_name or "").strip())
    if not slug:
        return None
    state_file = _swarm_state_dir() / f"{slug}.json"
    raw = _read_swarm_state_file(state_file)
    if not isinstance(raw, dict):
        return None
    raw.setdefault("name", slug)
    return ensure_swarm_handoffs(raw)


def _slugify_swarm_name(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", str(name or "").strip().lower()).strip("-")
    return slug[:48] or "swarm-manual"


def _profile_slug_for_swarm_agent(swarm_name: str, agent_name: str) -> str:
    """Unique Hermes profile slug scoped to one swarm (avoids global role collisions)."""
    return _normalize_name(f"{swarm_name}-{agent_name}")


def _swarm_agent_name_map(spec: SwarmSpec) -> dict[str, str]:
    """Map logical agent ids (tech-lead) -> Hermes profile slugs (swarm-x-tech-lead)."""
    return {
        str(a.name): _profile_slug_for_swarm_agent(spec.name, a.name)
        for a in spec.agents
        if str(a.name or "").strip()
    }


def _map_handoff_targets(
    targets: list[str],
    name_map: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for raw in targets:
        key = str(raw or "").strip()
        if not key:
            continue
        mapped = name_map.get(key, key)
        if mapped not in out:
            out.append(mapped)
    return out


_VALID_TOPOLOGIES = (
    "handoff",
    "pipeline",
    "broadcast",
    "team",
    "graph",
    "mesh",
    "star",
)


def _normalize_topology(value: Optional[str], *, default: str = "handoff") -> str:
    topo = str(value or "").strip().lower()
    return topo if topo in _VALID_TOPOLOGIES else default


def infer_entry_handoff_targets(
    entry: str,
    members: list[str],
    topology: str,
) -> list[str]:
    """Return child agent slugs the entry/orchestrator should delegate to."""
    entry_n = _normalize_name(str(entry or ""))
    ordered = [
        m for m in members
        if _normalize_name(str(m or "")) and _normalize_name(str(m or "")) != entry_n
    ]
    if not ordered:
        return []
    topo = _normalize_topology(topology, default="handoff")
    if topo == "pipeline":
        return ordered[:1]
    return ordered


def ensure_swarm_handoffs(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure the entry agent lists delegation targets when topology implies a hub."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    profiles = [
        _normalize_name(str(p))
        for p in (out.get("hermes_profiles") or [])
        if str(p).strip()
    ]
    profiles = [p for p in profiles if p]
    if len(profiles) < 2:
        return out

    agents = [dict(a) for a in (out.get("agents") or []) if isinstance(a, dict)]
    agents_by_name = {
        _normalize_name(str(a.get("name") or "")): a
        for a in agents
        if _normalize_name(str(a.get("name") or ""))
    }
    for prof in profiles:
        if prof not in agents_by_name:
            stub: dict[str, Any] = {
                "name": prof,
                "role": "",
                "instructions": "",
                "skills": [],
                "handoff_to": [],
            }
            agents.append(stub)
            agents_by_name[prof] = stub

    entry = _normalize_name(str(out.get("entry_agent") or "")) or profiles[0]
    out["entry_agent"] = entry
    entry_agent = agents_by_name.get(entry)
    if entry_agent is None:
        out["agents"] = agents
        return out

    if entry_agent.get("handoff_to"):
        out["agents"] = agents
        return out

    children = infer_entry_handoff_targets(
        entry,
        profiles,
        str(out.get("topology") or "handoff"),
    )
    if children:
        entry_agent["handoff_to"] = children
    out["agents"] = agents
    return out


def _normalize_agent_entry(
    raw: Any,
    existing_by_name: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    prior = dict(existing_by_name.get(name) or {})
    entry: dict[str, Any] = prior
    entry["name"] = name
    for key in (
        "role",
        "instructions",
        "model_id",
        "mascot",
        "display_name",
        "role_preset",
        "role_preset_label",
        "role_category",
    ):
        if raw.get(key) is not None:
            entry[key] = raw[key]
    if "handoff_to" in raw:
        entry["handoff_to"] = [
            str(h).strip() for h in (raw.get("handoff_to") or []) if str(h).strip()
        ]
    elif "handoff_to" not in entry:
        entry["handoff_to"] = []
    entry.setdefault("skills", list(prior.get("skills") or []))
    if "requires_approval" in raw:
        entry["requires_approval"] = bool(raw.get("requires_approval"))
    return entry


def _normalize_graph(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    nodes: list[dict[str, Any]] = []
    for raw in value.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("id") or "").strip()
        if not node_id:
            continue
        node_type = str(raw.get("type") or "agent").strip().lower()
        if node_type not in {"agent", "condition", "approval"}:
            node_type = "agent"
        try:
            x = float(raw.get("x") or 0)
            y = float(raw.get("y") or 0)
        except (TypeError, ValueError):
            x, y = 0.0, 0.0
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "slug": str(raw.get("slug") or "").strip(),
                "label": str(raw.get("label") or "").strip(),
                "x": x,
                "y": y,
            }
        )
    node_ids = {n["id"] for n in nodes}
    edges: list[dict[str, Any]] = []
    for raw in value.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("from") or "").strip()
        dst = str(raw.get("to") or "").strip()
        if not src or not dst or src not in node_ids or dst not in node_ids:
            continue
        edge_id = str(raw.get("id") or f"{src}->{dst}").strip()
        edges.append(
            {
                "id": edge_id,
                "from": src,
                "to": dst,
                "label": str(raw.get("label") or "").strip(),
            }
        )
    return {"nodes": nodes, "edges": edges}


def create_swarm_definition(
    name: str,
    *,
    description: str = "",
    topology: str = "handoff",
    entry_agent: str = "",
    hermes_profiles: Optional[list[str]] = None,
    agents: Optional[list[Any]] = None,
    graph: Optional[dict[str, Any]] = None,
    team_id: str = "",
) -> dict[str, Any]:
    """Create an empty swarm state file (metadata only; no agents yet)."""
    raw = str(name or "").strip()
    if not raw:
        return {"ok": False, "error": "informe um nome para o swarm"}
    safe = _slugify_swarm_name(raw)
    state_file = _swarm_state_dir() / f"{safe}.json"
    if _swarm_state_exists(state_file):
        return {"ok": False, "error": f"swarm '{safe}' já existe"}
    profiles = [str(p).strip() for p in (hermes_profiles or []) if str(p).strip()]
    agent_rows: list[dict[str, Any]] = []
    if agents:
        for raw_agent in agents:
            entry = _normalize_agent_entry(raw_agent, {})
            if entry:
                agent_rows.append(entry)
                slug = str(entry.get("name") or "")
                if slug and slug not in profiles:
                    profiles.append(slug)
    data = {
        "name": safe,
        "description": str(description or "").strip() or f"Swarm {safe}",
        "topology": _normalize_topology(topology),
        "entry_agent": str(entry_agent or "").strip() or (profiles[0] if profiles else ""),
        "agents": agent_rows,
        "hermes_profiles": profiles,
        "created_at": time.time(),
    }
    if str(team_id or "").strip():
        data["team_id"] = str(team_id).strip()
    normalized_graph = _normalize_graph(graph) if graph is not None else None
    if normalized_graph is not None:
        data["graph"] = normalized_graph
    data = ensure_swarm_handoffs(data)
    _write_swarm_state_file(state_file, data)
    try:
        from .swarm_graph import ensure_graph_checkpoint_preview

        ensure_graph_checkpoint_preview(data)
    except Exception as exc:
        log.debug("swarm create: preview checkpoint skipped for %s: %s", safe, exc)
    log.info("created swarm definition %s", safe)
    return {"ok": True, "name": safe, "swarm": data}


def update_swarm_definition(
    swarm_name: str,
    *,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    topology: Optional[str] = None,
    entry_agent: Optional[str] = None,
    agents: Optional[list[Any]] = None,
    hermes_profiles: Optional[list[str]] = None,
    graph: Optional[dict[str, Any]] = None,
    team_id: Optional[str] = None,
) -> dict[str, Any]:
    """Edit swarm metadata and architecture. Supports renaming (moves the state file)."""
    safe = _slugify_swarm_name(swarm_name)
    state_dir = _swarm_state_dir()
    state_file = state_dir / f"{safe}.json"
    if not _swarm_state_exists(state_file):
        return {"ok": False, "error": f"swarm '{safe}' não encontrado"}
    data = _read_swarm_state_file(state_file)
    if data is None:
        return {"ok": False, "error": f"falha ao ler swarm '{safe}'"}

    if description is not None:
        data["description"] = str(description).strip()
    if team_id is not None:
        tid = str(team_id).strip()
        if tid:
            data["team_id"] = tid
        else:
            data.pop("team_id", None)
    if topology is not None:
        data["topology"] = _normalize_topology(
            topology, default=str(data.get("topology") or "handoff")
        )
    existing_agents = [
        a for a in (data.get("agents") or []) if isinstance(a, dict)
    ]
    existing_by_name = {
        str(a.get("name") or ""): a
        for a in existing_agents
        if str(a.get("name") or "")
    }

    if hermes_profiles is not None:
        data["hermes_profiles"] = [
            str(p).strip() for p in hermes_profiles if str(p).strip()
        ]

    if agents is not None:
        normalized: list[dict[str, Any]] = []
        for raw in agents:
            entry = _normalize_agent_entry(raw, existing_by_name)
            if entry:
                normalized.append(entry)
        data["agents"] = normalized
        if hermes_profiles is None:
            profiles = list(data.get("hermes_profiles") or [])
            for agent in normalized:
                slug = str(agent.get("name") or "")
                if slug and slug not in profiles:
                    profiles.append(slug)
            data["hermes_profiles"] = profiles

    if entry_agent is not None:
        data["entry_agent"] = str(entry_agent).strip()
    elif agents is not None or hermes_profiles is not None:
        entry = str(data.get("entry_agent") or "").strip()
        profiles = [str(p) for p in (data.get("hermes_profiles") or [])]
        if entry and profiles and entry not in profiles:
            data["entry_agent"] = profiles[0]

    if graph is not None:
        normalized_graph = _normalize_graph(graph)
        if normalized_graph is not None:
            data["graph"] = normalized_graph
        elif graph == {}:
            data.pop("graph", None)

    final_name = safe
    target_file = state_file
    if new_name is not None and str(new_name).strip():
        renamed = _slugify_swarm_name(new_name)
        if renamed != safe:
            new_file = state_dir / f"{renamed}.json"
            if _swarm_state_exists(new_file):
                return {"ok": False, "error": f"swarm '{renamed}' já existe"}
            final_name = renamed
            target_file = new_file
    data["name"] = final_name

    data = ensure_swarm_handoffs(data)
    _write_swarm_state_file(target_file, data)
    if target_file != state_file:
        _delete_swarm_state_file(state_file)
    log.info("updated swarm definition %s -> %s", safe, final_name)
    return {"ok": True, "name": final_name, "renamed": final_name != safe, "swarm": data}


def _swarm_runtime_dir(swarm_name: str) -> Path:
    """Per-swarm runtime artifacts (graph checkpoints, eval history, etc.)."""
    from .local_paths import project_stack_root

    return project_stack_root() / "swarms" / _slugify_swarm_name(swarm_name)


def delete_swarm_definition(swarm_name: str) -> dict[str, Any]:
    """Delete a swarm state file. Hermes profiles/robots are left intact."""
    safe = _slugify_swarm_name(swarm_name)
    state_file = _swarm_state_dir() / f"{safe}.json"
    if not _swarm_state_exists(state_file):
        return {"ok": False, "error": f"swarm '{safe}' não encontrado"}
    data = _read_swarm_state_file(state_file) or {}
    profiles = list(data.get("hermes_profiles") or []) if isinstance(data, dict) else []
    try:
        _delete_swarm_state_file(state_file)
        runtime_dir = _swarm_runtime_dir(safe)
        if runtime_dir.is_dir():
            shutil.rmtree(runtime_dir, ignore_errors=True)
    except OSError as exc:
        return {"ok": False, "error": f"falha ao excluir swarm '{safe}': {exc}"}
    log.info("deleted swarm definition %s", safe)
    return {"ok": True, "name": safe, "freed_profiles": profiles}


def register_robot_in_swarm(
    swarm_name: str,
    profile_name: str,
    *,
    role: str = "",
    instructions: str = "",
    model_id: str = "",
    mascot: str = "",
    display_name: str = "",
    role_preset: str = "",
    role_preset_label: str = "",
    role_category: str = "",
    handoff_to: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Append a robot to an existing swarm state file (or create one)."""
    safe_name = _slugify_swarm_name(swarm_name)
    state_dir = _swarm_state_dir()
    state_file = state_dir / f"{safe_name}.json"
    if _swarm_state_exists(state_file):
        data = _read_swarm_state_file(state_file) or {}
    else:
        data = {
            "name": safe_name,
            "description": f"Swarm {safe_name}",
            "topology": "handoff",
            "entry_agent": profile_name,
            "agents": [],
            "hermes_profiles": [],
            "created_at": time.time(),
        }
    agents = list(data.get("agents") or [])
    existing = next(
        (a for a in agents if isinstance(a, dict) and str(a.get("name") or "") == profile_name),
        None,
    )
    if existing is None:
        agents.append(
            {
                "name": profile_name,
                "role": role,
                "instructions": instructions,
                "skills": [],
                "handoff_to": list(handoff_to or []),
                "model_id": model_id,
                "mascot": mascot,
                "display_name": display_name,
                "role_preset": role_preset,
                "role_preset_label": role_preset_label,
                "role_category": role_category,
            }
        )
    else:
        # Keep best-role metadata fresh on re-registration.
        if role_preset:
            existing["role_preset"] = role_preset
            existing["role_preset_label"] = role_preset_label
            existing["role_category"] = role_category
        if handoff_to and not existing.get("handoff_to"):
            existing["handoff_to"] = list(handoff_to)
    data["agents"] = agents
    profiles = list(data.get("hermes_profiles") or [])
    if profile_name not in profiles:
        profiles.append(profile_name)
    data["hermes_profiles"] = profiles
    if not data.get("entry_agent"):
        data["entry_agent"] = profile_name
    data = ensure_swarm_handoffs(data)
    _write_swarm_state_file(state_file, data)
    log.info("registered robot %s in swarm %s", profile_name, safe_name)
    return data


def _read_swarm_state_file(state_file: Path) -> Optional[dict[str, Any]]:
    from .runtime_state import load_json, swarm_def_key

    raw = load_json(swarm_def_key(state_file.stem), state_file)
    return raw if isinstance(raw, dict) else None


def _swarm_state_exists(state_file: Path) -> bool:
    return _read_swarm_state_file(state_file) is not None or state_file.is_file()


def _write_swarm_state_file(state_file: Path, data: dict[str, Any]) -> None:
    from .runtime_state import save_json, swarm_def_key

    save_json(swarm_def_key(state_file.stem), data, state_file)
    invalidate_swarms_full_cache()


def _delete_swarm_state_file(state_file: Path) -> None:
    from .runtime_state import delete_json, swarm_def_key

    delete_json(swarm_def_key(state_file.stem), state_file)
    invalidate_swarms_full_cache()


def _find_swarm_files_for_profile(profile_name: str) -> list[tuple[Path, dict[str, Any]]]:
    slug = profile_name.strip()
    hits: list[tuple[Path, dict[str, Any]]] = []
    state_dir = _swarm_state_dir()
    from .runtime_state import list_swarm_def_slugs

    for swarm_slug in list_swarm_def_slugs():
        state_file = state_dir / f"{swarm_slug}.json"
        data = _read_swarm_state_file(state_file)
        if not isinstance(data, dict):
            continue
        profiles = [str(p) for p in (data.get("hermes_profiles") or [])]
        agents = [
            str(a.get("name") or "")
            for a in (data.get("agents") or [])
            if isinstance(a, dict)
        ]
        if slug in profiles or slug in agents:
            hits.append((state_file, data))
    return hits


def remove_robot_from_swarm(
    profile_name: str,
    *,
    swarm_name: Optional[str] = None,
) -> list[str]:
    """Remove a robot from swarm state file(s). Returns affected swarm names."""
    slug = str(profile_name or "").strip()
    if not slug:
        return []
    target = _slugify_swarm_name(swarm_name) if swarm_name else ""
    removed_from: list[str] = []
    for state_file, data in _find_swarm_files_for_profile(slug):
        swarm_id = str(data.get("name") or state_file.stem)
        if target and swarm_id != target:
            continue
        agents = [
            a for a in (data.get("agents") or [])
            if isinstance(a, dict) and str(a.get("name") or "") != slug
        ]
        profiles = [p for p in (data.get("hermes_profiles") or []) if str(p) != slug]
        data["agents"] = agents
        data["hermes_profiles"] = profiles
        if str(data.get("entry_agent") or "") == slug:
            data["entry_agent"] = str(agents[0].get("name") or "") if agents else ""
        _write_swarm_state_file(state_file, data)
        removed_from.append(swarm_id)
    return removed_from


def update_robot_in_swarm(
    profile_name: str,
    *,
    swarm_name: Optional[str] = None,
    role: Optional[str] = None,
    instructions: Optional[str] = None,
    model_id: Optional[str] = None,
    mascot: Optional[str] = None,
    display_name: Optional[str] = None,
) -> dict[str, Any]:
    """Update swarm registry metadata for an existing robot."""
    slug = str(profile_name or "").strip()
    if not slug:
        return {"ok": False, "error": "profile_name is required"}

    current_swarm = ""
    for _, data in _find_swarm_files_for_profile(slug):
        current_swarm = str(data.get("name") or "")
        break

    desired_swarm = _slugify_swarm_name(swarm_name) if swarm_name else current_swarm
    if desired_swarm and desired_swarm != current_swarm:
        old_meta: dict[str, Any] = {}
        for _, data in _find_swarm_files_for_profile(slug):
            for agent in data.get("agents") or []:
                if isinstance(agent, dict) and str(agent.get("name") or "") == slug:
                    old_meta = dict(agent)
                    break
        remove_robot_from_swarm(slug, swarm_name=current_swarm or None)
        register_robot_in_swarm(
            desired_swarm,
            slug,
            role=str(role or old_meta.get("role") or ""),
            instructions=str(instructions or old_meta.get("instructions") or ""),
            model_id=str(model_id or old_meta.get("model_id") or ""),
            mascot=str(mascot or old_meta.get("mascot") or ""),
            display_name=str(display_name or old_meta.get("display_name") or ""),
        )
        return {"ok": True, "swarm_name": desired_swarm, "moved": True}

    updated = False
    for state_file, data in _find_swarm_files_for_profile(slug):
        swarm_id = str(data.get("name") or state_file.stem)
        if desired_swarm and swarm_id != desired_swarm:
            continue
        agents = list(data.get("agents") or [])
        found = False
        for agent in agents:
            if not isinstance(agent, dict) or str(agent.get("name") or "") != slug:
                continue
            found = True
            if role is not None:
                agent["role"] = role
            if instructions is not None:
                agent["instructions"] = instructions
            if model_id is not None:
                agent["model_id"] = model_id
            if mascot is not None:
                agent["mascot"] = mascot
            if display_name is not None:
                agent["display_name"] = display_name
        if not found:
            register_robot_in_swarm(
                desired_swarm or swarm_id or "swarm-manual",
                slug,
                role=role or "",
                instructions=instructions or "",
                model_id=model_id or "",
                mascot=mascot or "",
                display_name=display_name or "",
            )
        else:
            data["agents"] = agents
            _write_swarm_state_file(state_file, data)
        updated = True
        return {"ok": True, "swarm_name": swarm_id, "moved": False}

    if desired_swarm:
        register_robot_in_swarm(
            desired_swarm,
            slug,
            role=role or "",
            instructions=instructions or "",
            model_id=model_id or "",
            mascot=mascot or "",
            display_name=display_name or "",
        )
        return {"ok": True, "swarm_name": desired_swarm, "moved": False}

    if updated:
        return {"ok": True, "swarm_name": current_swarm, "moved": False}
    return {"ok": False, "error": f"robot '{slug}' not found in swarms"}


# ---------------------------------------------------------------------------
# Best-role assignment — map each swarm agent to the best Hermes role preset
# ---------------------------------------------------------------------------

# Intent keywords → preferred preset ids (only ids that exist are used).
# These bias the match toward the canonical software-team roles so that each
# swarm agent gets a real, functional SOUL (not a generic stub).
_ROLE_INTENT_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("orquestr", "coorden", "triag", "lider", "lider", "lead", "gerente", "supervisor", "arquitet", "architect"), "tech-lead"),
    (("backend", "api", "fastapi", "django", "servidor", "banco de dados", "postgres", "sql"), "backend-dev"),
    (("frontend", "front-end", "react", "ui", "interface", "css", "componente", "tela"), "frontend-dev"),
    (("full-stack", "fullstack", "full stack", "end-to-end", "ponta a ponta"), "fullstack-dev"),
    (("mobile", "android", "ios", "react native", "app nativo"), "mobile-dev"),
    (("qa", "teste", "testes", "tester", "quality", "qualidade", "regress", "cobertura"), "qa-engineer"),
    (("revis", "review", "reviewer", "code review"), "qa-engineer"),
    (("devops", "sre", "infra", "deploy", "pipeline", "ci/cd", "observab", "kubernetes", "docker"), "devops"),
    (("seguran", "security", "vulnerab", "threat", "hardening", "cve", "auditor"), "security"),
    (("dados", "data", "etl", "pipeline de dados", "analytics", "modelagem"), "data-engineer"),
    (("produto", "product", "backlog", "user stor", "priorizac", "discovery"), "product-manager"),
    (("design", "ux", "ui/ux", "wireframe", "protótipo", "prototipo"), "ux-designer"),
    (("pesquis", "research", "investig", "fontes", "paper", "estado da arte", "benchmark"), "ai-engineer"),
    (("ia", "ai", "machine learning", "llm", "rag", "modelo", "agente"), "ai-engineer"),
)

_STOPWORDS = {
    "de", "do", "da", "das", "dos", "para", "por", "com", "sem", "e", "ou", "a",
    "o", "as", "os", "um", "uma", "que", "no", "na", "em", "agente", "papel",
    "swarm", "crie", "criar", "the", "and", "of", "to", "for", "with", "agent",
}


def _norm_text(value: str) -> str:
    text = "".join(
        c
        for c in unicodedata.normalize("NFKD", str(value or ""))
        if unicodedata.category(c) != "Mn"
    )
    return text.lower()


def _tokens(value: str) -> set[str]:
    return {
        tok
        for tok in re.split(r"[^a-z0-9]+", _norm_text(value))
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


# Preset keyword sets are derived from the static TEAM_ROLE_PRESETS catalogue
# (628 entries with long prompts). Tokenizing them is expensive and was being
# repeated on every match_role_preset() call (once per robot), which dominated
# the /swarm/robots snapshot build. Cache per preset id — the catalogue never
# changes within a process.
_PRESET_KEYWORDS_CACHE: dict[str, set[str]] = {}


def _preset_keywords(preset: dict[str, str]) -> set[str]:
    pid = str(preset.get("id") or "")
    cached = _PRESET_KEYWORDS_CACHE.get(pid)
    if cached is not None:
        return cached
    keywords = (
        {t for t in pid.split("-") if len(t) >= 3}
        | _tokens(str(preset.get("label") or ""))
        | _tokens(str(preset.get("prompt") or ""))
        | _tokens(str(preset.get("category") or ""))
    )
    if pid:
        _PRESET_KEYWORDS_CACHE[pid] = keywords
    return keywords


@functools.lru_cache(maxsize=512)
def match_role_preset(
    role: str,
    instructions: str = "",
    name: str = "",
) -> Optional[dict[str, str]]:
    """Pick the best Hermes role preset for a swarm agent (functional, not stub).

    Combines an intent keyword map (canonical software roles) with a general
    token-overlap score across the whole preset catalogue, so each agent is
    assigned the most fitting professional role automatically.
    """
    hint = infer_role_preset_id(name=name, role=role, instructions=instructions)
    if hint:
        preset = _PRESET_BY_ID.get(hint)
        if preset:
            return preset

    blob = _norm_text(" ".join([name, role, instructions]))
    agent_tokens = _tokens(" ".join([name, role, instructions]))

    best: Optional[dict[str, str]] = None
    best_score = 0.0

    # 1) Intent map gives a strong, curated signal.
    for keywords, preset_id in _ROLE_INTENT_MAP:
        preset = _PRESET_BY_ID.get(preset_id)
        if not preset:
            continue
        hits = sum(1 for kw in keywords if kw in blob)
        if hits:
            score = 5.0 + hits
            if score > best_score:
                best_score = score
                best = preset

    # 2) General catalogue overlap can override a weak/no intent match.
    for preset in TEAM_ROLE_PRESETS:
        overlap = agent_tokens & _preset_keywords(preset)
        if not overlap:
            continue
        score = float(len(overlap))
        # Slight boost when the preset id token appears verbatim in the role.
        if str(preset.get("id") or "").split("-")[0] in agent_tokens:
            score += 1.5
        if score > best_score:
            best_score = score
            best = preset

    return best if best_score >= 2.0 else None


def _role_preset_expertise(preset: dict[str, str]) -> str:
    """Return the functional body (Escopo/Como atuar) of a preset's SOUL."""
    try:
        spec = preset_agent_spec(preset)
    except Exception:
        return ""
    soul = spec.soul or ""
    # Drop the leading "# <label>\n\n" heading; keep the expertise sections.
    parts = soul.split("\n\n", 1)
    return parts[1].strip() if len(parts) == 2 else soul.strip()


# ---------------------------------------------------------------------------
# Swarm agent specs — deterministic (no second LLM parse)
# ---------------------------------------------------------------------------

_SWARM_BASE_SKILLS = (
    "plan",
    "writing-plans",
    "systematic-debugging",
    "subagent-driven-development",
)
_MIN_REQUIREMENT_LEN = 120
_MIN_SOUL_LEN = 200


def _swarm_known_skills(create_cfg: HermesAgentCreateConfig) -> set[str]:
    return swarm_known_skill_names(create_cfg, include_qclaw_chat=True)


def build_swarm_soul(
    agent: SwarmAgentSpec,
    swarm: SwarmSpec,
    *,
    is_entry: bool,
    role_preset: Optional[dict[str, str]] = None,
) -> str:
    lines = [
        f"# {agent.role or agent.name}",
        "",
        f"Agente do swarm **{swarm.name}** ({swarm.topology}).",
    ]
    if role_preset:
        label = str(role_preset.get("label") or role_preset.get("id") or "").strip()
        if label:
            lines.append(f"Papel atribuído (melhor encaixe): **{label}**.")
    if is_entry:
        lines.append("**Entry agent** — triagem, coordenação e **resumos consolidados do swarm**.")
    expertise = _role_preset_expertise(role_preset) if role_preset else ""
    if expertise:
        lines.extend(["", "## Especialidade", expertise])
    lines.extend(
        [
            "",
            "## Papel",
            agent.instructions or agent.role or "Executar tarefas do swarm.",
        ]
    )
    if agent.handoff_to:
        lines.extend(["", "## Handoffs", f"Pode delegar para: {', '.join(agent.handoff_to)}."])
    lines.extend(
        [
            "",
            "## Kanban",
            "Trabalhe nos cards atribuídos a este perfil. Atualize status e documente resultados em `notes`.",
        ]
    )
    lines.extend(
        [
            "",
            "## Execução",
            "A cada cron você DEVE produzir trabalho verificável — nunca responda só com plano.",
            "Consulte kanban/cards atribuídos, execute, teste quando aplicável, documente resultado.",
            "",
            "## Memória",
            "Ao terminar cada atividade, registre o que fez: passos, ficheiros, commits, bloqueios.",
        ]
    )
    if is_entry:
        lines.extend(
            [
                "",
                "## Resumo do swarm (líder)",
                "Como líder, a cada execução agendada produza um **resumo consolidado** do trabalho "
                "de todos os agentes do swarm: cards done/doing/review, notes, bloqueios e próximos passos.",
                "Publique em `swarm-resumos/resumo-<data>.md` (artefatos do time) e no card kanban `SWARM-RESUMO`.",
            ]
        )
    return "\n".join(lines)


def build_swarm_requirement_prompt(
    agent: SwarmAgentSpec,
    swarm: SwarmSpec,
    *,
    workdir: Optional[str],
    profile_name: str,
) -> str:
    handoff_block = ""
    if agent.handoff_to:
        handoff_block = (
            f"\n## Handoff\n"
            f"Prepare entrega para: {', '.join(agent.handoff_to)} "
            f"(notas claras para o próximo agente).\n"
        )
    wd = (workdir or "").strip()
    kanban_block = (
        f"1. Abra o kanban no workdir `{wd}` e liste cards atribuídos a `{profile_name}`.\n"
        if wd
        else f"1. Verifique tarefas/cards atribuídos a `{profile_name}` no projeto.\n"
    )
    entry_block = ""
    if agent.name == swarm.entry_agent:
        delegate_targets = list(agent.handoff_to or [])
        if not delegate_targets:
            delegate_targets = [
                a.name
                for a in swarm.agents
                if a.name and a.name != agent.name
            ]
        if delegate_targets:
            names = ", ".join(delegate_targets)
            entry_block = (
                "2. Como orquestrador/entry agent, triage o objetivo do swarm e "
                f"delegue trabalho concreto para: {names}.\n"
                "   - Crie ou atualize cards no kanban com assignees corretos (um por especialista).\n"
                "   - Não execute o trabalho dos especialistas; coordene, desbloqueie e consolide entregas.\n"
                "   - Se um filho estiver ocioso, atribua a próxima tarefa e registre o handoff nas notas.\n"
                "   - **Resumo obrigatório**: consolide o que cada agente do swarm fez (cards done/doing, "
                "notes, bloqueios) em markdown; grave em `swarm-resumos/` e atualize o card `SWARM-RESUMO`.\n"
            )
        else:
            entry_block = (
                "2. Como entry agent, triage o objetivo do swarm e escolha a próxima ação concreta.\n"
                "   - Produza resumo consolidado do trabalho de todos os agentes no kanban.\n"
            )
    else:
        entry_block = f"2. Execute seu papel: {agent.instructions or agent.role}.\n"

    return (
        f"Você é **{agent.role or profile_name}** no swarm `{swarm.name}`.\n"
        f"Objetivo do swarm: {swarm.description}\n\n"
        f"## Missão desta execução\n"
        f"{kanban_block}"
        f"{entry_block}"
        f"3. Implemente progresso real (código, pesquisa, texto, revisão — conforme seu papel).\n"
        f"4. Rode testes/validações quando fizer sentido.\n"
        f"5. Atualize kanban: mova card para `doing`/`done` e preencha `notes` com resultado.\n"
        f"{handoff_block}"
        f"## Entrega obrigatória\n"
        f"- Resumo markdown do que executou nesta run (não pode ser vazio).\n"
        f"- Liste ficheiros alterados, commits ou artefactos produzidos.\n"
        f"- Se bloqueado, explique o bloqueio e o próximo passo sugerido.\n"
    )


def build_swarm_dev_spec(
    agent: SwarmAgentSpec,
    swarm: SwarmSpec,
    *,
    create_cfg: HermesAgentCreateConfig,
    workdir: Optional[str],
    schedule: str,
    role_preset: Optional[dict[str, str]] = None,
) -> DevAgentSpec:
    known = _swarm_known_skills(create_cfg)
    defaults = list(create_cfg.default_skills or DEFAULT_DEV_SKILL_SLUGS)
    if role_preset is None:
        role_preset = match_role_preset(agent.role, agent.instructions, agent.name)
    role_preset_id = ""
    if role_preset:
        role_preset_id = str(role_preset.get("id") or "").strip()
    if not role_preset_id:
        role_preset_id = str(agent.role_preset_id or "").strip()
    if not role_preset_id and role_preset:
        role_preset_id = str(role_preset.get("id") or "").strip()
    if not role_preset_id:
        role_preset_id = infer_role_preset_id(
            name=agent.name,
            role=agent.role,
            instructions=agent.instructions,
        )
    role_skills = role_skill_slugs(role_preset_id) if role_preset_id else []
    skills = normalize_skill_names(
        list(agent.skills) + role_skills + list(_SWARM_BASE_SKILLS),
        known,
        defaults=defaults,
    )
    slug = _profile_slug_for_swarm_agent(swarm.name, agent.name)
    is_entry = agent.name == swarm.entry_agent
    soul = _append_skills_to_soul(
        build_swarm_soul(agent, swarm, is_entry=is_entry, role_preset=role_preset),
        skills,
    )
    requirement = build_swarm_requirement_prompt(
        agent,
        swarm,
        workdir=workdir,
        profile_name=slug,
    )
    return DevAgentSpec(
        name=slug,
        description=agent.role or f"Agente {slug} do swarm {swarm.name}",
        soul=soul,
        requirement_prompt=requirement,
        schedule=schedule,
        skills=skills,
        workdir=(workdir.strip() if workdir and workdir.strip() else None),
    )


def validate_swarm_agent_outcome(
    result: dict[str, Any],
    dev_spec: DevAgentSpec,
    *,
    cron_required: bool,
) -> dict[str, Any]:
    issues: list[str] = []
    if not result.get("ok", True):
        issues.append(str(result.get("error") or "falha ao criar perfil"))
    if cron_required and not result.get("cron"):
        issues.append("sem cron — agente não executará automaticamente (stub)")
    if len(dev_spec.requirement_prompt) < _MIN_REQUIREMENT_LEN:
        issues.append("requirement_prompt curto demais")
    if len(dev_spec.soul) < _MIN_SOUL_LEN:
        issues.append("SOUL curto demais")
    if len(dev_spec.skills) < 4:
        issues.append("skills insuficientes (< 4 relevantes ao papel)")
    if cron_required and not str(dev_spec.workdir or "").strip():
        issues.append("workdir ausente — kanban e integração ficam incompletos")
    preview = str(result.get("requirement_preview") or dev_spec.requirement_prompt)
    if len(preview.strip()) < 80:
        issues.append("prompt de execução vazio ou genérico")
    is_stub = cron_required and not result.get("cron")
    return {
        "valid": not issues,
        "is_stub": is_stub,
        "issues": issues,
        "skills_count": len(dev_spec.skills),
        "requirement_len": len(dev_spec.requirement_prompt),
    }


def _create_one_swarm_agent(
    agent_spec: SwarmAgentSpec,
    swarm: SwarmSpec,
    *,
    dashboard_url: str,
    create_cfg: HermesAgentCreateConfig,
    workdir: Optional[str],
    schedule: str,
    cron_required: bool,
    stagger_fn: Optional[Callable[[str], str]],
    model_id: Optional[str] = None,
    chat_cfg: Any = None,
) -> dict[str, Any]:
    role_preset = _resolve_agent_role_preset(agent_spec)
    dev_spec = build_swarm_dev_spec(
        agent_spec,
        swarm,
        create_cfg=create_cfg,
        workdir=workdir,
        schedule=schedule,
        role_preset=role_preset,
    )
    preset_id = str(role_preset.get("id") or "") if role_preset else ""
    preset_label = str(role_preset.get("label") or "") if role_preset else ""
    preset_category = str(role_preset.get("category") or "") if role_preset else ""
    profile_meta = {
        "display_name": agent_spec.role or agent_spec.name,
        "swarm_name": swarm.name,
        "swarm_role": agent_spec.role,
        **swarm_profile_meta_extras(
            workdir=dev_spec.workdir,
            skills=dev_spec.skills,
        ),
    }
    if preset_id:
        profile_meta["role_preset"] = preset_id
        profile_meta["role_preset_label"] = preset_label

    # Filesystem-first: this is robust even when the Hermes dashboard HTTP API
    # is unreachable (which previously caused "profile error" for every swarm).
    profile_result = _create_swarm_profile(
        dev_spec,
        dashboard_url=dashboard_url,
        create_cfg=create_cfg,
        profile_meta=profile_meta,
        model_id=model_id,
        chat_cfg=chat_cfg,
    )
    profile_name = str(profile_result.get("name") or dev_spec.name)
    persist_swarm_profile_fields(
        profile_name,
        workdir=dev_spec.workdir,
        skills=dev_spec.skills,
        role_preset=preset_id,
        role_preset_label=preset_label,
        swarm_name=swarm.name,
    )
    disk_verify = verify_swarm_profile_on_disk(
        profile_name,
        workdir=dev_spec.workdir,
    )
    out: dict[str, Any] = {
        **profile_result,
        "description": dev_spec.description,
        "soul_preview": dev_spec.soul[:240] + ("…" if len(dev_spec.soul) > 240 else ""),
        "requirement_preview": dev_spec.requirement_prompt[:320]
        + ("…" if len(dev_spec.requirement_prompt) > 320 else ""),
        "schedule": dev_spec.schedule,
        "skills": dev_spec.skills,
        "workdir": dev_spec.workdir,
        "role_preset": preset_id,
        "role_preset_label": preset_label,
        "role_category": preset_category,
        "profile_disk_verify": disk_verify,
    }

    if not disk_verify.get("ok"):
        out["ok"] = False
        out["error"] = (
            out.get("error")
            or f"perfil {profile_name} incompleto no disco: "
            + "; ".join(disk_verify.get("issues") or [])
        )

    if cron_required:
        scheduled = dev_spec.schedule
        if stagger_fn is not None:
            try:
                staggered = stagger_fn(scheduled)
                if staggered:
                    scheduled = staggered
            except Exception:
                pass
        cron_result = create_hermes_cron_job(
            scheduled,
            dev_spec.requirement_prompt,
            name=dev_spec.description[:80] or profile_name,
            profile=profile_name,
            skills=dev_spec.skills,
            workdir=dev_spec.workdir,
            accept_hooks=create_cfg.cron_accept_hooks,
            timeout_seconds=create_cfg.cron_timeout_seconds,
        )
        out["cron"] = cron_result
        if not cron_result.get("ok"):
            out["ok"] = False
            out["error"] = (
                f"Perfil {profile_name} criado, mas cron falhou: "
                f"{cron_result.get('reason') or cron_result.get('summary')}"
            )

    validation = validate_swarm_agent_outcome(out, dev_spec, cron_required=cron_required)
    out["validation"] = validation
    out["requested_name"] = agent_spec.name
    out["profile_name"] = profile_name

    name_map = _swarm_agent_name_map(swarm)
    register_robot_in_swarm(
        swarm.name,
        profile_name,
        role=agent_spec.role,
        instructions=agent_spec.instructions,
        model_id=str(model_id or ""),
        display_name=agent_spec.role or agent_spec.name,
        role_preset=preset_id,
        role_preset_label=preset_label,
        role_category=preset_category,
        handoff_to=_map_handoff_targets(agent_spec.handoff_to, name_map),
    )
    return out


def _create_swarm_profile(
    dev_spec: DevAgentSpec,
    *,
    dashboard_url: str,
    create_cfg: HermesAgentCreateConfig,
    profile_meta: dict[str, Any],
    model_id: Optional[str] = None,
    chat_cfg: Any = None,
) -> dict[str, Any]:
    """Create the Hermes profile on disk first, falling back to the HTTP API.

    The dashboard API requires a healthy Hermes web server; when it is down the
    whole swarm used to fail with a profile error. Cloning the template profile
    on disk works offline and is picked up by Hermes exactly like API profiles.
    """
    template_profile = create_cfg.seed_role_catalog_template_profile
    resolved_model = str(model_id or "").strip() or None
    try:
        return create_hermes_profile_filesystem(
            dev_spec,
            profile_meta=profile_meta,
            template_profile=template_profile,
            model_id=resolved_model,
            chat_cfg=chat_cfg,
        )
    except Exception as fs_err:
        log.warning(
            "swarm profile filesystem create failed (%s); trying dashboard API",
            fs_err,
        )
        result = create_hermes_agent(
            dashboard_url,
            dev_spec,
            clone_from_default=create_cfg.clone_from_default,
            profile_meta=profile_meta,
            timeout=create_cfg.dashboard_api_timeout_seconds,
        )
        if resolved_model and result.get("name"):
            from .hermes_profile_model import write_profile_model_default

            profile_name = str(result.get("name") or dev_spec.name)
            if not write_profile_model_default(
                profile_name, resolved_model, chat_cfg=chat_cfg
            ):
                log.warning(
                    "swarm profile %s created via API but model %s was not saved",
                    profile_name,
                    resolved_model,
                )
        return result


# ---------------------------------------------------------------------------
# Create swarm: design + create Hermes profiles
# ---------------------------------------------------------------------------


def _resolve_agent_role_preset(
    agent_spec: SwarmAgentSpec,
) -> Optional[dict[str, str]]:
    """Resolve Hermes role preset for a swarm agent (explicit id or heuristic)."""
    explicit = str(agent_spec.role_preset_id or "").strip()
    hint = infer_role_preset_id(
        name=agent_spec.name,
        role=agent_spec.role,
        instructions=agent_spec.instructions,
        explicit=explicit,
    )
    if hint:
        preset = _PRESET_BY_ID.get(hint)
        if preset:
            return preset
    return match_role_preset(agent_spec.role, agent_spec.instructions, agent_spec.name)


def preset_to_swarm_spec(
    preset: dict[str, Any],
    *,
    name_override: Optional[str] = None,
    agent_counts: Optional[dict[str, Any]] = None,
) -> SwarmSpec:
    """Build a SwarmSpec from a SWARM_PRESETS catalog entry."""
    from .swarm_presets import apply_agent_counts_to_preset

    effective = (
        apply_agent_counts_to_preset(preset, agent_counts)
        if agent_counts is not None
        else preset
    )
    raw_name = str(name_override or effective.get("id") or "swarm").strip()
    agents_raw = effective.get("agents") or []
    if not agents_raw:
        raise ValueError("preset de swarm sem agentes")
    shared_skills = [
        str(s).strip()
        for s in (effective.get("skills") or [])
        if str(s).strip()
    ]
    agents = [
        SwarmAgentSpec(
            name=str(a.get("name") or f"agent-{i}").strip(),
            role=str(a.get("role") or "").strip(),
            instructions=str(a.get("instructions") or "").strip(),
            skills=shared_skills
            + [str(s).strip() for s in (a.get("skills") or []) if str(s).strip()],
            handoff_to=[
                str(h).strip() for h in (a.get("handoff_to") or []) if str(h).strip()
            ],
            role_preset_id=str(a.get("role_preset") or a.get("name") or "").strip(),
        )
        for i, a in enumerate(agents_raw)
        if isinstance(a, dict)
    ]
    if not agents:
        raise ValueError("preset de swarm sem agentes válidos")
    entry = str(effective.get("entry_agent") or agents[0].name).strip()
    return SwarmSpec(
        name=_slugify_swarm_name(raw_name),
        description=str(
            effective.get("description") or effective.get("name") or raw_name
        ).strip(),
        agents=agents,
        entry_agent=entry,
        topology=str(effective.get("topology") or "handoff").strip(),
    )


def _execute_swarm_spec(
    spec: SwarmSpec,
    *,
    dashboard_url: str,
    create_cfg: HermesAgentCreateConfig,
    workdir: Optional[str],
    schedule: str,
    schedule_enabled: Optional[bool],
    stagger_fn: Optional[Callable[[str], str]],
    preset_id: str = "",
    design_fallback: bool = False,
    model_id: Optional[str] = None,
    chat_cfg: Any = None,
    team_id: str = "",
) -> dict[str, Any]:
    """Create Hermes profiles + cron jobs for every agent in *spec*."""
    cron_required = (
        create_cfg.schedule_enabled if schedule_enabled is None else schedule_enabled
    )
    workdir = str(workdir or create_cfg.default_workdir or "").strip() or None
    if cron_required and not workdir:
        return {
            "ok": False,
            "execution_ready": False,
            "error": (
                "workdir é obrigatório para swarms executáveis — informe workdir "
                "ou configure hermes_agent_create.default_workdir"
            ),
            "swarm_name": spec.name,
        }

    created_profiles: list[str] = []
    agent_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stub_agents: list[str] = []

    for agent_spec in spec.agents:
        try:
            result = _create_one_swarm_agent(
                agent_spec,
                spec,
                dashboard_url=dashboard_url,
                create_cfg=create_cfg,
                workdir=workdir,
                schedule=schedule,
                cron_required=cron_required,
                stagger_fn=stagger_fn,
                model_id=model_id,
                chat_cfg=chat_cfg,
            )
            profile_name = str(result.get("profile_name") or result.get("name") or "")
            validation = result.get("validation") or {}
            cron_ok = False
            cron_raw = result.get("cron")
            if isinstance(cron_raw, dict):
                cron_ok = bool(cron_raw.get("ok"))
            agent_results.append(
                {
                    "requested_name": agent_spec.name,
                    "profile_name": profile_name,
                    "role": agent_spec.role,
                    "role_preset": str(result.get("role_preset") or ""),
                    "role_preset_label": str(result.get("role_preset_label") or ""),
                    "role_category": str(result.get("role_category") or ""),
                    "cron_ok": cron_ok,
                    "validation": validation,
                }
            )
            if validation.get("is_stub"):
                stub_agents.append(profile_name or agent_spec.name)
                errors.append(
                    {
                        "agent": agent_spec.name,
                        "profile": profile_name,
                        "error": "agente criado como stub (sem cron executável)",
                        "issues": validation.get("issues"),
                    }
                )
            elif not validation.get("valid"):
                errors.append(
                    {
                        "agent": agent_spec.name,
                        "profile": profile_name,
                        "error": "agente criado com avisos de qualidade",
                        "issues": validation.get("issues"),
                    }
                )
            if profile_name and result.get("ok", True) and not validation.get("is_stub"):
                if validation.get("valid") and (result.get("profile_disk_verify") or {}).get(
                    "ok", True
                ):
                    created_profiles.append(profile_name)
                    log.info("created executable swarm agent: %s", profile_name)
                elif not validation.get("valid"):
                    stub_agents.append(profile_name or agent_spec.name)
        except Exception as e:
            log.warning("failed to create swarm agent %s: %s", agent_spec.name, e)
            errors.append({"agent": agent_spec.name, "error": str(e)})

    state_file = _finalize_swarm_state(
        spec, agent_results, created_profiles, team_id=team_id
    )
    name_map = _swarm_agent_name_map(spec)
    entry_profile = name_map.get(spec.entry_agent, spec.entry_agent)
    for row in agent_results:
        if row.get("requested_name") == spec.entry_agent and row.get("profile_name"):
            entry_profile = str(row["profile_name"])
            break
    execution_ready = len(stub_agents) == 0 and len(created_profiles) == len(spec.agents)

    out: dict[str, Any] = {
        "ok": execution_ready,
        "execution_ready": execution_ready,
        "design_fallback": design_fallback,
        "swarm_name": spec.name,
        "description": spec.description,
        "topology": spec.topology,
        "entry_agent": entry_profile,
        "agents_requested": len(spec.agents),
        "agents_created": len(created_profiles),
        "agents_executable": len(created_profiles),
        "stub_agents": stub_agents or None,
        "hermes_profiles": created_profiles,
        "agent_results": agent_results,
        "errors": errors if errors else None,
        "state_file": str(state_file),
        "schedule": schedule,
        "cron_enabled": cron_required,
        "workdir": workdir,
        "agents_spec": [_agent_spec_summary(a) for a in spec.agents],
    }
    if preset_id:
        out["preset_id"] = preset_id
    if model_id:
        out["model_id"] = str(model_id).strip()
    if str(team_id or "").strip():
        out["team_id"] = str(team_id).strip()
    return out


def create_swarm_from_preset(
    preset_id: str,
    *,
    dashboard_url: str,
    create_cfg: HermesAgentCreateConfig,
    workdir: Optional[str] = None,
    schedule: Optional[str] = None,
    schedule_enabled: Optional[bool] = None,
    stagger_fn: Optional[Callable[[str], str]] = None,
    name_override: Optional[str] = None,
    agent_counts: Optional[dict[str, Any]] = None,
    model_id: Optional[str] = None,
    chat_cfg: Any = None,
    team_id: str = "",
) -> dict[str, Any]:
    """Create a full executable swarm from a ready-made SWARM_PRESETS template."""
    from .swarm_presets import get_swarm_preset

    pid = str(preset_id or "").strip()
    if not pid:
        raise ValueError("preset_id é obrigatório")
    preset = get_swarm_preset(pid)
    spec = preset_to_swarm_spec(
        preset,
        name_override=name_override,
        agent_counts=agent_counts,
    )

    state_file = _swarm_state_dir() / f"{spec.name}.json"
    if _swarm_state_exists(state_file):
        return {
            "ok": False,
            "error": f"swarm '{spec.name}' já existe",
            "swarm_name": spec.name,
            "preset_id": pid,
        }

    resolved_schedule = (schedule or create_cfg.default_schedule or "every 24h").strip()
    resolved_workdir = workdir
    if not resolved_workdir and create_cfg.default_workdir:
        resolved_workdir = create_cfg.default_workdir.strip() or None

    log.info(
        "creating swarm from preset %s -> %s (%d agents)",
        pid,
        spec.name,
        len(spec.agents),
    )
    result = _execute_swarm_spec(
        spec,
        dashboard_url=dashboard_url,
        create_cfg=create_cfg,
        workdir=resolved_workdir,
        schedule=resolved_schedule,
        schedule_enabled=schedule_enabled,
        stagger_fn=stagger_fn,
        preset_id=pid,
        model_id=model_id,
        chat_cfg=chat_cfg,
        team_id=team_id,
    )
    if agent_counts is not None:
        result["agent_counts"] = agent_counts
    return result


def create_swarm(
    text: str,
    *,
    dashboard_url: str,
    agent_cfg: AgentConfig,
    create_cfg: HermesAgentCreateConfig,
    workdir: Optional[str] = None,
    schedule: Optional[str] = None,
    schedule_enabled: Optional[bool] = None,
    stagger_fn: Optional[Callable[[str], str]] = None,
    model_id: Optional[str] = None,
    chat_cfg: Any = None,
    team_id: str = "",
) -> dict[str, Any]:
    """Design a swarm from natural language and create executable Hermes agents.

    Each agent gets SOUL + cron with a concrete requirement_prompt (not a stub).
    Returns validation per agent and flags any stub that slipped through.
    """
    log.info("designing swarm from text: %s", text[:100])
    design_fallback = False
    try:
        spec = _design_swarm_with_llm(text, agent_cfg=agent_cfg)
    except Exception as design_err:
        log.warning(
            "LLM swarm design failed (%s); using deterministic fallback design",
            design_err,
        )
        spec = _fallback_swarm_design(text)
        design_fallback = True
    log.info(
        "swarm designed: %s with %d agents (topology=%s, fallback=%s)",
        spec.name,
        len(spec.agents),
        spec.topology,
        design_fallback,
    )

    resolved_schedule = (schedule or create_cfg.default_schedule or "every 24h").strip()
    resolved_workdir = workdir
    if not resolved_workdir and create_cfg.default_workdir:
        resolved_workdir = create_cfg.default_workdir.strip() or None

    return _execute_swarm_spec(
        spec,
        dashboard_url=dashboard_url,
        create_cfg=create_cfg,
        workdir=resolved_workdir,
        schedule=resolved_schedule,
        schedule_enabled=schedule_enabled,
        stagger_fn=stagger_fn,
        design_fallback=design_fallback,
        model_id=model_id,
        chat_cfg=chat_cfg,
        team_id=team_id,
    )


def _agent_spec_summary(agent: SwarmAgentSpec) -> dict[str, Any]:
    preset = _resolve_agent_role_preset(agent)
    return {
        "name": agent.name,
        "role": agent.role,
        "handoff_to": agent.handoff_to,
        "skills": agent.skills,
        "role_preset": str(preset.get("id") or "") if preset else "",
        "role_preset_label": str(preset.get("label") or "") if preset else "",
        "role_category": str(preset.get("category") or "") if preset else "",
    }

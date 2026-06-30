"""Hermes agents bound to a project (local folder or GitHub) and a kanban board."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import AgentConfig, HermesAgentCreateConfig
from .hermes_cron import create_hermes_cron_job
from .hermes_profiles import (
    TEAM_ROLE_PRESETS,
    AgentSpec,
    DevAgentSpec,
    create_hermes_agent,
    preset_default_skills,
    resolve_role_spec,
)
from .hermes_mascots import (
    HERMES_MASCOTS,
    list_mascots,
    mascot_label,
    normalize_mascot,
)
from .hermes_skills import (
    DEFAULT_DEV_SKILL_SLUGS,
    format_skills_for_user,
    list_development_skills,
    normalize_skill_names,
)

_GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s#?]+)/([^/\s#?]+?)(?:\.git)?(?:/|$|\?|#)",
    re.IGNORECASE,
)
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$",
    re.IGNORECASE,
)

@dataclass
class ProjectAgentRequest:
    """User input for a project-bound Hermes agent."""

    display_name: str
    role_text: str
    project_source: str  # "local" | "github"
    mascot: str = "gois"
    role_preset: Optional[str] = None
    github_url: Optional[str] = None
    local_path: Optional[str] = None
    github_branch: str = "main"
    schedule: Optional[str] = None
    skills: list[str] = field(default_factory=list)
    slug_hint: Optional[str] = None
    task_id: Optional[str] = None
    # WhatsApp group JID do time — agente envia notificações para o grupo
    whatsapp_group_jid: Optional[str] = None


@dataclass
class ProjectAgentSpec(DevAgentSpec):
    display_name: str = ""
    mascot: str = "gois"
    project_source: str = "local"
    github_repo: Optional[str] = None
    github_clone_url: Optional[str] = None
    github_branch: str = "main"
    local_path: Optional[str] = None
    kanban_relative: str = "kanban.yaml"
    role_preset: Optional[str] = None
    task_id: Optional[str] = None
    # WhatsApp group JID do time (para notificações proativas do agente)
    whatsapp_group_jid: Optional[str] = None


def list_role_presets() -> list[dict[str, str]]:
    return list(TEAM_ROLE_PRESETS)


def parse_github_repo(url: str) -> tuple[str, str]:
    """Return (owner, repo) from a GitHub HTTPS or SSH URL."""
    text = url.strip()
    m = _GITHUB_RE.match(text) or _GITHUB_SSH_RE.match(text)
    if not m:
        raise ValueError(
            "github_url inválida — use https://github.com/owner/repo ou git@github.com:owner/repo"
        )
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def github_clone_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def expand_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def projects_root(create_cfg: HermesAgentCreateConfig) -> Path:
    return expand_path(create_cfg.projects_root)


def resolve_kanban_path(workdir: Path, create_cfg: HermesAgentCreateConfig) -> Path:
    for rel in create_cfg.kanban_filenames:
        candidate = workdir / rel
        if candidate.is_file():
            return candidate
    return workdir / create_cfg.kanban_filenames[0]


def resolve_workdir(
    profile_slug: str,
    req: ProjectAgentRequest,
    create_cfg: HermesAgentCreateConfig,
) -> Path:
    if req.project_source == "local":
        if not req.local_path or not str(req.local_path).strip():
            raise ValueError("local_path é obrigatório quando project_source=local")
        return expand_path(str(req.local_path))

    if req.project_source != "github":
        raise ValueError('project_source deve ser "local" ou "github"')

    if not req.github_url:
        raise ValueError("github_url é obrigatório quando project_source=github")

    owner, repo = parse_github_repo(req.github_url)
    root = projects_root(create_cfg) / profile_slug / "repo"
    return root


def slug_from_display_name(display_name: str, hint: Optional[str] = None) -> str:
    if hint and hint.strip():
        from .hermes_profiles import _normalize_name

        return _normalize_name(hint)
    from .hermes_profiles import _normalize_name

    return _normalize_name(display_name)


def load_kanban_tasks(
    kanban_path: Path,
    *,
    assignee: str,
    display_name: str,
) -> list[dict[str, Any]]:
    """Return open tasks assigned to this agent."""
    from .hermes_kanban import load_kanban

    board = load_kanban(kanban_path)
    if not board.get("exists") and not board.get("tasks"):
        return []

    tasks_raw = board.get("tasks") or []
    if not isinstance(tasks_raw, list):
        return []

    aliases = {
        assignee.strip().lower(),
        display_name.strip().lower(),
    }
    done_cols = {"done", "concluído", "concluido", "closed", "feito"}

    open_tasks: list[dict[str, Any]] = []
    for row in tasks_raw:
        if not isinstance(row, dict):
            continue
        col = str(row.get("column") or row.get("status") or "todo").strip().lower()
        if col in done_cols:
            continue
        assignees = row.get("assignees") or row.get("assignee") or []
        if isinstance(assignees, str):
            assignees = [assignees]
        if not isinstance(assignees, list):
            continue
        normalized = {str(a).strip().lower() for a in assignees if str(a).strip()}
        if not normalized.intersection(aliases):
            continue
        open_tasks.append(row)
    return open_tasks


def _require_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for kanban task assignment") from exc
    return yaml


def ensure_task_assignee(
    kanban_path: Path,
    *,
    task_id: str,
    assignee: str,
    display_name: str,
) -> dict[str, Any]:
    """Ensure a specific kanban task exists and is assigned to the new agent."""
    from .hermes_kanban import load_kanban, save_kanban

    board = load_kanban(kanban_path)
    if not board.get("exists") and not board.get("tasks"):
        raise ValueError(f"kanban não encontrado: {kanban_path}")
    tasks = board.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("kanban inválido: campo tasks ausente")
    for idx, row in enumerate(tasks):
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "").strip() != task_id:
            continue
        assignees = row.get("assignees") or row.get("assignee") or []
        if isinstance(assignees, str):
            assignees = [assignees]
        if not isinstance(assignees, list):
            assignees = []
        normalized = {str(a).strip().lower() for a in assignees if str(a).strip()}
        changed = False
        if assignee.strip().lower() not in normalized:
            assignees.append(assignee)
            changed = True
        display_alias = display_name.strip()
        if display_alias and display_alias.lower() not in normalized:
            assignees.append(display_alias)
            changed = True
        if changed:
            row["assignees"] = assignees
            tasks[idx] = row
            board["tasks"] = tasks
            save_kanban(kanban_path, board)
        return row
    raise ValueError(f"tarefa kanban não encontrada: {task_id}")


def build_project_soul(spec: ProjectAgentSpec) -> str:
    label = mascot_label(spec.mascot)
    lines = [
        f"# {spec.display_name}",
        "",
        f"**Mascote:** {label} (`{spec.mascot}`)",
        "",
        spec.soul.strip(),
        "",
        "## Projeto",
    ]
    if spec.project_source == "github" and spec.github_repo:
        lines.extend([
            f"- Repositório: `{spec.github_repo}`",
            f"- Clone: `{spec.github_clone_url}` (branch `{spec.github_branch}`)",
            f"- Workdir do agente: `{spec.workdir}`",
        ])
    else:
        lines.append(f"- Pasta local: `{spec.local_path or spec.workdir}`")

    lines.extend([
        "",
        "## Kanban",
        f"- Ficheiro: `{spec.kanban_relative}` (relativo ao workdir)",
        f"- Tarefas atribuídas a: `{spec.name}` ou `{spec.display_name}`",
        "- Colunas típicas: `todo`, `doing`, `done`",
    ])
    if spec.task_id:
        lines.append(f"- Tarefa designada: `{spec.task_id}`")
    return "\n".join(lines)


def build_project_requirement_prompt(spec: ProjectAgentSpec) -> str:
    kanban_hint = spec.kanban_relative
    assignee_note = f"`{spec.name}` ou `{spec.display_name}`"

    clone_block = ""
    if spec.project_source == "github" and spec.github_clone_url:
        clone_block = f"""
## Repositório GitHub
1. Workdir: `{spec.workdir}`.
2. Se a pasta não existir ou estiver vazia (sem `.git`), clone:
   `git clone --branch {spec.github_branch} {spec.github_clone_url} "{spec.workdir}"`
   (ou `git clone` + `git checkout {spec.github_branch}` se a branch não existir no remoto).
3. Se já existir `.git`, faça `git pull --rebase` (sem force-push).
"""
    else:
        clone_block = f"""
## Projeto local
1. Workdir: `{spec.workdir}`.
2. Não mova o projeto para outro sítio — trabalhe apenas dentro desta pasta.
"""

    attachment_steps = (
        "Se slides, materiais complementares ou outros entregáveis em arquivo tiverem sido "
        "gerados, anexe-os ao card da tarefa (skill qclaw-kanban-cards-attach-content / "
        "`qclaw_kanban_attach_upload`) e liste cada anexo em `notes`."
    )
    kanban_steps = f"""1. Abra `{kanban_hint}` no workdir (crie a partir do exemplo do projeto se não existir).
2. Liste tarefas em que `assignees` inclui {assignee_note} e a coluna **não** é `done`.
3. Escolha **uma** tarefa prioritária (menor `priority` numérico, ou a primeira em `todo`).
4. Atualize o kanban: mova a tarefa para `doing` antes de codar.
5. Implemente no repositório (código, testes, docs conforme a tarefa).
6. Ao concluir: {attachment_steps}
7. Mova a tarefa para coluna `done`, preencha `completed_at` (ISO) e escreva em `notes` um comentário com o resultado (resumo + hash do commit + anexos).
8. Se não houver tarefas atribuídas, reporte no resumo e não invente trabalho."""
    if spec.task_id:
        kanban_steps = f"""1. Abra `{kanban_hint}` no workdir.
2. Trabalhe **somente** na tarefa `{spec.task_id}` até ela estar em `done`.
3. Garanta que `{spec.task_id}` está atribuída a {assignee_note}; se faltar, atualize `assignees`.
4. Se `{spec.task_id}` não estiver em `done`, mova para `doing` e implemente progresso real.
5. Ao concluir: {attachment_steps}
6. Mova `{spec.task_id}` para `done`, preencha `completed_at` (ISO) e registre em `notes` um comentário do resultado (resumo + hash do commit + anexos).
7. Se `{spec.task_id}` já estiver `done`, responda apenas com status de conclusão e não inicie nova tarefa."""

    return f"""Você é **{spec.display_name}**, agente Hermes com o papel descrito no SOUL.

{clone_block}

## Kanban (obrigatório)
{kanban_steps}

## Git
1. `git status` — confirme branch correta (`{spec.github_branch}` ou a da tarefa).
2. Commit com mensagem clara (referencie id da tarefa kanban se existir).
3. `git push` para o remoto configurado (sem `--force` em `main`/`master`).

## Entrega
- Resumo curto: tarefa escolhida, ficheiros alterados, testes corridos, commit e push (ou bloqueio).
"""


def build_project_agent_spec(
    req: ProjectAgentRequest,
    *,
    role_spec: AgentSpec,
    create_cfg: HermesAgentCreateConfig,
    profile_slug: Optional[str] = None,
) -> ProjectAgentSpec:
    slug = profile_slug or slug_from_display_name(req.display_name, req.slug_hint)
    workdir_path = resolve_workdir(slug, req, create_cfg)
    if workdir_path.is_dir():
        kanban_rel = str(resolve_kanban_path(workdir_path, create_cfg).relative_to(workdir_path))
    else:
        kanban_rel = create_cfg.kanban_filenames[0]

    github_repo: Optional[str] = None
    github_clone: Optional[str] = None
    if req.project_source == "github" and req.github_url:
        owner, repo = parse_github_repo(req.github_url)
        github_repo = f"{owner}/{repo}"
        github_clone = github_clone_url(owner, repo)

    catalog = list_development_skills(categories=create_cfg.skill_categories)
    known = {str(r["name"]) for r in catalog.get("skills") or [] if r.get("name")}
    defaults = list(create_cfg.default_skills or DEFAULT_DEV_SKILL_SLUGS)
    preset_skills = preset_default_skills(req.role_preset or "")
    skills = normalize_skill_names(
        req.skills or preset_skills,
        known,
        defaults=defaults,
    )

    schedule = (req.schedule or create_cfg.default_schedule).strip()
    base_soul = role_spec.soul.strip()

    spec = ProjectAgentSpec(
        name=slug,
        description=role_spec.description,
        soul=base_soul,
        requirement_prompt="",
        schedule=schedule,
        skills=skills,
        workdir=str(workdir_path),
        display_name=req.display_name.strip(),
        mascot=normalize_mascot(req.mascot),
        project_source=req.project_source,
        github_repo=github_repo,
        github_clone_url=github_clone,
        github_branch=req.github_branch.strip() or create_cfg.default_github_branch,
        local_path=str(workdir_path) if req.project_source == "local" else None,
        kanban_relative=kanban_rel,
        role_preset=req.role_preset,
        task_id=req.task_id,
        whatsapp_group_jid=req.whatsapp_group_jid,
    )
    spec.soul = build_project_soul(spec)
    spec.requirement_prompt = build_project_requirement_prompt(spec)
    return spec


def profile_meta_from_spec(spec: ProjectAgentSpec) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "display_name": spec.display_name,
        "mascot": spec.mascot,
        "project": {
            "source": spec.project_source,
            "github": spec.github_repo,
            "github_branch": spec.github_branch,
            "local_path": spec.local_path,
            "workdir": spec.workdir,
        },
        "kanban_file": spec.kanban_relative,
    }
    if spec.role_preset:
        meta["role_preset"] = spec.role_preset
    if spec.task_id:
        meta["task_id"] = spec.task_id
    if spec.whatsapp_group_jid:
        meta["whatsapp_group_jid"] = spec.whatsapp_group_jid
    return meta


def validate_project_request(payload: dict[str, Any]) -> ProjectAgentRequest:
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("display_name é obrigatório")

    role_text = str(payload.get("role_text") or payload.get("text") or "").strip()
    role_preset = payload.get("role_preset")
    if role_preset is not None:
        role_preset = str(role_preset).strip() or None

    if not role_text and role_preset:
        for preset in TEAM_ROLE_PRESETS:
            if preset["id"] == role_preset:
                role_text = preset["prompt"]
                break

    if not role_text:
        raise ValueError("role_text ou role_preset é obrigatório")

    project_source = str(payload.get("project_source") or "local").strip().lower()
    if project_source not in ("local", "github"):
        raise ValueError('project_source deve ser "local" ou "github"')

    github_url = payload.get("github_url")
    local_path = payload.get("local_path")
    if project_source == "github" and not github_url:
        raise ValueError("github_url é obrigatório para projeto GitHub")
    if project_source == "local" and not local_path:
        raise ValueError("local_path é obrigatório para projeto local")

    skills_raw = payload.get("skills")
    skills: list[str] = []
    if isinstance(skills_raw, list):
        skills = [str(s).strip() for s in skills_raw if str(s).strip()]
    elif isinstance(skills_raw, str) and skills_raw.strip():
        skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

    schedule = payload.get("schedule")
    branch = str(payload.get("github_branch") or "").strip()
    task_id = str(payload.get("task_id") or payload.get("kanban_task_id") or "").strip() or None

    return ProjectAgentRequest(
        display_name=display_name,
        role_text=role_text,
        project_source=project_source,
        mascot=normalize_mascot(payload.get("mascot")),
        role_preset=role_preset,
        github_url=str(github_url).strip() if github_url else None,
        local_path=str(local_path).strip() if local_path else None,
        github_branch=branch,
        schedule=str(schedule).strip() if schedule else None,
        skills=skills,
        slug_hint=str(payload.get("slug") or payload.get("name") or "").strip() or None,
        task_id=task_id,
        whatsapp_group_jid=str(payload.get("whatsapp_group_jid") or "").strip() or None,
    )


def create_project_agent(
    payload: dict[str, Any],
    *,
    dashboard_url: str,
    agent_cfg: AgentConfig,
    create_cfg: HermesAgentCreateConfig,
    schedule_enabled: Optional[bool] = None,
    forced_workdir: Optional[str] = None,
    forced_kanban_file: Optional[str] = None,
) -> dict[str, Any]:
    """Create Hermes profile + cron for a named agent on a project with kanban."""
    req = validate_project_request(payload)
    role_spec = resolve_role_spec(req.role_text, req.role_preset, agent_cfg)
    project_spec = build_project_agent_spec(req, role_spec=role_spec, create_cfg=create_cfg)

    profile_result = create_hermes_agent(
        dashboard_url,
        project_spec,
        clone_from_default=create_cfg.clone_from_default,
        profile_meta=profile_meta_from_spec(project_spec),
        timeout=create_cfg.dashboard_api_timeout_seconds,
    )
    profile_name = profile_result["name"]
    project_spec.name = profile_name
    project_spec.workdir = str(resolve_workdir(profile_name, req, create_cfg))
    if forced_workdir and str(forced_workdir).strip():
        project_spec.workdir = str(Path(str(forced_workdir).strip()).expanduser().resolve())

    workdir_path = Path(project_spec.workdir)
    kanban_path = resolve_kanban_path(workdir_path, create_cfg)
    if forced_kanban_file and str(forced_kanban_file).strip():
        candidate = (workdir_path / str(forced_kanban_file).strip()).resolve()
        if str(candidate).startswith(str(workdir_path)):
            kanban_path = candidate
            try:
                project_spec.kanban_relative = str(kanban_path.relative_to(workdir_path))
            except ValueError:
                pass
    designated_task: Optional[dict[str, Any]] = None
    if req.task_id:
        designated_task = ensure_task_assignee(
            kanban_path,
            task_id=req.task_id,
            assignee=profile_name,
            display_name=project_spec.display_name,
        )
        project_spec.task_id = req.task_id
        project_spec.soul = build_project_soul(project_spec)
        project_spec.requirement_prompt = build_project_requirement_prompt(project_spec)
    open_tasks = load_kanban_tasks(
        kanban_path,
        assignee=profile_name,
        display_name=project_spec.display_name,
    )

    do_schedule = (
        create_cfg.schedule_enabled
        if schedule_enabled is None
        else schedule_enabled
    )
    cron_result: Optional[dict[str, Any]] = None
    if do_schedule:
        cron_result = create_hermes_cron_job(
            project_spec.schedule,
            project_spec.requirement_prompt,
            name=f"{project_spec.display_name} — kanban",
            profile=profile_name,
            skills=project_spec.skills,
            workdir=project_spec.workdir,
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

    catalog = list_development_skills(categories=create_cfg.skill_categories)
    out: dict[str, Any] = {
        **profile_result,
        "mode": "project",
        "display_name": project_spec.display_name,
        "mascot": project_spec.mascot,
        "description": project_spec.description,
        "project_source": project_spec.project_source,
        "github_repo": project_spec.github_repo,
        "workdir": project_spec.workdir,
        "kanban_file": project_spec.kanban_relative,
        "kanban_tasks_open": len(open_tasks),
        "kanban_tasks_preview": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "column": t.get("column"),
            }
            for t in open_tasks[:5]
        ],
        "schedule": project_spec.schedule,
        "task_id": project_spec.task_id,
        "designated_task": {
            "id": designated_task.get("id"),
            "title": designated_task.get("title"),
            "column": designated_task.get("column") or designated_task.get("status"),
        }
        if designated_task
        else None,
        "skills": project_spec.skills,
        "soul_preview": project_spec.soul[:280]
        + ("…" if len(project_spec.soul) > 280 else ""),
        "requirement_preview": project_spec.requirement_prompt[:320]
        + ("…" if len(project_spec.requirement_prompt) > 320 else ""),
        "skills_catalog_summary": format_skills_for_user(
            catalog.get("skills") or [],
            catalog.get("recommended") or list(DEFAULT_DEV_SKILL_SLUGS),
        ),
        "whatsapp_group_jid": project_spec.whatsapp_group_jid,
    }
    if cron_result:
        out["cron"] = {
            "job_id": cron_result.get("job_id"),
            "schedule": project_spec.schedule,
            "profile": profile_name,
            "skills": project_spec.skills,
            "summary": cron_result.get("summary"),
        }
    return out

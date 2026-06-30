"""Hermes kanban project/board HTTP handlers."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

_ENSURE_DEFAULT_TEAM_TTL = 60.0
_ensure_default_team_ts: dict[str, float] = {}
_ensure_default_team_last_cleanup = 0.0


def _cleanup_expired_ttl_entries(now: float, ttl: float = _ENSURE_DEFAULT_TEAM_TTL, cleanup_interval: float = 3600.0):
    """Periodically clean up expired entries to prevent unbounded dict growth."""
    global _ensure_default_team_last_cleanup
    if now - _ensure_default_team_last_cleanup < cleanup_interval:
        return
    _ensure_default_team_last_cleanup = now
    expired_keys = [k for k, ts in _ensure_default_team_ts.items() if now >= ts]
    for k in expired_keys:
        del _ensure_default_team_ts[k]

from .accounts import UserRecord
from .hermes_kanban import (
    apply_kanban_action,
    get_board,
    kanban_file_for_project,
    projects_from_profiles,
    team_agents_from_profiles,
)
from .hermes_profiles import list_hermes_profiles_filesystem

log = logging.getLogger(__name__)


class MonitorHermesKanbanHandlersMixin:
    def handle_hermes_kanban_projects(
        self, user: Optional[UserRecord] = None, *, quick: bool = False
    ) -> dict:
        """List project workdirs available for Kanban."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if actor is not None:
            _now = time.time()
            _cleanup_expired_ttl_entries(_now)
            if _now >= _ensure_default_team_ts.get(actor.id, 0.0):
                self.accounts.ensure_default_kanban_team(actor.id)
                _ensure_default_team_ts[actor.id] = _now + _ENSURE_DEFAULT_TEAM_TTL
        dashboard_url = self._hermes_dashboard_url()

        local_projects: list[dict[str, Any]] = []
        local_agents: list[dict[str, str]] = []
        if actor is not None:
            local_projects, local_agents = self._fallback_kanban_projects_for_user(actor)

        # No Hermes gateway configured: serve local team boards only.
        if quick or dashboard_url is None:
            return {
                "ok": True,
                "projects": local_projects,
                "agents": local_agents,
                "dashboard_url": dashboard_url,
                "local_only": True,
            }

        enrich = self.cfg.hermes_agent_create.profiles_enrich_local_meta
        hermes_timeout = 8.0
        profiles: list[dict[str, Any]] = []
        hermes_source = "api"
        try:
            if not enrich:
                disk_rows = list_hermes_profiles_filesystem()
                if disk_rows:
                    profiles = list(disk_rows)
                    hermes_source = "filesystem"
            if not profiles:
                profiles = self._cached_hermes_profiles(
                    dashboard_url,
                    actor,
                    timeout=hermes_timeout,
                    enrich_local_meta=enrich,
                )
                hermes_source = "api"
            if actor is not None and self.cfg.auth.enabled:
                profiles = self._filter_profiles_for_user(profiles, actor)
            projects = projects_from_profiles(profiles, self.cfg.hermes_agent_create)
            agents = team_agents_from_profiles(profiles)
            projects = self._merge_kanban_projects(projects, local_projects)
            agents = self._merge_kanban_agents(agents, local_agents)
            out: dict[str, Any] = {
                "ok": True,
                "projects": projects,
                "agents": agents,
                "dashboard_url": dashboard_url,
            }
            if hermes_source == "filesystem":
                out["source"] = "filesystem"
            return out
        except Exception as e:
            log.warning("Hermes kanban projects failed: %s", e)
            if local_projects:
                return {
                    "ok": True,
                    "projects": local_projects,
                    "agents": local_agents,
                    "dashboard_url": dashboard_url,
                    "fallback": True,
                    "warning": f"Hermes indisponível: {type(e).__name__}: {e}",
                }
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _prepare_kanban_payload(
        self, payload: dict[str, Any], actor: Optional[UserRecord]
    ) -> dict[str, Any]:
        """Resolve team boards to on-disk paths and ensure kanban.yaml exists."""
        prepared = dict(payload)
        if actor is None:
            return prepared
        team_id = str(prepared.get("team_id") or "").strip()
        if not team_id:
            workdir = str(prepared.get("workdir") or "").strip()
            if workdir:
                try:
                    resolved = Path(workdir).expanduser().resolve()
                except OSError:
                    resolved = None
                if resolved is not None:
                    teams = (
                        self.accounts.list_all_teams()
                        if getattr(actor, "is_admin", False)
                        else self.accounts.list_teams(actor.id)
                    )
                    for team in teams:
                        try:
                            if resolved == self.accounts.team_workdir(team).resolve():
                                team_id = team.id
                                break
                        except OSError:
                            continue
        if not team_id:
            return prepared
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError:
            return prepared
        prepared["team_id"] = team.id
        wd = self.accounts.team_dir(team.id)
        wd.mkdir(parents=True, exist_ok=True)
        self.accounts._ensure_team_kanban(team.id)
        kp = self.accounts.team_kanban_path(team.id)
        prepared["workdir"] = str(wd.resolve())
        prepared["kanban_file"] = kp.name
        return prepared

    def handle_hermes_kanban_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        query = self._prepare_kanban_payload(query, actor)
        workdir = str(query.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        kanban_file = query.get("kanban_file")
        if kanban_file is not None and not isinstance(kanban_file, str):
            return {"ok": False, "error": "kanban_file must be a string"}
        light = str(query.get("light") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            board = get_board(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=(kanban_file or "").strip() or None,
                light=light,
            )
            result = {"ok": True, **board}
            result = self._enrich_kanban_board_for_ui(
                result, query, actor, light=light
            )
            return result
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _enrich_kanban_board_for_ui(
        self,
        board: dict[str, Any],
        query: dict,
        actor: Optional[UserRecord],
        *,
        light: bool = False,
    ) -> dict[str, Any]:
        """Add team metadata and live execution hints for the kanban UI."""
        from .swarm_robots import enrich_kanban_board_execution

        team_id = str(query.get("team_id") or "").strip()
        if team_id and actor is not None:
            try:
                team = self.accounts.get_team(team_id, actor.id)
                members: list[dict] = []
                member_ids = self.accounts.list_team_members(team_id, actor.id)
                for mid in member_ids:
                    u = self.accounts.get_user_by_id(mid)
                    entry: dict = {"user_id": mid, "is_owner": mid == team.owner_id}
                    if u:
                        entry["username"] = u.username
                        entry["email"] = u.email
                    members.append(entry)
                board["members"] = members
                contacts = list(team.contacts) if team.contacts else []
                board["contacts"] = contacts
                board["profile_slugs"] = list(team.profile_slugs)
                if team.swarm_name:
                    board["swarm_name"] = team.swarm_name
            except Exception:
                pass
        if light:
            return board
        try:
            board = enrich_kanban_board_execution(
                self,
                board,
                team_id=team_id,
            )
        except Exception as exc:
            log.debug("kanban execution enrich failed: %s", exc)
        return board

    def handle_hermes_kanban_action(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(payload, actor)
        workdir = str(payload.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        try:
            board = apply_kanban_action(workdir, self.cfg.hermes_agent_create, payload)
            action = str(payload.get("action") or "").strip().lower()
            if action == "move_task":
                column = str(payload.get("column") or "").strip().lower()
                task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
                if column in ("doing", "review") and task_id:
                    task = next(
                        (
                            t
                            for t in (board.get("tasks") or [])
                            if str(t.get("id") or "").strip() == task_id
                        ),
                        None,
                    )
                    if isinstance(task, dict):
                        board_info = {
                            "workdir": workdir,
                            "kanban_file": payload.get("kanban_file"),
                            "team_id": str(payload.get("team_id") or "").strip(),
                        }
                        self._auto_enqueue_doing_task(
                            board_info,
                            task,
                            reason="move_to_doing",
                        )
            return {"ok": True, **board}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_task_history(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Return the robot execution history (Mongo) for a single kanban card."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(dict(query or {}), actor)
        workdir = str(payload.get("workdir") or "").strip()
        task_id = str(payload.get("task_id") or query.get("task_id") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        try:
            from . import kanban_execution_history as history

            limit_raw = str(query.get("limit") or "50").strip()
            try:
                limit = max(1, min(200, int(limit_raw)))
            except ValueError:
                limit = 50
            executions = history.list_task_executions(
                workdir=workdir, task_id=task_id, limit=limit
            )
            return {"ok": True, "task_id": task_id, "executions": executions}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_pm_analysis(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Project Manager agent: analyze deliverables and benefits of a task card."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}

        task_id = str(payload.get("task_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        column = str(payload.get("column") or "").strip()
        description = str(payload.get("description") or "").strip()
        notes = str(payload.get("notes") or "").strip()
        assignees = payload.get("assignees") or []
        skills = payload.get("skills") or []
        completed_at = str(payload.get("completed_at") or "").strip()
        created_at = str(payload.get("created_at") or "").strip()

        if not task_id:
            return {"ok": False, "error": "task_id is required"}

        # Build PM analysis based on task data
        deliverables: list[str] = []
        benefits: list[str] = []
        next_steps: list[str] = []
        risks: list[str] = []

        # Analyze deliverables from title, description, and notes
        if title:
            deliverables.append(f"Tarefa: {title}")
        if description:
            for line in description.splitlines():
                line = line.strip().lstrip("-•*").strip()
                if line and len(line) > 3:
                    deliverables.append(line)
        if notes:
            for line in notes.splitlines():
                line = line.strip().lstrip("-•*").strip()
                if line and len(line) > 3:
                    deliverables.append(f"Resultado: {line}")

        # Determine benefits based on task context
        if column == "done":
            benefits.append("Tarefa concluída com sucesso — incremento de valor entregue ao projeto")
            if completed_at:
                benefits.append(f"Entrega registrada em {completed_at} — rastreabilidade garantida")
        elif column == "doing":
            benefits.append("Tarefa em progresso ativo — redução do lead time")
        else:
            benefits.append("Tarefa planejada — visibilidade de backlog para o time")

        if skills:
            skills_str = ", ".join(skills) if isinstance(skills, list) else str(skills)
            benefits.append(f"Competências aplicadas: {skills_str}")

        if assignees:
            assignees_str = ", ".join(assignees) if isinstance(assignees, list) else str(assignees)
            benefits.append(f"Responsabilidade clara — atribuído a: {assignees_str}")

        if description:
            benefits.append("Critérios de aceite documentados — qualidade assegurada")

        # Suggest next steps
        if column == "done":
            next_steps.append("Validar entrega com stakeholders")
            next_steps.append("Documentar lições aprendidas")
            next_steps.append("Verificar se há tarefas dependentes bloqueadas")
        elif column == "doing":
            next_steps.append("Monitorar progresso e remover impedimentos")
            next_steps.append("Verificar se o prazo estimado está sendo cumprido")
            next_steps.append("Comunicar avanço ao time")
        else:
            next_steps.append("Priorizar tarefa no próximo sprint/ciclo")
            next_steps.append("Refinar critérios de aceite se necessário")
            next_steps.append("Atribuir responsável para execução")

        # Identify risks
        if column == "doing" and not notes:
            risks.append("Tarefa em andamento sem registro de progresso parcial")
        if not description:
            risks.append("Sem descrição detalhada — risco de escopo indefinido")
        if not assignees:
            risks.append("Sem responsável atribuído — risco de tarefa órfã")
        if column == "todo" and not skills:
            risks.append("Skills não definidas — pode atrasar na alocação de recurso")

        # Build summary
        status_label = {"done": "concluída", "doing": "em andamento"}.get(column, "pendente")
        summary = (
            f"Como Gerente de Projetos, avalio que a tarefa {task_id} "
            f"({title or 'sem título'}) está {status_label}. "
        )
        if column == "done":
            summary += (
                "A entrega foi registrada com sucesso no kanban. "
                "Recomendo validação com os stakeholders e documentação do aprendizado."
            )
        elif column == "doing":
            summary += (
                "O trabalho está em andamento. "
                "É importante monitorar impedimentos e garantir comunicação contínua."
            )
        else:
            summary += (
                "A tarefa aguarda início. "
                "Recomendo priorização e refinamento antes de iniciar a execução."
            )

        return {
            "ok": True,
            "task_id": task_id,
            "deliverables": deliverables,
            "benefits": benefits,
            "next_steps": next_steps,
            "risks": risks,
            "summary": summary,
        }

    def handle_hermes_kanban_requirements(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Requirements agent: analyze a card and ask the team what is missing.

        Generates requirements, implementation hints and clarifying questions
        addressed to team members, then persists them on the card so the detail
        view shows implementation + requirement details.
        """
        from .requirements_agent import analyze_requirements

        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}

        prepared = self._prepare_kanban_payload(payload, actor)
        workdir = str(prepared.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}

        kanban_file = prepared.get("kanban_file")
        kanban_file_str = (
            str(kanban_file).strip() if isinstance(kanban_file, str) and kanban_file.strip() else None
        )

        try:
            board = get_board(
                workdir, self.cfg.hermes_agent_create, kanban_file=kanban_file_str
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        task = next(
            (
                t
                for t in (board.get("tasks") or [])
                if str(t.get("id") or "").strip() == task_id
            ),
            None,
        )
        if not isinstance(task, dict):
            return {"ok": False, "error": f"tarefa {task_id} não encontrada"}

        # Gather the team so the agent knows who to ask.
        agents: list[Any] = []
        raw_agents = payload.get("agents")
        if isinstance(raw_agents, list):
            agents.extend(raw_agents)
        if not agents:
            try:
                projects = self.handle_hermes_kanban_projects(user, quick=True)
                if isinstance(projects, dict):
                    agents = list(projects.get("agents") or [])
            except Exception:
                agents = []

        analysis = analyze_requirements(task, agents, agent_cfg=self.cfg.agent)

        # Persist requirements + implementation + open questions on the card.
        persisted = False
        try:
            update_payload = {
                "action": "update_task",
                "task_id": task_id,
                "workdir": workdir,
                "kanban_file": kanban_file_str,
                "task": {
                    "requirements": analysis.get("requirements_text") or "",
                    "implementation_details": analysis.get("implementation_text") or "",
                    "open_questions": analysis.get("open_questions") or [],
                },
            }
            apply_kanban_action(workdir, self.cfg.hermes_agent_create, update_payload)
            persisted = True
        except Exception as e:
            log.warning("requirements agent: failed to persist card %s: %s", task_id, e)

        return {
            "ok": True,
            "task_id": task_id,
            "summary": analysis.get("summary") or "",
            "readiness": analysis.get("readiness") or "needs_info",
            "requirements": analysis.get("requirements") or [],
            "acceptance_criteria": analysis.get("acceptance_criteria") or [],
            "implementation_details": analysis.get("implementation_details") or [],
            "assumptions": analysis.get("assumptions") or [],
            "open_questions": analysis.get("open_questions") or [],
            "used_llm": bool(analysis.get("used_llm")),
            "persisted": persisted,
        }

"""Hermes agent and OpenAI swarm creation from dashboard chat."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .accounts import UserRecord
from .hermes_profiles import create_agent_from_text
from .hermes_project_agents import create_project_agent

log = logging.getLogger(__name__)


class MonitorHermesAgentCreateMixin:
    def handle_hermes_agent_create(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Create a Hermes dev profile + cron job from natural language (dashboard chat)."""
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled in config"}

        cron_pause_meta: dict[str, Any] = {}
        with self._pause_hermes_crons_for_agent_create() as cron_snapshot:
            if cron_snapshot is not None and cron_snapshot.paused_job_ids:
                cron_pause_meta = {
                    "cron_jobs_paused": len(cron_snapshot.paused_job_ids),
                }
            ready = self._wait_hermes_dashboard_for_create()
            if ready is not None:
                return ready

            mode = str(payload.get("mode") or "").strip().lower()
            if mode == "project":
                dashboard_url = self._hermes_dashboard_url()
                assert dashboard_url is not None
                try:
                    forced_workdir: Optional[str] = None
                    forced_kanban_file: Optional[str] = None
                    resolved_team_id: Optional[str] = None
                    if actor is not None:
                        resolved_team_id = str(payload.get("team_id") or "").strip()
                        if not resolved_team_id:
                            teams = self.accounts.list_teams(actor.id)
                            if len(teams) == 1:
                                resolved_team_id = teams[0].id
                                payload["team_id"] = resolved_team_id
                            elif not teams:
                                return {
                                    "ok": False,
                                    "error": (
                                        "team_id is required "
                                        "(nenhum time encontrado para o usuário)"
                                    ),
                                }
                            else:
                                return {
                                    "ok": False,
                                    "error": (
                                        "team_id is required "
                                        "(usuário possui múltiplos times; "
                                        "selecione um team_id)"
                                    ),
                                }
                        team = self.accounts.get_team(resolved_team_id, actor.id)
                        forced_workdir = str(self.accounts.team_workdir(team))
                        forced_kanban_file = "kanban.yaml"
                    result = create_project_agent(
                        payload,
                        dashboard_url=dashboard_url,
                        agent_cfg=self.cfg.agent,
                        create_cfg=self.cfg.hermes_agent_create,
                        schedule_enabled=payload.get("schedule_enabled")
                        if isinstance(payload.get("schedule_enabled"), bool)
                        else None,
                        forced_workdir=forced_workdir,
                        forced_kanban_file=forced_kanban_file,
                    )
                    if actor is not None and resolved_team_id and result.get("name"):
                        self.accounts.add_team_profile(
                            resolved_team_id, actor.id, str(result.get("name"))
                        )
                    self._invalidate_hermes_profiles_cache()
                    self._invalidate_hermes_cron_cache()
                    result["dashboard_url"] = dashboard_url
                    result.update(cron_pause_meta)
                    log.info(
                        "created Hermes project agent %s (%s)",
                        result.get("name"),
                        result.get("display_name"),
                    )
                    return result
                except Exception as e:
                    log.exception("Hermes project agent create failed: %s", e)
                    return {"ok": False, "error": f"{type(e).__name__}: {e}"}

            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "text is required"}

            schedule = payload.get("schedule")
            if schedule is not None and not isinstance(schedule, str):
                return {"ok": False, "error": "schedule must be a string"}

            workdir = payload.get("workdir")
            if workdir is not None and not isinstance(workdir, str):
                return {"ok": False, "error": "workdir must be a string"}
            team_id_raw = payload.get("team_id")
            if team_id_raw is not None and not isinstance(team_id_raw, str):
                return {"ok": False, "error": "team_id must be a string"}

            skills_raw = payload.get("skills")
            skills: Optional[list[str]] = None
            if skills_raw is not None:
                if isinstance(skills_raw, str):
                    skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
                elif isinstance(skills_raw, list):
                    skills = [str(s).strip() for s in skills_raw if str(s).strip()]
                else:
                    return {"ok": False, "error": "skills must be a string or list"}

            role_preset_raw = payload.get("role_preset")
            role_preset: Optional[str] = None
            if role_preset_raw is not None:
                role_preset = str(role_preset_raw).strip() or None
            if not skills and role_preset:
                from .hermes_profiles import preset_default_skills

                preset_skills = preset_default_skills(role_preset)
                if preset_skills:
                    skills = preset_skills

            schedule_enabled = payload.get("schedule_enabled")
            if schedule_enabled is not None and not isinstance(
                schedule_enabled, bool
            ):
                return {"ok": False, "error": "schedule_enabled must be a boolean"}

            mode = str(payload.get("mode") or "dev").strip().lower()
            if mode not in ("dev", "role"):
                return {"ok": False, "error": "mode must be 'dev' or 'role'"}

            resolved_team_id: Optional[str] = None
            if actor is not None:
                requested_team_id = str(team_id_raw or "").strip()
                if requested_team_id:
                    team = self.accounts.get_team(requested_team_id, actor.id)
                    resolved_team_id = team.id
                    if not (isinstance(workdir, str) and workdir.strip()):
                        workdir = str(self.accounts.team_workdir(team))
                elif mode == "dev" and not (isinstance(workdir, str) and workdir.strip()):
                    teams = self.accounts.list_teams(actor.id)
                    if len(teams) == 1:
                        resolved_team_id = teams[0].id
                        workdir = str(self.accounts.team_workdir(teams[0]))

            dashboard_url = self._hermes_dashboard_url()
            assert dashboard_url is not None
            try:
                result = create_agent_from_text(
                    text,
                    dashboard_url=dashboard_url,
                    agent_cfg=self.cfg.agent,
                    create_cfg=self.cfg.hermes_agent_create,
                    schedule=schedule,
                    skills=skills,
                    workdir=workdir,
                    schedule_enabled=schedule_enabled,
                    mode=mode,
                    stagger_fn=self._stagger_schedule_for_new_job,
                    role_preset=role_preset,
                )
                if actor is not None and not resolved_team_id:
                    hint_workdir = str(
                        (workdir if isinstance(workdir, str) else "")
                        or result.get("workdir")
                        or ""
                    ).strip()
                    if hint_workdir:
                        try:
                            hint_path = Path(hint_workdir).expanduser().resolve()
                        except OSError:
                            hint_path = None
                        if hint_path is not None:
                            for team in self.accounts.list_teams(actor.id):
                                try:
                                    if hint_path == self.accounts.team_workdir(team).resolve():
                                        resolved_team_id = team.id
                                        break
                                except OSError:
                                    continue
                if resolved_team_id and actor is not None:
                    try:
                        kanban_payload = self._prepare_kanban_payload(
                            {"team_id": resolved_team_id}, actor
                        )
                        kanban_workdir = str(kanban_payload.get("workdir") or "").strip()
                        kanban_file = str(kanban_payload.get("kanban_file") or "").strip()
                        if kanban_workdir:
                            title = str(
                                result.get("requested_name")
                                or result.get("name")
                                or text[:80]
                            ).strip()
                            task_description = str(text).strip()
                            assignee = str(result.get("name") or "").strip()
                            task_payload: dict[str, Any] = {
                                "action": "create_task",
                                "kanban_file": kanban_file or None,
                                "task": {
                                    "title": title or "Novo agente criado via chat",
                                    "description": task_description,
                                    "column": "todo",
                                },
                            }
                            if assignee:
                                task_payload["task"]["assignees"] = [assignee]
                            board = apply_kanban_action(
                                kanban_workdir,
                                self.cfg.hermes_agent_create,
                                task_payload,
                            )
                            result["kanban"] = {
                                "team_id": resolved_team_id,
                                "workdir": kanban_workdir,
                                "kanban_file": kanban_file,
                                "task_id": board.get("task_id"),
                            }
                    except Exception as kanban_err:
                        log.warning(
                            "auto-kanban create_task failed after chat create: %s",
                            kanban_err,
                        )
                elif actor is not None:
                    result["kanban_warning"] = (
                        "team_id não resolvido; cartão não criado automaticamente"
                    )
                if resolved_team_id:
                    result["team_id"] = resolved_team_id
                self._invalidate_hermes_profiles_cache()
                self._invalidate_hermes_cron_cache()
                result["dashboard_url"] = dashboard_url
                result.update(cron_pause_meta)
                log.info(
                    "created Hermes profile %s from chat (requested=%s, cron=%s)",
                    result.get("name"),
                    result.get("requested_name"),
                    (result.get("cron") or {}).get("job_id"),
                )
                return result
            except Exception as e:
                log.exception("Hermes agent create failed: %s", e)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openai_swarm_create(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Create an OpenAI-style agent swarm on Hermes from natural language or preset."""
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled in config"}

        preset_id = str(payload.get("preset_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not preset_id and not text:
            return {"ok": False, "error": "text or preset_id is required"}

        workdir = payload.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            return {"ok": False, "error": "workdir must be a string"}

        schedule = payload.get("schedule")
        if schedule is not None and not isinstance(schedule, str):
            return {"ok": False, "error": "schedule must be a string"}

        agent_counts = payload.get("agent_counts")
        if agent_counts is not None and not isinstance(agent_counts, dict):
            return {"ok": False, "error": "agent_counts must be an object"}

        model_id = str(payload.get("model_id") or payload.get("model") or "").strip() or None
        chat_cfg = (
            self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        )

        resolved_team = None
        if actor is not None:
            requested_team_id = str(payload.get("team_id") or "").strip()
            if requested_team_id:
                resolved_team = self._resolve_team_for_actor(requested_team_id, actor)
                if resolved_team and not (isinstance(workdir, str) and workdir.strip()):
                    workdir = str(self.accounts.team_workdir(resolved_team))
            elif not (isinstance(workdir, str) and workdir.strip()):
                teams = self.accounts.list_teams(actor.id)
                if len(teams) == 1:
                    resolved_team = teams[0]
                    workdir = str(self.accounts.team_workdir(teams[0]))

        team_id_for_swarm = resolved_team.id if resolved_team is not None else ""

        cron_pause_meta: dict[str, Any] = {}
        with self._pause_hermes_crons_for_agent_create() as cron_snapshot:
            if cron_snapshot is not None and cron_snapshot.paused_job_ids:
                cron_pause_meta = {
                    "cron_jobs_paused": len(cron_snapshot.paused_job_ids),
                }
            ready = self._wait_hermes_dashboard_for_create()
            if ready is not None:
                return ready

            dashboard_url = self._hermes_dashboard_url()
            assert dashboard_url is not None
            try:
                if preset_id:
                    from .openai_swarm import create_swarm_from_preset

                    result = create_swarm_from_preset(
                        preset_id,
                        dashboard_url=dashboard_url,
                        create_cfg=self.cfg.hermes_agent_create,
                        workdir=workdir,
                        schedule=schedule,
                        stagger_fn=self._stagger_schedule_for_new_job,
                        name_override=str(payload.get("name") or "").strip() or None,
                        agent_counts=agent_counts,
                        model_id=model_id,
                        chat_cfg=chat_cfg,
                        team_id=team_id_for_swarm,
                    )
                else:
                    from .openai_swarm import create_swarm

                    result = create_swarm(
                        text,
                        dashboard_url=dashboard_url,
                        agent_cfg=self.cfg.agent,
                        create_cfg=self.cfg.hermes_agent_create,
                        workdir=workdir,
                        schedule=schedule,
                        stagger_fn=self._stagger_schedule_for_new_job,
                        model_id=model_id,
                        chat_cfg=chat_cfg,
                        team_id=team_id_for_swarm,
                    )
                self._invalidate_hermes_profiles_cache()
                self._invalidate_hermes_cron_cache()
                self._invalidate_swarm_robots_cache()
                result["dashboard_url"] = dashboard_url
                result.update(cron_pause_meta)
                if actor is not None and isinstance(result, dict):
                    swarm_name = str(result.get("swarm_name") or "").strip()
                    profiles = [
                        str(p).strip()
                        for p in (result.get("hermes_profiles") or [])
                        if str(p).strip()
                    ]
                    if swarm_name:
                        team = resolved_team or self._resolve_team_for_swarm_link(
                            payload,
                            actor,
                            workdir=str(result.get("workdir") or workdir or ""),
                            result=result,
                        )
                        if team is not None:
                            link = self._link_team_to_swarm(
                                team,
                                swarm_name,
                                actor,
                                profile_slugs=profiles or None,
                            )
                            result["team_id"] = team.id
                            result["team_name"] = team.name
                            result["team_linked"] = True
                            if isinstance(link, dict) and link.get("kanban_repaired"):
                                result["team_kanban_repaired"] = True
                log.info(
                    "created OpenAI swarm '%s' with %d agents on Hermes",
                    result.get("swarm_name"),
                    result.get("agents_created", 0),
                )
                return result
            except Exception as e:
                log.exception("OpenAI swarm create failed: %s", e)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}


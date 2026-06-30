"""Swarm CRUD, robot management, and health endpoints."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .accounts import UserRecord

log = logging.getLogger(__name__)


class MonitorSwarmAdminMixin:
    def handle_swarm_robot_create(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Create a single swarm robot with profile + LLM."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        from .swarm_robots import create_swarm_robot

        existing_slug = str(
            payload.get("profile_slug") or payload.get("existing_profile") or ""
        ).strip()
        if not existing_slug:
            blocked = self._model_quota_guard()
            if blocked is not None:
                return blocked
            if not self.cfg.hermes_agent_create.enabled:
                return {"ok": False, "error": "hermes agent create is disabled"}

        try:
            result = create_swarm_robot(self, payload, user)
        except Exception as e:
            log.exception("swarm robot create failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if result.get("ok"):
            self._invalidate_hermes_profiles_cache()
            self._invalidate_swarm_robots_cache()
        return result

    def handle_swarm_robot_get(
        self, slug: str, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        from .swarm_robots import get_swarm_robot_detail

        try:
            return get_swarm_robot_detail(self, slug, user)
        except Exception as e:
            log.exception("swarm robot get failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_robot_source(
        self, slug: str, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        from .swarm_robots import get_swarm_robot_source

        try:
            return get_swarm_robot_source(self, slug, user)
        except Exception as e:
            log.exception("swarm robot source failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_robot_update(
        self, slug: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        from .swarm_robots import is_profile_assign_payload, update_swarm_robot

        if not is_profile_assign_payload(payload) and not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        try:
            result = update_swarm_robot(self, slug, payload, user)
        except Exception as e:
            log.exception("swarm robot update failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if result.get("ok"):
            self._invalidate_swarm_robots_cache()
        return result

    def handle_swarm_schedule(
        self,
        swarm_name: str,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        """Apply the same Hermes cron schedule to every robot in a swarm."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        if payload.get("resume") in (True, "true", 1, "1"):
            from .swarm_robots import resume_swarm_robots_schedule

            try:
                return resume_swarm_robots_schedule(self, swarm_name, user=user)
            except Exception as e:
                log.exception("swarm schedule resume failed: %s", e)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        schedule = str(payload.get("schedule") or payload.get("cron_schedule") or "").strip()
        if not schedule:
            return {"ok": False, "error": "schedule is required"}

        schedule_target = payload.get("schedule_target")
        task_id = payload.get("task_id") or payload.get("schedule_task_id")

        from .swarm_robots import schedule_swarm_robots

        try:
            return schedule_swarm_robots(
                self,
                swarm_name,
                schedule=schedule,
                user=user,
                schedule_target=(
                    str(schedule_target).strip()
                    if schedule_target is not None
                    else None
                ),
                task_id=str(task_id).strip() if task_id else None,
            )
        except Exception as e:
            log.exception("swarm schedule failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_model(
        self,
        swarm_name: str,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        """Apply the same LLM model to every robot in a swarm."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        model_id = str(payload.get("model_id") or payload.get("model") or "").strip()
        if not model_id:
            return {"ok": False, "error": "model_id is required"}

        from .swarm_robots import set_swarm_robots_model

        try:
            return set_swarm_robots_model(
                self,
                swarm_name,
                model_id=model_id,
                user=user,
            )
        except Exception as e:
            log.exception("swarm model update failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_robot_delete(
        self, slug: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        from .swarm_robots import delete_swarm_robot

        try:
            result = delete_swarm_robot(self, slug, payload, user)
        except Exception as e:
            log.exception("swarm robot delete failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if result.get("ok"):
            self._invalidate_hermes_profiles_cache()
            self._invalidate_swarm_robots_cache()
            self._invalidate_hermes_cron_cache()
            self._schedule_active_agents_snapshot_refresh()
        return result

    def _resolve_team_for_actor(self, team_id: str, actor: Optional[Any]):
        """Look up a team by id/name, scoped to the actor (or globally if no auth)."""
        tid = str(team_id or "").strip()
        if not tid:
            return None
        try:
            if actor is not None:
                return self.accounts.get_team(tid, actor.id)
            for team in self.accounts.list_all_teams():
                if team.id == tid or (team.name or "").strip().lower() == tid.lower():
                    return team
        except Exception as exc:
            log.debug("resolve team %s failed: %s", tid, exc)
        return None

    def _resolve_team_for_swarm_link(
        self,
        payload: dict,
        actor: Optional[Any],
        *,
        workdir: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
    ):
        """Best-effort team resolution when linking a newly created swarm."""
        if actor is None:
            return None
        requested = str(payload.get("team_id") or "").strip()
        if requested:
            return self._resolve_team_for_actor(requested, actor)

        hint_workdir = str(
            workdir or (result or {}).get("workdir") or ""
        ).strip()
        if hint_workdir:
            try:
                from pathlib import Path

                hint_path = Path(hint_workdir).expanduser().resolve()
                for team in self.accounts.list_teams(actor.id):
                    try:
                        if hint_path == self.accounts.team_workdir(team).resolve():
                            return team
                    except OSError:
                        continue
            except OSError:
                pass

        teams = self.accounts.list_teams(actor.id)
        if len(teams) == 1:
            return teams[0]
        return None

    def _link_team_to_swarm(
        self,
        team: Any,
        swarm_name: str,
        actor: Optional[Any],
        *,
        profile_slugs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Persist swarm_name + profile slugs on the team and team_id on the swarm."""
        name = str(swarm_name or "").strip()
        if not team or not name:
            return {"ok": False, "error": "team or swarm_name missing"}
        kanban: dict[str, Any] = {"ok": True, "tasks": 0, "kanban_repaired": False}
        if actor is not None:
            from .swarm_robots import ensure_swarm_team_kanban_ready

            try:
                kanban = ensure_swarm_team_kanban_ready(self, team.id, actor)
            except Exception as exc:
                log.debug("swarm team kanban ready check failed for %s: %s", team.id, exc)
        fields: dict[str, Any] = {"swarm_name": name}
        if profile_slugs:
            existing = [str(p).strip() for p in (team.profile_slugs or []) if str(p).strip()]
            merged = list(
                dict.fromkeys(existing + [str(p).strip() for p in profile_slugs if str(p).strip()])
            )
            if merged:
                fields["profile_slugs"] = merged
        try:
            self.accounts.update_team(team.id, team.owner_id, fields)
        except Exception as exc:
            log.debug("link team %s to swarm %s failed: %s", team.id, name, exc)
        try:
            from .openai_swarm import update_swarm_definition

            update_swarm_definition(name, team_id=team.id)
        except Exception as exc:
            log.debug("set team_id on swarm %s failed: %s", name, exc)
        return kanban

    def handle_swarm_create(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Create a new (empty) swarm definition."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        from .openai_swarm import create_swarm_definition

        name = str(payload.get("name") or "")
        description = str(payload.get("description") or "")
        topology = str(payload.get("topology") or "handoff")
        entry_agent = str(payload.get("entry_agent") or "")
        hermes_profiles = payload.get("hermes_profiles")

        # Optional: link the swarm to an existing team and seed it with the
        # team's member profiles (topology defaults to "team").
        team_id = str(payload.get("team_id") or "").strip()
        team = None
        if team_id:
            team = self._resolve_team_for_actor(team_id, actor)
            if team is None:
                return {"ok": False, "error": f"time '{team_id}' não encontrado"}
            team_id = team.id
            if not name:
                name = team.name or team.id
            if not description:
                description = team.description or f"Swarm do time {team.name or team.id}"
            if not payload.get("topology"):
                topology = "team"
            team_profiles = [str(p).strip() for p in (team.profile_slugs or []) if str(p).strip()]
            if team_profiles:
                merged = list(hermes_profiles or [])
                for slug in team_profiles:
                    if slug not in merged:
                        merged.append(slug)
                hermes_profiles = merged

        try:
            result = create_swarm_definition(
                name,
                description=description,
                topology=topology,
                entry_agent=entry_agent,
                hermes_profiles=hermes_profiles,
                agents=payload.get("agents"),
                graph=payload.get("graph"),
                team_id=team_id,
            )
        except Exception as e:
            log.exception("swarm create failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if result.get("ok"):
            if team is not None:
                kanban = self._link_team_to_swarm(
                    team, str(result.get("name") or name), actor
                )
                if isinstance(kanban, dict) and kanban.get("ok"):
                    result["team_kanban"] = {
                        "tasks": kanban.get("tasks", 0),
                        "repaired": kanban.get("kanban_repaired", False),
                    }
            self._invalidate_swarm_robots_cache()
        return result

    def handle_swarm_update(
        self, name: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Edit metadata of an existing swarm (name, description, topology, entry)."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        from .openai_swarm import update_swarm_definition

        team = None
        update_payload = dict(payload)
        if "team_id" in payload:
            team_id = str(payload.get("team_id") or "").strip()
            if team_id:
                team = self._resolve_team_for_actor(team_id, actor)
                if team is None:
                    return {"ok": False, "error": f"time '{team_id}' não encontrado"}
                update_payload["team_id"] = team.id

        try:
            result = update_swarm_definition(
                name,
                new_name=(
                    update_payload.get("new_name")
                    if update_payload.get("new_name") is not None
                    else update_payload.get("name")
                ),
                description=update_payload.get("description"),
                topology=update_payload.get("topology"),
                entry_agent=update_payload.get("entry_agent"),
                agents=update_payload.get("agents"),
                hermes_profiles=update_payload.get("hermes_profiles"),
                graph=update_payload.get("graph"),
                team_id=update_payload.get("team_id")
                if "team_id" in update_payload
                else None,
            )
        except Exception as e:
            log.exception("swarm update failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if result.get("ok"):
            if "team_id" in payload:
                swarm_name = str(
                    result.get("name")
                    or update_payload.get("new_name")
                    or update_payload.get("name")
                    or name
                )
                if team is not None:
                    self._link_team_to_swarm(team, swarm_name, actor)
            self._invalidate_swarm_robots_cache()
        return result

    def handle_swarm_delete(
        self, name: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Delete a swarm definition (Hermes profiles are kept)."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        from .openai_swarm import delete_swarm_definition

        try:
            result = delete_swarm_definition(name)
        except Exception as e:
            log.exception("swarm delete failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # No file-based swarm with this name: it may be a team-derived swarm
        # grouping. "Deleting" it means ungrouping — unlink the team's profiles
        # while keeping the team, kanban and Hermes profiles intact.
        if not result.get("ok") and actor is not None:
            team_result = self._ungroup_team_swarm(name, actor)
            if team_result is not None:
                result = team_result

        # Or an auto-grouped virtual swarm (e.g. orelhao-dev + orelhao-coder).
        if not result.get("ok"):
            from .swarm_robots import dismiss_virtual_swarm

            virtual_result = dismiss_virtual_swarm(name)
            if virtual_result is not None:
                result = virtual_result

        if result.get("ok"):
            self._invalidate_swarm_robots_cache()
        return result

    def _ungroup_team_swarm(
        self, name: str, actor: UserRecord
    ) -> Optional[dict[str, Any]]:
        """Unlink all profiles from a team-derived swarm (keeps the team).

        Returns a result dict when *name* resolves to a team the actor can edit,
        or ``None`` when it is not a team swarm (so the caller can keep the
        original not-found error).
        """
        try:
            team = self.accounts.get_team(name, actor.id)
        except Exception:
            return None
        freed = [str(s).strip() for s in (team.profile_slugs or []) if str(s).strip()]
        if not freed:
            return None
        try:
            self.accounts.update_team(team.id, actor.id, {"profile_slugs": []})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        log.info("ungrouped team swarm %s (freed %d profiles)", team.id, len(freed))
        return {
            "ok": True,
            "name": team.id,
            "ungrouped_team": team.id,
            "freed_profiles": freed,
        }

    def handle_swarm_health(
        self, swarm_name: str, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Read-only health audit for a swarm definition."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        from .swarm_health import check_swarm_health

        try:
            return check_swarm_health(swarm_name, monitor=self, actor=actor)
        except Exception as e:
            log.exception("swarm health failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_swarm_health_fix(
        self,
        swarm_name: str,
        payload: Optional[dict[str, Any]] = None,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        """Auto-resolve the repairable health issues for a swarm."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}

        mode = ""
        schedule = None
        if isinstance(payload, dict):
            mode = str(payload.get("mode") or "").strip().lower()
            raw_sched = str(payload.get("schedule") or "").strip()
            schedule = raw_sched or None

        try:
            if mode == "llm":
                from .swarm_health_agent import llm_fix_swarm_health

                return llm_fix_swarm_health(
                    swarm_name, monitor=self, actor=actor, agent_cfg=self.cfg.agent
                )
            from .swarm_health import fix_swarm_health

            return fix_swarm_health(
                swarm_name, monitor=self, schedule=schedule, actor=actor
            )
        except Exception as e:
            log.exception("swarm health fix failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}



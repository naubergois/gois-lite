"""Hermes profile cache, roles seed, and list handlers for GoisMonitor."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .accounts import UserRecord
from .hermes_profile_model import enrich_row_with_profile_model
from .hermes_profiles import (
    list_hermes_profiles,
    list_hermes_profiles_filesystem,
)
from .openclaw_chat import hermes_session_key

log = logging.getLogger(__name__)

HERMES_PROFILES_CACHE_SECONDS_DEFAULT = 120.0

class MonitorHermesProfilesMixin:
    def _invalidate_hermes_profiles_cache(self) -> None:
        self._hermes_profiles_cache_expires_at = 0.0
        self._hermes_profiles_cache_enriched = False
        self._hermes_profiles_cache_all = []
        self._hermes_profiles_cache_by_user = {}

    def _hermes_profiles_cache_ttl(self) -> float:
        hac = self.cfg.hermes_agent_create
        ttl = float(getattr(hac, "profiles_cache_seconds", 0) or 0)
        if ttl > 0:
            return ttl
        return HERMES_PROFILES_CACHE_SECONDS_DEFAULT

    def _enrich_profile_model_row(
        self,
        row: dict[str, Any],
        *,
        profile_key: str = "profile",
    ) -> None:
        if not self.cfg.hermes:
            return
        chat_cfg = (
            self.cfg.openclaw_chat if self.cfg.openclaw_chat.enabled else None
        )
        enrich_row_with_profile_model(
            row,
            profile_key=profile_key,
            chat_cfg=chat_cfg,
            agent_cfg=self.cfg.agent,
        )
    @staticmethod
    def _profiles_scope_roles(query: Optional[dict]) -> bool:
        """Roles dashboard lists every profile on disk, not only team-assigned slugs."""
        q = query or {}
        return str(q.get("scope") or "").strip().lower() == "roles"

    def _filter_profiles_for_user(
        self, profiles: list[dict], user: UserRecord
    ) -> list[dict]:
        if user.is_admin:
            return list(profiles)
        allowed = {
            slug
            for t in self.accounts.list_teams(user.id)
            for slug in t.profile_slugs
        }
        if not allowed:
            return list(profiles)
        return [
            p for p in profiles if str(p.get("name") or "").strip() in allowed
        ]

    def _cached_hermes_profiles(
        self,
        dashboard_url: str,
        user: Optional[UserRecord],
        *,
        timeout: Optional[float] = None,
        enrich_local_meta: Optional[bool] = None,
        skip_user_filter: bool = False,
    ) -> list[dict]:
        if enrich_local_meta is None:
            enrich_local_meta = bool(
                self.cfg.hermes_agent_create.profiles_enrich_local_meta
            )
        now = time.time()
        cache_valid = now < self._hermes_profiles_cache_expires_at
        user_key = user.id if user is not None else "__all__"
        if (
            cache_valid
            and self._hermes_profiles_cache_all
            and self._hermes_profiles_cache_enriched == enrich_local_meta
        ):
            profiles = list(self._hermes_profiles_cache_all)
            if user is not None and self.cfg.auth.enabled and not skip_user_filter:
                cached_user_profiles = self._hermes_profiles_cache_by_user.get(user_key)
                if cached_user_profiles is not None:
                    return list(cached_user_profiles)
                profiles = self._filter_profiles_for_user(profiles, user)
                self._hermes_profiles_cache_by_user[user_key] = list(profiles)
            return list(profiles)

        if timeout is None:
            profiles = list_hermes_profiles(
                dashboard_url, enrich_local_meta=enrich_local_meta
            )
        else:
            profiles = list_hermes_profiles(
                dashboard_url,
                timeout=timeout,
                enrich_local_meta=enrich_local_meta,
            )
        self._hermes_profiles_cache_all = list(profiles)
        self._hermes_profiles_cache_enriched = enrich_local_meta
        self._hermes_profiles_cache_by_user = {}

        if user is not None and self.cfg.auth.enabled and not skip_user_filter:
            profiles = self._filter_profiles_for_user(profiles, user)
            self._hermes_profiles_cache_by_user[user_key] = list(profiles)

        self._hermes_profiles_cache_expires_at = now + self._hermes_profiles_cache_ttl()
        return list(profiles)

    def _fallback_profiles_for_user(self, user: UserRecord) -> list[dict[str, Any]]:
        """Build a minimal profile list from local team metadata."""
        slugs: set[str] = set()
        for team in self.accounts.list_teams(user.id):
            for slug in team.profile_slugs:
                value = str(slug).strip()
                if value:
                    slugs.add(value)
        return [
            {"name": slug, "display_name": slug, "description": "perfil local (fallback)"}
            for slug in sorted(slugs, key=str.lower)
        ]
    def handle_hermes_role_presets(self) -> dict:
        """Return team role presets for the /roles dashboard."""
        from .hermes_profiles import (
            TEAM_ROLE_PRESETS,
            _ROLE_PRESET_DEFAULT_SKILLS,
            role_catalog_status,
        )

        return {
            "ok": True,
            "presets": TEAM_ROLE_PRESETS,
            "preset_skills": _ROLE_PRESET_DEFAULT_SKILLS,
            "dashboard_url": self._hermes_dashboard_url(),
            **role_catalog_status(),
        }

    def handle_hermes_profile_generate_personality(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Generate SOUL.md from natural language for the profile editor."""
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

        prompt = str(payload.get("prompt") or payload.get("text") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}

        display_name = str(payload.get("display_name") or "").strip() or None
        role = str(payload.get("role") or "").strip() or None
        role_preset = str(payload.get("role_preset") or "").strip() or None

        try:
            from .hermes_profiles import generate_personality_from_prompt

            return generate_personality_from_prompt(
                prompt,
                agent_cfg=self.cfg.agent,
                display_name=display_name,
                role=role,
                role_preset=role_preset,
            )
        except Exception as e:
            log.exception("generate personality failed: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_roles_seed_status(self) -> dict:
        """Progress of a background role-catalog seed (if any)."""
        return {"ok": True, **dict(self._role_catalog_seed_status)}

    def handle_hermes_roles_seed(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Create Hermes profiles from TEAM_ROLE_PRESETS (background by default)."""
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled in config"}

        sync = payload.get("sync") is True or payload.get("background") is False
        if sync:
            try:
                result = self._run_role_catalog_seed_sync(payload)
                result["dashboard_url"] = self._hermes_dashboard_url()
                return result
            except Exception as e:
                log.exception("Hermes roles seed failed: %s", e)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return self._start_role_catalog_seed_async(payload)

    def _run_role_catalog_seed_sync(self, payload: dict) -> dict[str, Any]:
        from .hermes_profiles import seed_team_role_presets

        dashboard_url = self._hermes_dashboard_url()
        if not dashboard_url:
            return {"ok": False, "error": "hermes dashboard URL not configured"}
        only_missing = payload.get("only_missing", True)
        if not isinstance(only_missing, bool):
            only_missing = bool(only_missing)
        preset_ids = payload.get("preset_ids")
        ids: Optional[list[str]] = None
        if isinstance(preset_ids, list):
            ids = [str(x).strip() for x in preset_ids if str(x).strip()]
        raw_categories = payload.get("categories")
        categories: Optional[list[str]] = None
        if isinstance(raw_categories, list):
            categories = [str(c).strip() for c in raw_categories if str(c).strip()]

        hac = self.cfg.hermes_agent_create
        result = seed_team_role_presets(
            dashboard_url,
            clone_from_default=hac.clone_from_default,
            only_missing=only_missing,
            preset_ids=ids,
            categories=categories,
            progress_every=hac.seed_role_catalog_progress_every,
            timeout=hac.dashboard_api_timeout_seconds,
            use_filesystem=hac.seed_role_catalog_use_filesystem,
            template_profile=hac.seed_role_catalog_template_profile,
        )
        if result.get("created"):
            self._invalidate_hermes_profiles_cache()
        return result

    def _start_role_catalog_seed_async(self, payload: dict) -> dict[str, Any]:
        if self._role_catalog_seed_status.get("running"):
            return {
                "ok": True,
                "started": False,
                "running": True,
                "status": dict(self._role_catalog_seed_status),
                "dashboard_url": self._hermes_dashboard_url(),
            }
        self._role_catalog_seed_status = {
            "running": True,
            "created": 0,
            "skipped": 0,
            "errors": 0,
            "total_presets": 0,
        }
        asyncio.create_task(
            self._role_catalog_seed_task(payload), name="hermes_role_catalog_seed"
        )
        return {
            "ok": True,
            "started": True,
            "running": True,
            "message": "Criação de papéis em segundo plano — acompanhe nos logs ou /hermes/roles/seed/status",
            "dashboard_url": self._hermes_dashboard_url(),
        }

    async def _role_catalog_seed_task(self, payload: dict) -> None:
        try:
            result = await asyncio.to_thread(self._run_role_catalog_seed_sync, payload)
            self._role_catalog_seed_status = {"running": False, **result}
            log.info(
                "role catalog seed finished: created=%d skipped=%d errors=%d",
                len(result.get("created") or []),
                len(result.get("skipped") or []),
                len(result.get("errors") or []),
            )
        except Exception as e:
            log.exception("background role catalog seed failed: %s", e)
            self._role_catalog_seed_status = {
                "running": False,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
    def handle_hermes_profiles_list(
        self,
        user: Optional[UserRecord] = None,
        query: Optional[dict] = None,
    ) -> dict:
        """List Hermes profiles via the dashboard API."""
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}
        dashboard_url = self._hermes_dashboard_url()
        assert dashboard_url is not None
        q = query or {}
        quick = str(q.get("quick") or "").lower() in ("1", "true", "yes", "on")
        force_refresh = str(q.get("refresh") or "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        enrich = self.cfg.hermes_agent_create.profiles_enrich_local_meta
        if quick or str(q.get("enrich") or "").lower() in ("0", "false", "no", "off"):
            enrich = False
        elif str(q.get("enrich") or "").lower() in ("1", "true", "yes", "on"):
            enrich = True
        scope_roles = self._profiles_scope_roles(q)

        if quick and not force_refresh:
            disk_rows = list_hermes_profiles_filesystem()
            if disk_rows:
                profiles = list(disk_rows)
                if user is not None and self.cfg.auth.enabled and not scope_roles:
                    profiles = self._filter_profiles_for_user(profiles, user)
                now = time.time()
                self._hermes_profiles_cache_all = list(disk_rows)
                self._hermes_profiles_cache_enriched = False
                self._hermes_profiles_cache_by_user = {}
                if user is not None and self.cfg.auth.enabled and not scope_roles:
                    self._hermes_profiles_cache_by_user[user.id] = list(profiles)
                self._hermes_profiles_cache_expires_at = (
                    now + self._hermes_profiles_cache_ttl()
                )
                from .hermes_profiles import role_catalog_status

                from .swarm_robots import filter_tombstoned_profiles

                return {
                    "ok": True,
                    "profiles": filter_tombstoned_profiles(profiles),
                    "dashboard_url": dashboard_url,
                    "quick": True,
                    "source": "filesystem",
                    **role_catalog_status(),
                }

        try:
            api_timeout = 8.0 if quick else None
            profiles = self._cached_hermes_profiles(
                dashboard_url,
                user,
                enrich_local_meta=enrich,
                timeout=api_timeout,
                skip_user_filter=scope_roles,
            )
            out: dict[str, Any] = {
                "ok": True,
                "profiles": profiles,
                "dashboard_url": dashboard_url,
            }
            # Enrich profiles with last chat activity timestamps from persistence
            if self.chat_persistence is not None:
                try:
                    db_rows = self.chat_persistence.history.list_sessions(limit=500)
                    ts_map: dict[str, int] = {}
                    for db_row in db_rows:
                        key = str(db_row.get("key") or "").strip()
                        ts = int(db_row.get("updatedAt") or 0)
                        if key and ts:
                            ts_map[key] = ts
                    for p in profiles:
                        if isinstance(p, dict):
                            name = str(p.get("name") or "").strip()
                            if name:
                                chat_ts = ts_map.get(hermes_session_key(name), 0)
                                if chat_ts:
                                    p["last_chat_at"] = chat_ts
                except Exception:
                    pass
            if quick:
                out["quick"] = True
            if scope_roles:
                out["scope"] = "roles"
            if force_refresh:
                out["refreshed"] = True
            from .swarm_robots import filter_tombstoned_profiles

            out["profiles"] = filter_tombstoned_profiles(out.get("profiles") or [])
            return out
        except Exception as e:
            log.warning("Hermes profile list failed: %s", e)
            if user is not None:
                from .swarm_robots import filter_tombstoned_profiles

                return {
                    "ok": True,
                    "profiles": filter_tombstoned_profiles(
                        self._fallback_profiles_for_user(user)
                    ),
                    "dashboard_url": dashboard_url,
                    "fallback": True,
                    "warning": f"Hermes indisponível: {type(e).__name__}: {e}",
                }
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

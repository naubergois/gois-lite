"""Hermes kanban helpers and core board handlers."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import UserRecord
from .hermes_kanban import (
    apply_kanban_action,
    get_board,
    kanban_file_for_project,
    normalize_assignees,
    projects_from_profiles,
    resolve_assignee_profile_slug,
    resolve_board_paths,
    save_kanban_attachment,
    suggest_assignee_for_task,
    team_agents_from_profiles,
)

log = logging.getLogger(__name__)

_KANBAN_WORKDIR_CACHE_TTL = 60.0
_kanban_workdir_cache: dict[str, tuple[float, set[str]]] = {}


class MonitorHermesKanbanMixin:
    @staticmethod
    def _merge_kanban_projects(
        primary: list[dict[str, Any]], extra: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        # Build lookup maps for extra (local) entries by workdir and profile/team_id
        extra_by_workdir: dict[str, dict[str, Any]] = {}
        extra_by_profile: dict[str, dict[str, Any]] = {}
        for row in extra:
            if not isinstance(row, dict):
                continue
            wd = str(row.get("workdir") or "").strip()
            if wd:
                extra_by_workdir[wd] = row
            prof = str(row.get("team_id") or row.get("profile") or "").strip()
            if prof:
                extra_by_profile[prof] = row

        _ENRICH_KEYS = (
            "team_id", "profile_slugs", "swarm_name", "latex_workspace_id",
            "artifacts_dir", "whatsapp_group", "whatsapp_numbers", "github_repos", "site_links",
        )

        def _enrich(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
            """Copy missing fields from source into target (copy, not mutate)."""
            result = dict(target)
            for k in _ENRICH_KEYS:
                if result.get(k) is None and source.get(k) is not None:
                    result[k] = source[k]
            return result

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in extra + primary:
            if not isinstance(row, dict):
                continue
            key = str(row.get("workdir") or row.get("team_id") or row.get("profile") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            # If this is a hermes/primary entry, enrich with local data
            wd = str(row.get("workdir") or "").strip()
            prof = str(row.get("team_id") or row.get("profile") or "").strip()
            local_src = extra_by_workdir.get(wd) or extra_by_profile.get(prof)
            if local_src is not None and local_src is not row:
                row = _enrich(row, local_src)
            out.append(row)
        out.sort(
            key=lambda p: (
                0
                if str(p.get("team_id") or "").startswith("projeto-padrao")
                else 1,
                str(p.get("label", "")).lower(),
            )
        )
        return out

    @staticmethod
    def _merge_kanban_agents(
        primary: list[dict[str, str]], extra: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for row in extra + primary:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or "").strip()
            if not slug or slug.lower() in seen:
                continue
            seen.add(slug.lower())
            out.append(row)
        out.sort(key=lambda a: a.get("display_name", "").lower())
        return out

    def _fallback_kanban_projects_for_user(
        self, user: UserRecord
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Build Kanban projects from local teams when Hermes API is unavailable."""
        projects: list[dict[str, Any]] = []
        agents: list[dict[str, str]] = []
        seen_agents: set[str] = set()

        teams = (
            self.accounts.list_all_teams()
            if getattr(user, "is_admin", False)
            else self.accounts.list_teams(user.id)
        )
        from .accounts_team_guard import _scan_team_dirs

        _, _alias_map = _scan_team_dirs(self.accounts.data_dir)
        for team in teams:
            workdir = self.accounts.team_workdir(team).resolve()
            kanban_path = self.accounts.team_kanban_path(team.id).resolve()
            label = team.name.strip() or team.id
            if team.github_url:
                label = f"{label} — {team.github_url}"
            artifacts_dir = str(self.accounts.team_artifacts_dir(team))
            aliases = sorted(_alias_map.get(str(team.id or "").strip(), []))
            projects.append(
                {
                    "profile": team.id,
                    "team_id": team.id,
                    "workdir": str(workdir),
                    "label": label,
                    "kanban_file": kanban_file_for_project(workdir, kanban_path),
                    "github": team.github_url,
                    "artifacts_dir": artifacts_dir,
                    "whatsapp_group": team.whatsapp_group,
                    "whatsapp_numbers": list(team.whatsapp_numbers),
                    "github_repos": list(team.github_repos),
                    "site_links": list(team.site_links),
                    "latex_workspace_id": team.latex_workspace_id,
                    "profile_slugs": list(team.profile_slugs),
                    "swarm_name": team.swarm_name,
                    "team_aliases": aliases,
                }
            )

            for slug in team.profile_slugs:
                key = str(slug).strip()
                if not key or key in seen_agents:
                    continue
                seen_agents.add(key)
                agents.append({"slug": key, "display_name": key, "mascot": ""})

        projects.sort(key=lambda p: str(p.get("label", "")).lower())
        agents.sort(key=lambda a: a["display_name"].lower())
        return projects, agents

    @staticmethod
    def _path_under_or_equal(child: Path, base: Path) -> bool:
        try:
            child_r = child.resolve()
            base_r = base.resolve()
        except OSError:
            return False
        if child_r == base_r:
            return True
        try:
            return child_r.is_relative_to(base_r)
        except (ValueError, AttributeError):
            return str(child_r).startswith(str(base_r).rstrip("/") + "/")

    @staticmethod
    def _sanitize_workdir_input(path: str) -> str:
        raw = str(path or "").strip().strip('"').strip("'")
        if raw.lower().startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(raw)
            raw = unquote(parsed.path)
        return raw.strip()

    def _resolve_kanban_workdir(
        self, path: str, anchor: str = "", *, extra_anchors: Optional[list[str]] = None
    ) -> Optional[Path]:
        """Resolve a kanban/cron workdir; relative paths are resolved against anchor(s)."""
        raw = self._sanitize_workdir_input(path)
        if not raw:
            return None
        anchors: list[str] = []
        for item in (anchor, *(extra_anchors or [])):
            text = self._sanitize_workdir_input(str(item or ""))
            if text and text not in anchors:
                anchors.append(text)
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                return candidate.resolve()
            for anchor_raw in anchors:
                try:
                    return (Path(anchor_raw).expanduser().resolve() / candidate).resolve()
                except OSError:
                    continue
            try:
                return (Path.cwd() / candidate).resolve()
            except OSError:
                return None
        except OSError:
            return None

    def _kanban_allowed_workdirs(self, user: Optional[UserRecord]) -> set[str]:
        if user is None:
            return set()
        cache_key = str(user.id)
        now = time.time()
        cached = _kanban_workdir_cache.get(cache_key)
        if cached and now < cached[0]:
            return cached[1]
        allowed = self._build_kanban_allowed_workdirs(user)
        _kanban_workdir_cache[cache_key] = (now + _KANBAN_WORKDIR_CACHE_TTL, allowed)
        return allowed

    def _build_kanban_allowed_workdirs(self, user: UserRecord) -> set[str]:
        allowed: set[str] = set()
        teams = (
            self.accounts.list_all_teams()
            if getattr(user, "is_admin", False)
            else self.accounts.list_teams(user.id)
        )
        for t in teams:
            for base in (self.accounts.team_dir(t.id), self.accounts.team_workdir(t)):
                try:
                    allowed.add(str(base.resolve()))
                except OSError:
                    continue
        if self.cfg.hermes and self.cfg.hermes_agent_create.enabled:
            dashboard_url = self._hermes_dashboard_url()
            if dashboard_url:
                try:
                    profiles = self._cached_hermes_profiles(
                        dashboard_url,
                        user,
                        timeout=2.0,
                        enrich_local_meta=True,
                    )
                    for proj in projects_from_profiles(
                        profiles, self.cfg.hermes_agent_create
                    ):
                        wd = str(proj.get("workdir") or "").strip()
                        if not wd:
                            continue
                        try:
                            allowed.add(str(Path(wd).expanduser().resolve()))
                        except OSError:
                            allowed.add(wd)
                except Exception:
                    log.debug(
                        "kanban allowed workdirs: Hermes projects skipped",
                        exc_info=True,
                    )
        return allowed

    def _kanban_workdir_allowed(
        self,
        user: Optional[UserRecord],
        path: str,
        *,
        anchor_workdir: str = "",
    ) -> bool:
        if not self.cfg.auth.enabled or user is None:
            return True
        if self.cfg.hermes_agent_create.kanban_allow_any_workdir:
            return self._resolve_kanban_workdir(path, anchor_workdir) is not None
        resolved = self._resolve_kanban_workdir(path, anchor_workdir)
        if resolved is None:
            return False
        for base in self._kanban_allowed_workdirs(user):
            try:
                base_path = Path(base).resolve()
            except OSError:
                continue
            if self._path_under_or_equal(resolved, base_path):
                return True
        anchor_raw = str(anchor_workdir or "").strip()
        if not anchor_raw:
            return False
        anchor_resolved = self._resolve_kanban_workdir(anchor_raw, "")
        if anchor_resolved is None:
            return False
        if not self._path_under_or_equal(resolved, anchor_resolved):
            return False
        for base in self._kanban_allowed_workdirs(user):
            try:
                base_path = Path(base).resolve()
            except OSError:
                continue
            if self._path_under_or_equal(anchor_resolved, base_path):
                return True
        return False

    def _kanban_valid_assignee_slugs(
        self, dashboard_url: str, user: Optional[UserRecord]
    ) -> set[str]:
        slugs: set[str] = set()
        try:
            profiles = self._cached_hermes_profiles(dashboard_url, user)
            for p in profiles:
                if isinstance(p, dict):
                    name = str(p.get("name") or "").strip()
                    if name:
                        slugs.add(name)
        except Exception:
            pass
        if user is not None:
            for team in self.accounts.list_teams(user.id):
                for slug in team.profile_slugs:
                    key = str(slug).strip()
                    if key:
                        slugs.add(key)
        return slugs

    def _kanban_assignee_extra_slugs(self, user: Optional[UserRecord]) -> list[str]:
        extra: list[str] = []
        if user is not None:
            for team in self.accounts.list_teams(user.id):
                extra.extend(
                    str(s).strip() for s in team.profile_slugs if str(s).strip()
                )
        return extra

    def _resolve_kanban_assignee(
        self,
        assignee: str,
        user: Optional[UserRecord] = None,
    ) -> str:
        """Map slug or display name to canonical Hermes profile slug."""
        text = str(assignee or "").strip()
        if not text:
            return ""
        dashboard_url = self._hermes_dashboard_url()
        profiles: list[dict] = []
        if dashboard_url:
            try:
                profiles = self._cached_hermes_profiles(dashboard_url, user)
            except Exception:
                pass
        extra = self._kanban_assignee_extra_slugs(user)
        resolved = resolve_assignee_profile_slug(text, profiles, extra_slugs=extra)
        if resolved:
            return resolved
        if dashboard_url:
            if text in self._kanban_valid_assignee_slugs(dashboard_url, user):
                return text
        elif text in extra:
            return text
        return ""

    def _kanban_assignee_error(self, assignee: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": f"assignee inválido: {assignee}. Escolha um perfil existente.",
        }

"""Hermes skills, mascots, and local folder browser."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .hermes_project_agents import list_mascots
from .hermes_skills import list_development_skills

log = logging.getLogger(__name__)


class MonitorHermesCatalogMixin:
    def handle_hermes_skills_list(self) -> dict:
        """List development skills for the dashboard chat."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}
        cc = self.cfg.hermes_agent_create
        catalog = list_development_skills(categories=cc.skill_categories)
        catalog["dashboard_url"] = self._hermes_dashboard_url()
        return catalog

    def handle_swarm_skills_list(self) -> dict:
        """Hermes dev + qclaw-chat + OpenClaw skills for swarm agent profile editors."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}
        from .hermes_skills import list_swarm_skills_catalog

        cc = self.cfg.hermes_agent_create
        runtime = None
        try:
            runtime = self._openclaw_runtime()
        except Exception:
            runtime = None
        catalog = list_swarm_skills_catalog(
            categories=cc.skill_categories,
            runtime=runtime,
            include_openclaw=runtime is not None,
        )
        catalog["dashboard_url"] = self._hermes_dashboard_url()
        return catalog

    def handle_hermes_mascots_list(self) -> dict:
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}
        return {"ok": True, "mascots": list_mascots()}

    def handle_hermes_local_folders(self, query: dict) -> dict:
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}
        raw = str(query.get("path") or "").strip()
        go_up = str(query.get("up") or "").strip() in {"1", "true", "yes"}
        include_files = str(query.get("files") or query.get("include_files") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        base = Path(raw).expanduser() if raw else Path.home()
        try:
            current = base.resolve(strict=False)
            if go_up:
                current = current.parent
            if not current.is_dir():
                return {"ok": False, "error": f"pasta inválida: {current}"}
            dirs: list[dict[str, str]] = []
            files: list[dict[str, Any]] = []
            allowed_suffixes = (
                ".txt",
                ".md",
                ".json",
                ".yaml",
                ".yml",
                ".py",
                ".js",
                ".ts",
                ".tsx",
                ".jsx",
                ".sh",
                ".sql",
                ".html",
                ".css",
                ".xml",
                ".csv",
                ".log",
                ".pdf",
                ".tex",
                ".bib",
            )
            for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if child.is_dir():
                        dirs.append({"name": child.name, "path": str(child.resolve(strict=False))})
                    elif include_files and child.is_file():
                        lower = child.name.lower()
                        if lower.endswith(allowed_suffixes):
                            files.append(
                                {
                                    "name": child.name,
                                    "path": str(child.resolve(strict=False)),
                                    "size": child.stat().st_size,
                                }
                            )
                except PermissionError:
                    continue
            out: dict[str, Any] = {
                "ok": True,
                "current": str(current),
                "parent": str(current.parent) if current.parent != current else None,
                "directories": dirs,
            }
            if include_files:
                out["files"] = files
            return out
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

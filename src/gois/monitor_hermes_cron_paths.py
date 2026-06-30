"""Hermes cron jobs.json path resolution across profiles."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .hermes_cron import cron_gateway_restart_blockers, resolve_agent_log_path
from .hermes_profiles import list_hermes_profiles_filesystem

log = logging.getLogger(__name__)


class MonitorHermesCronPathsMixin:
    def _hermes_cron_jobs_path(self) -> Path:
        from .hermes_cron import resolve_hermes_cron_jobs_path

        return resolve_hermes_cron_jobs_path(self.cfg.hermes_cron_recovery.jobs_path)

    def _hermes_cron_snapshot_sources(self) -> list[tuple[str, Path]]:
        from .hermes_cron import hermes_home_from_jobs_path, resolve_hermes_cron_jobs_path

        sources: list[tuple[str, Path]] = []
        seen: set[Path] = set()

        def _add(source_profile: str, jobs_path: Path) -> None:
            resolved = jobs_path.expanduser().resolve()
            if resolved in seen or not resolved.is_file():
                return
            seen.add(resolved)
            sources.append((source_profile, resolved))

        primary_jobs_path = self._hermes_cron_jobs_path()
        _add("default", primary_jobs_path)
        try:
            primary_home = hermes_home_from_jobs_path(primary_jobs_path)
        except Exception:
            primary_home = primary_jobs_path.expanduser().resolve().parent.parent
        if primary_home != resolve_hermes_cron_jobs_path(None).expanduser().resolve().parent.parent:
            return sources
        for profile in list_hermes_profiles_filesystem():
            slug = str(profile.get("name") or "").strip()
            path = str(profile.get("path") or "").strip()
            if not slug or slug == "default" or not path:
                continue
            _add(slug, Path(path) / "cron" / "jobs.json")

        return sources

    def _resolve_hermes_cron_job_context(self, job_ref: str) -> dict[str, Any]:
        from .hermes_cron import resolve_cron_job_ref

        needle = str(job_ref or "").strip()
        if not needle:
            return {"ok": False, "error": "job id is required"}

        matches: list[dict[str, Any]] = []
        for source_profile, jobs_path in self._hermes_cron_snapshot_sources():
            resolved = resolve_cron_job_ref(needle, jobs_path)
            if not resolved.get("ok"):
                continue
            job = dict(resolved.get("job") or {})
            job["source_profile"] = source_profile
            job["source_jobs_path"] = str(jobs_path)
            matches.append(
                {
                    "job": job,
                    "job_id": resolved.get("job_id"),
                    "resolved_from": resolved.get("resolved_from"),
                    "source_profile": source_profile,
                    "jobs_path": jobs_path,
                }
            )

        if not matches:
            return {"ok": False, "error": f"job {needle!r} não encontrado"}
        if len(matches) > 1:
            return {
                "ok": False,
                "error": f"referência de job {needle!r} é ambígua em múltiplos profiles",
                "matches": [
                    {
                        "job_id": item["job_id"],
                        "name": item["job"].get("name"),
                        "source_profile": item["source_profile"],
                    }
                    for item in matches
                ],
            }
        return {"ok": True, **matches[0]}

    def _hermes_cron_gateway_restart_blockers(self) -> list[str]:
        jobs_path = self._hermes_cron_jobs_path()
        log_path = resolve_agent_log_path(
            self.cfg.hermes.log_paths if self.cfg.hermes else None
        )
        return cron_gateway_restart_blockers(jobs_path, log_path)


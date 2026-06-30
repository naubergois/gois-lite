"""Cron concurrency gate status and release."""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class MonitorCronConcurrencyMixin:
    def _cron_concurrency_workspace(self):
        from .cron_concurrency import resolve_cron_workspace

        return resolve_cron_workspace(
            self.cfg.cron_concurrency,
            ruflo_cfg=self.cfg.ruflo_chat,
            runtime=self._openclaw_runtime(),
        )

    def handle_cron_concurrency_status(self, query: Optional[dict] = None) -> dict[str, Any]:
        from .cron_concurrency import enrich_status_with_running_jobs, read_status
        from .hermes_cron import detect_running_cron_jobs, resolve_agent_log_path

        cc = self.cfg.cron_concurrency
        if not cc.enabled:
            return {"ok": False, "error": "cron concurrency dashboard disabled"}
        q = query or {}
        prune = str(q.get("prune") or "").lower() in ("1", "true", "yes")
        try:
            status = read_status(
                cc,
                workspace=self._cron_concurrency_workspace(),
                prune_first=prune,
            )
            log_path = resolve_agent_log_path(self.cfg.hermes.log_paths if self.cfg.hermes else None)
            if log_path is not None and self.cfg.hermes:
                jobs_path = self._hermes_cron_jobs_path()
                running = detect_running_cron_jobs(log_path, jobs_path)
                status = enrich_status_with_running_jobs(status, running)
            return status
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_cron_concurrency_release(self, payload: dict) -> dict[str, Any]:
        from .cron_concurrency import release_slot

        cc = self.cfg.cron_concurrency
        if not cc.enabled:
            return {"ok": False, "error": "cron concurrency dashboard disabled"}
        name = str(payload.get("name") or payload.get("agent") or "").strip()
        try:
            return release_slot(
                cc,
                workspace=self._cron_concurrency_workspace(),
                agent_name=name,
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


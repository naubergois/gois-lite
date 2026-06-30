"""Hermes dashboard URL helpers and cron pause for agent create."""

from __future__ import annotations

import logging

import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .accounts import UserRecord
from .hermes_cron import (
    CronJobsPauseSnapshot,
    pause_all_active_hermes_cron_jobs,
    resume_hermes_cron_jobs_from_snapshot,
)
from .hermes_profiles import fetch_hermes_session_token, hermes_dashboard_http_up

log = logging.getLogger(__name__)


class MonitorHermesDashboardHelpersMixin:

    def _hermes_dashboard_url(self) -> Optional[str]:
        if not self.cfg.hermes:
            return None
        return self.cfg.hermes.dashboard_url or "http://127.0.0.1:9119"

    def _hermes_dashboard_create_preflight(self) -> Optional[dict[str, Any]]:
        """Fail fast before LLM/cron work when the Hermes dashboard API is unreachable."""
        dashboard_url = self._hermes_dashboard_url()
        if not dashboard_url:
            return {"ok": False, "error": "hermes is not configured"}
        if not hermes_dashboard_http_up(dashboard_url):
            return {
                "ok": False,
                "error": (
                    f"Dashboard Hermes offline em {dashboard_url}. "
                    "Aguarde o monitor subir o dashboard ou execute: "
                    "hermes dashboard --no-open"
                ),
            }
        try:
            fetch_hermes_session_token(dashboard_url, timeout=5.0)
        except Exception as e:
            return {
                "ok": False,
                "error": (
                    f"Não foi possível autenticar no dashboard Hermes ({dashboard_url}): "
                    f"{type(e).__name__}: {e}"
                ),
            }
        return None

    def _wait_hermes_dashboard_for_create(self) -> Optional[dict[str, Any]]:
        """Poll until the Hermes dashboard API accepts requests (after cron pause)."""
        hac = self.cfg.hermes_agent_create
        deadline = time.monotonic() + hac.dashboard_ready_wait_seconds
        last_error = "dashboard not ready"
        while True:
            preflight = self._hermes_dashboard_create_preflight()
            if preflight is None:
                return None
            last_error = str(preflight.get("error") or last_error)
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "error": (
                        f"Dashboard Hermes não ficou pronto em "
                        f"{hac.dashboard_ready_wait_seconds:.0f}s: {last_error}"
                    ),
                }
            time.sleep(max(0.5, hac.dashboard_ready_poll_seconds))

    @contextmanager
    def _pause_hermes_crons_for_agent_create(
        self,
    ) -> Iterator[Optional[CronJobsPauseSnapshot]]:
        """Pause active cron jobs for the duration of Hermes agent creation."""
        hac = self.cfg.hermes_agent_create
        if not self.cfg.hermes or not hac.pause_cron_jobs_during_create:
            yield None
            return

        jobs_path = self._hermes_cron_jobs_path()
        snapshot, meta = pause_all_active_hermes_cron_jobs(
            jobs_path,
            accept_hooks=hac.cron_accept_hooks,
            timeout_seconds=min(hac.cron_timeout_seconds, 120.0),
        )
        paused = int(meta.get("paused_count") or 0)
        if paused:
            log.info("paused %d Hermes cron job(s) for agent create", paused)
        if meta.get("failures"):
            log.warning(
                "Hermes cron pause failures during agent create: %s",
                meta["failures"],
            )
        try:
            yield snapshot
        finally:
            if snapshot.paused_job_ids:
                resume_meta = resume_hermes_cron_jobs_from_snapshot(
                    snapshot,
                    jobs_path,
                    accept_hooks=hac.cron_accept_hooks,
                    timeout_seconds=min(hac.cron_timeout_seconds, 120.0),
                )
                self._invalidate_hermes_cron_cache()
                log.info(
                    "resumed %d Hermes cron job(s) after agent create",
                    resume_meta.get("resumed_count", 0),
                )
                if resume_meta.get("failures"):
                    log.warning(
                        "Hermes cron resume failures after agent create: %s",
                        resume_meta["failures"],
                    )

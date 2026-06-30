"""Swarm-only Hermes cron enforcement for GoisMonitor."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .swarm_cron_policy import enforce_swarm_only_cron_jobs, swarm_only_cron_guard

log = logging.getLogger(__name__)


class MonitorSwarmCronPolicyMixin:
    def _swarm_only_cron_enabled(self) -> bool:
        if not self.cfg.hermes:
            return False
        return bool(self.cfg.hermes_cron_recovery.swarm_only)

    def _swarm_only_cron_guard(self, job: Optional[dict[str, Any]], *, job_id: str = "") -> Optional[dict[str, Any]]:
        if not self._swarm_only_cron_enabled():
            return None
        return swarm_only_cron_guard(
            job,
            jobs_path=self._hermes_cron_jobs_path(),
            job_id=job_id,
        )

    def _enforce_swarm_only_cron_jobs(self) -> dict[str, Any]:
        if not self._swarm_only_cron_enabled():
            return {"ok": True, "enabled": False, "paused_count": 0}
        cc = self.cfg.hermes_cron_recovery
        return enforce_swarm_only_cron_jobs(
            self._hermes_cron_jobs_path(),
            accept_hooks=cc.accept_hooks,
            timeout_seconds=min(cc.timeout_seconds, 120.0),
        )

    def _maybe_enforce_swarm_only_cron_jobs(self) -> None:
        if not self._swarm_only_cron_enabled():
            return
        now = time.time()
        if now < self._swarm_only_cron_enforce_ts + 45.0:
            return
        self._swarm_only_cron_enforce_ts = now
        try:
            result = self._enforce_swarm_only_cron_jobs()
        except Exception as exc:
            log.warning("swarm-only cron enforcement failed: %s", exc)
            return
        if result.get("paused_count"):
            self._invalidate_hermes_cron_cache()
            log.info(
                "swarm-only cron enforcement: %s",
                result.get("paused") or result.get("summary") or result,
            )

    async def _swarm_only_cron_loop(self) -> None:
        await asyncio.sleep(min(10.0, self.cfg.monitor.interval_seconds))
        while True:
            agent = self.tracker.get("swarm_cron_policy")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("swarm_cron_policy") as a:
                        result = await asyncio.to_thread(self._enforce_swarm_only_cron_jobs)
                        paused = int(result.get("paused_count") or 0)
                        if paused:
                            self._invalidate_hermes_cron_cache()
                        a.last_result = (
                            f"paused {paused} non-swarm job(s)"
                            if paused
                            else "ok"
                        )
                except Exception as exc:
                    log.warning("swarm-only cron loop failed: %s", exc)
            await asyncio.sleep(max(60.0, self.cfg.monitor.interval_seconds))

"""QClaw and Hermes health tick handlers."""

from __future__ import annotations

import logging
import time

from .processes import list_qclaw_processes, memory_summary

log = logging.getLogger(__name__)

_PROCESS_REFRESH_SEC = 60.0


class MonitorTicksMixin:

    async def _refresh_processes_cache(self, *, force: bool = False) -> None:
        now = time.time()
        if (
            not force
            and self._processes_cache
            and (now - getattr(self, "_processes_cache_ts", 0.0)) < _PROCESS_REFRESH_SEC
        ):
            return
        try:
            self._processes_cache = await list_qclaw_processes()
            self._processes_cache_ts = now
        except Exception as e:
            log.warning("processes refresh failed: %s", e)

    async def _tick(self) -> bool:
        now = time.time()
        result = await self.recovery.health_check()
        ok = bool(result.get("ok"))
        await self._refresh_processes_cache(force=not ok)
        if self.metrics and self._processes_cache:
            mem = memory_summary(self._processes_cache)
            self.metrics.memory_rss_total_bytes.set(mem["total_bytes"])
            for role, bytes_used in mem["by_role_bytes"].items():
                self.metrics.memory_rss_bytes.labels(role=role).set(bytes_used)

        if self.metrics:
            self.metrics.health_checks_total.labels(result="ok" if ok else "fail").inc()
            self.metrics.up.set(1 if ok else 0)

        if ok:
            if self.state.consecutive_failures:
                await self.notifier.notify(
                    "info",
                    f"{self.cfg.qclaw.name} recovered",
                    f"after {self.state.consecutive_failures} consecutive failures",
                )
            self.state.consecutive_failures = 0
            self.state.last_health_ok_ts = now
            if self.metrics:
                self.metrics.last_health_ok_ts.set(now)
                self.metrics.consecutive_failures.set(0)
            self._persist()
            return True

        self.state.consecutive_failures += 1
        self.state.last_health_fail_ts = now
        self.state.last_failure_summary = self.notifier.dump(result)
        if self.metrics:
            self.metrics.consecutive_failures.set(self.state.consecutive_failures)

        log.warning(
            "health check failed (%d/%d): %s",
            self.state.consecutive_failures,
            self.cfg.monitor.failure_threshold,
            self.state.last_failure_summary,
        )
        self._persist()

        if self.state.consecutive_failures < self.cfg.monitor.failure_threshold:
            return False
        await self._maybe_trigger_recovery(result)
        return False

    async def _hermes_tick(self) -> bool:
        if not self.hermes_recovery:
            return True
        now = time.time()
        result = await self.hermes_recovery.health_check()
        ok = bool(result.get("ok"))
        self._hermes_last_ok = ok
        await self._refresh_processes_cache(force=not ok)

        if self.metrics:
            self.metrics.hermes_up.set(1 if ok else 0)

        if ok:
            if self.state.hermes_consecutive_failures:
                await self.notifier.notify(
                    "info",
                    f"{self.cfg.hermes.name} recovered",
                    f"after {self.state.hermes_consecutive_failures} consecutive failures",
                )
            self.state.hermes_consecutive_failures = 0
            self.state.hermes_last_health_ok_ts = now
            if self.metrics:
                self.metrics.hermes_consecutive_failures.set(0)
            self._persist()
            return True

        self.state.hermes_consecutive_failures += 1
        self.state.hermes_last_health_fail_ts = now
        self.state.hermes_last_failure_summary = self.notifier.dump(result)
        if self.metrics:
            self.metrics.hermes_consecutive_failures.set(
                self.state.hermes_consecutive_failures
            )

        log.warning(
            "hermes health check failed (%d/%d): %s",
            self.state.hermes_consecutive_failures,
            (
                self.cfg.hermes_keepalive.failure_threshold
                if self.cfg.hermes_keepalive.enabled
                else self.cfg.monitor.failure_threshold
            ),
            self.state.hermes_last_failure_summary,
        )
        self._persist()

        threshold = (
            self.cfg.hermes_keepalive.failure_threshold
            if self.cfg.hermes_keepalive.enabled
            else self.cfg.monitor.failure_threshold
        )
        if self.state.hermes_consecutive_failures < threshold:
            return False
        await self._maybe_restart_hermes(
            last_result=result,
            reason="health-check failure threshold",
        )
        return False

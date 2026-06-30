"""Recovery agents and Hermes keepalive triggers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .agent import run_recovery_agent
from .hermes_cron_diagnostic_agent import (
    CRON_DIAGNOSTIC_TOOL_NAMES,
    build_hermes_cron_diagnostic_extras,
    default_hermes_cron_diagnostic_system_prompt,
)
from .hermes_cron_recovery_agent import (
    CRON_SCHEDULER_RECOVERY_TOOL_NAMES,
    build_hermes_cron_scheduler_recovery_extras,
    default_hermes_cron_scheduler_recovery_system_prompt,
)
from .hermes_recovery_agent import build_hermes_recovery_extras
from .hermes_cron import is_transient_cron_scheduler_error, wait_cron_tick_idle

log = logging.getLogger(__name__)

_HERMES_LOG_PREFIX = ".hermes/"


class MonitorRecoveryTriggersMixin:

    async def _maybe_restart_hermes(
        self,
        last_result: dict,
        reason: str = "health-check failure threshold",
        *,
        force: bool = False,
    ) -> None:
        """Direct `hermes gateway start` keepalive — no LLM agent."""
        if not self.cfg.hermes or not self.hermes_recovery:
            return
        kc = self.cfg.hermes_keepalive
        hra = self.cfg.hermes_recovery_agent
        if hra.enabled and hra.use_llm_instead_of_keepalive and not force:
            await self._maybe_trigger_hermes_recovery(last_result, reason=reason)
            return
        if not kc.enabled and not force:
            await self._maybe_trigger_hermes_recovery(last_result, reason=reason)
            return
        if not self.tracker.get("recovery_hermes").enabled:
            self.tracker.get("recovery_hermes").state = "paused"
            log.info("hermes keepalive paused; skipping restart")
            return
        now = time.time()
        last = self.state.hermes_last_recovery_ts or 0.0
        cooldown = kc.cooldown_seconds if kc.enabled else self.cfg.monitor.cooldown_seconds
        if now - last < cooldown:
            remaining = cooldown - (now - last)
            log.info("hermes: in cooldown (%.1fs remaining), skipping restart", remaining)
            return
        blockers = await asyncio.to_thread(self._hermes_cron_gateway_restart_blockers)
        if blockers and not force:
            log.info(
                "hermes: adiando restart do gateway — %s (reason=%s)",
                "; ".join(blockers),
                reason,
            )
            return
        max_attempts = (
            kc.max_recovery_attempts if kc.enabled else self.cfg.monitor.max_recovery_attempts
        )
        if self._hermes_recovery_attempts >= max_attempts:
            await self.notifier.notify(
                "error",
                f"{self.cfg.hermes.name}: max keepalive attempts reached",
                f"giving up after {self._hermes_recovery_attempts} attempts this run; "
                "restart the monitor to retry",
            )
            return

        self.state.hermes_last_recovery_ts = now
        self._hermes_recovery_attempts += 1
        if self.metrics:
            self.metrics.recovery_attempts_total.inc()
        self._persist()

        await self.notifier.notify(
            "warning",
            f"{self.cfg.hermes.name} down — restarting gateway",
            f"reason: {reason}\n"
            f"attempt {self._hermes_recovery_attempts}/{max_attempts}\n"
            f"last health: {self.notifier.dump(last_result)}",
        )

        report = ""
        verify: dict = {}
        ok = False
        try:
            async with self.tracker.track("recovery_hermes") as a:
                a.current_step = "hermes gateway start"
                report = await self.hermes_recovery.restart()
                a.current_step = "verify health"
                await asyncio.sleep(5.0)
                verify = await self.hermes_recovery.health_check()
                ok = bool(verify.get("ok"))
                self._hermes_last_ok = ok
                a.last_result = "up" if ok else "still down after restart"
        except Exception as e:
            log.exception("hermes keepalive crashed: %s", e)
            if self.metrics:
                self.metrics.recovery_agent_errors_total.inc()
            await self.notifier.notify(
                "error",
                f"{self.cfg.hermes.name} keepalive crashed",
                f"{type(e).__name__}: {e}",
            )
            return

        self.state.hermes_last_recovery_report = report
        if ok:
            self.state.hermes_consecutive_failures = 0
            self.state.hermes_last_health_ok_ts = time.time()
            if self.metrics:
                self.metrics.hermes_consecutive_failures.set(0)
                self.metrics.hermes_up.set(1)
        self._persist()
        level = "info" if ok else "warning"
        await self.notifier.notify(
            level,
            f"{self.cfg.hermes.name} keepalive {'ok' if ok else 'still failing'}",
            f"{report}\n\nverify: {self.notifier.dump(verify)}",
        )
        if (
            not ok
            and hra.enabled
            and hra.llm_enabled
            and hra.escalate_after_keepalive_failure
        ):
            await self._maybe_trigger_hermes_recovery(
                verify or last_result,
                reason="keepalive restart did not restore health",
            )

    async def _maybe_trigger_recovery(
        self, last_result: dict, reason: str = "health-check failure threshold"
    ) -> None:
        if not self.tracker.get("recovery").enabled:
            self.tracker.get("recovery").state = "paused"
            log.info("recovery agent is paused; skipping recovery trigger")
            return
        now = time.time()
        last = self.state.last_recovery_ts or 0.0
        if now - last < self.cfg.monitor.cooldown_seconds:
            remaining = self.cfg.monitor.cooldown_seconds - (now - last)
            log.info("in cooldown (%.1fs remaining), skipping recovery", remaining)
            return
        if self._recovery_attempts >= self.cfg.monitor.max_recovery_attempts:
            await self.notifier.notify(
                "error",
                f"{self.cfg.qclaw.name}: max recovery attempts reached",
                f"giving up after {self._recovery_attempts} attempts this run; "
                "restart the monitor to retry",
            )
            return

        self.state.last_recovery_ts = now
        self._recovery_attempts += 1
        if self.metrics:
            self.metrics.recovery_attempts_total.inc()
            self.metrics.last_recovery_ts.set(now)
        self._persist()

        await self.notifier.notify(
            "warning",
            f"{self.cfg.qclaw.name} unhealthy — invoking recovery agent",
            f"reason: {reason}\n"
            f"attempt {self._recovery_attempts}/{self.cfg.monitor.max_recovery_attempts}\n"
            f"last health: {self.notifier.dump(last_result)}",
        )

        try:
            async with self.tracker.track("recovery") as a:
                def _set_step(s):
                    a.current_step = s
                    if s is not None:
                        a.last_step = s
                failure_payload = (
                    f"reason: {reason}\n"
                    + self.notifier.dump(last_result)
                )
                report = await run_recovery_agent(
                    self.cfg.agent,
                    self.recovery,
                    failure_summary=failure_payload,
                    on_step=_set_step,
                    doctor_cfg=self.cfg.openclaw_doctor,
                )
                a.last_result = (report.splitlines()[0] if report else "(empty)")[:160]
        except Exception as e:
            log.exception("recovery agent crashed: %s", e)
            if self.metrics:
                self.metrics.recovery_agent_errors_total.inc()
            await self.notifier.notify(
                "error",
                f"{self.cfg.qclaw.name} recovery agent crashed",
                f"{type(e).__name__}: {e}",
            )
            return

        self.state.last_recovery_report = report
        self._persist()
        await self.notifier.notify(
            "info",
            f"{self.cfg.qclaw.name} recovery agent report",
            report,
        )


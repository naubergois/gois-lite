"""Hermes LLM recovery, openclaw doctor, and cron retry triggers."""

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


class MonitorRecoveryMiscMixin:
    async def _maybe_trigger_hermes_recovery(
        self, last_result: dict, reason: str = "health-check failure threshold"
    ) -> None:
        if not self.cfg.hermes or not self.hermes_recovery:
            return
        hra = self.cfg.hermes_recovery_agent
        if not hra.enabled or not hra.llm_enabled:
            log.info("hermes LLM recovery agent disabled; skipping")
            return
        if not self.tracker.get("recovery_hermes").enabled:
            self.tracker.get("recovery_hermes").state = "paused"
            log.info("hermes recovery agent is paused; skipping recovery trigger")
            return
        now = time.time()
        last = self.state.hermes_last_recovery_ts or 0.0
        if now - last < self.cfg.monitor.cooldown_seconds:
            remaining = self.cfg.monitor.cooldown_seconds - (now - last)
            log.info("hermes: in cooldown (%.1fs remaining), skipping recovery", remaining)
            return
        if self._hermes_recovery_attempts >= self.cfg.monitor.max_recovery_attempts:
            await self.notifier.notify(
                "error",
                f"{self.cfg.hermes.name}: max recovery attempts reached",
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
            f"{self.cfg.hermes.name} unhealthy — invoking recovery agent",
            f"reason: {reason}\n"
            f"attempt {self._hermes_recovery_attempts}/{self.cfg.monitor.max_recovery_attempts}\n"
            f"last health: {self.notifier.dump(last_result)}",
        )

        try:
            async with self.tracker.track("recovery_hermes") as a:
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
                    self.hermes_recovery,
                    failure_summary=failure_payload,
                    on_step=_set_step,
                    hermes_extras=build_hermes_recovery_extras(self),
                    system_prompt=hra.system_prompt,
                )
                a.last_result = (report.splitlines()[0] if report else "(empty)")[:160]
        except Exception as e:
            log.exception("hermes recovery agent crashed: %s", e)
            if self.metrics:
                self.metrics.recovery_agent_errors_total.inc()
            await self.notifier.notify(
                "error",
                f"{self.cfg.hermes.name} recovery agent crashed",
                f"{type(e).__name__}: {e}",
            )
            return

        self.state.hermes_last_recovery_report = report
        self._persist()
        await self.notifier.notify(
            "info",
            f"{self.cfg.hermes.name} recovery agent report",
            report,
        )

    async def _maybe_run_openclaw_doctor(self, matches: list) -> None:
        """Trigger `openclaw doctor --fix` when a log match names a pattern in
        openclaw_doctor.trigger_patterns. Honors cooldown + single-flight."""
        dc = self.cfg.openclaw_doctor
        if not dc.enabled or not dc.trigger_patterns:
            return
        agent = self.tracker.get("openclaw_doctor")
        if not agent.enabled:
            agent.state = "paused"
            return
        triggered = next(
            (m for m in matches if m.pattern in dc.trigger_patterns), None
        )
        if triggered is None:
            return
        now = time.time()
        last = self.state.last_doctor_ts or 0.0
        if now - last < dc.cooldown_seconds:
            remaining = dc.cooldown_seconds - (now - last)
            log.info(
                "openclaw_doctor: in cooldown (%.1fs remaining) — skipping (trigger=%s)",
                remaining, triggered.pattern,
            )
            return
        log.info(
            "openclaw_doctor: pattern %r matched — running `openclaw doctor --fix`",
            triggered.pattern,
        )
        try:
            async with self.tracker.track("openclaw_doctor") as a:
                a.current_step = f"doctor --fix (trigger={triggered.pattern})"
                doctor_result = await self.recovery.openclaw_doctor_fix(dc)
                a.last_result = (
                    f"ok rc={doctor_result.get('rc')}"
                    if doctor_result.get("ok")
                    else f"fail: {doctor_result.get('reason') or doctor_result.get('summary')}"
                )[:160]
        except Exception as e:
            log.exception("openclaw_doctor crashed: %s", e)
            await self.notifier.notify(
                "error",
                f"{self.cfg.qclaw.name} openclaw doctor crashed",
                f"{type(e).__name__}: {e}",
            )
            return
        self.state.last_doctor_ts = now
        self.state.last_doctor_ok = bool(doctor_result.get("ok"))
        self.state.last_doctor_trigger = triggered.pattern
        self.state.last_doctor_summary = (
            doctor_result.get("summary") or doctor_result.get("reason") or ""
        )[:300]
        self._persist()
        level = "info" if doctor_result.get("ok") else "warning"
        body = (
            f"trigger: {triggered.pattern} ({triggered.file})\n"
            f"rc: {doctor_result.get('rc')}\n"
            f"summary: {self.state.last_doctor_summary}\n"
        )
        if doctor_result.get("stdout_tail"):
            body += f"stdout (tail):\n{doctor_result['stdout_tail'][-800:]}\n"
        if doctor_result.get("stderr_tail"):
            body += f"stderr (tail):\n{doctor_result['stderr_tail'][-800:]}\n"
        await self.notifier.notify(
            level,
            f"{self.cfg.qclaw.name} openclaw doctor --fix {'ok' if doctor_result.get('ok') else 'failed'}",
            body,
        )

    async def _maybe_retry_hermes_cron(self, matches: list) -> None:
        """Re-run a failed Hermes cron job when a log match names a trigger pattern."""
        cc = self.cfg.hermes_cron_recovery
        if not cc.enabled or not cc.trigger_patterns or not self.hermes_recovery:
            return
        agent = self.tracker.get("hermes_cron_retry")
        if not agent.enabled:
            agent.state = "paused"
            return
        triggered = next(
            (
                m
                for m in matches
                if m.pattern in cc.trigger_patterns
                and _HERMES_LOG_PREFIX in m.file.replace("\\", "/")
            ),
            None,
        )
        if triggered is None:
            return
        now = time.time()
        last = self.state.last_hermes_cron_retry_ts or 0.0
        if now - last < cc.cooldown_seconds:
            remaining = cc.cooldown_seconds - (now - last)
            log.info(
                "hermes_cron_retry: in cooldown (%.1fs remaining) — skipping (trigger=%s)",
                remaining,
                triggered.pattern,
            )
            return
        log.info(
            "hermes_cron_retry: pattern %r matched — re-running failed cron job",
            triggered.pattern,
        )
        if is_transient_cron_scheduler_error(triggered.line):
            jobs_path = self._hermes_cron_jobs_path()
            idle = await asyncio.to_thread(
                wait_cron_tick_idle,
                jobs_path,
                timeout_seconds=90.0,
            )
            if not idle:
                log.warning(
                    "hermes_cron_retry: tick ainda ativo após espera — adiando retry"
                )
                return
            if cc.ensure_gateway and self.hermes_recovery:
                await self.hermes_recovery.ensure_hermes_cron_gateway(cc)
                await asyncio.sleep(5.0)
        retry_result: dict = {}
        try:
            async with self.tracker.track("hermes_cron_retry") as a:
                a.current_step = f"cron run (trigger={triggered.pattern})"
                retry_result = await self.hermes_recovery.hermes_cron_retry(
                    cc, triggered.line
                )
                a.last_result = (
                    f"ok job={retry_result.get('job_id')}"
                    if retry_result.get("ok")
                    else f"fail: {retry_result.get('reason') or retry_result.get('summary')}"
                )[:160]
        except Exception as e:
            log.exception("hermes_cron_retry crashed: %s", e)
            await self.notifier.notify(
                "error",
                f"{self.cfg.hermes.name} cron retry crashed",
                f"{type(e).__name__}: {e}",
            )
            return
        self.state.last_hermes_cron_retry_ts = now
        self.state.last_hermes_cron_retry_ok = bool(retry_result.get("ok"))
        self.state.last_hermes_cron_job_id = retry_result.get("job_id")
        self.state.last_hermes_cron_job_name = retry_result.get("job_name")
        self.state.last_hermes_cron_summary = (
            retry_result.get("summary") or retry_result.get("reason") or ""
        )[:300]
        self._persist()
        level = "info" if retry_result.get("ok") else "warning"
        body = (
            f"trigger: {triggered.pattern} ({triggered.file})\n"
            f"job: {retry_result.get('job_name') or '?'} "
            f"({retry_result.get('job_id') or '?'})\n"
            f"rc: {retry_result.get('rc')}\n"
            f"summary: {self.state.last_hermes_cron_summary}\n"
        )
        if retry_result.get("stdout_tail"):
            body += f"stdout (tail):\n{retry_result['stdout_tail'][-800:]}\n"
        if retry_result.get("stderr_tail"):
            body += f"stderr (tail):\n{retry_result['stderr_tail'][-800:]}\n"
        job_id = retry_result.get("job_id")
        await self._maybe_trigger_hermes_cron_diagnostic(
            reason=f"log match {triggered.pattern}",
            job_id=str(job_id) if job_id else None,
            trigger="log_match",
        )
        await self.notifier.notify(
            level,
            (
                f"{self.cfg.hermes.name} cron retry "
                f"{'ok' if retry_result.get('ok') else 'failed'}"
            ),
            body,
        )

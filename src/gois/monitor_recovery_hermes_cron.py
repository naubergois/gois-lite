"""Hermes cron diagnostic/scheduler recovery and retry agents."""

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


class MonitorRecoveryHermesCronMixin:
    async def _run_hermes_cron_diagnostic_agent(
        self,
        report: dict[str, Any],
        *,
        reason: str,
        job_id: Optional[str] = None,
    ) -> str:
        """Invoke the read-only cron diagnostic LLM agent."""
        if not self.cfg.hermes or not self.hermes_recovery:
            return "hermes not configured"
        cda = self.cfg.hermes_cron_diagnostic_agent
        if not cda.enabled:
            return "cron diagnostic agent disabled"
        if not cda.llm_enabled:
            healthy = bool(report.get("healthy"))
            likely = str(report.get("likely_cause") or "").strip() or "indefinida"
            issues = report.get("issues") if isinstance(report.get("issues"), list) else []
            recs = report.get("recommendations") if isinstance(report.get("recommendations"), list) else []
            scheduler = report.get("scheduler") if isinstance(report.get("scheduler"), dict) else {}
            jobs = report.get("jobs") if isinstance(report.get("jobs"), dict) else {}
            active_jobs = scheduler.get("active_jobs")
            stale_jobs = jobs.get("stale_job_count")
            lines = [
                "## Diagnóstico (sem LLM)",
                f"reason: {reason}",
                f"job_id: {job_id or '(all)'}",
                f"status: {'healthy' if healthy else 'degraded'}",
                f"causa provável: {likely}",
            ]
            if active_jobs is not None:
                lines.append(f"active_jobs: {active_jobs}")
            if stale_jobs is not None:
                lines.append(f"stale_jobs: {stale_jobs}")
            if issues:
                lines.append("\nIssues:")
                for item in issues[:6]:
                    if not isinstance(item, dict):
                        continue
                    sev = str(item.get("severity") or "info")
                    msg = str(item.get("message") or "")
                    if msg:
                        lines.append(f"- [{sev}] {msg}")
            if recs:
                lines.append("\nRecomendações:")
                for rec in recs[:6]:
                    txt = str(rec or "").strip()
                    if txt:
                        lines.append(f"- {txt}")
            lines.append("\nmode: prebuilt-no-llm")
            text = "\n".join(lines).strip()
            self.state.last_hermes_cron_diagnostic_ts = time.time()
            self.state.last_hermes_cron_diagnostic_healthy = healthy
            self.state.last_hermes_cron_diagnostic_ok = True
            self.state.last_hermes_cron_diagnostic_report = text[:2000]
            self._persist()
            return text
        extras = build_hermes_cron_diagnostic_extras(self)
        if extras is None:
            return "cron diagnostic extras unavailable"

        now = time.time()
        self.state.last_hermes_cron_diagnostic_ts = now
        self.state.last_hermes_cron_diagnostic_healthy = bool(report.get("healthy"))
        self._persist()

        import json as _json

        failure_payload = (
            f"reason: {reason}\n"
            f"job_id: {job_id or '(all)'}\n"
            f"structured_report:\n{_json.dumps(report, default=str)[:6000]}\n"
        )
        try:
            async with self.tracker.track("hermes_cron_diagnostic") as a:

                def _set_step(s: Optional[str]) -> None:
                    a.current_step = s
                    if s is not None:
                        a.last_step = s

                text = await run_recovery_agent(
                    self.cfg.agent,
                    self.hermes_recovery,
                    failure_summary=failure_payload,
                    on_step=_set_step,
                    cron_diagnostic_extras=extras,
                    system_prompt=(
                        cda.system_prompt
                        or default_hermes_cron_diagnostic_system_prompt()
                    ),
                    tool_allowlist=CRON_DIAGNOSTIC_TOOL_NAMES,
                )
                a.last_result = (text.splitlines()[0] if text else "(empty)")[:160]
        except Exception as e:
            log.exception("hermes cron diagnostic agent crashed: %s", e)
            if self.metrics:
                self.metrics.recovery_agent_errors_total.inc()
            self.state.last_hermes_cron_diagnostic_ok = False
            self.state.last_hermes_cron_diagnostic_report = (
                f"{type(e).__name__}: {e}"
            )[:500]
            self._persist()
            raise

        self.state.last_hermes_cron_diagnostic_ok = True
        self.state.last_hermes_cron_diagnostic_report = text[:2000]
        self._persist()
        return text

    async def _maybe_trigger_hermes_cron_diagnostic(
        self,
        *,
        reason: str,
        job_id: Optional[str] = None,
        trigger: str = "log_match",
    ) -> None:
        cda = self.cfg.hermes_cron_diagnostic_agent
        if not cda.enabled:
            return
        if trigger == "log_match" and not cda.auto_run_on_log_match:
            return
        if trigger == "scheduler_down" and not cda.auto_run_on_scheduler_down:
            return
        if not self.tracker.get("hermes_cron_diagnostic").enabled:
            self.tracker.get("hermes_cron_diagnostic").state = "paused"
            return
        now = time.time()
        last = self.state.last_hermes_cron_diagnostic_ts or 0.0
        if now - last < cda.cooldown_seconds:
            log.info(
                "cron diagnostic: cooldown %.1fs remaining",
                cda.cooldown_seconds - (now - last),
            )
            return
        if self._hermes_cron_diagnostic_attempts >= cda.max_attempts_per_run:
            log.warning("cron diagnostic: max attempts per run reached")
            return
        self._hermes_cron_diagnostic_attempts += 1
        report = self._hermes_cron_diagnostic_cache
        if not report:
            report = await self._refresh_hermes_cron_diagnostic_report(job_id=job_id)
        try:
            text = await self._run_hermes_cron_diagnostic_agent(
                report,
                reason=reason,
                job_id=job_id,
            )
        except Exception:
            return
        await self.notifier.notify(
            "info" if self.state.last_hermes_cron_diagnostic_ok else "warning",
            "Hermes cron diagnostic agent",
            text[:1500],
        )

    async def _run_hermes_cron_scheduler_recovery_agent(
        self,
        probe: dict[str, Any],
        *,
        reason: str,
    ) -> str:
        """Invoke the cron-focused LLM recovery agent; returns final report text."""
        if not self.cfg.hermes or not self.hermes_recovery:
            return "hermes not configured"
        csa = self.cfg.hermes_cron_scheduler_agent
        if not csa.enabled:
            return "cron scheduler recovery agent disabled"
        if not csa.llm_enabled:
            out = await self.hermes_recovery.ensure_hermes_cron_gateway(
                self.cfg.hermes_cron_recovery
            )
            probe_after = await self._refresh_hermes_cron_scheduler_probe()
            ok = bool(probe_after.get("ok"))
            self.state.last_hermes_cron_scheduler_recovery_ts = time.time()
            self.state.last_hermes_cron_scheduler_recovery_ok = ok
            lines = [
                "## Recuperação cron scheduler (sem LLM)",
                f"reason: {reason}",
                f"probe_before_ok: {bool(probe.get('ok'))}",
                f"ensure_action: {str(out.get('action') or 'none')}",
                f"probe_after_ok: {ok}",
                f"summary: {str(probe_after.get('summary') or out.get('summary') or '')}",
                "mode: prebuilt-no-llm",
            ]
            report = "\n".join(lines).strip()
            self.state.last_hermes_cron_scheduler_recovery_report = report[:2000]
            self._persist()
            return report
        extras = build_hermes_cron_scheduler_recovery_extras(self)
        if extras is None:
            return "cron scheduler recovery extras unavailable"
        diag_extras = build_hermes_cron_diagnostic_extras(
            self, require_agent_enabled=False
        )

        now = time.time()
        self.state.last_hermes_cron_scheduler_recovery_ts = now
        self._persist()

        diag_report = self._hermes_cron_diagnostic_cache
        if not diag_report:
            diag_report = await self._refresh_hermes_cron_diagnostic_report()

        import json as _json

        failure_payload = (
            f"reason: {reason}\n"
            f"scheduler probe: {self.notifier.dump(probe)}\n"
            f"jobs_path: {self.cfg.hermes_cron_recovery.jobs_path}\n"
            f"structured_diagnostic:\n{_json.dumps(diag_report, default=str)[:6000]}\n"
        )
        try:
            async with self.tracker.track("recovery_hermes_cron_scheduler") as a:

                def _set_step(s: Optional[str]) -> None:
                    a.current_step = s
                    if s is not None:
                        a.last_step = s

                report = await run_recovery_agent(
                    self.cfg.agent,
                    self.hermes_recovery,
                    failure_summary=failure_payload,
                    on_step=_set_step,
                    hermes_extras=extras,
                    cron_diagnostic_extras=diag_extras,
                    system_prompt=(
                        csa.system_prompt
                        or default_hermes_cron_scheduler_recovery_system_prompt()
                    ),
                    tool_allowlist=CRON_SCHEDULER_RECOVERY_TOOL_NAMES,
                )
                a.last_result = (report.splitlines()[0] if report else "(empty)")[:160]
        except Exception as e:
            log.exception("hermes cron scheduler recovery agent crashed: %s", e)
            if self.metrics:
                self.metrics.recovery_agent_errors_total.inc()
            self.state.last_hermes_cron_scheduler_recovery_ok = False
            self.state.last_hermes_cron_scheduler_recovery_report = (
                f"{type(e).__name__}: {e}"
            )[:500]
            self._persist()
            raise

        probe_after = await self._refresh_hermes_cron_scheduler_probe()
        ok = bool(probe_after.get("ok"))
        self.state.last_hermes_cron_scheduler_recovery_ok = ok
        self.state.last_hermes_cron_scheduler_recovery_report = report[:2000]
        self._persist()
        return report

    async def _maybe_trigger_hermes_cron_scheduler_recovery(
        self,
        probe: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Run the cron-scheduler LLM agent when automatic ensure did not fix firing."""
        csa = self.cfg.hermes_cron_scheduler_agent
        if not csa.enabled or not csa.auto_recover:
            return
        if not self.tracker.get("recovery_hermes_cron_scheduler").enabled:
            self.tracker.get("recovery_hermes_cron_scheduler").state = "paused"
            return
        now = time.time()
        last = self.state.last_hermes_cron_scheduler_recovery_ts or 0.0
        if now - last < csa.cooldown_seconds:
            log.info(
                "cron scheduler recovery: cooldown %.1fs remaining",
                csa.cooldown_seconds - (now - last),
            )
            return
        if self._hermes_cron_scheduler_recovery_attempts >= csa.max_attempts_per_run:
            log.warning("cron scheduler recovery: max attempts per run reached")
            return

        self._hermes_cron_scheduler_recovery_attempts += 1
        await self.notifier.notify(
            "warning",
            "Hermes cron scheduler — agente de recuperação",
            f"reason: {reason}\n{self.notifier.dump(probe)[:800]}",
        )
        try:
            report = await self._run_hermes_cron_scheduler_recovery_agent(
                probe, reason=reason
            )
        except Exception:
            return
        await self.notifier.notify(
            "info" if self.state.last_hermes_cron_scheduler_recovery_ok else "warning",
            "Relatório agente cron scheduler",
            report[:1500],
        )


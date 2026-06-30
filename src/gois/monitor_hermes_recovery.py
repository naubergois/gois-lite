"""Hermes dashboard/gateway recovery actions."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .hermes_cron_diagnostic_agent import (
    HERMES_CRON_DIAGNOSTIC_PRESET_ID,
    ensure_hermes_cron_diagnostic_profile,
)
from .hermes_cron_recovery_agent import HERMES_CRON_RECOVERY_PRESET_ID
from .hermes_recovery_agent import ensure_hermes_recovery_profile

log = logging.getLogger(__name__)


class MonitorHermesRecoveryMixin:
    def handle_hermes_recovery_status(self) -> dict[str, Any]:
        """Hermes recovery agent + dashboard health for the main monitor UI."""
        if not self.cfg.hermes or not self.hermes_recovery:
            return {"ok": False, "error": "hermes is not configured"}
        hra = self.cfg.hermes_recovery_agent
        profile_id = (hra.profile_id or "hermes-recovery").strip()
        dash_url = self.cfg.hermes.dashboard_url or "http://127.0.0.1:9119"
        dash_up = self._hermes_dashboard_up
        if dash_up is None:
            dash_up = False
        from .hermes_profiles import hermes_profiles_root

        root = hermes_profiles_root()
        profile_dir = root / profile_id
        csa = self.cfg.hermes_cron_scheduler_agent
        cron_prof = (csa.profile_id or HERMES_CRON_RECOVERY_PRESET_ID).strip()
        cron_profile_dir = root / cron_prof
        cda = self.cfg.hermes_cron_diagnostic_agent
        diag_prof = (cda.profile_id or HERMES_CRON_DIAGNOSTIC_PRESET_ID).strip()
        diag_profile_dir = root / diag_prof
        probe = self._hermes_cron_scheduler_probe or {}
        return {
            "ok": True,
            "profile_id": profile_id,
            "profile_installed": profile_dir.is_dir(),
            "profiles_root": str(root),
            "dashboard_url": dash_url,
            "dashboard_up": dash_up,
            "gateway_up": self._hermes_is_up(),
            "profile_url": f"{dash_url.rstrip('/')}/profiles",
            "recovery_enabled": hra.enabled,
            "cron_scheduler_agent": {
                "enabled": csa.enabled,
                "llm_enabled": csa.llm_enabled,
                "profile_id": cron_prof,
                "profile_installed": cron_profile_dir.is_dir(),
                "scheduler_ok": probe.get("ok"),
                "hermes_home": probe.get("hermes_home"),
            },
            "cron_diagnostic_agent": {
                "enabled": cda.enabled,
                "llm_enabled": cda.llm_enabled,
                "profile_id": diag_prof,
                "profile_installed": diag_profile_dir.is_dir(),
            },
        }

    def handle_hermes_recovery_action(self, payload: dict) -> dict[str, Any]:
        """Restart Hermes dashboard/gateway or ensure the recovery profile exists."""
        return asyncio.run(self._hermes_recovery_action_async(payload))

    async def _hermes_recovery_action_async(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.hermes or not self.hermes_recovery:
            return {"ok": False, "error": "hermes is not configured"}
        action = str(payload.get("action") or "").strip().lower()
        hra = self.cfg.hermes_recovery_agent
        if action == "ensure_profile":
            result = await asyncio.to_thread(
                ensure_hermes_recovery_profile,
                profile_id=hra.profile_id,
                template_profile=self.cfg.hermes_agent_create.seed_role_catalog_template_profile,
            )
            return {"ok": bool(result.get("ok", True)), **result}
        if action == "restart_dashboard":
            stop = await self.hermes_recovery.stop_hermes_dashboard()
            start_cmd = self._hermes_dashboard_start_command()
            spawn = await self.hermes_recovery.start_hermes_dashboard(start_cmd)
            up = await self.hermes_recovery.wait_hermes_dashboard_up(
                timeout_seconds=self.cfg.hermes_dashboard.startup_timeout_seconds,
            )
            self._hermes_dashboard_up = up
            self._hermes_dashboard_last_start_ts = time.time()
            return {
                "ok": up,
                "action": action,
                "stop": stop,
                "spawn": spawn,
                "dashboard_up": up,
                "dashboard_url": self.cfg.hermes.dashboard_url,
            }
        if action == "restart_gateway":
            report = await self.hermes_recovery.restart()
            await asyncio.sleep(3.0)
            verify = await self.hermes_recovery.health_check()
            ok = bool(verify.get("ok"))
            return {
                "ok": ok,
                "action": action,
                "report": report,
                "health": verify,
            }
        if action in {"ensure_cron_scheduler", "ensure_cron_gateway"}:
            cc = self.cfg.hermes_cron_recovery
            result = await self.hermes_recovery.ensure_hermes_cron_gateway(cc)
            self.state.last_hermes_cron_gateway_ensure_ts = time.time()
            self.state.last_hermes_cron_gateway_ok = bool(result.get("ok"))
            self.state.last_hermes_cron_gateway_summary = (
                result.get("summary") or result.get("reason") or ""
            )[:300]
            self._hermes_cron_scheduler_probe = dict(result)
            self._persist()
            return {"ok": bool(result.get("ok")), "action": action, **result}
        if action in {"run_cron_scheduler_agent", "recover_cron_scheduler"}:
            probe = await self._refresh_hermes_cron_scheduler_probe()
            report = await self._run_hermes_cron_scheduler_recovery_agent(
                probe,
                reason="manual dashboard action",
            )
            return {
                "ok": bool(self.state.last_hermes_cron_scheduler_recovery_ok),
                "action": action,
                "report": report,
                "scheduler": self._hermes_cron_scheduler_probe,
            }
        if action in {"run_cron_problematic_jobs_agent", "recover_cron_problematic_jobs"}:
            probe = await self._refresh_hermes_cron_scheduler_probe()
            snap = self._cached_hermes_cron_snapshot()
            error_jobs = [
                j
                for j in (snap.get("jobs") or [])
                if isinstance(j, dict)
                and j.get("active")
                and str(j.get("last_status") or "") == "error"
            ]
            names = [str(j.get("name") or j.get("id") or "?").strip() for j in error_jobs][:8]
            reason = (
                "manual dashboard action: resolve all problematic cron jobs"
                + (f" (count={len(error_jobs)})" if error_jobs else "")
                + (f" targets={', '.join(names)}" if names else "")
            )
            report = await self._run_hermes_cron_scheduler_recovery_agent(
                probe,
                reason=reason,
            )
            return {
                "ok": bool(self.state.last_hermes_cron_scheduler_recovery_ok),
                "action": action,
                "target_error_jobs": len(error_jobs),
                "report": report,
                "scheduler": self._hermes_cron_scheduler_probe,
            }
        if action == "ensure_cron_profile":
            csa = self.cfg.hermes_cron_scheduler_agent
            result = await asyncio.to_thread(
                ensure_hermes_cron_recovery_profile,
                profile_id=csa.profile_id,
                template_profile=self.cfg.hermes_agent_create.seed_role_catalog_template_profile,
            )
            return {"ok": bool(result.get("ok", True)), **result}
        if action in {"diagnose_cron", "run_cron_diagnostic_agent"}:
            job_id = str(payload.get("job_id") or "").strip() or None
            if action == "diagnose_cron":
                report = self.build_hermes_cron_diagnostic_report(job_id=job_id)
                return {"ok": True, "action": action, "report": report}
            text = await self._run_hermes_cron_diagnostic_agent(
                report=self.build_hermes_cron_diagnostic_report(job_id=job_id),
                reason="manual dashboard action",
                job_id=job_id,
            )
            return {
                "ok": bool(self.state.last_hermes_cron_diagnostic_ok),
                "action": action,
                "llm_report": text,
                "structured": self.build_hermes_cron_diagnostic_report(job_id=job_id),
            }
        if action == "ensure_cron_diagnostic_profile":
            cda = self.cfg.hermes_cron_diagnostic_agent
            result = await asyncio.to_thread(
                ensure_hermes_cron_diagnostic_profile,
                profile_id=cda.profile_id,
                template_profile=self.cfg.hermes_agent_create.seed_role_catalog_template_profile,
            )
            return {"ok": bool(result.get("ok", True)), **result}
        return {"ok": False, "error": f"unknown action {action!r}"}


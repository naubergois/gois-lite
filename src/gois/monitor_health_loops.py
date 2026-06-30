"""Core health, Hermes, reaper, and log-scanner asyncio loops."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

from .hermes_cron import probe_hermes_cron_scheduler

log = logging.getLogger(__name__)


class MonitorHealthLoopsMixin:
    async def _health_loop(self) -> None:
        while True:
            if not self.tracker.get("health").enabled:
                self.tracker.get("health").state = "paused"
            else:
                try:
                    async with self.tracker.track("health") as a:
                        ok = await self._tick()
                        a.last_result = "up" if ok else f"fail ({self.state.consecutive_failures}/{self.cfg.monitor.failure_threshold})"
                except Exception as e:
                    log.exception("health tick crashed: %s", e)
            await asyncio.sleep(self.cfg.monitor.interval_seconds)

    async def _model_quota_loop(self) -> None:
        await asyncio.sleep(min(10.0, self.cfg.monitor.interval_seconds))
        while True:
            agent = self.tracker.get("model_quota")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("model_quota") as a:
                        result = self._enforce_model_daily_quotas()
                        if result.get("blocked"):
                            exceeded = result.get("exceeded_models") or []
                            a.last_result = f"blocked ({len(exceeded)} modelo(s) excedido(s))"
                        elif result.get("enabled"):
                            a.last_result = "ok"
                        else:
                            a.last_result = "disabled"
                except Exception as exc:
                    log.warning("model quota enforcement failed: %s", exc)
            await asyncio.sleep(max(30.0, self.cfg.monitor.interval_seconds))

    async def _hermes_health_loop(self) -> None:
        first = True
        while True:
            if not first:
                await asyncio.sleep(self.cfg.monitor.interval_seconds)
            first = False
            if not self.tracker.get("health_hermes").enabled:
                self.tracker.get("health_hermes").state = "paused"
            else:
                try:
                    async with self.tracker.track("health_hermes") as a:
                        ok = await self._hermes_tick()
                        a.last_result = (
                            "up"
                            if ok
                            else (
                                f"fail ({self.state.hermes_consecutive_failures}/"
                                f"{self.cfg.monitor.failure_threshold})"
                            )
                        )
                except Exception as e:
                    log.exception("hermes health tick crashed: %s", e)

    async def _refresh_hermes_cron_scheduler_probe(self) -> dict[str, Any]:
        jobs_path = self._hermes_cron_jobs_path()
        probe = await asyncio.to_thread(probe_hermes_cron_scheduler, jobs_path)
        self._hermes_cron_scheduler_probe = dict(probe)
        return probe

    async def _hermes_cron_scheduler_handle_down(
        self,
        probe: dict[str, Any],
        *,
        agent_ctx: Any = None,
    ) -> None:
        """Try ensure gateway, then auto-invoke the cron recovery LLM agent if still down."""
        cc = self.cfg.hermes_cron_recovery
        csa = self.cfg.hermes_cron_scheduler_agent
        now = time.time()
        last_ensure = self.state.last_hermes_cron_gateway_ensure_ts or 0.0
        in_ensure_cooldown = now - last_ensure < cc.ensure_gateway_cooldown_seconds
        ensure_ran = False
        ensure_ok = False

        if not self.hermes_recovery:
            if agent_ctx is not None:
                agent_ctx.last_result = "scheduler down (no recovery)"
            return

        if not in_ensure_cooldown and cc.ensure_gateway:
            if agent_ctx is not None:
                agent_ctx.current_step = "ensure cron gateway"
            ensure_ran = True
            result = await self.hermes_recovery.ensure_hermes_cron_gateway(cc)
            self.state.last_hermes_cron_gateway_ensure_ts = now
            ensure_ok = bool(result.get("ok"))
            self.state.last_hermes_cron_gateway_ok = ensure_ok
            self.state.last_hermes_cron_gateway_summary = (
                result.get("summary") or result.get("reason") or ""
            )[:300]
            self._persist()
            probe = await self._refresh_hermes_cron_scheduler_probe()
            if agent_ctx is not None:
                agent_ctx.last_result = (
                    f"ensure {'ok' if ensure_ok else 'fail'}: "
                    f"{self.state.last_hermes_cron_gateway_summary}"
                )[:160]
            if not ensure_ok:
                await self.notifier.notify(
                    "warning",
                    "Hermes cron scheduler down",
                    (
                        f"jobs_path: {cc.jobs_path}\n"
                        f"hermes_home: {result.get('hermes_home')}\n"
                        f"{self.state.last_hermes_cron_gateway_summary}\n"
                        "Verifique se HERMES_HOME no .env coincide com jobs_path; "
                        "o agente hermes-cron-recovery pode corrigir automaticamente."
                    ),
                )
        else:
            probe = await self._refresh_hermes_cron_scheduler_probe()
            if agent_ctx is not None:
                if in_ensure_cooldown:
                    agent_ctx.last_result = "scheduler down (ensure cooldown)"
                else:
                    agent_ctx.last_result = "scheduler down"

        if probe.get("ok"):
            self._hermes_cron_scheduler_recovery_attempts = 0
            self._hermes_cron_diagnostic_attempts = 0
            if agent_ctx is not None:
                agent_ctx.last_result = (
                    f"scheduler ok ({probe.get('active_jobs', '?')} jobs)"
                )
            return

        await self._refresh_hermes_cron_diagnostic_report()
        await self._maybe_trigger_hermes_cron_diagnostic(
            reason="scheduler down",
            trigger="scheduler_down",
        )

        if not csa.enabled or not csa.auto_recover or not csa.llm_enabled:
            return

        if ensure_ran and not ensure_ok:
            reason = "cron scheduler gateway down after ensure"
        elif in_ensure_cooldown and not ensure_ran:
            reason = "cron scheduler down (ensure cooldown)"
        else:
            reason = "cron scheduler down"
        await self._maybe_trigger_hermes_cron_scheduler_recovery(probe, reason=reason)

    async def _hermes_cron_gateway_loop(self) -> None:
        cc = self.cfg.hermes_cron_recovery
        await asyncio.sleep(min(30.0, cc.ensure_gateway_interval_seconds))
        while True:
            agent = self.tracker.get("hermes_cron_gateway")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("hermes_cron_gateway") as a:
                        probe = await self._refresh_hermes_cron_scheduler_probe()
                        if probe.get("ok"):
                            a.last_result = (
                                f"scheduler ok ({probe.get('active_jobs', '?')} jobs)"
                            )
                            await asyncio.to_thread(
                                self._maybe_repair_recurring_cron_jobs
                            )
                        else:
                            await self._hermes_cron_scheduler_handle_down(probe, agent_ctx=a)
                except Exception as e:
                    log.exception("hermes cron gateway tick crashed: %s", e)
            await asyncio.sleep(cc.ensure_gateway_interval_seconds)

    async def _hermes_cron_maintenance_loop(self) -> None:
        """Periodically recompute ``next_run_at`` for recurring Hermes crons."""
        cc = self.cfg.hermes_cron_recovery
        await asyncio.sleep(min(60.0, cc.recurring_repair_interval_seconds))
        while True:
            agent = self.tracker.get("hermes_cron_maintenance")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("hermes_cron_maintenance") as a:
                        result = await asyncio.to_thread(
                            lambda: self._maybe_repair_recurring_cron_jobs(
                                force=True
                            ),
                        )
                        repaired = int(result.get("repaired") or 0)
                        stale = int(result.get("stale_after") or 0)
                        if result.get("skipped"):
                            a.last_result = "cooldown"
                        elif repaired or stale:
                            a.last_result = (
                                f"repaired {repaired}, stale {stale}"
                            )[:160]
                        else:
                            a.last_result = "ok"
                except Exception as e:
                    log.exception("hermes cron maintenance tick crashed: %s", e)
            await asyncio.sleep(max(60.0, cc.recurring_repair_interval_seconds))

    async def _hermes_dashboard_loop(self) -> None:
        while True:
            if not self.tracker.get("hermes_dashboard").enabled:
                self.tracker.get("hermes_dashboard").state = "paused"
            else:
                try:
                    async with self.tracker.track("hermes_dashboard") as a:
                        up = await self._ensure_hermes_dashboard()
                        a.last_result = "up" if up else "down (spawn attempted)"
                except Exception as e:
                    log.exception("hermes dashboard tick crashed: %s", e)
            await asyncio.sleep(self.cfg.hermes_dashboard.interval_seconds)

    def _hermes_dashboard_start_command(self) -> list[str]:
        """Build start command with --host/--port taken from dashboard_url."""
        from .hermes_cron import hermes_dashboard_argv

        hdc = self.cfg.hermes_dashboard
        extras = [
            a
            for a in hdc.start_command
            if a not in ("hermes", "dashboard", "--profile", "default")
        ]
        cmd = hermes_dashboard_argv(*extras)
        url = self.cfg.hermes.dashboard_url or "http://127.0.0.1:9119"
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9119
        if "--port" not in cmd:
            cmd.extend(["--host", host, "--port", str(port)])
        return cmd

    async def _ensure_hermes_dashboard(self) -> bool:
        """Start `hermes dashboard` when down; respect cooldown between spawns."""
        if not self.cfg.hermes or not self.hermes_recovery:
            return False
        hdc = self.cfg.hermes_dashboard
        url = self.cfg.hermes.dashboard_url or "http://127.0.0.1:9119"
        up = await self.hermes_recovery.hermes_dashboard_up()
        self._hermes_dashboard_up = up
        if up:
            return True
        now = time.time()
        if now - self._hermes_dashboard_last_start_ts < hdc.cooldown_seconds:
            remaining = hdc.cooldown_seconds - (now - self._hermes_dashboard_last_start_ts)
            log.info(
                "hermes dashboard down at %s; cooldown %.1fs remaining",
                url,
                remaining,
            )
            return False
        self._hermes_dashboard_last_start_ts = now
        stop_report = await self.hermes_recovery.stop_hermes_dashboard()
        if "stopped" in stop_report.lower():
            log.info("hermes dashboard: cleared stale process(es) before spawn")
        start_cmd = self._hermes_dashboard_start_command()
        report = await self.hermes_recovery.start_hermes_dashboard(start_cmd)
        log.info("hermes dashboard spawn: %s", report.splitlines()[0] if report else report)
        up = await self.hermes_recovery.wait_hermes_dashboard_up(
            timeout_seconds=hdc.startup_timeout_seconds,
        )
        self._hermes_dashboard_up = up
        if up:
            log.info("hermes dashboard up at %s", url)
        else:
            log.warning(
                "hermes dashboard still down at %s after spawn (see ~/.hermes/logs/dashboard-spawn.log)",
                url,
            )
        return up

    async def _reaper_loop(self) -> None:
        # Stagger so the first reaper run doesn't collide with first health tick.
        await asyncio.sleep(min(5.0, self.cfg.reaper.interval_seconds))
        while True:
            if not self.tracker.get("reaper").enabled:
                self.tracker.get("reaper").state = "paused"
            else:
                try:
                    async with self.tracker.track("reaper") as a:
                        summary = await self._reap_once()
                        a.last_result = summary
                except Exception as e:
                    log.exception("reaper tick crashed: %s", e)
            await asyncio.sleep(self.cfg.reaper.interval_seconds)

    async def _log_scanner_loop(self) -> None:
        # Small stagger so first scan doesn't collide with the first health tick.
        await asyncio.sleep(min(3.0, self.cfg.log_scanner.interval_seconds))
        while True:
            if not self.tracker.get("log_scanner").enabled:
                self.tracker.get("log_scanner").state = "paused"
            else:
                try:
                    async with self.tracker.track("log_scanner") as a:
                        result = await self.log_scanner.scan_once()
                        a.last_result = result.summary()
                        await self._handle_scan_result(result)
                except Exception as e:
                    log.exception("log scanner crashed: %s", e)
            await asyncio.sleep(self.cfg.log_scanner.interval_seconds)

    async def _runtime_integrity_loop(self) -> None:
        """Periodically reconcile Redis runtime blobs with legacy JSON mirrors."""
        from .runtime_integrity import reconcile_split_brain, runtime_stores_available

        interval = max(60.0, float(self.cfg.alerting.interval_seconds) * 2)
        await asyncio.sleep(min(30.0, interval))
        while True:
            agent = self.tracker.get("runtime_integrity")
            if not agent.enabled:
                agent.state = "paused"
            else:
                try:
                    async with self.tracker.track("runtime_integrity") as a:
                        if not runtime_stores_available():
                            a.last_result = "file-only (mongo/redis down)"
                        else:
                            result = await asyncio.to_thread(
                                reconcile_split_brain,
                                self._runtime_legacy_paths(),
                            )
                            reconciled = result.get("reconciled") or []
                            remaining = result.get("remaining") or []
                            if reconciled:
                                direction = result.get("direction") or "?"
                                a.last_result = (
                                    f"reconciled {len(reconciled)} key(s) ({direction})"
                                )
                                log.info(
                                    "runtime integrity: reconciled %d key(s) %s",
                                    len(reconciled),
                                    reconciled,
                                )
                            elif remaining:
                                a.last_result = (
                                    f"split-brain {len(remaining)} key(s) unresolved"
                                )
                            else:
                                a.last_result = "ok"
                except Exception as e:
                    log.exception("runtime integrity loop crashed: %s", e)
            await asyncio.sleep(interval)

    async def _ruflo_memory_maintenance_loop(self) -> None:
        """Checkpoint WAL, verify integrity, and rotate backups for RuFlo memory.db."""
        from .local_paths import _repo_root
        from .ruflo_memory_guard import maintain_memory_db
        from .ruflo_memory_repair import resolve_ruflo_memory_db_path

        sm = getattr(self.cfg, "swarm_memory", None)
        rc = getattr(self.cfg, "ruflo_chat", None)
        if not sm or not getattr(sm, "db_maintenance_enabled", True):
            agent = self.tracker.get("ruflo_memory_maintenance")
            agent.enabled = False
            agent.state = "paused"
            return
        interval = max(300.0, float(getattr(sm, "db_maintenance_interval_seconds", 3600.0) or 3600.0))
        await asyncio.sleep(min(60.0, interval))
        while True:
            agent = self.tracker.get("ruflo_memory_maintenance")
            if not getattr(sm, "db_maintenance_enabled", True):
                agent.enabled = False
                agent.state = "paused"
            elif not (sm.enabled or (rc and rc.enabled)):
                agent.state = "paused"
                agent.last_result = "ruflo/swarm memory disabled"
            else:
                try:
                    async with self.tracker.track("ruflo_memory_maintenance") as a:
                        db_path = resolve_ruflo_memory_db_path(
                            swarm_memory_cfg=sm,
                            ruflo_chat_cfg=rc,
                            repo_root=_repo_root(),
                        )
                        if db_path is None:
                            a.last_result = "no memory db path"
                        else:
                            result = await asyncio.to_thread(
                                maintain_memory_db,
                                db_path,
                                min_free_mb=int(getattr(sm, "db_min_free_mb", 512) or 512),
                                backup_keep=int(getattr(sm, "db_backup_keep", 3) or 3),
                                auto_repair=True,
                            )
                            if result.get("repair", {}).get("repaired"):
                                a.last_result = "repaired corrupted db"
                                log.warning("ruflo memory maintenance repaired corrupted db")
                            elif not result.get("ok"):
                                a.last_result = f"fail ({result.get('error')})"
                            elif result.get("backup_path"):
                                a.last_result = "checkpoint + backup ok"
                            else:
                                a.last_result = "ok"
                except Exception as exc:
                    log.exception("ruflo memory maintenance loop crashed: %s", exc)
            await asyncio.sleep(interval)


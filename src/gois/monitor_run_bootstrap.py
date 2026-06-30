"""Monitor ``run()`` task orchestration (startup hooks + asyncio tasks)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

_BACKGROUND_RESTART_SECONDS = 5.0
_BACKGROUND_RESTART_MAX_SECONDS = 120.0


class MonitorRunBootstrapMixin:
    def _spawn_background_loop(
        self,
        name: str,
        loop_fn: Callable[[], Awaitable[None]],
    ) -> asyncio.Task:
        """Supervise a long-running loop — restart after unexpected exit or crash."""

        async def _supervised() -> None:
            delay = _BACKGROUND_RESTART_SECONDS
            while True:
                try:
                    await loop_fn()
                    log.error(
                        "background loop %r exited unexpectedly — restarting in %.1fs",
                        name,
                        delay,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception(
                        "background loop %r crashed: %s — restarting in %.1fs",
                        name,
                        exc,
                        delay,
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, _BACKGROUND_RESTART_MAX_SECONDS)

        return asyncio.create_task(_supervised(), name=name)

    def _spawn_startup_warmup(
        self,
        name: str,
        warm_fn: Callable[[], Awaitable[None]],
    ) -> asyncio.Task:
        """Run a one-shot cache warmup without restarting after completion."""

        async def _run_once() -> None:
            try:
                await warm_fn()
            except Exception as exc:
                log.warning("startup warmup %r failed: %s", name, exc)

        return asyncio.create_task(_run_once(), name=name)

    async def run(self) -> None:
        log.info(
            "gois starting name=%s url=%s interval=%ss threshold=%d reaper=%s hermes=%s",
            self.cfg.qclaw.name,
            self.cfg.qclaw.health_url,
            self.cfg.monitor.interval_seconds,
            self.cfg.monitor.failure_threshold,
            self.cfg.reaper.enabled,
            bool(self.cfg.hermes),
        )
        await self._leader_startup()
        leader = self._leader_active()
        from .gois_lite import lite_startup_task_enabled

        if leader:
            # One-shot retroactive stagger for existing Hermes cron jobs that
            # share a minute slot. Best-effort: never block startup on failures.
            try:
                self._apply_cron_stagger_on_startup()
            except Exception as e:
                log.warning("cron stagger startup pass crashed: %s", e)
            try:
                self._maybe_repair_recurring_cron_jobs()
            except Exception as e:
                log.warning("recurring cron repair startup pass crashed: %s", e)
            if lite_startup_task_enabled("enforce_swarm_only_cron"):
                try:
                    self._enforce_swarm_only_cron_jobs()
                except Exception as e:
                    log.warning("swarm-only cron enforcement startup pass crashed: %s", e)
            try:
                self._resume_interrupted_chat_jobs()
            except Exception as e:
                log.warning("chat job resume on startup failed: %s", e)
            if lite_startup_task_enabled("bootstrap_skills_mcp"):
                try:
                    self._bootstrap_skills_mcp()
                except Exception as e:
                    log.warning("skills mcp bootstrap on startup failed: %s", e)
            if lite_startup_task_enabled("cleanup_rv_orphans"):
                try:
                    self._cleanup_rv_orphans_on_startup()
                except Exception as e:
                    log.warning("rv orphan cleanup on startup failed: %s", e)
            if lite_startup_task_enabled("repair_ruflo_memory"):
                try:
                    self._repair_ruflo_memory_db_on_startup()
                except Exception as e:
                    log.warning("ruflo memory repair on startup failed: %s", e)
            if lite_startup_task_enabled("ensure_ruflo_daemon"):
                try:
                    self._ensure_ruflo_daemon_on_startup()
                except Exception as e:
                    log.warning("ruflo daemon ensure on startup failed: %s", e)
            if lite_startup_task_enabled("verify_ruflo_cli"):
                try:
                    self._verify_ruflo_cli_on_startup()
                except Exception as e:
                    log.warning("ruflo cli verify on startup failed: %s", e)
            if lite_startup_task_enabled("warmup_embedded_roteiro_viral"):
                try:
                    from .roteiro_viral_local.embedded_worker import warmup_embedded_roteiro_viral

                    await warmup_embedded_roteiro_viral()
                except Exception as e:
                    log.warning("roteiro viral embedded startup failed: %s", e)
        else:
            log.info("standby instance — skipping one-shot startup mutations")

        tasks = [self._spawn_leader_loop("health", self._health_loop)]
        if self.cfg.hermes and self.hermes_recovery:
            tasks.append(
                self._spawn_leader_loop("health_hermes", self._hermes_health_loop)
            )
        if (
            self.cfg.hermes
            and self.hermes_recovery
            and self.cfg.hermes_dashboard.enabled
            and lite_startup_task_enabled("ensure_hermes_dashboard")
        ):
            if leader:
                await self._ensure_hermes_dashboard()
            tasks.append(
                self._spawn_leader_loop(
                    "hermes_dashboard", self._hermes_dashboard_loop
                )
            )
        if (
            self.cfg.hermes
            and self.cfg.hermes_agent_create.enabled
            and self.cfg.hermes_agent_create.seed_role_catalog_on_start
        ):
            tasks.append(
                self._spawn_leader_loop(
                    "role_catalog_seed", self._startup_seed_role_catalog
                )
            )
        if self.cfg.reaper.enabled:
            tasks.append(self._spawn_leader_loop("reaper", self._reaper_loop))
        tasks.append(
            self._spawn_startup_warmup(
                "active_agents_warm", self._startup_warm_active_agents_snapshot
            )
        )
        tasks.append(
            self._spawn_startup_warmup(
                "status_snapshot_warm", self._startup_warm_status_snapshot
            )
        )
        if self.cfg.ruflo_chat.enabled:
            tasks.append(
                self._spawn_startup_warmup(
                    "ruflo_status_warm", self._startup_warm_ruflo_status_snapshot
                )
            )
        if self.cfg.hermes and self.cfg.hermes_agent_create.enabled:
            tasks.append(
                self._spawn_startup_warmup(
                    "swarm_robots_warm", self._startup_warm_swarm_robots_snapshot
                )
            )
        if self.cfg.hermes:
            tasks.append(
                self._spawn_startup_warmup(
                    "hermes_cron_snapshot_warm",
                    self._startup_warm_hermes_cron_snapshot,
                )
            )
            tasks.append(
                self._spawn_startup_warmup(
                    "hermes_cron_token_stats_warm",
                    self._startup_warm_hermes_cron_token_stats,
                )
            )
            tasks.append(
                self._spawn_leader_loop("model_quota", self._model_quota_loop)
            )
            if self.cfg.hermes_cron_recovery.swarm_only:
                tasks.append(
                    self._spawn_leader_loop(
                        "swarm_cron_policy", self._swarm_only_cron_loop
                    )
                )
        if self.cfg.log_scanner.enabled and self.cfg.log_scanner.paths:
            tasks.append(
                self._spawn_leader_loop("log_scanner", self._log_scanner_loop)
            )
        if (
            leader
            and self.cfg.hermes
            and self.hermes_recovery
            and self.cfg.hermes_cron_recovery.enabled
        ):
            try:
                probe = await self._refresh_hermes_cron_scheduler_probe()
                if not probe.get("ok"):
                    await self._hermes_cron_scheduler_handle_down(probe)
            except Exception as e:
                log.warning("initial cron scheduler probe failed: %s", e)
        if (
            self.cfg.hermes
            and self.hermes_recovery
            and self.cfg.hermes_cron_recovery.enabled
            and self.cfg.hermes_cron_recovery.ensure_gateway
        ):
            tasks.append(
                self._spawn_leader_loop(
                    "hermes_cron_gateway", self._hermes_cron_gateway_loop
                )
            )
        if self.cfg.hermes and self.cfg.hermes_cron_recovery.enabled:
            tasks.append(
                self._spawn_leader_loop(
                    "hermes_cron_maintenance", self._hermes_cron_maintenance_loop
                )
            )
        if self.cfg.whatsapp_digest.enabled and self.cfg.whatsapp_digest.recipient:
            tasks.append(
                self._spawn_leader_loop(
                    "whatsapp_digest", self._whatsapp_digest_loop
                )
            )
        if self.cfg.skill_discovery.enabled:
            tasks.append(
                self._spawn_leader_loop(
                    "skill_discovery", self._skill_discovery_loop
                )
            )
        if self.cfg.alerting.enabled:
            tasks.append(self._spawn_leader_loop("alerting", self._alert_loop))
        tasks.append(
            self._spawn_leader_loop(
                "runtime_integrity", self._runtime_integrity_loop
            )
        )
        if getattr(self.cfg.swarm_memory, "db_maintenance_enabled", True):
            tasks.append(
                self._spawn_leader_loop(
                    "ruflo_memory_maintenance",
                    self._ruflo_memory_maintenance_loop,
                )
            )
        if self.cfg.mongodb_keepalive.enabled:
            tasks.append(
                self._spawn_leader_loop(
                    "mongodb_keepalive", self._mongodb_keepalive_loop
                )
            )
        wd = self.cfg.whatsapp_digest
        if (
            self.cfg.hermes
            and self.cfg.hermes_agent_create.enabled
        ):
            tasks.append(
                self._spawn_leader_loop(
                    "kanban_cron_sync", self._kanban_cron_sync_loop
                )
            )
        if wd.inbound_enabled and wd.inbound_sync_enabled and wd.recipient:
            tasks.append(
                self._spawn_leader_loop(
                    "whatsapp_sync", self._whatsapp_wacli_sync_loop
                )
            )
        if self.cfg.openclaw_chat.enabled and self.cfg.openclaw_chat.async_send:
            tasks.append(
                self._spawn_leader_loop(
                    "stale_chat_jobs", self._stale_chat_jobs_loop
                )
            )
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._leader_shutdown()

    # ------------------------------------------------------------------
    # Kanban cron sync loop
    # ------------------------------------------------------------------


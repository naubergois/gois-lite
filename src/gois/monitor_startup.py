"""Hermes profile seeding on monitor startup."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .hermes_cron_diagnostic_agent import ensure_hermes_cron_diagnostic_profile
from .hermes_cron_recovery_agent import ensure_hermes_cron_recovery_profile
from .hermes_recovery_agent import ensure_hermes_recovery_profile
from .local_paths import _repo_root
from .ruflo_daemon import ensure_ruflo_daemon
from .ruflo_chat import run_ruflo
from .ruflo_memory_guard import maintain_memory_db
from .ruflo_memory_repair import resolve_ruflo_memory_db_path

log = logging.getLogger(__name__)

# Once the role catalog has been seeded for this leadership term, the seed
# coroutine idles for this long instead of returning. Returning would make the
# leader-loop supervisor restart it immediately (it has no inter-run delay),
# busy-spinning a re-seed of the whole catalog every few seconds.
_ROLE_CATALOG_SEED_IDLE_SECONDS = 3600.0


class MonitorStartupMixin:
    def _cleanup_rv_orphans_on_startup(self) -> None:
        """Drop stale embedded RV uvicorn children left by prior gois restarts."""
        from .roteiro_viral_local.orphan_cleanup import kill_orphan_rv_subprocesses

        result = kill_orphan_rv_subprocesses(
            main_pid=os.getpid(),
            min_age_seconds=0.0,
        )
        if result.get("killed"):
            log.info(
                "rv orphan cleanup on startup: %d process(es) terminated",
                len(result["killed"]),
            )

    def _repair_ruflo_memory_db_on_startup(self) -> None:
        """Harden ``.swarm/memory.db`` before RuFlo memory search."""
        sm = getattr(self.cfg, "swarm_memory", None)
        rc = getattr(self.cfg, "ruflo_chat", None)
        if not (sm and sm.enabled) and not (rc and rc.enabled):
            return
        db_path = resolve_ruflo_memory_db_path(
            swarm_memory_cfg=sm,
            ruflo_chat_cfg=rc,
            repo_root=_repo_root(),
        )
        if db_path is None:
            return
        min_free_mb = int(getattr(sm, "db_min_free_mb", 512) or 512)
        backup_keep = int(getattr(sm, "db_backup_keep", 3) or 3)
        result = maintain_memory_db(
            db_path,
            min_free_mb=min_free_mb,
            backup_keep=backup_keep,
            auto_repair=True,
        )
        if result.get("repaired") or result.get("repair", {}).get("repaired"):
            log.info(
                "ruflo memory db repaired on startup (%s entries, backup %s)",
                result.get("entries_after") or result.get("repair", {}).get("entries_after"),
                result.get("backup_path") or result.get("repair", {}).get("backup_path"),
            )
        elif result.get("backup_path"):
            log.info("ruflo memory db startup backup: %s", result.get("backup_path"))
        elif not result.get("ok"):
            log.warning("ruflo memory db startup maintenance failed: %s", result.get("error"))

    def _ensure_ruflo_daemon_on_startup(self) -> None:
        """Keep RuFlo daemon up after monitor/login restarts (P0 infra)."""
        rc = getattr(self.cfg, "ruflo_chat", None)
        if not rc or not rc.enabled:
            return
        try:
            runtime = self._openclaw_runtime()
        except Exception as exc:
            log.warning("ruflo daemon startup skipped: runtime unavailable (%s)", exc)
            return
        try:
            result = ensure_ruflo_daemon(
                rc,
                runtime,
                swarm_memory_cfg=getattr(self.cfg, "swarm_memory", None),
            )
            if not result.get("ok"):
                log.warning("ruflo daemon startup failed: %s", result.get("stderr_tail"))
        except Exception as exc:
            log.warning("ruflo daemon startup crashed: %s", exc)

    def _verify_ruflo_cli_on_startup(self) -> None:
        """Warn when RuFlo CLI is slow or still on npx (P1 infra check)."""
        rc = getattr(self.cfg, "ruflo_chat", None)
        if not rc or not rc.enabled:
            return
        if rc.ruflo_bin in ("npx", "") or rc.ruflo_args:
            log.warning(
                "ruflo_chat uses npx on hot path (ruflo_bin=%r); "
                "run qclaw-ruflo-swarm-status-fix for a stable local CLI",
                rc.ruflo_bin,
            )
        try:
            runtime = self._openclaw_runtime()
        except Exception as exc:
            log.warning("ruflo cli probe skipped: runtime unavailable (%s)", exc)
            return
        t0 = time.perf_counter()
        try:
            code, _stdout, stderr = run_ruflo(
                rc,
                runtime,
                ["swarm", "status", "--format", "json"],
                timeout=min(float(rc.command_timeout_seconds), 12.0),
                swarm_memory_cfg=getattr(self.cfg, "swarm_memory", None),
            )
        except Exception as exc:
            log.warning("ruflo cli probe failed: %s", exc)
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms > 3000.0:
            log.warning(
                "ruflo swarm status probe slow: %.0fms (target <3000ms)",
                elapsed_ms,
            )
        elif code != 0:
            log.warning(
                "ruflo swarm status probe exit %s: %s",
                code,
                (stderr or "")[:200],
            )
        else:
            log.info("ruflo cli probe ok in %.0fms", elapsed_ms)

    async def _startup_seed_role_catalog(self) -> None:
        """Populate Hermes with the role catalog after the dashboard is up.

        This is a one-shot task, but it is supervised by ``_spawn_leader_loop``,
        which restarts ``loop_fn`` the instant it returns. Without the idle guard
        below it re-seeds the entire catalog (and re-pings the Hermes dashboard)
        every few seconds, stealing the event loop from chat handling.
        """
        hac = self.cfg.hermes_agent_create
        if not hac.enabled or not hac.seed_role_catalog_on_start:
            return
        if not self.cfg.hermes or not self.hermes_recovery:
            return
        if getattr(self, "_role_catalog_seeded", False):
            # Already seeded this leadership term — idle instead of returning so
            # the leader-loop supervisor does not busy-spin re-running us.
            await asyncio.sleep(_ROLE_CATALOG_SEED_IDLE_SECONDS)
            return
        for attempt in range(18):
            await asyncio.sleep(5.0 if attempt else 3.0)
            if await self._ensure_hermes_dashboard():
                break
        else:
            log.warning("role catalog seed on start: Hermes dashboard not ready")
            return
        if self._role_catalog_seed_status.get("running"):
            self._role_catalog_seeded = True
            return
        log.info("seeding Hermes role catalog on startup (only_missing=True)")
        self._start_role_catalog_seed_async({"only_missing": True})
        self._role_catalog_seeded = True
        await self._startup_ensure_hermes_recovery_profile()
        await self._startup_ensure_hermes_cron_recovery_profile()
        await self._startup_ensure_hermes_cron_diagnostic_profile()

    async def _startup_ensure_hermes_cron_diagnostic_profile(self) -> None:
        cda = self.cfg.hermes_cron_diagnostic_agent
        if not cda.enabled or not cda.seed_profile_on_start:
            return
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return
        hac = self.cfg.hermes_agent_create
        try:
            result = await asyncio.to_thread(
                ensure_hermes_cron_diagnostic_profile,
                profile_id=cda.profile_id,
                template_profile=hac.seed_role_catalog_template_profile,
            )
            if result.get("error"):
                log.warning(
                    "hermes cron diagnostic profile seed: %s", result.get("error")
                )
        except Exception as e:
            log.exception("hermes cron diagnostic profile seed failed: %s", e)

    async def _startup_ensure_hermes_cron_recovery_profile(self) -> None:
        csa = self.cfg.hermes_cron_scheduler_agent
        if not csa.enabled or not csa.seed_profile_on_start:
            return
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return
        hac = self.cfg.hermes_agent_create
        try:
            result = await asyncio.to_thread(
                ensure_hermes_cron_recovery_profile,
                profile_id=csa.profile_id,
                template_profile=hac.seed_role_catalog_template_profile,
            )
            if result.get("error"):
                log.warning(
                    "hermes cron recovery profile seed: %s", result.get("error")
                )
        except Exception as e:
            log.exception("hermes cron recovery profile seed failed: %s", e)

    async def _startup_ensure_hermes_recovery_profile(self) -> None:
        hra = self.cfg.hermes_recovery_agent
        if not hra.enabled or not hra.seed_profile_on_start:
            return
        if not self.cfg.hermes_agent_create.enabled:
            return
        hac = self.cfg.hermes_agent_create
        try:
            result = await asyncio.to_thread(
                ensure_hermes_recovery_profile,
                profile_id=hra.profile_id,
                template_profile=hac.seed_role_catalog_template_profile,
            )
            if result.get("error"):
                log.warning(
                    "hermes recovery profile seed: %s", result.get("error")
                )
        except Exception as e:
            log.warning("hermes recovery profile seed failed: %s", e)



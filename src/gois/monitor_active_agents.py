"""Active agents dashboard snapshot cache."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

from .chat_jobs import list_running_jobs as chat_jobs_list_running
from .kanban_schedule_jobs import (
    job_to_dict as kanban_schedule_job_to_dict,
    list_recent_jobs as kanban_schedule_list_recent,
)
from .tool_progress import list_active_tool_runs

log = logging.getLogger(__name__)

_ACTIVE_AGENTS_CACHE_SECONDS = 15.0


class MonitorActiveAgentsMixin:
    def _empty_active_agents_snapshot(self, *, now: Optional[float] = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        return {
            "ok": True,
            "updated_at": now,
            "summary": {
                "total_running": 0,
                "monitor_loops": 0,
                "hermes_crons": 0,
                "chat_sends": 0,
                "tool_runs": 0,
                "kanban_schedules": 0,
            },
            "monitor_loops": [],
            "monitor_loops_all": [],
            "hermes_crons": [],
            "chat_sends": [],
            "tool_runs": [],
            "kanban_schedules": [],
        }

    def _compute_active_agents_snapshot(self) -> dict[str, Any]:
        """Aggregate in-flight work for the /agentes dashboard tab."""
        now = time.time()
        loop_agents = self.tracker.snapshot()
        running_loops = [
            a
            for a in loop_agents
            if a.get("enabled") is not False and a.get("state") == "running"
        ]
        hermes_crons: list[dict[str, Any]] = []
        if self.cfg.hermes:
            cron_snap = self._cached_hermes_cron_snapshot()
            if cron_snap:
                hermes_crons = list(cron_snap.get("running") or [])
        chat_sends = [
            self._chat_job_status_dict(j) for j in chat_jobs_list_running()
        ]
        tool_runs = list_active_tool_runs()
        kanban_schedules = []
        for j in kanban_schedule_list_recent():
            row = kanban_schedule_job_to_dict(j)
            self._enrich_profile_model_row(row)
            kanban_schedules.append(row)
        total = (
            len(running_loops)
            + len(hermes_crons)
            + len(chat_sends)
            + len(tool_runs)
            + len(kanban_schedules)
        )
        return {
            "ok": True,
            "updated_at": now,
            "summary": {
                "total_running": total,
                "monitor_loops": len(running_loops),
                "hermes_crons": len(hermes_crons),
                "chat_sends": len(chat_sends),
                "tool_runs": len(tool_runs),
                "kanban_schedules": len(kanban_schedules),
            },
            "monitor_loops": running_loops,
            "monitor_loops_all": loop_agents,
            "hermes_crons": hermes_crons,
            "chat_sends": chat_sends,
            "tool_runs": tool_runs,
            "kanban_schedules": kanban_schedules,
        }

    def _refresh_active_agents_snapshot_sync(self) -> None:
        try:
            snapshot = self._compute_active_agents_snapshot()
        except Exception as exc:
            log.warning("active agents snapshot refresh failed: %s", exc)
        else:
            with self._active_agents_cache_lock:
                self._active_agents_cache_snapshot = dict(snapshot)
                self._active_agents_cache_expires_at = (
                    time.time() + _ACTIVE_AGENTS_CACHE_SECONDS
                )
        finally:
            with self._active_agents_cache_lock:
                self._active_agents_refreshing = False

    def _schedule_active_agents_snapshot_refresh(self) -> None:
        with self._active_agents_cache_lock:
            if self._active_agents_refreshing:
                return
            self._active_agents_refreshing = True
        thread = threading.Thread(
            target=self._refresh_active_agents_snapshot_sync,
            name="active_agents_snapshot_refresh",
            daemon=True,
        )
        thread.start()

    def handle_active_agents_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._active_agents_cache_lock:
            cached = (
                dict(self._active_agents_cache_snapshot)
                if self._active_agents_cache_snapshot is not None
                else None
            )
            cache_expires_at = self._active_agents_cache_expires_at
        if cached is not None and now < cache_expires_at:
            return cached
        if not refresh:
            self._schedule_active_agents_snapshot_refresh()
            if cached is not None:
                cached["stale"] = True
                return cached
            pending = self._empty_active_agents_snapshot(now=now)
            pending["stale"] = True
            pending["pending"] = True
            return pending
        snapshot = self._compute_active_agents_snapshot()
        with self._active_agents_cache_lock:
            self._active_agents_cache_snapshot = dict(snapshot)
            self._active_agents_cache_expires_at = time.time() + _ACTIVE_AGENTS_CACHE_SECONDS
        return snapshot

    async def _startup_warm_active_agents_snapshot(self) -> None:
        try:
            await asyncio.to_thread(self.handle_active_agents_snapshot, refresh=True)
        except Exception as exc:
            log.warning("active agents snapshot warmup failed: %s", exc)



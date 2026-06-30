"""Monitor /status snapshot and cache."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .diagnosis import build_qclaw_diagnosis
from .error_log import collect_errors
from .monitor_mcp import mcp_servers_status
from .processes import memory_summary
from .recovery import _resolve_health_url
from .skill_discovery import discovery_status

log = logging.getLogger(__name__)

_STATUS_CACHE_SECONDS = 30.0
_STATUS_CACHE_LIGHT_SECONDS = 45.0


class MonitorStatusMixin:
    def init_status_cache(self) -> None:
        self._status_cache_expires_at: float = 0.0
        self._status_cache_snapshot: Optional[dict[str, Any]] = None
        self._status_cache_lock = threading.Lock()
        self._status_refreshing: bool = False

    def handle_errors_list(self, query: dict) -> dict[str, Any]:
        if not self.cfg.error_log.enabled:
            return {"ok": False, "error": "error log is disabled"}
        try:
            limit = int(query.get("limit", self.cfg.error_log.max_errors))
        except (TypeError, ValueError):
            limit = self.cfg.error_log.max_errors
        return collect_errors(self.cfg, self.state, limit=limit)

    def _hermes_process_alive(self) -> bool:
        if not self.cfg.hermes:
            return False
        needle = (self.cfg.hermes.process_pattern or self.cfg.hermes.name).lower()
        for proc in self._processes_cache:
            cmd = proc.command.lower()
            if proc.name in ("Hermes gateway", "Hermes dashboard"):
                return True
            if "hermes_cli.main" in cmd and "gateway" in cmd and " run" in cmd:
                return True
            if "hermes" in cmd and "gateway" in cmd and " run" in cmd:
                return True
            if "hermes" in cmd and " dashboard" in cmd:
                return True
            if needle and needle in cmd:
                return True
        return False

    def _hermes_is_up(self) -> bool:
        if not self.cfg.hermes:
            return False
        if self.state.hermes_consecutive_failures > 0:
            return False
        if self._hermes_last_ok is not None:
            return self._hermes_last_ok
        return self._hermes_process_alive()

    def _runtime_legacy_paths(self) -> dict[str, Path]:
        """Legacy JSON paths compared with Redis/Mongo for split-brain detection."""
        from .cron_concurrency import resolve_cron_workspace
        from .local_paths import project_stack_root
        from .runtime_integrity import build_runtime_legacy_paths

        cron_ws = None
        if self.cfg.cron_concurrency.enabled:
            try:
                cron_ws = resolve_cron_workspace(self.cfg.cron_concurrency)
            except Exception:
                cron_ws = None

        pq_path = None
        pq_handler = getattr(self, "_priority_queue_handler", None)
        if pq_handler is not None:
            pq_path = getattr(pq_handler, "_state_path", None)

        return build_runtime_legacy_paths(
            monitor_state_path=getattr(self, "_state_path", None),
            priority_queue_state_path=pq_path,
            skill_suggestions_state_path=Path(
                self.cfg.skill_discovery.state_path
            ).expanduser(),
            stack_root=project_stack_root(),
            cron_workspace=cron_ws,
        )

    def status_snapshot(self, *, light: bool = True) -> dict:
        timings_ms: dict[str, float] = {}
        total_started_at = time.perf_counter()

        def _capture_timing(name: str, fn):
            started_at = time.perf_counter()
            value = fn()
            timings_ms[name] = round((time.perf_counter() - started_at) * 1000.0, 3)
            return value

        agents_snapshot = _capture_timing("agents", self.tracker.snapshot)
        qclaw_processes = _capture_timing(
            "qclaw_processes", lambda: [p.to_dict() for p in self._processes_cache]
        )
        qclaw_memory = _capture_timing(
            "qclaw_memory", lambda: memory_summary(self._processes_cache)
        )
        hermes_cron_diagnostic = _capture_timing(
            "hermes_cron_diagnostic",
            self._hermes_cron_diagnostic_status_payload,
        )
        openclaw_chat_status = _capture_timing(
            "openclaw_chat", self._openclaw_chat_status
        )
        model_quota_status = _capture_timing(
            "model_quota", lambda: self._model_daily_quota_status(enforce=False)
        )
        ruflo_status = _capture_timing(
            "ruflo", lambda: self.handle_ruflo_status(refresh=False)
        )
        skill_discovery_status = _capture_timing(
            "skill_discovery", lambda: discovery_status(self.cfg.skill_discovery)
        )
        whatsapp_inbound_status = _capture_timing(
            "whatsapp_inbound", self._whatsapp_inbound_status_snapshot
        )
        alerting_status = _capture_timing(
            "alerting",
            lambda: self.alert_engine.snapshot() if hasattr(self, "alert_engine") else {},
        )
        mcp_servers_payload = _capture_timing(
            "mcp_servers", lambda: mcp_servers_status()
        )
        from .mongo import mongo_status
        from .redis_store import redis_status
        from .runtime_integrity import build_integrity_snapshot
        from .runtime_state import runtime_stats

        mongo_status_payload = _capture_timing(
            "mongodb",
            lambda: mongo_status(
                include_collections=not light,
                include_persistence=not light,
            ),
        )
        runtime_payload = runtime_stats()
        redis_status_payload = _capture_timing(
            "redis",
            lambda: {**redis_status(), "runtime": runtime_payload},
        )
        legacy_paths = self._runtime_legacy_paths() if not light else {}
        data_integrity = (
            _capture_timing(
                "data_integrity",
                lambda: build_integrity_snapshot(
                    runtime_stats=runtime_payload,
                    legacy_paths=legacy_paths or None,
                ),
            )
            if not light
            else {"skipped": True, "reason": "light_snapshot"}
        )
        snap = {
            "name": self.cfg.qclaw.name,
            "process_pattern": self.cfg.qclaw.process_pattern,
            "health_url": _resolve_health_url(self.cfg.qclaw) or self.cfg.qclaw.health_url,
            "consecutive_failures": self.state.consecutive_failures,
            "failure_threshold": self.cfg.monitor.failure_threshold,
            "recovery_attempts_this_run": self._recovery_attempts,
            "max_recovery_attempts": self.cfg.monitor.max_recovery_attempts,
            "monitor_interval_seconds": self.cfg.monitor.interval_seconds,
            "cooldown_seconds": self.cfg.monitor.cooldown_seconds,
            "leader": self._leader_status_snapshot(),
            "last_recovery_ts": self.state.last_recovery_ts,
            "last_health_ok_ts": self.state.last_health_ok_ts,
            "last_health_fail_ts": self.state.last_health_fail_ts,
            "last_failure_summary": self.state.last_failure_summary,
            "last_recovery_report": self.state.last_recovery_report,
            "agents": agents_snapshot,
            "qclaw_processes": qclaw_processes,
            "qclaw_memory": qclaw_memory,
            "log_scanner": {
                "enabled": self.cfg.log_scanner.enabled,
                "interval_seconds": self.cfg.log_scanner.interval_seconds,
                "paths": self.cfg.log_scanner.paths,
                "patterns": [p.name for p in self.cfg.log_scanner.patterns],
                "last_ts": self.state.last_log_scan_ts,
                "last_matches": self.state.last_log_matches,
                "last_details": self.state.last_log_match_details or [],
                "trigger_recovery": self.cfg.log_scanner.trigger_recovery,
            },
            "reaper": {
                "enabled": self.cfg.reaper.enabled,
                "interval_seconds": self.cfg.reaper.interval_seconds,
                "target_pattern": self.cfg.reaper.target_pattern,
                "last_ts": self.state.last_reap_ts,
                "last_zombies": self.state.last_reap_zombies,
                "last_orphans": self.state.last_reap_orphans,
                "last_killed": self.state.last_reap_killed,
                "last_summary": self.state.last_reap_summary,
                "last_details": self.state.last_reap_details or [],
            },
            "openclaw_doctor": {
                "enabled": self.cfg.openclaw_doctor.enabled,
                "trigger_patterns": self.cfg.openclaw_doctor.trigger_patterns,
                "cooldown_seconds": self.cfg.openclaw_doctor.cooldown_seconds,
                "timeout_seconds": self.cfg.openclaw_doctor.timeout_seconds,
                "last_ts": self.state.last_doctor_ts,
                "last_ok": self.state.last_doctor_ok,
                "last_trigger": self.state.last_doctor_trigger,
                "last_summary": self.state.last_doctor_summary,
            },
            "hermes_cron_recovery": {
                "enabled": self.cfg.hermes_cron_recovery.enabled,
                "trigger_patterns": self.cfg.hermes_cron_recovery.trigger_patterns,
                "cooldown_seconds": self.cfg.hermes_cron_recovery.cooldown_seconds,
                "timeout_seconds": self.cfg.hermes_cron_recovery.timeout_seconds,
                "ensure_gateway": self.cfg.hermes_cron_recovery.ensure_gateway,
                "jobs_path": self.cfg.hermes_cron_recovery.jobs_path,
                "last_ts": self.state.last_hermes_cron_retry_ts,
                "last_ok": self.state.last_hermes_cron_retry_ok,
                "last_job_id": self.state.last_hermes_cron_job_id,
                "last_job_name": self.state.last_hermes_cron_job_name,
                "last_summary": self.state.last_hermes_cron_summary,
                "gateway_ensure_last_ts": self.state.last_hermes_cron_gateway_ensure_ts,
                "gateway_ensure_last_ok": self.state.last_hermes_cron_gateway_ok,
                "gateway_ensure_last_summary": self.state.last_hermes_cron_gateway_summary,
                "scheduler": self._hermes_cron_scheduler_probe,
            },
            "hermes_cron_scheduler_agent": {
                "enabled": self.cfg.hermes_cron_scheduler_agent.enabled,
                "llm_enabled": self.cfg.hermes_cron_scheduler_agent.llm_enabled,
                "auto_recover": self.cfg.hermes_cron_scheduler_agent.auto_recover,
                "profile_id": self.cfg.hermes_cron_scheduler_agent.profile_id,
                "cooldown_seconds": self.cfg.hermes_cron_scheduler_agent.cooldown_seconds,
                "last_ts": self.state.last_hermes_cron_scheduler_recovery_ts,
                "last_ok": self.state.last_hermes_cron_scheduler_recovery_ok,
                "last_report": (
                    (self.state.last_hermes_cron_scheduler_recovery_report or "")[:500]
                    if self.state.last_hermes_cron_scheduler_recovery_report
                    else None
                ),
            },
            "hermes_cron_diagnostic_agent": {
                "enabled": self.cfg.hermes_cron_diagnostic_agent.enabled,
                "llm_enabled": self.cfg.hermes_cron_diagnostic_agent.llm_enabled,
                "profile_id": self.cfg.hermes_cron_diagnostic_agent.profile_id,
                "auto_run_on_log_match": (
                    self.cfg.hermes_cron_diagnostic_agent.auto_run_on_log_match
                ),
                "auto_run_on_scheduler_down": (
                    self.cfg.hermes_cron_diagnostic_agent.auto_run_on_scheduler_down
                ),
                "last_ts": self.state.last_hermes_cron_diagnostic_ts,
                "last_ok": self.state.last_hermes_cron_diagnostic_ok,
                "last_healthy": self.state.last_hermes_cron_diagnostic_healthy,
                "last_report": (
                    (self.state.last_hermes_cron_diagnostic_report or "")[:500]
                    if self.state.last_hermes_cron_diagnostic_report
                    else None
                ),
            },
            "hermes_cron_diagnostic": hermes_cron_diagnostic,
            "up": (
                self.state.last_health_ok_ts is not None
                and self.state.consecutive_failures == 0
            ),
            "openclaw_chat": openclaw_chat_status,
            "model_quota": model_quota_status,
            "ruflo": ruflo_status,
            "ruflo_health": (
                self.ruflo_health_snapshot()
                if hasattr(self, "ruflo_health_snapshot")
                else {}
            ),
            "skill_discovery": skill_discovery_status,
            "kanban": {
                "default_team_id": "projeto-padrao",
                "url": "/kanban?team=projeto-padrao",
            },
            "whatsapp_inbound": whatsapp_inbound_status,
            "alerting": alerting_status,
            "mcp_servers": mcp_servers_payload,
            "mongodb": mongo_status_payload,
            "mongodb_keepalive": {
                "enabled": self.cfg.mongodb_keepalive.enabled,
                "interval_seconds": self.cfg.mongodb_keepalive.interval_seconds,
                "cooldown_seconds": self.cfg.mongodb_keepalive.cooldown_seconds,
                "max_restart_attempts": self.cfg.mongodb_keepalive.max_restart_attempts,
                "consecutive_failures": self.state.mongodb_consecutive_failures,
                "restart_attempts_this_run": self._mongodb_restart_attempts,
                "last_restart_ts": self.state.mongodb_last_restart_ts,
                "last_restart_ok": self.state.mongodb_last_restart_ok,
                "last_restart_summary": self.state.mongodb_last_restart_summary,
            },
            "redis": redis_status_payload,
            "data_integrity": data_integrity,
        }
        if not snap["up"]:
            snap["diagnosis"] = _capture_timing(
                "diagnosis", lambda: build_qclaw_diagnosis(snap)
            )
        if self.cfg.hermes:
            hermes_up = _capture_timing("hermes_is_up", self._hermes_is_up)
            hermes_process_alive = _capture_timing(
                "hermes_process_alive", self._hermes_process_alive
            )
            hermes_procs = _capture_timing(
                "hermes_processes",
                lambda: [
                    p.to_dict()
                    for p in self._processes_cache
                    if "hermes gateway" in p.command.lower() or p.name == "Hermes gateway"
                ],
            )
            hra = self.cfg.hermes_recovery_agent
            recovery_id = (hra.profile_id or "hermes-recovery").strip()
            dash_base = (
                self.cfg.hermes.dashboard_url or "http://127.0.0.1:9119"
            ).rstrip("/")
            hermes_cron_snapshot = _capture_timing(
                "hermes_cron", self._cached_hermes_cron_snapshot
            )
            snap["hermes"] = {
                "name": self.cfg.hermes.name,
                "process_pattern": self.cfg.hermes.process_pattern,
                "health_url": self.cfg.hermes.health_url,
                "dashboard_url": dash_base,
                "dashboard_up": self._hermes_dashboard_up,
                "recovery_profile_id": recovery_id,
                "recovery_profile_url": f"{dash_base}/profiles",
                "active": hermes_up,
                "up": hermes_up,
                "process_alive": hermes_process_alive,
                "processes": hermes_procs,
                "consecutive_failures": self.state.hermes_consecutive_failures,
                "recovery_attempts_this_run": self._hermes_recovery_attempts,
                "last_recovery_ts": self.state.hermes_last_recovery_ts,
                "last_health_ok_ts": self.state.hermes_last_health_ok_ts,
                "last_health_fail_ts": self.state.hermes_last_health_fail_ts,
                "last_failure_summary": self.state.hermes_last_failure_summary,
                "last_recovery_report": self.state.hermes_last_recovery_report,
                "cron": hermes_cron_snapshot,
                "cron_scheduler_ok": (
                    self._hermes_cron_scheduler_probe.get("ok")
                    if self._hermes_cron_scheduler_probe
                    else None
                ),
                "cron_scheduler": self._hermes_cron_scheduler_probe,
            }
        if self.cfg.whatsapp_digest.recipient:
            snap["whatsapp"] = self._whatsapp_public_config()
        snap["status_timing_ms"] = {
            "total": round((time.perf_counter() - total_started_at) * 1000.0, 3),
            "sections": timings_ms,
        }
        return snap

    def _status_snapshot_warming_placeholder(self) -> dict[str, Any]:
        """Fast response while the full status snapshot is built in background."""
        return {
            "warming": True,
            "stale": True,
            "name": self.cfg.qclaw.name,
            "process_pattern": self.cfg.qclaw.process_pattern,
            "health_url": _resolve_health_url(self.cfg.qclaw) or self.cfg.qclaw.health_url,
            "consecutive_failures": self.state.consecutive_failures,
            "failure_threshold": self.cfg.monitor.failure_threshold,
            "up": self.state.consecutive_failures == 0,
            "agents": self.tracker.snapshot(),
            "openclaw_chat": {
                "enabled": self.cfg.openclaw_chat.enabled,
                "backend": getattr(self.cfg.openclaw_chat, "backend", ""),
            },
            "leader": self._leader_status_snapshot(),
        }

    def _refresh_status_snapshot_sync(self, *, light: bool = True) -> None:
        try:
            snapshot = self.status_snapshot(light=light)
        except Exception as exc:
            log.warning("status snapshot refresh failed: %s", exc)
        else:
            ttl = _STATUS_CACHE_LIGHT_SECONDS if light else _STATUS_CACHE_SECONDS
            with self._status_cache_lock:
                self._status_cache_snapshot = dict(snapshot)
                self._status_cache_expires_at = time.time() + ttl
        finally:
            with self._status_cache_lock:
                self._status_refreshing = False

    def _schedule_status_snapshot_refresh(self, *, light: bool = True) -> None:
        with self._status_cache_lock:
            if self._status_refreshing:
                return
            self._status_refreshing = True
        thread = threading.Thread(
            target=lambda: self._refresh_status_snapshot_sync(light=light),
            name="status_snapshot_refresh",
            daemon=True,
        )
        thread.start()

    def handle_alerting_clear(self) -> dict[str, Any]:
        """Clear alert history (POST /api/alerting/clear)."""
        if hasattr(self, "alert_engine"):
            self.alert_engine.clear_history()
        return {"ok": True}

    def handle_status_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._status_cache_lock:
            cached = (
                dict(self._status_cache_snapshot)
                if self._status_cache_snapshot is not None
                else None
            )
            cache_expires_at = self._status_cache_expires_at
        if cached is not None and now < cache_expires_at:
            return cached
        if cached is not None and not refresh:
            self._schedule_status_snapshot_refresh(light=True)
            cached["stale"] = True
            return cached

        if cached is None and not refresh:
            self._schedule_status_snapshot_refresh(light=True)
            return self._status_snapshot_warming_placeholder()

        with self._status_cache_lock:
            now = time.time()
            if self._status_cache_snapshot is not None and now < self._status_cache_expires_at:
                return dict(self._status_cache_snapshot)
            snapshot = self.status_snapshot(light=not refresh)
            ttl = _STATUS_CACHE_SECONDS if refresh else _STATUS_CACHE_LIGHT_SECONDS
            self._status_cache_snapshot = dict(snapshot)
            self._status_cache_expires_at = time.time() + ttl
            return dict(snapshot)

    async def _startup_warm_status_snapshot(self) -> None:
        try:
            await asyncio.to_thread(self.handle_status_snapshot, refresh=True)
        except Exception as exc:
            log.warning("status snapshot warmup failed: %s", exc)


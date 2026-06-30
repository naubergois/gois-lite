"""Hermes cron cache, stagger, and token-stats for GoisMonitor."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import (
    build_cron_edit_argv,
    compute_cron_token_stats,
    hermes_cron_snapshot,
    maintain_recurring_cron_schedule,
    occupied_cron_minutes,
    plan_cron_stagger_rewrites,
    read_jobs_file,
    resolve_agent_log_path,
    run_hermes_cron_command,
    stagger_cron_schedule,
)

log = logging.getLogger(__name__)

HERMES_CRON_CACHE_SECONDS = 30.0
HERMES_CRON_TOKEN_STATS_CACHE_SECONDS = 300.0
HERMES_CRON_TOKEN_STATS_MAX_LOG_LINES = 50000

class MonitorHermesCronMixin:
    def _invalidate_hermes_cron_cache(self) -> None:
        self._hermes_cron_cache_expires_at = 0.0
        self._hermes_cron_cache_snapshot = None
    def _enrich_hermes_cron_snapshot(self, snap: dict[str, Any]) -> None:
        if not snap or not self.cfg.hermes:
            return
        for key in ("jobs", "running"):
            rows = snap.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    self._enrich_profile_model_row(row)

    def _stagger_schedule_for_new_job(self, schedule: str) -> str:
        """Return *schedule* with a non-colliding minute, when staggering is on."""
        if not self.cfg.hermes:
            return schedule
        cc = self.cfg.hermes_agent_create
        step = int(cc.cron_stagger_minutes or 0)
        if step <= 0:
            return schedule
        try:
            jobs, _ = read_jobs_file(self._hermes_cron_jobs_path())
            existing = occupied_cron_minutes(jobs)
        except Exception as e:
            log.warning("cron stagger: could not read jobs.json: %s", e)
            return schedule
        return stagger_cron_schedule(
            schedule,
            existing_minutes=existing,
            step=step,
            base_hour=int(cc.cron_stagger_base_hour or 0),
        )

    def _normalize_schedule_for_kanban(self, schedule: str) -> str:
        """Accept friendly timestamp inputs from Kanban and normalize to raw schedule."""
        raw = str(schedule or "").strip()
        if not raw:
            return raw

        out = raw
        if out.lower().startswith("timestamp:"):
            out = out.split(":", 1)[1].strip()

        lower = out.lower()
        marker = " (one-shot"
        if marker in lower:
            out = out[: lower.index(marker)].strip()

        while len(out) >= 2 and out[0] in ('"', "'") and out[-1] == out[0]:
            out = out[1:-1].strip()

        # Support pasted helper strings by extracting the first ISO-like token.
        match = re.search(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
            out,
        )
        if match:
            token = match.group(0).strip()
            if "T" not in token and " " in token:
                token = token.replace(" ", "T", 1)
            out = token

        # Accept common natural-language daily inputs used in the Kanban modal.
        daily_aliases = {
            "todo dia",
            "todos os dias",
            "diario",
            "diariamente",
            "every day",
            "daily",
            "cada dia",
        }
        lowered = out.lower().strip()
        if lowered in daily_aliases:
            return "0 9 * * *"

        m_daily_time = re.match(
            r"^(?:todo dia|todos os dias|diario|diariamente|every day|daily)\s*(?:as|at)?\s*(\d{1,2}):(\d{2})$",
            lowered,
        )
        if m_daily_time:
            hh = max(0, min(23, int(m_daily_time.group(1))))
            mm = max(0, min(59, int(m_daily_time.group(2))))
            return f"{mm} {hh} * * *"

        return out

    def _normalize_kanban_skills(self, raw: Any) -> list[str]:
        if raw is None:
            return []
        parts: list[str]
        if isinstance(raw, str):
            parts = re.split(r"[\n,;]", raw)
        elif isinstance(raw, list):
            parts = [str(item) for item in raw]
        else:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in parts:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    def _apply_cron_stagger_on_startup(self) -> None:
        """Rewrite minutes of existing colliding cron jobs once at boot."""
        if not self.cfg.hermes:
            return
        cc = self.cfg.hermes_agent_create
        if not cc.enabled or not cc.cron_stagger_existing_on_startup:
            return
        step = int(cc.cron_stagger_minutes or 0)
        if step <= 0:
            return
        try:
            jobs, _ = read_jobs_file(self._hermes_cron_jobs_path())
        except Exception as e:
            log.warning("cron stagger startup: could not read jobs.json: %s", e)
            return
        rewrites = plan_cron_stagger_rewrites(
            jobs,
            step=step,
            base_hour=int(cc.cron_stagger_base_hour or 0),
        )
        if not rewrites:
            return
        log.info(
            "cron stagger startup: rewriting %d colliding job(s) (step=%d)",
            len(rewrites),
            step,
        )
        for rw in rewrites:
            job_id = rw["job_id"]
            if not job_id:
                continue
            cmd = build_cron_edit_argv(
                job_id,
                schedule=rw["new_expr"],
                accept_hooks=cc.cron_accept_hooks,
            )
            try:
                result = run_hermes_cron_command(
                    cmd,
                    timeout_seconds=cc.cron_timeout_seconds,
                    job_id=job_id,
                    job_name=rw.get("name"),
                    jobs_path=self._hermes_cron_jobs_path(),
                )
            except Exception as e:
                log.warning(
                    "cron stagger startup: edit %s failed: %s", job_id, e
                )
                continue
            if not result.get("ok"):
                log.warning(
                    "cron stagger startup: edit %s rc=%s: %s",
                    job_id,
                    result.get("rc"),
                    (result.get("stderr_tail") or result.get("summary") or "")[:160],
                )
            else:
                log.info(
                    "cron stagger startup: %s %s -> %s",
                    job_id,
                    rw["expr"],
                    rw["new_expr"],
                )
        self._invalidate_hermes_cron_cache()

    def _recurring_cron_repair_interval(self) -> float:
        cc = self.cfg.hermes_cron_recovery
        return max(45.0, float(cc.recurring_repair_interval_seconds or 1800.0))

    def _maybe_repair_recurring_cron_jobs(self, *, force: bool = False) -> dict[str, Any]:
        """Re-enable recurring Hermes jobs left paused or with stale ``next_run_at``."""
        if not self.cfg.hermes:
            return {"ok": True, "skipped": True}
        now = time.time()
        interval = self._recurring_cron_repair_interval()
        if not force and now < self._recurring_cron_repair_ts + interval:
            return {"ok": True, "skipped": True, "cooldown_seconds": interval}
        self._recurring_cron_repair_ts = now
        cc = self.cfg.hermes_cron_recovery
        try:
            result = maintain_recurring_cron_schedule(
                self._hermes_cron_jobs_path(),
                stale_hours=float(cc.stale_cron_hours or 48.0),
            )
        except Exception as exc:
            log.warning("recurring cron repair failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        repaired = int(result.get("repaired") or 0)
        stale_after = int(result.get("stale_after") or 0)
        if repaired or stale_after:
            log.info(
                "recurring cron maintenance: %s",
                result.get("summary") or result,
            )
            self._invalidate_hermes_cron_cache()
        return result

    def _cached_hermes_cron_snapshot(self) -> dict[str, Any]:
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        now = time.time()
        cached = self._hermes_cron_cache_snapshot
        if cached is not None and now < self._hermes_cron_cache_expires_at:
            return dict(cached)

        # Avoid thundering-herd recomputes: if another thread is rebuilding,
        # serve stale data instead of blocking /status and /active-agents.
        if cached is not None and not self._hermes_cron_cache_lock.acquire(blocking=False):
            stale = dict(cached)
            stale["stale"] = True
            stale["pending"] = True
            return stale

        if cached is None:
            self._hermes_cron_cache_lock.acquire()
        try:
            now = time.time()
            cached = self._hermes_cron_cache_snapshot
            if cached is not None and now < self._hermes_cron_cache_expires_at:
                return dict(cached)
            self._maybe_repair_recurring_cron_jobs()
            self._maybe_enforce_swarm_only_cron_jobs()
            agent_log_path = resolve_agent_log_path(
                self.cfg.hermes.log_paths if self.cfg.hermes else None
            )
            sources = self._hermes_cron_snapshot_sources()
            snapshots: list[dict[str, Any]] = []

            def _snap_one(source_profile: str, jobs_path: Path) -> Optional[dict[str, Any]]:
                snap = hermes_cron_snapshot(jobs_path, agent_log_path=agent_log_path)
                if not snap.get("ok", True):
                    return None
                snap["source_profile"] = source_profile
                snap["source_jobs_path"] = str(jobs_path)
                return snap

            if len(sources) <= 1:
                for source_profile, jobs_path in sources:
                    result = _snap_one(source_profile, jobs_path)
                    if result is not None:
                        self._enrich_hermes_cron_snapshot(result)
                        snapshots.append(result)
            else:
                workers = min(8, len(sources))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {
                        pool.submit(_snap_one, sp, jp): (sp, jp)
                        for sp, jp in sources
                    }
                    raw: list[dict[str, Any]] = []
                    for fut in as_completed(futs):
                        result = fut.result()
                        if result is not None:
                            raw.append(result)
                raw.sort(key=lambda s: str(s.get("source_profile") or ""))
                for result in raw:
                    self._enrich_hermes_cron_snapshot(result)
                    snapshots.append(result)

            if not snapshots:
                snapshot = hermes_cron_snapshot(
                    self._hermes_cron_jobs_path(),
                    agent_log_path=agent_log_path,
                )
                self._enrich_hermes_cron_snapshot(snapshot)
                from .swarm_robots import apply_tombstone_filters_to_cron_snapshot

                snapshot = apply_tombstone_filters_to_cron_snapshot(snapshot)
                self._hermes_cron_cache_snapshot = dict(snapshot)
                self._hermes_cron_cache_expires_at = now + HERMES_CRON_CACHE_SECONDS
                return dict(snapshot)

            jobs: list[dict[str, Any]] = []
            running: list[dict[str, Any]] = []
            running_seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            job_seen: dict[str, int] = {}
            updated_at = None
            jobs_path = self._hermes_cron_jobs_path()

            for snap in snapshots:
                source_profile = str(snap.get("source_profile") or "default").strip() or "default"
                source_jobs_path = str(snap.get("source_jobs_path") or "").strip()
                updated_at = max(
                    [updated_at, snap.get("updated_at")],
                    key=lambda value: str(value or ""),
                )
                for row in snap.get("jobs") or []:
                    if not isinstance(row, dict):
                        continue
                    item = dict(row)
                    item["source_profile"] = source_profile
                    if source_jobs_path:
                        item["source_jobs_path"] = source_jobs_path
                    item_id = str(item.get("id") or "").strip()
                    if item_id and item_id in job_seen:
                        prev_idx = job_seen[item_id]
                        prev = jobs[prev_idx]
                        prev_profile = str(prev.get("source_profile") or "")
                        # Prefer canonical/default source when same ID appears
                        # duplicated across multiple profile snapshots.
                        if prev_profile != "default" and source_profile == "default":
                            jobs[prev_idx] = item
                        continue
                    if item_id:
                        job_seen[item_id] = len(jobs)
                    jobs.append(item)
                for row in snap.get("running") or []:
                    if not isinstance(row, dict):
                        continue
                    item = dict(row)
                    item["source_profile"] = source_profile
                    if source_jobs_path:
                        item["source_jobs_path"] = source_jobs_path
                    dedupe_key = (
                        str(item.get("session_id") or "").strip(),
                        str(item.get("job_id") or "").strip(),
                        str(item.get("started_at") or "").strip(),
                        str(item.get("name") or "").strip(),
                    )
                    existing = running_seen.get(dedupe_key)
                    if existing is None:
                        running_seen[dedupe_key] = item
                        continue
                    if str(item.get("last_activity_at") or "") >= str(
                        existing.get("last_activity_at") or ""
                    ):
                        running_seen[dedupe_key] = item

            running = sorted(
                running_seen.values(),
                key=lambda item: str(item.get("last_activity_at") or ""),
                reverse=True,
            )

            def sort_key(s: dict[str, Any]) -> tuple:
                return (
                    s.get("next_run_at") or "z",
                    s.get("source_profile") or "",
                    s.get("name") or "",
                )

            from .swarm_robots import apply_tombstone_filters_to_cron_snapshot

            active_jobs = []
            inactive_jobs = []
            for j in jobs:
                if j.get("active"):
                    active_jobs.append(j)
                else:
                    inactive_jobs.append(j)
            ordered = sorted(active_jobs, key=sort_key) + sorted(inactive_jobs, key=sort_key)
            snapshot = apply_tombstone_filters_to_cron_snapshot(
                {
                    "jobs_path": str(jobs_path.expanduser()),
                    "jobs_paths": [
                        snap.get("jobs_path") for snap in snapshots if snap.get("jobs_path")
                    ],
                    "source_profiles": [
                        snap.get("source_profile")
                        for snap in snapshots
                        if snap.get("source_profile")
                    ],
                    "updated_at": updated_at,
                    "running": running,
                    "jobs": ordered,
                    "ok": True,
                }
            )
            ordered = list(snapshot.get("jobs") or [])
            running = list(snapshot.get("running") or [])
            active_count = int(snapshot.get("active_count") or 0)
            paused_count = int(snapshot.get("paused_count") or 0)
            error_count = int(snapshot.get("error_count") or 0)

            token_stats = self._cached_hermes_cron_token_stats(agent_log_path)
            if token_stats:
                for summary in ordered:
                    stats = token_stats.get(str(summary.get("id") or ""))
                    if stats:
                        summary["avg_tokens"] = stats["avg_tokens"]
                        summary["token_runs"] = stats["runs"]
                        summary["token_max"] = stats["max_tokens"]
                        if stats.get("model"):
                            summary["token_model"] = stats["model"]

            snapshot = {
                "jobs_path": str(jobs_path.expanduser()),
                "jobs_paths": [snap.get("jobs_path") for snap in snapshots if snap.get("jobs_path")],
                "source_profiles": [snap.get("source_profile") for snap in snapshots if snap.get("source_profile")],
                "updated_at": updated_at,
                "total": len(ordered),
                "active_count": active_count,
                "paused_count": paused_count,
                "error_count": error_count,
                "running_count": len(running),
                "running": running,
                "jobs": ordered,
                "ok": True,
            }
            self._hermes_cron_cache_snapshot = dict(snapshot)
            self._hermes_cron_cache_expires_at = now + HERMES_CRON_CACHE_SECONDS
            return dict(snapshot)
        finally:
            self._hermes_cron_cache_lock.release()

    def _refresh_hermes_cron_token_stats_sync(
        self,
        agent_log_path: Optional[Path],
    ) -> None:
        try:
            snapshot = (
                compute_cron_token_stats(
                    agent_log_path,
                    max_log_lines=HERMES_CRON_TOKEN_STATS_MAX_LOG_LINES,
                )
                if agent_log_path is not None
                else {}
            )
        except Exception as exc:
            log.warning("hermes cron token stats refresh failed: %s", exc)
        else:
            with self._hermes_cron_token_stats_cache_lock:
                self._hermes_cron_token_stats_cache = dict(snapshot)
                self._hermes_cron_token_stats_cache_expires_at = (
                    time.time() + HERMES_CRON_TOKEN_STATS_CACHE_SECONDS
                )
        finally:
            with self._hermes_cron_token_stats_cache_lock:
                self._hermes_cron_token_stats_refreshing = False

    def _schedule_hermes_cron_token_stats_refresh(
        self,
        agent_log_path: Optional[Path],
    ) -> None:
        with self._hermes_cron_token_stats_cache_lock:
            if self._hermes_cron_token_stats_refreshing:
                return
            self._hermes_cron_token_stats_refreshing = True
        thread = threading.Thread(
            target=self._refresh_hermes_cron_token_stats_sync,
            args=(agent_log_path,),
            name="hermes_cron_token_stats_refresh",
            daemon=True,
        )
        thread.start()

    def _cached_hermes_cron_token_stats(
        self,
        agent_log_path: Optional[Path],
    ) -> dict[str, dict[str, Any]]:
        now = time.time()
        with self._hermes_cron_token_stats_cache_lock:
            cached = (
                dict(self._hermes_cron_token_stats_cache)
                if self._hermes_cron_token_stats_cache is not None
                else None
            )
            cache_expires_at = self._hermes_cron_token_stats_cache_expires_at
        if cached is not None and now < cache_expires_at:
            return cached
        self._schedule_hermes_cron_token_stats_refresh(agent_log_path)
        return cached or {}

    async def _startup_warm_hermes_cron_snapshot(self) -> None:
        if not self.cfg.hermes:
            return
        try:
            await asyncio.to_thread(self._cached_hermes_cron_snapshot)
        except Exception as exc:
            log.warning("hermes cron snapshot warmup failed: %s", exc)

    async def _startup_warm_hermes_cron_token_stats(self) -> None:
        if not self.cfg.hermes:
            return
        try:
            await asyncio.to_thread(
                self._refresh_hermes_cron_token_stats_sync,
                resolve_agent_log_path(
                    self.cfg.hermes.log_paths if self.cfg.hermes else None
                ),
            )
        except Exception as exc:
            log.warning("hermes cron token stats warmup failed: %s", exc)

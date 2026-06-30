"""Hermes cron CRUD list/get/edit/create handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .hermes_cron import (
    build_cron_edit_argv,
    catch_up_overdue_cron_jobs,
    compute_next_run_at_for_job,
    compute_upcoming_runs_for_job,
    create_hermes_cron_job,
    cron_output_dir_for_jobs_path,
    filter_cron_snapshot,
    find_job_by_id,
    job_detail_for_edit,
    read_jobs_file,
    resolve_created_cron_job_id,
    resolve_edit_schedule_payload,
    resume_all_paused_hermes_cron_jobs,
    run_hermes_cron_command,
    summarize_cron_job,
)
from .swarm_cron_policy import cron_job_belongs_to_swarm
from .swarm_robots import _swarm_links_for_profile_slug

log = logging.getLogger(__name__)


def build_swarm_cron_timeline(
    jobs: list[dict[str, Any]],
    *,
    horizon_hours: float = 24.0,
    runs_per_job: int = 6,
    reference: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build chronological swarm execution events for the Hermes monitor timeline."""
    reference = reference or datetime.now().astimezone()
    horizon_end = reference + timedelta(hours=max(horizon_hours, 0.25))
    events: list[dict[str, Any]] = []
    swarm_jobs = 0

    for raw in jobs:
        if not isinstance(raw, dict) or not cron_job_belongs_to_swarm(raw):
            continue
        swarm_jobs += 1
        summary = summarize_cron_job(raw)
        profile = str(raw.get("profile") or "").strip()
        swarm_names, _ = _swarm_links_for_profile_slug(profile)
        upcoming = compute_upcoming_runs_for_job(
            raw,
            limit=runs_per_job,
            horizon_hours=horizon_hours,
            reference=reference,
        )
        if not upcoming and summary.get("next_run_at"):
            try:
                next_dt = datetime.fromisoformat(
                    str(summary["next_run_at"]).replace("Z", "+00:00")
                )
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=reference.tzinfo)
                if reference <= next_dt <= horizon_end:
                    upcoming = [next_dt.isoformat()]
            except ValueError:
                upcoming = []

        for idx, at in enumerate(upcoming):
            events.append(
                {
                    "job_id": summary.get("id"),
                    "name": summary.get("name"),
                    "profile": summary.get("profile"),
                    "swarm_names": swarm_names,
                    "schedule_display": summary.get("schedule_display"),
                    "schedule_kind": summary.get("schedule_kind"),
                    "at": at,
                    "run_index": idx,
                    "active": bool(summary.get("active")),
                    "state": summary.get("state"),
                    "paused": not bool(summary.get("active")),
                }
            )

    events.sort(key=lambda row: (row.get("at") or "", row.get("name") or ""))
    return {
        "ok": True,
        "horizon_hours": horizon_hours,
        "window_start": reference.isoformat(),
        "window_end": horizon_end.isoformat(),
        "swarm_job_count": swarm_jobs,
        "event_count": len(events),
        "events": events,
    }


class MonitorHermesCronHandlersMixin:
    def handle_hermes_cron_create(self, payload: dict) -> dict:
        """Create a Hermes cron job from the advanced UI builder."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        try:
            resolved = self._resolve_advanced_cron_payload(payload or {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        if resolved.get("profile"):
            actor = self._accounts_actor(None)
            resolved_profile = self._resolve_kanban_assignee(resolved["profile"], actor)
            if not resolved_profile:
                return {
                    "ok": False,
                    "error": (
                        f"profile inválido: {resolved['profile']}. "
                        "Escolha um perfil existente."
                    ),
                }
            resolved = dict(resolved)
            resolved["profile"] = resolved_profile

        schedule = self._stagger_schedule_for_new_job(resolved["schedule"])
        jobs_path = self._hermes_cron_jobs_path()
        hac = self.cfg.hermes_agent_create

        cron = create_hermes_cron_job(
            schedule,
            resolved["prompt"],
            name=resolved["name"],
            profile=resolved["profile"],
            skills=resolved["skills"] or None,
            workdir=resolved["workdir"],
            repeat=resolved["repeat"],
            accept_hooks=hac.cron_accept_hooks if hac else False,
            timeout_seconds=(hac.cron_timeout_seconds if hac else 120.0),
            jobs_path=jobs_path,
        )
        if not cron.get("ok"):
            return {
                "ok": False,
                "error": cron.get("reason") or cron.get("summary") or "falha ao criar cron",
                "stdout_tail": cron.get("stdout_tail"),
                "stderr_tail": cron.get("stderr_tail"),
            }

        job_id = resolve_created_cron_job_id(
            cron,
            job_name=resolved["name"],
            jobs_path=jobs_path,
        )
        self._invalidate_hermes_cron_cache()

        job_row: Optional[dict] = None
        next_run_at: Optional[str] = None
        if job_id:
            raw_job = find_job_by_id(job_id, jobs_path)
            if raw_job is not None:
                next_run_at = compute_next_run_at_for_job(raw_job) or raw_job.get("next_run_at")
                output_root = cron_output_dir_for_jobs_path(jobs_path)
                job_row = summarize_cron_job(raw_job, output_root=output_root)

        return {
            "ok": True,
            "job_id": job_id,
            "schedule": schedule,
            "next_run_at": next_run_at,
            "job": job_row,
            "summary": cron.get("summary"),
        }

    def handle_hermes_cron_list(self, query: Optional[dict] = None) -> dict:
        """Return Hermes cron snapshot for dashboards (optional cache bust)."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        q = query or {}
        fresh = str(q.get("fresh") or "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if fresh:
            self._invalidate_hermes_cron_cache()
        snap = self._cached_hermes_cron_snapshot()
        job_id = str(q.get("job_id") or q.get("id") or q.get("cron") or "").strip()
        search = str(q.get("q") or q.get("search") or "").strip()
        if job_id or search:
            snap = filter_cron_snapshot(snap, job_id=job_id, query=search)
        return snap

    def handle_hermes_cron_swarm_timeline(self, query: Optional[dict] = None) -> dict:
        """Return upcoming swarm cron executions for the Hermes timeline UI."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        q = query or {}
        try:
            horizon_hours = float(q.get("hours") or q.get("horizon_hours") or 24)
        except (TypeError, ValueError):
            horizon_hours = 24.0
        horizon_hours = max(1.0, min(horizon_hours, 168.0))
        try:
            runs_per_job = int(q.get("runs") or q.get("runs_per_job") or 6)
        except (TypeError, ValueError):
            runs_per_job = 6
        runs_per_job = max(1, min(runs_per_job, 16))

        jobs_path = self._hermes_cron_jobs_path()
        jobs, _ = read_jobs_file(jobs_path)
        return build_swarm_cron_timeline(
            jobs,
            horizon_hours=horizon_hours,
            runs_per_job=runs_per_job,
        )

    def _hermes_cron_output_path(self) -> Path:
        return cron_output_dir_for_jobs_path(self._hermes_cron_jobs_path())

    def handle_hermes_cron_get(self, job_id: str) -> dict:
        """Return one cron job for the dashboard edit form."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        resolved = self._resolve_hermes_cron_job_context(job_id)
        if not resolved.get("ok"):
            out: dict[str, Any] = {"ok": False, "error": resolved.get("error")}
            if resolved.get("matches"):
                out["matches"] = resolved["matches"]
            return out
        job = resolved["job"]
        return {"ok": True, "job": job_detail_for_edit(job)}

    def handle_hermes_cron_edit(self, job_id: str, payload: dict) -> dict:
        """Edit a Hermes cron job via `hermes cron edit`."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        resolved = self._resolve_hermes_cron_job_context(job_id)
        if not resolved.get("ok"):
            out = {"ok": False, "error": resolved.get("error")}
            if resolved.get("matches"):
                out["matches"] = resolved["matches"]
            return out
        job = resolved["job"]
        canon_id = str(resolved["job_id"])
        jobs_path = resolved["jobs_path"]

        cc = self.cfg.hermes_cron_recovery
        resume_after = str(payload.get("resume_after_save") or "").lower() in (
            "1",
            "true",
            "yes",
        )

        fields: dict[str, Optional[str]] = {}
        for key in ("name", "prompt", "profile"):
            if key in payload:
                val = payload[key]
                if val is None:
                    fields[key] = ""
                elif isinstance(val, str):
                    fields[key] = val
                else:
                    return {"ok": False, "error": f"{key} must be a string or null"}

        schedule_keys = (
            "schedule",
            "builder_kind",
            "sched_kind",
            "interval_minutes",
            "interval_value",
            "interval_unit",
            "cron_preset",
            "cron_expr",
            "run_at",
        )
        if any(key in payload for key in schedule_keys):
            try:
                fields["schedule"] = resolve_edit_schedule_payload(payload)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

        if not fields:
            return {"ok": False, "error": "no editable fields provided"}

        if fields.get("profile"):
            actor = self._accounts_actor(None)
            resolved_profile = self._resolve_kanban_assignee(fields["profile"], actor)
            if not resolved_profile:
                return {
                    "ok": False,
                    "error": (
                        f"profile inválido: {fields['profile']}. "
                        "Escolha um perfil existente."
                    ),
                }
            fields["profile"] = resolved_profile

        cmd = build_cron_edit_argv(
            canon_id,
            schedule=fields.get("schedule"),
            name=fields.get("name"),
            prompt=fields.get("prompt"),
            profile=fields.get("profile"),
            accept_hooks=cc.accept_hooks,
        )
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=min(cc.timeout_seconds, 120.0),
            job_id=canon_id,
            job_name=str(job.get("name") or canon_id),
            jobs_path=jobs_path,
        )
        if result.get("ok"):
            self._invalidate_hermes_cron_cache()
            if resume_after:
                resume_result = self.handle_hermes_cron_action(canon_id, "resume")
                if not resume_result.get("ok"):
                    result["resume_warning"] = resume_result.get("error") or resume_result.get(
                        "summary"
                    )
            updated = find_job_by_id(canon_id, jobs_path)
            if updated:
                result["job"] = job_detail_for_edit(updated)
            if resolved.get("resolved_from"):
                result["resolved_job_id"] = canon_id
        return result

    def handle_hermes_cron_catch_up(self, query: Optional[dict] = None) -> dict:
        """Execute all overdue enabled cron jobs immediately."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        jobs_path = self._hermes_cron_jobs_path()
        q = query or {}
        include_paused = str(q.get("include_paused") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        skip_ids: set[str] = set()
        log_path = resolve_agent_log_path(
            self.cfg.hermes.log_paths if self.cfg.hermes else None
        )
        if log_path is not None:
            for row in detect_running_cron_jobs(log_path, jobs_path):
                if row.get("job_id"):
                    skip_ids.add(str(row["job_id"]))
        result = catch_up_overdue_cron_jobs(
            jobs_path,
            skip_running_ids=skip_ids or None,
            include_paused=include_paused,
        )
        if result.get("ok"):
            self._invalidate_hermes_cron_cache()
        return result

    def handle_hermes_cron_resume_all(self) -> dict[str, Any]:
        """Resume every Hermes cron job in paused state."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        jobs_path = self._hermes_cron_jobs_path()
        cc = self.cfg.hermes_cron_recovery
        result = resume_all_paused_hermes_cron_jobs(
            jobs_path,
            accept_hooks=cc.accept_hooks,
            timeout_seconds=cc.timeout_seconds,
        )
        if result.get("resumed_count", 0) > 0 or result.get("ok"):
            self._invalidate_hermes_cron_cache()
        snap = self._cached_hermes_cron_snapshot()
        result["active_count"] = snap.get("active_count")
        result["paused_count"] = snap.get("paused_count")
        return result

    def _find_kanban_task_by_cron_job_id(
        self, cron_job_id: str
    ) -> Optional[dict[str, Any]]:
        """Locate a kanban card that still references a (possibly stale) cron id."""
        needle = str(cron_job_id or "").strip()
        if not needle:
            return None
        for board_info in self._collect_kanban_board_infos(require_cron_link=True):
            for task in board_info.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                if str(task.get("cron_job_id") or "").strip() != needle:
                    continue
                return {"board": board_info, "task": task}
        return None

    def _recover_stale_kanban_cron_run(self, stale_job_id: str) -> dict[str, Any]:
        """Recreate and run a kanban-linked cron when jobs.json no longer has the id."""
        from .hermes_kanban import normalize_assignees

        match = self._find_kanban_task_by_cron_job_id(stale_job_id)
        if not match:
            return {"ok": False, "error": f"job {stale_job_id!r} não encontrado"}

        board_info = match["board"]
        task = match["task"]
        task_id = str(task.get("id") or "").strip()
        assignees = normalize_assignees(task.get("assignees") or task.get("assignee"))
        assignee = str(assignees[0] if assignees else "").strip()
        workdir = str(board_info.get("workdir") or "").strip()
        team_id = str(board_info.get("team_id") or "").strip()
        if not task_id or not assignee or not workdir:
            return {"ok": False, "error": f"job {stale_job_id!r} não encontrado"}

        payload: dict[str, Any] = {
            "task_id": task_id,
            "assignee": assignee,
            "workdir": workdir,
            "team_id": team_id,
            "once": True,
            "schedule": "1m",
        }
        kanban_file = str(board_info.get("kanban_file") or "").strip()
        if kanban_file:
            payload["kanban_file"] = kanban_file

        system_user = self._system_actor(payload)
        result = self._execute_kanban_schedule(payload, system_user)
        if result.get("ok"):
            result["recovered_from_stale_job_id"] = stale_job_id
        return result


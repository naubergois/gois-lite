"""Hermes cron run/pause/remove and profile delete."""

from __future__ import annotations

import logging
from typing import Any

from .hermes_cron import hermes_cron_argv, run_hermes_cron_command

log = logging.getLogger(__name__)


class MonitorHermesCronActionsMixin:
    def handle_hermes_cron_action(self, job_id: str, action: str) -> dict:
        """Pause, resume, run, or remove a Hermes cron job."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if action in {"delete", "rm"}:
            action = "remove"
        if action not in {"pause", "resume", "run", "remove"}:
            return {"ok": False, "error": f"unknown action {action!r}"}
        if action in {"resume", "run"}:
            blocked = self._model_quota_guard()
            if blocked is not None:
                return blocked

        jobs_path = self._hermes_cron_jobs_path()
        resolved = self._resolve_hermes_cron_job_context(job_id)
        if not resolved.get("ok") and action == "run":
            self._invalidate_hermes_cron_cache()
            resolved = self._resolve_hermes_cron_job_context(job_id)
        if not resolved.get("ok"):
            missing_id = str(job_id or "").strip()
            if action == "run" and missing_id:
                recovered = self._recover_stale_kanban_cron_run(missing_id)
                if recovered.get("ok"):
                    return recovered
            if action in {"pause", "remove"} and _CRON_CANONICAL_JOB_ID_RE.fullmatch(missing_id):
                # Idempotent behavior for stale dashboard entries: treat missing
                # canonical IDs as already absent and clear cached snapshots.
                self._invalidate_hermes_cron_cache()
                return {
                    "ok": True,
                    "already_absent": True,
                    "action": action,
                    "job_id": missing_id,
                    "summary": f"Cron job {missing_id} já está ausente.",
                }
            # If action is remove and the jobs.json might be corrupted,
            # fall back to force-remove which handles corrupted files.
            if action == "remove" and missing_id:
                import json as _json_chk
                try:
                    _raw = jobs_path.read_text(encoding="utf-8")
                    _json_chk.loads(_raw)
                except (_json_chk.JSONDecodeError, OSError):
                    return self.handle_hermes_cron_force_remove({"job_id": missing_id})
            out = {"ok": False, "error": resolved.get("error")}
            if resolved.get("matches"):
                out["matches"] = resolved["matches"]
            return out
        job = resolved["job"]
        canon_id = str(resolved["job_id"])
        jobs_path = resolved["jobs_path"]

        if action in {"resume", "run"}:
            swarm_blocked = self._swarm_only_cron_guard(job, job_id=canon_id)
            if swarm_blocked is not None:
                return swarm_blocked

        cc = self.cfg.hermes_cron_recovery
        if action == "run" and self.cfg.cron_concurrency.enabled:
            try:
                from .cron_concurrency import prepare_gate_before_cron_run, resolve_cron_workspace

                gate_ws = resolve_cron_workspace(self.cfg.cron_concurrency)
                prepare_gate_before_cron_run(
                    self.cfg.cron_concurrency,
                    workspace=gate_ws,
                )
            except Exception as exc:
                log.warning("cron gate pre-run prune failed: %s", exc)
        if action == "run":
            result = self._run_hermes_cron_job_now(
                canon_id,
                jobs_path=jobs_path,
                job_name=str(job.get("name") or canon_id),
            )
            if result.get("ok") and resolved.get("resolved_from"):
                result["resolved_job_id"] = canon_id
            return result
        cmd = hermes_cron_argv(action, canon_id, accept_hooks=cc.accept_hooks)
        timeout = min(cc.timeout_seconds, 60.0)
        result = run_hermes_cron_command(
            cmd,
            timeout_seconds=timeout,
            job_id=canon_id,
            job_name=str(job.get("name") or canon_id),
            jobs_path=jobs_path,
        )
        if result.get("ok"):
            self._invalidate_hermes_cron_cache()
            if resolved.get("resolved_from"):
                result["resolved_job_id"] = canon_id
        return result


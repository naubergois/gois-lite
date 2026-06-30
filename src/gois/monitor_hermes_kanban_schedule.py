"""Hermes kanban schedule handlers and async job runner."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import UserRecord
from .hermes_cron import spawn_hermes_cron_job_direct
from .hermes_kanban import apply_kanban_action, get_board
from .kanban_schedule_jobs import (
    append_progress as kanban_schedule_append_progress,
    complete_job as kanban_schedule_complete,
    create_job as kanban_schedule_create,
    fail_job as kanban_schedule_fail,
    get_job as kanban_schedule_get,
    is_job_cancelled as kanban_schedule_is_cancelled,
    job_to_dict as kanban_schedule_job_to_dict,
)

log = logging.getLogger(__name__)


class KanbanScheduleCancelled(Exception):
    """Raised when a kanban schedule job is cancelled mid-flight."""


class MonitorHermesKanbanScheduleMixin:
    def _kanban_schedule_progress(
        self,
        message: str,
        *,
        progress_job_id: Optional[str] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        if progress_job_id and kanban_schedule_is_cancelled(progress_job_id):
            raise KanbanScheduleCancelled()
        text = (message or "").strip()
        if not text:
            return
        if on_progress is not None:
            try:
                on_progress(text)
            except Exception:
                pass
        if progress_job_id:
            kanban_schedule_append_progress(progress_job_id, text)

    def _run_hermes_cron_job_now(
        self,
        job_id: str,
        *,
        jobs_path: Optional[Path] = None,
        job_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run a Hermes cron job immediately (direct subprocess, not scheduler queue)."""
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        canon_id = str(job_id or "").strip()
        if not canon_id:
            return {"ok": False, "error": "job_id is required"}
        jp = (jobs_path or self._hermes_cron_jobs_path()).expanduser().resolve()
        from .hermes_cron import find_job_by_id

        job_row = find_job_by_id(canon_id, jp)
        swarm_blocked = self._swarm_only_cron_guard(job_row, job_id=canon_id)
        if swarm_blocked is not None:
            return swarm_blocked
        cc = self.cfg.hermes_cron_recovery
        create_cfg = self.cfg.hermes_agent_create
        if self.cfg.cron_concurrency.enabled:
            try:
                from .cron_concurrency import (
                    prepare_gate_before_cron_run,
                    resolve_cron_workspace,
                )

                prepare_gate_before_cron_run(
                    self.cfg.cron_concurrency,
                    workspace=resolve_cron_workspace(self.cfg.cron_concurrency),
                )
            except Exception as exc:
                log.warning("cron gate pre-run prune failed: %s", exc)
        accept_hooks = bool(
            getattr(create_cfg, "cron_accept_hooks", False) or cc.accept_hooks
        )
        result = spawn_hermes_cron_job_direct(
            canon_id,
            jp,
            accept_hooks=accept_hooks,
        )
        if result.get("ok"):
            self._invalidate_hermes_cron_cache()
        elif not job_name and result.get("job_name"):
            job_name = str(result["job_name"])
        return result

    def _run_kanban_schedule_job(
        self,
        job_id: str,
        payload: dict,
        user: Optional[UserRecord],
    ) -> None:
        try:
            if kanban_schedule_is_cancelled(job_id):
                return
            result = self._execute_kanban_schedule(
                payload, user, progress_job_id=job_id
            )
            if kanban_schedule_is_cancelled(job_id):
                return
            if result.get("ok"):
                kanban_schedule_complete(job_id, result)
            else:
                kanban_schedule_fail(
                    job_id,
                    str(result.get("error") or "agendamento falhou"),
                    result=result,
                )
        except KanbanScheduleCancelled:
            log.info("kanban schedule job %s cancelled by user", job_id)
        except Exception as e:
            log.exception("async kanban schedule failed job=%s: %s", job_id, e)
            kanban_schedule_fail(job_id, f"{type(e).__name__}: {e}")

    def handle_hermes_kanban_schedule(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        use_async = payload.get("async") is not False

        if not use_async:
            return self._execute_kanban_schedule(payload, user)

        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        assignee = str(payload.get("assignee") or "").strip()
        if not assignee:
            return {"ok": False, "error": "assignee is required"}
        actor = self._system_actor(payload)
        resolved_assignee = self._resolve_kanban_assignee(assignee, actor)
        if not resolved_assignee:
            return self._kanban_assignee_error(assignee)
        payload = dict(payload)
        payload["assignee"] = resolved_assignee
        job = kanban_schedule_create(task_id, profile=resolved_assignee)
        kanban_schedule_append_progress(job.id, "Pedido recebido — a iniciar…")
        thread = threading.Thread(
            target=self._run_kanban_schedule_job,
            kwargs={"job_id": job.id, "payload": dict(payload), "user": user},
            name=f"kanban-schedule-{job.id}",
            daemon=True,
        )
        thread.start()
        return {
            "ok": True,
            "async": True,
            "jobId": job.id,
            "taskId": task_id,
            "status": "running",
        }

    def handle_hermes_kanban_schedule_status(self, query: dict) -> dict:
        job_id = str(query.get("job_id") or query.get("jobId") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        job = kanban_schedule_get(job_id)
        if job is None:
            return {"ok": False, "error": "job not found"}
        out = kanban_schedule_job_to_dict(job)
        self._enrich_profile_model_row(out)
        return out


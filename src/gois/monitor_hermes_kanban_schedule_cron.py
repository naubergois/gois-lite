"""Hermes kanban schedule cron finalization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .hermes_cron import (
    compact_cron_snapshot_for_chat,
    create_hermes_cron_job,
    cron_output_dir_for_jobs_path,
    find_job_by_id,
    find_kanban_task_cron_jobs,
    prune_kanban_task_cron_duplicates,
    resolve_created_cron_job_id,
    summarize_cron_job,
    update_hermes_cron_job,
)
from .hermes_kanban import apply_kanban_action

log = logging.getLogger(__name__)


class MonitorHermesKanbanScheduleCronMixin:
    def _finalize_kanban_schedule_cron(
        self,
        *,
        prog: Callable[[str], None],
        assignee: str,
        task_id: str,
        schedule: str,
        once: bool,
        cron_wd_path: Path,
        board_workdir: str,
        board: dict[str, Any],
        task: dict[str, Any],
        kanban_file: Optional[str],
        selected_skills: list[str],
        payload_has_skills: bool,
        model_id: Optional[str],
        prompt: str,
    ) -> dict[str, Any]:
        job_name = f"{assignee} — {task_id}"
        staggered_schedule = self._stagger_schedule_for_new_job(schedule)
        jobs_path = self._hermes_cron_jobs_path()
        existing_job_id = str(task.get("cron_job_id") or "").strip()
        canonical, _dupes = find_kanban_task_cron_jobs(
            jobs_path,
            task_id,
            assignee=assignee,
            preferred_job_id=existing_job_id or None,
        )
        reused = False
        job_id: Optional[str] = None
        if canonical:
            job_id = str(canonical.get("id") or "").strip() or existing_job_id or None
            prog(
                f"A reutilizar cron existente ({staggered_schedule}) para {assignee}…"
            )
            cron = update_hermes_cron_job(
                job_id,
                schedule=staggered_schedule,
                name=job_name,
                prompt=prompt,
                profile=assignee,
                accept_hooks=self.cfg.hermes_agent_create.cron_accept_hooks,
                timeout_seconds=self.cfg.hermes_agent_create.cron_timeout_seconds,
                jobs_path=jobs_path,
            )
            reused = True
        else:
            if existing_job_id:
                log.info(
                    "kanban schedule: cron_job_id %s ausente em jobs.json — "
                    "a criar novo cron para %s",
                    existing_job_id,
                    task_id,
                )
            prog(
                f"A criar cron no Hermes ({staggered_schedule}) para {assignee}…"
            )
            cron = create_hermes_cron_job(
                staggered_schedule,
                prompt,
                name=job_name,
                profile=assignee,
                model=model_id,
                workdir=str(cron_wd_path),
                repeat=1 if once else None,
                accept_hooks=self.cfg.hermes_agent_create.cron_accept_hooks,
                timeout_seconds=self.cfg.hermes_agent_create.cron_timeout_seconds,
                jobs_path=jobs_path,
            )
        if not cron.get("ok"):
            action = "atualizar" if reused else "criar"
            return {
                "ok": False,
                "error": cron.get("reason")
                or cron.get("summary")
                or f"falha ao {action} cron",
            }
        if not reused:
            job_id = resolve_created_cron_job_id(
                cron,
                job_name=job_name,
                jobs_path=jobs_path,
            )
        elif not job_id:
            job_id = resolve_created_cron_job_id(
                cron,
                job_name=job_name,
                jobs_path=jobs_path,
            )
        pruned_duplicates = 0
        if job_id:
            pruned_duplicates = prune_kanban_task_cron_duplicates(
                jobs_path,
                task_id,
                keep_job_id=job_id,
            )
            if pruned_duplicates:
                log.info(
                    "kanban schedule: removidos %d cron(s) duplicado(s) para %s",
                    pruned_duplicates,
                    task_id,
                )
        if job_id:
            cron["job_id"] = job_id
            prog("A gravar cron no cartão do kanban…")
            try:
                board = apply_kanban_action(
                    board_workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "update_task",
                        "task_id": task_id,
                        "task": {
                            "cron_job_id": job_id,
                            "cron_schedule": staggered_schedule,
                            "skills": selected_skills
                            if (selected_skills or payload_has_skills)
                            else task.get("skills"),
                        },
                        "kanban_file": kanban_file,
                    },
                )
                task = next(
                    (
                        t
                        for t in (board.get("tasks") or [])
                        if str(t.get("id") or "").strip() == task_id
                    ),
                    task,
                )
            except Exception as e:
                log.warning(
                    "kanban schedule: cron %s criado, mas falhou ao gravar no cartão: %s",
                    job_id,
                    e,
                )

        prog("A atualizar lista de crons…")
        self._invalidate_hermes_cron_cache()
        output_root = cron_output_dir_for_jobs_path(jobs_path)
        raw_job = find_job_by_id(job_id, jobs_path) if job_id else None
        cron_job_row = (
            summarize_cron_job(raw_job, output_root=output_root) if raw_job else None
        )
        cron_snapshot = compact_cron_snapshot_for_chat(
            self._cached_hermes_cron_snapshot(),
            ensure_job_ids=[job_id] if job_id else None,
        )
        if job_id and cron_job_row and isinstance(cron_snapshot.get("jobs"), list):
            rows = cron_snapshot["jobs"]
            if not any(str(r.get("id") or "") == job_id for r in rows):
                cron_snapshot["jobs"] = [cron_job_row, *rows]
        immediate_run: dict[str, Any] | None = None
        if once and job_id:
            prog("A executar cron Hermes agora (subprocesso direto)…")
            immediate_run = self._run_hermes_cron_job_now(
                job_id,
                jobs_path=jobs_path,
                job_name=job_name,
            )
            if immediate_run.get("ok"):
                prog("Execução do cron iniciada/concluída.")
            else:
                err = str(
                    immediate_run.get("error")
                    or immediate_run.get("reason")
                    or immediate_run.get("summary")
                    or "falha ao executar cron"
                )
                log.warning(
                    "kanban schedule: execução imediata falhou para %s: %s",
                    job_id,
                    err,
                )
                prog(f"Execução imediata falhou ({err}).")
                return {
                    "ok": False,
                    "error": err,
                    "task_id": task_id,
                    "assignee": assignee,
                    "cron": {"job_id": job_id},
                    "immediate_run": immediate_run,
                }

        prog(
            "Agendamento concluído"
            + (f" — cron {job_id}" if job_id else "")
            + ".",
        )
        return {
            "ok": True,
            "task_id": task_id,
            "assignee": assignee,
            "schedule": schedule,
            "staggered_schedule": staggered_schedule,
            "once": once,
            "workdir": str(cron_wd_path),
            "skills": selected_skills,
            "reused": reused,
            "pruned_duplicates": pruned_duplicates,
            "cron": {
                "job_id": job_id,
                "summary": cron.get("summary"),
            },
            "cron_job": cron_job_row,
            "cron_snapshot": cron_snapshot,
            "immediate_run": immediate_run,
        }


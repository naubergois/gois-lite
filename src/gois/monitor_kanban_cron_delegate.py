"""Team card delegation and auto-start enqueue for kanban cron sync."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .hermes_cron import (
    compute_next_run_at_for_job,
    cron_next_run_is_plausible,
)

log = logging.getLogger(__name__)

_KANBAN_AUTO_START_ERROR_BACKOFF = 1800.0  # 30 min



class MonitorKanbanCronDelegateMixin:
    def _delegate_unassigned_team_cards_tick(self) -> None:
        """Team leader auto-assigns open backlog cards to the best specialist."""
        from .hermes_kanban import normalize_assignees, propose_task_assignments
        from .swarm_robots import _norm_slug, team_leader_slug

        owner_id = self._resolve_scheduling_owner_id()
        if not owner_id:
            return

        for team in self.accounts.list_all_teams():
            team_id = str(team.id or "").strip()
            if not team_id or team_id.startswith("time-agendamento") or team_id == "agendamentos":
                continue
            if not team.profile_slugs:
                continue
            try:
                board = self.accounts.read_kanban(team.id, team.owner_id)
            except Exception:
                continue
            if not isinstance(board, dict):
                continue
            workdir = str(board.get("workdir") or "").strip()
            if not workdir:
                workdir = str(self.accounts.team_workdir(team))
            kanban_file = board.get("kanban_file")
            swarm_name = str(team.swarm_name or "").strip() or team.id
            swarm_state = self._ephemeral_team_swarm_state(team, swarm_name)
            agent_rows = [
                {"slug": str(s).strip(), "role": ""}
                for s in (team.profile_slugs or [])
                if str(s).strip()
            ]
            leader = team_leader_slug(
                list(team.profile_slugs or []),
                agent_rows,
                entry_agent=str(swarm_state.get("entry_agent") or ""),
            )
            specialists = [
                row
                for row in agent_rows
                if _norm_slug(str(row.get("slug") or "")) != leader
            ]
            if not specialists:
                continue
            proposals = propose_task_assignments(
                list(board.get("tasks") or []),
                specialists,
                columns={"todo", "backlog"},
                only_unassigned=True,
            )
            if not proposals:
                continue
            base: dict[str, Any] = {
                "workdir": workdir,
                "team_id": team.id,
            }
            if kanban_file is not None:
                base["kanban_file"] = kanban_file
            for task_id, slug in proposals.items():
                result = self.handle_hermes_kanban_action(
                    {
                        **base,
                        "action": "assign_task",
                        "task_id": task_id,
                        "assignees": [slug],
                    },
                    self._system_actor(base),
                )
                if not result.get("ok"):
                    continue
                self.handle_hermes_kanban_action(
                    {
                        **base,
                        "action": "update_task",
                        "task_id": task_id,
                        "task": {
                            "notes": (
                                f"Delegado automaticamente pelo líder do time "
                                f"para `{slug}` (melhor competência/carga)."
                            ),
                        },
                    },
                    self._system_actor(base),
                )
                log.info(
                    "delegate-tick: time %s card %s → %s",
                    team.name,
                    task_id,
                    slug,
                )

    def _priority_queue_task_ids(self) -> set[str]:
        if self._priority_queue_handler is None:
            return set()
        queue = self._priority_queue_handler.engine.get_queue()
        ids: set[str] = set()
        for bucket in ("queued", "running"):
            for row in queue.get(bucket) or []:
                if isinstance(row, dict):
                    tid = str(row.get("task_id") or "").strip()
                    if tid:
                        ids.add(tid)
        return ids

    def _task_has_active_execution(self, task: dict, snap: dict) -> bool:
        task_id = str(task.get("id") or "").strip()
        if task_id and task_id in self._priority_queue_task_ids():
            return True

        cron_job_id = str(task.get("cron_job_id") or "").strip()
        if not cron_job_id:
            return False

        running_ids = {
            str(row.get("job_id") or "").strip()
            for row in (snap.get("running") or [])
            if isinstance(row, dict) and str(row.get("job_id") or "").strip()
        }
        if cron_job_id in running_ids:
            return True

        for job in snap.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            if str(job.get("id") or "").strip() != cron_job_id:
                continue
            if bool(job.get("running")) or cron_job_id in running_ids:
                return True
            last_status = str(job.get("last_status") or "").strip().lower()
            if last_status in ("ok", "error", "failed"):
                return False
            if last_status == "running":
                return cron_job_id in running_ids
            # Cron agendado / aguardando — não reenfileirar via auto-start,
            # exceto quando o cron está preso (sem next_run_at ou atrasado
            # além da tolerância): nesse caso a fila de prioridades assume.
            if bool(job.get("enabled", True)):
                if not cron_next_run_is_plausible(job):
                    log.info(
                        "auto-start: cron %s do card %s sem próxima execução "
                        "plausível — fila de prioridades assume",
                        cron_job_id,
                        task_id,
                    )
                    return False
                return True
            return False
        return False

    def _auto_enqueue_doing_task(
        self,
        board_info: dict,
        task: dict,
        *,
        reason: str = "",
    ) -> bool:
        """Enqueue a doing/review card for Hermes execution when idle."""
        if self._priority_queue_handler is None:
            return False
        if self._model_quota_guard() is not None:
            return False

        from .hermes_kanban import normalize_assignees

        task_id = str(task.get("id") or "").strip()
        column = str(task.get("column") or "").strip().lower()
        if not task_id or column not in ("doing", "review"):
            return False

        assignees = normalize_assignees(task.get("assignees") or task.get("assignee"))
        assignee = str(assignees[0] if assignees else "").strip()
        if not assignee:
            log.debug("auto-start skip %s: sem responsável", task_id)
            return False

        workdir = str(board_info.get("workdir") or task.get("workdir") or "").strip()
        if not workdir:
            return False

        kanban_file = board_info.get("kanban_file")

        team_id = str(board_info.get("team_id") or "").strip()
        actor = self._system_actor({"workdir": workdir, "team_id": team_id})
        resolved_assignee = self._resolve_kanban_assignee(assignee, actor)
        if not resolved_assignee:
            log.debug(
                "auto-start skip %s: assignee inválido %s",
                task_id,
                assignee,
            )
            return False
        assignee = resolved_assignee

        snap = self._cached_hermes_cron_snapshot()
        if self._task_has_active_execution(task, snap):
            return False

        cron_job_id = str(task.get("cron_job_id") or "").strip()
        if cron_job_id:
            cron_job = self._cron_job_from_snapshot(cron_job_id, snap)
            if cron_job and str(cron_job.get("last_status") or "").strip().lower() == "error":
                last_error = str(cron_job.get("last_error") or "").strip() or "cron falhou"
                self._mark_kanban_task_failed(
                    workdir=workdir,
                    task_id=task_id,
                    error=last_error,
                    kanban_file=str(kanban_file).strip() if kanban_file else None,
                    source="cron Hermes",
                )
                return False

        cooldown_key = f"{workdir}:{task_id}"
        now = time.time()
        last_try = self._kanban_auto_start_cooldown.get(cooldown_key, 0.0)
        if now - last_try < 120.0:
            return False

        # Circuit breaker: stop hammering a card that keeps failing terminally
        # (e.g. missing Hermes profile, not authenticated). Without this, the
        # auto-scheduler re-enqueues the same doomed task every ~minute and the
        # priority-queue state balloons with thousands of error cards.
        try:
            recent_err = self._priority_queue_handler.engine.recent_error_for_task(
                task_id, _KANBAN_AUTO_START_ERROR_BACKOFF
            )
        except Exception:  # noqa: BLE001 — circuit breaker must never crash the tick
            recent_err = None
        if recent_err:
            log.debug(
                "auto-start skip %s: erro terminal recente (%s) — backoff %ds",
                task_id,
                recent_err[:100],
                int(_KANBAN_AUTO_START_ERROR_BACKOFF),
            )
            self._mark_kanban_task_failed(
                workdir=workdir,
                task_id=task_id,
                error=recent_err,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                source="fila de prioridades",
            )
            return False

        skills = task.get("skills") or []
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        elif not isinstance(skills, list):
            skills = []

        priority_raw = task.get("priority")
        try:
            priority = int(priority_raw) if priority_raw is not None else 5
        except (TypeError, ValueError):
            priority = 5

        model_id = str(task.get("model_id") or "").strip() or None
        title = str(task.get("title") or task_id).strip() or task_id

        try:
            self._priority_queue_handler.engine.enqueue(
                task_id=task_id,
                title=title,
                priority=priority,
                skills=list(skills),
                assignee=assignee,
                workdir=workdir,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                team_id=team_id,
                model_id=model_id,
            )
        except ValueError as exc:
            log.debug("auto-start skip %s: %s", task_id, exc)
            return False
        except Exception as exc:
            log.warning("auto-start failed for %s: %s", task_id, exc)
            return False

        self._kanban_auto_start_cooldown[cooldown_key] = now
        log.info(
            "auto-start: enfileirou %s (%s) assignee=%s reason=%s",
            task_id,
            title[:60],
            assignee,
            reason or "tick",
        )
        return True

    def _cron_job_from_snapshot(
        self, cron_job_id: str, snap: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        needle = str(cron_job_id or "").strip()
        if not needle:
            return None
        for row in snap.get("jobs") or []:
            if isinstance(row, dict) and str(row.get("id") or "").strip() == needle:
                return row
        return None

    def _cron_next_run_is_past(self, job: dict[str, Any]) -> bool:
        """True when a summarized cron job's next_run_at is due (works without schedule dict)."""
        if not job.get("enabled", True):
            return False
        state = str(job.get("state") or "").strip().lower()
        if state == "paused":
            return False
        next_iso = str(job.get("next_run_at") or "").strip()
        if not next_iso:
            next_iso = str(compute_next_run_at_for_job(job) or "").strip()
        if not next_iso:
            return False
        try:
            next_dt = datetime.fromisoformat(next_iso.replace("Z", "+00:00"))
        except ValueError:
            return False
        now = datetime.now().astimezone()
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=now.tzinfo)
        return next_dt <= now

    def _auto_try_run_overdue_cron_for_task(
        self,
        task: dict,
        snap: dict[str, Any],
    ) -> bool:
        """Disparar cron Hermes atrasado para card em doing/review (como Executar agora)."""
        cron_job_id = str(task.get("cron_job_id") or "").strip()
        task_id = str(task.get("id") or "").strip()
        if not cron_job_id or not task_id:
            return False

        running_ids = {
            str(row.get("job_id") or "").strip()
            for row in (snap.get("running") or [])
            if isinstance(row, dict) and str(row.get("job_id") or "").strip()
        }
        if cron_job_id in running_ids:
            return False

        job = self._cron_job_from_snapshot(cron_job_id, snap)
        if not job or not self._cron_next_run_is_past(job):
            return False

        if self._model_quota_guard() is not None:
            return False

        cooldown_key = f"cron-run:{cron_job_id}"
        now = time.time()
        last_try = self._kanban_auto_start_cooldown.get(cooldown_key, 0.0)
        if now - last_try < 120.0:
            return False

        result = self._run_hermes_cron_job_now(cron_job_id)
        self._kanban_auto_start_cooldown[cooldown_key] = now
        if result.get("ok"):
            log.info(
                "auto-start: cron %s disparado para card %s (atrasado)",
                cron_job_id,
                task_id,
            )
            return True

        err = str(
            result.get("error")
            or result.get("reason")
            or result.get("summary")
            or "falha ao executar cron"
        )
        log.warning(
            "auto-start: cron %s falhou para card %s: %s",
            cron_job_id,
            task_id,
            err[:200],
        )
        return False

    def _auto_start_stuck_doing_cards_tick(self) -> None:
        """Pick up doing/review cards that never got a Hermes schedule."""
        if self._priority_queue_handler is None:
            return
        now = time.time()
        if now - self._kanban_auto_start_last_scan < 30.0:
            return
        self._kanban_auto_start_last_scan = now
        snap = self._cached_hermes_cron_snapshot()
        for board_info in self._collect_kanban_board_infos(require_cron_link=False):
            for task in board_info.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                column = str(task.get("column") or "").strip().lower()
                if column not in ("doing", "review"):
                    continue
                if self._auto_try_run_overdue_cron_for_task(task, snap):
                    continue
                self._auto_enqueue_doing_task(board_info, task, reason="stuck_doing")

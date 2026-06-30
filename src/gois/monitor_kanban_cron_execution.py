"""Priority queue callbacks, auto-enqueue, and delegation."""

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



class MonitorKanbanCronExecutionMixin:

    def _on_priority_queue_terminal_failure(self, card: Any) -> None:
        """Mark kanban when the priority queue gives up on a card."""
        workdir = str(getattr(card, "workdir", "") or "").strip()
        task_id = str(getattr(card, "task_id", "") or "").strip()
        error = str(getattr(card, "error", "") or "execução falhou").strip()
        kanban_file = getattr(card, "kanban_file", None)
        self._finish_kanban_execution_record(card, status="error", error=error)
        if workdir and task_id:
            self._mark_kanban_task_failed(
                workdir=workdir,
                task_id=task_id,
                error=error,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                source="fila de prioridades",
            )

    def _on_priority_queue_card_start(self, card: Any) -> None:
        """Move a kanban card to doing when the priority queue starts executing it."""
        workdir = str(getattr(card, "workdir", "") or "").strip()
        task_id = str(getattr(card, "task_id", "") or "").strip()
        kanban_file = getattr(card, "kanban_file", None)
        team_id = str(getattr(card, "team_id", "") or "").strip()
        if workdir and task_id:
            self._start_kanban_execution_record(card)
            self._move_kanban_task_to_doing(
                workdir=workdir,
                task_id=task_id,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                team_id=team_id,
            )
            self._stamp_kanban_task_executor(
                workdir=workdir,
                task_id=task_id,
                executor=str(getattr(card, "assignee", "") or "").strip(),
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                team_id=team_id,
            )

    def _start_kanban_execution_record(self, card: Any) -> None:
        """Open a Mongo execution-history record for a card run (idempotent per card)."""
        card_id = str(getattr(card, "id", "") or "").strip()
        workdir = str(getattr(card, "workdir", "") or "").strip()
        task_id = str(getattr(card, "task_id", "") or "").strip()
        if not card_id or not workdir or not task_id:
            return
        runs = getattr(self, "_kanban_exec_runs", None)
        if runs is None:
            runs = {}
            self._kanban_exec_runs = runs
        if card_id in runs:  # retry of the same card — keep the original record
            return
        try:
            from . import kanban_execution_history as history

            baseline = history.git_baseline(workdir)
            kanban_file = getattr(card, "kanban_file", None)
            execution_id = history.record_execution_start(
                workdir=workdir,
                task_id=task_id,
                team_id=str(getattr(card, "team_id", "") or "").strip(),
                kanban_file=str(kanban_file).strip() if kanban_file else None,
                executor=str(getattr(card, "assignee", "") or "").strip(),
                title=str(getattr(card, "title", "") or "").strip(),
                model_id=getattr(card, "model_id", None),
                baseline=baseline,
            )
            runs[card_id] = {"execution_id": execution_id, "baseline": baseline}
        except Exception as exc:  # noqa: BLE001 — history is best-effort
            log.debug("kanban exec history: start failed for %s: %s", task_id, exc)

    def _finish_kanban_execution_record(
        self, card: Any, *, status: str, error: str = ""
    ) -> None:
        """Finalize the Mongo execution-history record for a finished card run."""
        card_id = str(getattr(card, "id", "") or "").strip()
        runs = getattr(self, "_kanban_exec_runs", None)
        if not card_id or not isinstance(runs, dict):
            return
        info = runs.pop(card_id, None)
        if not info:
            return
        try:
            from . import kanban_execution_history as history

            workdir = str(getattr(card, "workdir", "") or "").strip()
            result = getattr(card, "result", None)
            progress = list(getattr(card, "progress", None) or [])
            files_changed = history.git_changed_files(workdir, info.get("baseline"))
            history.record_execution_finish(
                info.get("execution_id"),
                status=status,
                files_changed=files_changed,
                summary=history.extract_summary(result, progress),
                improvements=history.extract_improvements(result),
                progress=progress,
                output=str(getattr(card, "last_progress", "") or ""),
                error=error,
                result=result if isinstance(result, dict) else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("kanban exec history: finish failed: %s", exc)

    def _stamp_kanban_task_executor(
        self,
        *,
        workdir: str,
        task_id: str,
        executor: str,
        kanban_file: Optional[str] = None,
        team_id: str = "",
    ) -> None:
        """Persist last_executor on a kanban card for UI coloring after runs."""
        assignee = str(executor or "").strip()
        tid = str(task_id or "").strip()
        wd = str(workdir or "").strip()
        if not assignee or not tid or not wd:
            return
        base: dict[str, Any] = {"workdir": wd, "team_id": team_id}
        if kanban_file:
            base["kanban_file"] = kanban_file
        try:
            self.handle_hermes_kanban_action(
                {
                    **base,
                    "action": "update_task",
                    "task_id": tid,
                    "task": {
                        "last_executor": assignee,
                        "last_executed_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                self._system_actor(base),
            )
        except Exception as exc:
            log.debug("kanban: stamp last_executor failed for %s: %s", tid, exc)

    def _move_kanban_task_to_doing(
        self,
        *,
        workdir: str,
        task_id: str,
        kanban_file: Optional[str] = None,
        team_id: str = "",
    ) -> bool:
        """Best-effort move of a task into the doing column before robot execution."""
        from .hermes_kanban import _COLUMN_ALIASES, get_board, resolve_work_column_id

        wd = str(workdir or "").strip()
        tid = str(task_id or "").strip()
        if not wd or not tid:
            return False
        try:
            board = get_board(
                wd,
                self.cfg.hermes_agent_create,
                kanban_file=kanban_file,
            )
            task = next(
                (
                    row
                    for row in (board.get("tasks") or [])
                    if isinstance(row, dict)
                    and str(row.get("id") or "").strip() == tid
                ),
                None,
            )
            if not isinstance(task, dict):
                return False
            current_col = str(task.get("column") or "").strip().lower()
            doing_cols = _COLUMN_ALIASES.get("doing", {"doing"})
            review_cols = _COLUMN_ALIASES.get("review", {"review"})
            done_cols = _COLUMN_ALIASES.get("done", {"done"})
            if (
                current_col in doing_cols
                or current_col in review_cols
                or current_col in done_cols
            ):
                return False
            doing_col = resolve_work_column_id(board, "doing")
            base: dict[str, Any] = {"workdir": wd, "team_id": team_id}
            if kanban_file:
                base["kanban_file"] = kanban_file
            result = self.handle_hermes_kanban_action(
                {
                    **base,
                    "action": "move_task",
                    "task_id": tid,
                    "column": doing_col,
                },
                self._system_actor(base),
            )
            if result.get("ok"):
                log.info(
                    "kanban: card %s movido para %s ao iniciar execução do robô",
                    tid,
                    doing_col,
                )
                return True
            log.debug(
                "kanban: falha ao mover %s para %s: %s",
                tid,
                doing_col,
                result.get("error"),
            )
        except Exception as exc:
            log.debug("kanban: move to doing failed for %s: %s", tid, exc)
        return False

    def _on_priority_queue_card_done(self, card: Any) -> None:
        """After a card finishes, chain the next open card from the same team."""
        workdir = str(getattr(card, "workdir", "") or "").strip()
        task_id = str(getattr(card, "task_id", "") or "").strip()
        assignee = str(getattr(card, "assignee", "") or "").strip()
        team_id = str(getattr(card, "team_id", "") or "").strip()
        kanban_file = getattr(card, "kanban_file", None)
        self._finish_kanban_execution_record(card, status="done")
        if not workdir or not task_id or not assignee:
            return
        self._stamp_kanban_task_executor(
            workdir=workdir,
            task_id=task_id,
            executor=assignee,
            kanban_file=str(kanban_file).strip() if kanban_file else None,
            team_id=team_id,
        )
        board_info = {
            "workdir": workdir,
            "team_id": team_id,
            "kanban_file": str(kanban_file).strip() if kanban_file else None,
        }
        self._chain_next_team_card(
            board_info,
            completed_task_id=task_id,
            assignee=assignee,
            reason="priority_queue_done",
        )

    def _chain_next_team_card(
        self,
        board_info: dict[str, Any],
        *,
        completed_task_id: str,
        assignee: str,
        reason: str = "",
    ) -> bool:
        """Assign and enqueue the next kanban card for the team until backlog is empty."""
        from .swarm_robots import (
            _build_assignee_alias_map,
            _load_kanban_context,
            _norm_slug,
            _pick_team_card_for_schedule,
            resolve_delegate_assignee,
            team_leader_slug,
        )

        team_id = str(board_info.get("team_id") or "").strip()
        workdir = str(board_info.get("workdir") or "").strip()
        kanban_file = board_info.get("kanban_file")
        if not team_id or not workdir:
            return False

        team = next(
            (row for row in self.accounts.list_all_teams() if row.id == team_id),
            None,
        )
        if team is None:
            return False

        boards, _ = _load_kanban_context(self, None)
        alias_map = _build_assignee_alias_map(
            {
                _norm_slug(assignee): {"slug": assignee, "display_name": assignee},
            }
        )
        pick = _pick_team_card_for_schedule(
            assignee,
            {team_id},
            boards,
            alias_map,
            exclude_task_ids={str(completed_task_id or "").strip()},
            cron_snap=self._cached_hermes_cron_snapshot(),
            pq_task_ids=self._priority_queue_task_ids(),
        )
        if not pick or not pick.get("task_id"):
            log.debug(
                "chain-next: sem mais cards para %s no time %s (%s)",
                assignee,
                team_id,
                reason or "done",
            )
            return False

        task_id = str(pick["task_id"]).strip()
        board_data: Optional[dict[str, Any]] = None
        try:
            board_data = self.accounts.read_kanban(team.id, team.owner_id)
        except Exception:
            pass
        task_row = None
        if isinstance(board_data, dict):
            task_row = next(
                (
                    row
                    for row in (board_data.get("tasks") or [])
                    if isinstance(row, dict)
                    and str(row.get("id") or "").strip() == task_id
                ),
                None,
            )

        resolved_assignee = assignee
        delegated = False
        if isinstance(task_row, dict):
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
            from .hermes_kanban import normalize_assignees

            load_counts: dict[str, int] = {}
            if isinstance(board_data, dict):
                for row in board_data.get("tasks") or []:
                    if not isinstance(row, dict):
                        continue
                    for slug in normalize_assignees(
                        row.get("assignees") or row.get("assignee")
                    ):
                        load_counts[slug] = load_counts.get(slug, 0) + 1
            resolved_assignee, delegated = resolve_delegate_assignee(
                task_row,
                assignee,
                agent_rows,
                leader_slug=leader,
                load_counts=load_counts,
            )

        base: dict[str, Any] = {
            "workdir": str(pick.get("workdir") or workdir),
            "team_id": team_id,
        }
        if pick.get("kanban_file") is not None:
            base["kanban_file"] = pick.get("kanban_file")
        elif kanban_file:
            base["kanban_file"] = kanban_file

        assign_payload = {
            **base,
            "action": "assign_task",
            "task_id": task_id,
            "assignees": [resolved_assignee],
        }
        assign_result = self.handle_hermes_kanban_action(
            assign_payload,
            self._system_actor(base),
        )
        if not assign_result.get("ok"):
            log.warning(
                "chain-next: falha ao atribuir %s → %s: %s",
                task_id,
                resolved_assignee,
                assign_result.get("error"),
            )
            return False

        if delegated:
            self.handle_hermes_kanban_action(
                {
                    **base,
                    "action": "update_task",
                    "task_id": task_id,
                    "task": {
                        "notes": (
                            f"Delegado pelo líder do time para `{resolved_assignee}` "
                            "(próximo card da fila)."
                        ),
                    },
                },
                self._system_actor(base),
            )

        move_result = self.handle_hermes_kanban_action(
            {
                **base,
                "action": "move_task",
                "task_id": task_id,
                "column": "doing",
            },
            self._system_actor(base),
        )
        if not move_result.get("ok"):
            return False

        task = next(
            (
                row
                for row in (move_result.get("tasks") or [])
                if isinstance(row, dict)
                and str(row.get("id") or "").strip() == task_id
            ),
            task_row if isinstance(task_row, dict) else {"id": task_id},
        )
        chain_board = {
            **base,
            "workdir": str(pick.get("workdir") or workdir),
        }
        enqueued = self._auto_enqueue_doing_task(
            chain_board,
            task if isinstance(task, dict) else {"id": task_id, "column": "doing"},
            reason=reason or "chain_next",
        )
        if enqueued:
            log.info(
                "chain-next: %s → %s assignee=%s delegated=%s",
                completed_task_id,
                task_id,
                resolved_assignee,
                delegated,
            )
        return enqueued


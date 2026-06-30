"""Kanban board discovery and cron status application."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)



class MonitorKanbanCronBoardsMixin:

    def _collect_kanban_boards_with_cron_cards(self) -> list[dict]:
        """Return kanban boards that have at least one cron-linked card."""
        return self._collect_kanban_board_infos(require_cron_link=True)

    def _collect_kanban_board_infos(self, *, require_cron_link: bool = False) -> list[dict]:
        """Discover kanban boards from cron workdirs, profiles, and account teams."""
        from .hermes_kanban import load_kanban
        from .hermes_project_agents import resolve_kanban_path
        from .kanban_mongo import kanban_board_available

        boards: list[dict] = []
        create_cfg = self.cfg.hermes_agent_create
        seen_paths: set[str] = set()

        def _append_board(
            *,
            wd: Path,
            kp: Path,
            tasks: list[dict],
            team_id: str = "",
        ) -> None:
            if require_cron_link and not any(
                str(t.get("cron_job_id") or "").strip() for t in tasks
            ):
                return
            kp_str = str(kp)
            if kp_str in seen_paths:
                return
            seen_paths.add(kp_str)
            boards.append(
                {
                    "workdir": str(wd),
                    "kanban_path": kp_str,
                    "kanban_file": (
                        str(kp.relative_to(wd))
                        if kp.is_relative_to(wd)
                        else str(kp.name)
                    ),
                    "tasks": tasks,
                    "team_id": team_id,
                }
            )

        snap = self._cached_hermes_cron_snapshot()
        workdirs: list[str] = []
        for job in snap.get("jobs") or []:
            wd = str(job.get("workdir") or "").strip()
            if wd:
                workdirs.append(wd)

        for profile in self._hermes_profiles_cache_all:
            if not isinstance(profile, dict):
                continue
            project = profile.get("project")
            if isinstance(project, dict):
                wd = str(project.get("workdir") or project.get("local_path") or "").strip()
                if wd:
                    workdirs.append(wd)

        for wd_raw in workdirs:
            try:
                wd = Path(wd_raw).expanduser().resolve()
                if not wd.is_dir():
                    continue
                kp = resolve_kanban_path(wd, create_cfg)
                if not kanban_board_available(kp):
                    continue
                board = load_kanban(kp)
                _append_board(wd=wd, kp=kp, tasks=board.get("tasks") or [])
            except Exception as exc:
                log.debug("kanban board scan: skipping workdir %s: %s", wd_raw, exc)

        try:
            teams_data = (self.accounts._load().get("teams") or {})
        except Exception as exc:
            log.debug("kanban board scan: cannot load account teams: %s", exc)
            teams_data = {}

        for team_id, row in teams_data.items():
            if not isinstance(row, dict):
                continue
            tid = str(row.get("id") or team_id or "").strip()
            if not tid:
                continue
            project_source = str(row.get("project_source") or "").strip().lower()
            local_path = str(row.get("local_path") or "").strip()
            try:
                if project_source == "local" and local_path:
                    wd = Path(local_path).expanduser().resolve()
                elif project_source == "github":
                    wd = self.accounts.team_repo_dir(tid).resolve()
                else:
                    wd = self.accounts.team_dir(tid).resolve()
                if not wd.is_dir():
                    continue
                kp = self.accounts.team_kanban_path(tid).resolve()
                if not kanban_board_available(kp):
                    continue
                board = load_kanban(kp)
                _append_board(
                    wd=wd,
                    kp=kp,
                    tasks=board.get("tasks") or [],
                    team_id=tid,
                )
            except Exception as exc:
                log.debug("kanban board scan: skipping account team %s: %s", tid, exc)

        return boards

    def _kanban_failure_column(self, column_ids: set[str]) -> Optional[str]:
        """Pick the best failure column present on a board."""
        for candidate in ("error", "blocked", "bloqueado"):
            if candidate in column_ids:
                return candidate
        return None

    def _mark_kanban_task_failed(
        self,
        *,
        workdir: str,
        task_id: str,
        error: str,
        kanban_file: Optional[str] = None,
        source: str = "swarm",
    ) -> bool:
        """Move a card to error/blocked and annotate why execution stopped."""
        from .hermes_kanban import apply_kanban_action, get_board

        wd = str(workdir or "").strip()
        tid = str(task_id or "").strip()
        err = str(error or "execução falhou").strip()
        if not wd or not tid or not self.cfg.hermes_agent_create.enabled:
            return False

        try:
            board = get_board(wd, self.cfg.hermes_agent_create, kanban_file=kanban_file)
        except Exception as exc:
            log.warning("mark kanban failed: could not load board for %s: %s", tid, exc)
            return False

        task = next(
            (t for t in (board.get("tasks") or []) if str(t.get("id") or "").strip() == tid),
            None,
        )
        if not isinstance(task, dict):
            return False

        col_ids = {
            str(c.get("id") or "").strip().lower()
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        current_col = str(task.get("column") or "").strip().lower()
        failure_col = self._kanban_failure_column(col_ids)
        if failure_col and current_col == failure_col:
            return True
        note = f"⚠️ {source}: execução não concluída — {err[:240]}"
        existing_notes = str(task.get("notes") or "")
        if note in existing_notes:
            return True

        try:
            if failure_col and current_col not in (failure_col, "done"):
                apply_kanban_action(wd, self.cfg.hermes_agent_create, {
                    "action": "move_task",
                    "task_id": tid,
                    "column": failure_col,
                    "kanban_file": kanban_file,
                })
            apply_kanban_action(wd, self.cfg.hermes_agent_create, {
                "action": "update_task",
                "task_id": tid,
                "task": {
                    "notes": (
                        (str(task.get("notes") or "").strip() + "\n\n" + note).strip()
                    ),
                },
                "kanban_file": kanban_file,
            })
            log.info(
                "kanban: card %s marcado como falha (%s) — segue para o próximo",
                tid,
                failure_col or current_col,
            )
            return True
        except Exception as exc:
            log.warning("mark kanban failed for %s: %s", tid, exc)
            return False


    def _complete_kanban_task(
        self,
        *,
        workdir: str,
        task_id: str,
        result_comment: str,
        kanban_file: Optional[str] = None,
        team_id: str = "",
    ) -> bool:
        """Move a kanban card to done with a completion note."""
        from .hermes_kanban import _COLUMN_ALIASES, apply_kanban_action, get_board

        wd = str(workdir or "").strip()
        tid = str(task_id or "").strip()
        if not wd or not tid or not self.cfg.hermes_agent_create.enabled:
            return False
        try:
            board = get_board(wd, self.cfg.hermes_agent_create, kanban_file=kanban_file)
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
            done_cols = _COLUMN_ALIASES.get("done", {"done"})
            if current_col in done_cols:
                return True
            base: dict[str, Any] = {"workdir": wd, "team_id": team_id}
            if kanban_file:
                base["kanban_file"] = kanban_file
            apply_kanban_action(
                wd,
                self.cfg.hermes_agent_create,
                {
                    **base,
                    "action": "complete_task",
                    "task_id": tid,
                    "result_comment": result_comment,
                },
            )
            log.info("kanban: card %s → done (%s)", tid, result_comment[:80])
            return True
        except Exception as exc:
            log.warning("kanban complete failed for %s: %s", tid, exc)
            return False

    def _repair_doing_cards_with_cron_ok(
        self,
        boards: list[dict[str, Any]],
        all_jobs: list[dict],
        running_job_ids: set[str],
    ) -> None:
        """Complete doing/review cards whose Hermes cron already finished ok."""
        from .hermes_kanban import _COLUMN_ALIASES

        if not all_jobs:
            return
        jobs_by_id = {
            str(job.get("id") or "").strip(): job
            for job in all_jobs
            if isinstance(job, dict) and str(job.get("id") or "").strip()
        }
        doing_cols = _COLUMN_ALIASES.get("doing", {"doing"})
        review_cols = _COLUMN_ALIASES.get("review", {"review"})
        done_cols = _COLUMN_ALIASES.get("done", {"done"})
        for board_info in boards:
            workdir = str(board_info.get("workdir") or "").strip()
            if not workdir:
                continue
            kanban_file = board_info.get("kanban_file")
            kanban_file_str = (
                str(kanban_file).strip() if kanban_file is not None else None
            )
            team_id = str(board_info.get("team_id") or "").strip()
            for task in board_info.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    continue
                current_col = str(task.get("column") or "").strip().lower()
                if current_col in done_cols:
                    continue
                if current_col not in doing_cols and current_col not in review_cols:
                    continue
                cron_job_id = str(task.get("cron_job_id") or "").strip()
                if not cron_job_id or cron_job_id in running_job_ids:
                    continue
                job = jobs_by_id.get(cron_job_id)
                if not job:
                    continue
                if str(job.get("last_status") or "").strip().lower() != "ok":
                    continue
                last_run = str(job.get("last_run_at") or "").strip()
                job_name = str(job.get("name") or cron_job_id).strip()
                note = (
                    f"✅ Concluído pelo cron `{job_name}`"
                    + (f" em {last_run}" if last_run else "")
                    + " (sincronização automática)."
                )
                if self._complete_kanban_task(
                    workdir=workdir,
                    task_id=task_id,
                    result_comment=note,
                    kanban_file=kanban_file_str,
                    team_id=team_id,
                ):
                    assignees_raw = task.get("assignees") or task.get("assignee")
                    from .hermes_kanban import normalize_assignees

                    assignees = normalize_assignees(assignees_raw)
                    assignee = str(assignees[0] if assignees else "").strip()
                    if assignee:
                        self._chain_next_team_card(
                            board_info,
                            completed_task_id=task_id,
                            assignee=assignee,
                            reason="cron_ok_repair",
                        )

    def _apply_cron_status_to_card(
        self,
        *,
        board_info: dict,
        task: dict,
        job: dict,
        current_status: str,
        prev_status: str,
    ) -> None:
        """Update a kanban card based on cron job status transition."""
        from .hermes_kanban import apply_kanban_action

        workdir = board_info["workdir"]
        kanban_file = str(board_info.get("kanban_file") or "").strip() or None
        task_id = str(task.get("id") or "").strip()
        job_id = str(job.get("id") or "").strip()
        job_name = str(job.get("name") or job_id).strip()
        current_col = str(task.get("column") or "").strip().lower()

        try:
            if current_status == "running" and current_col not in ("doing", "done"):
                log.info(
                    "kanban sync: job %s started → moving %s to doing",
                    job_id, task_id,
                )
                apply_kanban_action(workdir, self.cfg.hermes_agent_create, {
                    "action": "move_task",
                    "task_id": task_id,
                    "column": "doing",
                    "kanban_file": kanban_file,
                })

            elif current_status == "ok" and current_col != "done":
                last_run = str(job.get("last_run_at") or "").strip()
                note = f"✅ Concluído pelo cron `{job_name}` em {last_run or 'data desconhecida'}."
                log.info(
                    "kanban sync: job %s ok → completing card %s",
                    job_id, task_id,
                )
                apply_kanban_action(workdir, self.cfg.hermes_agent_create, {
                    "action": "complete_task",
                    "task_id": task_id,
                    "result_comment": note,
                    "kanban_file": kanban_file,
                })
                from .hermes_kanban import normalize_assignees

                assignees = normalize_assignees(
                    task.get("assignees") or task.get("assignee")
                )
                assignee = str(assignees[0] if assignees else "").strip()
                if assignee:
                    self._chain_next_team_card(
                        board_info,
                        completed_task_id=task_id,
                        assignee=assignee,
                        reason="cron_ok",
                    )

            elif current_status == "error":
                last_run = str(job.get("last_run_at") or "").strip()
                last_error = str(job.get("last_error") or "").strip()
                error_detail = f": {last_error[:200]}" if last_error else ""
                note = (
                    f"⚠️ Erro no cron `{job_name}` em {last_run or 'data desconhecida'}"
                    f"{error_detail}"
                )
                log.warning(
                    "kanban sync: job %s error → adding note to card %s",
                    job_id, task_id,
                )
                # If the board has a dedicated "error" column (e.g. the scheduling team),
                # move the card there instead of leaving it in the current column.
                try:
                    from .hermes_kanban import load_kanban as _load_kanban
                    _kp = board_info.get("kanban_path")
                    if _kp:
                        _board = _load_kanban(Path(_kp))
                        _col_ids = {
                            str(c.get("id") or "").strip().lower()
                            for c in (_board.get("columns") or [])
                            if isinstance(c, dict)
                        }
                        if "error" in _col_ids and current_col not in ("error", "done"):
                            apply_kanban_action(workdir, self.cfg.hermes_agent_create, {
                                "action": "move_task",
                                "task_id": task_id,
                                "column": "error",
                                "kanban_file": kanban_file,
                            })
                except Exception:
                    pass
                apply_kanban_action(workdir, self.cfg.hermes_agent_create, {
                    "action": "update_task",
                    "task_id": task_id,
                    "task": {
                        "notes": (
                            (str(task.get("notes") or "").strip() + "\n\n" + note).strip()
                        ),
                    },
                    "kanban_file": kanban_file,
                })

        except Exception as exc:
            log.warning(
                "kanban sync: failed to update card %s (job %s, status %s): %s",
                task_id, job_id, current_status, exc,
            )

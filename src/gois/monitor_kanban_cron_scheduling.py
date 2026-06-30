"""Scheduling team and unassigned cron job kanban cards."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .hermes_kanban import apply_kanban_action
from .team_presets import SCHEDULING_KANBAN_COLUMNS

log = logging.getLogger(__name__)

_SCHEDULING_TEAM_ID_BASE = "time-agendamento"
_SCHEDULING_TEAM_NAME = "Time de Agendamento"
_SCHEDULING_TEAM_DESCRIPTION = (
    "Regra automática: todo job cron sem card em outros times "
    "é criado neste time de agendamento."
)


class MonitorKanbanCronSchedulingMixin:
    def _resolve_scheduling_owner_id(self) -> Optional[str]:
        """Choose a stable owner for the scheduling team.

        Preference order:
        1. The bootstrap admin user (username == cfg.auth.bootstrap_admin_username,
           typically "admin") — this is the real human operator visible in the dashboard.
        2. A user whose id is exactly "local" (legacy single-user installs).
        3. The owner of the bare "projeto-padrao" team (oldest default project).
        4. The owner of the oldest team overall.
        5. Any user id found in the store.
        """
        try:
            data = self.accounts._load()
        except Exception:
            return None

        users = data.get("users") or {}
        teams = data.get("teams") or {}
        if not isinstance(users, dict) or not isinstance(teams, dict):
            return None

        # 1. Bootstrap admin by username (configured or default "admin").
        admin_username = "admin"
        try:
            admin_username = self.cfg.auth.bootstrap_admin_username or "admin"
        except Exception:
            pass
        for row in users.values():
            if not isinstance(row, dict):
                continue
            if str(row.get("username") or "").strip() == admin_username:
                uid = str(row.get("id") or "").strip()
                if uid:
                    return uid

        # 2. Legacy "local" user.
        for row in users.values():
            if not isinstance(row, dict):
                continue
            uid = str(row.get("id") or "").strip()
            if uid == "local":
                return uid

        # 3. Owner of the bare "projeto-padrao" team.
        for row in teams.values():
            if not isinstance(row, dict):
                continue
            tid = str(row.get("id") or "").strip()
            owner = str(row.get("owner_id") or "").strip()
            if owner and tid == "projeto-padrao":
                return owner

        # 4. Owner of the oldest team.
        owners: list[tuple[float, str]] = []
        for row in teams.values():
            if not isinstance(row, dict):
                continue
            owner = str(row.get("owner_id") or "").strip()
            if not owner:
                continue
            try:
                created = float(row.get("created_at") or 0.0)
            except (TypeError, ValueError):
                created = 0.0
            owners.append((created, owner))
        if owners:
            owners.sort(key=lambda item: item[0])
            return owners[0][1]

        for row in users.values():
            if not isinstance(row, dict):
                continue
            uid = str(row.get("id") or "").strip()
            if uid:
                return uid
        return None

    def _scheduling_team_id_for_owner(self, owner_id: str) -> str:
        """Build an owner-safe scheduling team id."""
        data = self.accounts._load()
        teams = data.get("teams") or {}
        row = teams.get(_SCHEDULING_TEAM_ID_BASE) if isinstance(teams, dict) else None
        if not isinstance(row, dict) or str(row.get("owner_id") or "") == owner_id:
            return _SCHEDULING_TEAM_ID_BASE
        safe = re.sub(r"[^a-z0-9-]", "", str(owner_id).lower())[:16] or "owner"
        return f"{_SCHEDULING_TEAM_ID_BASE}-{safe}"

    def _ensure_scheduling_team(self, owner_id: str):
        """Create (or return) the dedicated scheduling team."""
        team_id = self._scheduling_team_id_for_owner(owner_id)
        try:
            team = self.accounts.get_team(team_id, owner_id)
        except ValueError:
            team = self.accounts.create_team(
                owner_id,
                name=_SCHEDULING_TEAM_NAME,
                team_id=team_id,
                description=_SCHEDULING_TEAM_DESCRIPTION,
                seed_kanban=True,
            )
        try:
            board = self.accounts.read_kanban(team.id, owner_id)
            tasks = [
                dict(task)
                for task in (board.get("tasks") or [])
                if isinstance(task, dict)
            ]
            desired_columns = [dict(col) for col in SCHEDULING_KANBAN_COLUMNS]
            desired_column_ids = {
                str(col.get("id") or "").strip().lower()
                for col in desired_columns
                if isinstance(col, dict)
            }
            current_column_ids = {
                str(col.get("id") or "").strip().lower()
                for col in (board.get("columns") or [])
                if isinstance(col, dict)
            }
            changed = current_column_ids != desired_column_ids
            for task in tasks:
                column = str(task.get("column") or "").strip().lower()
                if column in desired_column_ids:
                    continue
                if column in {"backlog", "todo", "scheduled", "schedule", "agendamento"}:
                    task["column"] = "agendado"
                elif column in {"review", "testes-usabilidade"}:
                    task["column"] = "doing"
                else:
                    task["column"] = "agendado"
                changed = True
            if changed:
                self.accounts.write_kanban(
                    team.id,
                    owner_id,
                    {"columns": desired_columns, "tasks": tasks},
                )
        except Exception as exc:
            log.warning("kanban scheduling: failed to normalize scheduling team board: %s", exc)
        return team

    def _ensure_unassigned_cron_jobs_scheduling_cards(
        self,
        all_jobs: list[dict[str, Any]],
        cron_to_cards: dict[str, list[tuple[dict, dict]]],
    ) -> Optional[dict]:
        """Create one scheduling card for each cron job without any kanban card.

        Returns a board_info dict for the scheduling team (suitable for inclusion
        in cron_to_cards) so that the caller can track status transitions for
        newly-created scheduling cards within the same tick.  Returns None if the
        scheduling team could not be resolved or has no cron-linked tasks.
        """
        linked_job_ids = {
            str(job_id).strip()
            for job_id in cron_to_cards.keys()
            if str(job_id).strip()
        }
        unassigned_jobs = [
            row
            for row in all_jobs
            if isinstance(row, dict)
            and str(row.get("id") or "").strip()
            and str(row.get("id") or "").strip() not in linked_job_ids
        ]
        if not unassigned_jobs and not cron_to_cards:
            return None

        owner_id = self._resolve_scheduling_owner_id()
        if not owner_id:
            return None

        try:
            team = self._ensure_scheduling_team(owner_id)
            board = self.accounts.read_kanban(team.id, owner_id)
        except Exception as exc:
            log.warning("kanban scheduling: cannot ensure scheduling team: %s", exc)
            return None

        scheduling_kanban_path = str(self.accounts.team_kanban_path(team.id).resolve())
        existing_job_ids = {
            str(task.get("cron_job_id") or "").strip()
            for task in (board.get("tasks") or [])
            if isinstance(task, dict) and str(task.get("cron_job_id") or "").strip()
        }
        workdir = str(self.accounts.team_dir(team.id).resolve())
        kanban_file = self.accounts.team_kanban_path(team.id).name

        # If a job is now linked in another board, remove its duplicate card
        # from the scheduling team so this lane only tracks truly unallocated jobs.
        linked_elsewhere_job_ids: set[str] = set()
        for raw_job_id, cards in cron_to_cards.items():
            job_id = str(raw_job_id or "").strip()
            if not job_id:
                continue
            for board_info, _task in cards:
                kp = str((board_info or {}).get("kanban_path") or "").strip()
                if kp and kp != scheduling_kanban_path:
                    linked_elsewhere_job_ids.add(job_id)
                    break

        stale_task_ids: list[str] = []
        cleared_assignee_task_ids: list[str] = []
        for task in (board.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            title = str(task.get("title") or "").strip()
            if title.startswith("Cron não alocado:") and (
                task.get("assignees") or task.get("assignee")
            ):
                task.pop("assignees", None)
                task.pop("assignee", None)
                tid = str(task.get("id") or "").strip()
                if tid:
                    cleared_assignee_task_ids.append(tid)
            task_id = str(task.get("id") or "").strip()
            job_id = str(task.get("cron_job_id") or "").strip()
            if task_id and job_id and job_id in linked_elsewhere_job_ids:
                stale_task_ids.append(task_id)

        # Write an audit note on destination cards before removing the duplicate
        # card from the scheduling team.
        audit_ts = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        for job_id in linked_elsewhere_job_ids:
            cards = cron_to_cards.get(job_id) or []
            for board_info, task in cards:
                target_kanban_path = str((board_info or {}).get("kanban_path") or "").strip()
                if not target_kanban_path or target_kanban_path == scheduling_kanban_path:
                    continue
                task_id = str((task or {}).get("id") or "").strip()
                if not task_id:
                    continue
                marker = f"[audit:scheduling-transfer:{job_id}]"
                current_notes = str((task or {}).get("notes") or "").strip()
                if marker in current_notes:
                    continue
                audit_note = (
                    f"{marker}\n"
                    f"Movido automaticamente do Time de Agendamento em {audit_ts} "
                    f"(job_id: {job_id})."
                )
                merged_notes = (
                    f"{current_notes}\n\n{audit_note}".strip()
                    if current_notes
                    else audit_note
                )
                workdir = str((board_info or {}).get("workdir") or "").strip()
                kanban_file = (board_info or {}).get("kanban_file")
                kanban_file_str = (
                    str(kanban_file).strip() if kanban_file is not None else None
                )
                if not workdir:
                    continue
                try:
                    from .hermes_kanban import get_board

                    board = get_board(
                        workdir,
                        self.cfg.hermes_agent_create,
                        kanban_file=kanban_file_str,
                    )
                    task_exists = any(
                        str(row.get("id") or "").strip() == task_id
                        for row in (board.get("tasks") or [])
                        if isinstance(row, dict)
                    )
                    if not task_exists:
                        log.debug(
                            "kanban scheduling: skip audit note for job %s; task %s missing on board",
                            job_id,
                            task_id,
                        )
                        continue
                    apply_kanban_action(
                        workdir,
                        self.cfg.hermes_agent_create,
                        {
                            "action": "update_task",
                            "kanban_file": kanban_file_str,
                            "task_id": task_id,
                            "task": {"notes": merged_notes},
                        },
                    )
                except Exception as exc:
                    log.warning(
                        "kanban scheduling: failed to append audit note for job %s in task %s: %s",
                        job_id,
                        task_id,
                        exc,
                    )

        for task_id in stale_task_ids:
            try:
                apply_kanban_action(
                    workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "delete_task",
                        "kanban_file": kanban_file,
                        "task_id": task_id,
                    },
                )
            except Exception as exc:
                log.warning(
                    "kanban scheduling: failed to remove stale scheduling card %s: %s",
                    task_id,
                    exc,
                )

        for task_id in cleared_assignee_task_ids:
            try:
                apply_kanban_action(
                    workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "update_task",
                        "kanban_file": kanban_file,
                        "task_id": task_id,
                        "task": {"assignees": [], "assignee": None},
                    },
                )
            except Exception as exc:
                log.warning(
                    "kanban scheduling: failed to clear assignee on card %s: %s",
                    task_id,
                    exc,
                )

        if stale_task_ids or cleared_assignee_task_ids:
            board = self.accounts.read_kanban(team.id, owner_id)
            existing_job_ids = {
                str(task.get("cron_job_id") or "").strip()
                for task in (board.get("tasks") or [])
                if isinstance(task, dict) and str(task.get("cron_job_id") or "").strip()
            }

        for job in unassigned_jobs:
            job_id = str(job.get("id") or "").strip()
            if not job_id or job_id in existing_job_ids:
                continue
            profile = str(job.get("profile") or "").strip()
            name = str(job.get("name") or job_id).strip()
            schedule = job.get("schedule")
            schedule_txt = ""
            if isinstance(schedule, dict):
                schedule_txt = str(
                    schedule.get("display") or schedule.get("expr") or ""
                ).strip()
            elif schedule is not None:
                schedule_txt = str(schedule).strip()
            status_txt = str(job.get("last_status") or "").strip() or "desconhecido"
            wd_txt = str(job.get("workdir") or "").strip()

            details = [
                f"Job cron sem alocação em outros times.",
                f"- job_id: {job_id}",
                f"- profile: {profile or '-'}",
                f"- schedule: {schedule_txt or '-'}",
                f"- status atual: {status_txt}",
            ]
            if wd_txt:
                details.append(f"- workdir: {wd_txt}")

            task_payload: dict[str, Any] = {
                "title": f"Cron não alocado: {name}",
                "description": "\n".join(details),
                "column": "agendado",
                "cron_job_id": job_id,
            }
            if schedule_txt:
                task_payload["cron_schedule"] = schedule_txt
            # Keep scheduling-lane cards unassigned — profile is in description only.
            # Assigning the cron profile would flood each robot's card list on /swarm.

            try:
                apply_kanban_action(
                    workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "create_task",
                        "kanban_file": kanban_file,
                        "task": task_payload,
                    },
                )
                existing_job_ids.add(job_id)
                log.info(
                    "kanban scheduling: created card for unassigned cron job %s in team %s",
                    job_id,
                    team.id,
                )
            except Exception as exc:
                log.warning(
                    "kanban scheduling: failed to create card for cron job %s: %s",
                    job_id,
                    exc,
                )

        # Re-read the board after all mutations so the caller gets up-to-date tasks
        # (including any cards just created) for status-transition tracking.
        try:
            final_board = self.accounts.read_kanban(team.id, owner_id)
            final_tasks = [
                t for t in (final_board.get("tasks") or []) if isinstance(t, dict)
            ]
            if not any(str(t.get("cron_job_id") or "").strip() for t in final_tasks):
                return None
            kanban_path_obj = self.accounts.team_kanban_path(team.id)
            return {
                "workdir": workdir,
                "kanban_path": scheduling_kanban_path,
                "kanban_file": kanban_path_obj.name,
                "tasks": final_tasks,
            }
        except Exception as exc:
            log.warning(
                "kanban scheduling: failed to read final scheduling board: %s", exc
            )
            return None

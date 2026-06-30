"""Hermes kanban schedule execution engine."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import UserRecord
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
from .hermes_kanban import apply_kanban_action, get_board
from .model_router import resolve_effective_model_id

log = logging.getLogger(__name__)


class MonitorHermesKanbanScheduleExecMixin:
    def _execute_kanban_schedule(
        self,
        payload: dict,
        user: Optional[UserRecord],
        *,
        progress_job_id: Optional[str] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> dict:
        blocked = self._model_quota_guard()
        if blocked is not None:
            return blocked
        prog = lambda msg: self._kanban_schedule_progress(  # noqa: E731
            msg, progress_job_id=progress_job_id, on_progress=on_progress
        )
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes:
            return {"ok": False, "error": "hermes is not configured"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        prog("A validar cartão e perfil Hermes…")

        raw_payload = dict(payload)
        client_board_workdir = str(raw_payload.get("workdir") or "").strip()
        team_id_hint = str(raw_payload.get("team_id") or "").strip()
        payload = self._prepare_kanban_payload(raw_payload, actor)
        board_workdir = str(payload.get("workdir") or "").strip()
        if not board_workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, board_workdir):
            if client_board_workdir and self._kanban_workdir_allowed(
                actor, client_board_workdir
            ):
                pass
            else:
                return {
                    "ok": False,
                    "error": "workdir não pertence ao usuário autenticado",
                }
        cron_anchor = client_board_workdir or board_workdir
        if actor is not None:
            tid = team_id_hint or str(payload.get("team_id") or "").strip()
            if tid:
                try:
                    team = self.accounts.get_team(tid, actor.id)
                    cron_anchor = str(self.accounts.team_workdir(team).resolve())
                except ValueError:
                    pass

        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}

        assignee = str(payload.get("assignee") or "").strip()
        if not assignee:
            return {"ok": False, "error": "assignee is required"}

        once_raw = payload.get("once", True)
        once = once_raw if isinstance(once_raw, bool) else str(once_raw).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        schedule = self._normalize_schedule_for_kanban(
            str(payload.get("schedule") or "").strip()
        )
        if once and not schedule:
            schedule = "1m"
        if not schedule:
            schedule = self.cfg.hermes_agent_create.default_schedule
        if not schedule:
            return {"ok": False, "error": "schedule is required"}

        kanban_file_raw = payload.get("kanban_file")
        if kanban_file_raw is not None and not isinstance(kanban_file_raw, str):
            return {"ok": False, "error": "kanban_file must be a string"}
        kanban_file = str(kanban_file_raw or "").strip() or None

        board = get_board(
            board_workdir,
            self.cfg.hermes_agent_create,
            kanban_file=kanban_file,
        )
        task = next(
            (t for t in (board.get("tasks") or []) if str(t.get("id") or "").strip() == task_id),
            None,
        )
        if not isinstance(task, dict):
            return {"ok": False, "error": f"tarefa {task_id} não encontrada"}

        current_col = str(task.get("column") or "").strip().lower()
        if current_col not in ("doing", "review", "done"):
            from .hermes_kanban import resolve_work_column_id

            doing_col = resolve_work_column_id(board, "doing")
            try:
                board = apply_kanban_action(
                    board_workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "move_task",
                        "task_id": task_id,
                        "column": doing_col,
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
                prog("Cartão movido para Em progresso…")
            except Exception as exc:
                log.debug(
                    "kanban schedule: não foi possível mover %s para %s: %s",
                    task_id,
                    doing_col,
                    exc,
                )

        payload_has_skills = "skills" in payload
        payload_skills = self._normalize_kanban_skills(payload.get("skills"))
        task_skills = self._normalize_kanban_skills(task.get("skills"))
        selected_skills = payload_skills or task_skills

        # Model override: prefer payload, fallback to task's saved model, then profile
        model_id = (
            str(raw_payload.get("model_id") or payload.get("model_id") or "").strip()
            or str(task.get("model_id") or "").strip()
            or None
        )
        if not model_id:
            from .hermes_profile_model import read_profile_model_default

            model_id = read_profile_model_default(assignee) or None
        if model_id and self.cfg.openclaw_chat.enabled:
            task_title = str(task.get("title") or task_id).strip()
            task_body = str(
                task.get("description") or task.get("text") or task.get("body") or ""
            ).strip()
            effective_model_id, route = resolve_effective_model_id(
                self.cfg.openclaw_chat,
                model_id,
                message=f"{task_title}\n{task_body}".strip(),
                role=assignee,
                skills=selected_skills,
                title=task_title,
            )
            if route:
                prog(f"Auto → {route.label} ({route.reason})")
            model_id = effective_model_id or model_id

        task_workdir = str(
            raw_payload.get("task_workdir")
            or payload.get("task_workdir")
            or task.get("workdir")
            or ""
        ).strip()
        cron_workdir = task_workdir or cron_anchor

        # Resolve artifacts_dir from team if available
        artifacts_dir_str = ""
        if actor is not None:
            tid = team_id_hint or str(payload.get("team_id") or "").strip()
            if tid:
                try:
                    team_obj = self.accounts.get_team(tid, actor.id)
                    artifacts_dir_str = str(self.accounts.team_artifacts_dir(team_obj))
                except ValueError:
                    pass

        resolve_anchors = [
            a
            for a in (cron_anchor, client_board_workdir, board_workdir)
            if str(a or "").strip()
        ]
        cron_wd_path = self._resolve_kanban_workdir(
            cron_workdir, cron_anchor, extra_anchors=resolve_anchors[1:]
        )
        if cron_wd_path is None:
            return {
                "ok": False,
                "error": (
                    f"pasta inválida ou inacessível: {cron_workdir or '(vazia)'} "
                    "(use um caminho absoluto, ex. /Users/voce/projeto)"
                ),
            }
        if not self.cfg.hermes_agent_create.kanban_allow_any_workdir:
            if not self._kanban_workdir_allowed(
                actor, str(cron_wd_path), anchor_workdir=cron_anchor
            ):
                return {
                    "ok": False,
                    "error": "pasta da tarefa não pertence ao usuário autenticado",
                }
        if not cron_wd_path.is_dir():
            return {"ok": False, "error": f"pasta não existe: {cron_wd_path}"}

        dashboard_url = self._hermes_dashboard_url()
        assert dashboard_url is not None
        resolved_assignee = self._resolve_kanban_assignee(assignee, actor)
        if not resolved_assignee:
            return self._kanban_assignee_error(assignee)
        assignee = resolved_assignee

        from .hermes_profile_model import read_profile_execution_backend
        from .kanban_ide_handoff import is_ide_execution_backend, run_kanban_ide_handoff

        execution_backend = read_profile_execution_backend(assignee)
        if is_ide_execution_backend(execution_backend):
            prog(
                f"Handoff IDE → {execution_backend} "
                f"(contexto + abrir ferramenta)…"
            )
            kanban_file_name = str(board.get("kanban_file") or kanban_file or "").strip()
            handoff = run_kanban_ide_handoff(
                repo_root=cron_wd_path,
                ide=execution_backend,
                task=task,
                workdir=board_workdir,
                kanban_file=kanban_file_name,
                base_url=dashboard_url,
                open_ide=True,
            )
            if not handoff.get("ok"):
                return handoff
            return {
                **handoff,
                "task_id": task_id,
                "assignee": assignee,
                "workdir": str(cron_wd_path),
                "kanban_file": kanban_file_name,
            }

        persist_assign = payload.get("persist_assign", True)
        if persist_assign if isinstance(persist_assign, bool) else str(
            persist_assign
        ).strip().lower() not in ("0", "false", "no", "off"):
            prog("A atualizar responsável no kanban…")
            task_patch: dict[str, Any] = {"assignees": [assignee]}
            if task_workdir:
                task_patch["workdir"] = task_workdir
            if selected_skills or payload_has_skills:
                task_patch["skills"] = selected_skills
            try:
                board = apply_kanban_action(
                    board_workdir,
                    self.cfg.hermes_agent_create,
                    {
                        "action": "update_task",
                        "task_id": task_id,
                        "task": task_patch,
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
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        title = str(task.get("title") or task_id).strip()
        skills_block = ""
        if selected_skills:
            joined = ", ".join(f"`{name}`" for name in selected_skills)
            skills_block = (
                f"- Skills obrigatórias para esta tarefa: {joined}\n"
                "- Antes de codar, carregar as skills obrigatórias e seguir as instruções delas.\n"
            )

        prompt = (
            "Você deve executar UMA tarefa do kanban.\n"
            f"- Workdir: `{cron_wd_path}`\n"
            f"- Kanban (projeto): `{board_workdir}` / `{board.get('kanban_file')}`\n"
            + (f"- Diretório de artefatos: `{artifacts_dir_str}`\n" if artifacts_dir_str else "")
            + f"- Tarefa alvo: `{task_id}` - {title}\n"
            f"{skills_block}"
            "- Passos obrigatórios:\n"
            "  1) Abrir o kanban e localizar a tarefa alvo.\n"
            "  2) Se não estiver em done, mover para doing e executar o trabalho.\n"
            "  3) Ao concluir, mover para done, preencher completed_at (ISO) e atualizar notes com resumo + commit.\n"
            "  4) Se já estiver done, apenas reportar status sem iniciar outra tarefa.\n"
            "- Não escolher outra tarefa.\n"
            + ("- Salvar artefatos gerados (relatórios, builds, exports) no diretório de artefatos.\n" if artifacts_dir_str else "")
        )
        return self._finalize_kanban_schedule_cron(
            prog=prog,
            assignee=assignee,
            task_id=task_id,
            schedule=schedule,
            once=once,
            cron_wd_path=cron_wd_path,
            board_workdir=board_workdir,
            board=board,
            task=task,
            kanban_file=kanban_file,
            selected_skills=selected_skills,
            payload_has_skills=payload_has_skills,
            model_id=model_id,
            prompt=prompt,
        )

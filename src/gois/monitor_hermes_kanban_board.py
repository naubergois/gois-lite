"""Hermes kanban attachments, chat card create, and source viewer."""

from __future__ import annotations

import logging
import mimetypes
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .accounts import UserRecord
from .hermes_kanban import (
    apply_kanban_action,
    copy_kanban_attachment,
    delete_kanban_attachment,
    get_board,
    get_kanban_attachment,
    get_kanban_attachment_from_stored_path,
    get_kanban_attachments_zip,
    kanban_file_for_project,
    move_kanban_attachment,
    normalize_assignees,
    projects_from_profiles,
    resolve_assignee_profile_slug,
    resolve_board_paths,
    save_kanban_attachment,
    save_kanban_attachment_bytes,
    suggest_assignee_for_task,
    team_agents_from_profiles,
)

log = logging.getLogger(__name__)


class MonitorHermesKanbanBoardMixin:
    def _kanban_attachment_max_bytes(self) -> int:
        return int(
            getattr(self.cfg.hermes_agent_create, "max_kanban_attachment_bytes", 0)
            or 209_715_200
        )

    def _kanban_attachment_size_error(self, size: int, file_name: str) -> dict | None:
        limit = self._kanban_attachment_max_bytes()
        if limit > 0 and size > limit:
            mb = limit / (1024 * 1024)
            return {
                "ok": False,
                "error": f"{file_name} excede o limite de {mb:.0f} MB para anexos do kanban",
            }
        return None

    def handle_hermes_kanban_attachment_upload(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(payload, actor)
        workdir = str(payload.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        file_name = str(payload.get("name") or payload.get("file_name") or "arquivo").strip() or "arquivo"
        mime_type = str(payload.get("mime_type") or "application/octet-stream").strip()
        data_b64 = str(payload.get("data_base64") or "").strip()
        file_path = str(payload.get("file_path") or "").strip()
        raw_bytes: bytes | None = None
        if file_path and not data_b64:
            src = Path(file_path).expanduser()
            if not src.is_file():
                return {"ok": False, "error": f"arquivo não encontrado: {file_path}"}
            try:
                raw_bytes = src.read_bytes()
            except OSError as exc:
                return {"ok": False, "error": f"cannot read file: {exc}"}
            if file_name in {"", "arquivo"}:
                file_name = src.name
            if mime_type in {"", "application/octet-stream"}:
                mime_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            size_err = self._kanban_attachment_size_error(len(raw_bytes), file_name)
            if size_err:
                return size_err
        if not raw_bytes and not data_b64:
            return {"ok": False, "error": "data_base64 or file_path is required"}
        try:
            kanban_file = payload.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            if raw_bytes is not None:
                meta = save_kanban_attachment_bytes(
                    kp, task_id, file_name, mime_type, raw_bytes
                )
            else:
                import base64

                try:
                    decoded = base64.b64decode(
                        data_b64.partition(",")[2] if data_b64.startswith("data:") else data_b64,
                        validate=True,
                    )
                except Exception as exc:
                    return {"ok": False, "error": f"base64 inválido: {exc}"}
                size_err = self._kanban_attachment_size_error(len(decoded), file_name)
                if size_err:
                    return size_err
                meta = save_kanban_attachment(kp, task_id, file_name, mime_type, data_b64)
            return {"ok": True, **meta}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachment_upload_binary(
        self, query: dict, raw: bytes, user: Optional[UserRecord] = None
    ) -> dict:
        """Upload attachment from raw request body (for large videos in the kanban UI)."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        prepared = self._prepare_kanban_payload(dict(query), actor)
        workdir = str(prepared.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        task_id = str(query.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        if not raw:
            return {"ok": False, "error": "empty body"}
        file_name = str(query.get("name") or query.get("file_name") or "arquivo").strip() or "arquivo"
        mime_type = str(query.get("mime_type") or "application/octet-stream").strip()
        if mime_type in {"", "application/octet-stream"}:
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        size_err = self._kanban_attachment_size_error(len(raw), file_name)
        if size_err:
            return size_err
        try:
            kanban_file = prepared.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            meta = save_kanban_attachment_bytes(kp, task_id, file_name, mime_type, raw)
            return {"ok": True, **meta}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachment_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> tuple[bytes, str] | dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        prepared = self._prepare_kanban_payload(query, actor)
        workdir = str(prepared.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        task_id = str(query.get("task_id") or "").strip()
        safe_name = str(query.get("safe_name") or "").strip()
        stored_path = str(query.get("stored_path") or "").strip()
        if not task_id or (not safe_name and not stored_path):
            return {"ok": False, "error": "task_id and safe_name or stored_path are required"}
        try:
            kanban_file = prepared.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            if stored_path:
                try:
                    data, mime = get_kanban_attachment_from_stored_path(
                        kp, stored_path, task_id=task_id
                    )
                    return data, mime
                except FileNotFoundError:
                    if not safe_name:
                        raise
            try:
                data, mime = get_kanban_attachment(kp, task_id, safe_name)
                return data, mime
            except FileNotFoundError:
                if stored_path:
                    ext = Path(stored_path).expanduser()
                    if ext.is_file():
                        data = ext.read_bytes()
                        mime = mimetypes.guess_type(ext.name)[0] or "application/octet-stream"
                        return data, mime
                if not stored_path:
                    board = get_board(
                        workdir,
                        self.cfg.hermes_agent_create,
                        kanban_file=str(kanban_file).strip() if kanban_file else None,
                    )
                    for task in board.get("tasks") or []:
                        if not isinstance(task, dict) or task.get("id") != task_id:
                            continue
                        for att in task.get("attachments") or []:
                            if not isinstance(att, dict):
                                continue
                            att_safe = str(att.get("safe_name") or "").strip()
                            att_stored = str(att.get("stored_path") or "").strip()
                            if att_safe != safe_name and not att_stored:
                                continue
                            if att_stored:
                                data, mime = get_kanban_attachment_from_stored_path(
                                    kp, att_stored, task_id=task_id
                                )
                                return data, mime
                raise
        except FileNotFoundError:
            return {"ok": False, "error": "attachment not found"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachments_zip_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> tuple[bytes, str] | dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        prepared = self._prepare_kanban_payload(query, actor)
        workdir = str(prepared.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        task_id = str(query.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        try:
            kanban_file = prepared.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            folder = str(query.get("folder") or "").strip()
            data, fname = get_kanban_attachments_zip(kp, task_id, folder=folder)
            return data, fname
        except FileNotFoundError:
            return {"ok": False, "error": "attachments not found"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachment_delete(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(payload, actor)
        workdir = str(payload.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        task_id = str(payload.get("task_id") or "").strip()
        safe_name = str(payload.get("safe_name") or "").strip()
        if not task_id or not safe_name:
            return {"ok": False, "error": "task_id and safe_name are required"}
        try:
            kanban_file = payload.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            delete_kanban_attachment(kp, task_id, safe_name)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachment_move(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Move an attachment from one card to another."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(payload, actor)
        workdir = str(payload.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        source_task_id = str(payload.get("source_task_id") or "").strip()
        dest_task_id = str(payload.get("dest_task_id") or "").strip()
        safe_name = str(payload.get("safe_name") or "").strip()
        if not source_task_id or not dest_task_id or not safe_name:
            return {"ok": False, "error": "source_task_id, dest_task_id and safe_name are required"}
        try:
            kanban_file = payload.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            meta = move_kanban_attachment(kp, source_task_id, dest_task_id, safe_name)
            return {"ok": True, **meta}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_hermes_kanban_attachment_copy(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Copy an attachment from one card to another."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        payload = self._prepare_kanban_payload(payload, actor)
        workdir = str(payload.get("workdir") or "").strip()
        if not workdir:
            return {"ok": False, "error": "workdir is required"}
        if not self._kanban_workdir_allowed(actor, workdir):
            return {"ok": False, "error": "workdir não pertence ao usuário autenticado"}
        source_task_id = str(payload.get("source_task_id") or "").strip()
        dest_task_id = str(payload.get("dest_task_id") or "").strip()
        safe_name = str(payload.get("safe_name") or "").strip()
        if not source_task_id or not dest_task_id or not safe_name:
            return {"ok": False, "error": "source_task_id, dest_task_id and safe_name are required"}
        try:
            kanban_file = payload.get("kanban_file")
            _, kp = resolve_board_paths(
                workdir,
                self.cfg.hermes_agent_create,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
            meta = copy_kanban_attachment(kp, source_task_id, dest_task_id, safe_name)
            return {"ok": True, **meta}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_chat_kanban_create_card(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Create a Kanban card from QClaw chat, resolving team/workdir automatically."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}

        incoming = payload or {}
        task_raw = incoming.get("task") if isinstance(incoming.get("task"), dict) else {}
        title = str(task_raw.get("title") or incoming.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}

        task: dict[str, Any] = {
            "title": title,
            "description": str(task_raw.get("description") or incoming.get("description") or "").strip(),
            "column": str(task_raw.get("column") or incoming.get("column") or "todo").strip().lower() or "todo",
        }

        priority = task_raw.get("priority")
        if priority is None:
            priority = incoming.get("priority")
        if priority is not None and str(priority).strip() != "":
            try:
                task["priority"] = int(priority)
            except (TypeError, ValueError):
                return {"ok": False, "error": "priority must be an integer"}

        assignees = task_raw.get("assignees")
        if assignees is None:
            assignees = incoming.get("assignees")
        if isinstance(assignees, list):
            clean_assignees = [str(a).strip() for a in assignees if str(a).strip()]
            if clean_assignees:
                task["assignees"] = clean_assignees

        skills = task_raw.get("skills")
        if skills is None:
            skills = incoming.get("skills")
        if isinstance(skills, list):
            clean_skills = [str(s).strip() for s in skills if str(s).strip()]
            if clean_skills:
                task["skills"] = clean_skills

        if not str(task.get("id") or "").strip():
            explicit_id = str(
                task_raw.get("id")
                or incoming.get("task_id")
                or incoming.get("card_id")
                or incoming.get("id")
                or ""
            ).strip()
            if explicit_id:
                task["id"] = explicit_id

        team_id = str(incoming.get("team_id") or "").strip()
        workdir = str(incoming.get("workdir") or "").strip()
        if actor is not None and team_id:
            try:
                team_id = self.accounts.get_team(team_id, actor.id).id
            except ValueError:
                from .accounts_team_guard import resolve_team_identifier

                team_id = resolve_team_identifier(
                    self.accounts.data_dir, team_id, store=self.accounts
                )
        elif team_id:
            from .accounts_team_guard import resolve_team_identifier

            team_id = resolve_team_identifier(
                self.accounts.data_dir, team_id, store=self.accounts
            )
        if actor is not None and not team_id and workdir:
            try:
                wanted = Path(workdir).expanduser().resolve()
            except OSError:
                wanted = None
            if wanted is not None:
                for team in self.accounts.list_teams(actor.id):
                    try:
                        if wanted == self.accounts.team_workdir(team).resolve():
                            team_id = team.id
                            break
                    except OSError:
                        continue

        if actor is not None and not team_id:
            team_id = self.accounts.ensure_default_kanban_team(actor.id).id

        if not task.get("assignees") and actor is not None and team_id:
            try:
                team = self.accounts.get_team(team_id, actor.id)
                profile_rows = [
                    {"name": slug, "display_name": slug}
                    for slug in (team.profile_slugs or [])
                    if str(slug).strip()
                ]
                agents = team_agents_from_profiles(profile_rows)
                if len(agents) > 1:
                    load_counts: dict[str, int] = {}
                    try:
                        prepared = self._prepare_kanban_payload(
                            {"team_id": team_id, "workdir": workdir}, actor
                        )
                        board = get_board(
                            str(prepared.get("workdir") or workdir or ""),
                            self.cfg.hermes_agent_create,
                            kanban_file=str(prepared.get("kanban_file") or "").strip()
                            or None,
                        )
                        for row in board.get("tasks") or []:
                            if not isinstance(row, dict):
                                continue
                            for slug in normalize_assignees(
                                row.get("assignees") or row.get("assignee")
                            ):
                                load_counts[slug] = load_counts.get(slug, 0) + 1
                    except Exception:
                        load_counts = {}
                    suggested = suggest_assignee_for_task(
                        task, agents, load_counts=load_counts
                    )
                    if suggested:
                        task["assignees"] = [suggested]
            except ValueError:
                pass

        action_payload: dict[str, Any] = {
            "action": "create_task",
            "task": task,
        }
        if team_id:
            action_payload["team_id"] = team_id
        if workdir:
            action_payload["workdir"] = workdir

        out = self.handle_hermes_kanban_action(action_payload, user)
        if out.get("ok"):
            out["team_id"] = team_id or str(out.get("team_id") or "")
        return out

    def handle_team_card_from_file(
        self, team_id: str, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Create a new Kanban card for a team and attach an existing team file to it."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except Exception as e:  # noqa: BLE001 - surface resolution failure to client
            return {"ok": False, "error": f"Time não encontrado: {e}"}

        incoming = payload or {}
        rel = str(incoming.get("relative_path") or incoming.get("rel") or "").strip()
        file_path = str(incoming.get("path") or incoming.get("file") or "").strip()
        name = str(incoming.get("name") or incoming.get("file_name") or "").strip()
        if not rel and not file_path:
            return {"ok": False, "error": "relative_path or path is required"}

        from .team_files_download import resolve_team_file

        resolved = resolve_team_file(
            team_id=team.id,
            relative_path=rel,
            path=file_path,
        )
        if not resolved.get("ok"):
            return resolved
        resolved_tid = str(resolved.get("team_id") or "").strip()
        if resolved_tid and resolved_tid != team.id:
            return {"ok": False, "error": "arquivo não pertence a este time"}

        src = Path(str(resolved.get("path") or "")).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"arquivo não encontrado: {src.name}"}
        file_name = name or src.name

        title = str(incoming.get("title") or "").strip() or file_name
        column = str(incoming.get("column") or "todo").strip().lower() or "todo"
        description = str(incoming.get("description") or "").strip()

        created = self.handle_chat_kanban_create_card(
            {
                "team_id": team.id,
                "title": title,
                "column": column,
                "description": description,
            },
            user,
        )
        if not created.get("ok"):
            return {
                "ok": False,
                "stage": "create_card",
                "error": created.get("error") or "falha ao criar card",
                "response": created,
            }
        task_id = str(created.get("task_id") or "").strip()
        workdir = str(created.get("workdir") or "").strip()
        if not task_id or not workdir:
            return {
                "ok": False,
                "stage": "create_card",
                "error": "card criado mas sem id/workdir para anexar",
                "card": created,
            }

        try:
            raw = src.read_bytes()
        except OSError as exc:
            return {
                "ok": False,
                "stage": "attach",
                "error": f"cannot read file: {exc}",
                "card": created,
            }
        import base64

        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        attached = self.handle_hermes_kanban_attachment_upload(
            {
                "team_id": team.id,
                "workdir": workdir,
                "task_id": task_id,
                "name": file_name,
                "mime_type": mime,
                "data_base64": base64.b64encode(raw).decode("ascii"),
            },
            user,
        )
        return {
            "ok": bool(attached.get("ok")),
            "team_id": team.id,
            "task_id": task_id,
            "workdir": workdir,
            "title": title,
            "file_name": file_name,
            "card": created,
            "attachment": attached if attached.get("ok") else None,
            "attach_error": None if attached.get("ok") else attached.get("error"),
        }

    def handle_kanban_ide_handoff(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Hand off a kanban card to Kiro/Cursor/VS Code/Antigravity for local dev."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        from .kanban_ide_handoff_ops import kanban_ide_handoff_dispatch

        incoming = dict(payload or {})
        action = str(incoming.pop("action", None) or "handoff").strip().lower()
        prepared = self._prepare_kanban_payload(incoming, actor)

        def _kanban_get(query: dict[str, Any]) -> dict[str, Any]:
            q = self._prepare_kanban_payload(dict(query), actor)
            return self.handle_hermes_kanban_get(q, user)

        def _kanban_action(action_payload: dict[str, Any]) -> dict[str, Any]:
            return self.handle_hermes_kanban_action(action_payload, user)

        def _repo_root_resolver(task: dict[str, Any], board: dict[str, Any]) -> Path:
            task_wd = str(task.get("workdir") or "").strip()
            if task_wd:
                p = Path(task_wd).expanduser().resolve()
                if p.is_dir():
                    return p
            team_id = str(
                prepared.get("team_id") or board.get("team_id") or ""
            ).strip()
            if team_id:
                team = self.accounts.get_team(team_id, actor.id)
                return self.accounts.team_workdir(team).resolve()
            board_wd = str(board.get("workdir") or prepared.get("workdir") or "").strip()
            if board_wd:
                return Path(board_wd).expanduser().resolve()
            raise OSError("não foi possível resolver o repositório do card")

        return kanban_ide_handoff_dispatch(
            action,
            prepared,
            kanban_get_fn=_kanban_get,
            kanban_action_fn=_kanban_action,
            repo_root_resolver=_repo_root_resolver,
        )

    def handle_hermes_kanban_source(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Read generated source file content for Kanban task details."""
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not self.cfg.hermes_agent_create.enabled:
            return {"ok": False, "error": "hermes agent create is disabled"}

        board_hint = {
            "workdir": query.get("workdir"),
            "team_id": query.get("team_id"),
        }
        prepared = self._prepare_kanban_payload(board_hint, actor)
        board_workdir = self._sanitize_workdir_input(
            str(prepared.get("workdir") or query.get("workdir") or "")
        )
        if not board_workdir:
            return {"ok": False, "error": "workdir is required"}

        source_workdir_raw = self._sanitize_workdir_input(
            str(query.get("source_workdir") or board_workdir)
        )
        anchor_workdir = self._sanitize_workdir_input(
            str(query.get("anchor_workdir") or board_workdir)
        )

        source_root = self._resolve_kanban_workdir(
            source_workdir_raw,
            board_workdir,
        )
        if source_root is None:
            return {"ok": False, "error": "source_workdir inválido"}

        if not self._kanban_workdir_allowed(
            actor,
            str(source_root),
            anchor_workdir=anchor_workdir,
        ):
            return {
                "ok": False,
                "error": "source_workdir não pertence ao usuário autenticado",
            }

        rel_path = str(query.get("path") or query.get("file") or "").strip()
        if not rel_path:
            return {"ok": False, "error": "path is required"}
        rel_path = rel_path.strip('"').strip("'")

        raw_candidate = Path(rel_path).expanduser()
        try:
            if raw_candidate.is_absolute():
                candidate = raw_candidate.resolve()
            else:
                candidate = (source_root / raw_candidate).resolve()
        except OSError:
            return {"ok": False, "error": "path inválido"}

        if not self._path_under_or_equal(candidate, source_root):
            return {"ok": False, "error": "path fora do diretório permitido"}
        if not candidate.is_file():
            return {"ok": False, "error": "arquivo não encontrado"}

        max_bytes = 250000
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            return {"ok": False, "error": f"cannot read file: {exc}"}

        if b"\x00" in raw[:8192]:
            return {"ok": False, "error": "arquivo binário não suportado"}

        truncated = len(raw) > max_bytes
        payload = raw[:max_bytes]
        content = payload.decode("utf-8", errors="replace")

        try:
            display_path = str(candidate.relative_to(source_root))
        except ValueError:
            display_path = candidate.name

        out: dict[str, Any] = {
            "ok": True,
            "path": display_path,
            "workdir": str(source_root),
            "content": content,
            "truncated": truncated,
            "size": len(raw),
        }
        if truncated:
            out["max_bytes"] = max_bytes

        want_diff = str(query.get("diff") or "").lower() in {"1", "true", "yes", "on"}
        if want_diff:
            from .hermes_cron import read_git_file_diff

            diff_text = read_git_file_diff(source_root, display_path)
            if diff_text:
                out["content"] = diff_text
                out["mode"] = "diff"
            else:
                out["mode"] = "file"
        return out

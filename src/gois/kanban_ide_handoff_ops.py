"""Dispatch kanban card → IDE handoff (Kiro, Cursor, VS Code, Antigravity)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from .kanban_ide_handoff import (
    execution_backend_label,
    is_ide_execution_backend,
    list_execution_backends,
    normalize_execution_backend,
    run_kanban_ide_handoff,
    suggest_ide_for_task,
)
from .kanban_project_zip import resolve_project_dir


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _monitor_base_url() -> str:
    raw = os.environ.get("QCLAW_MONITOR_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    port = os.environ.get("QCLAW_MONITOR_PORT", "9101").strip() or "9101"
    host = os.environ.get("QCLAW_MONITOR_HOST", "127.0.0.1").strip() or "127.0.0.1"
    return f"http://{host}:{port}"


def _find_task(tasks: list[Any], task_id: str) -> Optional[dict[str, Any]]:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    for row in tasks:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or row.get("task_id") or "").strip() == tid:
            return row
    return None


def kanban_ide_handoff_dispatch(
    action: str,
    args: dict[str, Any],
    *,
    kanban_get_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    kanban_action_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    repo_root_resolver: Optional[Callable[[dict[str, Any], dict[str, Any]], Path]] = None,
) -> dict[str, Any]:
    """Shared handler for chat tool and MCP."""
    act = str(action or "handoff").strip().lower()
    payload = dict(args or {})

    if act in {"list", "list_backends", "backends"}:
        return {"ok": True, "backends": list_execution_backends()}

    if act in {"suggest", "suggest_ide"}:
        task = payload.get("task")
        if not isinstance(task, dict):
            return {"ok": False, "error": "task dict is required for suggest_ide"}
        suggested = suggest_ide_for_task(task)
        return {
            "ok": True,
            "suggested_ide": suggested,
            "label": execution_backend_label(suggested),
        }

    if act not in {"handoff", "run", "execute", ""}:
        return {"ok": False, "error": f"ação desconhecida: {action!r}"}

    task_id = str(payload.get("task_id") or payload.get("card_id") or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}

    ide_raw = str(payload.get("ide") or payload.get("backend") or "").strip()
    dry_run = _coerce_bool(payload.get("dry_run"), default=False)
    open_ide = _coerce_bool(payload.get("open_ide"), default=True)
    team_id = str(payload.get("team_id") or "").strip()
    workdir = str(payload.get("workdir") or "").strip()
    kanban_file = str(payload.get("kanban_file") or "").strip() or None

    board_payload: dict[str, Any] = {}
    if team_id:
        board_payload["team_id"] = team_id
    if workdir:
        board_payload["workdir"] = workdir
    if kanban_file:
        board_payload["kanban_file"] = kanban_file

    if kanban_get_fn is None:
        if team_id:
            from .team_swarm_ops import get_team_kanban

            fetched = get_team_kanban(team_id)
        elif workdir:
            from .team_swarm_ops import _request

            from urllib.parse import quote

            kf = kanban_file or "kanban.yaml"
            fetched = _request(
                "GET",
                "/hermes/kanban?"
                f"workdir={quote(workdir)}&kanban_file={quote(kf)}",
            )
        else:
            return {
                "ok": False,
                "error": "team_id ou workdir é obrigatório",
            }
    else:
        fetched = kanban_get_fn(board_payload)
    if not fetched.get("ok"):
        return fetched
    if isinstance(fetched.get("kanban"), dict):
        inner = dict(fetched["kanban"])
        fetched = {**fetched, **inner, "ok": fetched.get("ok", True)}
    board = fetched

    task = _find_task(list(board.get("tasks") or []), task_id)
    if task is None:
        return {"ok": False, "error": f"card {task_id} não encontrado no board"}

    ide = normalize_execution_backend(ide_raw) if ide_raw else suggest_ide_for_task(task)
    if not is_ide_execution_backend(ide):
        return {
            "ok": False,
            "error": f"IDE inválida: {ide_raw!r} (use kiro|cursor|vscode|antigravity)",
        }

    board_workdir = str(board.get("workdir") or workdir or "").strip()
    board_kanban_file = str(
        board.get("kanban_file") or kanban_file or "kanban.yaml"
    ).strip()

    repo_root: Optional[Path] = None
    if repo_root_resolver is not None:
        try:
            repo_root = repo_root_resolver(task, board)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    if repo_root is None:
        task_wd = str(task.get("workdir") or "").strip()
        resolved = resolve_project_dir(
            team_id=team_id,
            team_name=str(payload.get("team_name") or "").strip(),
            workdir=task_wd or workdir,
            path=str(payload.get("path") or payload.get("project_path") or "").strip(),
        )
        if not resolved.get("ok"):
            return resolved
        repo_root = Path(str(resolved.get("project_path") or "")).expanduser()
    if not repo_root.is_dir():
        return {"ok": False, "error": f"repositório não encontrado: {repo_root}"}

    if not dry_run and board_workdir:
        from .hermes_kanban import resolve_work_column_id

        current_col = str(task.get("column") or "").strip().lower()
        if current_col not in {"doing", "review", "done", "concluido", "concluído"}:
            doing_col = resolve_work_column_id(board, "doing")
            move_payload: dict[str, Any] = {
                "workdir": board_workdir,
                "kanban_file": board_kanban_file,
                "action": "move_task",
                "task_id": task_id,
                "column": doing_col,
            }
            if team_id:
                move_payload["team_id"] = team_id
            if kanban_action_fn is not None:
                moved = kanban_action_fn(move_payload)
            else:
                from .team_swarm_ops import _request

                moved = _request("POST", "/hermes/kanban", payload=move_payload)
            if not moved.get("ok"):
                return moved
            task = _find_task(list(moved.get("tasks") or []), task_id) or task
            task = dict(task)
            task["column"] = doing_col

    handoff = run_kanban_ide_handoff(
        repo_root=repo_root,
        ide=ide,
        task=task,
        workdir=board_workdir or str(repo_root),
        kanban_file=board_kanban_file,
        base_url=str(payload.get("base_url") or _monitor_base_url()),
        open_ide=open_ide and not dry_run,
    )
    if not handoff.get("ok"):
        return handoff

    return {
        **handoff,
        "task_id": task_id,
        "task_title": str(task.get("title") or task_id),
        "team_id": team_id or str(board.get("team_id") or ""),
        "repo_root": str(repo_root),
        "workdir": board_workdir or str(repo_root),
        "kanban_file": board_kanban_file,
        "dry_run": dry_run,
        "summary": (
            f"Handoff {execution_backend_label(ide)} — card {task_id} "
            f"({'dry-run' if dry_run else 'doing'}) — "
            f"{len(handoff.get('context_files') or [])} arquivo(s) de contexto"
        ),
    }

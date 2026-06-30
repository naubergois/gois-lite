"""Aggregate swarm robots, Hermes profiles, and kanban card assignments."""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TYPE_CHECKING

from datetime import datetime

from .hermes_cron import (
    CronJobsPauseSnapshot,
    collect_generated_files,
    compute_next_run_at_for_job,
    cron_next_run_is_plausible,
    list_cron_job_runs,
    read_jobs_file,
    remove_cron_jobs_from_file,
    remove_cron_jobs_for_profile,
    resolve_hermes_cron_jobs_path,
    resume_hermes_cron_jobs_from_snapshot,
    update_hermes_cron_job,
)
from .hermes_kanban import get_board, is_human_task
from .hermes_profile_model import (
    model_fields_for_profile,
    read_profile_config_dict,
    read_profile_display_name,
    read_profile_execution_backend,
    read_profile_meta_dict,
    read_profile_model_default,
    read_profile_skills,
    write_profile_meta,
    write_profile_model_default,
    write_profile_skills,
)
from .kanban_ide_handoff import (
    execution_backend_label,
    is_ide_execution_backend,
    list_execution_backends,
    normalize_execution_backend,
)
from .hermes_profiles import (
    AgentSpec,
    _PRESET_BY_ID,
    _append_skills_to_soul,
    _normalize_name,
    create_hermes_agent,
    create_hermes_profile_filesystem,
    hermes_profiles_root,
    preset_agent_spec,
)
from .hermes_project_agents import normalize_mascot
from .kanban_schedule_jobs import (
    job_to_dict as kanban_schedule_job_to_dict,
    list_recent_jobs as kanban_schedule_list_recent,
)
from .chat_jobs import list_running_jobs as chat_jobs_list_running
from .tool_progress import list_active_tool_runs
from .openai_swarm import (
    ensure_swarm_handoffs,
    infer_entry_handoff_targets,
    load_swarms_full,
    match_role_preset,
    register_robot_in_swarm,
    remove_robot_from_swarm,
    update_robot_in_swarm,
)

if TYPE_CHECKING:
    from .monitor import GoisMonitor

log = logging.getLogger(__name__)

from .swarm_robots_slugs import (
    _build_assignee_alias_map,
    _norm_slug,
    _profile_slug,
    _profile_slug_variants,
    _resolve_assignee_slug,
    _strip_accents,
)
from .swarm_robots_tombstones import (
    add_robot_tombstone,
    apply_tombstone_filters_to_cron_snapshot,
    clear_robot_tombstone,
    filter_tombstoned_cron_jobs,
    filter_tombstoned_profiles,
    load_robot_tombstones,
    _robot_tombstone_path,
    _save_robot_tombstones,
)


_SWARM_HINT_RE = (
    "swarm",
    "orelhao",
    "orelhão",
    "ruflo",
    "hive",
)

_ORCH_HINT_RE = (
    "orquestr",
    "orchestr",
    "coordena",
    "coordinator",
    "triage",
    "triagem",
    "lead",
    "supervisor",
    "gerente",
)



def _task_assignees(
    task: dict[str, Any],
    alias_map: Optional[dict[str, str]] = None,
) -> list[str]:
    raw = task.get("assignees") or task.get("assignee") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        slug = (
            _resolve_assignee_slug(text, alias_map)
            if alias_map
            else next(iter(_profile_slug_variants(text)), "")
        )
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _profile_meta(profile: dict[str, Any]) -> dict[str, Any]:
    name = str(profile.get("name") or profile.get("slug") or "").strip()
    color = normalize_robot_color(str(profile.get("color") or ""))
    if not color and name:
        disk = read_profile_meta_dict(name)
        color = normalize_robot_color(str(disk.get("color") or ""))
    return {
        "slug": name,
        "display_name": str(
            profile.get("display_name") or profile.get("label") or name
        ).strip(),
        "mascot": str(profile.get("mascot") or "").strip(),
        "description": str(profile.get("description") or "").strip(),
        "model_id": str(profile.get("model_id") or profile.get("model") or "").strip(),
        "execution_backend": str(profile.get("execution_backend") or "llm").strip() or "llm",
        "color": color,
    }


def _is_swarmish(text: str) -> bool:
    low = _norm_slug(text)
    return any(h in low for h in _SWARM_HINT_RE)


def _is_scheduling_lane_board(board: dict[str, Any]) -> bool:
    """True for the automatic cron scheduling team (not real agent work)."""
    team_id = _norm_slug(str(board.get("team_id") or ""))
    if not team_id:
        return False
    return team_id == "agendamentos" or team_id.startswith("time-agendamento")


def _is_scheduling_lane_card(
    task: dict[str, Any],
    board: Optional[dict[str, Any]] = None,
) -> bool:
    """Cards auto-created for unallocated cron jobs belong on the scheduling lane only."""
    if board and _is_scheduling_lane_board(board):
        return True
    title = str(task.get("title") or "").strip()
    return title.startswith("Cron não alocado:")


def _card_sort_key(card: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if card.get("column") == "doing" else 1,
        0 if card.get("column") == "review" else 1,
        str(card.get("title") or "").lower(),
    )


def _task_to_card(
    task: dict[str, Any],
    board: dict[str, Any],
    columns: dict[str, str],
) -> dict[str, Any]:
    workdir = str(board.get("workdir") or "")
    project_label = str(board.get("project_label") or workdir)
    team_id = str(board.get("team_id") or "").strip()
    col = _norm_slug(task.get("column"))
    card: dict[str, Any] = {
        "id": str(task.get("id") or ""),
        "title": str(task.get("title") or task.get("id") or "—"),
        "column": col,
        "column_label": columns.get(col) or col or "—",
        "priority": str(task.get("priority") or ""),
        "workdir": workdir,
        "project_label": project_label,
        "team_id": team_id,
        "status": col,
        "cron_job_id": str(task.get("cron_job_id") or "").strip(),
    }
    for key in (
        "working_by",
        "executing",
        "executor_slug",
        "last_executor",
        "last_executed_at",
        "color",
    ):
        value = task.get(key)
        if value is not None and str(value).strip() != "":
            card[key] = value
    return card


def _build_cards_by_assignee(
    boards: list[dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Single pass over all boards — O(tasks) instead of O(robots × tasks)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for board in boards:
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            card = _task_to_card(task, board, columns)
            for slug in _task_assignees(task, alias_map):
                index.setdefault(slug, []).append(card)
    for cards in index.values():
        cards.sort(key=_card_sort_key)
    return index


_DONE_KANBAN_COLS = frozenset({"done", "concluido", "concluído", "closed", "feito"})

_INCOMPLETE_OUTPUT_MARKERS = (
    "em andamento",
    "in progress",
    "pendente",
    "bloqueado",
    "blocked",
    "não conclu",
    "nao conclu",
    "incompleto",
    "parcial",
    "ainda não",
    "ainda nao",
    "não foi possível concluir",
    "nao foi possivel concluir",
    "não está done",
    "nao esta done",
)

_STRONG_DONE_MARKERS = (
    "tarefa concluída",
    "tarefa concluida",
    "card concluído",
    "card concluido",
    "movido para done",
    "movida para done",
    "movido para concluído",
    "movido para concluido",
    "status: done",
    "status done",
    "já está done",
    "ja esta done",
    "já está concluído",
    "ja esta concluido",
    "trabalho concluído",
    "trabalho concluido",
    "entrega concluída",
    "entrega concluida",
    "complete_task",
    "marcado como done",
)


def infer_kanban_task_completed(output: str, *, task_id: str = "") -> bool:
    """Heuristic: agent output signals the kanban card work is finished."""
    text = str(output or "").strip()
    if len(text) < 12:
        return False
    low = text.lower()
    if any(m in low for m in _INCOMPLETE_OUTPUT_MARKERS):
        return False
    tid = str(task_id or "").strip()
    if tid:
        explicit = re.search(
            rf"(?:conclu[ií]do|done|finalizado|completado)\s*:\s*{re.escape(tid)}",
            text,
            re.IGNORECASE,
        )
        if explicit:
            return True
        if tid.lower() in low and any(
            token in low for token in ("conclu", "done", "finaliz", "complet")
        ):
            return True
    if any(m in low for m in _STRONG_DONE_MARKERS):
        return True
    return False


def collect_swarm_completed_tasks(
    agent_cards: dict[str, list[dict[str, Any]]],
    outputs: dict[str, str],
) -> dict[str, str]:
    """Map task_id -> completion note inferred from per-agent swarm outputs."""
    completed: dict[str, str] = {}
    for slug, cards in (agent_cards or {}).items():
        output = str(outputs.get(slug) or "").strip()
        if not output:
            slug_key = _norm_slug(slug)
            for key, val in outputs.items():
                if _norm_slug(str(key)) == slug_key:
                    output = str(val or "").strip()
                    break
        if not output:
            continue
        for card in cards or []:
            if not isinstance(card, dict):
                continue
            tid = str(card.get("id") or "").strip()
            if not tid or tid in completed:
                continue
            if infer_kanban_task_completed(output, task_id=tid):
                snippet = output[:400].replace("\n", " ").strip()
                completed[tid] = f"✅ Concluído pelo swarm ({slug}): {snippet}"
    return completed


def _kanban_task_blocked_for_pick(
    task: dict[str, Any],
    *,
    cron_snap: Optional[dict[str, Any]] = None,
    pq_task_ids: Optional[set[str]] = None,
) -> bool:
    """True when a doing/review card should not be auto-picked again."""
    col = _norm_slug(task.get("column"))
    if col not in ("doing", "review"):
        return False
    task_id = str(task.get("id") or "").strip()
    if task_id and pq_task_ids and task_id in pq_task_ids:
        return True
    cron_job_id = str(task.get("cron_job_id") or "").strip()
    if not cron_job_id or not isinstance(cron_snap, dict):
        return False
    running_ids = {
        str(row.get("job_id") or "").strip()
        for row in (cron_snap.get("running") or [])
        if isinstance(row, dict) and str(row.get("job_id") or "").strip()
    }
    if cron_job_id in running_ids:
        return True
    job = _cron_job_record(cron_snap, cron_job_id)
    if not job:
        return False
    last_status = str(job.get("last_status") or "").strip().lower()
    if last_status == "ok":
        return True
    if bool(job.get("running")) or last_status == "running":
        return cron_job_id in running_ids or bool(job.get("running"))
    if bool(job.get("enabled", True)) and cron_next_run_is_plausible(job):
        return True
    return False


def finalize_swarm_kanban_cards(
    monitor: "GoisMonitor",
    *,
    team_ctx: dict[str, Any],
    run_result: dict[str, Any],
) -> list[str]:
    """Move team kanban cards to done when swarm outputs indicate completion."""
    if not run_result.get("ok") or run_result.get("paused") or run_result.get("rejected"):
        return []
    if not team_ctx.get("ok"):
        return []
    agent_cards = team_ctx.get("agent_cards") or {}
    outputs = run_result.get("outputs") or {}
    if not agent_cards or not isinstance(outputs, dict):
        return []
    completed = collect_swarm_completed_tasks(agent_cards, outputs)
    if not completed:
        return []

    workdir = str(team_ctx.get("workdir") or "").strip()
    team_id = str(team_ctx.get("team_id") or "").strip()
    if not workdir:
        return []

    kanban_file: Optional[str] = None
    if team_id:
        try:
            for row in monitor.accounts.list_all_teams():
                if str(row.id or "").strip() != team_id:
                    continue
                board = monitor.accounts.read_kanban(row.id, row.owner_id)
                if isinstance(board, dict) and board.get("kanban_file"):
                    kanban_file = str(board.get("kanban_file")).strip() or None
                break
        except Exception:
            pass

    base: dict[str, Any] = {"workdir": workdir, "team_id": team_id}
    if kanban_file:
        base["kanban_file"] = kanban_file
    actor = monitor._system_actor(base)
    done_ids: list[str] = []
    for task_id, comment in completed.items():
        result = monitor.handle_hermes_kanban_action(
            {
                **base,
                "action": "complete_task",
                "task_id": task_id,
                "result_comment": comment,
            },
            actor,
        )
        if result.get("ok"):
            done_ids.append(task_id)
            log.info("swarm finalize: card %s → done", task_id)
    return done_ids


def _build_open_cards_by_team(
    boards: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Index team_id -> open kanban cards for the schedule card picker."""
    index: dict[str, list[dict[str, Any]]] = {}
    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        if not team_id:
            continue
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if is_human_task(task):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            col = _norm_slug(task.get("column"))
            if col in _DONE_KANBAN_COLS:
                continue
            index.setdefault(team_id, []).append(_task_to_card(task, board, columns))
    for cards in index.values():
        cards.sort(key=_card_sort_key)
    return index


def _build_task_title_index(boards: list[dict[str, Any]]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()
            if tid and tid not in titles:
                titles[tid] = str(task.get("title") or tid)
    return titles


_TASK_ID_TOKEN_RE = re.compile(r"\b(TASK-\d+)\b", re.IGNORECASE)


def _build_task_team_index(boards: list[dict[str, Any]]) -> dict[str, str]:
    """Map kanban task id -> owning team/swarm board."""
    out: dict[str, str] = {}
    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        if not team_id:
            continue
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()
            if tid:
                out[tid] = team_id
    return out


def _extract_task_id_token(text: str) -> str:
    match = _TASK_ID_TOKEN_RE.search(str(text or ""))
    return match.group(1).upper() if match else ""


def _resolve_history_team_id(
    *,
    task_id: str = "",
    explicit_team_id: str = "",
    title: str = "",
    task_team_index: dict[str, str],
) -> str:
    explicit = str(explicit_team_id or "").strip()
    if explicit:
        return explicit
    tid = str(task_id or "").strip()
    if tid:
        resolved = task_team_index.get(tid, "")
        if resolved:
            return resolved
    from_title = _extract_task_id_token(title)
    if from_title:
        return task_team_index.get(from_title, "")
    return ""


def _build_task_lookup(boards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()
            if tid:
                lookup[tid] = task
    return lookup


def _format_job_result_text(
    result: Any,
    *,
    error: str = "",
) -> str:
    err = str(error or "").strip()
    if err:
        return err
    if not isinstance(result, dict):
        if result is None:
            return ""
        text = str(result).strip()
        return text
    for key in ("reply", "summary", "output", "message", "response", "text", "result"):
        val = result.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    if result.get("ok") is True:
        return "Concluído com sucesso."
    try:
        import json

        return json.dumps(result, ensure_ascii=False, indent=2)[:4000]
    except Exception:
        return str(result)[:4000]


def _task_deliverable_text(task: dict[str, Any]) -> str:
    notes = str(task.get("notes") or "").strip()
    impl = str(
        task.get("implementation_details") or task.get("implementation") or ""
    ).strip()
    desc = str(task.get("description") or "").strip()
    parts: list[str] = []
    if notes:
        parts.append(notes)
    if impl and impl not in notes:
        parts.append(impl)
    if not notes and desc:
        parts.append(desc)
    return "\n\n".join(parts).strip()


def _task_card_snapshot(
    task: dict[str, Any],
    board: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compact kanban card payload for activity / exec-log UI."""
    col = _norm_slug(task.get("column"))
    columns: dict[str, str] = {}
    if board:
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
    board_workdir = str(board.get("workdir") or "").strip() if board else ""
    project_label = (
        str(board.get("project_label") or board_workdir).strip() if board else ""
    )
    assignees = task.get("assignees")
    if not isinstance(assignees, list):
        assignees = []
    skills = task.get("skills")
    if not isinstance(skills, list):
        skills = []
    return {
        "id": str(task.get("id") or "").strip(),
        "title": str(task.get("title") or task.get("id") or "—").strip(),
        "column": col,
        "column_label": columns.get(col) or col or "—",
        "description": str(task.get("description") or "").strip(),
        "notes": str(task.get("notes") or "").strip(),
        "requirements": str(task.get("requirements") or "").strip(),
        "implementation_details": str(
            task.get("implementation_details") or task.get("implementation") or ""
        ).strip(),
        "implementation_location": str(
            task.get("implementation_location") or task.get("app_location") or ""
        ).strip(),
        "priority": task.get("priority"),
        "assignees": [str(a).strip() for a in assignees if str(a).strip()],
        "swarm": str(task.get("swarm") or task.get("swarm_name") or "").strip(),
        "skills": [str(s).strip() for s in skills if str(s).strip()],
        "workdir": str(task.get("workdir") or board_workdir).strip(),
        "project_label": project_label,
        "team_id": str(board.get("team_id") or "").strip() if board else "",
        "team_name": str(board.get("team_name") or "").strip() if board else "",
        "cron_job_id": str(task.get("cron_job_id") or "").strip(),
        "cron_schedule": str(task.get("cron_schedule") or "").strip(),
        "last_executor": str(task.get("last_executor") or "").strip(),
        "last_executed_at": task.get("last_executed_at"),
        "completed_at": task.get("completed_at"),
    }


def _build_task_board_index(
    boards: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map kanban task id -> board metadata for card snapshots."""
    out: dict[str, dict[str, Any]] = {}
    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()
            if tid:
                out[tid] = board
    return out


def _build_all_board_indices(
    boards: list[dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
) -> tuple[
    dict[str, list[dict[str, Any]]],  # cards_by_assignee
    dict[str, list[dict[str, Any]]],  # open_cards_by_team
    dict[str, str],                   # task_title_index
    dict[str, dict[str, Any]],        # task_lookup
    dict[str, dict[str, Any]],        # task_board_index
]:
    """Build all five board indices in a single pass over boards/tasks."""
    cards_by_assignee: dict[str, list[dict[str, Any]]] = {}
    open_cards_by_team: dict[str, list[dict[str, Any]]] = {}
    task_title_index: dict[str, str] = {}
    task_lookup: dict[str, dict[str, Any]] = {}
    task_board_index: dict[str, dict[str, Any]] = {}

    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()

            # task_lookup and task_board_index: all tasks
            if tid:
                task_lookup[tid] = task
                task_board_index[tid] = board
                if tid not in task_title_index:
                    task_title_index[tid] = str(task.get("title") or tid)

            is_scheduling = _is_scheduling_lane_card(task, board)
            is_human = is_human_task(task)

            # cards_by_assignee: skip scheduling lane and human-only cards
            if not is_scheduling and not is_human:
                card = _task_to_card(task, board, columns)
                for slug in _task_assignees(task, alias_map):
                    cards_by_assignee.setdefault(slug, []).append(card)

                # open_cards_by_team: skip done and scheduling lane
                if team_id:
                    col = _norm_slug(task.get("column"))
                    if col not in _DONE_KANBAN_COLS:
                        open_cards_by_team.setdefault(team_id, []).append(card)

    for cards in cards_by_assignee.values():
        cards.sort(key=_card_sort_key)
    for cards in open_cards_by_team.values():
        cards.sort(key=_card_sort_key)

    return cards_by_assignee, open_cards_by_team, task_title_index, task_lookup, task_board_index


def _cards_for_assignee(
    assignee: str,
    boards: list[dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
    *,
    cards_index: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    slug = _resolve_assignee_slug(assignee, alias_map) if alias_map else _profile_slug(assignee)
    if not slug:
        slug = _norm_slug(assignee)
    if not slug:
        return []
    if cards_index is not None:
        return list(cards_index.get(slug, []))
    cards: list[dict[str, Any]] = []
    for board in boards:
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if is_human_task(task):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            if slug not in _task_assignees(task, alias_map):
                continue
            cards.append(_task_to_card(task, board, columns))
    cards.sort(key=_card_sort_key)
    return cards


def _load_kanban_context(
    monitor: GoisMonitor,
    user: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    boards: list[dict[str, Any]] = []
    agents: list[dict[str, str]] = []
    if not monitor.cfg.hermes or not monitor.cfg.hermes_agent_create.enabled:
        return boards, agents
    try:
        projects_payload = monitor.handle_hermes_kanban_projects(user, quick=True)
    except Exception as exc:
        log.warning("swarm robots: kanban projects failed: %s", exc)
        return boards, agents
    if not projects_payload.get("ok"):
        return boards, agents
    for row in projects_payload.get("agents") or []:
        if isinstance(row, dict):
            agents.append(row)
    create_cfg = monitor.cfg.hermes_agent_create
    projects = [
        p for p in (projects_payload.get("projects") or [])
        if isinstance(p, dict) and str(p.get("workdir") or "").strip()
    ]

    actor = monitor._accounts_actor(user)

    def _load_one(project: dict[str, Any]) -> Optional[dict[str, Any]]:
        team_id = str(project.get("team_id") or "").strip()
        if team_id and actor is not None:
            try:
                board = monitor.accounts.read_kanban(team_id, actor.id)
                board["project_label"] = str(project.get("label") or team_id)
                return board
            except ValueError as exc:
                log.debug("swarm robots: skip team board %s: %s", team_id, exc)
        workdir = str(project.get("workdir") or "").strip()
        kanban_file = project.get("kanban_file")
        try:
            board = get_board(
                workdir,
                create_cfg,
                kanban_file=str(kanban_file).strip() if kanban_file else None,
            )
        except Exception as exc:
            log.debug("swarm robots: skip board %s: %s", workdir, exc)
            return None
        board["project_label"] = str(project.get("label") or workdir)
        board["team_id"] = str(project.get("team_id") or "")
        return board

    if len(projects) <= 1:
        for project in projects:
            board = _load_one(project)
            if board is not None:
                boards.append(board)
    else:
        workers = min(8, len(projects))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_load_one, project) for project in projects]
            try:
                for future in as_completed(futures, timeout=15):
                    try:
                        board = future.result()
                    except Exception as exc:
                        log.warning("swarm robots: kanban board load failed: %s", exc)
                        board = None
                    if board is not None:
                        boards.append(board)
            except FuturesTimeoutError:
                log.warning("swarm robots: kanban context timed out, using partial results")
    return boards, agents


_SCHEDULE_TEAM_REQUIRED_MSG = (
    "Para ser agendado, o agente deve estar atrelado a um time. "
    "Vincule o perfil ao time na aba Times e Swarm."
)
_SCHEDULE_CARD_REQUIRED_MSG = (
    "Modo card: nenhum card aberto encontrado nos times vinculados. "
    "Crie uma tarefa no kanban ou use o modo Time (escolha automática)."
)
_TEAM_AUTO_CRON_SUFFIX = " — kanban auto"


def _swarm_links_for_profile_slug(profile_slug: str) -> tuple[set[str], set[str]]:
    """Resolve swarm links for a profile: (swarm_names, swarm_team_ids).

    A profile that belongs to a swarm is considered linked to whichever team the
    swarm points at — either via ``team.swarm_name`` (matched by name) or via the
    swarm's own ``team_id`` field.
    """
    needle = _norm_slug(profile_slug)
    if not needle:
        return set(), set()
    swarm_names: set[str] = set()
    swarm_team_ids: set[str] = set()
    try:
        for swarm in load_swarms_full():
            if not isinstance(swarm, dict):
                continue
            profiles = {
                _norm_slug(str(p))
                for p in (swarm.get("hermes_profiles") or [])
                if str(p).strip()
            }
            agent_names = {
                _norm_slug(str(a.get("name")))
                for a in (swarm.get("agents") or [])
                if isinstance(a, dict) and str(a.get("name") or "").strip()
            }
            if needle not in profiles and needle not in agent_names:
                continue
            name = str(swarm.get("name") or "").strip()
            if name:
                swarm_names.add(name)
            team_id = str(swarm.get("team_id") or "").strip()
            if team_id:
                swarm_team_ids.add(team_id)
    except Exception as exc:
        log.debug("swarm robots: swarm links for profile %s unavailable: %s", profile_slug, exc)
    return swarm_names, swarm_team_ids


def _teams_for_profile_slug(
    monitor: GoisMonitor,
    profile_slug: str,
    user: Any = None,
) -> list[Any]:
    """Return account teams linked to this Hermes profile.

    A team counts as linked when the profile is in its ``profile_slugs`` *or*
    when the profile belongs to a swarm that the team is attached to (via
    ``team.swarm_name`` or the swarm's ``team_id``).
    """
    needle = _norm_slug(profile_slug)
    if not needle:
        return []
    swarm_names, swarm_team_ids = _swarm_links_for_profile_slug(profile_slug)
    teams: list[Any] = []
    try:
        monitor._accounts_actor(user)
        for team in monitor.accounts.list_all_teams():
            slugs = {_norm_slug(str(p)) for p in (team.profile_slugs or []) if str(p).strip()}
            team_swarm = str(getattr(team, "swarm_name", "") or "").strip()
            team_id = str(getattr(team, "id", "") or "").strip()
            if (
                needle in slugs
                or (team_swarm and team_swarm in swarm_names)
                or (team_id and team_id in swarm_team_ids)
            ):
                teams.append(team)
    except Exception as exc:
        log.debug("swarm robots: teams for profile %s unavailable: %s", profile_slug, exc)
    return teams


def _team_id_from_swarm_name(
    swarm_name: str,
    *,
    swarms_by_name: Optional[dict[str, dict[str, Any]]] = None,
    teams_index: Optional[dict[str, dict[str, str]]] = None,
) -> str:
    """Resolve a kanban team id from a swarm name (file link or team.swarm_name)."""
    name = str(swarm_name or "").strip()
    if not name:
        return ""
    if swarms_by_name:
        swarm = swarms_by_name.get(name) or {}
        tid = str(swarm.get("team_id") or "").strip()
        if tid:
            return tid
    if teams_index:
        for tid, meta in teams_index.items():
            if str(meta.get("swarm_name") or "").strip() == name:
                return str(tid or "").strip()
    return ""


def _resolve_robot_team_info(
    slug: str,
    *,
    team_map: dict[str, dict[str, str]],
    teams_index: dict[str, dict[str, str]],
    swarm_name: str = "",
    swarms_by_name: Optional[dict[str, dict[str, Any]]] = None,
    cards: Optional[list[dict[str, Any]]] = None,
) -> dict[str, str]:
    """Resolve team_id/team_name for UI + scheduling (profile, swarm, or kanban cards)."""
    tm = team_map.get(_norm_slug(slug)) or {}
    team_id = str(tm.get("team_id") or "").strip()
    team_name = str(tm.get("team_name") or "").strip()

    if not team_id:
        team_id = _team_id_from_swarm_name(
            swarm_name,
            swarms_by_name=swarms_by_name,
            teams_index=teams_index,
        )

    if not team_id and cards:
        card_team_ids = {
            str(c.get("team_id") or "").strip()
            for c in cards
            if str(c.get("team_id") or "").strip()
        }
        if len(card_team_ids) == 1:
            team_id = card_team_ids.pop()

    if team_id and not team_name:
        team_name = str((teams_index.get(team_id) or {}).get("team_name") or team_id)
    return {"team_id": team_id, "team_name": team_name}


def _team_ids_for_profile(
    monitor: GoisMonitor,
    profile_slug: str,
    user: Any = None,
    *,
    swarm_name: Optional[str] = None,
) -> set[str]:
    ids = {
        str(team.id or "").strip()
        for team in _teams_for_profile_slug(monitor, profile_slug, user)
        if str(team.id or "").strip()
    }
    if ids:
        return ids
    extra = _team_id_from_swarm_name(str(swarm_name or "").strip())
    return {extra} if extra else set()


def _pick_team_card_for_schedule(
    profile_slug: str,
    team_ids: set[str],
    boards: list[dict[str, Any]],
    alias_map: dict[str, str],
    *,
    exclude_task_ids: Optional[set[str]] = None,
    cron_snap: Optional[dict[str, Any]] = None,
    pq_task_ids: Optional[set[str]] = None,
) -> Optional[dict[str, Any]]:
    """Pick the best open kanban card from linked teams for Hermes scheduling."""
    slug = _norm_slug(profile_slug)
    if not slug or not team_ids:
        return None

    skip_ids = {
        str(tid or "").strip()
        for tid in (exclude_task_ids or set())
        if str(tid or "").strip()
    }

    assigned_active: list[dict[str, Any]] = []
    assigned_todo: list[dict[str, Any]] = []
    claimable: list[dict[str, Any]] = []
    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        if team_id not in team_ids:
            continue
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        kanban_file = board.get("kanban_file")
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            col = _norm_slug(task.get("column"))
            if col in _DONE_KANBAN_COLS:
                continue
            task_id = str(task.get("id") or "").strip()
            if task_id and task_id in skip_ids:
                continue
            if _kanban_task_blocked_for_pick(
                task,
                cron_snap=cron_snap,
                pq_task_ids=pq_task_ids,
            ):
                continue
            assignees = _task_assignees(task, alias_map)
            card = _task_to_card(task, board, columns)
            if kanban_file is not None:
                card["kanban_file"] = kanban_file
            if slug in assignees:
                if col in ("doing", "review"):
                    assigned_active.append(card)
                else:
                    assigned_todo.append(card)
            elif not assignees:
                claimable.append(card)

    candidates: list[dict[str, Any]] = []
    for pool in (assigned_active, assigned_todo, claimable):
        candidates.extend(pool)
    if not candidates:
        return None
    candidates.sort(key=_card_sort_key)
    best = candidates[0]
    return {
        "task_id": str(best.get("id") or "").strip(),
        "team_id": str(best.get("team_id") or "").strip(),
        "workdir": str(best.get("workdir") or "").strip(),
        "kanban_file": best.get("kanban_file"),
        "title": str(best.get("title") or best.get("id") or "").strip(),
        "priority": best.get("priority"),
        "skills": list(best.get("skills") or []),
    }


def _is_kanban_task_cron_job(job: dict[str, Any]) -> bool:
    prompt = str(job.get("prompt") or job.get("message") or "").strip().lower()
    return bool(prompt and any(marker in prompt for marker in _KANBAN_TASK_PROMPT_MARKERS))


def _task_id_from_kanban_cron_job(job: dict[str, Any]) -> Optional[str]:
    """Extract kanban task id from a card-targeted Hermes cron job."""
    name = str(job.get("name") or "")
    if " — " in name:
        tail = name.rsplit(" — ", 1)[-1].strip()
        if tail:
            return tail
    prompt = str(job.get("prompt") or job.get("message") or "")
    match = re.search(r"Tarefa alvo: `([^`]+)`", prompt, re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip() or None
    return None


def _is_team_auto_cron_job(job: dict[str, Any]) -> bool:
    """True for recurring team-wide kanban auto-pick crons (not card-specific)."""
    if _is_kanban_task_cron_job(job):
        return False
    if bool(job.get("no_agent")) or str(job.get("script") or "").strip():
        return False
    name = str(job.get("name") or "")
    if name.endswith(_TEAM_AUTO_CRON_SUFFIX):
        return True
    prompt = str(job.get("prompt") or job.get("message") or "").strip()
    return bool(prompt)


def _schedule_meta_from_jobs(
    jobs: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """Return (schedule_target, schedule_task_id) inferred from profile cron jobs."""
    ordered = sorted(jobs, key=lambda row: 0 if row.get("active") else 1)
    for job in ordered:
        if _is_kanban_task_cron_job(job):
            return "card", _task_id_from_kanban_cron_job(job)
    for job in ordered:
        if _is_team_auto_cron_job(job):
            return "team", None
    return None, None


def _remove_cron_job_ids(jobs_path: Path, job_ids: list[str]) -> dict[str, Any]:
    ids = {str(job_id or "").strip() for job_id in job_ids if str(job_id or "").strip()}
    if not ids:
        return {"ok": True, "removed_count": 0}
    return remove_cron_jobs_from_file(jobs_path, job_ids=ids)


def _invalidate_hermes_cron_cache(monitor: "GoisMonitor") -> None:
    inval = getattr(monitor, "_invalidate_hermes_cron_cache", None)
    if callable(inval):
        inval()


def _profile_cron_jobs_split(
    jobs: list[dict[str, Any]],
    profile_key: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_jobs = [
        job
        for job in jobs
        if isinstance(job, dict) and _norm_slug(str(job.get("profile") or "")) == profile_key
    ]
    team_jobs = [job for job in all_jobs if _is_team_auto_cron_job(job)]
    card_jobs = [job for job in all_jobs if _is_kanban_task_cron_job(job)]
    return all_jobs, team_jobs, card_jobs


def _resolve_schedule_card_pick(
    profile_slug: str,
    team_ids: set[str],
    boards: list[dict[str, Any]],
    alias_map: dict[str, str],
    *,
    task_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve a kanban card for card-targeted scheduling."""
    explicit = str(task_id or "").strip()
    if not explicit:
        return _pick_team_card_for_schedule(profile_slug, team_ids, boards, alias_map)

    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        if team_id not in team_ids:
            continue
        columns = {
            _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
            for c in (board.get("columns") or [])
            if isinstance(c, dict)
        }
        kanban_file = board.get("kanban_file")
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if str(task.get("id") or "").strip() != explicit:
                continue
            if _is_scheduling_lane_card(task, board):
                return None
            col = _norm_slug(task.get("column"))
            if col in _DONE_KANBAN_COLS:
                return None
            card = _task_to_card(task, board, columns)
            if kanban_file is not None:
                card["kanban_file"] = kanban_file
            return {
                "task_id": explicit,
                "team_id": team_id,
                "workdir": str(board.get("workdir") or "").strip(),
                "kanban_file": kanban_file,
                "title": str(card.get("title") or explicit).strip(),
                "priority": card.get("priority"),
                "skills": list(card.get("skills") or []),
            }
    return None


def _build_team_auto_schedule_prompt(
    profile_slug: str,
    *,
    workdir: str,
    team_name: str = "",
    display_name: str = "",
    leader_context: Optional[dict[str, Any]] = None,
) -> str:
    """Prompt for recurring cron that picks the best open kanban card each run."""
    ctx = leader_context or {}
    if ctx.get("is_leader"):
        return _build_leader_swarm_summary_prompt(
            profile_slug,
            workdir=workdir,
            team_name=team_name,
            display_name=display_name,
            leader_context=ctx,
        )
    label = display_name.strip() or profile_slug
    team_line = f"- Time: `{team_name}`\n" if team_name else ""
    return (
        "Você deve executar trabalho do kanban do time vinculado.\n"
        f"- Workdir do time: `{workdir}`\n"
        f"{team_line}"
        f"- Perfil Hermes: `{profile_slug}` ({label})\n\n"
        "## Escolha da tarefa (automática)\n"
        "1. Abra o kanban no workdir acima.\n"
        "2. Escolha UMA tarefa aberta (não concluída), nesta ordem:\n"
        f"   a) Cards em `doing`/`review` atribuídos a `{label}` ou `{profile_slug}`\n"
        f"   b) Cards em `todo`/`backlog` atribuídos a você\n"
        "   c) Cards sem responsável em `todo`/`backlog` — reivindique e execute\n"
        "3. Se não houver cards abertos, reporte backlog vazio e encerre.\n\n"
        "## Execução\n"
        "- Mova para `doing`, implemente, valide, mova para `done` com notes e completed_at.\n"
        "- Não pegue mais de uma tarefa nesta execução.\n"
        "- Resumo final: tarefa escolhida, ficheiros alterados, testes, bloqueios.\n"
    )


def _build_leader_swarm_summary_prompt(
    profile_slug: str,
    *,
    workdir: str,
    team_name: str = "",
    display_name: str = "",
    leader_context: Optional[dict[str, Any]] = None,
) -> str:
    """Cron prompt for swarm leaders: consolidate all members' work."""
    ctx = leader_context or {}
    label = display_name.strip() or profile_slug
    team_line = f"- Time: `{team_name}`\n" if team_name else ""
    swarm_line = ""
    swarm_name = str(ctx.get("swarm_name") or "").strip()
    if swarm_name:
        swarm_line = f"- Swarm: `{swarm_name}`\n"
    members = list(ctx.get("member_labels") or ctx.get("member_slugs") or [])
    members_block = ", ".join(members) if members else "(demais perfis do time no kanban)"
    artifacts = str(ctx.get("artifacts_dir") or "").strip()
    artifacts_block = (
        f"- Diretório de artefatos: `{artifacts}`\n"
        if artifacts
        else ""
    )
    return (
        f"Você é o **líder/orquestrador** do swarm ({label} / `{profile_slug}`).\n"
        f"- Workdir do time: `{workdir}`\n"
        f"{team_line}"
        f"{swarm_line}"
        f"{artifacts_block}\n"
        "## Missão desta execução (líder)\n"
        "Produza o **resumo consolidado** do que TODOS os agentes do swarm fizeram.\n"
        "Não substitua o trabalho dos especialistas — coordene, consolide e delegue.\n\n"
        "### 1. Coletar entregas de cada agente\n"
        f"Agentes do swarm (além de você): {members_block}.\n"
        "1. Abra o kanban no workdir acima.\n"
        "2. Para **cada agente** listado (e qualquer outro assignee do time), revise:\n"
        "   - Cards em `done` (notes, completed_at, commits mencionados)\n"
        "   - Cards em `doing`/`review` (progresso parcial e bloqueios)\n"
        "   - Cards em `todo`/`backlog` ainda não iniciados\n"
        "3. Se existirem saídas de cron ou logs no projeto, use-as para enriquecer o resumo.\n\n"
        "### 2. Resumo consolidado (obrigatório)\n"
        "Escreva markdown com:\n"
        "- **Por agente**: tarefas concluídas, em progresso, bloqueios, ficheiros/commits\n"
        "- **Visão geral**: progresso do swarm, riscos, dependências\n"
        "- **Próximos passos**: delegações sugeridas (assignees no kanban)\n\n"
        "### 3. Publicar o resumo\n"
        + (
            f"1. Grave em `{artifacts}/swarm-resumos/resumo-<YYYY-MM-DD>.md` (crie a pasta se faltar).\n"
            if artifacts
            else "1. Grave o resumo num ficheiro markdown no workdir do time (`swarm-resumos/`).\n"
        )
        + "2. Crie ou atualize o card kanban `SWARM-RESUMO` (coluna `review` ou `done`) "
        "com o resumo completo em `notes` e data ISO em `completed_at` se aplicável.\n"
        "3. Se especialistas estiverem ociosos com backlog aberto, atribua cards a eles "
        "(não execute o trabalho técnico deles).\n\n"
        "## Entrega obrigatória\n"
        "- O resumo consolidado em markdown (não pode ser vazio).\n"
        "- Lista de agentes cobertos e cards analisados.\n"
        "- Caminho do ficheiro gravado e id do card `SWARM-RESUMO`.\n"
    )


def resolve_swarm_leader_context(
    monitor: "GoisMonitor",
    profile_slug: str,
    user: Any = None,
) -> dict[str, Any]:
    """Return leader/member context for cron prompts and swarm coordination."""
    slug = _norm_slug(profile_slug)
    out: dict[str, Any] = {
        "is_leader": False,
        "leader_slug": "",
        "member_slugs": [],
        "member_labels": [],
        "swarm_name": "",
        "team_id": "",
        "team_name": "",
        "artifacts_dir": "",
    }
    if not slug:
        return out

    teams = _teams_for_profile_slug(monitor, profile_slug, user)
    if not teams:
        return out

    team = teams[0]
    out["team_id"] = str(team.id or "")
    out["team_name"] = str(team.name or "")

    profiles = [str(s).strip() for s in (team.profile_slugs or []) if str(s).strip()]
    swarm_name = str(team.swarm_name or "").strip() or str(team.id or "")
    out["swarm_name"] = swarm_name

    entry_agent = ""
    agent_rows = [{"slug": _norm_slug(p), "role": ""} for p in profiles]
    try:
        from .openai_swarm import load_swarms_full

        for row in load_swarms_full():
            if str(row.get("name") or "") == swarm_name:
                entry_agent = str(row.get("entry_agent") or "").strip()
                for agent in row.get("agents") or []:
                    if not isinstance(agent, dict):
                        continue
                    name = _norm_slug(str(agent.get("name") or ""))
                    for spec in agent_rows:
                        if spec["slug"] == name:
                            spec["role"] = str(agent.get("role") or "")
                break
    except Exception:
        pass

    if not entry_agent:
        try:
            from .swarm_graph import _load_swarm_state

            state = _load_swarm_state(swarm_name)
            if isinstance(state, dict):
                entry_agent = str(state.get("entry_agent") or "").strip()
                for agent in state.get("agents") or []:
                    if not isinstance(agent, dict):
                        continue
                    name = _norm_slug(str(agent.get("name") or ""))
                    for spec in agent_rows:
                        if spec["slug"] == name:
                            spec["role"] = str(agent.get("role") or "")
        except Exception:
            pass

    leader = team_leader_slug(profiles, agent_rows, entry_agent=entry_agent)
    if not leader and entry_agent:
        leader = _norm_slug(entry_agent)
    out["leader_slug"] = leader

    if leader and _norm_slug(leader) == slug:
        out["is_leader"] = True
        members = [_norm_slug(p) for p in profiles if _norm_slug(p) and _norm_slug(p) != slug]
        out["member_slugs"] = members
        out["member_labels"] = [
            f"{read_profile_display_name(m) or m} (`{m}`)" for m in members
        ]
        try:
            out["artifacts_dir"] = str(monitor.accounts.team_artifacts_dir(team))
        except Exception:
            pass

    return out


def _create_team_auto_cron(
    monitor: "GoisMonitor",
    profile_slug: str,
    schedule: str,
    *,
    create_cfg: Any,
    user: Any = None,
) -> dict[str, Any]:
    """Create a recurring cron that auto-picks the best team kanban card each run."""
    from .hermes_cron import create_hermes_cron_job

    teams = _teams_for_profile_slug(monitor, profile_slug, user)
    if not teams:
        return {
            "ok": False,
            "error": _SCHEDULE_TEAM_REQUIRED_MSG,
            "requires_team": True,
        }
    team = teams[0]
    team_workdir = str(monitor.accounts.team_workdir(team))
    display_name = read_profile_display_name(profile_slug) or profile_slug
    skills = read_profile_skills(profile_slug) or None

    staggered = schedule
    stagger = getattr(monitor, "_stagger_schedule_for_new_job", None)
    if callable(stagger):
        try:
            staggered = stagger(schedule) or schedule
        except Exception:
            pass

    prompt = _build_team_auto_schedule_prompt(
        profile_slug,
        workdir=team_workdir,
        team_name=str(team.name or "").strip(),
        display_name=display_name,
        leader_context=resolve_swarm_leader_context(monitor, profile_slug, user),
    )
    jobs_path = _resolve_hermes_cron_jobs_path_for_monitor(monitor)
    result = create_hermes_cron_job(
        staggered,
        prompt,
        name=f"{display_name}{_TEAM_AUTO_CRON_SUFFIX}",
        profile=profile_slug,
        skills=skills,
        workdir=team_workdir,
        accept_hooks=bool(getattr(create_cfg, "cron_accept_hooks", False)),
        timeout_seconds=float(getattr(create_cfg, "cron_timeout_seconds", 120.0)),
        jobs_path=jobs_path,
    )
    if result.get("ok"):
        result["action"] = "created"
        result["schedule_target"] = "team"
        result["team_id"] = str(team.id or "")
        inval = getattr(monitor, "_invalidate_hermes_cron_cache", None)
        if callable(inval):
            inval()
    else:
        result.setdefault(
            "error",
            result.get("reason") or result.get("summary") or "falha ao criar cron",
        )
    return result


def team_leader_slug(
    profile_slugs: list[str],
    agent_rows: list[dict[str, Any]],
    *,
    entry_agent: str = "",
) -> str:
    """Resolve the team leader / orchestrator profile slug."""
    entry = _norm_slug(entry_agent)
    known = {_norm_slug(str(p)) for p in profile_slugs if str(p).strip()}
    if entry and entry in known:
        return entry
    agents = [
        {"name": str(row.get("slug") or ""), "role": str(row.get("role") or "")}
        for row in agent_rows
        if isinstance(row, dict) and str(row.get("slug") or "").strip()
    ]
    guessed = _guess_orchestrator_slug(list(known), agents)
    return _norm_slug(guessed)


_DEVELOPMENT_SWARM_PRESET_PREFIX = "swarm-dev-"
_CONTENT_SWARM_CATEGORIES = frozenset(
    {"infoprodutos", "youtube", "conteúdo", "conteudo", "pesquisa"}
)
_CONTENT_ROLE_PRESET_IDS = frozenset(
    {
        "instructional-designer",
        "video-producer",
        "course-publisher",
        "sales-copywriter",
        "technical-writer",
    }
)
_DEVELOPMENT_ROLE_PRESET_IDS = frozenset(
    str(p.get("id") or "").strip()
    for p in _PRESET_BY_ID.values()
    if str(p.get("category") or "").strip().lower() == "desenvolvimento"
)
_DEVELOPMENT_SLUG_HINTS = (
    "backend",
    "frontend",
    "fullstack",
    "mobile-dev",
    "coder",
    "developer",
    "dev-",
)
_DEVELOPMENT_TEST_POLICY_MARKER = "## Política obrigatória — testes"
_DEVELOPMENT_TEST_POLICY = (
    "\n\n## Política obrigatória — testes\n"
    "Este é um swarm de desenvolvimento: toda entrega de código DEVE incluir testes "
    "automatizados.\n"
    "- Escreva ou atualize testes unitários/integração cobrindo o comportamento alterado.\n"
    "- Execute os testes do projeto e reporte o resultado (passou/falhou).\n"
    "- Não marque o card como concluído sem testes verdes ou justificativa documentada.\n"
)


def _profile_slug_indicates_development(slug: str) -> bool:
    key = _norm_slug(slug)
    if not key:
        return False
    preset = _PRESET_BY_ID.get(key)
    if preset and str(preset.get("category") or "").strip().lower() == "desenvolvimento":
        return True
    matched = match_role_preset(key)
    if matched and str(matched.get("category") or "").strip().lower() == "desenvolvimento":
        return True
    return any(hint in key for hint in _DEVELOPMENT_SLUG_HINTS)


def _profile_slug_indicates_content(slug: str) -> bool:
    key = _norm_slug(slug)
    if not key:
        return False
    preset = _PRESET_BY_ID.get(key)
    pid = str(preset.get("id") or key).strip() if preset else key
    if pid in _CONTENT_ROLE_PRESET_IDS:
        return True
    if preset:
        cat = str(preset.get("category") or "").strip().lower()
        if cat in _CONTENT_SWARM_CATEGORIES:
            return True
    matched = match_role_preset(key)
    if matched:
        cat = str(matched.get("category") or "").strip().lower()
        if cat in _CONTENT_SWARM_CATEGORIES:
            return True
        if str(matched.get("id") or "").strip() in _CONTENT_ROLE_PRESET_IDS:
            return True
    return False


def _swarm_state_indicates_development(swarm_state: Optional[dict[str, Any]]) -> bool:
    if not isinstance(swarm_state, dict):
        return False
    from .swarm_presets import is_course_swarm_state

    if is_course_swarm_state(swarm_state):
        return False
    cat = str(swarm_state.get("category") or "").strip().lower()
    if cat == "desenvolvimento":
        return True
    preset_id = str(swarm_state.get("preset_id") or "").strip()
    if preset_id.startswith(_DEVELOPMENT_SWARM_PRESET_PREFIX):
        return True
    if preset_id:
        try:
            from .swarm_presets import get_swarm_preset

            preset = get_swarm_preset(preset_id)
        except ValueError:
            preset = None
        if isinstance(preset, dict):
            preset_cat = str(preset.get("category") or "").strip().lower()
            if preset_cat == "desenvolvimento":
                return True
            if preset_cat in _CONTENT_SWARM_CATEGORIES:
                return False
    return False


def is_development_team_swarm(
    monitor: GoisMonitor,
    team: Any,
    swarm_name: str,
    *,
    swarm_state: Optional[dict[str, Any]] = None,
    profiles: Optional[list[str]] = None,
) -> bool:
    """True when the linked team swarm is a software-development workflow."""
    state = swarm_state
    if state is None:
        try:
            from .swarm_graph import _load_swarm_state

            state = _load_swarm_state(str(swarm_name or "").strip())
        except Exception:  # noqa: BLE001 - best-effort probe
            state = None
    if _swarm_state_indicates_development(state):
        return True

    slugs = list(profiles or [])
    if not slugs and hasattr(monitor, "_team_swarm_profiles"):
        try:
            slugs = monitor._team_swarm_profiles(team, swarm_name)
        except Exception:  # noqa: BLE001 - fallback to team roles
            slugs = []
    if not slugs:
        slugs = list(getattr(team, "profile_slugs", None) or [])

    dev_roles = sum(1 for slug in slugs if _profile_slug_indicates_development(slug))
    content_roles = sum(1 for slug in slugs if _profile_slug_indicates_content(slug))
    if dev_roles and content_roles == 0:
        return True
    return dev_roles > content_roles


def augment_development_swarm_objective(
    objective: str,
    *,
    monitor: GoisMonitor,
    team: Any,
    swarm_name: str,
    swarm_state: Optional[dict[str, Any]] = None,
    profiles: Optional[list[str]] = None,
) -> str:
    """Append mandatory test policy for development team swarms."""
    text = str(objective or "").strip()
    if not text:
        return text
    if _DEVELOPMENT_TEST_POLICY_MARKER in text:
        return text
    if not is_development_team_swarm(
        monitor,
        team,
        swarm_name,
        swarm_state=swarm_state,
        profiles=profiles,
    ):
        return text
    return text + _DEVELOPMENT_TEST_POLICY


def ensure_swarm_team_kanban_ready(
    monitor: GoisMonitor,
    team_id: str,
    user: Any = None,
) -> dict[str, Any]:
    """Repair team kanban storage and ensure team dirs exist before swarm ops."""
    actor = monitor._accounts_actor(user)
    if monitor.cfg.auth.enabled and actor is None:
        return {"ok": False, "error": "not authenticated"}
    tid = str(team_id or "").strip()
    if not tid:
        return {"ok": False, "error": "team_id is required"}
    try:
        team = monitor.accounts.get_team(tid, actor.id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    team_dir = monitor.accounts.team_dir(team.id)
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "workspace").mkdir(parents=True, exist_ok=True)

    kanban_path = monitor.accounts.team_kanban_path(team.id)
    monitor.accounts._ensure_team_kanban(team.id)

    repair: dict[str, Any] = {"ok": True, "repaired": False, "tasks": 0}

    try:
        board = monitor.accounts.read_kanban(team.id, actor.id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    tasks = board.get("tasks") if isinstance(board.get("tasks"), list) else []
    return {
        "ok": True,
        "team_id": team.id,
        "team_name": team.name,
        "tasks": len(tasks),
        "kanban_repaired": bool(repair.get("repaired")),
        "workdir": str(team_dir),
    }


def _swarm_run_task_priority(task: dict[str, Any]) -> tuple[Any, ...]:
    """Lower tuple sorts first — todo/backlog before doing; unassigned before assigned."""
    col = _norm_slug(task.get("column"))
    col_rank = {
        "todo": 0,
        "backlog": 1,
        "doing": 2,
        "in_progress": 3,
        "em progresso": 3,
        "review": 4,
    }.get(col, 5)
    assignees = task.get("assignees") or []
    unassigned_rank = 0 if not assignees else 1
    return (col_rank, unassigned_rank, str(task.get("id") or ""))


def preview_swarm_run_cards(
    monitor: GoisMonitor,
    team_id: str,
    swarm_name: str,
    user: Any,
    *,
    max_cards: int = 1,
) -> dict[str, Any]:
    """Read-only preview of which kanban card(s) the next swarm run would pick."""
    ready = ensure_swarm_team_kanban_ready(monitor, team_id, user)
    if not ready.get("ok"):
        return ready
    actor = monitor._accounts_actor(user)
    tid = str(ready.get("team_id") or team_id or "").strip()
    try:
        team = monitor.accounts.get_team(tid, actor.id)
        board = monitor.accounts.read_kanban(team.id, actor.id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    all_tasks = [t for t in (board.get("tasks") or []) if isinstance(t, dict)]
    selected = _select_swarm_run_tasks(
        all_tasks, board if isinstance(board, dict) else {}, max_cards=max_cards
    )
    next_cards: list[dict[str, str]] = []
    for task in selected:
        tid_task = str(task.get("id") or "").strip()
        title = str(task.get("title") or tid_task).strip()
        col = str(task.get("column") or "todo").strip().lower()
        next_cards.append(
            {
                "id": tid_task,
                "title": title,
                "column": col,
                "label": f"{tid_task}: {title}" if tid_task else title,
            }
        )
    open_count = len(
        _select_swarm_run_tasks(
            all_tasks, board if isinstance(board, dict) else {}, max_cards=0
        )
    )
    out: dict[str, Any] = {
        "ok": True,
        "team_id": team.id,
        "team_name": team.name,
        "swarm_name": str(swarm_name or "").strip(),
        "next_card": next_cards[0] if next_cards else None,
        "next_cards": next_cards,
        "pending_count": open_count,
        "max_cards": max_cards,
    }
    if not next_cards:
        out["hint"] = "Nenhum card aberto no kanban — crie cards em todo/backlog."
    return out


def _select_swarm_run_tasks(
    tasks: list[dict[str, Any]],
    board: dict[str, Any],
    *,
    max_cards: int,
) -> list[dict[str, Any]]:
    """Pick the next open kanban cards for a swarm run (one card per execution by default)."""
    done_cols = {"done", "concluido", "concluído", "closed", "feito"}
    eligible: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if _is_scheduling_lane_card(task, board):
            continue
        col = _norm_slug(task.get("column"))
        if col in done_cols:
            continue
        eligible.append(task)
    eligible.sort(key=_swarm_run_task_priority)
    if max_cards <= 0:
        return eligible
    return eligible[:max_cards]


def prepare_swarm_test_team_context(
    monitor: GoisMonitor,
    team_id: str,
    swarm_name: str,
    user: Any,
    *,
    assign_unassigned: bool = True,
    max_cards: int = 1,
) -> dict[str, Any]:
    """Load team kanban, assign open cards to swarm agents, build run objective.

    Used when ``run_all`` executes a swarm linked to an account team so agents
    receive real kanban cards. By default picks **one** card per run; pass
    ``max_cards=0`` to include every open card.
    """
    ready = ensure_swarm_team_kanban_ready(monitor, team_id, user)
    if not ready.get("ok"):
        return ready

    from .hermes_kanban import normalize_assignees, propose_task_assignments

    actor = monitor._accounts_actor(user)
    if monitor.cfg.auth.enabled and actor is None:
        return {"ok": False, "error": "not authenticated"}

    tid = str(team_id or "").strip()
    if not tid:
        return {"ok": False, "error": "team_id is required"}

    try:
        team = monitor.accounts.get_team(tid, actor.id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        board = monitor.accounts.read_kanban(team.id, actor.id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(board, dict):
        return {"ok": False, "error": "kanban do time indisponível"}

    workdir = str(board.get("workdir") or "").strip()
    if not workdir:
        workdir = str(monitor.accounts.team_workdir(team))
    kanban_file = board.get("kanban_file")

    swarm_profiles = monitor._team_swarm_profiles(team, swarm_name)
    if not swarm_profiles:
        swarm_profiles = [
            str(s).strip()
            for s in (team.profile_slugs or [])
            if str(s).strip()
        ]
    profiles_by_slug = {
        _norm_slug(slug): {"slug": slug, "display_name": slug}
        for slug in swarm_profiles
        if slug
    }
    alias_map = _build_assignee_alias_map(profiles_by_slug)
    agent_rows = [
        {"slug": slug, "role": ""}
        for slug in swarm_profiles
        if slug
    ]

    all_tasks = [
        t for t in (board.get("tasks") or []) if isinstance(t, dict)
    ]
    selected_tasks = _select_swarm_run_tasks(
        all_tasks, board if isinstance(board, dict) else {}, max_cards=max_cards
    )
    selected_ids = {
        str(t.get("id") or "").strip()
        for t in selected_tasks
        if str(t.get("id") or "").strip()
    }

    assignments_applied: list[dict[str, str]] = []
    if assign_unassigned and agent_rows and selected_tasks:
        leader = team_leader_slug(
            list(swarm_profiles),
            agent_rows,
            entry_agent=str(
                (monitor._ephemeral_team_swarm_state(team, swarm_name) or {}).get(
                    "entry_agent"
                )
                or ""
            ),
        )
        specialists = [
            row
            for row in agent_rows
            if _norm_slug(str(row.get("slug") or "")) != leader
        ]
        targets = specialists or agent_rows
        proposals = propose_task_assignments(
            selected_tasks,
            targets,
            columns={"todo", "backlog"},
            only_unassigned=True,
        )
        base: dict[str, Any] = {
            "workdir": workdir,
            "team_id": team.id,
        }
        if kanban_file is not None:
            base["kanban_file"] = kanban_file
        system_user = monitor._system_actor(base)
        for task_id, slug in proposals.items():
            result = monitor.handle_hermes_kanban_action(
                {
                    **base,
                    "action": "assign_task",
                    "task_id": task_id,
                    "assignees": [slug],
                },
                system_user,
            )
            if not result.get("ok"):
                continue
            assignments_applied.append({"task_id": task_id, "assignee": slug})
        if assignments_applied:
            try:
                board = monitor.accounts.read_kanban(team.id, actor.id)
            except ValueError:
                pass

    board = board if isinstance(board, dict) else {}
    board.setdefault("team_id", team.id)
    board.setdefault("workdir", workdir)
    if kanban_file is not None:
        board.setdefault("kanban_file", kanban_file)

    done_cols = {"done", "concluido", "concluído", "closed", "feito"}
    columns = {
        _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
        for c in (board.get("columns") or [])
        if isinstance(c, dict)
    }

    agent_cards: dict[str, list[dict[str, Any]]] = {}
    objective_lines: list[str] = []
    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tid_task = str(task.get("id") or "").strip()
        if max_cards > 0 and selected_ids and tid_task not in selected_ids:
            continue
        if _is_scheduling_lane_card(task, board):
            continue
        col = _norm_slug(task.get("column"))
        if col in done_cols:
            continue
        assignees = _task_assignees(task, alias_map)
        swarm_assignees = [
            slug for slug in assignees if slug in profiles_by_slug or slug in alias_map.values()
        ]
        if not swarm_assignees:
            continue
        card = _task_to_card(task, board, columns)
        tid_task = str(card.get("id") or "").strip()
        title = str(card.get("title") or tid_task).strip()
        for slug in swarm_assignees:
            canonical = _resolve_assignee_slug(slug, alias_map) or slug
            agent_cards.setdefault(canonical, []).append(card)
        owners = ", ".join(swarm_assignees)
        prefix = f"{tid_task}: " if tid_task else ""
        objective_lines.append(f"- {prefix}{title} (→ {owners})")

    for slug in agent_cards:
        agent_cards[slug].sort(key=_card_sort_key)

    cards_moved: list[str] = []
    if agent_cards:
        doing_col = "doing"
        for col in board.get("columns") or []:
            if isinstance(col, dict) and _norm_slug(col.get("id")) in {
                "doing",
                "in_progress",
                "em progresso",
            }:
                doing_col = str(col.get("id") or "doing")
                break
        team_kanban_path = monitor.accounts.team_kanban_path(team.id)
        move_base: dict[str, Any] = {
            "workdir": str(monitor.accounts.team_dir(team.id)),
            "team_id": team.id,
            "kanban_file": (
                str(kanban_file).strip()
                if kanban_file
                else team_kanban_path.name
            ),
        }
        system_user = monitor._system_actor(move_base)
        seen_move: set[str] = set()
        for cards in agent_cards.values():
            for card in cards:
                if not isinstance(card, dict):
                    continue
                tid = str(card.get("id") or "").strip()
                if not tid or tid in seen_move:
                    continue
                if _norm_slug(card.get("column")) in {"doing", "review"}:
                    seen_move.add(tid)
                    continue
                result = monitor.handle_hermes_kanban_action(
                    {
                        **move_base,
                        "action": "move_task",
                        "task_id": tid,
                        "column": doing_col,
                    },
                    system_user,
                )
                if result.get("ok"):
                    seen_move.add(tid)
                    cards_moved.append(tid)
                    card["column"] = doing_col
        if cards_moved:
            try:
                board = monitor.accounts.read_kanban(team.id, actor.id)
            except ValueError:
                pass

    if objective_lines:
        if max_cards == 1 and len(objective_lines) == 1:
            header = (
                f"Executar swarm `{swarm_name}` — próximo card do time "
                f"{team.name} no kanban"
            )
        else:
            header = (
                f"Executar swarm `{swarm_name}` — cards do time "
                f"{team.name} no kanban"
            )
        objective = header + ":\n" + "\n".join(objective_lines[:20])
    else:
        objective = monitor._team_swarm_objective(team, board)

    objective = augment_development_swarm_objective(
        objective,
        monitor=monitor,
        team=team,
        swarm_name=swarm_name,
        profiles=swarm_profiles,
    )

    selected_card_id = ""
    if selected_ids:
        selected_card_id = sorted(selected_ids)[0]

    return {
        "ok": True,
        "team_id": team.id,
        "team_name": team.name,
        "swarm_name": swarm_name,
        "workdir": workdir,
        "objective": objective,
        "agent_cards": agent_cards,
        "assignments_applied": assignments_applied,
        "cards_moved": cards_moved,
        "cards_count": sum(len(v) for v in agent_cards.values()),
        "selected_card_id": selected_card_id,
        "selected_card_ids": sorted(selected_ids),
        "max_cards": max_cards,
    }


def resolve_delegate_assignee(
    task: dict[str, Any],
    requested_assignee: str,
    agent_rows: list[dict[str, Any]],
    *,
    leader_slug: str = "",
    load_counts: Optional[dict[str, int]] = None,
) -> tuple[str, bool]:
    """Leader delegates to the most competent specialist; others keep the request."""
    from .hermes_kanban import suggest_assignee_for_task

    req = _norm_slug(requested_assignee)
    leader = _norm_slug(leader_slug)
    if not req or not leader or req != leader:
        return str(requested_assignee or "").strip(), False
    specialists = [
        row
        for row in agent_rows
        if isinstance(row, dict)
        and _norm_slug(str(row.get("slug") or ""))
        and _norm_slug(str(row.get("slug") or "")) != leader
    ]
    if not specialists:
        return str(requested_assignee or "").strip(), False
    delegated = suggest_assignee_for_task(
        task,
        specialists,
        load_counts=load_counts or {},
    )
    if not delegated:
        return str(requested_assignee or "").strip(), False
    return delegated, True


def _load_teams_data(
    monitor: GoisMonitor,
    user: Any,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Load teams once, return (profile_team_map, teams_index).

    Consolidates two former calls to ``list_all_teams()`` into one.
    """
    team_map: dict[str, dict[str, str]] = {}
    teams_idx: dict[str, dict[str, str]] = {}
    try:
        actor = monitor._accounts_actor(user)
        if actor is None:
            return team_map, teams_idx
        for team in monitor.accounts.list_all_teams():
            # teams_index part
            tid = str(team.id or "").strip()
            if tid:
                teams_idx[tid] = {
                    "team_name": str(team.name or tid),
                    "description": str(team.description or ""),
                    "swarm_name": str(team.swarm_name or "").strip(),
                }
            # profile_team_map part
            for slug in team.profile_slugs or []:
                norm = _norm_slug(slug)
                if norm:
                    team_map[norm] = {
                        "team_id": str(team.id or ""),
                        "team_name": str(team.name or team.id or ""),
                        "description": str(team.description or ""),
                    }
    except Exception as exc:
        log.debug("swarm robots: teams data unavailable: %s", exc)
    return team_map, teams_idx


def _profile_team_map(
    monitor: GoisMonitor,
    user: Any,
) -> dict[str, dict[str, str]]:
    """Map profile slug -> team metadata for /swarm grouping."""
    team_map, _ = _load_teams_data(monitor, user)
    return team_map


def _teams_index(
    monitor: GoisMonitor,
    user: Any,
) -> dict[str, dict[str, str]]:
    """Map team id -> team metadata for swarm ↔ time linking."""
    _, teams_idx = _load_teams_data(monitor, user)
    return teams_idx


def _kanban_assignee_slugs(
    boards: list[dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
) -> set[str]:
    slugs: set[str] = set()
    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            slugs.update(_task_assignees(task, alias_map))
    return slugs


def _kanban_agent_slugs(agents: list[dict[str, str]]) -> set[str]:
    slugs: set[str] = set()
    for row in agents:
        if not isinstance(row, dict):
            continue
        for variant in _profile_slug_variants(
            str(row.get("slug") or row.get("name") or "")
        ):
            slugs.add(variant)
    return slugs


def _is_orchestratorish(text: str) -> bool:
    low = _norm_slug(_strip_accents(str(text or "")))
    return any(h in low for h in _ORCH_HINT_RE)


def _guess_orchestrator_slug(
    profiles: list[str],
    agents: list[dict[str, Any]],
    profiles_by_slug: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """Pick the most likely orchestrator slug among swarm/team members."""
    slugs = [_norm_slug(str(p)) for p in profiles if _norm_slug(str(p))]
    if len(slugs) <= 1:
        return slugs[0] if slugs else ""

    agents_by_slug = {
        _norm_slug(str(a.get("name") or "")): a
        for a in agents
        if isinstance(a, dict) and _norm_slug(str(a.get("name") or ""))
    }

    def _score(slug: str) -> int:
        meta = (profiles_by_slug or {}).get(slug, {})
        agent = agents_by_slug.get(slug) or {}
        text = " ".join(
            [
                slug,
                str(meta.get("display_name") or ""),
                str(meta.get("description") or ""),
                str(agent.get("role") or ""),
            ]
        )
        score = 5 if _is_orchestratorish(text) else 0
        if slug.endswith("-dev") or slug.endswith("-lead"):
            score += 2
        if any(slug.endswith(s) for s in ("-coder", "-worker", "-tester", "-reviewer")):
            score -= 1
        handoffs = len(agent.get("handoff_to") or [])
        if handoffs >= 2:
            score += 3
        elif handoffs >= 1:
            score += 1
        return score

    ranked = sorted(slugs, key=lambda s: (-_score(s), s))
    return ranked[0] if _score(ranked[0]) > 0 else ""


def _resolve_entry_agent_slug(
    entry: str,
    *,
    profiles: list[str],
    agents: list[dict[str, Any]],
    profiles_by_slug: Optional[dict[str, dict[str, Any]]] = None,
    alias_map: Optional[dict[str, str]] = None,
) -> str:
    """Resolve entry_agent from swarm state to a canonical profile slug."""
    profile_slugs = {_norm_slug(str(p)) for p in profiles if _norm_slug(str(p))}
    agent_slugs = {
        _norm_slug(str(a.get("name") or ""))
        for a in agents
        if isinstance(a, dict) and _norm_slug(str(a.get("name") or ""))
    }
    known = profile_slugs | agent_slugs

    raw = str(entry or "").strip()
    if raw:
        candidates: set[str] = set()
        candidates.add(_norm_slug(raw))
        candidates.update(_profile_slug_variants(raw))
        if alias_map:
            for variant in list(candidates):
                resolved = alias_map.get(variant)
                if resolved:
                    candidates.add(_norm_slug(resolved))
        for candidate in candidates:
            if candidate in known:
                return candidate
        entry_norm = _norm_slug(raw)
        for slug in sorted(known):
            if entry_norm and (entry_norm in slug or slug in entry_norm):
                return slug

    return _guess_orchestrator_slug(profiles, agents, profiles_by_slug)


def _apply_orchestrator_flags(
    robots: list[dict[str, Any]],
    swarms: list[dict[str, Any]],
    profiles_by_slug: dict[str, dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
) -> None:
    """Mark orchestrator robots and normalize entry_agent on swarm summaries."""
    by_swarm: dict[str, list[dict[str, Any]]] = {}
    for robot in robots:
        swarm_name = str(robot.get("swarm_name") or "")
        if swarm_name:
            by_swarm.setdefault(swarm_name, []).append(robot)

    for swarm in swarms:
        name = str(swarm.get("name") or "")
        group = by_swarm.get(name, [])
        profiles = list(swarm.get("hermes_profiles") or [])
        agents = list(swarm.get("agents") or [])
        resolved = _resolve_entry_agent_slug(
            str(swarm.get("entry_agent") or ""),
            profiles=profiles,
            agents=agents,
            profiles_by_slug=profiles_by_slug,
            alias_map=alias_map,
        )
        if resolved:
            swarm["entry_agent"] = resolved
        elif len(group) >= 2:
            guessed = _guess_orchestrator_slug(profiles, agents, profiles_by_slug)
            if guessed:
                swarm["entry_agent"] = guessed
                resolved = guessed

        if not resolved or len(group) < 2:
            continue

        resolved_norm = _norm_slug(resolved)
        member_slugs = sorted(
            {
                _norm_slug(str(robot.get("slug") or ""))
                for robot in group
                if _norm_slug(str(robot.get("slug") or ""))
            }
        )
        topology = str(swarm.get("topology") or "handoff")
        inferred_children = infer_entry_handoff_targets(
            resolved_norm,
            member_slugs,
            topology,
        )
        for robot in group:
            slug = _norm_slug(str(robot.get("slug") or ""))
            is_orch = slug == resolved_norm
            robot["is_entry"] = is_orch
            robot["is_orchestrator"] = is_orch
            if is_orch and not (robot.get("handoff_to") or []):
                robot["handoff_to"] = list(inferred_children)


def _team_swarm_entries(
    team_map: dict[str, dict[str, str]],
    candidate_slugs: set[str],
    profiles_by_slug: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Build pseudo-swarm rows from team-linked profiles."""
    by_team: dict[str, dict[str, Any]] = {}
    for slug in candidate_slugs:
        tm = team_map.get(slug)
        if not tm:
            continue
        tid = tm["team_id"]
        if not tid:
            continue
        entry = by_team.setdefault(
            tid,
            {
                "name": tid,
                "description": tm["description"] or tm["team_name"],
                "topology": "team",
                "entry_agent": "",
                "agents_count": 0,
                "hermes_profiles": [],
                "agents": [],
                # Team-derived swarms aren't file-editable, but support "delete"
                # as ungroup: unlinking the team's profiles. The team, kanban and
                # Hermes profiles are kept intact.
                "deletable": True,
                "source": "team",
                "team_id": tid,
                "team_name": tm["team_name"] or tid,
            },
        )
        entry["hermes_profiles"].append(slug)
    for entry in by_team.values():
        entry["agents_count"] = len(entry["hermes_profiles"])
        entry["entry_agent"] = _guess_orchestrator_slug(
            entry["hermes_profiles"],
            entry.get("agents") or [],
            profiles_by_slug,
        )
        ensure_swarm_handoffs(entry)
    return list(by_team.values())


def _swarm_meta_for_profile(
    slug: str,
    swarm_index: dict[str, dict[str, Any]],
    team_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    meta = dict(swarm_index.get(slug) or {})
    if meta.get("swarm_name"):
        return meta
    tm = team_map.get(slug)
    if not tm:
        return meta
    return {
        "swarm_name": tm["team_id"],
        "swarm_description": tm["description"] or tm["team_name"],
        "topology": "team",
        "entry_agent": "",
        "is_entry": False,
        "role": meta.get("role") or "",
        "handoff_to": list(meta.get("handoff_to") or []),
    }


def _swarm_agent_index(
    swarms: list[dict[str, Any]],
    profiles_by_slug: Optional[dict[str, dict[str, Any]]] = None,
    alias_map: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Map profile slug -> swarm agent metadata."""
    index: dict[str, dict[str, Any]] = {}
    for swarm in swarms:
        swarm_name = str(swarm.get("name") or "")
        profiles = swarm.get("hermes_profiles") or []
        if not isinstance(profiles, list):
            profiles = []
        agents = [
            a for a in (swarm.get("agents") or []) if isinstance(a, dict)
        ]
        entry = _resolve_entry_agent_slug(
            str(swarm.get("entry_agent") or ""),
            profiles=profiles,
            agents=agents,
            profiles_by_slug=profiles_by_slug,
            alias_map=alias_map,
        )
        agents_by_name = {
            _norm_slug(a.get("name")): a
            for a in agents
            if a.get("name")
        }
        for prof in profiles:
            slug = _norm_slug(str(prof))
            if not slug:
                continue
            spec = agents_by_name.get(slug) or {}
            index[slug] = {
                "swarm_name": swarm_name,
                "swarm_description": str(swarm.get("description") or ""),
                "topology": str(swarm.get("topology") or ""),
                "entry_agent": entry,
                "is_entry": bool(entry) and _norm_slug(entry) == slug,
                "role": str(spec.get("role") or ""),
                "handoff_to": list(spec.get("handoff_to") or []),
                "role_preset": str(spec.get("role_preset") or ""),
                "role_preset_label": str(spec.get("role_preset_label") or ""),
                "role_category": str(spec.get("role_category") or ""),
            }
        for agent in agents:
            slug = _norm_slug(str(agent.get("name") or ""))
            if not slug or slug in index:
                continue
            index[slug] = {
                "swarm_name": swarm_name,
                "swarm_description": str(swarm.get("description") or ""),
                "topology": str(swarm.get("topology") or ""),
                "entry_agent": entry,
                "is_entry": bool(entry) and _norm_slug(entry) == slug,
                "role": str(agent.get("role") or ""),
                "handoff_to": list(agent.get("handoff_to") or []),
                "role_preset": str(agent.get("role_preset") or ""),
                "role_preset_label": str(agent.get("role_preset_label") or ""),
                "role_category": str(agent.get("role_category") or ""),
            }
    return index


def _task_title_for_id(
    task_id: str,
    boards: list[dict[str, Any]],
    *,
    title_index: Optional[dict[str, str]] = None,
) -> str:
    tid = str(task_id or "").strip()
    if not tid:
        return ""
    if title_index is not None:
        return title_index.get(tid, tid)
    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if str(task.get("id") or "").strip() == tid:
                return str(task.get("title") or tid)
    return tid


def _execution_log_lines(job: dict[str, Any]) -> list[str]:
    """Prefer agent.log tail; fall back to progress lines or last message."""
    log_tail = job.get("log_tail")
    if isinstance(log_tail, list):
        lines = [str(x).strip() for x in log_tail if str(x).strip()]
        if lines:
            return lines
    progress = job.get("progress_lines")
    if isinstance(progress, list):
        lines = [str(x).strip() for x in progress if str(x).strip()]
        if lines:
            return lines
    last = str(job.get("progress") or "").strip()
    return [last] if last else []


def _canonical_profile_slug(
    raw: str,
    alias_map: Optional[dict[str, str]] = None,
) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if alias_map:
        resolved = _resolve_assignee_slug(text, alias_map)
        if resolved:
            return resolved
    slug = _profile_slug(text)
    return slug or _norm_slug(text)


def _profile_from_session_key(session_key: str) -> str:
    key = str(session_key or "").strip()
    if not key:
        return ""
    if key.startswith("hermes:"):
        parts = key.split(":")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()
    if ":" in key:
        tail = key.rsplit(":", 1)[-1].strip()
        if tail and _profile_slug(tail):
            return tail
    return ""


def _append_running_job(
    out: dict[str, list[dict[str, Any]]],
    raw_profile: str,
    row: dict[str, Any],
    alias_map: Optional[dict[str, str]] = None,
) -> None:
    slug = _canonical_profile_slug(raw_profile, alias_map)
    if slug:
        out.setdefault(slug, []).append(row)


def _running_job_row(
    *,
    kind: str,
    name: str,
    progress_lines: list[str],
    log_tail: Optional[list[str]] = None,
    progress: str = "",
    job_id: str = "",
    task_id: str = "",
    started_at: Any = None,
) -> dict[str, Any]:
    lines = [str(x).strip() for x in progress_lines if str(x).strip()]
    tail = [str(x).strip() for x in (log_tail or lines) if str(x).strip()]
    last = str(progress or "").strip() or (lines[-1] if lines else "") or (tail[-1] if tail else "")
    feed = tail or lines or ([last] if last else [])
    row: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "progress": last,
        "progress_lines": feed,
        "log_tail": tail or feed,
    }
    if job_id:
        row["job_id"] = job_id
    if task_id:
        row["task_id"] = task_id
    if started_at is not None:
        row["started_at"] = started_at
    return row


def _priority_queue_rows(
    monitor: GoisMonitor,
    alias_map: Optional[dict[str, str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Map profile slug -> priority-queue cards (running first, then queued)."""
    handler = getattr(monitor, "_priority_queue_handler", None)
    if handler is None:
        return {}
    try:
        queue = handler.engine.get_queue()
    except Exception as exc:
        log.debug("swarm robots: priority queue unavailable: %s", exc)
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for bucket, kind in (("running", "priority_queue"), ("queued", "priority_queue_queued")):
        for row in queue.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            profile_raw = str(row.get("assignee") or "").strip()
            lines = [
                str(x).strip()
                for x in (row.get("progress") or [])
                if str(x).strip()
            ]
            last_progress = str(row.get("last_progress") or "").strip()
            progress_lines = lines or ([last_progress] if last_progress else [])
            if bucket == "queued" and not progress_lines:
                progress_lines = ["Na fila de prioridades aguardando vez…"]
            _append_running_job(
                out,
                profile_raw,
                _running_job_row(
                    kind=kind,
                    name=str(row.get("title") or row.get("task_id") or ""),
                    progress_lines=progress_lines,
                    progress=last_progress or progress_lines[-1] if progress_lines else "",
                    job_id=str(row.get("id") or ""),
                    task_id=str(row.get("task_id") or ""),
                    started_at=row.get("started_at"),
                ),
                alias_map,
            )
    return out


def _priority_queue_errors_by_task(
    monitor: GoisMonitor,
) -> dict[str, str]:
    """Map task_id -> most recent priority-queue terminal error message."""
    handler = getattr(monitor, "_priority_queue_handler", None)
    if handler is None:
        return {}
    try:
        return handler.engine.last_error_by_task()
    except Exception as exc:  # noqa: BLE001 — never break the robots view
        log.debug("swarm robots: priority queue errors unavailable: %s", exc)
        return {}


_KANBAN_TASK_PROMPT_MARKERS = (
    "você deve executar uma tarefa do kanban",
    "tarefa alvo:",
)


def _empty_schedule_summary() -> dict[str, Any]:
    return {
        "has_schedule": False,
        "jobs_count": 0,
        "active_count": 0,
        "paused_count": 0,
        "next_run_at": None,
        "next_run_in_seconds": None,
        "schedule_display": None,
        "schedule_input": None,
        "nearest_job_id": None,
        "nearest_job_name": None,
        "status": "none",
        "schedule_target": None,
        "schedule_task_id": None,
    }


def _schedule_input_from_job(job: dict[str, Any], default: str = "") -> str:
    """Return an editable Hermes schedule string for a cron job."""
    sched = job.get("schedule")
    if isinstance(sched, dict):
        display = str(sched.get("display") or "").strip()
        if display:
            return display
        expr = str(sched.get("expr") or "").strip()
        if expr:
            return expr
        minutes = sched.get("minutes")
        if minutes is not None:
            return f"every {int(minutes)}m"
    legacy = str(job.get("schedule_display") or "").strip()
    if legacy:
        return legacy
    return default


def _is_recurring_robot_cron(job: dict[str, Any]) -> bool:
    """True for agent cron jobs (exclude shell wrappers and one-off kanban runs)."""
    if bool(job.get("no_agent")) or str(job.get("script") or "").strip():
        return False
    prompt = str(job.get("prompt") or job.get("message") or "").strip().lower()
    if prompt and any(marker in prompt for marker in _KANBAN_TASK_PROMPT_MARKERS):
        return False
    return True


def _resolve_hermes_cron_jobs_path_for_monitor(
    monitor: "GoisMonitor",
) -> Path:
    getter = getattr(monitor, "_hermes_cron_jobs_path", None)
    if callable(getter):
        try:
            return Path(getter())
        except Exception:
            pass
    return resolve_hermes_cron_jobs_path()


def _upsert_robot_cron_schedule(
    monitor: "GoisMonitor",
    profile_slug: str,
    schedule: str,
    *,
    swarm_name: Optional[str] = None,
    user: Any = None,
    schedule_target: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update the recurring Hermes cron for one robot profile."""
    schedule = str(schedule or "").strip()
    if not schedule:
        return {"ok": False, "error": "schedule is required"}

    create_cfg = getattr(getattr(monitor, "cfg", None), "hermes_agent_create", None)
    if create_cfg is None:
        return {"ok": False, "error": "hermes_agent_create não configurado"}

    team_ids = _team_ids_for_profile(
        monitor, profile_slug, user, swarm_name=swarm_name
    )
    if not team_ids:
        return {
            "ok": False,
            "error": _SCHEDULE_TEAM_REQUIRED_MSG,
            "requires_team": True,
        }

    target = str(schedule_target or "").strip().lower()
    explicit_task = str(task_id or "").strip()
    want_card = target == "card" or bool(explicit_task)
    if target == "team":
        want_card = False

    jobs_path = _resolve_hermes_cron_jobs_path_for_monitor(monitor)
    jobs, _ = read_jobs_file(jobs_path)
    profile_key = _norm_slug(profile_slug)
    _all_jobs, team_jobs, card_jobs = _profile_cron_jobs_split(jobs, profile_key)

    if not target and not team_jobs and not card_jobs:
        boards_probe, _ = _load_kanban_context(monitor, user)
        alias_probe = _build_assignee_alias_map(
            {profile_key: {"slug": profile_key, "display_name": profile_slug}},
        )
        if _pick_team_card_for_schedule(
            profile_slug, team_ids, boards_probe, alias_probe
        ):
            want_card = True

    if want_card:
        remove_ids: list[str] = []
        if team_jobs:
            remove_ids.extend(str(job.get("id") or "").strip() for job in team_jobs)
        if card_jobs and explicit_task:
            for job in card_jobs:
                bound_task = _task_id_from_kanban_cron_job(job) or ""
                if bound_task and bound_task != explicit_task:
                    remove_ids.append(str(job.get("id") or "").strip())
        if remove_ids:
            removed = _remove_cron_job_ids(jobs_path, remove_ids)
            if not removed.get("ok"):
                return {
                    "ok": False,
                    "error": removed.get("error") or "falha ao trocar modo de agendamento",
                }
            _invalidate_hermes_cron_cache(monitor)
            jobs, _ = read_jobs_file(jobs_path)
            _all_jobs, team_jobs, card_jobs = _profile_cron_jobs_split(jobs, profile_key)

        boards, _ = _load_kanban_context(monitor, user)
        profiles_by_slug = {
            profile_key: {"slug": profile_key, "display_name": profile_slug},
        }
        alias_map = _build_assignee_alias_map(profiles_by_slug)
        pick = _resolve_schedule_card_pick(
            profile_slug,
            team_ids,
            boards,
            alias_map,
            task_id=explicit_task or None,
        )
        if not pick or not pick.get("task_id") or not pick.get("workdir"):
            return {
                "ok": False,
                "error": _SCHEDULE_CARD_REQUIRED_MSG,
                "requires_team_card": True,
            }

        payload: dict[str, Any] = {
            "workdir": pick["workdir"],
            "team_id": pick.get("team_id") or "",
            "task_id": pick["task_id"],
            "assignee": profile_slug,
            "once": False,
            "schedule": schedule,
            "async": False,
        }
        if pick.get("kanban_file") is not None:
            payload["kanban_file"] = pick["kanban_file"]

        actor_getter = getattr(monitor, "_system_actor", None)
        actor = actor_getter(payload) if callable(actor_getter) else None
        schedule_runner = getattr(monitor, "_execute_kanban_schedule", None)
        if not callable(schedule_runner):
            return {"ok": False, "error": "agendamento kanban indisponível"}
        result = schedule_runner(payload, actor)
        if result.get("ok"):
            result["action"] = "updated" if card_jobs else "created"
            result["task_id"] = pick["task_id"]
            result["team_id"] = pick.get("team_id")
            result["schedule_target"] = "card"
            _invalidate_hermes_cron_cache(monitor)
        else:
            result.setdefault("error", result.get("error") or "falha ao criar cron")
        return result

    if card_jobs:
        removed = _remove_cron_job_ids(
            jobs_path,
            [str(job.get("id") or "").strip() for job in card_jobs],
        )
        if not removed.get("ok"):
            return {
                "ok": False,
                "error": removed.get("error") or "falha ao trocar para modo time",
            }
        _invalidate_hermes_cron_cache(monitor)
        jobs, _ = read_jobs_file(jobs_path)
        _all_jobs, team_jobs, card_jobs = _profile_cron_jobs_split(jobs, profile_key)

    if team_jobs:
        active = [job for job in team_jobs if job.get("active")]
        target_job = active[0] if active else team_jobs[0]
        job_id = str(target_job.get("id") or "").strip()
        if not job_id:
            return {"ok": False, "error": "cron job sem id"}
        result = update_hermes_cron_job(
            job_id,
            schedule=schedule,
            accept_hooks=bool(getattr(create_cfg, "cron_accept_hooks", False)),
            timeout_seconds=float(getattr(create_cfg, "cron_timeout_seconds", 120.0)),
            jobs_path=jobs_path,
        )
        if result.get("ok"):
            result["action"] = "updated"
            result["job_id"] = job_id
            result["schedule_target"] = "team"
            _invalidate_hermes_cron_cache(monitor)
        else:
            result.setdefault(
                "error",
                result.get("reason") or result.get("summary") or "falha ao atualizar cron",
            )
        return result

    return _create_team_auto_cron(
        monitor,
        profile_slug,
        schedule,
        create_cfg=create_cfg,
        user=user,
    )


def _is_cron_job_paused(job: dict[str, Any]) -> bool:
    state = job.get("state")
    return not job.get("enabled", True) or (
        isinstance(state, str) and state.lower() == "paused"
    )


def _resume_robot_cron_schedule(
    monitor: GoisMonitor,
    profile_slug: str,
) -> dict[str, Any]:
    """Resume every paused Hermes cron job bound to one robot profile."""
    profile_key = _norm_slug(profile_slug)
    if not profile_key:
        return {"ok": False, "error": "slug is required"}

    create_cfg = getattr(getattr(monitor, "cfg", None), "hermes_agent_create", None)
    if create_cfg is None:
        return {"ok": False, "error": "hermes_agent_create não configurado"}

    jobs_path = _resolve_hermes_cron_jobs_path_for_monitor(monitor)
    jobs, _ = read_jobs_file(jobs_path)
    all_jobs, _, _ = _profile_cron_jobs_split(jobs, profile_key)
    paused_ids = [
        str(job.get("id") or "").strip()
        for job in all_jobs
        if _is_cron_job_paused(job) and str(job.get("id") or "").strip()
    ]
    if not paused_ids:
        return {
            "ok": True,
            "resumed_count": 0,
            "paused_count": 0,
            "message": "nenhum cron pausado para este agente",
        }

    snapshot = CronJobsPauseSnapshot(paused_job_ids=tuple(paused_ids))
    result = resume_hermes_cron_jobs_from_snapshot(
        snapshot,
        jobs_path,
        accept_hooks=bool(getattr(create_cfg, "cron_accept_hooks", False)),
        timeout_seconds=float(getattr(create_cfg, "cron_timeout_seconds", 120.0)),
    )
    _invalidate_hermes_cron_cache(monitor)
    inval_swarm = getattr(monitor, "_invalidate_swarm_robots_cache", None)
    if callable(inval_swarm):
        inval_swarm()

    failures = result.get("failures") or []
    first_error = None
    if failures:
        first = failures[0] if isinstance(failures[0], dict) else {}
        first_error = first.get("error") or first.get("summary") or "falha ao despausar cron"

    return {
        "ok": bool(result.get("ok")),
        "resumed_count": int(result.get("resumed_count") or 0),
        "paused_count": len(paused_ids),
        "failures": failures,
        "action": "resumed",
        "error": first_error,
    }


def _parse_next_run_ts(job: dict[str, Any]) -> Optional[float]:
    """Return the next cron run as a Unix timestamp, if known."""
    next_iso = str(job.get("next_run_at") or "").strip()
    if not next_iso:
        next_iso = str(compute_next_run_at_for_job(job) or "").strip()
    if not next_iso:
        return None
    try:
        dt = datetime.fromisoformat(next_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except ValueError:
        return None


def _summarize_profile_cron_jobs(
    jobs: list[dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Aggregate Hermes cron jobs assigned to one robot profile."""
    if not jobs:
        return _empty_schedule_summary()

    now = time.time() if now is None else now
    active = [job for job in jobs if job.get("active")]
    paused_count = len(jobs) - len(active)

    nearest_job: Optional[dict[str, Any]] = None
    nearest_ts: Optional[float] = None
    for job in active:
        ts = _parse_next_run_ts(job)
        if ts is None:
            continue
        if nearest_ts is None or ts < nearest_ts:
            nearest_ts = ts
            nearest_job = job

    if not active:
        status = "paused"
    elif nearest_ts is None:
        status = "overdue"
    elif nearest_ts <= now - 300:
        status = "overdue"
    else:
        status = "scheduled"

    next_run_at: Optional[str] = None
    next_run_in_seconds: Optional[int] = None
    if nearest_ts is not None:
        next_run_at = datetime.fromtimestamp(nearest_ts).isoformat()
        next_run_in_seconds = max(0, int(nearest_ts - now))

    schedule_display = None
    schedule_input = None
    nearest_job_id = None
    nearest_job_name = None
    if nearest_job:
        schedule_display = str(nearest_job.get("schedule_display") or "").strip() or None
        schedule_input = _schedule_input_from_job(nearest_job) or schedule_display
        nearest_job_id = str(nearest_job.get("id") or "").strip() or None
        nearest_job_name = str(
            nearest_job.get("name") or nearest_job.get("id") or ""
        ).strip() or None
    elif active:
        fallback = active[0]
        schedule_display = str(fallback.get("schedule_display") or "").strip() or None
        schedule_input = _schedule_input_from_job(fallback) or schedule_display
        nearest_job_id = str(fallback.get("id") or "").strip() or None
        nearest_job_name = str(
            fallback.get("name") or fallback.get("id") or ""
        ).strip() or None
    elif jobs:
        fallback = jobs[0]
        schedule_display = str(fallback.get("schedule_display") or "").strip() or None
        schedule_input = _schedule_input_from_job(fallback) or schedule_display
        nearest_job_id = str(fallback.get("id") or "").strip() or None
        nearest_job_name = str(
            fallback.get("name") or fallback.get("id") or ""
        ).strip() or None

    schedule_target, schedule_task_id = _schedule_meta_from_jobs(jobs)

    return {
        "has_schedule": True,
        "jobs_count": len(jobs),
        "active_count": len(active),
        "paused_count": paused_count,
        "next_run_at": next_run_at,
        "next_run_in_seconds": next_run_in_seconds,
        "schedule_display": schedule_display,
        "schedule_input": schedule_input,
        "nearest_job_id": nearest_job_id,
        "nearest_job_name": nearest_job_name,
        "status": status,
        "schedule_target": schedule_target,
        "schedule_task_id": schedule_task_id,
    }


def _schedules_by_profile(
    cron_snap: dict[str, Any],
    alias_map: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, Any]]:
    """Map canonical profile slug -> cron schedule summary."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in cron_snap.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        profile_raw = str(
            row.get("profile") or row.get("source_profile") or ""
        ).strip()
        slug = _canonical_profile_slug(profile_raw, alias_map)
        if not slug:
            continue
        grouped.setdefault(slug, []).append(row)
    return {slug: _summarize_profile_cron_jobs(rows) for slug, rows in grouped.items()}


def _cron_job_record(
    cron_snap: dict[str, Any],
    job_id: str,
) -> Optional[dict[str, Any]]:
    needle = str(job_id or "").strip()
    if not needle:
        return None
    for row in cron_snap.get("jobs") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == needle:
            return row
    return None


def _card_has_execution_failure(
    card: dict[str, Any],
    cron_snap: Optional[dict[str, Any]],
    *,
    pq_error: Optional[str] = None,
) -> bool:
    """True when a doing/review card already failed and should be skipped."""
    if pq_error:
        return True
    cron_job_id = str(card.get("cron_job_id") or "").strip()
    if cron_snap and cron_job_id:
        job = _cron_job_record(cron_snap, cron_job_id)
        if job and str(job.get("last_status") or "").strip().lower() == "error":
            return True
    return False


def _pick_working_card(
    active_cards: list[dict[str, Any]],
    cron_snap: Optional[dict[str, Any]],
    pq_errors_by_task: dict[str, str],
) -> Optional[dict[str, Any]]:
    """First actionable card for a robot; skips ones that already failed."""
    for card in active_cards:
        task_id = str(card.get("id") or "").strip()
        if _card_has_execution_failure(
            card,
            cron_snap,
            pq_error=pq_errors_by_task.get(task_id),
        ):
            continue
        return card
    return None


def _robot_pre_start_brief(
    robot: dict[str, Any],
    *,
    execution_log: Optional[list[str]] = None,
) -> str:
    """Human-readable plan shown before Hermes publishes a live log."""
    lines: list[str] = []
    wc = robot.get("working_card") if isinstance(robot.get("working_card"), dict) else {}
    card_id = str(wc.get("id") or "").strip()
    card_title = str(wc.get("title") or "").strip()
    if card_id and card_title:
        lines.append(f"Card em foco: {card_id} — {card_title}")
    elif card_id or card_title:
        lines.append(f"Card em foco: {card_id or card_title}")

    role = str(robot.get("role") or "").strip()
    if role:
        lines.append(f"Papel: {role}")

    mission = str(robot.get("description") or "").strip()
    if mission:
        preview = mission if len(mission) <= 240 else mission[:240] + "…"
        lines.append(f"Missão: {preview}")

    swarm_name = str(robot.get("swarm_name") or "").strip()
    if swarm_name and not card_id and not card_title:
        lines.append(f"Swarm: {swarm_name}")

    schedule = robot.get("schedule") if isinstance(robot.get("schedule"), dict) else {}
    if schedule.get("has_schedule") and str(schedule.get("status") or "") == "paused":
        lines.append("Cron pausado — use Despausar ou Executar agora para rodar.")
    elif schedule.get("next_run_at"):
        lines.append(f"Próximo cron: {schedule.get('next_run_at')}")

    for row in execution_log or []:
        text = str(row or "").strip()
        if text and text not in lines:
            lines.append(text)

    if not lines:
        return "Aguardando card do kanban ou execução manual do swarm."
    return "\n".join(lines)


def _working_card_idle_hints(
    working_card: dict[str, Any],
    cron_snap: Optional[dict[str, Any]],
    *,
    queue_queued: int = 0,
    pq_error: Optional[str] = None,
) -> list[str]:
    """Explain why a doing/review card has no live Hermes log yet."""
    wc = working_card or {}
    task_id = str(wc.get("id") or "—")
    lines = [
        "Card em execução no Kanban, mas o Hermes ainda não publicou log ao vivo.",
        "A fila de prioridades tentará agendar automaticamente.",
        f"Tarefa: {task_id} · use Executar agora ou 📅 Agendar no Kanban se continuar parado.",
    ]
    if queue_queued > 0:
        lines.insert(
            1,
            f"{queue_queued} card(s) deste agente na fila de prioridades aguardando vez.",
        )

    # Surface the real reason the auto-scheduler keeps bouncing this card, so
    # users can act (e.g. create the missing Hermes profile) instead of seeing
    # only the generic "ainda não publicou log" message.
    pq_error = (pq_error or "").strip()
    if pq_error:
        lines.insert(
            0,
            f"Última tentativa da fila falhou: {pq_error[:200]}",
        )
        if "does not exist" in pq_error or "não existe" in pq_error.lower():
            lines.append(
                "Auto-agendamento pausado para esta tarefa até o erro ser corrigido."
            )

    cron_job_id = str(wc.get("cron_job_id") or "").strip()
    if cron_snap and cron_job_id:
        job = _cron_job_record(cron_snap, cron_job_id)
        if job:
            live_ids = {
                str(row.get("job_id") or "").strip()
                for row in (cron_snap.get("running") or [])
                if isinstance(row, dict)
            }
            last_status = str(job.get("last_status") or "").strip().lower()
            if last_status == "running" and cron_job_id not in live_ids:
                lines.insert(
                    0,
                    f"Cron `{cron_job_id}` marcado como running no jobs.json, "
                    "mas sem sessão ativa no agent.log (estado obsoleto).",
                )
            elif last_status == "error":
                err = str(job.get("last_error") or "").strip()
                lines.insert(
                    0,
                    f"Última execução do cron `{cron_job_id}` falhou"
                    + (f": {err[:160]}" if err else "."),
                )
            elif cron_job_id not in live_ids:
                next_run = str(job.get("next_run_at") or "").strip()
                if not cron_next_run_is_plausible(job):
                    detail = f" (era {next_run})" if next_run else ""
                    lines.insert(
                        0,
                        f"Cron `{cron_job_id}` sem próxima execução plausível{detail} — "
                        "a fila de prioridades vai assumir este card.",
                    )
                elif next_run:
                    lines.insert(0, f"Cron `{cron_job_id}` agendado; próxima execução: {next_run}.")
    return lines


def _errors_by_profile(
    cron_snap: dict[str, Any],
    alias_map: Optional[dict[str, str]] = None,
    *,
    max_per_profile: int = 10,
    recent_jobs: Optional[list] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Map profile slug -> recent errors (cron jobs + kanban schedule)."""
    out: dict[str, list[dict[str, Any]]] = {}

    def _add(raw_profile: str, entry: dict[str, Any]) -> None:
        slug = _canonical_profile_slug(raw_profile, alias_map)
        if not slug:
            return
        rows = out.setdefault(slug, [])
        if len(rows) < max_per_profile:
            rows.append(entry)

    for job in cron_snap.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("last_status") or "").strip().lower() != "error":
            continue
        profile_raw = str(
            job.get("profile") or job.get("source_profile") or ""
        ).strip()
        message = str(job.get("last_error") or "").strip()
        _add(
            profile_raw,
            {
                "source": "cron",
                "name": str(job.get("name") or job.get("id") or ""),
                "job_id": str(job.get("id") or ""),
                "when": str(job.get("last_run_at") or ""),
                "message": message or "Última execução do cron terminou com erro.",
            },
        )

    _recent = recent_jobs if recent_jobs is not None else kanban_schedule_list_recent()
    for job in _recent:
        if job.status != "error":
            continue
        when = ""
        if job.finished_at:
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(job.finished_at)
            )
        _add(
            job.profile,
            {
                "source": "kanban",
                "name": str(job.task_id or job.id or ""),
                "job_id": str(job.id or ""),
                "when": when,
                "message": str(job.error or "").strip()
                or "Agendamento do card falhou.",
            },
        )
    return out


def _running_by_profile(
    monitor: GoisMonitor,
    alias_map: Optional[dict[str, str]] = None,
    *,
    pq_rows: Optional[dict[str, list[dict[str, Any]]]] = None,
    cron_snap: Optional[dict[str, Any]] = None,
    recent_jobs: Optional[list] = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if monitor.cfg.hermes:
        try:
            snap = cron_snap if cron_snap is not None else monitor._cached_hermes_cron_snapshot()
            for job in snap.get("running") or []:
                if not isinstance(job, dict):
                    continue
                profile_raw = str(
                    job.get("profile") or job.get("source_profile") or ""
                ).strip()
                log_tail = [
                    str(x).strip()
                    for x in (job.get("log_tail") or [])
                    if str(x).strip()
                ]
                last_msg = str(job.get("last_message") or "")
                progress_lines = log_tail or ([last_msg] if last_msg else [])
                _append_running_job(
                    out,
                    profile_raw,
                    _running_job_row(
                        kind="cron",
                        name=str(job.get("name") or job.get("job_id") or ""),
                        progress_lines=progress_lines,
                        log_tail=log_tail,
                        progress=last_msg,
                        job_id=str(job.get("job_id") or ""),
                        started_at=job.get("started_at"),
                    ),
                    alias_map,
                )
        except Exception:
            pass
    _recent = recent_jobs if recent_jobs is not None else kanban_schedule_list_recent()
    for job in _recent:
        if job.status != "running":
            continue
        row = kanban_schedule_job_to_dict(job)
        lines = [
            str(x).strip()
            for x in (row.get("progress") or [])
            if str(x).strip()
        ]
        last_progress = str(row.get("lastProgress") or "").strip()
        progress_lines = lines or ([last_progress] if last_progress else [])
        _append_running_job(
            out,
            job.profile,
            _running_job_row(
                kind="kanban_schedule",
                name=str(row.get("taskId") or ""),
                progress_lines=progress_lines,
                progress=last_progress,
                job_id=str(row.get("jobId") or ""),
                task_id=str(row.get("taskId") or ""),
                started_at=row.get("startedAt"),
            ),
            alias_map,
        )
    for job in chat_jobs_list_running():
        profile_raw = str(job.profile or job.agent_id or "").strip()
        lines = [str(x).strip() for x in job.progress if str(x).strip()]
        last_progress = str(job.last_progress or "").strip()
        progress_lines = lines or ([last_progress] if last_progress else [])
        preview = str(job.message_text or "").strip()
        name = preview[:80] if preview else (last_progress[:80] or "chat")
        _append_running_job(
            out,
            profile_raw,
            _running_job_row(
                kind="chat",
                name=name,
                progress_lines=progress_lines,
                progress=last_progress,
                job_id=str(job.id or ""),
                started_at=job.started_at,
            ),
            alias_map,
        )
    for run in list_active_tool_runs():
        if not isinstance(run, dict):
            continue
        profile_raw = _profile_from_session_key(str(run.get("sessionKey") or ""))
        label = str(run.get("label") or run.get("kind") or "ferramentas").strip()
        turn = run.get("turn")
        max_turns = run.get("maxTurns")
        detail = label
        if turn is not None and max_turns:
            detail = f"{label} · passo {turn}/{max_turns}"
        _append_running_job(
            out,
            profile_raw,
            _running_job_row(
                kind="tool",
                name=label,
                progress_lines=[detail] if detail else [],
                progress=detail,
                job_id=str(run.get("jobId") or ""),
                started_at=run.get("startedAt"),
            ),
            alias_map,
        )
    pq_by_profile = pq_rows if pq_rows is not None else _priority_queue_rows(monitor, alias_map)
    for slug, rows in pq_by_profile.items():
        for row in rows:
            if row.get("kind") == "priority_queue":
                out.setdefault(slug, []).insert(0, row)
            else:
                out.setdefault(slug, []).append(row)
    return out


def _enrich_running_jobs(
    jobs: list[dict[str, Any]],
    boards: list[dict[str, Any]],
    *,
    title_index: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for job in jobs:
        row = dict(job)
        task_id = str(row.get("task_id") or "").strip()
        if task_id:
            title = _task_title_for_id(task_id, boards, title_index=title_index)
            if title:
                row["name"] = title
        enriched.append(row)
    return enriched


def _robot_swarmish(robot: dict[str, Any]) -> bool:
    return _is_swarmish(str(robot.get("slug") or "")) or _is_swarmish(
        str(robot.get("display_name") or "")
    )


def _virtual_swarm_dismissals_path() -> Path:
    from .local_paths import project_stack_root
    from .runtime_swarm_paths import swarm_definitions_dir

    d = swarm_definitions_dir(project_stack_root())
    d.mkdir(parents=True, exist_ok=True)
    return d / ".dismissed_virtual_swarms.json"


DISMISSED_VIRTUAL_SWARMS_KEY = "swarm:dismissed_virtual"


def load_dismissed_virtual_swarms() -> set[str]:
    """Virtual swarm labels dismissed by the user (auto-grouping suppressed)."""
    from .runtime_state import load_json

    data = load_json(DISMISSED_VIRTUAL_SWARMS_KEY, _virtual_swarm_dismissals_path())
    if not isinstance(data, list):
        return set()
    return {_norm_slug(s) for s in data if _norm_slug(str(s))}


def _save_dismissed_virtual_swarms(labels: set[str]) -> None:
    from .runtime_state import save_json

    try:
        save_json(
            DISMISSED_VIRTUAL_SWARMS_KEY,
            sorted(labels),
            _virtual_swarm_dismissals_path(),
        )
    except Exception as exc:
        log.warning("swarm: failed to save virtual dismissals: %s", exc)


def dismiss_virtual_swarm(name: str) -> Optional[dict[str, Any]]:
    """Stop auto-grouping robots under a virtual swarm label.

    Returns a result dict when *name* is a dismissible virtual label, or
    ``None`` when it does not apply (caller keeps the original error).
    """
    safe = _norm_slug(name)
    if not safe or not _is_swarmish(safe):
        return None
    dismissed = load_dismissed_virtual_swarms()
    if safe in dismissed:
        return {
            "ok": True,
            "name": safe,
            "already_dismissed": True,
            "dismissed_virtual_swarm": safe,
        }
    dismissed.add(safe)
    _save_dismissed_virtual_swarms(dismissed)
    log.info("dismissed virtual swarm grouping %s", safe)
    return {"ok": True, "name": safe, "dismissed_virtual_swarm": safe}


def _snapshot_swarm_row_to_state(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a merged /swarm snapshot row into executable swarm state."""
    profiles = [
        str(p).strip()
        for p in (row.get("hermes_profiles") or [])
        if str(p).strip()
    ]
    state: dict[str, Any] = {
        "name": row.get("name"),
        "description": str(row.get("description") or ""),
        "topology": str(row.get("topology") or "handoff"),
        "entry_agent": str(row.get("entry_agent") or ""),
        "hermes_profiles": profiles,
        "agents": [
            dict(a) for a in (row.get("agents") or []) if isinstance(a, dict)
        ],
    }
    if row.get("team_id"):
        state["team_id"] = row["team_id"]
    source = str(row.get("source") or "").strip()
    if source:
        state["source"] = source
    return ensure_swarm_handoffs(state)


def load_merged_swarm_state(
    swarm_name: str,
    monitor: "GoisMonitor",
    user: Any = None,
) -> Optional[dict[str, Any]]:
    """Resolve swarm state from the merged robots snapshot (virtual/team UI groups)."""
    handler = getattr(monitor, "handle_swarm_robots_snapshot", None)
    if handler is None:
        return None
    try:
        snapshot = handler(user)
    except Exception as exc:
        log.debug("merged swarm snapshot failed for %s: %s", swarm_name, exc)
        return None
    if not snapshot.get("ok", True):
        return None
    target = _norm_slug(swarm_name)
    for row in snapshot.get("swarms") or []:
        if not isinstance(row, dict):
            continue
        if _norm_slug(str(row.get("name") or "")) != target:
            continue
        return _snapshot_swarm_row_to_state(row)
    return None


def _assign_virtual_swarms(
    robots: list[dict[str, Any]],
    merged_swarms: list[dict[str, Any]],
    known_swarm_names: set[str],
    profiles_by_slug: dict[str, dict[str, Any]],
    team_map: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    """Group orphan swarmish robots (e.g. orelhao-*) for hub layout on /swarm."""
    dismissed = load_dismissed_virtual_swarms()
    orphans = [
        r
        for r in robots
        if not str(r.get("swarm_name") or "").strip() and _robot_swarmish(r)
    ]
    if len(orphans) < 2:
        return

    by_label: dict[str, list[dict[str, Any]]] = {}
    for robot in orphans:
        slug = str(robot.get("slug") or "")
        parts = [p for p in slug.split("-") if p]
        label = parts[0] if parts else slug
        if not _is_swarmish(label):
            label = "agentes-swarm"
        by_label.setdefault(label, []).append(robot)

    for label, group in by_label.items():
        if len(group) < 2:
            continue
        swarm_name = label
        suffix = 2
        while swarm_name in known_swarm_names:
            swarm_name = f"{label}-{suffix}"
            suffix += 1
        if _norm_slug(swarm_name) in dismissed:
            continue
        profiles = [str(r.get("slug") or "") for r in group if str(r.get("slug") or "")]
        entry = _guess_orchestrator_slug(profiles, [], profiles_by_slug)
        description = f"Enxame {swarm_name}"
        team_meta = _resolve_swarm_team_meta(
            {"hermes_profiles": profiles},
            team_map or {},
        )
        for robot in group:
            robot["swarm_name"] = swarm_name
            robot["swarm_description"] = description
            robot["topology"] = robot.get("topology") or "handoff"
        merged_swarms.append(
            {
                "name": swarm_name,
                "description": description,
                "topology": "handoff",
                "entry_agent": entry,
                "agents_count": len(group),
                "hermes_profiles": profiles,
                "agents": [],
                # Virtual groupings aren't file-editable, but can be dismissed
                # (robots stay on disk, shown under "Outros agentes").
                "deletable": True,
                "source": "virtual",
                "team_id": team_meta.get("team_id") or "",
                "team_name": team_meta.get("team_name") or "",
            }
        )
        ensure_swarm_handoffs(merged_swarms[-1])
        known_swarm_names.add(swarm_name)


def _swarm_last_test(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Last graph run (test or flow) for the robots UI."""
    from .swarm_graph import build_graph_view, ensure_graph_checkpoint_preview

    try:
        ensure_graph_checkpoint_preview(state)
        view = build_graph_view(state)
    except Exception as exc:
        log.debug("swarm last_test view failed for %s: %s", state.get("name"), exc)
        return None
    last_run = view.get("last_run")
    if not isinstance(last_run, dict) or not str(last_run.get("status") or "").strip():
        return None
    return {
        "nodes": view.get("nodes") or [],
        "nodes_done": int(view.get("nodes_done") or 0),
        "nodes_total": int(view.get("nodes_total") or 0),
        "order": view.get("order") or [],
        "last_run": last_run,
    }


ROBOT_COLORS: list[str] = [
    "#6aa9ff",
    "#a78bfa",
    "#3ddc84",
    "#ffb454",
    "#ff5a5f",
    "#22d3ee",
    "#f97316",
    "#ec4899",
    "#14b8a6",
    "#eab308",
    "#8b5cf6",
    "#06b6d4",
    "#84cc16",
    "#f43f5e",
    "#6366f1",
]

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_robot_color(value: str) -> str:
    """Return lowercase ``#rrggbb`` or empty when invalid / unset."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith("#"):
        text = "#" + text
    if not _HEX_COLOR_RE.match(text):
        return ""
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def resolve_robot_color(
    *,
    slug: str = "",
    role_preset: str = "",
    role: str = "",
    color: str = "",
) -> str:
    """Profile color overrides role-based palette; else stable hash by role_preset."""
    custom = normalize_robot_color(color)
    if custom:
        return custom
    key = str(role_preset or role or slug or "").strip().lower()
    if not key:
        return ROBOT_COLORS[0]
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return ROBOT_COLORS[h % len(ROBOT_COLORS)]


_ACTIVITY_WINDOW_SECONDS = 4 * 3600


def _parse_activity_ts(value: Any) -> Optional[float]:
    """Parse ISO datetime or unix timestamp for activity filtering."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        try:
            return float(text)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except ValueError:
        return None


def _activity_within_window(
    *,
    started_at: Any = None,
    finished_at: Any = None,
    now: float,
    window_seconds: float,
) -> bool:
    """True when an event started or finished inside the activity window."""
    candidates = [
        _parse_activity_ts(finished_at),
        _parse_activity_ts(started_at),
    ]
    for ts in candidates:
        if ts is not None and (now - ts) <= window_seconds:
            return True
    return False


_ACTIVITY_ROBOT_FIELDS = (
    "robot_slug",
    "robot_name",
    "mascot",
    "color",
    "role_preset",
    "role",
    "team_id",
    "team_name",
)


def _merge_activity_item(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge activity rows without dropping robot identity or execution log."""
    merged = dict(existing)
    patch = dict(incoming)
    for field in _ACTIVITY_ROBOT_FIELDS:
        if not str(patch.get(field) or "").strip():
            existing_val = str(existing.get(field) or "").strip()
            if existing_val:
                patch[field] = existing[field]
    incoming_log = patch.get("execution_log")
    if not (
        isinstance(incoming_log, list) and any(str(x).strip() for x in incoming_log)
    ):
        existing_log = existing.get("execution_log")
        if isinstance(existing_log, list) and any(str(x).strip() for x in existing_log):
            patch["execution_log"] = existing_log
    if not str(patch.get("deliverable") or "").strip():
        existing_del = str(existing.get("deliverable") or "").strip()
        if existing_del:
            patch["deliverable"] = existing_del
    if not patch.get("card") and isinstance(existing.get("card"), dict):
        patch["card"] = existing["card"]
    rank = {"running": 0, "working": 1, "done": 2, "error": 3}
    inc_col = str(patch.get("column") or "")
    ex_col = str(merged.get("column") or "")
    if rank.get(inc_col, 9) < rank.get(ex_col, 9):
        for field in (
            "column",
            "column_label",
            "source",
            "job_id",
            "progress",
            "started_at",
            "cancellable",
            "is_live",
        ):
            if field in patch:
                merged[field] = patch[field]
    elif rank.get(inc_col, 9) > rank.get(ex_col, 9):
        for field in ("column", "column_label", "is_live", "cancellable"):
            patch.pop(field, None)
    merged.update(patch)
    return merged


def _find_robot_slug_for_task(
    task_id: str,
    robots_by_slug: dict[str, dict[str, Any]],
) -> str:
    tid = str(task_id or "").strip()
    if not tid:
        return ""
    for slug, robot in robots_by_slug.items():
        if not isinstance(robot, dict):
            continue
        wc = robot.get("working_card")
        if isinstance(wc, dict) and str(wc.get("id") or "").strip() == tid:
            return str(robot.get("slug") or slug).strip()
        for card in robot.get("cards") or []:
            if not isinstance(card, dict):
                continue
            if str(card.get("id") or "").strip() == tid:
                return str(robot.get("slug") or slug).strip()
        for job in robot.get("running_jobs") or []:
            if not isinstance(job, dict):
                continue
            if str(job.get("task_id") or "").strip() == tid:
                return str(robot.get("slug") or slug).strip()
    return ""


_CANCELLABLE_JOB_KINDS = frozenset(
    {"chat", "kanban_schedule", "priority_queue", "swarm_test"}
)


def _lookup_active_execution_for_task(
    task_id: str,
    *,
    pq_queue: Optional[dict[str, Any]] = None,
    recent_jobs: Optional[list] = None,
) -> Optional[dict[str, Any]]:
    """Find a live execution job for a kanban task id."""
    tid = str(task_id or "").strip()
    if not tid:
        return None
    _recent = recent_jobs if recent_jobs is not None else kanban_schedule_list_recent()
    for job in _recent:
        if job.status != "running":
            continue
        if str(job.task_id or "").strip() != tid:
            continue
        return {
            "job_id": str(job.id or ""),
            "source": "kanban_schedule",
            "column": "running",
            "progress": str(job.last_progress or "").strip(),
            "started_at": job.started_at,
            "column_label": "Executando",
        }
    if isinstance(pq_queue, dict):
        for row in pq_queue.get("running") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("task_id") or "").strip() != tid:
                continue
            return {
                "job_id": str(row.get("id") or ""),
                "source": "priority_queue",
                "column": "running",
                "progress": str(row.get("last_progress") or "").strip(),
                "started_at": row.get("started_at"),
                "column_label": "Executando",
            }
    return None


def _resolve_task_robot_slug(
    task_id: str,
    robots_by_slug: dict[str, dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
    *,
    recent_jobs: Optional[list] = None,
) -> str:
    slug = _find_robot_slug_for_task(task_id, robots_by_slug)
    if slug:
        return slug
    tid = str(task_id or "").strip()
    if not tid:
        return ""
    _recent = recent_jobs if recent_jobs is not None else kanban_schedule_list_recent()
    for job in _recent:
        if job.status != "running":
            continue
        if str(job.task_id or "").strip() != tid:
            continue
        resolved = _canonical_profile_slug(job.profile, alias_map)
        if resolved:
            return resolved
    return ""


def _activity_cancellable(*, source: str, job_id: str, column: str) -> bool:
    kind = str(source or "").strip().lower()
    if kind in {"kanban", "kanban_card"}:
        kind = "kanban_schedule"
    if kind not in _CANCELLABLE_JOB_KINDS:
        return False
    if not str(job_id or "").strip():
        return False
    return str(column or "").strip() == "running"


def _enrich_activity_cancel_fields(
    item: dict[str, Any],
    *,
    pq_queue: Optional[dict[str, Any]] = None,
    recent_jobs: Optional[list] = None,
) -> dict[str, Any]:
    """Resolve job_id/source so running cards show a working stop control."""
    column = str(item.get("column") or "").strip()
    if column != "running":
        return item
    task_id = str(item.get("task_id") or "").strip()
    job_id = str(item.get("job_id") or "").strip()
    source = str(item.get("source") or "").strip().lower()
    if source in {"kanban", "kanban_card"}:
        source = "kanban_schedule"
    if not job_id and task_id:
        active = _lookup_active_execution_for_task(task_id, pq_queue=pq_queue, recent_jobs=recent_jobs)
        if active:
            job_id = str(active.get("job_id") or "").strip()
            source = str(active.get("source") or source).strip().lower()
            if source in {"kanban", "kanban_card"}:
                source = "kanban_schedule"
            item["job_id"] = job_id
            item["source"] = source
            if not str(item.get("started_at") or "").strip():
                item["started_at"] = active.get("started_at")
            if not str(item.get("progress") or "").strip():
                item["progress"] = active.get("progress")
    elif source in {"kanban", "kanban_card"} and job_id:
        item["source"] = "kanban_schedule"
        source = "kanban_schedule"
    item["cancellable"] = _activity_cancellable(
        source=source or str(item.get("source") or ""),
        job_id=job_id or str(item.get("job_id") or ""),
        column=column,
    )
    return item


def _activity_agent_label(robot: dict[str, Any], slug: str) -> str:
    display = str(robot.get("display_name") or "").strip()
    if display:
        return display
    preset = str(robot.get("role_preset_label") or "").strip()
    if preset:
        return preset
    role = str(robot.get("role") or "").strip()
    if role:
        return role
    return str(slug or "").strip() or "Agente"


def _task_id_from_title(title: str) -> str:
    """Extract ``TASK-…`` id from a kanban card title line."""
    head = str(title or "").strip().split(":", 1)[0].strip()
    if head.upper().startswith("TASK-"):
        return head.split()[0]
    return ""


def _activity_item_keys(item: dict[str, Any]) -> list[str]:
    """Candidate dedup keys for one activity row (newest / most specific first)."""
    tid = str(item.get("task_id") or "").strip()
    title = str(item.get("title") or "").strip()
    title_tid = _task_id_from_title(title)
    keys: list[str] = []
    if title_tid and title.startswith(title_tid):
        keys.append(title_tid)
    if tid and tid not in keys:
        keys.append(tid)
    job_id = str(item.get("job_id") or "").strip()
    if job_id and job_id not in keys:
        keys.append(job_id)
    item_id = str(item.get("id") or "").strip()
    robot_slug = str(item.get("robot_slug") or "").strip()
    if item_id and item_id not in keys and item_id != robot_slug:
        keys.append(item_id)
    if robot_slug and title:
        robot_title = f"{_norm_slug(robot_slug)}|{title.casefold()}"
        if robot_title not in keys:
            keys.append(robot_title)
    if not keys:
        keys.append(
            "|".join(
                [
                    robot_slug,
                    title,
                    str(item.get("source") or ""),
                    str(item.get("started_at") or ""),
                ]
            )
        )
    return keys


def _find_activity_item_key(
    items: dict[str, dict[str, Any]],
    item: dict[str, Any],
) -> str:
    """Return the canonical map key for an incoming row, if any alias already exists."""
    incoming = set(_activity_item_keys(item))
    for existing_key, existing in items.items():
        if existing_key in incoming:
            return existing_key
        if set(_activity_item_keys(existing)) & incoming:
            return existing_key
    return ""


def _upsert_activity_item(
    items: dict[str, dict[str, Any]],
    item: dict[str, Any],
    *,
    prefer_live: bool = False,
) -> None:
    key = _find_activity_item_key(items, item) or _activity_item_keys(item)[0]
    existing = items.get(key)
    if existing is None:
        items[key] = item
        return
    if prefer_live or (item.get("is_live") and not existing.get("is_live")):
        items[key] = _merge_activity_item(existing, item)
        return
    if existing.get("is_live"):
        return
    existing_ts = _parse_activity_ts(existing.get("finished_at")) or _parse_activity_ts(
        existing.get("started_at")
    )
    incoming_ts = _parse_activity_ts(item.get("finished_at")) or _parse_activity_ts(
        item.get("started_at")
    )
    if incoming_ts is not None and (
        existing_ts is None or incoming_ts >= existing_ts
    ):
        items[key] = _merge_activity_item(existing, item)


def _resolve_swarm_team_meta(
    swarm: dict[str, Any],
    team_map: dict[str, dict[str, str]],
    teams_index: Optional[dict[str, dict[str, str]]] = None,
) -> dict[str, str]:
    """Resolve team_id/team_name for swarm rows (file, team, or virtual)."""
    tid = str(swarm.get("team_id") or "").strip()
    tname = str(swarm.get("team_name") or "").strip()
    if not tid:
        team_ids: set[str] = set()
        for slug in swarm.get("hermes_profiles") or []:
            tm = team_map.get(_norm_slug(str(slug or "")))
            if tm and tm.get("team_id"):
                team_ids.add(str(tm["team_id"]))
                if not tname:
                    tname = str(tm.get("team_name") or "")
        if len(team_ids) == 1:
            tid = team_ids.pop()
    if tid and not tname and teams_index:
        tname = str((teams_index.get(tid) or {}).get("team_name") or tid)
    return {"team_id": tid, "team_name": tname}


def _swarm_checkpoint_card_label(cp: dict[str, Any]) -> tuple[str, str]:
    """Return ``(card_id, title)`` from a swarm graph checkpoint."""
    agent_cards = cp.get("agent_cards") if isinstance(cp.get("agent_cards"), dict) else {}
    for cards in agent_cards.values():
        if not isinstance(cards, list) or not cards:
            continue
        card = cards[0]
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "").strip()
        title = str(card.get("title") or "").strip()
        if card_id or title:
            return card_id, title
    title = _title_from_swarm_objective(str(cp.get("objective") or ""))
    return _task_id_from_title(title), title


def _swarm_checkpoint_execution_log(cp: dict[str, Any], swarm_name: str) -> list[str]:
    """Human-readable progress lines for an in-flight swarm graph run."""
    lines: list[str] = []
    card_id, card_title = _swarm_checkpoint_card_label(cp)
    if card_id or card_title:
        if card_id and card_title:
            lines.append(f"Card a resolver: {card_id} — {card_title}")
        else:
            lines.append(f"Card a resolver: {card_id or card_title}")

    order = [str(x).strip() for x in (cp.get("order") or []) if str(x).strip()]
    visited = [str(x).strip() for x in (cp.get("visited") or []) if str(x).strip()]
    outputs = cp.get("outputs") if isinstance(cp.get("outputs"), dict) else {}
    done = max(len(visited), len(outputs))
    total = len(order) if order else 0
    current = str(cp.get("current") or "").strip()

    if str(cp.get("status") or "") == "running" and current:
        step = done + 1 if current not in visited else done
        suffix = f" ({step}/{total})" if total else ""
        lines.append(f"Agente `{current}` em execução{suffix}")
    elif visited:
        tail = ", ".join(f"`{n}`" for n in visited[-4:])
        lines.append(f"Agentes concluídos: {tail}")

    for row in cp.get("progress_lines") or []:
        text = str(row or "").strip()
        if text and text not in lines:
            lines.append(text)

    if not lines:
        lines.append(f"Execução do swarm `{swarm_name}` em andamento")
    return lines


def _title_from_swarm_objective(objective: str, slug: str = "") -> str:
    """Best-effort card line from a swarm run objective."""
    slug_n = _norm_slug(slug)
    for line in str(objective or "").splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text:
            continue
        if slug_n and slug_n in _norm_slug(text):
            return text.split("(→", 1)[0].strip()
        if text.upper().startswith("TASK-"):
            return text.split("(→", 1)[0].strip()
    return ""


def _apply_swarm_checkpoint_states(
    robots: list[dict[str, Any]],
    swarms: list[dict[str, Any]],
    alias_map: Optional[dict[str, str]] = None,
    *,
    checkpoints_cache: Optional[dict[str, Any]] = None,
) -> None:
    """Mark robots as running when a swarm graph checkpoint is in-flight."""
    from .swarm_graph import load_checkpoint

    robots_by_slug = {
        _norm_slug(str(row.get("slug") or "")): row
        for row in robots
        if isinstance(row, dict) and row.get("slug")
    }
    for swarm in swarms:
        name = str(swarm.get("name") or "").strip()
        if not name:
            continue
        cp = checkpoints_cache.get(name) if checkpoints_cache is not None else load_checkpoint(name)
        if not cp or str(cp.get("status") or "") != "running":
            continue
        current_raw = str(cp.get("current") or "").strip()
        current = _canonical_profile_slug(current_raw, alias_map) or _norm_slug(current_raw)
        if not current:
            continue
        robot = robots_by_slug.get(current)
        if not robot:
            continue
        robot["state"] = "running"
        agent_cards = cp.get("agent_cards") if isinstance(cp.get("agent_cards"), dict) else {}
        cards = list(
            agent_cards.get(current)
            or agent_cards.get(current_raw)
            or []
        )
        if cards and isinstance(cards[0], dict):
            robot["working_card"] = cards[0]
        elif not robot.get("working_card"):
            title = _title_from_swarm_objective(str(cp.get("objective") or ""), current)
            if title:
                robot["working_card"] = {"id": "", "title": title, "column": "doing"}
        robot["execution_log"] = _swarm_checkpoint_execution_log(cp, name)


def _resolve_live_working_card(
    robot: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(task_id, title)`` from a robot's active kanban card, if any."""
    if not isinstance(robot, dict):
        return "", ""
    wc = robot.get("working_card") if isinstance(robot.get("working_card"), dict) else {}
    title = str(wc.get("title") or "").strip()
    task_id = str(wc.get("id") or "").strip()
    if not task_id:
        task_id = _task_id_from_title(title)
    return task_id, title


def _swarm_checkpoint_activity_items(
    swarms: list[dict[str, Any]],
    robots_by_slug: dict[str, dict[str, Any]],
    team_map: dict[str, dict[str, str]],
    alias_map: Optional[dict[str, str]] = None,
    teams_index: Optional[dict[str, dict[str, str]]] = None,
    *,
    checkpoints_cache: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Live rows for in-flight swarm graph smoke tests (/swarm graph run)."""
    from .swarm_graph import load_checkpoint

    items: list[dict[str, Any]] = []
    for swarm in swarms:
        if not isinstance(swarm, dict):
            continue
        name = str(swarm.get("name") or "").strip()
        if not name:
            continue
        cp = checkpoints_cache.get(name) if checkpoints_cache is not None else load_checkpoint(name)
        if not cp or str(cp.get("status") or "") != "running":
            continue
        current_raw = str(cp.get("current") or "").strip()
        slug = _canonical_profile_slug(current_raw, alias_map) or _norm_slug(current_raw)
        agent_cards = cp.get("agent_cards") if isinstance(cp.get("agent_cards"), dict) else {}
        live_slug = _resolve_live_executor_slug(
            slug or current_raw,
            known_slugs=set(robots_by_slug.keys()),
        )
        if not live_slug:
            for key, card_list in agent_cards.items():
                if not isinstance(card_list, list):
                    continue
                live_slug = _resolve_live_executor_slug(
                    str(key or ""),
                    known_slugs=set(robots_by_slug.keys()),
                )
                if live_slug:
                    break
        if not live_slug:
            continue
        slug = live_slug
        meta = _robot_activity_meta(slug or current_raw, robots_by_slug, team_map)
        team_meta = _resolve_swarm_team_meta(swarm, team_map, teams_index)
        if team_meta["team_id"]:
            meta["team_id"] = team_meta["team_id"]
            meta["team_name"] = team_meta["team_name"]
        kanban_label = str(team_meta.get("team_name") or team_meta.get("team_id") or "").strip()
        if kanban_label:
            meta["kanban_label"] = kanban_label
        meta["swarm_name"] = name

        title = ""
        task_id = ""
        cards = list(agent_cards.get(slug or "") or agent_cards.get(current_raw) or [])
        if cards and isinstance(cards[0], dict):
            title = str(cards[0].get("title") or "").strip()
            task_id = str(cards[0].get("id") or "").strip()
        if not title:
            title = _title_from_swarm_objective(str(cp.get("objective") or ""), slug)
        if not title:
            cp_card_id, cp_card_title = _swarm_checkpoint_card_label(cp)
            if cp_card_title:
                title = cp_card_title
            if cp_card_id:
                task_id = cp_card_id
            if not title:
                title = f"Execução swarm {name}"
        if not task_id:
            task_id = _task_id_from_title(title)
        robot = robots_by_slug.get(_norm_slug(slug or current_raw)) or {}
        live_tid, live_title = _resolve_live_working_card(robot)
        if live_title:
            title = live_title
        if live_tid:
            task_id = live_tid

        exec_log = _swarm_checkpoint_execution_log(cp, name)
        progress = exec_log[-1] if exec_log else f"Agente {current_raw} em execução"

        items.append(
            {
                **meta,
                "id": task_id or f"{name}:{current_raw}",
                "task_id": task_id,
                "job_id": f"swarm-graph:{name}",
                "title": title,
                "column": "running",
                "source": "swarm_test",
                "started_at": cp.get("updated_at"),
                "finished_at": None,
                "progress": progress,
                "execution_log": exec_log,
                "is_live": True,
                "column_label": "Executando",
                "cancellable": _activity_cancellable(
                    source="swarm_test",
                    job_id=f"swarm-graph:{name}",
                    column="running",
                ),
            }
        )
    return items


def _robot_activity_meta(
    slug: str,
    robots_by_slug: dict[str, dict[str, Any]],
    team_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    robot = robots_by_slug.get(_norm_slug(slug)) or {}
    team = team_map.get(_norm_slug(slug)) or {}
    role_preset = str(robot.get("role_preset") or "").strip()
    role = str(robot.get("role") or "").strip()
    role_preset_label = str(robot.get("role_preset_label") or "").strip()
    profile_color = str(robot.get("color") or "").strip()
    if profile_color and not normalize_robot_color(profile_color):
        profile_color = normalize_robot_color(
            str(read_profile_meta_dict(slug).get("color") or "")
        )
    robot_name = _activity_agent_label(robot, slug)
    return {
        "robot_slug": slug,
        "robot_name": robot_name,
        "mascot": str(robot.get("mascot") or "robot").strip() or "robot",
        "role_preset": role_preset,
        "role_preset_label": role_preset_label,
        "role": role,
        "swarm_name": str(robot.get("swarm_name") or "").strip(),
        "color": resolve_robot_color(
            slug=slug,
            role_preset=role_preset,
            role=role,
            color=profile_color,
        ),
        "team_id": str(team.get("team_id") or "").strip(),
        "team_name": str(team.get("team_name") or "").strip(),
    }


def _enrich_activity_kanban_meta(
    row: dict[str, Any],
    *,
    task_board_index: Optional[dict[str, dict[str, Any]]] = None,
) -> None:
    """Fill team/kanban fields from card snapshot or board index when profile mapping is incomplete."""
    card = row.get("card") if isinstance(row.get("card"), dict) else {}
    board: Optional[dict[str, Any]] = None
    task_id = str(row.get("task_id") or "").strip()
    if task_board_index and task_id:
        board = task_board_index.get(task_id)

    def _pick(*sources: Any) -> str:
        for src in sources:
            text = str(src or "").strip()
            if text:
                return text
        return ""

    team_id = _pick(row.get("team_id"), card.get("team_id"), board and board.get("team_id"))
    team_name = _pick(
        row.get("team_name"),
        card.get("team_name"),
        board and board.get("team_name"),
    )
    project_label = _pick(
        row.get("project_label"),
        card.get("project_label"),
        board and board.get("project_label"),
        board and board.get("workdir"),
    )
    if team_id:
        row["team_id"] = team_id
    if team_name:
        row["team_name"] = team_name
    if project_label:
        row["project_label"] = project_label
    kanban_label = team_name or project_label or team_id
    if kanban_label:
        row["kanban_label"] = kanban_label


def _collect_executing_kanbans(
    live_items: list[dict[str, Any]],
    swarms: Optional[list[dict[str, Any]]] = None,
    *,
    team_map: Optional[dict[str, dict[str, str]]] = None,
    teams_index: Optional[dict[str, dict[str, str]]] = None,
    checkpoints_cache: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Distinct team/kanban boards with live execution for the robots UI."""
    rows: dict[str, dict[str, Any]] = {}

    def _upsert(
        team_id: str,
        team_name: str,
        *,
        swarm_name: str = "",
        source: str = "",
    ) -> None:
        tid = str(team_id or "").strip()
        tname = str(team_name or "").strip()
        if not tid and not tname:
            return
        key = tid or _norm_slug(tname)
        row = rows.get(key)
        if row is None:
            label = tname or tid
            row = {
                "team_id": tid,
                "team_name": tname,
                "kanban_label": label,
                "live_count": 0,
                "swarm_names": [],
                "sources": [],
            }
            rows[key] = row
        row["live_count"] = int(row.get("live_count") or 0) + 1
        if swarm_name:
            names = row.setdefault("swarm_names", [])
            if swarm_name not in names:
                names.append(swarm_name)
        if source:
            sources = row.setdefault("sources", [])
            if source not in sources:
                sources.append(source)

    for item in live_items:
        if not isinstance(item, dict):
            continue
        card = item.get("card") if isinstance(item.get("card"), dict) else {}
        tid = str(item.get("team_id") or card.get("team_id") or "").strip()
        tname = str(
            item.get("kanban_label")
            or item.get("team_name")
            or card.get("team_name")
            or card.get("project_label")
            or ""
        ).strip()
        if not tname and tid and teams_index:
            tname = str((teams_index.get(tid) or {}).get("team_name") or tid)
        swarm_name = str(item.get("swarm_name") or "").strip()
        source = str(item.get("source") or "").strip()
        _upsert(tid, tname, swarm_name=swarm_name, source=source)

    for swarm in swarms or []:
        if not isinstance(swarm, dict):
            continue
        name = str(swarm.get("name") or "").strip()
        if not name:
            continue
        if checkpoints_cache is not None:
            cp = checkpoints_cache.get(name)
        else:
            from .swarm_graph import load_checkpoint
            cp = load_checkpoint(name)
        if not cp or str(cp.get("status") or "") != "running":
            continue
        team_meta = _resolve_swarm_team_meta(
            swarm, team_map or {}, teams_index
        )
        tid = str(team_meta.get("team_id") or "").strip()
        already = any(
            isinstance(item, dict)
            and str(item.get("team_id") or "").strip() == tid
            and str(item.get("swarm_name") or "").strip() == name
            for item in live_items
        )
        if already:
            continue
        _upsert(
            tid,
            str(team_meta.get("team_name") or ""),
            swarm_name=name,
            source="swarm_test",
        )

    out = list(rows.values())
    out.sort(
        key=lambda r: str(
            r.get("team_name") or r.get("kanban_label") or r.get("team_id") or ""
        )
    )
    for row in out:
        row["swarm_names"] = sorted(row.get("swarm_names") or [])
        row["sources"] = sorted(row.get("sources") or [])
    return out


def _build_activity_board(
    monitor: GoisMonitor,
    robots: list[dict[str, Any]],
    boards: list[dict[str, Any]],
    team_map: dict[str, dict[str, str]],
    cron_snap: dict[str, Any],
    alias_map: Optional[dict[str, str]] = None,
    *,
    swarms: Optional[list[dict[str, Any]]] = None,
    teams_index: Optional[dict[str, dict[str, str]]] = None,
    now: Optional[float] = None,
    window_seconds: float = _ACTIVITY_WINDOW_SECONDS,
    title_index: Optional[dict[str, str]] = None,
    task_lookup: Optional[dict[str, dict[str, Any]]] = None,
    task_board_index: Optional[dict[str, dict[str, Any]]] = None,
    checkpoints_cache: Optional[dict[str, Any]] = None,
    recent_jobs: Optional[list] = None,
) -> dict[str, Any]:
    """Live robot↔card pairs and a 4h activity kanban for /swarm/robots."""
    now = time.time() if now is None else now
    if title_index is None:
        title_index = _build_task_title_index(boards)
    if task_lookup is None:
        task_lookup = _build_task_lookup(boards)
    if task_board_index is None:
        task_board_index = _build_task_board_index(boards)
    robots_by_slug = {
        _norm_slug(str(row.get("slug") or "")): row
        for row in robots
        if isinstance(row, dict) and row.get("slug")
    }
    items_map: dict[str, dict[str, Any]] = {}
    pq_queue: dict[str, Any] = {}
    pq_handler = getattr(monitor, "_priority_queue_handler", None)
    if pq_handler is not None:
        try:
            raw_queue = pq_handler.engine.get_queue()
            if isinstance(raw_queue, dict):
                pq_queue = raw_queue
        except Exception:
            pq_queue = {}

    def add_item(item: dict[str, Any], *, prefer_live: bool = False) -> None:
        if not item.get("title"):
            return
        if not item.get("is_live") and not _activity_within_window(
            started_at=item.get("started_at"),
            finished_at=item.get("finished_at"),
            now=now,
            window_seconds=window_seconds,
        ):
            return
        _upsert_activity_item(items_map, item, prefer_live=prefer_live)

    for item in _swarm_checkpoint_activity_items(
        swarms or [],
        robots_by_slug,
        team_map,
        alias_map,
        teams_index,
        checkpoints_cache=checkpoints_cache,
    ):
        add_item(item, prefer_live=True)

    for robot in robots:
        if not isinstance(robot, dict):
            continue
        slug = str(robot.get("slug") or "").strip()
        if not slug:
            continue
        state = str(robot.get("state") or "idle")
        if state not in {"running", "working"}:
            continue
        meta = _robot_activity_meta(slug, robots_by_slug, team_map)
        wc = robot.get("working_card") if isinstance(robot.get("working_card"), dict) else {}
        job = None
        running_jobs = robot.get("running_jobs") or []
        if isinstance(running_jobs, list) and running_jobs:
            job = running_jobs[0] if isinstance(running_jobs[0], dict) else None
        title = str(wc.get("title") or "").strip()
        task_id = str(wc.get("id") or "").strip()
        if not task_id:
            task_id = _task_id_from_title(title)
        started_at = None
        source = "kanban_card"
        progress = ""
        job_id = ""
        if job:
            title = title or str(job.get("name") or job.get("task_id") or "").strip()
            task_id = task_id or str(job.get("task_id") or "").strip()
            started_at = job.get("started_at")
            source = str(job.get("kind") or "job")
            progress = str(job.get("progress") or "").strip()
            job_id = str(job.get("job_id") or "").strip()
        if not job_id and task_id:
            active = _lookup_active_execution_for_task(task_id, pq_queue=pq_queue, recent_jobs=recent_jobs)
            if active:
                job_id = str(active.get("job_id") or "").strip()
                source = str(active.get("source") or source).strip().lower()
                if not started_at:
                    started_at = active.get("started_at")
                if not progress:
                    progress = str(active.get("progress") or "").strip()
        if not title:
            title = task_id or "Tarefa em andamento"
        column = "running" if state == "running" else "working"
        execution_log = robot.get("execution_log")
        if not isinstance(execution_log, list):
            execution_log = []
        execution_log = [str(x).strip() for x in execution_log if str(x).strip()]
        if not execution_log and job:
            execution_log = _execution_log_lines(job)
        live_task = task_lookup.get(task_id) if task_id else None
        live_board = task_board_index.get(task_id) if task_id else None
        live_card = (
            _task_card_snapshot(live_task, live_board)
            if isinstance(live_task, dict)
            else None
        )
        add_item(
            {
                **meta,
                "id": task_id or job_id or slug,
                "task_id": task_id,
                "job_id": job_id,
                "title": title,
                "column": column,
                "source": source,
                "started_at": started_at,
                "finished_at": None,
                "progress": progress,
                "execution_log": execution_log,
                "card": live_card,
                "is_live": True,
                "column_label": "Executando" if column == "running" else "Em foco",
                "cancellable": _activity_cancellable(
                    source=source, job_id=job_id, column=column
                ),
            },
            prefer_live=True,
        )

    _recent = recent_jobs if recent_jobs is not None else kanban_schedule_list_recent()
    for job in _recent:
        slug = _canonical_profile_slug(job.profile, alias_map) or _norm_slug(job.profile)
        meta = _robot_activity_meta(slug or job.profile, robots_by_slug, team_map)
        column = {
            "running": "running",
            "done": "done",
            "error": "error",
        }.get(str(job.status or ""), "error")
        ks_task_id = str(job.task_id or "").strip()
        ks_execution_log = [
            str(x).strip() for x in (job.progress or []) if str(x).strip()
        ]
        if not ks_execution_log:
            last = str(job.last_progress or "").strip()
            if last:
                ks_execution_log = [last]
        ks_task = task_lookup.get(ks_task_id) if ks_task_id else None
        ks_deliverable = _format_job_result_text(
            job.result,
            error=str(job.error or ""),
        )
        if not ks_deliverable and isinstance(ks_task, dict):
            ks_deliverable = _task_deliverable_text(ks_task)
        add_item(
            {
                **meta,
                "id": ks_task_id or str(job.id),
                "task_id": ks_task_id,
                "job_id": str(job.id or ""),
                "title": title_index.get(ks_task_id)
                or ks_task_id
                or str(job.id or "Card kanban"),
                "column": column,
                "source": "kanban_schedule",
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "progress": str(job.last_progress or "").strip(),
                "execution_log": ks_execution_log,
                "deliverable": ks_deliverable,
                "is_live": job.status == "running",
                "column_label": {
                    "running": "Executando",
                    "done": "Concluído",
                    "error": "Erro",
                }.get(column, column),
                "error": str(job.error or "").strip() or None,
                "cancellable": _activity_cancellable(
                    source="kanban_schedule",
                    job_id=str(job.id or ""),
                    column=column,
                ),
            },
            prefer_live=job.status == "running",
        )

    if isinstance(pq_queue, dict):
        bucket_map = {
            "running": ("running", True),
            "done": ("done", False),
            "errors": ("error", False),
        }
        for bucket, (column, live) in bucket_map.items():
            for row in pq_queue.get(bucket) or []:
                if not isinstance(row, dict):
                    continue
                slug = _canonical_profile_slug(
                    str(row.get("assignee") or ""), alias_map
                ) or _norm_slug(str(row.get("assignee") or ""))
                meta = _robot_activity_meta(
                    slug or str(row.get("assignee") or ""),
                    robots_by_slug,
                    team_map,
                )
                pq_task_id = str(row.get("task_id") or "").strip()
                pq_execution_log = [
                    str(x).strip() for x in (row.get("progress") or []) if str(x).strip()
                ]
                if not pq_execution_log:
                    last = str(row.get("last_progress") or "").strip()
                    if last:
                        pq_execution_log = [last]
                pq_deliverable = _format_job_result_text(
                    row.get("result"),
                    error=str(row.get("error") or ""),
                )
                if not pq_deliverable:
                    pq_deliverable = str(row.get("last_progress") or "").strip()
                pq_task = task_lookup.get(pq_task_id) if pq_task_id else None
                if not pq_deliverable and isinstance(pq_task, dict):
                    pq_deliverable = _task_deliverable_text(pq_task)
                add_item(
                    {
                        **meta,
                        "id": pq_task_id or str(row.get("id") or ""),
                        "task_id": pq_task_id,
                        "job_id": str(row.get("id") or ""),
                        "title": str(
                            row.get("title")
                            or title_index.get(pq_task_id)
                            or pq_task_id
                            or "Card"
                        ),
                        "column": column,
                        "source": "priority_queue",
                        "started_at": row.get("started_at"),
                        "finished_at": row.get("finished_at"),
                        "progress": str(row.get("last_progress") or "").strip(),
                        "execution_log": pq_execution_log,
                        "deliverable": pq_deliverable,
                        "is_live": live,
                        "column_label": {
                            "running": "Executando",
                            "done": "Concluído",
                            "error": "Erro",
                        }.get(column, column),
                        "error": str(row.get("error") or "").strip() or None,
                        "cancellable": _activity_cancellable(
                            source="priority_queue",
                            job_id=str(row.get("id") or ""),
                            column=column,
                        ),
                    },
                    prefer_live=live,
                )

    for job in cron_snap.get("running") or []:
        if not isinstance(job, dict):
            continue
        slug = _canonical_profile_slug(
            str(job.get("profile") or job.get("source_profile") or ""),
            alias_map,
        )
        meta = _robot_activity_meta(
            slug or str(job.get("profile") or ""),
            robots_by_slug,
            team_map,
        )
        task_id = str(job.get("task_id") or "").strip()
        cron_msg = str(job.get("last_message") or job.get("progress") or "").strip()
        cron_log = [cron_msg] if cron_msg else []
        add_item(
            {
                **meta,
                "id": task_id or str(job.get("job_id") or ""),
                "task_id": task_id,
                "job_id": str(job.get("job_id") or ""),
                "title": str(job.get("name") or task_id or "Cron Hermes"),
                "column": "running",
                "source": "cron",
                "started_at": job.get("started_at"),
                "finished_at": None,
                "progress": cron_msg,
                "execution_log": cron_log,
                "is_live": True,
                "column_label": "Executando",
            },
            prefer_live=True,
        )

    for job in cron_snap.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        last_status = str(job.get("last_status") or "").strip().lower()
        if last_status not in {"ok", "error", "done", "success"}:
            continue
        last_run = job.get("last_run_at")
        if not _activity_within_window(
            finished_at=last_run,
            started_at=last_run,
            now=now,
            window_seconds=window_seconds,
        ):
            continue
        slug = _canonical_profile_slug(
            str(job.get("profile") or job.get("source_profile") or ""),
            alias_map,
        )
        meta = _robot_activity_meta(
            slug or str(job.get("profile") or ""),
            robots_by_slug,
            team_map,
        )
        column = "error" if last_status == "error" else "done"
        cron_err = str(job.get("last_error") or "").strip()
        cron_msg = str(job.get("last_message") or "").strip()
        cron_deliverable = cron_err if column == "error" else cron_msg
        cron_log = [cron_msg or cron_err] if (cron_msg or cron_err) else []
        add_item(
            {
                **meta,
                "id": str(job.get("id") or job.get("name") or ""),
                "task_id": "",
                "job_id": str(job.get("id") or ""),
                "title": str(job.get("name") or job.get("id") or "Cron"),
                "column": column,
                "source": "cron",
                "started_at": last_run,
                "finished_at": last_run,
                "progress": cron_err or cron_msg,
                "execution_log": cron_log,
                "deliverable": cron_deliverable,
                "is_live": False,
                "column_label": "Concluído" if column == "done" else "Erro",
                "error": cron_err or None,
            }
        )

    for board in boards:
        team_id = str(board.get("team_id") or "").strip()
        team_name = str(board.get("team_name") or team_id).strip()
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            completed_at = task.get("completed_at")
            col = _norm_slug(task.get("column"))
            is_live_card = col in {"doing", "review"}
            if not is_live_card and not completed_at:
                continue
            if not is_live_card and not _activity_within_window(
                finished_at=completed_at,
                started_at=completed_at,
                now=now,
                window_seconds=window_seconds,
            ):
                continue
            task_id = str(task.get("id") or "").strip()
            assignees = _task_assignees(task, alias_map)
            slug = assignees[0] if assignees else ""
            if is_live_card and not slug:
                slug = _resolve_task_robot_slug(task_id, robots_by_slug, alias_map, recent_jobs=recent_jobs)
            meta = _robot_activity_meta(slug, robots_by_slug, team_map)
            if team_id:
                meta["team_id"] = team_id
                meta["team_name"] = team_name
            robot_row = robots_by_slug.get(_norm_slug(slug)) if slug else {}
            execution_log: list[str] = []
            if isinstance(robot_row, dict):
                raw_log = robot_row.get("execution_log")
                if isinstance(raw_log, list):
                    execution_log = [str(x).strip() for x in raw_log if str(x).strip()]
                if not execution_log:
                    running_jobs = robot_row.get("running_jobs") or []
                    if running_jobs and isinstance(running_jobs[0], dict):
                        execution_log = _execution_log_lines(running_jobs[0])
            column = "working" if is_live_card else "done"
            source = "kanban_card"
            job_id = ""
            progress = ""
            started_at_card = completed_at if is_live_card else None
            if is_live_card and isinstance(robot_row, dict):
                if str(robot_row.get("state") or "") == "running":
                    live_tid, _live_title = _resolve_live_working_card(robot_row)
                    if live_tid and live_tid == task_id:
                        column = "running"
            active_exec = (
                _lookup_active_execution_for_task(task_id, pq_queue=pq_queue, recent_jobs=recent_jobs)
                if is_live_card
                else None
            )
            if active_exec:
                column = str(active_exec.get("column") or "running")
                source = str(active_exec.get("source") or source)
                job_id = str(active_exec.get("job_id") or "")
                progress = str(active_exec.get("progress") or "")
                started_at_card = active_exec.get("started_at") or started_at_card
            deliverable = _task_deliverable_text(task) if not is_live_card else ""
            add_item(
                {
                    **meta,
                    "id": task_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "title": str(
                        task.get("title") or title_index.get(task_id) or task_id
                    ),
                    "column": column,
                    "source": source,
                    "started_at": started_at_card,
                    "finished_at": None if is_live_card else completed_at,
                    "progress": progress,
                    "execution_log": execution_log,
                    "deliverable": deliverable,
                    "card": _task_card_snapshot(task, board),
                    "is_live": is_live_card,
                    "column_label": (
                        "Executando"
                        if column == "running"
                        else ("Em foco" if is_live_card else "Concluído")
                    ),
                    "cancellable": _activity_cancellable(
                        source=source, job_id=job_id, column=column
                    ),
                },
                prefer_live=is_live_card,
            )

    items = list(items_map.values())
    for row in items:
        _enrich_activity_cancel_fields(row, pq_queue=pq_queue, recent_jobs=recent_jobs)
        tid = str(row.get("task_id") or "").strip()
        if tid:
            titled = title_index.get(tid)
            if titled and str(row.get("title") or "").strip() in {"", tid}:
                row["title"] = titled
            if not str(row.get("deliverable") or "").strip() and not row.get("is_live"):
                task = task_lookup.get(tid)
                if isinstance(task, dict):
                    row["deliverable"] = _task_deliverable_text(task)
            if not row.get("card"):
                task = task_lookup.get(tid)
                board = task_board_index.get(tid)
                if isinstance(task, dict):
                    row["card"] = _task_card_snapshot(task, board)
        _enrich_activity_kanban_meta(row, task_board_index=task_board_index)
    rank = {"running": 0, "working": 1, "done": 2, "error": 3}

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        ts = _parse_activity_ts(row.get("finished_at")) or _parse_activity_ts(
            row.get("started_at")
        )
        return (
            0 if row.get("is_live") else 1,
            rank.get(str(row.get("column") or ""), 9),
            -(ts or 0),
        )

    items.sort(key=sort_key)
    live = [row for row in items if row.get("is_live")]
    executing_kanbans = _collect_executing_kanbans(
        live,
        swarms,
        team_map=team_map,
        teams_index=teams_index,
        checkpoints_cache=checkpoints_cache,
    )
    columns = [
        {"id": "running", "title": "Executando"},
        {"id": "working", "title": "Em foco"},
        {"id": "done", "title": "Concluído"},
        {"id": "error", "title": "Erro"},
    ]
    return {
        "window_hours": int(window_seconds // 3600),
        "window_seconds": int(window_seconds),
        "updated_at": now,
        "live": live,
        "executing_kanbans": executing_kanbans,
        "columns": columns,
        "items": items,
        "summary": {
            "live": len(live),
            "total": len(items),
            "running": sum(1 for row in items if row.get("column") == "running"),
            "working": sum(1 for row in items if row.get("column") == "working"),
            "done": sum(1 for row in items if row.get("column") == "done"),
            "error": sum(1 for row in items if row.get("column") == "error"),
            "executing_kanbans": len(executing_kanbans),
        },
    }


def _minimal_activity_board(*, window_hours: int = 4) -> dict[str, Any]:
    """Empty activity kanban — used when nothing is live/recent (fast path)."""
    return {
        "window_hours": window_hours,
        "window_seconds": window_hours * 3600,
        "updated_at": time.time(),
        "live": [],
        "executing_kanbans": [],
        "columns": [
            {"id": "running", "title": "Executando"},
            {"id": "working", "title": "Em foco"},
            {"id": "done", "title": "Concluído"},
            {"id": "error", "title": "Erro"},
        ],
        "items": [],
        "summary": {
            "live": 0,
            "total": 0,
            "running": 0,
            "working": 0,
            "done": 0,
            "error": 0,
            "executing_kanbans": 0,
        },
    }


def _snapshot_needs_activity_board(
    robots: list[dict[str, Any]],
    cron_snap: dict[str, Any],
    recent_jobs: Optional[list[Any]],
) -> bool:
    """Skip the heavy activity scan when nothing ran recently."""
    if any(
        isinstance(row, dict) and row.get("state") in ("running", "working")
        for row in robots
    ):
        return True
    for job in (cron_snap or {}).get("running") or []:
        if isinstance(job, dict):
            return True
    now = time.time()
    cutoff = now - _ACTIVITY_WINDOW_SECONDS
    for job in recent_jobs or []:
        if getattr(job, "status", None) == "running":
            return True
        finished = getattr(job, "finished_at", None)
        if finished is not None and float(finished) >= cutoff:
            return True
    return False


def _compact_robot_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty list fields from /swarm/robots rows to shrink JSON payloads."""
    out = dict(row)
    for key in (
        "cards",
        "schedule_cards",
        "execution_log",
        "running_jobs",
        "errors",
        "handoff_to",
    ):
        val = out.get(key)
        if val in (None, [], ()):
            out.pop(key, None)
    if not out.get("working_card"):
        out.pop("working_card", None)
    return out


def _load_swarm_checkpoints_parallel(
    swarms: list[dict[str, Any]],
) -> dict[str, Any]:
    """Load graph checkpoints concurrently (one disk read per swarm)."""
    from .swarm_graph import load_checkpoint as _load_cp

    names = [
        str(sw.get("name") or "").strip()
        for sw in swarms
        if str(sw.get("name") or "").strip()
    ]
    if not names:
        return {}

    def _read(name: str) -> tuple[str, Any]:
        try:
            return name, _load_cp(name)
        except Exception:
            return name, None

    cache: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        for name, cp in pool.map(_read, names):
            cache[name] = cp
    return cache


def build_swarm_robots_meta(
    monitor: GoisMonitor,
    user: Any = None,
) -> dict[str, Any]:
    """Fast SSE preamble: swarms list without the heavy robot loop."""
    now = time.time()
    swarms = load_swarms_full()
    _team_map, teams_index = _load_teams_data(monitor, user)
    file_swarms: list[dict[str, Any]] = []
    for s in swarms:
        team_id = str(s.get("team_id") or "").strip()
        team_info = teams_index.get(team_id) or {}
        file_swarms.append(
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "topology": s.get("topology"),
                "entry_agent": s.get("entry_agent"),
                "agents_count": len(s.get("agents") or []),
                "team_id": team_id,
                "team_name": team_info.get("team_name") or "",
            }
        )
    return {
        "ok": True,
        "phase": "meta",
        "streaming": True,
        "updated_at": now,
        "swarms": file_swarms,
        "summary": {
            "robots": None,
            "swarms": len(file_swarms),
            "running": 0,
            "working": 0,
            "cards_assigned": 0,
            "streaming": True,
        },
    }


def build_swarm_robots_snapshot_light(
    monitor: GoisMonitor,
    user: Any = None,
) -> dict[str, Any]:
    """Minimal /swarm/robots payload for kanban assignee lookups (no kanban/cron scan)."""
    now = time.time()
    swarms = load_swarms_full()
    profiles_payload: dict[str, Any] = {"profiles": []}
    agent_create = getattr(monitor.cfg, "hermes_agent_create", None)
    if monitor.cfg.hermes and agent_create and agent_create.enabled:
        try:
            profiles_payload = monitor.handle_hermes_profiles_list(
                user, {"quick": "1"}
            )
        except Exception as exc:
            log.warning("swarm robots light: profiles failed: %s", exc)

    profiles_by_slug: dict[str, dict[str, Any]] = {}
    for row in profiles_payload.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        meta = _profile_meta(row)
        if meta["slug"]:
            profiles_by_slug[_norm_slug(meta["slug"])] = meta

    if monitor.cfg.hermes:
        disk_rows = getattr(monitor, "_hermes_profiles_cache_all", None) or []
        for row in disk_rows:
            if not isinstance(row, dict):
                continue
            meta = _profile_meta(row)
            key = _norm_slug(meta["slug"])
            if key and key not in profiles_by_slug:
                profiles_by_slug[key] = meta

    team_map, teams_index = _load_teams_data(monitor, user)
    alias_map = _build_assignee_alias_map(profiles_by_slug)
    swarm_index = _swarm_agent_index(
        swarms,
        profiles_by_slug=profiles_by_slug,
        alias_map=alias_map,
    )

    candidate_slugs: set[str] = set(swarm_index)
    for slug, meta in profiles_by_slug.items():
        if _is_swarmish(meta["slug"]) or _is_swarmish(meta.get("description", "")):
            candidate_slugs.add(slug)
    candidate_slugs.update(team_map)
    if not candidate_slugs and profiles_by_slug:
        candidate_slugs = set(profiles_by_slug)

    tombstones = load_robot_tombstones()
    if tombstones:
        candidate_slugs = {
            slug
            for slug in candidate_slugs
            if _norm_slug(slug) not in tombstones
            or _norm_slug(slug) in swarm_index
        }

    file_swarms: list[dict[str, Any]] = []
    for s in swarms:
        team_id = str(s.get("team_id") or "").strip()
        team_info = teams_index.get(team_id) or {}
        file_swarms.append(
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "topology": s.get("topology"),
                "entry_agent": s.get("entry_agent"),
                "agents_count": len(s.get("agents") or []),
                "team_id": team_id,
                "team_name": team_info.get("team_name") or "",
            }
        )

    robots: list[dict[str, Any]] = []
    for slug in sorted(candidate_slugs):
        profile = profiles_by_slug.get(slug, {})
        swarm_meta = _swarm_meta_for_profile(slug, swarm_index, team_map)
        robots.append(
            {
                "slug": slug,
                "display_name": profile.get("display_name") or slug,
                "mascot": profile.get("mascot") or "robot",
                "role": profile.get("description") or "",
                "swarm_name": swarm_meta.get("swarm_name") or "",
                "state": "idle",
            }
        )

    return {
        "ok": True,
        "light": True,
        "updated_at": now,
        "swarms": file_swarms,
        "summary": {
            "robots": len(robots),
            "swarms": len(file_swarms),
            "running": 0,
            "working": 0,
            "cards_assigned": 0,
        },
        "robots": robots,
        "activity": {"columns": [], "items": [], "summary": {"live": 0, "total": 0}},
        "memory": {"enabled": False, "backend": "noop", "timelines": {}},
        "quality": {"enabled": False, "reports": {}},
    }


def build_swarm_robots_snapshot(
    monitor: GoisMonitor,
    user: Any = None,
    *,
    include_panels: bool = False,
    light: bool = False,
    on_robots_batch: Optional[Callable[[list[dict[str, Any]], int, int], None]] = None,
    robots_batch_size: int = 8,
) -> dict[str, Any]:
    """Build visual swarm robots payload for /swarm/robots."""
    if light:
        return build_swarm_robots_snapshot_light(monitor, user)
    now = time.time()
    swarms = load_swarms_full()
    chat_cfg = getattr(monitor.cfg, "openclaw_chat", None)
    agent_cfg = getattr(monitor.cfg, "agent", None)
    model_fields_cache: dict[str, dict[str, str]] = {}

    def _cached_model_fields(slug: str) -> dict[str, str]:
        key = _norm_slug(slug)
        if key not in model_fields_cache:
            model_fields_cache[key] = model_fields_for_profile(
                slug,
                chat_cfg=chat_cfg,
                agent_cfg=agent_cfg,
            )
        return model_fields_cache[key]

    # Phase 1: fetch independent data sources in parallel.
    _do_profiles = monitor.cfg.hermes and monitor.cfg.hermes_agent_create.enabled
    _do_cron = bool(monitor.cfg.hermes)

    def _fetch_profiles() -> dict[str, Any]:
        try:
            return monitor.handle_hermes_profiles_list(user, {"quick": "1"})
        except Exception as exc:
            log.warning("swarm robots: profiles failed: %s", exc)
            return {"profiles": []}

    def _fetch_cron_snap() -> dict[str, Any]:
        try:
            return monitor._cached_hermes_cron_snapshot()
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=5) as _pool:
        _fut_profiles = _pool.submit(_fetch_profiles) if _do_profiles else None
        _fut_kanban = _pool.submit(_load_kanban_context, monitor, user)
        _fut_teams = _pool.submit(_load_teams_data, monitor, user)
        _fut_cron = _pool.submit(_fetch_cron_snap) if _do_cron else None
        _fut_recent = _pool.submit(kanban_schedule_list_recent)
        _fut_tombstones = _pool.submit(load_robot_tombstones)

        profiles_payload: dict[str, Any] = _fut_profiles.result() if _fut_profiles else {"profiles": []}
        boards, kanban_agents = _fut_kanban.result()
        team_map, teams_index = _fut_teams.result()
        cron_snap: dict[str, Any] = _fut_cron.result() if _fut_cron else {}
        recent_jobs = _fut_recent.result()
        tombstones = _fut_tombstones.result()

    profiles_by_slug: dict[str, dict[str, Any]] = {}
    for row in profiles_payload.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        meta = _profile_meta(row)
        if meta["slug"]:
            profiles_by_slug[_norm_slug(meta["slug"])] = meta

    if monitor.cfg.hermes:
        # Reuse the unfiltered filesystem list from handle_hermes_profiles_list
        # (kanban assignees may reference profiles outside the user filter).
        disk_rows = getattr(monitor, "_hermes_profiles_cache_all", None) or []
        for row in disk_rows:
            if not isinstance(row, dict):
                continue
            meta = _profile_meta(row)
            key = _norm_slug(meta["slug"])
            if key and key not in profiles_by_slug:
                profiles_by_slug[key] = meta

    alias_map = _build_assignee_alias_map(profiles_by_slug)
    (
        cards_by_assignee,
        open_cards_by_team,
        task_title_index,
        task_lookup,
        task_board_index,
    ) = _build_all_board_indices(boards, alias_map)
    swarm_index = _swarm_agent_index(
        swarms,
        profiles_by_slug=profiles_by_slug,
        alias_map=alias_map,
    )
    pq_by_profile = _priority_queue_rows(monitor, alias_map)

    running_by_profile = _running_by_profile(
        monitor,
        alias_map,
        pq_rows=pq_by_profile,
        cron_snap=cron_snap,
        recent_jobs=recent_jobs,
    )
    pq_errors_by_task = _priority_queue_errors_by_task(monitor)
    errors_by_profile = _errors_by_profile(cron_snap, alias_map, recent_jobs=recent_jobs)
    schedules_by_profile = _schedules_by_profile(cron_snap, alias_map)

    candidate_slugs: set[str] = set(swarm_index)
    for slug, meta in profiles_by_slug.items():
        if _is_swarmish(meta["slug"]) or _is_swarmish(meta.get("description", "")):
            candidate_slugs.add(slug)
    candidate_slugs.update(team_map)
    candidate_slugs.update(_kanban_assignee_slugs(boards, alias_map))
    candidate_slugs.update(_kanban_agent_slugs(kanban_agents))
    candidate_slugs.update(running_by_profile.keys())
    for slug in list(candidate_slugs):
        canonical = _resolve_assignee_slug(slug, alias_map) if alias_map else _profile_slug(slug)
        if canonical:
            candidate_slugs.add(canonical)

    if not candidate_slugs and profiles_by_slug:
        candidate_slugs = set(profiles_by_slug)

    # Robots explicitly deleted stay hidden even when the Hermes profile still
    # exists on disk; re-register in a swarm to show them again.
    if tombstones:
        candidate_slugs = {
            slug
            for slug in candidate_slugs
            if _norm_slug(slug) not in tombstones
            or _norm_slug(slug) in swarm_index
        }

    file_swarms = []
    for s in swarms:
        team_id = str(s.get("team_id") or "").strip()
        team_info = teams_index.get(team_id) or {}
        file_swarms.append(
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "topology": s.get("topology"),
                "entry_agent": s.get("entry_agent"),
                "agents_count": len(s.get("agents") or []),
                "hermes_profiles": s.get("hermes_profiles") or [],
                "agents": s.get("agents") or [],
                "graph": s.get("graph") or None,
                "last_test": _swarm_last_test(s),
                "editable": True,
                "team_id": team_id,
                "team_name": team_info.get("team_name") or "",
            }
        )
    known_swarm_names = {str(s.get("name") or "") for s in file_swarms}
    merged_swarms = list(file_swarms)
    for team_swarm in _team_swarm_entries(
        team_map, candidate_slugs, profiles_by_slug
    ):
        if team_swarm["name"] not in known_swarm_names:
            merged_swarms.append(team_swarm)

    swarms_by_name = {
        str(s.get("name") or ""): s
        for s in merged_swarms
        if str(s.get("name") or "").strip()
    }

    exec_backends_cache: dict[str, str] = {}

    robots: list[dict[str, Any]] = []
    slug_list = sorted(candidate_slugs)
    slug_total = len(slug_list)

    def _emit_robot_batch() -> None:
        if not on_robots_batch or robots_batch_size <= 0 or not robots:
            return
        batch_size = robots_batch_size
        if len(robots) % batch_size != 0:
            return
        offset = len(robots) - batch_size
        on_robots_batch(robots[offset:], offset, slug_total)

    for slug in slug_list:
        profile = profiles_by_slug.get(slug, {})
        swarm_meta = _swarm_meta_for_profile(slug, swarm_index, team_map)
        cards = _cards_for_assignee(
            slug, boards, alias_map, cards_index=cards_by_assignee
        )
        active_cards = [
            c for c in cards if _norm_slug(c.get("column")) in {"doing", "review"}
        ]
        working_card = _pick_working_card(active_cards, cron_snap, pq_errors_by_task)
        running = _enrich_running_jobs(
            running_by_profile.get(slug, []),
            boards,
            title_index=task_title_index,
        )
        execution_log = _execution_log_lines(running[0]) if running else []
        state = "idle"
        if running:
            state = "running"
        elif working_card:
            state = "working"
        pq_rows = pq_by_profile.get(slug, [])
        pq_queued = sum(1 for row in pq_rows if row.get("kind") == "priority_queue_queued")
        execution_pending = bool(working_card and not running)
        if execution_pending and not execution_log and working_card:
            wc_task_id = str(working_card.get("id") or "").strip()
            execution_log = _working_card_idle_hints(
                working_card,
                cron_snap,
                queue_queued=pq_queued,
                pq_error=pq_errors_by_task.get(wc_task_id),
            )
        profile_model = str(profile.get("model_id") or "").strip()
        if state == "idle" and profile_model:
            model_fields = {
                "modelId": profile_model,
                "modelLabel": profile_model.replace("-", " ").replace("_", " "),
            }
        else:
            model_fields = _cached_model_fields(slug)
        if state in ("running", "working"):
            exec_backend = exec_backends_cache.get(slug)
            if exec_backend is None:
                try:
                    exec_backend = read_profile_execution_backend(slug)
                except Exception:
                    exec_backend = "llm"
                exec_backends_cache[slug] = exec_backend
        else:
            exec_backend = "llm"
        robot_errors = errors_by_profile.get(slug, [])
        schedule_key = _resolve_assignee_slug(slug, alias_map) if alias_map else _profile_slug(slug)
        schedule = schedules_by_profile.get(schedule_key or slug) or _empty_schedule_summary()

        role_label = swarm_meta.get("role") or ""
        preset_label = str(swarm_meta.get("role_preset_label") or "")
        preset_category = str(swarm_meta.get("role_category") or "")
        # Older robots predate best-role assignment; infer only for non-idle rows.
        if not preset_label and state != "idle":
            inferred = match_role_preset(
                role_label,
                profile.get("description") or "",
                slug,
            )
            if inferred:
                preset_label = str(inferred.get("label") or "")
                preset_category = str(inferred.get("category") or "")

        team_info = _resolve_robot_team_info(
            slug,
            team_map=team_map,
            teams_index=teams_index,
            swarm_name=str(swarm_meta.get("swarm_name") or ""),
            swarms_by_name=swarms_by_name,
            cards=cards,
        )
        team_id = str(team_info.get("team_id") or "").strip()
        schedule_cards = list(open_cards_by_team.get(team_id) or [])
        role_preset_id = str(swarm_meta.get("role_preset") or "").strip()
        robot_color = resolve_robot_color(
            slug=slug,
            role_preset=role_preset_id,
            role=role_label,
            color=str(profile.get("color") or ""),
        )
        robot_row = {
                "slug": slug,
                "display_name": profile.get("display_name") or slug,
                "mascot": profile.get("mascot") or "robot",
                "role": role_label,
                "role_preset": role_preset_id,
                "role_preset_label": preset_label,
                "role_category": preset_category,
                "color": robot_color,
                "description": profile.get("description") or "",
                "model_id": model_fields.get("modelId") or profile.get("model_id") or "",
                "model_label": model_fields.get("modelLabel") or "",
                "execution_backend": exec_backend,
                "execution_backend_label": execution_backend_label(exec_backend),
                "swarm_name": swarm_meta.get("swarm_name") or "",
                "swarm_description": swarm_meta.get("swarm_description") or "",
                "topology": swarm_meta.get("topology") or "",
                "is_entry": bool(swarm_meta.get("is_entry")),
                "is_orchestrator": bool(swarm_meta.get("is_entry")),
                "handoff_to": list(swarm_meta.get("handoff_to") or []),
                "team_id": team_info.get("team_id") or "",
                "team_name": team_info.get("team_name") or "",
                "state": state,
                "execution_pending": execution_pending,
                "running_jobs": running,
                "execution_log": execution_log,
                "cards_total": len(cards),
                "cards": cards,
                "schedule_cards": schedule_cards,
                "working_card": working_card,
                "errors": robot_errors,
                "errors_total": len(robot_errors),
                "schedule": schedule,
            }
        robot_row["pre_start_brief"] = _robot_pre_start_brief(
            robot_row,
            execution_log=execution_log if execution_pending or state == "working" else None,
        )
        robots.append(robot_row)
        _emit_robot_batch()

    if on_robots_batch and robots_batch_size > 0 and robots:
        remainder = len(robots) % robots_batch_size
        if remainder:
            on_robots_batch(robots[-remainder:], len(robots) - remainder, slug_total)

    _assign_virtual_swarms(
        robots, merged_swarms, known_swarm_names, profiles_by_slug, team_map
    )
    _apply_orchestrator_flags(robots, merged_swarms, profiles_by_slug, alias_map)

    checkpoints_cache = _load_swarm_checkpoints_parallel(merged_swarms)

    _apply_swarm_checkpoint_states(
        robots, merged_swarms, alias_map, checkpoints_cache=checkpoints_cache
    )

    robots.sort(
        key=lambda r: (
            0 if r.get("state") == "running" else 1,
            0 if r.get("state") == "working" else 1,
            0 if r.get("is_entry") else 1,
            str(r.get("swarm_name") or ""),
            str(r.get("display_name") or r.get("slug") or ""),
        )
    )
    robots = [_compact_robot_row(r) for r in robots]

    memory_timelines = (
        _swarm_memory_timelines(monitor, merged_swarms) if include_panels else None
    )
    quality = (
        _swarm_quality_reports(monitor, merged_swarms) if include_panels else None
    )
    if _snapshot_needs_activity_board(robots, cron_snap, recent_jobs):
        activity = _build_activity_board(
            monitor,
            robots,
            boards,
            team_map,
            cron_snap,
            alias_map,
            swarms=merged_swarms,
            teams_index=teams_index,
            now=now,
            title_index=task_title_index,
            task_lookup=task_lookup,
            task_board_index=task_board_index,
            checkpoints_cache=checkpoints_cache,
            recent_jobs=recent_jobs,
        )
    else:
        activity = _minimal_activity_board()

    return {
        "ok": True,
        "updated_at": now,
        "swarms": merged_swarms,
        "summary": {
            "robots": len(robots),
            "swarms": len(merged_swarms),
            "running": sum(1 for r in robots if r.get("state") == "running"),
            "working": sum(1 for r in robots if r.get("state") == "working"),
            "cards_assigned": sum(r.get("cards_total") or 0 for r in robots),
            "activity_live": activity.get("summary", {}).get("live", 0),
            "activity_recent": activity.get("summary", {}).get("total", 0),
        },
        "robots": robots,
        "activity": activity,
        "memory": memory_timelines
        or {"enabled": False, "backend": "noop", "timelines": {}},
        "quality": quality or {"enabled": False, "reports": {}},
        "execution_backends": list_execution_backends(),
    }


def _swarm_quality_reports(
    monitor: GoisMonitor, swarms: list[dict[str, Any]], *, limit: int = 20
) -> dict[str, Any]:
    """Phase 3: per-swarm evaluation trend for the /swarm/robots panel."""
    cfg = getattr(monitor.cfg, "swarm_eval", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return {"enabled": False, "reports": {}}
    try:
        from .swarm_eval import load_eval_history
    except Exception as exc:
        log.debug("swarm robots: eval history skipped: %s", exc)
        return {"enabled": False, "reports": {}}

    store_dir = getattr(cfg, "store_dir", "./.stack/swarms")
    reports: dict[str, Any] = {}
    for s in swarms:
        name = str(s.get("name") or "")
        if not name:
            continue
        try:
            hist = load_eval_history(name, store_dir=store_dir, limit=limit)
        except Exception:
            hist = []
        if not hist:
            continue
        latest = hist[-1]
        reports[name] = {
            "latest": latest,
            "trend": [
                {"at": h.get("evaluated_at"), "score": h.get("overall_score")}
                for h in hist
            ],
            "runs": len(hist),
        }
    return {
        "enabled": True,
        "threshold": getattr(cfg, "threshold", 70.0),
        "reports": reports,
    }


def _swarm_memory_timelines(
    monitor: GoisMonitor, swarms: list[dict[str, Any]], *, limit: int = 50
) -> dict[str, Any]:
    """Phase 2: per-swarm blackboard timeline for the /swarm/robots panel."""
    cfg = getattr(monitor.cfg, "swarm_memory", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return {"enabled": False, "backend": "noop", "timelines": {}}
    try:
        from .swarm_memory import build_swarm_memory

        mem = build_swarm_memory(cfg)
    except Exception as exc:
        log.debug("swarm robots: memory build skipped: %s", exc)
        return {"enabled": False, "backend": "noop", "timelines": {}}

    timelines: dict[str, list[dict[str, Any]]] = {}
    for s in swarms:
        name = str(s.get("name") or "")
        if not name:
            continue
        try:
            entries = mem.timeline(name)[-limit:]
        except Exception:
            entries = []
        if entries:
            timelines[name] = [
                {
                    "author": e.author,
                    "role": e.role,
                    "content": e.content,
                    "ts": e.ts,
                }
                for e in entries
            ]
    return {
        "enabled": True,
        "backend": getattr(mem, "backend", "local"),
        "timelines": timelines,
    }


def _swarm_robot_soul(display_name: str, role: str, instructions: str) -> str:
    lines = [f"# {display_name}", "", "Agente do swarm gois."]
    if role:
        lines.extend(["", f"## Papel", role])
    if instructions:
        lines.extend(["", "## Instruções", instructions])
    lines.extend(
        [
            "",
            "## Kanban",
            "Trabalhe nos cards atribuídos a este perfil. "
            "Atualize status, peça revisão e documente resultados.",
        ]
    )
    return "\n".join(lines)


def _build_swarm_robot_spec(
    *,
    display_name: str,
    role: str,
    text: str,
    role_preset: Optional[str],
    agent_cfg: Any,
) -> AgentSpec:
    pid = str(role_preset or "").strip()
    if pid and pid in _PRESET_BY_ID:
        spec = preset_agent_spec(_PRESET_BY_ID[pid])
        slug = _normalize_name(display_name or spec.name)
        soul = spec.soul
        if text and text.strip() and text.strip().lower() != role.lower():
            soul = _swarm_robot_soul(display_name, role or spec.description, text)
        return AgentSpec(
            name=slug,
            description=role or spec.description or display_name,
            soul=soul,
        )
    slug = _normalize_name(display_name or role or "swarm-robot")
    return AgentSpec(
        name=slug,
        description=role or display_name,
        soul=_swarm_robot_soul(display_name, role, text),
    )


def is_profile_assign_payload(payload: dict[str, Any]) -> bool:
    """True when the payload only links an existing profile to a swarm."""
    edit_keys = (
        "display_name",
        "role",
        "model_id",
        "execution_backend",
        "mascot",
        "color",
        "text",
        "instructions",
        "schedule",
        "cron_schedule",
        "skills",
    )
    if any(payload.get(k) is not None for k in edit_keys):
        return False
    return payload.get("swarm_name") is not None


def _profile_exists_on_disk(profile_slug: str) -> bool:
    return bool(_profile_dir_candidates(profile_slug))


def _profile_is_live(
    profile_slug: str,
    *,
    known_slugs: Optional[set[str]] = None,
) -> bool:
    """True when a Hermes profile still exists and was not tombstoned as deleted."""
    slug = _norm_slug(str(profile_slug or ""))
    if not slug or slug == "default":
        return False
    if slug in load_robot_tombstones():
        return False
    if known_slugs is not None:
        normalized = {_norm_slug(s) for s in known_slugs if _norm_slug(s)}
        if slug in normalized:
            return True
    return _profile_exists_on_disk(slug)


def _resolve_live_executor_slug(
    slug: str,
    *,
    fallbacks: Optional[Iterable[str]] = None,
    known_slugs: Optional[set[str]] = None,
) -> str:
    """Return the first candidate slug that maps to a live profile, else empty."""
    seen: set[str] = set()
    for raw in (slug, *(fallbacks or ())):
        key = _canonical_profile_slug(str(raw or ""), None) or _norm_slug(str(raw or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        if _profile_is_live(key, known_slugs=known_slugs):
            return key
    return ""


def _filter_live_profile_slugs(slugs: Iterable[str]) -> list[str]:
    """Keep only profile slugs that still exist on disk."""
    out: list[str] = []
    for raw in slugs:
        slug = str(raw or "").strip()
        if not slug or slug in out:
            continue
        if _profile_is_live(slug):
            out.append(slug)
    return out


def assign_profile_to_swarm(
    monitor: GoisMonitor,
    profile_slug: str,
    swarm_name: str,
    *,
    user: Any = None,
) -> dict[str, Any]:
    """Register an existing Hermes profile as a swarm robot (no profile creation)."""
    if not monitor.cfg.hermes:
        return {"ok": False, "error": "hermes is not configured"}

    slug = _norm_slug(profile_slug)
    if not slug:
        return {"ok": False, "error": "profile_slug is required"}
    if slug == "default":
        return {"ok": False, "error": "cannot assign the default profile"}

    if not _profile_exists_on_disk(slug):
        return {
            "ok": False,
            "error": f"perfil Hermes '{slug}' não encontrado no disco",
        }

    target_swarm = str(swarm_name or "swarm-manual").strip() or "swarm-manual"
    chat_cfg = monitor.cfg.openclaw_chat if monitor.cfg.openclaw_chat.enabled else None
    display_name = read_profile_display_name(slug) or slug.replace("-", " ").replace("_", " ")
    model_id = read_profile_model_default(slug) or ""
    instructions = _read_profile_soul(slug)

    register_robot_in_swarm(
        target_swarm,
        slug,
        instructions=instructions,
        model_id=model_id,
        display_name=display_name,
    )
    clear_robot_tombstone(slug)

    actor = monitor._accounts_actor(user)
    if actor is not None:
        teams = monitor.accounts.list_teams(actor.id)
        if len(teams) == 1:
            try:
                monitor.accounts.add_team_profile(teams[0].id, actor.id, slug)
            except Exception:
                pass

    monitor._invalidate_hermes_profiles_cache()
    model_fields = model_fields_for_profile(
        slug,
        chat_cfg=chat_cfg,
        agent_cfg=getattr(monitor.cfg, "agent", None),
    )
    return {
        "ok": True,
        "name": slug,
        "display_name": display_name,
        "swarm_name": target_swarm,
        "model_id": model_fields.get("modelId") or model_id,
        "model_label": model_fields.get("modelLabel") or "",
        "assigned": True,
    }


def create_swarm_robot(
    monitor: GoisMonitor,
    payload: dict[str, Any],
    user: Any = None,
) -> dict[str, Any]:
    """Create a Hermes profile robot with its own LLM and register it in a swarm."""
    existing_slug = str(
        payload.get("profile_slug") or payload.get("existing_profile") or ""
    ).strip()
    if existing_slug:
        swarm_name = str(payload.get("swarm_name") or "swarm-manual").strip() or "swarm-manual"
        result = assign_profile_to_swarm(
            monitor,
            existing_slug,
            swarm_name,
            user=user,
        )
        if result.get("ok"):
            monitor._invalidate_swarm_robots_cache()
        return result

    if not monitor.cfg.hermes or not monitor.cfg.hermes_agent_create.enabled:
        return {"ok": False, "error": "hermes agent create is disabled"}

    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        return {"ok": False, "error": "display_name is required"}

    execution_backend = normalize_execution_backend(
        payload.get("execution_backend") or payload.get("code_runner") or "llm"
    )
    model_id = str(payload.get("model_id") or "").strip()
    if not model_id and not is_ide_execution_backend(execution_backend):
        return {"ok": False, "error": "model_id is required for agentes LLM"}
    if not model_id:
        model_id = str(getattr(monitor.cfg.agent, "model", "") or "deepseek-chat").strip()

    role = str(payload.get("role") or "").strip()
    text = str(payload.get("text") or payload.get("instructions") or "").strip()
    role_preset = str(payload.get("role_preset") or "").strip() or None
    mascot = normalize_mascot(str(payload.get("mascot") or "robot"))
    swarm_name = str(payload.get("swarm_name") or "swarm-manual").strip() or "swarm-manual"

    # Auto-assign the best-fitting role when the caller didn't pick one, so the
    # robot gets a real, functional profile instead of a generic stub.
    matched_preset: Optional[dict[str, Any]] = None
    if not role_preset:
        matched_preset = match_role_preset(role, text, display_name)
        if matched_preset:
            role_preset = str(matched_preset.get("id") or "") or None
    preset_label = str(matched_preset.get("label") or "") if matched_preset else ""
    preset_category = str(matched_preset.get("category") or "") if matched_preset else ""

    if not text and not role and not role_preset:
        return {"ok": False, "error": "informe role, instructions ou role_preset"}

    if not text:
        text = f"Crie um agente swarm chamado {display_name}"
        if role:
            text += f" com o papel: {role}."
        text += " Trabalhe de forma autônoma em cards Kanban atribuídos a este perfil."

    create_cfg = monitor.cfg.hermes_agent_create
    chat_cfg = monitor.cfg.openclaw_chat if monitor.cfg.openclaw_chat.enabled else None
    spec = _build_swarm_robot_spec(
        display_name=display_name,
        role=role,
        text=text,
        role_preset=role_preset,
        agent_cfg=monitor.cfg.agent,
    )
    profile_meta: dict[str, Any] = {
        "display_name": display_name,
        "mascot": mascot,
        "execution_backend": execution_backend,
    }
    custom_color = normalize_robot_color(str(payload.get("color") or ""))
    if custom_color:
        profile_meta["color"] = custom_color
    template_profile = create_cfg.seed_role_catalog_template_profile

    profile_result: dict[str, Any]
    try:
        profile_result = create_hermes_profile_filesystem(
            spec,
            profile_meta=profile_meta,
            template_profile=template_profile,
            model_id=model_id,
            chat_cfg=chat_cfg,
        )
    except Exception as fs_err:
        log.warning("swarm robot filesystem create failed: %s; trying API", fs_err)
        dashboard_url = monitor._hermes_dashboard_url()
        if not dashboard_url:
            return {"ok": False, "error": f"{type(fs_err).__name__}: {fs_err}"}
        profile_result = create_hermes_agent(
            dashboard_url,
            spec,
            clone_from_default=create_cfg.clone_from_default,
            profile_meta=profile_meta,
            timeout=create_cfg.dashboard_api_timeout_seconds,
        )
        if not write_profile_model_default(spec.name, model_id, chat_cfg=chat_cfg):
            profile_result["model_warning"] = (
                f"perfil criado, mas model {model_id} não foi gravado"
            )

    profile_name = str(profile_result.get("name") or "").strip()
    if not profile_name:
        return {"ok": False, "error": "profile creation failed"}

    skills_raw = payload.get("skills")
    if isinstance(skills_raw, list):
        skills = [str(s).strip() for s in skills_raw if str(s).strip()]
        if skills:
            write_profile_skills(profile_name, skills)
            soul_text = _read_profile_soul(profile_name)
            if soul_text.strip():
                _write_profile_soul(profile_name, _sync_soul_skills(soul_text, skills))

    if profile_result.get("mode") != "filesystem" and chat_cfg:
        if not write_profile_model_default(profile_name, model_id, chat_cfg=chat_cfg):
            profile_result["model_warning"] = (
                f"perfil criado, mas model {model_id} não foi gravado"
            )

    register_robot_in_swarm(
        swarm_name,
        profile_name,
        role=role or spec.description[:120],
        instructions=text,
        model_id=model_id,
        mascot=mascot,
        display_name=display_name,
        role_preset=role_preset or "",
        role_preset_label=preset_label,
        role_category=preset_category,
    )
    clear_robot_tombstone(profile_name)

    cron_result: dict[str, Any] | None = None
    explicit_schedule = str(
        payload.get("schedule") or payload.get("cron_schedule") or ""
    ).strip()
    linked_teams = _teams_for_profile_slug(monitor, profile_name, user)
    team_kanban_meta: dict[str, Any] | None = None
    if linked_teams:
        try:
            team_kanban_meta = ensure_swarm_team_kanban_ready(
                monitor, linked_teams[0].id, user
            )
        except Exception as exc:
            log.debug("swarm robot: kanban ready for team %s: %s", linked_teams[0].id, exc)
    if (create_cfg.schedule_enabled or explicit_schedule) and linked_teams:
        team_workdir = str(
            (team_kanban_meta or {}).get("workdir")
            or monitor.accounts.team_workdir(linked_teams[0])
        )
        from .openai_swarm import (
            SwarmAgentSpec,
            SwarmSpec,
            build_swarm_dev_spec,
        )

        agent_stub = SwarmAgentSpec(
            name=profile_name,
            role=role or spec.description,
            instructions=text,
        )
        swarm_stub = SwarmSpec(
            name=swarm_name,
            description=f"Swarm {swarm_name}",
            agents=[agent_stub],
            entry_agent=profile_name,
        )
        dev_spec = build_swarm_dev_spec(
            agent_stub,
            swarm_stub,
            create_cfg=create_cfg,
            workdir=team_workdir or None,
            schedule=create_cfg.default_schedule,
        )
        from .hermes_cron import create_hermes_cron_job

        scheduled = explicit_schedule or create_cfg.default_schedule
        stagger = getattr(monitor, "_stagger_schedule_for_new_job", None)
        if callable(stagger):
            try:
                scheduled = stagger(scheduled) or scheduled
            except Exception:
                pass
        cron_result = create_hermes_cron_job(
            scheduled,
            dev_spec.requirement_prompt,
            name=dev_spec.description[:80] or profile_name,
            profile=profile_name,
            skills=dev_spec.skills,
            workdir=team_workdir or None,
            accept_hooks=create_cfg.cron_accept_hooks,
            timeout_seconds=create_cfg.cron_timeout_seconds,
        )
        if cron_result and not cron_result.get("ok"):
            profile_result["cron_error"] = cron_result.get("reason") or cron_result.get(
                "summary"
            )
        else:
            from .agent_role_skills import persist_swarm_profile_fields

            persist_swarm_profile_fields(
                profile_name,
                workdir=team_workdir or None,
                skills=dev_spec.skills,
                role_preset=role_preset or "",
                role_preset_label=preset_label,
                swarm_name=swarm_name,
            )
    elif create_cfg.schedule_enabled or explicit_schedule:
        profile_result["cron_skipped"] = _SCHEDULE_TEAM_REQUIRED_MSG

    actor = monitor._accounts_actor(user)
    if actor is not None:
        teams = monitor.accounts.list_teams(actor.id)
        if len(teams) == 1:
            try:
                monitor.accounts.add_team_profile(teams[0].id, actor.id, profile_name)
            except Exception:
                pass

    model_fields = model_fields_for_profile(
        profile_name,
        chat_cfg=chat_cfg,
        agent_cfg=monitor.cfg.agent,
    )

    out: dict[str, Any] = {
        "ok": True,
        "name": profile_name,
        "display_name": display_name,
        "mascot": mascot,
        "role": role,
        "role_preset": role_preset or "",
        "role_preset_label": preset_label,
        "role_category": preset_category,
        "color": resolve_robot_color(
            slug=profile_name,
            role_preset=role_preset or "",
            role=role,
            color=custom_color,
        ),
        "custom_color": custom_color,
        "model_id": model_fields.get("modelId") or model_id,
        "model_label": model_fields.get("modelLabel") or model_id,
        "execution_backend": execution_backend,
        "execution_backend_label": execution_backend_label(execution_backend),
        "swarm_name": swarm_name,
        "description": spec.description,
        "profile": profile_result,
    }
    if team_kanban_meta and team_kanban_meta.get("ok"):
        out["team_kanban"] = {
            "team_id": team_kanban_meta.get("team_id"),
            "tasks": team_kanban_meta.get("tasks", 0),
            "repaired": team_kanban_meta.get("kanban_repaired", False),
        }
    return out


def _read_profile_soul(profile_name: str, *, max_chars: int = 12000) -> str:
    for base in _profile_dir_candidates(profile_name):
        soul_path = base / "SOUL.md"
        if not soul_path.is_file():
            continue
        try:
            text = soul_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > max_chars:
            return text[:max_chars]
        return text
    return ""


def _sync_soul_skills(soul: str, skills: list[str]) -> str:
    base = re.sub(r"\n\n## Hermes skills.*", "", soul or "", flags=re.DOTALL).strip()
    if not skills:
        return base
    return _append_skills_to_soul(base, skills)


def _swarm_name_for_profile(slug: str) -> str:
    from .openai_swarm import _find_swarm_files_for_profile

    profile_key = _norm_slug(slug)
    if not profile_key:
        return ""
    for state_file, data in _find_swarm_files_for_profile(str(slug)):
        profiles = data.get("hermes_profiles") or []
        if any(_norm_slug(str(p)) == profile_key for p in profiles):
            return str(data.get("name") or state_file.stem)
        agents = [a for a in (data.get("agents") or []) if isinstance(a, dict)]
        if any(_norm_slug(str(a.get("name") or "")) == profile_key for a in agents):
            return str(data.get("name") or state_file.stem)
    return ""


def _robot_detail_from_disk(profile_slug: str) -> dict[str, Any]:
    meta = read_profile_meta_dict(profile_slug)
    display_name = (
        str(meta.get("display_name") or "").strip()
        or read_profile_display_name(profile_slug)
        or profile_slug.replace("-", " ").replace("_", " ")
    )
    custom_color = normalize_robot_color(str(meta.get("color") or ""))
    return {
        "slug": profile_slug,
        "display_name": display_name,
        "role": str(meta.get("description") or "").strip(),
        "mascot": str(meta.get("mascot") or "robot"),
        "color": resolve_robot_color(
            slug=profile_slug,
            role=str(meta.get("description") or "").strip(),
            color=custom_color,
        ),
        "custom_color": custom_color,
        "model_id": read_profile_model_default(profile_slug) or "",
        "execution_backend": read_profile_execution_backend(profile_slug),
        "execution_backend_label": execution_backend_label(
            read_profile_execution_backend(profile_slug)
        ),
        "swarm_name": _swarm_name_for_profile(profile_slug),
        "instructions": _read_profile_soul(profile_slug),
        "skills": read_profile_skills(profile_slug),
        "deletable": profile_slug != "default",
        "profile_only": True,
    }


def _write_profile_soul(profile_name: str, text: str) -> bool:
    dirs = _profile_dir_candidates(profile_name)
    if not dirs:
        return False
    soul_path = dirs[0] / "SOUL.md"
    try:
        soul_path.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def _profile_dir_candidates(profile_name: str) -> list[Path]:
    """Resolve on-disk profile directories for a slug (handles spacing/case)."""
    slug = str(profile_name or "").strip()
    norm = _norm_slug(slug)
    if not norm:
        return []
    roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in (
        hermes_profiles_root().expanduser(),
        Path.home() / ".hermes" / "profiles",
    ):
        key = str(root)
        if key in seen_roots or not root.is_dir():
            continue
        seen_roots.add(key)
        roots.append(root)
    try:
        from .local_paths import project_stack_root

        stack_root = project_stack_root() / "hermes" / "profiles"
        key = str(stack_root)
        if stack_root.is_dir() and key not in seen_roots:
            seen_roots.add(key)
            roots.append(stack_root)
    except Exception:
        pass

    hits: list[Path] = []
    seen_dirs: set[str] = set()
    for root in roots:
        for name in (slug, norm):
            candidate = root / name
            key = str(candidate)
            if candidate.is_dir() and key not in seen_dirs:
                seen_dirs.add(key)
                hits.append(candidate)
        for path in root.iterdir():
            if not path.is_dir() or path.name.startswith("."):
                continue
            if _norm_slug(path.name) != norm:
                continue
            key = str(path)
            if key not in seen_dirs:
                seen_dirs.add(key)
                hits.append(path)
    return hits




def _delete_profile_filesystem(profile_name: str) -> bool:
    name = str(profile_name or "").strip()
    if not name or name.lower() == "default":
        return False
    deleted_any = False
    for target in _profile_dir_candidates(name):
        try:
            shutil.rmtree(target)
        except OSError as exc:
            log.warning("swarm robot: failed to delete profile dir %s: %s", target, exc)
            continue
        if not target.exists():
            deleted_any = True
    return deleted_any


def _unlink_profile_from_all_teams(
    monitor: GoisMonitor,
    profile_slug: str,
) -> list[str]:
    try:
        return monitor.accounts.remove_profile_from_all_teams(profile_slug)
    except Exception as exc:
        log.warning("swarm robot: team unlink failed for %s: %s", profile_slug, exc)
        return []


_ROBOT_SOURCE_EXPLANATIONS: dict[str, str] = {
    "SOUL.md": (
        "Instruções principais do agente — personalidade, papel, regras de "
        "trabalho e comportamento no Kanban."
    ),
    "profile.yaml": (
        "Metadados do perfil Hermes: nome exibido, mascote, descrição e flags."
    ),
    "config.yaml": (
        "Configuração do LLM (modelo padrão e provider) usado pelo robô."
    ),
    "AGENTS.md": "Definição de sub-agentes, handoffs e coordenação interna.",
    "TOOLS.md": "Ferramentas MCP e skills permitidas para este perfil.",
    "SYSTEM.md": "Prompt de sistema adicional injetado nas sessões Hermes.",
    "PROMPT.md": "Prompt base alternativo ou complementar ao SOUL.md.",
    "prompt.md": "Prompt base alternativo ou complementar ao SOUL.md.",
    "README.md": "Documentação local do perfil e notas de manutenção.",
    "cron/jobs.json": (
        "Crons Hermes vinculados a este perfil — agenda e prompts de execução."
    ),
    "swarm-registry": (
        "Registro do enxame (JSON): papel, instruções, handoffs e metadados "
        "salvos no state file do swarm."
    ),
}

_ROBOT_SOURCE_PRIORITY = (
    "SOUL.md",
    "profile.yaml",
    "config.yaml",
    "AGENTS.md",
    "TOOLS.md",
    "SYSTEM.md",
    "PROMPT.md",
    "prompt.md",
    "README.md",
    "cron/jobs.json",
)

_SOURCE_LANG_BY_EXT = {
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".py": "python",
    ".sh": "bash",
    ".txt": "text",
}


def _source_language(path: Path) -> str:
    return _SOURCE_LANG_BY_EXT.get(path.suffix.lower(), "text")


def _read_text_file(path: Path, *, max_chars: int = 120_000) -> tuple[str, bool]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    if len(raw) > max_chars:
        return raw[:max_chars], True
    return raw, False


def _profile_source_entries(profile_dir: Path) -> list[dict[str, Any]]:
    """Collect readable profile files with human-readable explanations."""
    if not profile_dir.is_dir():
        return []

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []

    def _add(rel_name: str, path: Path) -> None:
        key = rel_name.replace("\\", "/")
        if key in seen or not path.is_file():
            return
        try:
            if path.stat().st_size > 400_000:
                return
        except OSError:
            return
        content, truncated = _read_text_file(path)
        if not content.strip():
            return
        seen.add(key)
        entries.append(
            {
                "name": path.name,
                "path": key,
                "absolute_path": str(path),
                "description": _ROBOT_SOURCE_EXPLANATIONS.get(
                    key,
                    _ROBOT_SOURCE_EXPLANATIONS.get(path.name, "Arquivo do perfil Hermes."),
                ),
                "language": _source_language(path),
                "content": content,
                "truncated": truncated,
            }
        )

    for rel in _ROBOT_SOURCE_PRIORITY:
        _add(rel, profile_dir / rel)

    for path in sorted(profile_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".md", ".yaml", ".yml", ".json", ".txt"}:
            continue
        _add(path.name, path)

    cron_jobs = profile_dir / "cron" / "jobs.json"
    if cron_jobs.is_file():
        _add("cron/jobs.json", cron_jobs)

    order = {name: idx for idx, name in enumerate(_ROBOT_SOURCE_PRIORITY)}
    entries.sort(
        key=lambda row: (
            order.get(str(row.get("path") or ""), 999),
            str(row.get("path") or ""),
        )
    )
    return entries


def _swarm_registry_source(slug: str) -> Optional[dict[str, Any]]:
    from .openai_swarm import _find_swarm_files_for_profile

    profile_key = _norm_slug(slug)
    if not profile_key:
        return None
    for state_file, data in _find_swarm_files_for_profile(str(slug)):
        agents = [
            a for a in (data.get("agents") or []) if isinstance(a, dict)
        ]
        agent = next(
            (
                a
                for a in agents
                if _norm_slug(str(a.get("name") or "")) == profile_key
            ),
            None,
        )
        if agent is None:
            continue
        payload = {
            "swarm_name": str(data.get("name") or state_file.stem),
            "topology": str(data.get("topology") or ""),
            "entry_agent": str(data.get("entry_agent") or ""),
            "description": str(data.get("description") or ""),
            "agent": agent,
        }
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        return {
            "name": state_file.name,
            "path": "swarm-registry",
            "absolute_path": str(state_file),
            "description": _ROBOT_SOURCE_EXPLANATIONS["swarm-registry"],
            "language": "json",
            "content": content,
            "truncated": False,
        }
    return None


def _build_robot_agent_code(
    robot: dict[str, Any],
    *,
    soul: str = "",
    skills: Optional[list[str]] = None,
    base_url: str = "http://127.0.0.1:9101",
) -> dict[str, str]:
    """Build export snippets to recreate or invoke this Hermes robot."""
    skills = [str(s).strip() for s in (skills or []) if str(s).strip()]
    display_name = str(robot.get("display_name") or robot.get("slug") or "").strip()
    robot_slug = str(robot.get("slug") or "").strip()
    payload: dict[str, Any] = {
        "display_name": display_name,
        "role": str(robot.get("role") or "").strip(),
        "model_id": str(robot.get("model_id") or "").strip(),
        "execution_backend": str(
            robot.get("execution_backend")
            or (read_profile_execution_backend(robot_slug) if robot_slug else "llm")
            or "llm"
        ).strip()
        or "llm",
        "mascot": str(robot.get("mascot") or "robot").strip() or "robot",
        "swarm_name": str(robot.get("swarm_name") or "swarm-manual").strip()
        or "swarm-manual",
        "text": str(soul or robot.get("instructions") or robot.get("description") or "").strip(),
    }
    role_preset = str(robot.get("role_preset") or "").strip()
    if role_preset:
        payload["role_preset"] = role_preset
    if skills:
        payload["skills"] = skills

    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_one_line = json.dumps(payload, ensure_ascii=False)
    curl_data = json_one_line.replace("'", "'\\''")
    endpoint = f"{base_url.rstrip('/')}/swarm/robots/create"
    curl = (
        f"curl -sS -X POST '{endpoint}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{curl_data}'"
    )
    slug = str(robot.get("slug") or display_name).strip()
    python = (
        "#!/usr/bin/env python3\n"
        f'"""Recriar robô Hermes: {display_name} (slug: {slug})"""\n'
        "import json\n"
        "import urllib.request\n\n"
        f"PAYLOAD = json.loads({json.dumps(json_one_line)!r})\n\n"
        "req = urllib.request.Request(\n"
        f"    {endpoint!r},\n"
        "    data=json.dumps(PAYLOAD).encode('utf-8'),\n"
        "    headers={'Content-Type': 'application/json'},\n"
        "    method='POST',\n"
        ")\n"
        "with urllib.request.urlopen(req) as resp:\n"
        "    print(resp.read().decode('utf-8'))\n"
    )
    return {"json": json_text, "curl": curl, "python": python}


def _build_explained_markdown(
    robot: dict[str, Any],
    files: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Código-fonte — {robot.get('display_name') or robot.get('slug')}",
        "",
        f"**Slug:** `{robot.get('slug') or '—'}`",
    ]
    if robot.get("role"):
        lines.append(f"**Papel:** {robot.get('role')}")
    if robot.get("model_label") or robot.get("model_id"):
        lines.append(
            f"**LLM:** {robot.get('model_label') or robot.get('model_id')}"
        )
    if robot.get("swarm_name"):
        lines.append(f"**Swarm:** {robot.get('swarm_name')}")
    lines.append("")

    for row in files:
        rel = str(row.get("path") or row.get("name") or "arquivo")
        desc = str(row.get("description") or "").strip()
        lines.extend(["---", "", f"## {rel}", ""])
        if desc:
            lines.extend([f"> {desc}", ""])
        body = str(row.get("content") or "").rstrip()
        if body:
            lang = str(row.get("language") or "text")
            fence = lang if lang in {"markdown", "yaml", "json", "python", "bash"} else ""
            lines.extend([f"```{fence}", body, "```", ""])
        if row.get("truncated"):
            lines.append("*(conteúdo truncado para visualização)*")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def get_swarm_robot_source(
    monitor: GoisMonitor,
    slug: str,
    user: Any = None,
) -> dict[str, Any]:
    """Return explained source files for one swarm robot."""
    detail = get_swarm_robot_detail(monitor, slug, user)
    if not detail.get("ok"):
        return detail

    robot = dict(detail.get("robot") or {})
    profile_slug = str(robot.get("slug") or slug).strip()
    profile_dirs = _profile_dir_candidates(profile_slug)
    profile_dir = profile_dirs[0] if profile_dirs else None

    files: list[dict[str, Any]] = []
    if profile_dir is not None:
        files.extend(_profile_source_entries(profile_dir))

    registry = _swarm_registry_source(profile_slug)
    if registry is not None:
        files.append(registry)

    if not files:
        instructions = str(robot.get("instructions") or "").strip()
        if instructions:
            files.append(
                {
                    "name": "SOUL.md",
                    "path": "SOUL.md",
                    "absolute_path": "",
                    "description": _ROBOT_SOURCE_EXPLANATIONS["SOUL.md"],
                    "language": "markdown",
                    "content": instructions,
                    "truncated": False,
                }
            )
        else:
            return {
                "ok": False,
                "error": f"nenhum arquivo-fonte encontrado para '{profile_slug}'",
                "slug": profile_slug,
            }

    soul_text = str(robot.get("instructions") or "").strip()
    if not soul_text:
        for row in files:
            if str(row.get("path") or "") == "SOUL.md":
                soul_text = str(row.get("content") or "").strip()
                break
    skills = robot.get("skills")
    if skills is None:
        skills = read_profile_skills(profile_slug)

    explained = _build_explained_markdown(robot, files)
    agent_code = _build_robot_agent_code(
        robot,
        soul=soul_text,
        skills=skills if isinstance(skills, list) else None,
    )
    return {
        "ok": True,
        "slug": profile_slug,
        "display_name": robot.get("display_name") or profile_slug,
        "profile_dir": str(profile_dir) if profile_dir else "",
        "files": files,
        "explained_markdown": explained,
        "agent_code": agent_code,
        "files_count": len(files),
    }


def get_swarm_robot_detail(
    monitor: GoisMonitor,
    slug: str,
    user: Any = None,
) -> dict[str, Any]:
    """Return editable fields for one robot."""
    profile_slug = _norm_slug(slug)
    if not profile_slug:
        return {"ok": False, "error": "slug is required"}
    if profile_slug == "default":
        return {"ok": False, "error": "cannot edit the default profile"}

    handler = getattr(monitor, "handle_swarm_robots_snapshot", None)
    if handler is not None:
        snapshot = handler(user)
    else:
        snapshot = build_swarm_robots_snapshot(monitor, user)
    robot = next(
        (r for r in snapshot.get("robots") or [] if _norm_slug(r.get("slug")) == profile_slug),
        None,
    )
    if not robot:
        if _profile_exists_on_disk(profile_slug):
            robot = _robot_detail_from_disk(profile_slug)
        else:
            return {"ok": False, "error": f"robot '{profile_slug}' not found"}

    soul_slug = str(robot.get("slug") or profile_slug)
    skills = robot.get("skills")
    if skills is None:
        skills = read_profile_skills(soul_slug)
    saved_meta = read_profile_meta_dict(soul_slug)
    custom_color = normalize_robot_color(str(saved_meta.get("color") or ""))
    role_text = str(robot.get("role") or saved_meta.get("description") or "")
    return {
        "ok": True,
        "robot": {
            **robot,
            "custom_color": custom_color,
            "color": resolve_robot_color(
                slug=soul_slug,
                role_preset=str(robot.get("role_preset") or ""),
                role=role_text,
                color=custom_color,
            ),
            "instructions": _read_profile_soul(soul_slug),
            "skills": skills,
            "deletable": profile_slug != "default",
        },
    }


def update_swarm_robot(
    monitor: GoisMonitor,
    slug: str,
    payload: dict[str, Any],
    user: Any = None,
) -> dict[str, Any]:
    """Update robot metadata, Hermes profile, and swarm registration."""
    if payload.get("resume_schedule") in (True, "true", 1, "1"):
        profile_slug = _norm_slug(slug)
        if not profile_slug:
            return {"ok": False, "error": "slug is required"}
        if profile_slug == "default":
            return {"ok": False, "error": "cannot edit the default profile"}
        return _resume_robot_cron_schedule(monitor, profile_slug)

    if is_profile_assign_payload(payload):
        result = assign_profile_to_swarm(
            monitor,
            slug,
            str(payload.get("swarm_name") or "swarm-manual").strip() or "swarm-manual",
            user=user,
        )
        if result.get("ok"):
            monitor._invalidate_swarm_robots_cache()
        return result

    if not monitor.cfg.hermes or not monitor.cfg.hermes_agent_create.enabled:
        return {"ok": False, "error": "hermes agent create is disabled"}

    profile_slug = _norm_slug(slug)
    if not profile_slug:
        return {"ok": False, "error": "slug is required"}
    if profile_slug == "default":
        return {"ok": False, "error": "cannot edit the default profile"}

    display_name = payload.get("display_name")
    role = payload.get("role")
    model_id = payload.get("model_id")
    execution_backend_raw = payload.get("execution_backend")
    mascot = payload.get("mascot")
    color_raw = payload.get("color")
    swarm_name = payload.get("swarm_name")
    instructions = payload.get("text") or payload.get("instructions")
    skills_raw = payload.get("skills")

    chat_cfg = monitor.cfg.openclaw_chat if monitor.cfg.openclaw_chat.enabled else None
    meta_updates: dict[str, Any] = {}
    if display_name is not None:
        meta_updates["display_name"] = str(display_name).strip()
    if mascot is not None:
        meta_updates["mascot"] = normalize_mascot(str(mascot))
    if role is not None:
        meta_updates["description"] = str(role).strip()
        meta_updates["description_auto"] = False
    if color_raw is not None:
        normalized_color = normalize_robot_color(str(color_raw).strip())
        if str(color_raw).strip() and not normalized_color:
            return {"ok": False, "error": "color inválida (use #rrggbb)"}
        meta_updates["color"] = normalized_color
    if execution_backend_raw is not None:
        meta_updates["execution_backend"] = normalize_execution_backend(
            execution_backend_raw
        )

    if meta_updates and not write_profile_meta(profile_slug, meta_updates):
        return {"ok": False, "error": f"failed to update profile.yaml for '{profile_slug}'"}

    if model_id is not None and str(model_id).strip():
        if not write_profile_model_default(
            profile_slug,
            str(model_id).strip(),
            chat_cfg=chat_cfg,
        ):
            return {"ok": False, "error": f"failed to update model for '{profile_slug}'"}

    soul_updated = False
    if instructions is not None and str(instructions).strip():
        soul_text = str(instructions).strip()
        if isinstance(skills_raw, list):
            soul_text = _sync_soul_skills(soul_text, [
                str(s).strip() for s in skills_raw if str(s).strip()
            ])
        if not _write_profile_soul(profile_slug, soul_text):
            return {"ok": False, "error": f"failed to update SOUL.md for '{profile_slug}'"}
    elif isinstance(skills_raw, list):
        soul_text = _read_profile_soul(profile_slug)
        synced = _sync_soul_skills(soul_text, [
            str(s).strip() for s in skills_raw if str(s).strip()
        ])
        if synced != soul_text.strip():
            if not _write_profile_soul(profile_slug, synced):
                return {"ok": False, "error": f"failed to update SOUL.md for '{profile_slug}'"}

    skills_out: list[str] = []
    if isinstance(skills_raw, list):
        skills_out = [str(s).strip() for s in skills_raw if str(s).strip()]
        if not write_profile_skills(profile_slug, skills_out):
            return {"ok": False, "error": f"failed to update skills for '{profile_slug}'"}

    swarm_result = update_robot_in_swarm(
        profile_slug,
        swarm_name=str(swarm_name).strip() if swarm_name is not None else None,
        role=str(role).strip() if role is not None else None,
        instructions=str(instructions).strip() if instructions is not None else None,
        model_id=str(model_id).strip() if model_id is not None else None,
        mascot=normalize_mascot(str(mascot)) if mascot is not None else None,
        display_name=str(display_name).strip() if display_name is not None else None,
    )
    if swarm_result.get("ok") is False and swarm_name is not None:
        return swarm_result

    cron_result: dict[str, Any] | None = None
    schedule_raw = payload.get("schedule")
    if schedule_raw is None:
        schedule_raw = payload.get("cron_schedule")
    schedule_target_raw = payload.get("schedule_target")
    schedule_task_raw = payload.get("task_id") or payload.get("schedule_task_id")
    if schedule_raw is not None:
        schedule_str = str(schedule_raw).strip()
        if schedule_str:
            cron_result = _upsert_robot_cron_schedule(
                monitor,
                profile_slug,
                schedule_str,
                swarm_name=str(swarm_name).strip() if swarm_name is not None else None,
                user=user,
                schedule_target=(
                    str(schedule_target_raw).strip()
                    if schedule_target_raw is not None
                    else None
                ),
                task_id=(
                    str(schedule_task_raw).strip() if schedule_task_raw else None
                ),
            )
            if not cron_result.get("ok"):
                return {
                    "ok": False,
                    "error": cron_result.get("error")
                    or cron_result.get("reason")
                    or "falha ao configurar cron",
                    "cron": cron_result,
                }

    monitor._invalidate_hermes_profiles_cache()
    model_fields = model_fields_for_profile(
        profile_slug,
        chat_cfg=chat_cfg,
        agent_cfg=monitor.cfg.agent,
    )
    saved_meta = read_profile_meta_dict(profile_slug)
    saved_color = normalize_robot_color(str(saved_meta.get("color") or ""))
    role_text = str(meta_updates.get("description") or role or saved_meta.get("description") or "")
    return {
        "ok": True,
        "name": profile_slug,
        "display_name": meta_updates.get("display_name") or display_name,
        "mascot": meta_updates.get("mascot") or mascot,
        "role": role_text,
        "color": resolve_robot_color(slug=profile_slug, role=role_text, color=saved_color),
        "custom_color": saved_color,
        "model_id": model_fields.get("modelId") or model_id,
        "model_label": model_fields.get("modelLabel") or "",
        "swarm_name": swarm_result.get("swarm_name") or swarm_name,
        "skills": skills_out if isinstance(skills_raw, list) else read_profile_skills(profile_slug),
        "swarm": swarm_result,
        "cron": cron_result,
    }


def resume_swarm_robots_schedule(
    monitor: GoisMonitor,
    swarm_name: str,
    *,
    user: Any = None,
) -> dict[str, Any]:
    """Resume paused Hermes crons for every robot in a swarm."""
    name = str(swarm_name or "").strip()
    if not name:
        return {"ok": False, "error": "swarm_name is required"}

    snapshot = build_swarm_robots_snapshot(monitor, user)
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "error": snapshot.get("error") or "falha ao carregar robôs do swarm",
        }

    slugs = sorted(
        {
            str(row.get("slug") or "").strip()
            for row in (snapshot.get("robots") or [])
            if isinstance(row, dict)
            and str(row.get("swarm_name") or "").strip() == name
            and str(row.get("slug") or "").strip()
        }
    )
    if not slugs:
        return {"ok": False, "error": f"nenhum robô encontrado no swarm '{name}'"}

    resumed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for slug in slugs:
        result = _resume_robot_cron_schedule(monitor, slug)
        count = int(result.get("resumed_count") or 0)
        if result.get("ok") and count > 0:
            resumed.append({"slug": slug, "resumed_count": count})
        elif result.get("ok"):
            skipped.append({"slug": slug})
        else:
            failed.append(
                {
                    "slug": slug,
                    "error": result.get("error") or "falha ao despausar cron",
                }
            )

    monitor._invalidate_hermes_cron_cache()
    monitor._invalidate_swarm_robots_cache()

    partial = bool(resumed and failed)
    out: dict[str, Any] = {
        "ok": not failed or bool(resumed),
        "partial": partial,
        "swarm_name": name,
        "robots_total": len(slugs),
        "resumed_count": len(resumed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "resumed": resumed,
        "skipped": skipped,
        "failed": failed,
        "action": "resumed",
    }
    if failed and not resumed:
        out["ok"] = False
        out["error"] = failed[0].get("error") or "falha ao despausar cron"
    elif failed:
        out["warning"] = (
            f"{len(failed)} agente(s) falharam ao despausar; "
            f"{len(resumed)} retomado(s)"
        )
    elif not resumed:
        out["message"] = "nenhum cron pausado neste swarm"
    return out


def schedule_swarm_robots(
    monitor: GoisMonitor,
    swarm_name: str,
    *,
    schedule: str,
    user: Any = None,
    schedule_target: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update recurring Hermes crons for every robot in a swarm."""
    name = str(swarm_name or "").strip()
    if not name:
        return {"ok": False, "error": "swarm_name is required"}

    schedule_str = str(schedule or "").strip()
    if not schedule_str:
        return {"ok": False, "error": "schedule is required"}

    snapshot = build_swarm_robots_snapshot(monitor, user)
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "error": snapshot.get("error") or "falha ao carregar robôs do swarm",
        }

    slugs = sorted(
        {
            str(row.get("slug") or "").strip()
            for row in (snapshot.get("robots") or [])
            if isinstance(row, dict)
            and str(row.get("swarm_name") or "").strip() == name
            and str(row.get("slug") or "").strip()
        }
    )
    if not slugs:
        return {"ok": False, "error": f"nenhum robô encontrado no swarm '{name}'"}

    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for slug in slugs:
        result = _upsert_robot_cron_schedule(
            monitor,
            slug,
            schedule_str,
            swarm_name=name,
            user=user,
            schedule_target=schedule_target,
            task_id=task_id,
        )
        if result.get("ok"):
            applied.append(
                {
                    "slug": slug,
                    "job_id": result.get("job_id"),
                    "action": result.get("action"),
                }
            )
        else:
            failed.append(
                {
                    "slug": slug,
                    "error": result.get("error")
                    or result.get("reason")
                    or "falha ao configurar cron",
                }
            )

    monitor._invalidate_hermes_cron_cache()
    monitor._invalidate_swarm_robots_cache()

    partial = bool(applied and failed)
    out: dict[str, Any] = {
        "ok": bool(applied),
        "partial": partial,
        "swarm_name": name,
        "schedule": schedule_str,
        "robots_total": len(slugs),
        "applied_count": len(applied),
        "failed_count": len(failed),
        "applied": applied,
        "failed": failed,
    }
    if not applied:
        out["error"] = (
            failed[0].get("error") if failed else "nenhum cron configurado"
        )
    elif partial:
        out["warning"] = (
            f"{len(applied)} de {len(slugs)} agente(s) agendado(s); "
            f"{len(failed)} falha(s)"
        )
    return out


def _set_robot_model(
    monitor: GoisMonitor,
    profile_slug: str,
    model_id: str,
) -> dict[str, Any]:
    """Write the LLM model default for one robot profile."""
    model_id = str(model_id or "").strip()
    if not model_id:
        return {"ok": False, "error": "model_id is required"}

    profile_key = _norm_slug(profile_slug)
    if not profile_key:
        return {"ok": False, "error": "slug is required"}
    if profile_key == "default":
        return {"ok": False, "error": "cannot edit the default profile"}

    exec_backend = read_profile_execution_backend(profile_key)
    if is_ide_execution_backend(exec_backend):
        return {
            "ok": True,
            "skipped": True,
            "reason": "execution_backend_ide",
            "message": "agente usa execução via IDE; modelo LLM ignorado",
        }

    chat_cfg = monitor.cfg.openclaw_chat if monitor.cfg.openclaw_chat.enabled else None
    if not write_profile_model_default(profile_key, model_id, chat_cfg=chat_cfg):
        return {"ok": False, "error": f"failed to update model for '{profile_key}'"}

    model_fields = model_fields_for_profile(
        profile_key,
        chat_cfg=chat_cfg,
        agent_cfg=monitor.cfg.agent,
    )
    return {
        "ok": True,
        "model_id": model_fields.get("modelId") or model_id,
        "model_label": model_fields.get("modelLabel") or model_id,
    }


def set_swarm_robots_model(
    monitor: GoisMonitor,
    swarm_name: str,
    *,
    model_id: str,
    user: Any = None,
) -> dict[str, Any]:
    """Set the LLM model for every robot in a swarm."""
    name = str(swarm_name or "").strip()
    if not name:
        return {"ok": False, "error": "swarm_name is required"}

    model_id = str(model_id or "").strip()
    if not model_id:
        return {"ok": False, "error": "model_id is required"}

    snapshot = build_swarm_robots_snapshot(monitor, user)
    if not snapshot.get("ok"):
        return {
            "ok": False,
            "error": snapshot.get("error") or "falha ao carregar robôs do swarm",
        }

    slugs = sorted(
        {
            str(row.get("slug") or "").strip()
            for row in (snapshot.get("robots") or [])
            if isinstance(row, dict)
            and str(row.get("swarm_name") or "").strip() == name
            and str(row.get("slug") or "").strip()
        }
    )
    if not slugs:
        return {"ok": False, "error": f"nenhum robô encontrado no swarm '{name}'"}

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for slug in slugs:
        result = _set_robot_model(monitor, slug, model_id)
        if result.get("ok") and result.get("skipped"):
            skipped.append({"slug": slug, "reason": result.get("reason") or "skipped"})
        elif result.get("ok"):
            applied.append(
                {
                    "slug": slug,
                    "model_id": result.get("model_id") or model_id,
                    "model_label": result.get("model_label") or "",
                }
            )
        else:
            failed.append(
                {
                    "slug": slug,
                    "error": result.get("error") or "falha ao definir modelo",
                }
            )

    monitor._invalidate_hermes_profiles_cache()
    monitor._invalidate_swarm_robots_cache()

    partial = bool(applied and failed)
    out: dict[str, Any] = {
        "ok": bool(applied),
        "partial": partial,
        "swarm_name": name,
        "model_id": model_id,
        "robots_total": len(slugs),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
    }
    if applied:
        out["model_label"] = applied[0].get("model_label") or model_id
    if not applied:
        out["error"] = (
            failed[0].get("error")
            if failed
            else (
                "nenhum agente LLM atualizado"
                if skipped
                else "nenhum modelo configurado"
            )
        )
    elif partial:
        out["warning"] = (
            f"{len(applied)} de {len(slugs)} agente(s) atualizado(s); "
            f"{len(failed)} falha(s)"
        )
    elif skipped and not failed:
        out["warning"] = (
            f"{len(skipped)} agente(s) ignorado(s) (execução via IDE)"
        )
    return out


def delete_swarm_robot(
    monitor: GoisMonitor,
    slug: str,
    payload: Optional[dict[str, Any]] = None,
    user: Any = None,
) -> dict[str, Any]:
    """Remove robot from swarm registry and optionally delete Hermes profile."""
    if not monitor.cfg.hermes:
        return {"ok": False, "error": "hermes is not configured"}

    profile_slug = str(slug or "").strip()
    profile_key = _norm_slug(profile_slug)
    if not profile_key:
        return {"ok": False, "error": "slug is required"}
    if profile_key == "default":
        return {"ok": False, "error": "cannot delete the default profile"}

    payload = payload or {}
    delete_profile = payload.get("delete_profile", True)
    swarm_only = payload.get("swarm_only", False)
    if swarm_only:
        delete_profile = False

    running = _running_by_profile(monitor).get(profile_key, [])
    if running and delete_profile:
        return {
            "ok": False,
            "error": "robô com jobs em execução; pare-os antes de excluir o perfil",
            "running_jobs": running,
        }

    removed_swarms = remove_robot_from_swarm(profile_slug)
    if not removed_swarms:
        removed_swarms = remove_robot_from_swarm(profile_key)
    removed_teams = _unlink_profile_from_all_teams(monitor, profile_slug)
    if not removed_teams:
        removed_teams = _unlink_profile_from_all_teams(monitor, profile_key)

    removed_crons: list[dict[str, Any]] = []
    for cron_slug in dict.fromkeys([profile_slug, profile_key]):
        cron_result = remove_cron_jobs_for_profile(cron_slug)
        if cron_result.get("removed_count"):
            removed_crons.extend(cron_result.get("removed") or [])
    if removed_crons:
        monitor._invalidate_hermes_cron_cache()

    profile_dirs_exist = bool(
        _profile_dir_candidates(profile_slug)
        or (profile_key != profile_slug and _profile_dir_candidates(profile_key))
    )
    profile_deleted = False
    profile_error = ""
    if delete_profile and profile_dirs_exist:
        profile_deleted = _delete_profile_filesystem(profile_slug)
        if not profile_deleted and profile_key != profile_slug:
            profile_deleted = _delete_profile_filesystem(profile_key)
        if not profile_deleted:
            for api_name in (profile_slug, profile_key):
                try:
                    api_result = monitor.handle_hermes_profile_delete(api_name)
                    if api_result.get("ok"):
                        profile_deleted = True
                        break
                    profile_error = str(api_result.get("error") or "profile delete failed")
                except Exception as exc:
                    profile_error = str(exc)

    monitor._invalidate_hermes_profiles_cache()
    # Deleting something that no longer exists anywhere is a success
    # (idempotent): "ghost" robots only appear in the panel because they
    # are kanban assignees or stale registry entries.
    ghost = (
        not removed_swarms
        and not removed_teams
        and not profile_deleted
        and not profile_dirs_exist
    )
    ok = bool(
        removed_swarms
        or removed_teams
        or profile_deleted
        or not delete_profile
        or ghost
    )
    if ok:
        # Hide the slug from kanban/team-derived listings from now on.
        add_robot_tombstone(profile_key)
    result: dict[str, Any] = {
        "ok": ok,
        "name": profile_slug,
        "removed_from_swarms": removed_swarms,
        "removed_from_teams": removed_teams,
        "removed_crons": removed_crons,
        "profile_deleted": profile_deleted,
    }
    if ghost:
        result["note"] = (
            f"'{profile_slug}' não tinha perfil nem registro de swarm; "
            "removido apenas da listagem"
        )
    elif not delete_profile and ok:
        result["profile_kept"] = True
    if profile_error:
        result["profile_error"] = profile_error
    if not ok:
        result["error"] = profile_error or "não foi possível excluir o robô"
    return result


def _team_board_agents(
    team: Any,
    *,
    swarm: Optional[dict[str, Any]],
    robots_by_slug: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect swarm agents linked to a team (profile_slugs + swarm file)."""
    agent_slugs: set[str] = set()
    for raw in team.profile_slugs or []:
        norm = _norm_slug(str(raw))
        if norm:
            agent_slugs.add(norm)
    if swarm:
        for raw in swarm.get("hermes_profiles") or []:
            norm = _norm_slug(str(raw))
            if norm:
                agent_slugs.add(norm)

    agents: list[dict[str, Any]] = []
    for slug in sorted(agent_slugs):
        robot = robots_by_slug.get(slug) or {}
        working = robot.get("working_card") if isinstance(robot.get("working_card"), dict) else {}
        role_preset = str(robot.get("role_preset") or "").strip()
        role = str(robot.get("role") or "").strip()
        agents.append(
            {
                "slug": slug,
                "display_name": str(robot.get("display_name") or slug),
                "state": str(robot.get("state") or "idle"),
                "role": role,
                "role_preset": role_preset,
                "color": resolve_robot_color(
                    slug=slug,
                    role_preset=role_preset,
                    role=role,
                    color=str(robot.get("color") or ""),
                ),
                "mascot": str(robot.get("mascot") or "robot"),
                "working_card_id": str(working.get("id") or "").strip(),
            }
        )
    return agents


def swarm_running_exec_by_task(
    monitor: GoisMonitor,
    *,
    team_id: str = "",
    board_task_ids: Optional[set[str]] = None,
) -> dict[str, str]:
    """Map task_id -> executor slug for in-flight swarm graph runs on a team."""
    tid_filter = str(team_id or "").strip()
    if not tid_filter:
        return {}

    from .openai_swarm import load_swarms_full
    from .swarm_graph import load_checkpoint

    team_map = _profile_team_map(monitor, None)
    teams_index = {
        t.id: {"team_name": t.name}
        for t in monitor.accounts.list_all_teams()
    }
    exec_map: dict[str, str] = {}
    for state in load_swarms_full():
        if not isinstance(state, dict):
            continue
        meta = _resolve_swarm_team_meta(state, team_map, teams_index)
        if str(meta.get("team_id") or "").strip() != tid_filter:
            continue
        swarm_name = str(state.get("name") or "").strip()
        if not swarm_name:
            continue
        cp = load_checkpoint(swarm_name)
        if not cp or str(cp.get("status") or "") != "running":
            continue
        current_raw = str(cp.get("current") or "").strip()
        slug = _canonical_profile_slug(current_raw, None) or _norm_slug(current_raw)
        agent_cards = (
            cp.get("agent_cards") if isinstance(cp.get("agent_cards"), dict) else {}
        )
        cards = list(
            agent_cards.get(slug or "")
            or agent_cards.get(current_raw)
            or []
        )
        task_id = ""
        if cards and isinstance(cards[0], dict):
            task_id = str(cards[0].get("id") or "").strip()
        if not task_id:
            task_id = _task_id_from_title(
                _title_from_swarm_objective(str(cp.get("objective") or ""), slug)
            )
        if not task_id:
            continue
        if board_task_ids is not None and task_id not in board_task_ids:
            continue
        executor = _resolve_live_executor_slug(current_raw or slug)
        if not executor:
            for key, card_list in agent_cards.items():
                if not isinstance(card_list, list):
                    continue
                owns_task = any(
                    isinstance(card, dict)
                    and str(card.get("id") or "").strip() == task_id
                    for card in card_list
                )
                if not owns_task:
                    continue
                executor = _resolve_live_executor_slug(str(key or ""))
                if executor:
                    break
        if executor:
            exec_map[task_id] = executor
    return exec_map


def _parse_exec_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _record_task_executor(
    store: dict[str, tuple[float, str]],
    task_id: str,
    executor: str,
    ts: float,
) -> None:
    tid = str(task_id or "").strip()
    slug = str(executor or "").strip()
    if not tid or not slug or ts <= 0:
        return
    prev = store.get(tid)
    if prev is None or ts >= prev[0]:
        store[tid] = (ts, slug)


def _collect_current_exec_by_task(
    monitor: GoisMonitor,
    board: dict[str, Any],
    *,
    team_id: str = "",
) -> dict[str, str]:
    """Map task_id -> profile slug for in-flight executions."""
    exec_by_task: dict[str, str] = {}
    board_task_ids = {
        str(t.get("id") or "").strip()
        for t in (board.get("tasks") or [])
        if isinstance(t, dict) and str(t.get("id") or "").strip()
    }

    handler = getattr(monitor, "_priority_queue_handler", None)
    if handler is not None:
        try:
            queue = handler.engine.get_queue()
        except Exception as exc:
            log.debug("kanban enrich: priority queue unavailable: %s", exc)
            queue = {}
        for row in queue.get("running") or []:
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("task_id") or "").strip()
            assignee = str(row.get("assignee") or "").strip()
            if task_id and assignee:
                exec_by_task[task_id] = assignee

    try:
        from .kanban_schedule_jobs import list_running_jobs

        for job in list_running_jobs():
            task_id = str(job.task_id or "").strip()
            profile = str(job.profile or "").strip()
            if task_id and profile:
                exec_by_task.setdefault(task_id, profile)
    except Exception as exc:
        log.debug("kanban enrich: schedule jobs unavailable: %s", exc)

    cron_snap: Optional[dict[str, Any]] = None
    try:
        cron_snap = monitor._cached_hermes_cron_snapshot()
    except Exception as exc:
        log.debug("kanban enrich: cron snapshot unavailable: %s", exc)

    cron_profile_by_id: dict[str, str] = {}
    running_cron_ids: set[str] = set()
    if isinstance(cron_snap, dict):
        running_cron_ids = {
            str(row.get("job_id") or "").strip()
            for row in (cron_snap.get("running") or [])
            if isinstance(row, dict) and str(row.get("job_id") or "").strip()
        }
        for job in cron_snap.get("running") or []:
            if not isinstance(job, dict):
                continue
            task_id = str(job.get("task_id") or "").strip()
            profile = str(
                job.get("profile") or job.get("source_profile") or ""
            ).strip()
            if task_id and profile:
                exec_by_task.setdefault(task_id, profile)
        for job in cron_snap.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or "").strip()
            if not job_id:
                continue
            profile = str(
                job.get("profile") or job.get("source_profile") or ""
            ).strip()
            if profile:
                cron_profile_by_id[job_id] = profile

    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id or task_id in exec_by_task:
            continue
        cron_job_id = str(task.get("cron_job_id") or "").strip()
        if not cron_job_id:
            continue
        if cron_job_id not in running_cron_ids:
            continue
        executor = cron_profile_by_id.get(cron_job_id, "")
        if not executor:
            from .hermes_kanban import normalize_assignees

            assignees = normalize_assignees(
                task.get("assignees") or task.get("assignee")
            )
            executor = str(assignees[0] if assignees else "").strip()
        if executor:
            exec_by_task[task_id] = executor

    resolved_team_id = str(team_id or board.get("team_id") or "").strip()
    if resolved_team_id and board_task_ids:
        for task_id, executor in swarm_running_exec_by_task(
            monitor,
            team_id=resolved_team_id,
            board_task_ids=board_task_ids,
        ).items():
            exec_by_task.setdefault(task_id, executor)

    for task_id, executor in list(exec_by_task.items()):
        live = _resolve_live_executor_slug(executor)
        if live:
            exec_by_task[task_id] = live
        else:
            del exec_by_task[task_id]

    return exec_by_task


def _collect_last_exec_by_task(
    monitor: GoisMonitor,
    board: dict[str, Any],
) -> dict[str, str]:
    """Map task_id -> last executor slug (priority queue, cron, kanban yaml)."""
    ranked: dict[str, tuple[float, str]] = {}

    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        executor = str(
            task.get("last_executor") or task.get("executed_by") or ""
        ).strip()
        if not task_id or not executor:
            continue
        ts = _parse_exec_ts(
            task.get("last_executed_at") or task.get("completed_at")
        )
        if ts <= 0:
            ts = time.time()
        _record_task_executor(ranked, task_id, executor, ts)

    handler = getattr(monitor, "_priority_queue_handler", None)
    if handler is not None:
        getter = getattr(handler.engine, "last_executor_by_task", None)
        if callable(getter):
            try:
                for task_id, (ts, assignee) in getter().items():
                    _record_task_executor(ranked, task_id, assignee, ts)
            except Exception as exc:
                log.debug("kanban enrich: pq last executor unavailable: %s", exc)

    try:
        cron_snap = monitor._cached_hermes_cron_snapshot()
    except Exception as exc:
        log.debug("kanban enrich: cron snapshot unavailable: %s", exc)
        cron_snap = None

    if isinstance(cron_snap, dict):
        for job in cron_snap.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            last_status = str(job.get("last_status") or "").strip().lower()
            if last_status not in {"ok", "error", "done", "success"}:
                continue
            task_id = _task_id_from_kanban_cron_job(job) or ""
            profile = str(
                job.get("profile") or job.get("source_profile") or ""
            ).strip()
            ts = _parse_exec_ts(job.get("last_run_at"))
            if task_id and profile and ts > 0:
                _record_task_executor(ranked, task_id, profile, ts)

    out: dict[str, str] = {}
    for task_id, (_, slug) in ranked.items():
        live = _resolve_live_executor_slug(slug)
        if live:
            out[task_id] = live
    return out


def _apply_task_executor_visual(
    task: dict[str, Any],
    *,
    current_by_task: dict[str, str],
    last_by_task: dict[str, str],
) -> None:
    """Set executor_slug/color on a kanban task for UI highlighting."""
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return
    current = str(current_by_task.get(task_id) or "").strip()
    last = str(
        current
        or last_by_task.get(task_id)
        or task.get("last_executor")
        or task.get("executed_by")
        or ""
    ).strip()
    live_current = _resolve_live_executor_slug(current) if current else ""
    live_last = _resolve_live_executor_slug(last) if last else ""
    if not live_last:
        task.pop("executor_slug", None)
        task.pop("last_executor", None)
        task.pop("color", None)
        task.pop("executing", None)
        task.pop("working_by", None)
        return
    task["executor_slug"] = live_last
    task["last_executor"] = live_last
    task["color"] = resolve_robot_color(slug=live_last)
    if live_current:
        task["working_by"] = live_current
        task["executing"] = True
    else:
        task.pop("executing", None)
        task.pop("working_by", None)


def enrich_kanban_board_execution(
    monitor: GoisMonitor,
    board: dict[str, Any],
    *,
    team_id: str = "",
) -> dict[str, Any]:
    """Annotate kanban tasks with live robot execution metadata for the UI."""
    if not isinstance(board, dict):
        return board

    current_by_task = _collect_current_exec_by_task(
        monitor, board, team_id=team_id
    )
    last_by_task = _collect_last_exec_by_task(monitor, board)

    resolved_team_id = str(team_id or board.get("team_id") or "").strip()
    if current_by_task and resolved_team_id:
        from .hermes_kanban import resolve_work_column_id

        workdir = str(board.get("workdir") or "").strip()
        kanban_file = board.get("kanban_file")
        kanban_file_str = str(kanban_file).strip() if kanban_file else None
        doing_col = resolve_work_column_id(board, "doing")
        for task_id in current_by_task:
            if monitor._move_kanban_task_to_doing(
                workdir=workdir,
                task_id=task_id,
                kanban_file=kanban_file_str,
                team_id=resolved_team_id,
            ):
                for task in board.get("tasks") or []:
                    if (
                        isinstance(task, dict)
                        and str(task.get("id") or "").strip() == task_id
                    ):
                        task["column"] = doing_col
                        break

    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        _apply_task_executor_visual(
            task,
            current_by_task=current_by_task,
            last_by_task=last_by_task,
        )

    return board


def build_teams_swarm_board(
    monitor: GoisMonitor,
    user: Any = None,
    *,
    snapshot: Optional[dict[str, Any]] = None,
    boards: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Board payload for /swarm/teams-board — teams, kanban cards and swarm agents."""
    from .hermes_kanban import suggest_assignee_for_task

    actor = monitor._accounts_actor(user)
    if monitor.cfg.auth.enabled and actor is None:
        return {"ok": False, "error": "not authenticated"}

    if snapshot is None:
        snapshot = build_swarm_robots_snapshot(monitor, user)
    robots_by_slug = {
        _norm_slug(str(row.get("slug") or "")): row
        for row in (snapshot.get("robots") or [])
        if isinstance(row, dict) and row.get("slug")
    }
    swarms_by_name = {
        str(row.get("name") or ""): row
        for row in (snapshot.get("swarms") or [])
        if isinstance(row, dict) and row.get("name")
    }

    profiles_by_slug: dict[str, dict[str, Any]] = {}
    for row in robots_by_slug.values():
        slug = _norm_slug(str(row.get("slug") or ""))
        if slug:
            profiles_by_slug[slug] = {
                "slug": slug,
                "display_name": row.get("display_name") or slug,
            }
    alias_map = _build_assignee_alias_map(profiles_by_slug)

    if boards is None:
        boards, _ = _load_kanban_context(monitor, user)
    boards_by_team = {
        str(board.get("team_id") or "").strip(): board
        for board in boards
        if str(board.get("team_id") or "").strip()
    }

    teams_out: list[dict[str, Any]] = []
    for team in monitor.accounts.list_all_teams():
        board = boards_by_team.get(team.id)
        if board is None:
            try:
                raw_board = monitor.accounts.read_kanban(team.id, team.owner_id)
            except Exception:
                raw_board = None
            if isinstance(raw_board, dict):
                board = dict(raw_board)
                board.setdefault("team_id", team.id)

        swarm_name = str(team.swarm_name or "").strip() or team.id
        swarm = swarms_by_name.get(swarm_name) or swarms_by_name.get(team.id)
        agents = _team_board_agents(
            team,
            swarm=swarm if isinstance(swarm, dict) else None,
            robots_by_slug=robots_by_slug,
        )
        agent_rows = [
            {"slug": a["slug"], "role": a.get("role") or ""}
            for a in agents
        ]
        agent_colors = {
            str(a.get("slug") or ""): str(a.get("color") or "")
            for a in agents
            if a.get("slug")
        }

        columns_out: list[dict[str, str]] = []
        tasks_out: list[dict[str, Any]] = []
        workdir = ""
        kanban_file: Optional[str] = None

        if isinstance(board, dict):
            board = enrich_kanban_board_execution(
                monitor,
                dict(board),
                team_id=str(team.id or ""),
            )
            workdir = str(board.get("workdir") or "").strip()
            kanban_file_raw = board.get("kanban_file")
            kanban_file = (
                str(kanban_file_raw).strip() if kanban_file_raw is not None else None
            ) or None
            col_map = {
                _norm_slug(c.get("id")): str(c.get("title") or c.get("id") or "")
                for c in (board.get("columns") or [])
                if isinstance(c, dict)
            }
            columns_out = [
                {"id": cid, "title": title}
                for cid, title in col_map.items()
                if cid
            ]
            for task in board.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                if _is_scheduling_lane_card(task, board):
                    continue
                card = _task_to_card(task, board, col_map)
                assignees = _task_assignees(task, alias_map)
                card["assignees"] = assignees
                card["skills"] = list(task.get("skills") or [])
                working_by = str(
                    card.get("working_by") or task.get("working_by") or ""
                ).strip()
                if not working_by:
                    for agent in agents:
                        if agent.get("working_card_id") == card["id"]:
                            working_by = agent["slug"]
                            break
                    if working_by:
                        card["working_by"] = working_by
                card["claimable"] = (
                    card.get("column") not in {"done"}
                    and not working_by
                )
                suggested = ""
                if not assignees and agent_rows:
                    suggested = suggest_assignee_for_task(task, agent_rows)
                card["suggested_agent"] = suggested
                executor_slug = str(
                    card.get("executor_slug")
                    or working_by
                    or card.get("last_executor")
                    or task.get("last_executor")
                    or ""
                ).strip()
                live_executor = _resolve_live_executor_slug(executor_slug)
                if live_executor:
                    card["executor_slug"] = live_executor
                    card["color"] = str(card.get("color") or "").strip() or (
                        agent_colors.get(live_executor)
                        or resolve_robot_color(slug=live_executor)
                    )
                else:
                    card.pop("executor_slug", None)
                    card.pop("working_by", None)
                    card.pop("last_executor", None)
                    card.pop("color", None)
                tasks_out.append(card)

        teams_out.append(
            {
                "id": team.id,
                "name": team.name,
                "description": str(team.description or ""),
                "swarm_name": swarm_name,
                "swarm_topology": str((swarm or {}).get("topology") or "team"),
                "profile_slugs": [
                    str(s).strip()
                    for s in (team.profile_slugs or [])
                    if str(s).strip()
                ],
                "agents_count": len(agents),
                "agents": agents,
                "columns": columns_out,
                "tasks": tasks_out,
                "workdir": workdir,
                "kanban_file": kanban_file,
            }
        )

    teams_out.sort(key=lambda row: str(row.get("name") or row.get("id") or "").lower())
    return {
        "ok": True,
        "updated_at": time.time(),
        "teams": teams_out,
        "summary": {
            "teams": len(teams_out),
            "tasks": sum(len(t.get("tasks") or []) for t in teams_out),
            "agents": sum(len(t.get("agents") or []) for t in teams_out),
        },
    }


def _profile_workdir_from_slug(slug: str) -> str:
    """Best-effort workdir for a Hermes profile slug."""
    name = str(slug or "").strip()
    if not name:
        return ""
    for reader in (read_profile_meta_dict, read_profile_config_dict):
        data = reader(name)
        wd = str(data.get("workdir") or "").strip()
        if wd:
            return wd
    return ""


def _cron_workdir_for_profile(
    monitor: GoisMonitor,
    slug: str,
    alias_map: Optional[dict[str, str]] = None,
) -> str:
    """Resolve cron job workdir for a profile, if any."""
    canonical = _canonical_profile_slug(slug, alias_map) or _norm_slug(slug)
    if not canonical:
        return ""
    try:
        cron_snap = monitor._cached_hermes_cron_snapshot()
    except Exception:
        return ""
    for job in cron_snap.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        prof = _canonical_profile_slug(
            str(job.get("profile") or job.get("source_profile") or ""),
            alias_map,
        ) or _norm_slug(str(job.get("profile") or ""))
        if prof != canonical:
            continue
        wd = str(job.get("workdir") or "").strip()
        if wd:
            return wd
    return ""


def handle_swarm_activity_changes(
    monitor: GoisMonitor,
    query: dict[str, Any],
    user: Any = None,
) -> dict[str, Any]:
    """List files changed by an agent run (git + paths mentioned in output)."""
    job_id = str(query.get("job_id") or query.get("jobId") or "").strip()
    if job_id:
        result = monitor.handle_hermes_cron_result(job_id)
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "generated_files": result.get("generated_files") or [],
            "workdir": str(result.get("workdir") or ""),
            "response_preview": str(result.get("preview") or "")[:500],
        }

    robot_slug = str(query.get("robot_slug") or query.get("slug") or "").strip()
    team_id = str(query.get("team_id") or "").strip()
    deliverable = str(
        query.get("deliverable")
        or query.get("text")
        or query.get("response")
        or ""
    ).strip()
    workdir = str(query.get("workdir") or query.get("source_workdir") or "").strip()

    alias_map: dict[str, str] = {}
    if monitor.cfg.hermes and monitor.cfg.hermes_agent_create.enabled:
        try:
            profiles_payload = monitor.handle_hermes_profiles_list(user, {"quick": "1"})
            profiles_by_slug = {
                _norm_slug(str(row.get("slug") or "")): row
                for row in profiles_payload.get("profiles") or []
                if isinstance(row, dict) and row.get("slug")
            }
            alias_map = _build_assignee_alias_map(profiles_by_slug)
        except Exception:
            alias_map = {}

    if not workdir and team_id:
        try:
            workdir = str(monitor.accounts.team_workdir(team_id) or "").strip()
        except Exception:
            workdir = workdir or ""

    if not workdir and robot_slug:
        workdir = _cron_workdir_for_profile(monitor, robot_slug, alias_map)
    if not workdir and robot_slug:
        workdir = _profile_workdir_from_slug(robot_slug)

    if not workdir and (deliverable or robot_slug):
        from .hermes_cron import resolve_cron_workdir

        create_cfg = (
            monitor.cfg.hermes_agent_create
            if monitor.cfg.hermes_agent_create.enabled
            else None
        )
        workdir = resolve_cron_workdir(
            None,
            deliverable,
            profile_slug=robot_slug,
            create_cfg=create_cfg,
        )

    if not workdir and not deliverable:
        return {"ok": True, "generated_files": [], "workdir": ""}

    files = collect_generated_files(deliverable, workdir or None)
    return {
        "ok": True,
        "generated_files": files,
        "workdir": workdir,
    }


_AGENT_HISTORY_WINDOW_SECONDS = 7 * 24 * 3600
_AGENT_HISTORY_LIMIT = 60


def _history_entry_key(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("source") or ""),
        str(entry.get("job_id") or ""),
        str(entry.get("task_id") or ""),
        str(entry.get("run_file") or ""),
        str(entry.get("started_at") or ""),
        str(entry.get("finished_at") or ""),
        str(entry.get("title") or "")[:80],
    ]
    return "|".join(parts)


def _history_sort_ts(entry: dict[str, Any]) -> float:
    for key in ("finished_at", "started_at"):
        ts = _parse_activity_ts(entry.get(key))
        if ts is not None:
            return ts
    return 0.0


def _history_status_from_column(column: str) -> str:
    col = str(column or "").strip().lower()
    if col in {"error", "errors"}:
        return "error"
    if col in {"running", "working"}:
        return col
    return "done"


def _append_history_entry(
    items: dict[str, dict[str, Any]],
    entry: dict[str, Any],
) -> None:
    if not str(entry.get("title") or "").strip():
        title = str(entry.get("task_id") or entry.get("job_id") or "").strip()
        if title:
            entry["title"] = title
    if not str(entry.get("title") or "").strip():
        return
    key = _history_entry_key(entry)
    existing = items.get(key)
    if existing is None:
        items[key] = entry
        return
    for field in (
        "execution_log",
        "deliverable",
        "preview",
        "error",
        "run_file",
        "finished_at",
        "started_at",
    ):
        if not str(existing.get(field) or "").strip() and str(entry.get(field) or "").strip():
            existing[field] = entry[field]
    if len(entry.get("execution_log") or []) > len(existing.get("execution_log") or []):
        existing["execution_log"] = entry["execution_log"]


def build_agent_execution_history(
    monitor: GoisMonitor,
    slug: str,
    user: Any = None,
    *,
    limit: int = _AGENT_HISTORY_LIMIT,
    window_seconds: float = _AGENT_HISTORY_WINDOW_SECONDS,
    team_id: Optional[str] = None,
    task_id: Optional[str] = None,
    cron_snap: Optional[dict[str, Any]] = None,
    alias_map: Optional[dict[str, str]] = None,
    profiles_by_slug: Optional[dict[str, dict[str, Any]]] = None,
    boards: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Aggregate recent executions for one Hermes profile / swarm robot."""
    raw_slug = str(slug or "").strip()
    if not raw_slug:
        return {"ok": False, "error": "slug is required"}

    filter_task_id = str(task_id or "").strip().upper()

    if alias_map is None or profiles_by_slug is None:
        _alias_map: dict[str, str] = {}
        _profiles_by_slug: dict[str, dict[str, Any]] = {}
        if monitor.cfg.hermes and monitor.cfg.hermes_agent_create.enabled:
            try:
                profiles_payload = monitor.handle_hermes_profiles_list(user, {"quick": "1"})
                for row in profiles_payload.get("profiles") or []:
                    if not isinstance(row, dict):
                        continue
                    meta = _profile_meta(row)
                    if meta["slug"]:
                        _profiles_by_slug[_norm_slug(meta["slug"])] = meta
                _alias_map = _build_assignee_alias_map(_profiles_by_slug)
            except Exception:
                _alias_map = {}
        alias_map = _alias_map
        profiles_by_slug = _profiles_by_slug

    canonical = _canonical_profile_slug(raw_slug, alias_map) or _norm_slug(raw_slug)
    profile = profiles_by_slug.get(canonical) or {}
    display_name = str(
        profile.get("display_name") or read_profile_display_name(raw_slug) or raw_slug
    ).strip()

    now = time.time()
    items_map: dict[str, dict[str, Any]] = {}
    if cron_snap is None:
        cron_snap = {}
        if monitor.cfg.hermes:
            try:
                cron_snap = monitor._cached_hermes_cron_snapshot()
            except Exception:
                cron_snap = {}

    output_root = None
    if monitor.cfg.hermes:
        try:
            output_root = monitor._hermes_cron_output_path()
        except Exception:
            output_root = None

    def profile_matches(raw_profile: str) -> bool:
        resolved = _canonical_profile_slug(raw_profile, alias_map) or _norm_slug(raw_profile)
        return resolved == canonical

    if boards is None:
        boards, _kanban_agents = _load_kanban_context(monitor, user)
    task_title_index = _build_task_title_index(boards)
    task_team_index = _build_task_team_index(boards)
    filter_team_id = str(team_id or "").strip()

    def history_team_id(
        *,
        task_id: str = "",
        explicit_team_id: str = "",
        title: str = "",
    ) -> str:
        return _resolve_history_team_id(
            task_id=task_id,
            explicit_team_id=explicit_team_id,
            title=title,
            task_team_index=task_team_index,
        )

    for job in cron_snap.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        profile_raw = str(job.get("profile") or job.get("source_profile") or "").strip()
        if not profile_matches(profile_raw):
            continue
        job_id = str(job.get("id") or "").strip()
        job_name = str(job.get("name") or job_id or "Cron").strip()
        last_run = job.get("last_run_at")
        last_status = str(job.get("last_status") or "").strip().lower()
        if last_run and _activity_within_window(
            finished_at=last_run,
            started_at=last_run,
            now=now,
            window_seconds=window_seconds,
        ):
            cron_err = str(job.get("last_error") or "").strip()
            cron_msg = str(job.get("last_message") or "").strip()
            column = "error" if last_status == "error" else "done"
            cron_task_id = _extract_task_id_token(job_name) or _extract_task_id_token(cron_msg)
            _append_history_entry(
                items_map,
                {
                    "id": job_id or job_name,
                    "task_id": cron_task_id,
                    "job_id": job_id,
                    "title": job_name,
                    "source": "cron",
                    "status": _history_status_from_column(column),
                    "column": column,
                    "started_at": last_run,
                    "finished_at": last_run,
                    "preview": (cron_err or cron_msg)[:500],
                    "deliverable": cron_err if column == "error" else cron_msg,
                    "execution_log": [cron_msg or cron_err] if (cron_msg or cron_err) else [],
                    "error": cron_err or None,
                    "robot_slug": canonical,
                    "team_id": history_team_id(task_id=cron_task_id, title=job_name),
                },
            )
        if output_root is not None and job_id:
            for run in list_cron_job_runs(job_id, output_root, limit=12):
                run_time = run.get("run_time")
                if not _activity_within_window(
                    finished_at=run_time,
                    started_at=run_time,
                    now=now,
                    window_seconds=window_seconds,
                ):
                    continue
                preview = str(run.get("preview") or "").strip()
                cron_task_id = _extract_task_id_token(job_name) or _extract_task_id_token(preview)
                _append_history_entry(
                    items_map,
                    {
                        "id": f"{job_id}:{run.get('file') or run_time}",
                        "task_id": cron_task_id,
                        "job_id": job_id,
                        "run_file": str(run.get("file") or ""),
                        "title": job_name,
                        "source": "cron",
                        "status": "done",
                        "column": "done",
                        "started_at": run_time,
                        "finished_at": run_time,
                        "preview": preview[:500],
                        "deliverable": preview,
                        "execution_log": [preview] if preview else [],
                        "robot_slug": canonical,
                        "team_id": history_team_id(task_id=cron_task_id, title=job_name),
                    },
                )

    for job in kanban_schedule_list_recent():
        if not profile_matches(job.profile):
            continue
        ks_task_id = str(job.task_id or "").strip()
        finished = job.finished_at or job.started_at
        if not _activity_within_window(
            finished_at=finished,
            started_at=job.started_at,
            now=now,
            window_seconds=window_seconds,
        ) and job.status != "running":
            continue
        column = {
            "running": "running",
            "done": "done",
            "error": "error",
        }.get(str(job.status or ""), "error")
        ks_log = [str(x).strip() for x in (job.progress or []) if str(x).strip()]
        if not ks_log:
            last = str(job.last_progress or "").strip()
            if last:
                ks_log = [last]
        deliverable = _format_job_result_text(job.result, error=str(job.error or ""))
        _append_history_entry(
            items_map,
            {
                "id": ks_task_id or str(job.id),
                "task_id": ks_task_id,
                "job_id": str(job.id or ""),
                "title": ks_task_id or str(job.id or "Card kanban"),
                "source": "kanban_schedule",
                "status": _history_status_from_column(column),
                "column": column,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "preview": (deliverable or str(job.error or ""))[:500],
                "deliverable": deliverable,
                "execution_log": ks_log,
                "error": str(job.error or "").strip() or None,
                "robot_slug": canonical,
                "team_id": history_team_id(task_id=ks_task_id, title=ks_task_id),
            },
        )

    pq_handler = getattr(monitor, "_priority_queue_handler", None)
    if pq_handler is not None:
        try:
            raw_queue = pq_handler.engine.get_queue()
        except Exception:
            raw_queue = {}
        if isinstance(raw_queue, dict):
            for bucket, column in (
                ("running", "running"),
                ("done", "done"),
                ("errors", "error"),
            ):
                for row in raw_queue.get(bucket) or []:
                    if not isinstance(row, dict):
                        continue
                    if not profile_matches(str(row.get("assignee") or "")):
                        continue
                    finished = row.get("finished_at") or row.get("started_at")
                    if not _activity_within_window(
                        finished_at=finished,
                        started_at=row.get("started_at"),
                        now=now,
                        window_seconds=window_seconds,
                    ) and column != "running":
                        continue
                    pq_task_id = str(row.get("task_id") or "").strip()
                    pq_log = [
                        str(x).strip() for x in (row.get("progress") or []) if str(x).strip()
                    ]
                    deliverable = _format_job_result_text(
                        row.get("result"),
                        error=str(row.get("error") or ""),
                    )
                    pq_title = str(row.get("title") or pq_task_id or "Card").strip()
                    _append_history_entry(
                        items_map,
                        {
                            "id": pq_task_id or str(row.get("id") or ""),
                            "task_id": pq_task_id,
                            "job_id": str(row.get("id") or ""),
                            "title": pq_title,
                            "source": "priority_queue",
                            "status": _history_status_from_column(column),
                            "column": column,
                            "started_at": row.get("started_at"),
                            "finished_at": row.get("finished_at"),
                            "preview": (deliverable or str(row.get("error") or ""))[:500],
                            "deliverable": deliverable,
                            "execution_log": pq_log,
                            "error": str(row.get("error") or "").strip() or None,
                            "robot_slug": canonical,
                            "team_id": history_team_id(
                                task_id=pq_task_id,
                                explicit_team_id=str(row.get("team_id") or ""),
                                title=pq_title,
                            ),
                        },
                    )

    for board in boards:
        for task in board.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if _is_scheduling_lane_card(task, board):
                continue
            assignees = _task_assignees(task, alias_map)
            task_slug = assignees[0] if assignees else ""
            if not task_slug or not profile_matches(task_slug):
                continue
            completed_at = task.get("completed_at")
            col = _norm_slug(task.get("column"))
            if col in {"doing", "review"}:
                continue
            if not completed_at:
                continue
            if not _activity_within_window(
                finished_at=completed_at,
                started_at=completed_at,
                now=now,
                window_seconds=window_seconds,
            ):
                continue
            task_id = str(task.get("id") or "").strip()
            deliverable = _task_deliverable_text(task)
            board_team_id = str(board.get("team_id") or "").strip()
            _append_history_entry(
                items_map,
                {
                    "id": task_id,
                    "task_id": task_id,
                    "job_id": "",
                    "title": str(task.get("title") or task_title_index.get(task_id) or task_id),
                    "source": "kanban_card",
                    "status": "done",
                    "column": "done",
                    "started_at": completed_at,
                    "finished_at": completed_at,
                    "preview": deliverable[:500],
                    "deliverable": deliverable,
                    "execution_log": [],
                    "robot_slug": canonical,
                    "team_id": history_team_id(
                        task_id=task_id,
                        explicit_team_id=board_team_id,
                        title=str(task.get("title") or task_id),
                    ),
                },
            )

    items = sorted(items_map.values(), key=_history_sort_ts, reverse=True)
    if filter_task_id:
        items = [
            row
            for row in items
            if str(row.get("task_id") or "").strip().upper() == filter_task_id
            or _extract_task_id_token(str(row.get("title") or "")) == filter_task_id
        ]
    if filter_team_id:
        items = [
            row
            for row in items
            if str(row.get("team_id") or "").strip() == filter_team_id
        ]
    if limit > 0:
        items = items[:limit]

    return {
        "ok": True,
        "slug": canonical,
        "display_name": display_name,
        "window_days": int(window_seconds // 86400),
        "team_id": filter_team_id or None,
        "task_id": filter_task_id or None,
        "items": items,
        "total": len(items),
    }


def handle_swarm_agent_history(
    monitor: GoisMonitor,
    slug: str,
    user: Any = None,
    *,
    query: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    q = query or {}
    try:
        limit = int(q.get("limit") or _AGENT_HISTORY_LIMIT)
    except (TypeError, ValueError):
        limit = _AGENT_HISTORY_LIMIT
    limit = max(1, min(limit, 120))
    try:
        days = int(q.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 30))
    filter_team_id = str(q.get("team_id") or "").strip()
    filter_task_id = str(q.get("task_id") or "").strip()
    return build_agent_execution_history(
        monitor,
        slug,
        user,
        limit=limit,
        window_seconds=float(days * 86400),
        team_id=filter_team_id or None,
        task_id=filter_task_id or None,
    )

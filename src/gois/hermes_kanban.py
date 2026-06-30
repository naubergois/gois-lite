"""Kanban board load/save and task management for Hermes project agents."""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import HermesAgentCreateConfig
from .hermes_project_agents import resolve_kanban_path
from .team_presets import DEFAULT_KANBAN_COLUMNS

DEFAULT_COLUMNS: list[dict[str, str]] = list(DEFAULT_KANBAN_COLUMNS)
_COLUMN_KEYS = ("columns", "lanes", "raias")
_TASK_KEYS = ("tasks", "posts", "cards")

_TASK_ID_RE = re.compile(r"^TASK-\d{3,}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _merge_result_note(existing_notes: Any, result_comment: Any) -> str:
    existing = str(existing_notes or "").strip()
    comment = str(result_comment or "").strip()
    if not comment:
        return existing
    if not existing:
        return comment
    return f"{existing}\n\n{comment}"


def _require_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for kanban support") from exc
    return yaml


def _kanban_dumper(yaml_module: Any) -> type:
    """Dumper that never emits YAML anchors (tasks/posts duplicate row dicts)."""

    class KanbanDumper(yaml_module.SafeDumper):
        def ignore_aliases(self, data: object) -> bool:
            return True

    return KanbanDumper


def _pick_first_list(data: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def normalize_columns(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        return [dict(c) for c in DEFAULT_COLUMNS]
    cols: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or row.get("column") or row.get("name") or "").strip()
        title = str(row.get("title") or row.get("label") or cid).strip()
        if cid:
            cols.append({"id": cid, "title": title or cid})
    return cols or [dict(c) for c in DEFAULT_COLUMNS]


_COLUMN_ALIASES: dict[str, set[str]] = {
    "doing": {"doing", "in_progress", "em progresso"},
    "review": {"review", "revisao", "revisão"},
    "done": {"done", "concluido", "concluído"},
}


def resolve_work_column_id(
    board: dict[str, Any],
    purpose: str = "doing",
) -> str:
    """Return the board column id for a workflow stage (doing, review, done)."""
    wanted = _COLUMN_ALIASES.get(str(purpose or "").strip().lower(), {purpose})
    for col in board.get("columns") or []:
        if not isinstance(col, dict):
            continue
        cid = str(col.get("id") or "").strip()
        if cid and cid.lower() in wanted:
            return cid
    return str(purpose or "doing")


def _fallback_column_id(columns: list[dict[str, str]]) -> str:
    """Return a safe existing column id for tasks when input is invalid."""
    col_ids = [str(c.get("id") or "").strip().lower() for c in columns if isinstance(c, dict)]
    col_ids = [cid for cid in col_ids if cid]
    if "todo" in col_ids:
        return "todo"
    if "backlog" in col_ids:
        return "backlog"
    return col_ids[0] if col_ids else "todo"


def normalize_assignees(raw: Any) -> list[str]:
    if raw is None:
        return []
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
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def assignee_alias_keys(*, slug: str = "", display_name: str = "") -> set[str]:
    """Normalized keys used to match kanban assignees to profile slugs."""
    keys: set[str] = set()
    for raw in (slug, display_name):
        text = str(raw or "").strip().lower()
        if not text:
            continue
        keys.add(text)
        keys.add(text.replace("-", " "))
        keys.add(text.replace(" ", "-"))
    return keys


def task_assignee_keys(task: dict[str, Any]) -> set[str]:
    raw = task.get("assignees") or task.get("assignee") or []
    if isinstance(raw, str):
        raw = [raw]
    keys: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            text = str(item or "").strip()
            if text:
                keys.update(assignee_alias_keys(slug=text, display_name=text))
    return keys


def task_assigned_to_profile(
    task: dict[str, Any],
    *,
    slug: str,
    display_name: str = "",
) -> bool:
    profile_keys = assignee_alias_keys(slug=slug, display_name=display_name)
    return bool(task_assignee_keys(task) & profile_keys)


def resolve_assignee_profile_slug(
    assignee: str,
    profiles: list[dict[str, Any]],
    *,
    extra_slugs: Optional[list[str]] = None,
) -> str:
    """Map assignee token (slug or display name) to canonical profile slug."""
    text = str(assignee or "").strip()
    if not text:
        return ""
    input_keys = assignee_alias_keys(slug=text, display_name=text)
    for p in profiles:
        if not isinstance(p, dict):
            continue
        slug = str(p.get("name") or "").strip()
        if not slug:
            continue
        if slug == text:
            return slug
        display = str(p.get("display_name") or p.get("label") or slug).strip()
        profile_keys = assignee_alias_keys(slug=slug, display_name=display)
        if input_keys & profile_keys:
            return slug
    for slug in extra_slugs or []:
        key = str(slug or "").strip()
        if not key:
            continue
        if key == text:
            return key
        if input_keys & assignee_alias_keys(slug=key, display_name=key):
            return key
    return ""


def normalize_assignee_token(
    assignee: str,
    profiles: list[dict[str, Any]],
    *,
    extra_slugs: Optional[list[str]] = None,
) -> str:
    """Resolve assignee label to canonical profile slug (alias for jobs/queue/cron)."""
    return resolve_assignee_profile_slug(assignee, profiles, extra_slugs=extra_slugs)


_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "developer": ("implement", "código", "codigo", "feature", "bug", "api", "endpoint", "crud", "refactor"),
    "backend-dev": ("backend", "api", "endpoint", "servidor", "database", "sql"),
    "frontend-dev": ("frontend", "ui", "react", "css", "layout", "componente"),
    "tester": ("test", "teste", "qa", "validar", "cobertura", "regress"),
    "reviewer": ("review", "revisar", "aprovar", "qualidade", "feedback"),
    "devops": ("deploy", "pipeline", "ci", "cd", "infra", "docker", "kubernetes"),
    "writer": ("document", "readme", "artigo", "tutorial", "roteiro", "conteúdo", "conteudo"),
    "designer": ("design", "ui", "ux", "wireframe", "figma", "layout"),
    "researcher": ("pesquis", "investig", "analis", "explor", "benchmark"),
    "architect": ("arquitet", "design", "diagrama", "modelagem", "estrutura"),
    "security": ("segurança", "seguranca", "vulnerab", "audit", "auth", "lgpd"),
    "coordinator": ("coorden", "acompanh", "status", "report", "reunião", "reuniao"),
    "product-owner": ("requisit", "prioriz", "backlog", "stakeholder", "mvp", "roadmap"),
    "data-engineer": ("etl", "pipeline", "dados", "mongodb", "migra", "schema"),
    "support": ("suporte", "triagem", "ticket", "incidente"),
}


def _agent_score_for_task(agent: dict[str, Any], task: dict[str, Any]) -> int:
    slug = str(agent.get("slug") or "").strip().lower()
    display = str(agent.get("display_name") or "").strip().lower()
    role = str(agent.get("role") or agent.get("description") or "").strip().lower()
    text = " ".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            " ".join(str(s) for s in (task.get("skills") or [])),
        ]
    ).lower()
    score = 0
    for token in (slug, display, role):
        if token and token in text:
            score += 3
        if token and token.replace("-", " ") in text:
            score += 2
    for hint in _ROLE_HINTS.get(slug, ()):
        if hint in text:
            score += 2
    for skill in task.get("skills") or []:
        skill_text = str(skill or "").strip().lower()
        if not skill_text:
            continue
        if slug and slug in skill_text:
            score += 2
        if role and role in skill_text:
            score += 1
    return score


def suggest_assignee_for_task(
    task: dict[str, Any],
    agents: list[dict[str, Any]],
    *,
    load_counts: Optional[dict[str, int]] = None,
) -> str:
    """Pick the best agent for a task; tie-break by lowest current load."""
    candidates = [
        a
        for a in agents
        if isinstance(a, dict) and str(a.get("slug") or "").strip()
    ]
    if not candidates:
        return ""
    scored: list[tuple[int, int, str]] = []
    counts = load_counts or {}
    for agent in candidates:
        slug = str(agent.get("slug") or "").strip()
        score = _agent_score_for_task(agent, task)
        scored.append((score, counts.get(slug, 0), slug))
    scored.sort(key=lambda row: (-row[0], row[1], row[2].lower()))
    return scored[0][2]


def propose_task_assignments(
    tasks: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    *,
    columns: Optional[set[str]] = None,
    only_unassigned: bool = True,
) -> dict[str, str]:
    """Map task_id -> assignee slug using role hints and round-robin load balancing."""
    target_cols = columns or {"todo", "backlog"}
    load_counts: dict[str, int] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        col = str(task.get("column") or "").strip().lower()
        if col not in target_cols:
            continue
        for slug in normalize_assignees(task.get("assignees") or task.get("assignee")):
            load_counts[slug] = load_counts.get(slug, 0) + 1

    ordered = sorted(
        [t for t in tasks if isinstance(t, dict)],
        key=lambda t: (
            int(t.get("priority") or 99),
            str(t.get("id") or ""),
        ),
    )
    out: dict[str, str] = {}
    for task in ordered:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        col = str(task.get("column") or "").strip().lower()
        if col not in target_cols:
            continue
        if only_unassigned and normalize_assignees(task.get("assignees") or task.get("assignee")):
            continue
        assignee = suggest_assignee_for_task(task, agents, load_counts=load_counts)
        if not assignee:
            continue
        out[task_id] = assignee
        load_counts[assignee] = load_counts.get(assignee, 0) + 1
    return out


def normalize_skills(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[\n,;]", raw)
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parts:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def normalize_open_questions(raw: Any) -> list[dict[str, str]]:
    """Normalize a task's requirement clarifying questions.

    Accepts a list of strings or dicts and returns a list of
    ``{"question", "ask_to", "answer", "status"}`` entries.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [line for line in re.split(r"[\n]", raw) if line.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            question = item.strip().lstrip("-•*").strip()
            ask_to = ""
            answer = ""
        elif isinstance(item, dict):
            question = str(item.get("question") or item.get("q") or "").strip()
            ask_to = str(item.get("ask_to") or item.get("assignee") or "").strip()
            answer = str(item.get("answer") or item.get("a") or "").strip()
        else:
            continue
        if not question:
            continue
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "question": question,
                "ask_to": ask_to,
                "answer": answer,
                "status": "answered" if answer else "open",
            }
        )
    return out


TRELLO_LABEL_COLORS = frozenset({
    "green", "yellow", "orange", "red", "purple", "blue", "sky", "lime", "pink", "black",
})
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_card_color(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in TRELLO_LABEL_COLORS:
        return low
    if _HEX_COLOR_RE.match(text):
        return text
    return None


def normalize_labels(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part.strip() for part in re.split(r"[\n,;]", raw) if part.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            match = re.match(r"^([a-z#]+)\s*:\s*(.+)$", text, re.IGNORECASE)
            if match:
                color = match.group(1).strip().lower()
                name = match.group(2).strip()
            else:
                name = text
                color = ""
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("text") or item.get("label") or "").strip()
            color = str(item.get("color") or "").strip().lower()
        else:
            continue
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        label: dict[str, str] = {"name": name}
        if color in TRELLO_LABEL_COLORS:
            label["color"] = color
        out.append(label)
    return out


def normalize_due_date(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        return None


def normalize_due_complete(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def normalize_checklist(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [line.strip() for line in re.split(r"[\n]", raw) if line.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip().lstrip("-•*").strip()
            checked = False
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("name") or item.get("title") or "").strip()
            checked = normalize_due_complete(item.get("checked") or item.get("done"))
        else:
            continue
        if not text:
            continue
        out.append({"text": text, "checked": checked})
    return out


def normalize_performer_type(raw: Any) -> str:
    """Return ``human`` or ``agent`` (default ``agent`` for legacy boards)."""
    text = str(raw or "").strip().lower()
    if text in ("human", "humano", "person", "manual"):
        return "human"
    return "agent"


def is_human_task(task: dict[str, Any]) -> bool:
    """True when the card is meant to be done by a person, not an AI agent."""
    if not isinstance(task, dict):
        return False
    if normalize_performer_type(task.get("performer_type")) == "human":
        return True
    # Legacy: explicit human assignee prefix without performer_type.
    for ref in normalize_assignees(task.get("assignees") or task.get("assignee")):
        low = ref.lower()
        if low.startswith("user:") or low.startswith("contact:") or low.startswith("human:"):
            return True
    return False


def normalize_start_date(raw: Any) -> Optional[str]:
    """Normalize Trello-style start date (YYYY-MM-DD)."""
    return normalize_due_date(raw)


def normalize_comments(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [{"text": line.strip()} for line in re.split(r"[\n]", raw) if line.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            author = ""
            created_at = ""
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("comment") or item.get("body") or "").strip()
            author = str(item.get("author") or item.get("by") or "").strip()
            created_at = str(item.get("created_at") or item.get("at") or "").strip()
        else:
            continue
        if not text:
            continue
        row: dict[str, str] = {"text": text}
        if author:
            row["author"] = author
        if created_at:
            row["created_at"] = created_at
        out.append(row)
    return out


def normalize_task(row: dict[str, Any]) -> dict[str, Any]:
    task_id = str(row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not task_id:
        raise ValueError("task id is required")
    if not title:
        raise ValueError("task title is required")
    col = str(row.get("column") or row.get("status") or "todo").strip().lower()
    priority_raw = row.get("priority")
    priority: Optional[int] = None
    if priority_raw is not None and str(priority_raw).strip() != "":
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError):
            priority = None
    workdir_raw = row.get("workdir") or row.get("local_path") or row.get("folder")
    workdir: Optional[str] = None
    if workdir_raw is not None and str(workdir_raw).strip():
        workdir = str(workdir_raw).strip()

    cron_job_raw = row.get("cron_job_id") or row.get("cron_job")
    cron_job_id: Optional[str] = None
    if cron_job_raw is not None and str(cron_job_raw).strip():
        cron_job_id = str(cron_job_raw).strip()
    cron_schedule_raw = row.get("cron_schedule")
    cron_schedule: Optional[str] = None
    if cron_schedule_raw is not None and str(cron_schedule_raw).strip():
        cron_schedule = str(cron_schedule_raw).strip()

    model_id_raw = row.get("model_id")
    model_id: Optional[str] = None
    if model_id_raw is not None and str(model_id_raw).strip():
        model_id = str(model_id_raw).strip()

    attachments_raw = row.get("attachments")
    attachments: list[dict[str, Any]] = []
    if isinstance(attachments_raw, list):
        for att in attachments_raw:
            if not isinstance(att, dict):
                continue
            label = str(
                att.get("name") or att.get("file_name") or att.get("safe_name") or ""
            ).strip()
            if label:
                attachments.append(att)

    last_executor = str(
        row.get("last_executor") or row.get("executed_by") or ""
    ).strip()
    last_executed_at = row.get("last_executed_at")

    out: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "column": col,
        "priority": priority,
        "assignees": normalize_assignees(row.get("assignees") or row.get("assignee")),
        "swarm": str(row.get("swarm") or row.get("swarm_name") or "").strip(),
        "skills": normalize_skills(row.get("skills") or row.get("skill")),
        "description": str(row.get("description") or "").strip(),
        "notes": str(row.get("notes") or "").strip(),
        "requirements": str(row.get("requirements") or "").strip(),
        "implementation_details": str(
            row.get("implementation_details") or row.get("implementation") or ""
        ).strip(),
        "implementation_location": str(
            row.get("implementation_location")
            or row.get("app_location")
            or row.get("locate_in_app")
            or ""
        ).strip(),
        "open_questions": normalize_open_questions(row.get("open_questions")),
        "color": normalize_card_color(row.get("color") or row.get("cover_color")),
        "labels": normalize_labels(row.get("labels") or row.get("label")),
        "due_date": normalize_due_date(row.get("due_date") or row.get("due")),
        "due_complete": normalize_due_complete(row.get("due_complete")),
        "checklist": normalize_checklist(row.get("checklist") or row.get("checklists")),
        "comments": normalize_comments(row.get("comments") or row.get("observations")),
        "performer_type": normalize_performer_type(
            row.get("performer_type") or row.get("executor_type") or row.get("assignee_type")
        ),
        "start_date": normalize_start_date(row.get("start_date") or row.get("start")),
        "workdir": workdir,
        "model_id": model_id,
        "completed_at": row.get("completed_at"),
        "created_at": row.get("created_at"),
        "cron_job_id": cron_job_id,
        "cron_schedule": cron_schedule,
        "attachments": attachments,
    }
    if last_executor:
        out["last_executor"] = last_executor
    if last_executed_at is not None and str(last_executed_at).strip():
        out["last_executed_at"] = last_executed_at
    return out


def _normalize_board_result(
    raw: dict[str, Any], kanban_path: Path
) -> dict[str, Any]:
    """Normalize a raw board dict (from Mongo or YAML) into canonical form."""
    data = {
        "columns": raw.get("columns"),
        "tasks": raw.get("tasks"),
    }
    tasks_raw = _pick_first_list(data, _TASK_KEYS)
    tasks: list[dict[str, Any]] = []
    if isinstance(tasks_raw, list):
        for row in tasks_raw:
            if not isinstance(row, dict):
                continue
            try:
                tasks.append(normalize_task(row))
            except ValueError:
                continue
    columns = normalize_columns(_pick_first_list(data, _COLUMN_KEYS))
    fallback_col = _fallback_column_id(columns)
    col_ids = {
        str(c.get("id") or "").strip().lower()
        for c in columns
        if isinstance(c, dict)
    }
    for task in tasks:
        col = str(task.get("column") or "").strip().lower()
        if col and col in col_ids:
            continue
        task["column"] = fallback_col
    return {
        "columns": columns,
        "tasks": tasks,
        "path": str(kanban_path),
        "exists": True,
    }


def _import_yaml_to_mongo_once(kanban_path: Path) -> dict[str, Any] | None:
    """One-time import of a YAML kanban file into MongoDB.

    Returns the imported board dict or None if YAML doesn't exist.
    """
    from .kanban_mongo import save_board_by_path

    yaml = _require_yaml()
    if not kanban_path.is_file():
        return None
    try:
        data = yaml.safe_load(kanban_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    tasks_raw = _pick_first_list(data, _TASK_KEYS)
    tasks: list[dict[str, Any]] = []
    if isinstance(tasks_raw, list):
        for row in tasks_raw:
            if isinstance(row, dict):
                try:
                    tasks.append(normalize_task(row))
                except ValueError:
                    continue
    columns = normalize_columns(_pick_first_list(data, _COLUMN_KEYS))
    board = {"columns": columns, "tasks": tasks}
    save_board_by_path(kanban_path, board)
    return board


def load_kanban(kanban_path: Path) -> dict[str, Any]:
    """Load kanban board from MongoDB (single source of truth).

    If the board doesn't exist in Mongo but a YAML file exists on disk,
    performs a one-time import to Mongo and returns the result.
    """
    from .kanban_mongo import (
        load_board_by_path,
        mongo_kanban_enabled,
    )

    # 1. Try loading from MongoDB (canonical source)
    mongo_board = load_board_by_path(kanban_path, fallback_yaml=False)
    if mongo_board is not None:
        return _normalize_board_result(mongo_board, kanban_path)

    # 2. If Mongo is enabled but board not found, try one-time YAML import
    if mongo_kanban_enabled():
        imported = _import_yaml_to_mongo_once(kanban_path)
        if imported is not None:
            return _normalize_board_result(imported, kanban_path)
        # No YAML either — return empty board
        return {
            "columns": [dict(c) for c in DEFAULT_COLUMNS],
            "tasks": [],
            "path": str(kanban_path),
            "exists": False,
        }

    # 3. Mongo not available — graceful degradation to YAML (read-only)
    yaml = _require_yaml()
    if not kanban_path.is_file():
        return {
            "columns": [dict(c) for c in DEFAULT_COLUMNS],
            "tasks": [],
            "path": str(kanban_path),
            "exists": False,
        }
    try:
        data = yaml.safe_load(kanban_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read kanban: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"kanban YAML is invalid ({kanban_path}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        data = {}
    return _normalize_board_result(data, kanban_path)


def _build_task_row(task: dict[str, Any]) -> dict[str, Any]:
    """Build a serialisable task row from a normalized task dict."""
    row: dict[str, Any] = {
        "id": task["id"],
        "title": task["title"],
        "column": task["column"],
    }
    _OPTIONAL_KEYS = (
        "priority", "assignees", "swarm", "skills", "description",
        "notes", "requirements", "implementation_details",
        "implementation_location",
        "open_questions", "color", "labels", "due_date", "due_complete",
        "start_date", "checklist", "comments", "performer_type", "workdir", "model_id",
        "completed_at", "created_at", "cron_job_id", "cron_schedule",
        "last_executor", "last_executed_at", "attachments",
    )
    for key in _OPTIONAL_KEYS:
        val = task.get(key)
        if val is not None and val != "" and val != [] and val != {}:
            row[key] = val
        elif key == "priority" and val is not None:
            row[key] = val
    return row


def save_kanban(kanban_path: Path, board: dict[str, Any]) -> None:
    """Persist kanban board to MongoDB (single source of truth).

    YAML is no longer written.  MongoDB is the canonical store.
    """
    from .kanban_mongo import (
        mongo_kanban_enabled,
        save_board_by_path,
    )

    columns = normalize_columns(board.get("columns"))
    tasks_raw = board.get("tasks") or []
    tasks: list[dict[str, Any]] = []
    if isinstance(tasks_raw, list):
        for row in tasks_raw:
            if isinstance(row, dict):
                tasks.append(normalize_task(row))

    task_rows = [_build_task_row(t) for t in tasks]

    saved = save_board_by_path(
        kanban_path,
        {"columns": columns, "tasks": task_rows},
    )
    if saved:
        return

    if mongo_kanban_enabled():
        raise ValueError(
            f"failed to persist kanban board to MongoDB ({kanban_path})"
        )

    # Mongo not available — graceful degradation to YAML
    yaml = _require_yaml()
    payload: dict[str, Any] = {
        "columns": columns,
        "raias": columns,
        "tasks": task_rows,
    }
    kanban_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(
        payload,
        Dumper=_kanban_dumper(yaml),
        sort_keys=False,
        allow_unicode=True,
    )
    _atomic_write_text(kanban_path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and concurrency-safely.

    The swarm runs several Hermes agents that move/update cards on the same
    board at once. A fixed ``.tmp`` sibling would be clobbered by overlapping
    writers, leaving a torn file that fails to parse (YAML ScannerError). A
    unique temp file per write plus an atomic ``os.replace`` guarantees readers
    only ever see a complete document (last writer wins; never corruption).
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def next_task_id(tasks: list[dict[str, Any]]) -> str:
    max_num = 0
    for task in tasks:
        tid = str(task.get("id") or "")
        m = _TASK_ID_RE.match(tid)
        if m:
            try:
                max_num = max(max_num, int(tid.split("-", 1)[1]))
            except ValueError:
                pass
    return f"TASK-{max_num + 1:03d}"


def team_agents_from_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build assignee options from Hermes profile rows."""
    agents: list[dict[str, str]] = []
    seen: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        slug = str(profile.get("name") or "").strip()
        if not slug or slug == "default" or slug in seen:
            continue
        seen.add(slug)
        display = str(profile.get("display_name") or "").strip()
        mascot = str(profile.get("mascot") or "").strip()
        agents.append({
            "slug": slug,
            "display_name": display or slug,
            "mascot": mascot,
        })
    agents.sort(key=lambda a: a["display_name"].lower())
    return agents


def projects_from_profiles(
    profiles: list[dict[str, Any]],
    create_cfg: HermesAgentCreateConfig,
) -> list[dict[str, Any]]:
    """Discover kanban project workdirs from profile metadata."""
    projects: list[dict[str, Any]] = []
    seen: set[str] = set()

    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        slug = str(profile.get("name") or "").strip()
        if not slug or slug == "default":
            continue

        workdir: Optional[str] = None
        kanban_file: Optional[str] = None
        github: Optional[str] = None

        project = profile.get("project")
        if isinstance(project, dict):
            workdir = str(project.get("workdir") or project.get("local_path") or "").strip() or None
            kanban_file = str(project.get("kanban_file") or "").strip() or None
            github = str(project.get("github") or "").strip() or None

        meta_kanban = profile.get("kanban_file")
        if meta_kanban and not kanban_file:
            kanban_file = str(meta_kanban).strip() or None

        if not workdir:
            continue
        if workdir in seen:
            continue
        seen.add(workdir)

        wd = Path(workdir).expanduser()
        kanban_path = resolve_kanban_path(wd, create_cfg)
        if kanban_file:
            kanban_path = wd / kanban_file

        display = str(profile.get("display_name") or slug).strip()
        label = display
        if github:
            label = f"{display} — {github}"

        projects.append({
            "profile": slug,
            "workdir": str(wd.resolve()),
            "label": label,
            "kanban_file": kanban_file_for_project(wd.resolve(), kanban_path.resolve()),
            "github": github,
        })

    projects.sort(key=lambda p: p["label"].lower())
    return projects


def kanban_file_for_project(workdir: Path, kanban_path: Path) -> str:
    """Return kanban path relative to workdir, or absolute when stored elsewhere."""
    wd = workdir.expanduser().resolve()
    kp = kanban_path.expanduser().resolve()
    if kp.is_relative_to(wd):
        return str(kp.relative_to(wd))
    return str(kp)


def resolve_board_paths(
    workdir: str,
    create_cfg: HermesAgentCreateConfig,
    *,
    kanban_file: Optional[str] = None,
) -> tuple[Path, Path]:
    wd = Path(workdir).expanduser().resolve()
    if not wd.is_dir():
        raise ValueError(f"workdir não existe: {wd}")
    if kanban_file:
        kf = Path(str(kanban_file).strip()).expanduser()
        if kf.is_absolute():
            return wd, kf.resolve()
        kp = (wd / kanban_file).resolve()
        if not str(kp).startswith(str(wd)):
            raise ValueError("kanban_file inválido")
        return wd, kp
    return wd, resolve_kanban_path(wd, create_cfg)


_STORED_ATT_NAME_RE = re.compile(r"^\d{10,}_(.+)$")


def _display_name_from_stored(stored_name: str) -> str:
    name = str(stored_name or "").strip()
    match = _STORED_ATT_NAME_RE.match(name)
    return (match.group(1) if match else name) or "file"


def _abs_stored_path(path: Path) -> str:
    return str(path.expanduser().absolute())


def _team_relative_attachment_path(stored: str, kanban_path: Path) -> Optional[Path]:
    """Map legacy absolute paths to the current team ``.kanban-attachments`` tree."""
    parts = Path(str(stored or "").strip()).parts
    marker = ".kanban-attachments"
    if marker not in parts:
        return None
    idx = parts.index(marker)
    candidate = kanban_path.parent / Path(*parts[idx:])
    return candidate if candidate.is_file() else None


def _resolve_stored_attachment_path(
    stored: str,
    *,
    att_dir: Path,
    kanban_path: Path,
) -> Optional[Path]:
    """Resolve ``stored_path`` metadata to an on-disk file (absolute)."""
    raw = str(stored or "").strip()
    if not raw:
        return None

    p = Path(raw).expanduser()
    candidates: list[Path] = [p]
    team_rel = _team_relative_attachment_path(raw, kanban_path)
    if team_rel is not None:
        candidates.append(team_rel)
    if not p.is_absolute():
        candidates.append(att_dir / p)
        candidates.append(kanban_path.parent / p)
        if raw.startswith(".stack/") or raw.startswith(".stack\\"):
            candidates.append(Path.cwd() / p)
            candidates.append(kanban_path.parent.parent.parent.parent / p)

    seen: set[str] = set()
    for cand in candidates:
        try:
            probe = cand.expanduser()
        except OSError:
            continue
        pkey = str(probe)
        if pkey in seen:
            continue
        seen.add(pkey)
        if probe.is_file():
            return probe
    return None


def _linked_attachment_paths(
    att_dir: Path,
    attachments: list[Any],
) -> set[str]:
    linked: set[str] = set()
    for att in attachments:
        if not isinstance(att, dict):
            continue
        safe_file = str(att.get("safe_name") or "").strip()
        display = str(att.get("name") or att.get("file_name") or "").strip()
        resolved = _resolve_attachment_path(att_dir, safe_file, display)
        if resolved is not None:
            linked.add(str(resolved.resolve()))
        stored = str(att.get("stored_path") or att.get("path") or "").strip()
        if stored:
            try:
                stored_path = Path(stored).expanduser().resolve()
            except OSError:
                continue
            if stored_path.is_file():
                linked.add(str(stored_path))
    return linked


def _meta_from_orphan_file(file_path: Path, *, att_dir: Optional[Path] = None) -> dict[str, Any]:
    mime_type, _ = mimetypes.guess_type(file_path.name)
    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0
    if att_dir is not None:
        rel = _relative_attachment_label(att_dir, file_path)
        safe_name = rel if "/" in rel else file_path.name
        display = rel if "/" in rel else _display_name_from_stored(file_path.name)
    else:
        display = _display_name_from_stored(file_path.name)
        safe_name = file_path.name
    return {
        "name": display,
        "safe_name": safe_name,
        "mime_type": mime_type or "application/octet-stream",
        "size": size,
        "stored_path": str(file_path.resolve()),
        "uploaded_at": _now_iso(),
        "source": "disk_sync",
    }


_LIGHT_DESC_MAX = 200


def strip_board_for_light_ui(board: dict[str, Any]) -> dict[str, Any]:
    """Drop heavy task fields for the Kanban column view (``light=1``)."""
    tasks = board.get("tasks")
    if not isinstance(tasks, list):
        return board
    light_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        row = dict(task)
        desc = str(row.get("description") or "").strip()
        if len(desc) > _LIGHT_DESC_MAX:
            row["description"] = desc[: _LIGHT_DESC_MAX - 1] + "…"
        if str(row.get("notes") or "").strip():
            row["notes"] = "…"
        for key in (
            "requirements",
            "implementation_details",
            "implementation_location",
            "comments",
        ):
            row.pop(key, None)
        open_q = row.get("open_questions")
        if isinstance(open_q, list) and open_q:
            row["open_questions"] = [{"_light": True}]
        light_tasks.append(row)
    out = dict(board)
    out["tasks"] = light_tasks
    return out


def reconcile_board_attachments(
    board: dict[str, Any],
    kanban_path: Path,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Prune dead attachment metadata, fix legacy paths, and index on-disk orphans."""
    att_base = kanban_path.parent / ".kanban-attachments"
    pruned = 0
    fixed_paths = 0
    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
        att_dir = att_base / safe_id
        related_dirs = _related_attachment_dirs(att_base, task_id)
        attachments = task.get("attachments")
        if not isinstance(attachments, list):
            attachments = []
        kept: list[dict[str, Any]] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            safe_file = str(att.get("safe_name") or "").strip()
            display = str(att.get("name") or att.get("file_name") or "").strip()
            stored = str(att.get("stored_path") or att.get("path") or "").strip()
            team_rel = _team_relative_attachment_path(stored, kanban_path)
            if team_rel is not None:
                new_path = _abs_stored_path(team_rel)
                if stored != new_path:
                    fixed_paths += 1
                att["stored_path"] = new_path
                stored = new_path
            resolved = _resolve_attachment_path_for_task(
                att_base,
                task_id,
                safe_file,
                display,
                stored,
            )
            if resolved is None and stored:
                resolved = _resolve_stored_attachment_path(
                    stored,
                    att_dir=att_dir,
                    kanban_path=kanban_path,
                )
            if resolved is not None:
                new_path = _abs_stored_path(resolved)
                if att.get("stored_path") != new_path:
                    fixed_paths += 1
                att["stored_path"] = new_path
                if not safe_file:
                    att["safe_name"] = resolved.name
                kept.append(att)
                continue
            pruned += 1
        task["attachments"] = kept

    enrich_board_attachments(
        board,
        kanban_path,
        persist_orphans=False,
        scan_orphans=True,
    )
    if persist:
        save_kanban(kanban_path, board)
    return {
        "ok": True,
        "pruned": pruned,
        "fixed_paths": fixed_paths,
        "kanban_path": str(kanban_path),
    }


def bootstrap_reconcile_team_attachments(scope: str) -> dict[str, Any]:
    """Repair attachment metadata for every team board under ``scope``."""
    from .accounts_team_guard import _scan_team_dirs
    from .kanban_mongo import mongo_kanban_enabled

    if not mongo_kanban_enabled():
        return {"ok": True, "skipped": "mongo_kanban_disabled", "teams": 0}

    data_dir = Path(scope).expanduser().resolve()
    canonical_dirs, _ = _scan_team_dirs(data_dir)
    results: list[dict[str, Any]] = []
    totals = {"teams": 0, "pruned": 0, "fixed_paths": 0}

    for team_id in sorted(canonical_dirs):
        kanban_path = canonical_dirs[team_id] / "kanban.yaml"
        try:
            board = load_kanban(kanban_path)
        except Exception as exc:
            results.append({"team_id": team_id, "ok": False, "error": str(exc)})
            continue
        if not board.get("tasks"):
            continue
        row = reconcile_board_attachments(board, kanban_path, persist=True)
        totals["teams"] += 1
        totals["pruned"] += int(row.get("pruned") or 0)
        totals["fixed_paths"] += int(row.get("fixed_paths") or 0)
        if int(row.get("pruned") or 0) or int(row.get("fixed_paths") or 0):
            results.append({"team_id": team_id, **row})

    return {"ok": True, **totals, "repaired": results}


def enrich_board_attachments(
    board: dict[str, Any],
    kanban_path: Path,
    *,
    persist_orphans: bool = True,
    scan_orphans: bool = True,
) -> dict[str, Any]:
    """Ensure each task attachment has ``stored_path`` for UI download/preview.

    Files present under ``.kanban-attachments/<task_id>/`` but missing from
    task metadata are indexed automatically (orphan recovery).
    """
    att_base = kanban_path.parent / ".kanban-attachments"
    orphans_added = False
    paths_normalized = False
    for task in board.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
        att_dir = att_base / safe_id
        related_dirs = _related_attachment_dirs(att_base, task_id)
        attachments = task.get("attachments")
        if not isinstance(attachments, list):
            attachments = []
            task["attachments"] = attachments
        for att in attachments:
            if not isinstance(att, dict):
                continue
            safe_file = str(att.get("safe_name") or "").strip()
            display = str(att.get("name") or att.get("file_name") or "").strip()
            resolved = _resolve_attachment_path_for_task(
                att_base,
                task_id,
                safe_file,
                display,
            )
            if resolved is not None:
                new_path = _abs_stored_path(resolved)
                if att.get("stored_path") != new_path:
                    paths_normalized = True
                att["stored_path"] = new_path
                if not safe_file:
                    att["safe_name"] = resolved.name
                continue
            stored = str(att.get("stored_path") or att.get("path") or "").strip()
            if stored:
                stored_resolved = _resolve_stored_attachment_path(
                    stored,
                    att_dir=att_dir,
                    kanban_path=kanban_path,
                )
                if stored_resolved is not None:
                    new_path = _abs_stored_path(stored_resolved)
                    if att.get("stored_path") != new_path:
                        paths_normalized = True
                    att["stored_path"] = new_path
                    continue
            if safe_file:
                fallback = _resolve_attachment_path_for_task(
                    att_base,
                    task_id,
                    safe_file,
                )
                if fallback is not None:
                    new_path = _abs_stored_path(fallback)
                else:
                    new_path = _abs_stored_path(att_dir / safe_file)
                if att.get("stored_path") != new_path:
                    paths_normalized = True
                att["stored_path"] = new_path
        if not scan_orphans or not related_dirs:
            continue
        linked: set[str] = set()
        for rel_dir in related_dirs:
            linked |= _linked_attachment_paths(rel_dir, attachments)
        for rel_dir in related_dirs:
            for file_path in sorted(_attachment_files(rel_dir), key=lambda p: p.as_posix()):
                key = str(file_path.resolve())
                if key in linked:
                    continue
                attachments.append(_meta_from_orphan_file(file_path, att_dir=rel_dir))
                linked.add(key)
                orphans_added = True
    if scan_orphans and att_base.is_dir():
        loose_linked = {
            str(Path(str(att.get("stored_path") or "")).expanduser().resolve())
            for task in board.get("tasks") or []
            if isinstance(task, dict)
            for att in (task.get("attachments") or [])
            if isinstance(att, dict) and att.get("stored_path")
        }
        for file_path in sorted(
            (p for p in att_base.iterdir() if p.is_file()),
            key=lambda p: p.as_posix(),
        ):
            key = str(file_path.resolve())
            if key in loose_linked:
                continue
            task_id = _match_loose_attachment_task(file_path, board.get("tasks") or [])
            if not task_id:
                continue
            for task in board.get("tasks") or []:
                if not isinstance(task, dict) or str(task.get("id") or "") != task_id:
                    continue
                attachments = task.get("attachments")
                if not isinstance(attachments, list):
                    attachments = []
                    task["attachments"] = attachments
                attachments.append(_meta_from_orphan_file(file_path, att_dir=att_base))
                loose_linked.add(key)
                orphans_added = True
                break
    if scan_orphans and persist_orphans and (orphans_added or paths_normalized):
        save_kanban(kanban_path, board)
    return board


def get_board(
    workdir: str,
    create_cfg: HermesAgentCreateConfig,
    *,
    kanban_file: Optional[str] = None,
    light: bool = False,
) -> dict[str, Any]:
    wd, kp = resolve_board_paths(workdir, create_cfg, kanban_file=kanban_file)
    board = load_kanban(kp)
    board["workdir"] = str(wd)
    board["kanban_file"] = str(kp.relative_to(wd)) if kp.is_relative_to(wd) else kp.name
    if light:
        board = enrich_board_attachments(
            board, kp, persist_orphans=False, scan_orphans=False
        )
        return strip_board_for_light_ui(board)
    return enrich_board_attachments(board, kp)


def apply_kanban_action(
    workdir: str,
    create_cfg: HermesAgentCreateConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Mutate kanban board according to action payload."""
    action = str(payload.get("action") or "").strip().lower()
    kanban_file = payload.get("kanban_file")
    kanban_file_str = str(kanban_file).strip() if kanban_file else None

    _, kp = resolve_board_paths(
        workdir,
        create_cfg,
        kanban_file=kanban_file_str,
    )
    board = load_kanban(kp)
    tasks: list[dict[str, Any]] = list(board.get("tasks") or [])
    columns = board.get("columns") or DEFAULT_COLUMNS
    col_ids = {str(c.get("id") or "").strip().lower() for c in columns}
    fallback_col = _fallback_column_id(list(columns))

    created_task_id: Optional[str] = None
    auto_assignments_applied: list[dict[str, str]] = []
    if action == "create_task":
        task_raw = payload.get("task")
        if not isinstance(task_raw, dict):
            raise ValueError("task object is required")
        task = dict(task_raw)
        if not str(task.get("id") or "").strip():
            task["id"] = next_task_id(tasks)
        task.setdefault("column", "todo")
        task.setdefault("created_at", _now_iso())
        normalized = normalize_task(task)
        if any(t["id"] == normalized["id"] for t in tasks):
            raise ValueError(f"tarefa {normalized['id']} já existe")
        if normalized["column"] not in col_ids:
            normalized["column"] = fallback_col
        tasks.append(normalized)
        created_task_id = normalized["id"]
    elif action == "update_task":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        task_raw = payload.get("task")
        if not task_id or not isinstance(task_raw, dict):
            raise ValueError("task_id and task object are required")
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            raise ValueError(f"tarefa {task_id} não encontrada")
        merged = {**tasks[idx], **task_raw, "id": task_id}
        normalized = normalize_task(merged)
        if normalized["column"] not in col_ids:
            previous_col = str(tasks[idx].get("column") or "").strip().lower()
            normalized["column"] = previous_col if previous_col in col_ids else fallback_col
        if normalized["column"] == "done" and not normalized.get("completed_at"):
            normalized["completed_at"] = _now_iso()
        elif normalized["column"] != "done":
            normalized["completed_at"] = None
        tasks[idx] = normalized
    elif action == "move_task":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        column = str(payload.get("column") or "").strip().lower()
        if not task_id or not column:
            raise ValueError("task_id and column are required")
        if column not in col_ids:
            raise ValueError(f"coluna desconhecida: {column}")
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            raise ValueError(f"tarefa {task_id} não encontrada")
        tasks[idx]["column"] = column
        if column == "done" and not tasks[idx].get("completed_at"):
            tasks[idx]["completed_at"] = _now_iso()
        elif column != "done":
            tasks[idx]["completed_at"] = None
    elif action == "complete_task":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            raise ValueError(f"tarefa {task_id} não encontrada")
        result_comment = (
            payload.get("result_comment")
            or payload.get("comment")
            or payload.get("result")
            or payload.get("notes")
        )
        tasks[idx]["column"] = "done"
        tasks[idx]["completed_at"] = _now_iso()
        tasks[idx]["notes"] = _merge_result_note(tasks[idx].get("notes"), result_comment)
    elif action == "assign_task":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        assignees = normalize_assignees(payload.get("assignees"))
        if not task_id:
            raise ValueError("task_id is required")
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            raise ValueError(f"tarefa {task_id} não encontrada")
        tasks[idx]["assignees"] = assignees
    elif action == "set_card_swarm":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        swarm = str(payload.get("swarm") or payload.get("swarm_name") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            raise ValueError(f"tarefa {task_id} não encontrada")
        tasks[idx]["swarm"] = swarm
    elif action == "auto_assign_tasks":
        agents_raw = payload.get("agents") or []
        agents: list[dict[str, Any]] = [
            a for a in agents_raw if isinstance(a, dict) and str(a.get("slug") or "").strip()
        ]
        if not agents:
            raise ValueError("agents list is required for auto_assign_tasks")
        raw_cols = payload.get("columns")
        columns: Optional[set[str]] = None
        if isinstance(raw_cols, list):
            columns = {str(c).strip().lower() for c in raw_cols if str(c).strip()}
        only_unassigned = payload.get("only_unassigned", True)
        if isinstance(only_unassigned, str):
            only_unassigned = only_unassigned.strip().lower() not in ("0", "false", "no", "off")
        assignments = propose_task_assignments(
            tasks,
            agents,
            columns=columns,
            only_unassigned=bool(only_unassigned),
        )
        applied: list[dict[str, str]] = []
        for task_id, assignee in assignments.items():
            idx = next((i for i, t in enumerate(tasks) if t.get("id") == task_id), None)
            if idx is None:
                continue
            tasks[idx]["assignees"] = [assignee]
            applied.append({"task_id": task_id, "assignee": assignee})
        auto_assignments_applied = applied
    elif action == "delete_task":
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        before = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) == before:
            raise ValueError(f"tarefa {task_id} não encontrada")
    elif action == "delete_tasks":
        task_ids_to_delete = payload.get("task_ids") or payload.get("ids") or []
        if not task_ids_to_delete or not isinstance(task_ids_to_delete, list):
            raise ValueError("task_ids (list) is required")
        task_ids_to_delete = {str(tid).strip() for tid in task_ids_to_delete if tid}
        if not task_ids_to_delete:
            raise ValueError("task_ids must contain at least one valid id")
        deleted_ids = {
            t["id"] for t in tasks if t.get("id") in task_ids_to_delete
        }
        tasks = [t for t in tasks if t.get("id") not in task_ids_to_delete]
        if not deleted_ids:
            raise ValueError("nenhuma tarefa encontrada com os IDs fornecidos")
    elif action == "add_column":
        title = str(payload.get("title") or payload.get("name") or "").strip()
        if not title:
            raise ValueError("title is required for add_column")
        raw_id = str(payload.get("column_id") or payload.get("id") or "").strip()
        if raw_id:
            col_id = raw_id.lower()
        else:
            col_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "lista"
        if col_id in col_ids:
            raise ValueError(f"coluna {col_id} já existe")
        columns = list(columns) + [{"id": col_id, "title": title}]
        board["columns"] = columns
    elif action == "rename_column":
        column_id = str(payload.get("column_id") or payload.get("id") or "").strip().lower()
        title = str(payload.get("title") or payload.get("name") or "").strip()
        if not column_id or not title:
            raise ValueError("column_id and title are required for rename_column")
        found = False
        new_columns: list[dict[str, Any]] = []
        for col in columns:
            if not isinstance(col, dict):
                continue
            cid = str(col.get("id") or "").strip().lower()
            if cid == column_id:
                new_columns.append({**col, "id": cid, "title": title})
                found = True
            else:
                new_columns.append(col)
        if not found:
            raise ValueError(f"coluna {column_id} não encontrada")
        columns = new_columns
        board["columns"] = columns
    elif action == "delete_column":
        column_id = str(payload.get("column_id") or payload.get("id") or "").strip().lower()
        if not column_id:
            raise ValueError("column_id is required for delete_column")
        if column_id not in col_ids:
            raise ValueError(f"coluna {column_id} não encontrada")
        if len(col_ids) <= 1:
            raise ValueError("não é possível apagar a única coluna do quadro")
        move_to = str(payload.get("move_to") or payload.get("target_column") or "").strip().lower()
        if not move_to or move_to == column_id or move_to not in col_ids:
            move_to = next(
                (str(c.get("id") or "").strip().lower() for c in columns
                 if str(c.get("id") or "").strip().lower() != column_id),
                fallback_col,
            )
        for task in tasks:
            if str(task.get("column") or "").strip().lower() == column_id:
                task["column"] = move_to
        board["columns"] = [
            c for c in columns
            if isinstance(c, dict) and str(c.get("id") or "").strip().lower() != column_id
        ]
    elif action == "save_board":
        incoming = payload.get("board")
        if not isinstance(incoming, dict):
            raise ValueError("board object is required")
        board["columns"] = normalize_columns(_pick_first_list(incoming, _COLUMN_KEYS))
        existing_by_id = {
            str(t.get("id") or ""): t for t in tasks if isinstance(t, dict) and t.get("id")
        }
        tasks = []
        for row in _pick_first_list(incoming, _TASK_KEYS):
            if isinstance(row, dict):
                normalized = normalize_task(row)
                tid = str(normalized.get("id") or "")
                prev = existing_by_id.get(tid) or {}
                if not normalized.get("attachments") and prev.get("attachments"):
                    normalized["attachments"] = prev["attachments"]
                tasks.append(normalized)
    else:
        raise ValueError(
            "action must be create_task, update_task, move_task, complete_task, "
            "assign_task, set_card_swarm, auto_assign_tasks, delete_task, "
            "delete_tasks, add_column, rename_column, delete_column, or save_board"
        )

    board["tasks"] = tasks
    enrich_board_attachments(board, kp, persist_orphans=False)
    save_kanban(kp, board)
    result = get_board(workdir, create_cfg, kanban_file=kanban_file_str)
    result["action"] = action
    if created_task_id:
        result["task_id"] = created_task_id
    if action == "delete_tasks":
        result["deleted_ids"] = list(deleted_ids)
        result["deleted_count"] = len(deleted_ids)
    if auto_assignments_applied:
        result["assignments"] = auto_assignments_applied
        result["assigned_count"] = len(auto_assignments_applied)
    return result


# ── Attachment helpers ──────────────────────────────────────────────────────


def _attachments_dir(kanban_path: Path, task_id: str) -> Path:
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    return kanban_path.parent / ".kanban-attachments" / safe_id


def _related_attachment_dirs(att_base: Path, task_id: str) -> list[Path]:
    """Return the primary task dir plus sibling aux dirs (e.g. ``TASK-106-figures``)."""
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    dirs: list[Path] = []
    seen: set[str] = set()
    primary = att_base / safe_id
    if primary.is_dir():
        dirs.append(primary)
        seen.add(str(primary.resolve()))
    prefix = f"{safe_id}-"
    if att_base.is_dir():
        for entry in sorted(att_base.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(prefix):
                continue
            key = str(entry.resolve())
            if key not in seen:
                dirs.append(entry)
                seen.add(key)
    return dirs


def _resolve_attachment_path_for_task(
    att_base: Path,
    task_id: str,
    *names: str,
) -> Optional[Path]:
    for att_dir in _related_attachment_dirs(att_base, task_id):
        hit = _resolve_attachment_path(att_dir, *names)
        if hit is not None:
            return hit
    return None


def _match_loose_attachment_task(
    file_path: Path,
    tasks: list[Any],
) -> str:
    """Map a file sitting directly under ``.kanban-attachments/`` to a task id."""
    name = file_path.name.lower()
    match = re.search(r"(task[-_]\d+)", name, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper().replace("_", "-")
    tokens = [tok for tok in re.split(r"[^\w]+", name) if len(tok) >= 4]
    best_id = ""
    best_score = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        hay = f"{task.get('title') or ''} {task.get('description') or ''}".lower()
        score = sum(1 for tok in tokens if tok in hay)
        if score > best_score:
            best_score = score
            best_id = task_id
    return best_id if best_score > 0 else ""


def _safe_att_name(name: str) -> str:
    base = Path(name).name or "file"
    safe = re.sub(r"[^\w\-. ]+", "_", base).strip("._")
    return (safe[:200] or "file")


def _attachment_files(att_dir: Path) -> list[Path]:
    """All regular files under a task attachment directory (includes subfolders)."""
    if not att_dir.is_dir():
        return []
    return [p for p in att_dir.rglob("*") if p.is_file()]


def _relative_attachment_label(att_dir: Path, file_path: Path) -> str:
    try:
        rel = file_path.resolve().relative_to(att_dir.resolve())
        return rel.as_posix()
    except ValueError:
        return file_path.name


def _resolve_attachment_path(att_dir: Path, *names: str) -> Optional[Path]:
    """Locate an attachment file when metadata uses display name instead of stored safe_name."""
    if not att_dir.is_dir():
        return None
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in names:
        for variant in (
            str(raw or "").strip(),
            re.sub(r"[^\w\-. ]+", "_", str(raw or "").strip())[:220],
            Path(str(raw or "")).name,
        ):
            if variant and variant not in seen:
                seen.add(variant)
                candidates.append(variant)
    if not candidates:
        return None
    files = _attachment_files(att_dir)
    for name in candidates:
        direct = att_dir / name
        if direct.is_file():
            return direct
        rel_direct = (att_dir / Path(name)).resolve()
        if rel_direct.is_file() and _path_under_or_equal_literal(rel_direct, att_dir):
            return rel_direct
        basename = Path(name).name
        matches = [
            p
            for p in files
            if p.name == name
            or p.name == basename
            or p.name.endswith(f"_{name}")
            or p.name.endswith(f"_{basename}")
            or _relative_attachment_label(att_dir, p) == name
            or _relative_attachment_label(att_dir, p).endswith(f"/{name}")
            or _relative_attachment_label(att_dir, p).endswith(f"/{basename}")
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            matches.sort(key=lambda p: p.name, reverse=True)
            return matches[0]
    return None


def save_kanban_attachment_bytes(
    kanban_path: Path,
    task_id: str,
    file_name: str,
    mime_type: str,
    data: bytes,
) -> dict[str, Any]:
    """Persist raw bytes to disk and update task attachments list."""
    att_dir = _attachments_dir(kanban_path, task_id)
    att_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_att_name(file_name)
    ts = int(time.time() * 1000)
    dest = att_dir / f"{ts}_{safe_name}"
    dest.write_bytes(data)

    board = load_kanban(kanban_path)
    tasks: list[dict[str, Any]] = list(board.get("tasks") or [])
    idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
    if idx is None:
        dest.unlink(missing_ok=True)
        raise ValueError(f"tarefa {task_id} não encontrada")

    att_meta: dict[str, Any] = {
        "name": file_name,
        "safe_name": dest.name,
        "mime_type": mime_type,
        "size": len(data),
        "uploaded_at": _now_iso(),
        "stored_path": _abs_stored_path(dest),
    }
    existing = tasks[idx].get("attachments") or []
    if not isinstance(existing, list):
        existing = []
    existing.append(att_meta)
    tasks[idx]["attachments"] = existing
    board["tasks"] = tasks
    save_kanban(kanban_path, board)
    return att_meta


def save_kanban_attachment(
    kanban_path: Path,
    task_id: str,
    file_name: str,
    mime_type: str,
    data_b64: str,
) -> dict[str, Any]:
    """Decode base64 data and persist to disk; update task attachments list."""
    # Strip data-URL prefix if present (e.g. "data:image/png;base64,...")
    if data_b64.startswith("data:"):
        _, _, data_b64 = data_b64.partition(",")
    # Remove whitespace/newlines that may have been injected
    data_b64 = data_b64.strip()
    try:
        data = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        raise ValueError(f"base64 inválido: {exc}") from exc
    return save_kanban_attachment_bytes(kanban_path, task_id, file_name, mime_type, data)


def _kanban_attachments_root(kanban_path: Path) -> Path:
    return kanban_path.parent / ".kanban-attachments"


def _path_under_or_equal(child: Path, base: Path) -> bool:
    try:
        child_r = child.resolve()
        base_r = base.resolve()
    except OSError:
        return False
    if child_r == base_r:
        return True
    try:
        return child_r.is_relative_to(base_r)
    except (ValueError, AttributeError):
        return str(child_r).startswith(str(base_r).rstrip("/") + "/")


def _path_under_or_equal_literal(child: Path, base: Path) -> bool:
    """Like ``_path_under_or_equal`` but without following symlinks."""
    try:
        child_r = child.absolute()
        base_r = base.absolute()
    except OSError:
        return False
    if child_r == base_r:
        return True
    try:
        return child_r.is_relative_to(base_r)
    except (ValueError, AttributeError):
        return str(child_r).startswith(str(base_r).rstrip("/") + "/")


def get_kanban_attachment(
    kanban_path: Path,
    task_id: str,
    safe_name: str,
) -> tuple[bytes, str]:
    """Return (file_bytes, mime_type) for a task attachment."""
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    safe_file = re.sub(r"[^\w\-. ]+", "_", safe_name)[:220]
    att_dir = _kanban_attachments_root(kanban_path) / safe_id
    att_path = _resolve_attachment_path(att_dir, safe_file, safe_name.strip())
    if att_path is None:
        raise FileNotFoundError(f"attachment not found: {safe_file}")
    data = att_path.read_bytes()
    mime = mimetypes.guess_type(att_path.name)[0] or "application/octet-stream"
    return data, mime


def get_kanban_attachment_from_stored_path(
    kanban_path: Path,
    stored_path: str,
    *,
    task_id: str = "",
) -> tuple[bytes, str]:
    """Read attachment bytes from an absolute stored_path under .kanban-attachments."""
    root = _kanban_attachments_root(kanban_path)
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64] if task_id else ""
    task_dir = root / safe_id if safe_id else root
    att_path = _resolve_stored_attachment_path(
        stored_path,
        att_dir=task_dir,
        kanban_path=kanban_path,
    )
    if att_path is None:
        att_path = Path(stored_path).expanduser()
    if not _path_under_or_equal_literal(att_path, root):
        raise FileNotFoundError("stored_path outside attachments directory")
    if task_id:
        safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
        task_dir = root / safe_id
        if not _path_under_or_equal_literal(att_path, task_dir):
            raise FileNotFoundError("stored_path does not match task_id")
    if not att_path.is_file():
        if task_id:
            safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
            fallback = _resolve_attachment_path(
                root / safe_id,
                att_path.name,
                Path(stored_path).name,
            )
            if fallback is not None:
                att_path = fallback
        if not att_path.is_file():
            raise FileNotFoundError(f"attachment not found: {att_path.name}")
    data = att_path.read_bytes()
    mime = mimetypes.guess_type(att_path.name)[0] or "application/octet-stream"
    return data, mime


_STORE_ONLY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".mp3",
        ".mp4",
        ".m4a",
        ".mov",
        ".avi",
        ".mkv",
        ".pptx",
        ".docx",
        ".xlsx",
    }
)


def _zip_compress_type(path: Path) -> int:
    if path.suffix.lower() in _STORE_ONLY_SUFFIXES:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _build_attachment_file_lookup(att_dir: Path) -> dict[str, Path]:
    """Index attachment files once (single rglob) for fast zip/download lookups."""
    lookup: dict[str, Path] = {}
    if not att_dir.is_dir():
        return lookup
    for fp in att_dir.rglob("*"):
        if not fp.is_file():
            continue
        try:
            rel = fp.relative_to(att_dir).as_posix()
        except ValueError:
            rel = fp.name
        for key in (rel, rel.lower(), fp.name, fp.name.lower()):
            if key and key not in lookup:
                lookup[key] = fp
    return lookup


def _resolve_task_attachment_file(
    kanban_path: Path,
    task_id: str,
    att: dict[str, Any],
    *,
    file_lookup: dict[str, Path] | None = None,
) -> Path | None:
    safe_name = str(att.get("safe_name") or "").strip()
    display = str(att.get("name") or att.get("file_name") or "").strip()
    stored_path = str(att.get("stored_path") or att.get("path") or "").strip()
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    att_dir = _kanban_attachments_root(kanban_path) / safe_id

    if stored_path:
        candidate = Path(stored_path).expanduser()
        if candidate.is_file():
            return candidate

    lookup = file_lookup if file_lookup is not None else _build_attachment_file_lookup(att_dir)
    for raw in (safe_name, display, Path(stored_path).name if stored_path else ""):
        name = str(raw or "").strip()
        if not name:
            continue
        for key in (name, name.lower(), Path(name).name, Path(name).name.lower()):
            hit = lookup.get(key)
            if hit is not None and hit.is_file():
                return hit

    direct = att_dir / safe_name if safe_name else None
    if direct is not None and direct.is_file():
        return direct
    if display:
        rel = att_dir / Path(display)
        if rel.is_file() and _path_under_or_equal_literal(rel, att_dir):
            return rel

    if file_lookup is None:
        return _resolve_attachment_path(att_dir, safe_name, display, stored_path)
    return None


def _zip_paths_to_bytes(entries: list[tuple[Path, str]]) -> bytes:
    buf = io.BytesIO()
    used_names: dict[str, int] = {}
    written = 0
    with zipfile.ZipFile(buf, "w") as zf:
        for file_path, arcname in entries:
            if not file_path.is_file():
                continue
            safe_arc = _safe_att_name(arcname) or file_path.name
            if safe_arc in used_names:
                used_names[safe_arc] += 1
                stem = Path(safe_arc).stem
                ext = Path(safe_arc).suffix
                safe_arc = f"{stem}_{used_names[safe_arc]}{ext}"
            else:
                used_names[safe_arc] = 1
            zf.write(
                file_path,
                safe_arc,
                compress_type=_zip_compress_type(file_path),
            )
            written += 1
    if written == 0:
        raise FileNotFoundError("no readable attachments")
    return buf.getvalue()


def _zip_folder_to_bytes(folder_path: Path) -> bytes:
    folder = folder_path.expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError("folder not found")
    entries: list[tuple[Path, str]] = []
    for root, _dirnames, filenames in os.walk(folder):
        root_path = Path(root)
        for filename in filenames:
            fp = root_path / filename
            if not fp.is_file():
                continue
            arcname = fp.relative_to(folder).as_posix()
            entries.append((fp, arcname))
    return _zip_paths_to_bytes(entries)


def _attachment_archive_name(att: dict[str, Any]) -> str:
    display = str(att.get("name") or att.get("file_name") or "").strip()
    safe_name = str(att.get("safe_name") or "").strip()
    if display:
        return _safe_att_name(display)
    if safe_name:
        parts = safe_name.split("_", 1)
        if len(parts) == 2 and parts[0].isdigit():
            return _safe_att_name(parts[1])
        return _safe_att_name(safe_name)
    stored = str(att.get("stored_path") or att.get("path") or "").strip()
    if stored:
        return _safe_att_name(Path(stored).name)
    return "arquivo"


def _read_task_attachment_bytes(
    kanban_path: Path,
    task_id: str,
    att: dict[str, Any],
) -> tuple[bytes, str]:
    resolved = _resolve_task_attachment_file(kanban_path, task_id, att)
    if resolved is not None:
        data = resolved.read_bytes()
        mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return data, mime
    safe_name = str(att.get("safe_name") or "").strip()
    if safe_name:
        return get_kanban_attachment(kanban_path, task_id, safe_name)
    raise FileNotFoundError("attachment metadata incomplete")


def _attachment_folder_name(att: dict[str, Any]) -> str:
    name = str(att.get("name") or att.get("file_name") or "").strip()
    if "/" in name:
        return name.split("/", 1)[0]
    stored = str(att.get("stored_path") or att.get("path") or "").replace("\\", "/")
    match = re.search(r"\.kanban-attachments/[^/]+/([^/]+)/", stored)
    return match.group(1) if match else ""


def _attachments_in_folder(
    attachments: list[dict[str, Any]],
    folder: str,
) -> list[dict[str, Any]]:
    folder_key = str(folder or "").strip().strip("/")
    if not folder_key:
        return attachments
    out: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if _attachment_folder_name(att) == folder_key:
            out.append(att)
            continue
        name = str(att.get("name") or att.get("file_name") or "").strip()
        if name.startswith(f"{folder_key}/"):
            out.append(att)
    return out


def _attachment_archive_name_in_folder(att: dict[str, Any], folder: str = "") -> str:
    folder_key = str(folder or "").strip().strip("/")
    name = str(att.get("name") or att.get("file_name") or "").strip()
    if folder_key and name.startswith(f"{folder_key}/"):
        return _safe_att_name(name[len(folder_key) + 1 :])
    return _attachment_archive_name(att)


def _build_attachments_zip_bytes(
    kanban_path: Path,
    task_id: str,
    attachments: list[dict[str, Any]],
    *,
    folder: str = "",
) -> bytes:
    if not attachments:
        raise FileNotFoundError("no attachments")

    folder_key = str(folder or "").strip().strip("/")
    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    att_dir = _kanban_attachments_root(kanban_path) / safe_id

    if folder_key:
        folder_path = att_dir / folder_key
        if folder_path.is_dir():
            return _zip_folder_to_bytes(folder_path)

    file_lookup = _build_attachment_file_lookup(att_dir)
    entries: list[tuple[Path, str]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        file_path = _resolve_task_attachment_file(
            kanban_path,
            task_id,
            att,
            file_lookup=file_lookup,
        )
        if file_path is None:
            continue
        arcname = _attachment_archive_name_in_folder(att, folder_key)
        entries.append((file_path, arcname))
    return _zip_paths_to_bytes(entries)


def get_kanban_attachments_zip(
    kanban_path: Path,
    task_id: str,
    *,
    folder: str = "",
) -> tuple[bytes, str]:
    """Return (zip_bytes, suggested_filename) for task attachments."""
    board = load_kanban(kanban_path)
    tasks: list[dict[str, Any]] = list(board.get("tasks") or [])
    task = next((t for t in tasks if isinstance(t, dict) and t.get("id") == task_id), None)
    if task is None:
        raise ValueError(f"tarefa {task_id} não encontrada")

    attachments = [a for a in (task.get("attachments") or []) if isinstance(a, dict)]
    folder_key = str(folder or "").strip().strip("/")
    if folder_key:
        attachments = _attachments_in_folder(attachments, folder_key)
    if not attachments:
        raise FileNotFoundError("no attachments")

    safe_id = re.sub(r"[^\w\-.]", "_", task_id)[:64]
    if folder_key:
        safe_folder = re.sub(r"[^\w\-.]", "_", folder_key)[:64]
        fname = f"{safe_id}_{safe_folder}.zip"
    else:
        fname = f"{safe_id}_anexos.zip"
    data = _build_attachments_zip_bytes(
        kanban_path,
        task_id,
        attachments,
        folder=folder_key,
    )
    return data, fname


def delete_kanban_attachment(
    kanban_path: Path,
    task_id: str,
    safe_name: str,
) -> None:
    """Remove attachment file and update task metadata."""
    name_key = str(safe_name or "").strip()
    att_dir = _attachments_dir(kanban_path, task_id)

    board = load_kanban(kanban_path)
    tasks: list[dict[str, Any]] = list(board.get("tasks") or [])
    idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)

    att_meta: dict[str, Any] | None = None
    if idx is not None:
        atts = tasks[idx].get("attachments") or []
        att_meta = next(
            (
                a
                for a in atts
                if isinstance(a, dict)
                and (
                    str(a.get("safe_name") or "").strip() == name_key
                    or str(a.get("name") or "").strip() == name_key
                )
            ),
            None,
        )

    att_path: Path | None = None
    if att_meta is not None:
        stored = str(att_meta.get("stored_path") or att_meta.get("path") or "").strip()
        if stored:
            try:
                stored_path = Path(stored).expanduser()
                if stored_path.is_file() and _path_under_or_equal_literal(
                    stored_path, att_dir
                ):
                    att_path = stored_path
            except OSError:
                pass
    if att_path is None and name_key:
        safe_file = re.sub(r"[^\w\-. ]+", "_", name_key)[:220]
        display = ""
        if att_meta is not None:
            display = str(att_meta.get("name") or att_meta.get("file_name") or "").strip()
        att_path = _resolve_attachment_path(att_dir, safe_file, name_key, display)

    deleted_path_key = ""
    if att_path is not None and att_path.is_file():
        deleted_path_key = str(att_path.resolve())
        att_path.unlink(missing_ok=True)

    if idx is not None:
        atts = tasks[idx].get("attachments") or []

        def _keep_attachment(att: Any) -> bool:
            if not isinstance(att, dict):
                return True
            if str(att.get("safe_name") or "").strip() == name_key:
                return False
            if str(att.get("name") or "").strip() == name_key:
                return False
            if deleted_path_key:
                stored = str(att.get("stored_path") or att.get("path") or "").strip()
                if stored:
                    try:
                        if str(Path(stored).expanduser().resolve()) == deleted_path_key:
                            return False
                    except OSError:
                        pass
            return True

        tasks[idx]["attachments"] = [a for a in atts if _keep_attachment(a)]
        board["tasks"] = tasks
        save_kanban(kanban_path, board)


def copy_kanban_attachment(
    kanban_path: Path,
    source_task_id: str,
    dest_task_id: str,
    safe_name: str,
) -> dict[str, Any]:
    """Copy an attachment from one card to another (same board).

    Returns the new attachment metadata on the destination card.
    """
    import shutil

    board = load_kanban(kanban_path)
    tasks: list[dict[str, Any]] = list(board.get("tasks") or [])

    src_idx = next((i for i, t in enumerate(tasks) if t["id"] == source_task_id), None)
    if src_idx is None:
        raise ValueError(f"tarefa origem {source_task_id} não encontrada")
    dst_idx = next((i for i, t in enumerate(tasks) if t["id"] == dest_task_id), None)
    if dst_idx is None:
        raise ValueError(f"tarefa destino {dest_task_id} não encontrada")

    # Find the attachment metadata on source
    src_atts = tasks[src_idx].get("attachments") or []
    att_meta = next((a for a in src_atts if a.get("safe_name") == safe_name), None)
    if att_meta is None:
        raise FileNotFoundError(f"anexo {safe_name!r} não encontrado na tarefa {source_task_id}")

    # Resolve file paths
    src_dir = _attachments_dir(kanban_path, source_task_id)
    src_file = src_dir / safe_name
    if not src_file.is_file():
        raise FileNotFoundError(f"arquivo do anexo não encontrado: {safe_name}")

    dst_dir = _attachments_dir(kanban_path, dest_task_id)
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Generate new safe_name with fresh timestamp to avoid collisions
    original_name = att_meta.get("name") or safe_name
    ts = int(time.time() * 1000)
    new_safe_name = f"{ts}_{_safe_att_name(original_name)}"
    dst_file = dst_dir / new_safe_name

    shutil.copy2(src_file, dst_file)

    # Update board metadata
    new_meta: dict[str, Any] = {
        "name": original_name,
        "safe_name": new_safe_name,
        "mime_type": att_meta.get("mime_type", "application/octet-stream"),
        "size": dst_file.stat().st_size,
        "uploaded_at": _now_iso(),
        "copied_from": source_task_id,
        "stored_path": _abs_stored_path(dst_file),
    }
    dst_atts = tasks[dst_idx].get("attachments") or []
    if not isinstance(dst_atts, list):
        dst_atts = []
    dst_atts.append(new_meta)
    tasks[dst_idx]["attachments"] = dst_atts
    board["tasks"] = tasks
    save_kanban(kanban_path, board)
    return new_meta


def move_kanban_attachment(
    kanban_path: Path,
    source_task_id: str,
    dest_task_id: str,
    safe_name: str,
) -> dict[str, Any]:
    """Move an attachment from one card to another (same board).

    Copies to destination then removes from source. Returns new metadata.
    """
    new_meta = copy_kanban_attachment(kanban_path, source_task_id, dest_task_id, safe_name)
    # Remove original file and metadata from source
    delete_kanban_attachment(kanban_path, source_task_id, safe_name)
    new_meta.pop("copied_from", None)
    new_meta["moved_from"] = source_task_id
    return new_meta

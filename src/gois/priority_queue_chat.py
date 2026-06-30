"""Chat command parser for the priority queue.

Supports natural-language-like commands in Portuguese:
  - "priorizar TASK-005 como 1"
  - "pausar fila"
  - "retomar"
  - "remover TASK-003"
  - "status"
  - "enfileirar TASK-010 prioridade 2 skill plan,test-driven-development"
  - "listar fila"
"""

from __future__ import annotations

import re
from typing import Any, Optional

_TASK_RE = re.compile(r"(TASK-\d{3,})", re.IGNORECASE)
_PRIORITY_RE = re.compile(
    r"(?:prioridade|priority|como|prio|p)\s*[:=]?\s*(\d{1,2})",
    re.IGNORECASE,
)
_SKILLS_RE = re.compile(
    r"(?:skills?|habilidades?)\s*[:=]?\s*([^\n;]+)",
    re.IGNORECASE,
)
_ASSIGNEE_RE = re.compile(
    r"(?:assignee|agente|perfil|profile)\s*[:=]?\s*(\S+)",
    re.IGNORECASE,
)


def parse_chat_command(message: str) -> dict[str, Any]:
    """Parse a chat message into a priority queue action.

    Returns:
        dict with 'action' key and relevant params, or
        dict with 'error' key if not recognized.
    """
    text = message.strip().lower()

    # Status / listar
    if text in ("status", "fila", "listar", "listar fila", "queue", "list"):
        return {"action": "status"}

    # Pausar
    if text in ("pausar", "pausar fila", "pause", "parar"):
        return {"action": "pause"}

    # Retomar
    if text in ("retomar", "resume", "continuar", "retomar fila", "despausar"):
        return {"action": "resume"}

    # Priorizar (mudar prioridade)
    prio_cmds = ("priorizar", "mudar prioridade", "alterar prioridade", "prioritize")
    for cmd in prio_cmds:
        if text.startswith(cmd):
            task_m = _TASK_RE.search(message)
            prio_m = _PRIORITY_RE.search(message)
            if task_m and prio_m:
                return {
                    "action": "update_priority_by_task",
                    "task_id": task_m.group(1).upper(),
                    "priority": int(prio_m.group(1)),
                }
            elif task_m:
                return {
                    "action": "update_priority_by_task",
                    "task_id": task_m.group(1).upper(),
                    "priority": 1,  # default to highest
                }
            return {"error": "Informe o ID da tarefa (ex: TASK-005)"}

    # Remover
    if text.startswith("remover") or text.startswith("remove") or text.startswith("tirar"):
        task_m = _TASK_RE.search(message)
        if task_m:
            return {"action": "remove_by_task", "task_id": task_m.group(1).upper()}
        return {"error": "Informe o ID da tarefa para remover"}

    # Enfileirar
    enqueue_cmds = ("enfileirar", "adicionar", "enqueue", "add", "agendar")
    for cmd in enqueue_cmds:
        if text.startswith(cmd):
            task_m = _TASK_RE.search(message)
            if not task_m:
                return {"error": "Informe o ID da tarefa (ex: TASK-005)"}
            priority = 5  # default
            prio_m = _PRIORITY_RE.search(message)
            if prio_m:
                priority = int(prio_m.group(1))
            skills: list[str] = []
            skills_m = _SKILLS_RE.search(message)
            if skills_m:
                skills = [s.strip() for s in skills_m.group(1).split(",") if s.strip()]
            assignee = ""
            assignee_m = _ASSIGNEE_RE.search(message)
            if assignee_m:
                assignee = assignee_m.group(1)
            return {
                "action": "enqueue_by_task",
                "task_id": task_m.group(1).upper(),
                "priority": priority,
                "skills": skills,
                "assignee": assignee,
            }

    # Fallback: try to detect task + priority in any format
    task_m = _TASK_RE.search(message)
    prio_m = _PRIORITY_RE.search(message)
    if task_m and prio_m:
        return {
            "action": "update_priority_by_task",
            "task_id": task_m.group(1).upper(),
            "priority": int(prio_m.group(1)),
        }

    return {
        "error": (
            "Comando não reconhecido. Exemplos:\n"
            "• enfileirar TASK-005 prioridade 2\n"
            "• priorizar TASK-003 como 1\n"
            "• remover TASK-007\n"
            "• pausar / retomar\n"
            "• status"
        )
    }

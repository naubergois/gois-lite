"""Priority queue HTTP handler integration for the monitor.

This module provides the handler functions that bridge the HTTP routes
to the PriorityQueueEngine, and includes the chat command processor.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .hermes_kanban import normalize_assignees, suggest_assignee_for_task
from .priority_queue import PriorityQueueEngine
from .priority_queue_chat import parse_chat_command

log = logging.getLogger(__name__)


class PriorityQueueHandler:
    """Encapsulates priority queue operations for the HTTP layer."""

    def __init__(
        self,
        state_path: Path,
        quota_checker: Callable[[], bool],
        schedule_runner: Callable[[dict], dict],
        board_loader: Optional[Callable[[str, Optional[str]], dict]] = None,
        assignee_resolver: Optional[Callable[..., str]] = None,
        on_terminal_failure: Optional[Callable[[Any], None]] = None,
        on_card_done: Optional[Callable[[Any], None]] = None,
        on_card_start: Optional[Callable[[Any], None]] = None,
    ):
        """
        Args:
            state_path: Path to persist queue state JSON.
            quota_checker: Returns True if daily quota is exceeded.
            schedule_runner: Executes a kanban schedule payload (sync mode).
            board_loader: Loads a board given (workdir, kanban_file) to resolve tasks.
            assignee_resolver: Maps assignee label -> canonical profile slug.
            on_terminal_failure: Called when a card permanently fails after retries.
        """
        self.engine = PriorityQueueEngine(
            state_path=state_path,
            quota_checker=quota_checker,
            schedule_runner=schedule_runner,
            on_terminal_failure=on_terminal_failure,
            on_card_done=on_card_done,
            on_card_start=on_card_start,
        )
        self._board_loader = board_loader
        self._assignee_resolver = assignee_resolver
        self.engine.start_worker()

    def handle_get(self) -> dict[str, Any]:
        """GET /priority-queue — return queue state."""
        return self.engine.get_queue()

    def handle_post(self, payload: dict, user: Any = None) -> dict[str, Any]:
        """POST /priority-queue — dispatch action."""
        action = str(payload.get("action") or "").strip().lower()

        if action == "enqueue":
            return self._handle_enqueue(payload)
        elif action == "update_priority":
            return self._handle_update_priority(payload)
        elif action == "remove":
            return self._handle_remove(payload)
        elif action == "pause":
            self.engine.pause_queue()
            return {"ok": True, "status": "paused"}
        elif action == "resume":
            self.engine.resume_queue()
            return {"ok": True, "status": "resumed"}
        elif action == "reorder":
            card_ids = payload.get("card_ids") or []
            if not isinstance(card_ids, list):
                return {"ok": False, "error": "card_ids deve ser uma lista"}
            self.engine.reorder(card_ids)
            return {"ok": True, "status": "reordered"}
        elif action == "chat_command":
            return self._handle_chat_command(payload)
        elif action == "get_card":
            card_id = str(payload.get("card_id") or "").strip()
            card = self.engine.get_card(card_id)
            if card is None:
                return {"ok": False, "error": "card não encontrado"}
            return {"ok": True, "card": card}
        elif action == "update_model":
            return self._handle_update_model(payload)
        else:
            return {
                "ok": False,
                "error": (
                    "action deve ser: enqueue, update_priority, update_model, remove, "
                    "pause, resume, reorder, chat_command, get_card"
                ),
            }

    def _normalize_assignee(self, assignee: str, payload: Optional[dict] = None) -> str:
        text = str(assignee or "").strip()
        if not text or self._assignee_resolver is None:
            return text
        try:
            resolved = self._assignee_resolver(text, payload)
        except TypeError:
            resolved = self._assignee_resolver(text)
        resolved = str(resolved or "").strip()
        return resolved or text

    def _handle_enqueue(self, payload: dict) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        priority = int(payload.get("priority", 5))
        skills = payload.get("skills") or []
        assignee = str(payload.get("assignee") or "").strip()
        workdir = str(payload.get("workdir") or "").strip()
        kanban_file = payload.get("kanban_file")
        team_id = str(payload.get("team_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip() or None

        if not task_id:
            return {"ok": False, "error": "task_id é obrigatório"}
        if not title:
            # Try to resolve title from board
            if self._board_loader and workdir:
                try:
                    board = self._board_loader(workdir, kanban_file)
                    task = next(
                        (t for t in (board.get("tasks") or []) if t.get("id") == task_id),
                        None,
                    )
                    if task:
                        title = str(task.get("title") or task_id)
                        if not skills and task.get("skills"):
                            skills = list(task["skills"])
                        if not assignee and task.get("assignees"):
                            assignee = str(task["assignees"][0] or "").strip()
                        agents_raw = payload.get("agents") or []
                        agents = [
                            a
                            for a in agents_raw
                            if isinstance(a, dict) and str(a.get("slug") or "").strip()
                        ]
                        if not assignee and agents:
                            load_counts: dict[str, int] = {}
                            for row in board.get("tasks") or []:
                                if not isinstance(row, dict):
                                    continue
                                for slug in normalize_assignees(
                                    row.get("assignees") or row.get("assignee")
                                ):
                                    load_counts[slug] = load_counts.get(slug, 0) + 1
                            assignee = suggest_assignee_for_task(
                                task, agents, load_counts=load_counts
                            )
                except Exception as e:
                    log.warning("board_loader failed for title resolution: %s", e)
            if not title:
                title = task_id

        if not isinstance(skills, list):
            skills = [str(s).strip() for s in str(skills).split(",") if s.strip()]

        assignee = self._normalize_assignee(assignee, payload)

        try:
            card = self.engine.enqueue(
                task_id=task_id,
                title=title,
                priority=priority,
                skills=skills,
                assignee=assignee,
                workdir=workdir,
                kanban_file=kanban_file if kanban_file else None,
                team_id=team_id,
                model_id=model_id,
            )
            return {
                "ok": True,
                "card_id": card.id,
                "task_id": card.task_id,
                "priority": card.priority,
                "status": card.status.value,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def _handle_update_priority(self, payload: dict) -> dict[str, Any]:
        card_id = str(payload.get("card_id") or "").strip()
        priority = payload.get("priority")
        if not card_id:
            return {"ok": False, "error": "card_id é obrigatório"}
        if priority is None:
            return {"ok": False, "error": "priority é obrigatório"}
        try:
            card = self.engine.update_priority(card_id, int(priority))
            return {"ok": True, "card_id": card.id, "priority": card.priority}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def _handle_update_model(self, payload: dict) -> dict[str, Any]:
        card_id = str(payload.get("card_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip() or None
        if not card_id:
            return {"ok": False, "error": "card_id é obrigatório"}
        try:
            card = self.engine.update_model(card_id, model_id)
            return {"ok": True, "card_id": card.id, "model_id": card.model_id or ""}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def _handle_remove(self, payload: dict) -> dict[str, Any]:
        card_id = str(payload.get("card_id") or "").strip()
        if not card_id:
            return {"ok": False, "error": "card_id é obrigatório"}
        try:
            self.engine.remove(card_id)
            return {"ok": True, "removed": card_id}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def _handle_chat_command(self, payload: dict) -> dict[str, Any]:
        message = str(payload.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "message é obrigatório"}

        parsed = parse_chat_command(message)

        if "error" in parsed:
            return {"ok": False, "error": parsed["error"]}

        action = parsed["action"]

        if action == "status":
            queue = self.engine.get_queue()
            queued_count = len(queue.get("queued") or [])
            running_count = len(queue.get("running") or [])
            status_parts = []
            if queue.get("paused"):
                status_parts.append("⏸ Fila pausada")
            elif queue.get("quota_exceeded"):
                status_parts.append("⚠ Cota excedida")
            else:
                status_parts.append("● Ativa")
            status_parts.append(f"{queued_count} na fila")
            status_parts.append(f"{running_count} em execução")
            status_parts.append(f"{queue['total_completed']} concluídos")
            reply = " | ".join(status_parts)
            return {"ok": True, "reply": reply}

        elif action == "pause":
            self.engine.pause_queue()
            return {"ok": True, "reply": "⏸ Fila pausada"}

        elif action == "resume":
            self.engine.resume_queue()
            return {"ok": True, "reply": "▶ Fila retomada"}

        elif action == "update_priority_by_task":
            task_id = parsed["task_id"]
            priority = parsed["priority"]
            # Find card by task_id
            queue = self.engine.get_queue()
            card = next(
                (c for c in (queue.get("queued") or []) if c.get("task_id") == task_id),
                None,
            )
            if card is None:
                return {"ok": False, "error": f"{task_id} não está na fila"}
            try:
                self.engine.update_priority(card["id"], priority)
                return {"ok": True, "reply": f"✓ {task_id} → prioridade {priority}"}
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        elif action == "remove_by_task":
            task_id = parsed["task_id"]
            queue = self.engine.get_queue()
            card = next(
                (c for c in (queue.get("queued") or []) if c.get("task_id") == task_id),
                None,
            )
            if card is None:
                return {"ok": False, "error": f"{task_id} não está na fila"}
            try:
                self.engine.remove(card["id"])
                return {"ok": True, "reply": f"✓ {task_id} removido da fila"}
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        elif action == "enqueue_by_task":
            task_id = parsed["task_id"]
            priority = parsed.get("priority", 5)
            skills = parsed.get("skills", [])
            assignee = self._normalize_assignee(parsed.get("assignee", ""))
            # We need workdir — try to get from existing queue or use first project
            try:
                card = self.engine.enqueue(
                    task_id=task_id,
                    title=task_id,  # will be resolved
                    priority=priority,
                    skills=skills,
                    assignee=assignee,
                )
                return {
                    "ok": True,
                    "reply": f"✓ {task_id} enfileirado (prioridade {priority})",
                }
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        return {"ok": False, "error": "ação interna não implementada"}

    def shutdown(self) -> None:
        """Stop the worker thread."""
        self.engine.stop_worker()

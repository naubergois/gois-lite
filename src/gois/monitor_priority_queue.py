"""Priority queue HTTP handlers for :class:`gois.monitor.GoisMonitor`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .accounts import UserRecord
from .priority_queue_handler import PriorityQueueHandler

log = logging.getLogger(__name__)


class MonitorPriorityQueueMixin:
    """Fila de prioridades — GET/POST handlers and agent enable/disable."""

    def init_priority_queue(self) -> None:
        from .local_paths import project_stack_root

        state_file = project_stack_root() / "priority_queue" / "state.json"
        self._priority_queue_handler: Optional[PriorityQueueHandler] = None
        if self.cfg.hermes and self.cfg.hermes_agent_create.enabled:
            self._priority_queue_handler = PriorityQueueHandler(
                state_path=state_file,
                quota_checker=lambda: bool(
                    self._model_daily_quota_status(enforce=True).get("blocked")
                ),
                schedule_runner=lambda payload: self._execute_kanban_schedule(
                    payload, self._system_actor(payload)
                ),
                board_loader=self._priority_queue_board_loader,
                assignee_resolver=lambda raw, payload=None: self._resolve_kanban_assignee(
                    raw, self._system_actor(payload)
                ),
                on_terminal_failure=self._on_priority_queue_terminal_failure,
                on_card_done=self._on_priority_queue_card_done,
                on_card_start=self._on_priority_queue_card_start,
            )
            self.tracker.register(
                "priority_queue",
                "fila de prioridades — executa cards por prioridade até conclusão ou cota",
            )

    def _priority_queue_board_loader(
        self, workdir: str, kanban_file: Optional[str]
    ) -> dict:
        """Load a kanban board for the priority queue to resolve task metadata."""
        from .hermes_kanban import get_board

        return get_board(workdir, self.cfg.hermes_agent_create, kanban_file=kanban_file)

    def handle_priority_queue_get(self) -> dict:
        """GET /priority-queue"""
        if self._priority_queue_handler is None:
            return {"ok": False, "error": "fila de prioridades não configurada"}
        return self._priority_queue_handler.handle_get()

    def handle_priority_queue_post(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """POST /priority-queue"""
        if self._priority_queue_handler is None:
            return {"ok": False, "error": "fila de prioridades não configurada"}
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        return self._priority_queue_handler.handle_post(payload, user)

    def handle_agent_action(self, name: str, action: str) -> dict:
        """Enable/disable/toggle a background agent from the HTTP server thread."""
        a = self.tracker.get(name)
        if action == "enable":
            a.enabled = True
            if a.state == "paused":
                a.state = "idle"
        elif action == "disable":
            a.enabled = False
            a.state = "paused"
        elif action == "toggle":
            a.enabled = not a.enabled
            a.state = "paused" if not a.enabled else "idle"
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
        log.info(
            "operator action: agent=%s action=%s enabled=%s",
            name,
            action,
            a.enabled,
        )
        return {"ok": True, "name": a.name, "enabled": a.enabled, "state": a.state}

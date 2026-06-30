"""OpenClaw memory, skills catalog, and skill discovery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .accounts import UserRecord
from .openclaw_skills import list_openclaw_skills
from .skill_discovery import (
    discover_skill_suggestions,
    dismiss_suggestion,
    discovery_status,
    format_suggestions_whatsapp,
    list_pending_suggestions,
    mark_suggestions_notified,
)
from .whatsapp_outbound import enqueue_whatsapp

log = logging.getLogger(__name__)


class MonitorOpenclawSkillsMixin:
    def handle_openclaw_memory_list(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if self.chat_persistence is None:
            return {
                "ok": False,
                "error": "chat history is disabled (openclaw_chat.history_enabled=false)",
            }
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}

        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        try:
            session_limit = int(query.get("sessions") or 30)
        except (TypeError, ValueError):
            session_limit = 30
        try:
            facts_per_session = int(query.get("facts") or 12)
        except (TypeError, ValueError):
            facts_per_session = 12
        return self.chat_persistence.history.list_user_memory(
            user_id=self._chat_user_id(user),
            agent_id=agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None,
            session_limit=session_limit,
            facts_per_session=facts_per_session,
        )

    def handle_openclaw_project_memory_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if self.project_memory is None:
            return {"ok": False, "error": "project memory is disabled"}
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        snap = self.project_memory.snapshot()
        return {"ok": True, **snap}

    def handle_openclaw_project_memory_put(
        self, body: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if self.project_memory is None:
            return {"ok": False, "error": "project memory is disabled"}
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}
        payload = body if isinstance(body, dict) else {}
        if "brief" in payload:
            self.project_memory.set_brief(str(payload.get("brief") or ""))
        facts = payload.get("facts")
        if isinstance(facts, list):
            self.project_memory.replace_facts(facts)
        if self.chat_persistence is not None and self.chat_persistence.memory is not None:
            self.project_memory.index_in_chroma(self.chat_persistence.memory)
        snap = self.project_memory.snapshot()
        return {"ok": True, **snap}

    def handle_openclaw_skills_list(self, query: dict) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        cc = self.cfg.openclaw_chat
        extra = list(getattr(cc, "extra_skill_dirs", None) or [])
        return list_openclaw_skills(
            self._openclaw_runtime(),
            agent_id=agent_id or cc.default_agent,
            gois_extra_dirs=extra,
        )

    def handle_openclaw_integration_get(self, query: dict) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .openclaw_integration import integration_status

        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        cc = self.cfg.openclaw_chat
        if isinstance(agent_id, str) and agent_id.strip():
            cc = cc.model_copy(update={"default_agent": agent_id.strip()})
        return integration_status(
            self._openclaw_runtime(),
            cc,
            self.cfg.openclaw_doctor,
            self.recovery,
        )

    def handle_openclaw_tools_list(self, query: dict) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .openclaw_tools import list_openclaw_native_tools

        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        return list_openclaw_native_tools(
            self._openclaw_runtime(),
            self.cfg.openclaw_doctor,
            agent_id=str(agent_id or self.cfg.openclaw_chat.default_agent or "main"),
        )

    def handle_openclaw_skill_dirs_add(self, payload: dict) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .openclaw_integration import add_extra_skill_dir

        path = str((payload or {}).get("path") or "").strip()
        return add_extra_skill_dir(
            self._openclaw_runtime(),
            self.cfg.openclaw_chat,
            path,
        )

    def handle_mcp_skills(self, query: dict, payload: Optional[dict] = None) -> dict:
        """REST bridge for qclaw-skills MCP (list/get/search/run/tools/reference)."""
        from .skills_mcp import dispatch_skills_action

        action = str(
            (payload or {}).get("action")
            or query.get("action")
            or "list"
        ).strip()
        args: dict = {}
        if payload:
            args.update(payload)
        for key, val in query.items():
            if key == "action":
                continue
            if key not in args or args.get(key) in (None, ""):
                args[key] = val
        return dispatch_skills_action(action, args)

    def handle_mcp_cards(self, query: dict, payload: Optional[dict] = None) -> dict:
        """REST bridge for qclaw-cards MCP (kanban, errors, swarm)."""
        from .cards_mcp import dispatch_cards_action

        action = str(
            (payload or {}).get("action")
            or (payload or {}).get("tool")
            or query.get("action")
            or query.get("tool")
            or ""
        ).strip()
        if not action:
            return {"ok": False, "error": "action (MCP tool name) is required"}
        args: dict = {}
        if payload:
            args.update(payload)
        for key, val in query.items():
            if key in ("action", "tool"):
                continue
            if key not in args or args.get(key) in (None, ""):
                args[key] = val
        return dispatch_cards_action(action, args)

    def handle_skill_suggestions_list(self) -> dict:
        sd = self.cfg.skill_discovery
        if not sd.enabled:
            return {"ok": True, "enabled": False, "pending": []}
        out = discovery_status(sd)
        if sd.notify_dashboard:
            return out
        return {**out, "pending": []}

    def handle_skill_suggestions_scan(self) -> dict:
        sd = self.cfg.skill_discovery
        if not sd.enabled:
            return {"ok": False, "error": "skill discovery disabled"}
        agent = self.cfg.openclaw_chat.default_agent
        result = discover_skill_suggestions(sd, agent_id=agent)
        self._maybe_notify_skill_suggestions(result.get("new") or [])
        return result

    def handle_skill_suggestions_dismiss(self, payload: dict) -> dict:
        sd = self.cfg.skill_discovery
        sid = str((payload or {}).get("id") or "").strip()
        return dismiss_suggestion(sd, sid)

    def _maybe_notify_skill_suggestions(self, new_rows: list) -> None:
        sd = self.cfg.skill_discovery
        if not sd.enabled or not sd.notify_whatsapp or not new_rows:
            return
        wd = self.cfg.whatsapp_digest
        if not wd.recipient:
            return
        to_notify = [
            r for r in new_rows
            if isinstance(r, dict) and not r.get("notified")
        ]
        if not to_notify:
            return
        message = format_suggestions_whatsapp(
            to_notify, max_items=sd.whatsapp_max_items
        )
        if not message:
            return
        wd_monitor = wd.model_copy(update={"skip_context_guard": True})
        enqueue_whatsapp(wd_monitor, message, wait=False)
        mark_suggestions_notified(
            sd, [str(r.get("id") or "") for r in to_notify if r.get("id")]
        )



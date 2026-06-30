"""OpenClaw chat sessions, status helpers, and WhatsApp session injection."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from .accounts import UserRecord
from .chat_jobs import messages_awaiting_reply_fields
from .chat_prompt_policy import history_nudge_fields
from .openclaw_chat import (
    default_session_key,
    delete_openclaw_session,
    list_sessions,
    read_messages,
    resolve_qclaw_runtime,
)
from .openclaw_chat_sessions import (
    create_openclaw_session,
    ensure_session_persisted,
    hermes_session_key,
    list_openclaw_agents,
    openclaw_key_for_hermes_profile,
    parse_hermes_session_key,
    purge_openclaw_sessions,
)

log = logging.getLogger(__name__)


class MonitorOpenclawSessionsMixin:
    def _chat_user_id(self, user: Optional[UserRecord]) -> Optional[str]:
        if not self.cfg.auth.enabled:
            return None
        return user.id if user is not None else None

    def _resolve_team_info_for_session(
        self,
        session_key: str,
        user: Optional[UserRecord],
        *,
        team_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve team metadata + kanban for chat context injection.

        Prefers the session's stored assignment; falls back to explicit *team_id*
        from the request (e.g. first message before persistence).
        """
        actor = self._accounts_actor(user)
        if actor is None:
            return None
        tid = ""
        if self.chat_persistence is not None:
            try:
                sess_row = self.chat_persistence.history.get_session_by_key(session_key)
                if sess_row is not None and sess_row.team_id:
                    tid = str(sess_row.team_id).strip()
            except Exception:
                pass
        if not tid:
            tid = str(team_id or "").strip()
        if not tid:
            return None
        try:
            team_rec = self.accounts.get_team(tid, actor.id)
        except ValueError:
            team_rec = None
            if getattr(actor, "is_admin", False):
                team_rec = next(
                    (t for t in self.accounts.list_all_teams() if t.id == tid),
                    None,
                )
            if team_rec is None:
                return None
        info = team_rec.to_public()
        resolved_id = team_rec.id
        try:
            kanban_data = self.accounts.read_kanban(resolved_id, actor.id)
            info["__kanban"] = kanban_data
        except Exception:
            info["__kanban"] = None
        try:
            from .team_normas_ops import list_normas

            nr = list_normas(resolved_id)
            info["__normas"] = list(nr.get("normas") or []) if nr.get("ok") else []
        except Exception:
            info["__normas"] = []
        return info
    def _openclaw_runtime(self):
        return resolve_qclaw_runtime(self.cfg.qclaw)

    def _sessions_from_hermes_profiles(
        self,
        user: Optional[UserRecord],
        *,
        warning: str,
        limit: int,
    ) -> Optional[dict[str, Any]]:
        """Build chat session list from Hermes profiles when OpenClaw is unavailable."""
        if not self.cfg.hermes or not self.cfg.hermes_agent_create.enabled:
            return None
        dashboard_url = self._hermes_dashboard_url()
        if not dashboard_url:
            return None
        profiles_result = self.handle_hermes_profiles_list(user)
        if not profiles_result.get("ok"):
            return None
        profiles = profiles_result.get("profiles") or []

        # Build a map of real updatedAt timestamps from chat persistence
        persisted_ts: dict[str, int] = {}
        if self.chat_persistence is not None:
            try:
                db_rows = self.chat_persistence.history.list_sessions(limit=500)
                for db_row in db_rows:
                    key = str(db_row.get("key") or "").strip()
                    ts = int(db_row.get("updatedAt") or 0)
                    if key and ts:
                        persisted_ts[key] = ts
            except Exception:
                pass

        sessions: list[dict[str, Any]] = []
        agents: list[str] = []
        now_ms = int(time.time() * 1000)
        for row in profiles:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            title = str(row.get("display_name") or name).strip() or name
            key = hermes_session_key(name)
            # Use real updatedAt from persistence if available, else 0
            real_ts = persisted_ts.get(key, 0)
            sessions.append(
                {
                    "key": key,
                    "agentId": name,
                    "title": title,
                    "kind": "hermes",
                    "updatedAt": real_ts or now_ms,
                    "description": row.get("description"),
                }
            )
            agents.append(name)
        if not sessions:
            return None
        # Sort by modification date (most recent first)
        sessions.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
        capped = sessions[: max(1, min(limit, 200))]
        cc = self.cfg.openclaw_chat
        return {
            "ok": True,
            "sessions": capped,
            "count": len(capped),
            "total": len(sessions),
            "source": "hermes",
            "fallback": True,
            "warning": warning,
            "default_session_key": capped[0]["key"],
            "default_agent": capped[0]["agentId"],
            "agents": agents,
            "backend": "hermes",
            "dashboard_url": dashboard_url,
            "control_url": None,
        }

    def _finish_openclaw_sessions_result(
        self,
        result: dict[str, Any],
        cc: Any,
        runtime: Any,
        *,
        include_tool_scan: bool = False,
    ) -> dict[str, Any]:
        result["default_session_key"] = default_session_key(cc, runtime)
        result["default_agent"] = cc.default_agent
        result["agents"] = list_openclaw_agents(runtime)
        result["backend"] = (cc.backend or "deepseek").strip().lower()
        result["source"] = "openclaw"
        if include_tool_scan:
            result["openclaw_chat"] = self._openclaw_chat_status()
        else:
            payload = self._openclaw_chat_ui_payload(include_models=False)
            payload["backend"] = result["backend"]
            result["openclaw_chat"] = payload
        self._inject_whatsapp_session(result)
        return result

    def handle_openclaw_sessions_list(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        cc = self.cfg.openclaw_chat
        limit = query.get("limit")
        try:
            lim = int(limit) if limit is not None else cc.sessions_limit
        except (TypeError, ValueError):
            lim = cc.sessions_limit
        lim = max(1, min(lim, 200))
        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        refresh = str(query.get("refresh") or "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        runtime = self._openclaw_runtime()
        result = list_sessions(
            runtime,
            limit=lim,
            agent_id=agent_id,
            cache_seconds=cc.sessions_cache_seconds,
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            skip_cache=refresh,
        )
        if result.get("ok"):
            return self._finish_openclaw_sessions_result(
                result, cc, runtime, include_tool_scan=False
            )
        return result

    def handle_openclaw_session_new(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        cc = self.cfg.openclaw_chat
        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        aid = (agent_id or cc.default_agent).strip() or cc.default_agent
        title = query.get("title")
        if title is not None and not isinstance(title, str):
            return {"ok": False, "error": "title must be a string"}
        team_id = query.get("team_id")
        if team_id is not None and not isinstance(team_id, str):
            return {"ok": False, "error": "team_id must be a string"}
        return create_openclaw_session(
            self._openclaw_runtime(),
            agent_id=aid,
            title=(title or "Nova conversa").strip() or "Nova conversa",
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            team_id=team_id,
        )

    def handle_openclaw_session_delete(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        key = str(
            payload.get("session_key")
            or payload.get("key")
            or payload.get("sessionKey")
            or ""
        ).strip()
        if not key:
            return {"ok": False, "error": "session_key is required"}
        agent_id = payload.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        aid = agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
        cc = self.cfg.openclaw_chat
        runtime = self._openclaw_runtime()
        protect: set[str] = {default_session_key(cc, runtime)}
        wa = self._whatsapp_inbound_session_key()
        if wa:
            protect.add(wa)
        kind = payload.get("kind")
        kind_str = kind.strip() if isinstance(kind, str) else ""
        return delete_openclaw_session(
            runtime,
            session_key=key,
            agent_id=aid,
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            protected_keys=protect,
            kind=kind_str,
        )

    def handle_openclaw_sessions_purge(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "body must be a JSON object"}
        mode = payload.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            return {"ok": False, "error": "mode is required (old or all)"}
        cc = self.cfg.openclaw_chat
        agent_id = payload.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        aid = (agent_id or cc.default_agent).strip() or cc.default_agent
        try:
            older_than_days = int(payload.get("older_than_days", 30))
        except (TypeError, ValueError):
            older_than_days = 30
        protect: set[str] = set()
        raw_protect = payload.get("protect_keys")
        if isinstance(raw_protect, list):
            for item in raw_protect:
                if isinstance(item, str) and item.strip():
                    protect.add(item.strip())
        runtime = self._openclaw_runtime()
        protect.add(default_session_key(cc, runtime))
        wa = self._whatsapp_inbound_session_key()
        if wa:
            protect.add(wa)
        result = purge_openclaw_sessions(
            runtime,
            mode=mode.strip().lower(),
            older_than_days=older_than_days,
            protect_keys=protect,
            agent_id=aid,
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
        )
        return result

    def handle_chat_assign_team(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Assign a chat session to a team (or reassign)."""
        session_key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not session_key:
            return {"ok": False, "error": "session_key is required"}
        team_id = str(payload.get("team_id") or "").strip()
        if not team_id:
            return {"ok": False, "error": "team_id is required"}
        # Validate that the team exists and belongs to the user.
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except ValueError as e:
            # Admins may assign any team in the system, not only their own.
            team = None
            if getattr(actor, "is_admin", False):
                team = next(
                    (t for t in self.accounts.list_all_teams() if t.id == team_id),
                    None,
                )
            if team is None:
                return {"ok": False, "error": str(e)}
        if self.chat_persistence is None:
            return {"ok": False, "error": "chat persistence not available"}
        if not ensure_session_persisted(
            self._openclaw_runtime(),
            self.chat_persistence,
            session_key=session_key,
            user_id=self._chat_user_id(user),
        ):
            return {"ok": False, "error": "session not found"}
        ok = self.chat_persistence.history.assign_team(session_key, team.id)
        if not ok:
            return {"ok": False, "error": "failed to assign team to session"}
        return {"ok": True, "session_key": session_key, "team_id": team.id, "team_name": team.name}

    def handle_chat_session_swarm(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Toggle the "Modo Swarm" (RuFlo orchestration) flag for a session."""
        session_key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not session_key:
            return {"ok": False, "error": "session_key is required"}
        enabled = payload.get("swarm")
        if enabled is None:
            enabled = payload.get("swarm_mode") or payload.get("enabled")
        if self.chat_persistence is None:
            return {"ok": False, "error": "chat persistence not available"}
        if not ensure_session_persisted(
            self._openclaw_runtime(),
            self.chat_persistence,
            session_key=session_key,
            user_id=self._chat_user_id(user),
        ):
            return {"ok": False, "error": "session not found"}
        ok = self.chat_persistence.history.set_session_swarm(session_key, bool(enabled))
        if not ok:
            return {"ok": False, "error": "session not found"}
        return {"ok": True, "session_key": session_key, "swarm_mode": bool(enabled)}

    def handle_openclaw_context_documents(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """List, add, remove or clear documents pinned to a chat session."""
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if self.chat_persistence is None:
            return {"ok": False, "error": "chat persistence not available"}
        cc = self.cfg.openclaw_chat
        key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not key:
            runtime = self._openclaw_runtime()
            key = default_session_key(cc, runtime)
        if not ensure_session_persisted(
            self._openclaw_runtime(),
            self.chat_persistence,
            session_key=key,
            user_id=self._chat_user_id(user),
        ):
            return {"ok": False, "error": "session not found"}
        sess = self.chat_persistence.history.get_session_by_key(key)
        team_id = sess.team_id if sess else None
        from .chat_context_documents import dispatch_context_documents

        return dispatch_context_documents(
            payload,
            history_store=self.chat_persistence.history,
            chat_cfg=cc,
            team_id=team_id,
        )

    def handle_openclaw_session_context_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        """Return session-scoped context for the dashboard context dialog."""
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        if self.chat_persistence is None:
            return {"ok": False, "error": "chat persistence not available"}
        if self.cfg.auth.enabled and user is None:
            return {"ok": False, "error": "not authenticated"}

        cc = self.cfg.openclaw_chat
        key = str(query.get("session_key") or query.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "session_key is required"}
        if not ensure_session_persisted(
            self._openclaw_runtime(),
            self.chat_persistence,
            session_key=key,
            user_id=self._chat_user_id(user),
        ):
            return {"ok": False, "error": "session not found"}

        history = self.chat_persistence.history
        sess = history.get_session_by_key(key)
        try:
            facts_limit = int(query.get("facts") or 40)
        except (TypeError, ValueError):
            facts_limit = 40
        facts_limit = max(1, min(facts_limit, 100))

        documents = history.list_context_documents(key)
        facts = history.list_context(key, limit=facts_limit)
        team_id = sess.team_id if sess else None

        return {
            "ok": True,
            "session_key": key,
            "title": (sess.title if sess else "") or "",
            "team_id": team_id,
            "documents": documents,
            "facts": facts,
            "documents_count": len(documents),
            "facts_count": len(facts),
        }

    def handle_openclaw_messages_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict:
        del user  # reserved for future Hermes transcript API
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        key = str(query.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "key is required"}
        cc = self.cfg.openclaw_chat
        limit = query.get("limit")
        try:
            lim = int(limit) if limit is not None else cc.messages_limit
        except (TypeError, ValueError):
            lim = cc.messages_limit
        agent_id = query.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        light = str(query.get("light") or "").strip().lower() in ("1", "true", "yes")
        profile = parse_hermes_session_key(key)
        if profile:
            mapped_key = openclaw_key_for_hermes_profile(profile, cc.default_agent)
            runtime = self._openclaw_runtime()
            msgs = read_messages(
                runtime,
                session_key=mapped_key,
                agent_id=cc.default_agent,
                limit=max(1, min(lim, 500)),
                light=light,
                persistence=self.chat_persistence,
            )
            dashboard_url = (self._hermes_dashboard_url() or "").rstrip("/")
            profile_url = f"{dashboard_url}/profiles/{profile}" if dashboard_url else ""
            title = profile
            if msgs.get("ok") and msgs.get("title"):
                title = str(msgs["title"])
            elif msgs.get("ok"):
                title = profile
            out: dict[str, Any] = {
                "ok": True,
                "sessionKey": key,
                "agentId": profile,
                "title": title,
                "messages": (msgs.get("messages") or []) if msgs.get("ok") else [],
                "source": "hermes",
                "exists": True,
                "hermes_profile": profile,
                "hermes_dashboard_url": profile_url,
                "control_url": runtime.control_url,
            }
            if not (msgs.get("messages") or []):
                out["hint"] = (
                    "Sessão Hermes — mensagens locais via DeepSeek; "
                    "histórico completo no dashboard Hermes."
                )
            out.update(
                messages_awaiting_reply_fields(
                    out.get("messages") or [],
                    client_key=key,
                )
            )
            out.update(
                history_nudge_fields(out.get("messages") or [], chat_cfg=cc)
            )
            return out
        out = read_messages(
            self._openclaw_runtime(),
            session_key=key,
            agent_id=agent_id,
            limit=max(1, min(lim, 500)),
            light=light,
            persistence=self.chat_persistence,
        )
        if out.get("ok"):
            out.update(
                messages_awaiting_reply_fields(
                    out.get("messages") or [],
                    client_key=key,
                )
            )
            out.update(
                history_nudge_fields(out.get("messages") or [], chat_cfg=cc)
            )
        return out
    def _whatsapp_inbound_session_key(self) -> str:
        wd = self.cfg.whatsapp_digest
        if wd.inbound_session_key and str(wd.inbound_session_key).strip():
            return str(wd.inbound_session_key).strip()
        from .openclaw_chat import _normalize_whatsapp_recipient

        digits = _normalize_whatsapp_recipient(wd.recipient)
        if digits:
            return f"agent:main:whatsapp:dm:+{digits}"
        return self.cfg.openclaw_chat.default_session_key

    def _is_whatsapp_session_key(self, session_key: str) -> bool:
        key = (session_key or "").strip()
        if not key:
            return False
        if key == self._whatsapp_inbound_session_key():
            return True
        return ":whatsapp:" in key

    def _whatsapp_public_config(self) -> dict[str, Any]:
        wd = self.cfg.whatsapp_digest
        from .openclaw_chat import _normalize_whatsapp_recipient

        digits = _normalize_whatsapp_recipient(wd.recipient)
        return {
            "enabled": bool(wd.recipient),
            "recipient": wd.recipient,
            "digits": digits or None,
            "session_key": self._whatsapp_inbound_session_key(),
            "inbound_enabled": wd.inbound_enabled,
            "inbound_auto_reply": wd.inbound_auto_reply,
            "inbound_sync_enabled": wd.inbound_sync_enabled,
            "webhook_url": (
                self._whatsapp_webhook_url() if wd.inbound_enabled else None
            ),
        }

    def _whatsapp_inbound_status_snapshot(self) -> dict[str, Any]:
        wd = self.cfg.whatsapp_digest
        return {
            "inbound_enabled": wd.inbound_enabled,
            "auto_reply_enabled": wd.inbound_auto_reply,
            "failures": self.state.whatsapp_inbound_failures,
            "last_error": self.state.whatsapp_inbound_last_error,
            "last_failure_ts": self.state.whatsapp_inbound_last_failure_ts,
        }

    def _whatsapp_session_row(self) -> Optional[dict[str, Any]]:
        wd = self.cfg.whatsapp_digest
        cc = self.cfg.openclaw_chat
        if not wd.recipient or not cc.enabled:
            return None
        key = self._whatsapp_inbound_session_key()
        from .openclaw_chat import _normalize_whatsapp_recipient

        digits = _normalize_whatsapp_recipient(wd.recipient)
        title = f"WhatsApp +{digits}" if digits else "WhatsApp"
        return {
            "key": key,
            "sessionKey": key,
            "agentId": cc.default_agent,
            "title": title,
            "kind": "whatsapp",
            "updatedAt": int(time.time() * 1000),
            "pinned": True,
        }

    def _inject_whatsapp_session(self, result: dict[str, Any]) -> None:
        row = self._whatsapp_session_row()
        if not row:
            return
        sessions = result.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        key = str(row["key"])
        filtered = [
            s
            for s in sessions
            if isinstance(s, dict) and str(s.get("key") or "") != key
        ]
        result["sessions"] = [row, *filtered]
        result["whatsapp"] = self._whatsapp_public_config()

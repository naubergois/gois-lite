"""MongoDB-backed chat history for the gois dashboard."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .mongo import get_collection
from .mongo_sqlite_bridge import resolve_scope_path, scope_from_path

log = logging.getLogger(__name__)

SESSIONS_COLLECTION = "chat_sessions"
MESSAGES_COLLECTION = "chat_messages"
CONTEXT_COLLECTION = "chat_context"

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_PATH_RE = re.compile(
    r"(?:^|[\s(])((?:/~?)?[\w./-]+\.(?:py|yaml|yml|json|md|sh|ts|tsx|js))\b"
)


@dataclass(frozen=True)
class ChatSessionRow:
    id: str
    session_key: str
    agent_id: str
    title: str
    source: str
    user_id: Optional[str]
    team_id: Optional[str]
    created_at: float
    updated_at: float
    swarm_mode: bool = False


class ChatHistoryStore:
    """Persistent sessions, messages, and context snippets for dashboard chat."""

    def __init__(
        self,
        scope_path: Path | str,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        self.scope_path = resolve_scope_path(scope_path, db_path=db_path)
        self.scope_path.parent.mkdir(parents=True, exist_ok=True)
        self._scope = scope_from_path(self.scope_path)
        self._sessions = get_collection(SESSIONS_COLLECTION)
        self._messages = get_collection(MESSAGES_COLLECTION)
        self._context = get_collection(CONTEXT_COLLECTION)
        self._ensure_indexes()

    @property
    def db_path(self) -> Path:
        """Deprecated alias for :attr:`scope_path` (legacy SQLite scope key)."""
        return self.scope_path

    def _ensure_indexes(self) -> None:
        try:
            self._sessions.create_index(
                [("_scope", 1), ("session_key", 1)], unique=True
            )
            self._sessions.create_index([("_scope", 1), ("id", 1)], unique=True)
            self._sessions.create_index(
                [("_scope", 1), ("agent_id", 1), ("updated_at", -1)]
            )
            self._messages.create_index([("_scope", 1), ("id", 1)], unique=True)
            self._messages.create_index(
                [("_scope", 1), ("session_id", 1), ("created_at", 1)]
            )
            self._context.create_index([("_scope", 1), ("id", 1)], unique=True)
            self._context.create_index(
                [("_scope", 1), ("session_id", 1), ("created_at", 1)]
            )
        except Exception as exc:  # pragma: no cover
            log.debug("chat_history index setup skipped: %s", exc)

    def _session_filter(
        self,
        *,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        filt: dict[str, Any] = {"_scope": self._scope}
        if agent_id:
            filt["agent_id"] = agent_id.strip()
        if user_id:
            filt["$or"] = [{"user_id": None}, {"user_id": user_id}]
        return filt

    def assign_team(self, session_key: str, team_id: str) -> bool:
        """Assign (or reassign) a chat session to a team. Returns True if updated."""
        tid = (team_id or "").strip() or None
        result = self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {"$set": {"team_id": tid, "updated_at": time.time()}},
        )
        return result.matched_count > 0

    def set_session_swarm(self, session_key: str, enabled: bool) -> bool:
        """Toggle the "Modo Swarm" flag for a chat session. Returns True if updated."""
        result = self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {"$set": {"swarm_mode": bool(enabled), "updated_at": time.time()}},
        )
        return result.matched_count > 0

    def list_context_documents(self, session_key: str) -> list[dict[str, Any]]:
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return []
        doc = self._sessions.find_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {"context_documents": 1},
        )
        raw = doc.get("context_documents") if doc else None
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for row in raw:
            if isinstance(row, dict) and row.get("path"):
                out.append(dict(row))
        return out

    def add_context_document(
        self, session_key: str, row: dict[str, Any]
    ) -> bool:
        sess = self.get_session_by_key(session_key)
        if sess is None or not row.get("path"):
            return False
        now = time.time()
        entry = {**row, "added_at": float(row.get("added_at") or now)}
        result = self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {
                "$push": {"context_documents": entry},
                "$set": {"updated_at": now},
            },
        )
        return result.modified_count > 0

    def remove_context_document(self, session_key: str, doc_id: str) -> bool:
        did = (doc_id or "").strip()
        if not did:
            return False
        result = self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {
                "$pull": {"context_documents": {"id": did}},
                "$set": {"updated_at": time.time()},
            },
        )
        return result.modified_count > 0

    def clear_context_documents(self, session_key: str) -> bool:
        result = self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {
                "$set": {"context_documents": [], "updated_at": time.time()},
            },
        )
        return result.matched_count > 0

    def create_session(
        self,
        *,
        agent_id: str,
        title: str,
        session_key: Optional[str] = None,
        source: str = "dashboard",
        user_id: Optional[str] = None,
        team_id: Optional[str] = None,
        swarm_mode: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        sid = uuid.uuid4().hex
        aid = (agent_id or "main").strip() or "main"
        key = (session_key or "").strip() or (
            f"agent:{aid}:qcm-{int(now * 1000)}-{secrets_hex(4)}"
        )
        label = (title or "Nova conversa").strip() or "Nova conversa"
        tid = (team_id or "").strip() or None
        swarm = bool(swarm_mode)
        self._sessions.insert_one(
            {
                "_scope": self._scope,
                "id": sid,
                "session_key": key,
                "agent_id": aid,
                "title": label,
                "source": source,
                "user_id": user_id,
                "team_id": tid,
                "swarm_mode": swarm,
                "created_at": now,
                "updated_at": now,
            }
        )
        return {
            "id": sid,
            "session_key": key,
            "agentId": aid,
            "title": label,
            "source": source,
            "team_id": tid,
            "swarm_mode": swarm,
            "updatedAt": int(now * 1000),
            "kind": "webchat",
        }

    def get_session_by_key(self, session_key: str) -> Optional[ChatSessionRow]:
        doc = self._sessions.find_one(
            {"_scope": self._scope, "session_key": session_key.strip()}
        )
        if doc is None:
            return None
        return _doc_to_session(doc)

    def touch_session(self, session_key: str, *, title: Optional[str] = None) -> None:
        now = time.time()
        updates: dict[str, Any] = {"updated_at": now}
        if title:
            updates["title"] = title.strip()
        self._sessions.update_one(
            {"_scope": self._scope, "session_key": session_key.strip()},
            {"$set": updates},
        )

    def list_sessions(
        self,
        *,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 2000))
        cursor = (
            self._sessions.find(self._session_filter(agent_id=agent_id, user_id=user_id))
            .sort("updated_at", -1)
            .limit(lim)
        )
        out: list[dict[str, Any]] = []
        for doc in cursor:
            sess = _doc_to_session(doc)
            out.append(
                {
                    "key": sess.session_key,
                    "sessionId": sess.id,
                    "agentId": sess.agent_id,
                    "title": sess.title,
                    "kind": "webchat",
                    "updatedAt": int(sess.updated_at * 1000),
                    "source": "history",
                    "team_id": sess.team_id,
                    "swarm_mode": sess.swarm_mode,
                }
            )
        return out

    def append_message(
        self,
        session_key: str,
        *,
        role: str,
        text: str,
        tools_used: Optional[list[str]] = None,
        extras: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return None
        now = time.time()
        mid = uuid.uuid4().hex
        if extras:
            from .openclaw_chat_media_tools import sanitize_extras_for_persistence

            extras = sanitize_extras_for_persistence(extras)
        self._messages.insert_one(
            {
                "_scope": self._scope,
                "id": mid,
                "session_id": sess.id,
                "role": role,
                "text": text,
                "tools_used": json.dumps(tools_used) if tools_used else None,
                "extras_json": json.dumps(extras) if extras else None,
                "created_at": now,
            }
        )
        self._sessions.update_one(
            {"_scope": self._scope, "id": sess.id},
            {"$set": {"updated_at": now}},
        )
        return mid

    def count_conversation_messages(self, session_key: str) -> int:
        """Count user and assistant rows (excludes status/progress lines)."""
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return 0
        return self._messages.count_documents(
            {
                "_scope": self._scope,
                "session_id": sess.id,
                "role": {"$in": ["user", "assistant"]},
            }
        )

    def list_messages(
        self,
        session_key: str,
        *,
        limit: int = 120,
        light: bool = False,
    ) -> list[dict[str, Any]]:
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return []
        lim = max(1, min(int(limit), 1000))
        rows = list(
            self._messages.find(
                {"_scope": self._scope, "session_id": sess.id},
                {"role": 1, "text": 1, "tools_used": 1, "extras_json": 1, "created_at": 1},
            )
            .sort("created_at", -1)
            .limit(lim)
        )
        rows.reverse()
        out: list[dict[str, Any]] = []
        for doc in rows:
            role = doc.get("role", "")
            if role == "user":
                mapped = "user"
            elif role == "status":
                mapped = "status"
            elif role == "reasoning":
                mapped = "reasoning"
            else:
                mapped = "assistant"
            created = float(doc.get("created_at") or 0.0)
            entry: dict[str, Any] = {
                "role": mapped,
                "text": doc.get("text", ""),
                "timestamp": _iso_from_epoch(created),
                "id": f"m-{created}",
            }
            raw_tools = doc.get("tools_used")
            if raw_tools:
                try:
                    parsed_tools = json.loads(raw_tools)
                    if isinstance(parsed_tools, list) and parsed_tools:
                        skills_list = [
                            t[6:] for t in parsed_tools if t.startswith("skill:")
                        ]
                        tools_list = [
                            t for t in parsed_tools if not t.startswith("skill:")
                        ]
                        if tools_list:
                            entry["toolsUsed"] = tools_list
                        if skills_list:
                            entry["skillsUsed"] = skills_list
                except (json.JSONDecodeError, TypeError):
                    pass
            raw_extras = doc.get("extras_json")
            if raw_extras:
                try:
                    parsed_extras = json.loads(raw_extras)
                    if isinstance(parsed_extras, dict):
                        from .openclaw_chat_media_tools import hydrate_extras_from_paths

                        parsed_extras = hydrate_extras_from_paths(
                            parsed_extras,
                            light=light,
                        )
                        if parsed_extras.get("media"):
                            entry["media"] = parsed_extras["media"]
                        if parsed_extras.get("wacliQr"):
                            entry["wacliQr"] = parsed_extras["wacliQr"]
                        if parsed_extras.get("desktopScreenshot"):
                            entry["desktopScreenshot"] = parsed_extras[
                                "desktopScreenshot"
                            ]
                        if parsed_extras.get("interactiveQuestion"):
                            entry["interactiveQuestion"] = parsed_extras[
                                "interactiveQuestion"
                            ]
                        if parsed_extras.get("templatePreview"):
                            entry["templatePreview"] = parsed_extras["templatePreview"]
                        if parsed_extras.get("miniCurriculumPreview"):
                            entry["miniCurriculumPreview"] = parsed_extras[
                                "miniCurriculumPreview"
                            ]
                        if parsed_extras.get("creativeWidget"):
                            entry["creativeWidget"] = parsed_extras["creativeWidget"]
                        if parsed_extras.get("mcpSkillsUsed"):
                            entry["mcpSkillsUsed"] = parsed_extras["mcpSkillsUsed"]
                        if parsed_extras.get("modelLabel"):
                            entry["modelLabel"] = parsed_extras["modelLabel"]
                        if parsed_extras.get("modelId"):
                            entry["modelId"] = parsed_extras["modelId"]
                        attachments = parsed_extras.get("attachments")
                        if attachments:
                            entry["attachments"] = attachments
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(entry)
        return out

    def add_context(
        self,
        session_key: str,
        *,
        kind: str,
        content: str,
        importance: float = 1.0,
        metadata: Optional[dict[str, Any]] = None,
        context_id: Optional[str] = None,
    ) -> Optional[str]:
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return None
        body = (content or "").strip()
        if not body:
            return None
        cid = (context_id or "").strip() or uuid.uuid4().hex
        now = time.time()
        self._context.insert_one(
            {
                "_scope": self._scope,
                "id": cid,
                "session_id": sess.id,
                "kind": kind,
                "content": body,
                "importance": float(importance),
                "metadata_json": (
                    json.dumps(metadata, ensure_ascii=False) if metadata else None
                ),
                "created_at": now,
            }
        )
        return cid

    def list_context(
        self,
        session_key: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        sess = self.get_session_by_key(session_key)
        if sess is None:
            return []
        lim = max(1, min(int(limit), 100))
        cursor = (
            self._context.find(
                {"_scope": self._scope, "session_id": sess.id},
                {"kind": 1, "content": 1, "importance": 1, "metadata_json": 1, "created_at": 1},
            )
            .sort("created_at", -1)
            .limit(lim)
        )
        out: list[dict[str, Any]] = []
        for doc in cursor:
            meta = {}
            raw_meta = doc.get("metadata_json")
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                except json.JSONDecodeError:
                    meta = {}
            out.append(
                {
                    "kind": doc.get("kind", ""),
                    "content": doc.get("content", ""),
                    "importance": doc.get("importance", 1.0),
                    "metadata": meta,
                    "created_at": doc.get("created_at", 0.0),
                }
            )
        return out

    def list_user_memory(
        self,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_limit: int = 30,
        facts_per_session: int = 12,
    ) -> dict[str, Any]:
        """Aggregate stored memory across sessions for the dashboard memory view."""
        slim = max(1, min(int(session_limit), 200))
        flim = max(1, min(int(facts_per_session), 50))
        sessions_out: list[dict[str, Any]] = []
        by_kind: dict[str, int] = {}
        grand_total = 0
        session_docs = list(
            self._sessions.find(self._session_filter(agent_id=agent_id, user_id=user_id))
            .sort("updated_at", -1)
            .limit(slim)
        )
        for sdoc in session_docs:
            sess = _doc_to_session(sdoc)
            ctx_filt = {"_scope": self._scope, "session_id": sess.id}
            total_for_session = self._context.count_documents(ctx_filt)
            for row in self._context.aggregate(
                [
                    {"$match": ctx_filt},
                    {"$group": {"_id": "$kind", "c": {"$sum": 1}}},
                ]
            ):
                kind = row["_id"]
                count = int(row["c"])
                by_kind[kind] = by_kind.get(kind, 0) + count
                grand_total += count
            fact_rows = list(
                self._context.find(
                    ctx_filt,
                    {"kind": 1, "content": 1, "importance": 1, "metadata_json": 1, "created_at": 1},
                )
                .sort("created_at", -1)
                .limit(flim)
            )
            facts: list[dict[str, Any]] = []
            for doc in fact_rows:
                meta: dict[str, Any] = {}
                raw_meta = doc.get("metadata_json")
                if raw_meta:
                    try:
                        meta = json.loads(raw_meta)
                    except json.JSONDecodeError:
                        meta = {}
                facts.append(
                    {
                        "kind": doc.get("kind", ""),
                        "content": doc.get("content", ""),
                        "importance": float(doc.get("importance") or 1.0),
                        "metadata": meta,
                        "created_at": _iso_from_epoch(float(doc.get("created_at") or 0.0)),
                    }
                )
            sessions_out.append(
                {
                    "session_key": sess.session_key,
                    "title": sess.title,
                    "agent_id": sess.agent_id,
                    "updated_at": _iso_from_epoch(sess.updated_at),
                    "created_at": _iso_from_epoch(sess.created_at),
                    "fact_count": int(total_for_session),
                    "facts": facts,
                }
            )
        return {
            "ok": True,
            "total_sessions": len(sessions_out),
            "total_facts": grand_total,
            "by_kind": by_kind,
            "sessions": sessions_out,
            "user_id": user_id,
            "agent_id": agent_id,
        }

    def record_exchange(
        self,
        session_key: str,
        *,
        user_text: str,
        assistant_text: str,
        tools_used: Optional[list[str]] = None,
        title: Optional[str] = None,
    ) -> None:
        """Persist a user/assistant turn and extract lightweight context facts."""
        if title:
            self.touch_session(session_key, title=title)
        else:
            self.touch_session(session_key)
        self.append_message(session_key, role="user", text=user_text)
        self.append_message(
            session_key, role="assistant", text=assistant_text, tools_used=tools_used
        )
        for fact in extract_context_facts(
            user_text, assistant_text, tools_used=tools_used
        ):
            meta = dict(fact.get("metadata") or {})
            cid = uuid.uuid4().hex
            meta["context_id"] = cid
            self.add_context(
                session_key,
                kind=fact["kind"],
                content=fact["content"],
                importance=fact.get("importance", 1.0),
                metadata=meta,
                context_id=cid,
            )

    def delete_session(
        self,
        session_key: str,
        *,
        user_id: Optional[str] = None,
    ) -> bool:
        key = (session_key or "").strip()
        if not key:
            return False
        doc = self._sessions.find_one(
            {"_scope": self._scope, "session_key": key},
            {"id": 1},
        )
        if doc is None:
            return False
        sid = doc["id"]
        scope = {"_scope": self._scope}
        self._messages.delete_many({**scope, "session_id": sid})
        self._context.delete_many({**scope, "session_id": sid})
        # Legacy SQLite imports keyed messages/context by session_key.
        self._messages.delete_many({**scope, "session_key": key})
        self._context.delete_many({**scope, "session_key": key})
        result = self._sessions.delete_one({**scope, "id": sid})
        return result.deleted_count > 0


def secrets_hex(nbytes: int) -> str:
    return uuid.uuid4().hex[: max(2, nbytes * 2)]


def _doc_to_session(doc: dict[str, Any]) -> ChatSessionRow:
    return ChatSessionRow(
        id=doc.get("id", ""),
        session_key=doc.get("session_key", ""),
        agent_id=doc.get("agent_id", ""),
        title=doc.get("title", ""),
        source=doc.get("source", "dashboard"),
        user_id=doc.get("user_id"),
        team_id=doc.get("team_id"),
        created_at=float(doc.get("created_at") or 0.0),
        updated_at=float(doc.get("updated_at") or 0.0),
        swarm_mode=bool(doc.get("swarm_mode") or False),
    )


def _iso_from_epoch(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def extract_context_facts(
    user_text: str,
    assistant_text: str,
    *,
    tools_used: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Heuristic extraction of durable facts from a chat turn."""
    facts: list[dict[str, Any]] = []
    user = (user_text or "").strip()
    assistant = (assistant_text or "").strip()
    if user:
        summary = user if len(user) <= 400 else user[:397] + "…"
        facts.append(
            {
                "kind": "user_intent",
                "content": summary,
                "importance": 1.0,
            }
        )
    if assistant:
        summary = assistant if len(assistant) <= 600 else assistant[:597] + "…"
        facts.append(
            {
                "kind": "assistant_summary",
                "content": summary,
                "importance": 0.9,
            }
        )
    if tools_used:
        facts.append(
            {
                "kind": "tools",
                "content": "Ferramentas: " + ", ".join(tools_used),
                "importance": 0.7,
                "metadata": {"tools": tools_used},
            }
        )
    combined = f"{user}\n{assistant}"
    for url in _URL_RE.findall(combined)[:5]:
        facts.append(
            {"kind": "url", "content": url, "importance": 0.6, "metadata": {"url": url}}
        )
    for path in _PATH_RE.findall(combined)[:5]:
        clean = path.strip()
        facts.append(
            {
                "kind": "path",
                "content": clean,
                "importance": 0.65,
                "metadata": {"path": clean},
            }
        )
    return facts


@dataclass
class ChatPersistence:
    """MongoDB history + optional ChromaDB retrieval."""

    history: "ChatHistoryStore"
    memory: Any = None

    def remember_exchange(
        self,
        session_key: str,
        *,
        user_text: str,
        assistant_text: str,
        tools_used: Optional[list[str]] = None,
        extras: Optional[dict[str, Any]] = None,
        title: Optional[str] = None,
        agent_id: str = "main",
        user_already_saved: bool = False,
    ) -> None:
        key = session_key.strip()
        if not key:
            return
        if self.history.get_session_by_key(key) is None:
            self.history.create_session(
                agent_id=agent_id,
                title=(title or "Nova conversa").strip() or "Nova conversa",
                session_key=key,
                source="dashboard",
            )
        if title:
            self.history.touch_session(key, title=title)
        else:
            self.history.touch_session(key)
        user_mid = (
            "saved"
            if user_already_saved
            else self.history.append_message(key, role="user", text=user_text)
        )
        asst_mid = self.history.append_message(
            key,
            role="assistant",
            text=assistant_text,
            tools_used=tools_used,
            extras=extras,
        )
        if user_mid is None or asst_mid is None:
            log.warning(
                "chat history: could not append messages for session %s", key
            )
            return
        sess = self.history.get_session_by_key(key)
        try:
            for fact in extract_context_facts(
                user_text, assistant_text, tools_used=tools_used
            ):
                cid = uuid.uuid4().hex
                self.history.add_context(
                    key,
                    kind=fact["kind"],
                    content=fact["content"],
                    importance=fact.get("importance", 1.0),
                    metadata={**(fact.get("metadata") or {}), "context_id": cid},
                    context_id=cid,
                )
                if (
                    sess is not None
                    and self.memory is not None
                    and getattr(self.memory, "available", False)
                ):
                    self.memory.index_context(
                        context_id=cid,
                        session_key=key,
                        agent_id=sess.agent_id,
                        kind=fact["kind"],
                        content=fact["content"],
                        importance=float(fact.get("importance", 1.0)),
                    )
        except Exception as e:
            log.warning("chat context indexing failed for %s: %s", key, e)

    def project_and_session_context_block(
        self,
        session_key: str,
        query: str,
        *,
        agent_id: Optional[str] = None,
        project_store: Any = None,
        project_max_chars: int = 12000,
        session_limit: int = 8,
        project_search_limit: int = 8,
    ) -> tuple[str, str]:
        """Combine project memory prompt + Chroma hits with per-session context."""
        project_parts: list[str] = []
        if project_store is not None:
            block = project_store.prompt_block(project_max_chars)
            if block:
                project_parts.append(block)
            if self.memory is not None:
                hits = project_store.search_chroma(
                    self.memory,
                    query,
                    limit=project_search_limit,
                )
                if hits:
                    chroma_block = format_context_block(
                        hits,
                        max_chars=max(500, project_max_chars // 3),
                    )
                    if chroma_block:
                        project_parts.append(chroma_block)
        project_block = "\n\n".join(project_parts).strip()
        session_block = self.relevant_context_block(
            session_key,
            query,
            agent_id=agent_id,
            limit=session_limit,
        )
        return project_block, session_block

    def relevant_context_block(
        self,
        session_key: str,
        query: str,
        *,
        agent_id: Optional[str] = None,
        limit: int = 8,
    ) -> str:
        facts: list[dict[str, Any]] = []
        if self.memory is not None and getattr(self.memory, "available", False):
            for hit in self.memory.search(
                query, session_key=session_key, agent_id=agent_id, limit=limit
            ):
                facts.append(
                    {
                        "kind": hit.get("kind", "memory"),
                        "content": hit.get("content", ""),
                    }
                )
        if len(facts) < limit:
            for row in self.history.list_context(session_key, limit=limit):
                facts.append(row)
        return format_context_block(facts[:limit])


def format_context_block(facts: list[dict[str, Any]], *, max_chars: int = 4000) -> str:
    if not facts:
        return ""
    lines: list[str] = []
    total = 0
    for fact in facts:
        line = f"- [{fact.get('kind', 'note')}] {fact.get('content', '')}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def migrate_sqlite_to_mongo(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
) -> int:
    """Copy rows from a legacy chat SQLite db into MongoDB. Non-destructive."""
    import sqlite3

    from .mongo_sqlite_bridge import import_sqlite_rows

    path = resolve_scope_path(scope_path, db_path=db_path)
    if not path.is_file():
        return 0
    store = ChatHistoryStore(path)
    if store._sessions.count_documents({"_scope": store._scope}, limit=1) > 0:
        return 0

    def _session_doc(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        doc = {k: row[k] for k in keys}
        if "team_id" not in keys:
            doc["team_id"] = None
        return doc

    def _message_doc(row: sqlite3.Row) -> dict[str, Any]:
        doc = {k: row[k] for k in row.keys()}
        if "extras_json" not in doc:
            doc["extras_json"] = None
        return doc

    def _context_doc(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in row.keys()}

    counts = [
        import_sqlite_rows(
            path,
            table="chat_sessions",
            collection=SESSIONS_COLLECTION,
            scope=store._scope,
            row_to_doc=_session_doc,
        ),
        import_sqlite_rows(
            path,
            table="chat_messages",
            collection=MESSAGES_COLLECTION,
            scope=store._scope,
            row_to_doc=_message_doc,
        ),
        import_sqlite_rows(
            path,
            table="chat_context",
            collection=CONTEXT_COLLECTION,
            scope=store._scope,
            row_to_doc=_context_doc,
        ),
    ]
    total = sum(counts)
    if total:
        log.info(
            "Imported chat history from %s into MongoDB (%d sessions, %d messages, %d context)",
            path,
            counts[0],
            counts[1],
            counts[2],
        )
    return total

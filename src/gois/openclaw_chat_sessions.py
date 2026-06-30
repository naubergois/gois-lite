"""OpenClaw session keys, on-disk store, and dashboard session CRUD."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .chat_history import ChatPersistence
from .chat_jobs import cancel_jobs_for_session
from .config import OpenclawChatConfig

from .openclaw_chat_runtime import QclawRuntime

log = logging.getLogger(__name__)

_SESSION_LIST_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SESSION_FILE_CACHE: dict[str, tuple[float, dict]] = {}


def set_session_list_cache(key: str, value: tuple[float, list[dict]]) -> None:
    _SESSION_LIST_CACHE[key] = value


def get_session_list_cache(key: str) -> tuple[float, list[dict]] | None:
    return _SESSION_LIST_CACHE.get(key)


def clear_session_list_cache() -> None:
    _SESSION_LIST_CACHE.clear()


def _read_sessions_json(store_path: Path) -> Optional[dict]:
    """Read and parse a sessions.json, using mtime-based caching for large files."""
    path_str = str(store_path)
    try:
        st = store_path.stat()
        mtime = st.st_mtime
    except OSError:
        return None
    cached = _SESSION_FILE_CACHE.get(path_str)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        store = json.loads(store_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s: %s", store_path, e)
        return None
    if not isinstance(store, dict):
        return None
    _SESSION_FILE_CACHE[path_str] = (mtime, store)
    return store

def _session_kind(key: str) -> str:
    if ":cron:" in key:
        return "cron"
    if ":whatsapp:" in key:
        return "whatsapp"
    if key.endswith(":main"):
        return "main"
    if ":webchat:" in key or ":session-" in key or ":conv-" in key:
        return "webchat"
    return "other"


def protected_session_delete_reason(
    session_key: str,
    *,
    kind: str = "",
    protected_keys: Optional[set[str]] = None,
) -> Optional[str]:
    """Return a user-facing reason when a dashboard session must not be deleted."""
    key = (session_key or "").strip()
    if not key:
        return "session_key inválida"
    if key.startswith("hermes:"):
        return "Remova perfis Hermes no dashboard Hermes."
    kind_norm = (kind or _session_kind(key)).strip().lower()
    if kind_norm == "whatsapp" or ":whatsapp:" in key:
        return "A sessão WhatsApp é fixa no painel e não pode ser apagada."
    if kind_norm == "cron" or ":cron:" in key:
        return "Sessões cron são geridas pelo OpenClaw."
    if kind_norm == "main" or key.endswith(":main"):
        return "A sessão principal do agente não pode ser apagada aqui."
    protected = {str(k).strip() for k in (protected_keys or set()) if str(k).strip()}
    if key in protected:
        return "Esta conversa está protegida pelo monitor."
    return None


def _session_title(key: str, entry: dict) -> str:
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    parts = key.split(":")
    if len(parts) >= 4:
        return ":".join(parts[3:])
    return parts[-1] if parts else key


def _summarize_store_entries(store: dict) -> list[dict]:
    rows: list[dict] = []
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        sid = entry.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        updated = entry.get("updatedAt")
        try:
            updated_at = int(updated) if updated is not None else 0
        except (TypeError, ValueError):
            updated_at = 0
        rows.append(
            {
                "key": key,
                "sessionId": sid,
                "updatedAt": updated_at,
                "title": _session_title(key, entry),
                "kind": _session_kind(key),
                "lastTo": entry.get("lastTo"),
                "model": entry.get("model"),
            }
        )
    rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return rows

def _iso_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _new_session_title() -> str:
    """Human-friendly default title for a freshly created chat session."""
    return f"Nova conversa {datetime.now().strftime('%H:%M')}"

def _agent_from_session_key(session_key: str, fallback: str) -> str:
    parts = session_key.split(":")
    if len(parts) >= 2 and parts[0] == "agent":
        return parts[1] or fallback
    return fallback


def _sessions_store_path(runtime: QclawRuntime, agent_id: str) -> Path:
    return runtime.state_dir / "agents" / agent_id / "sessions" / "sessions.json"


def _load_session_store(store_path: Path) -> dict:
    if not store_path.is_file():
        return {}
    return _read_sessions_json(store_path) or {}


def _save_session_store(store_path: Path, store: dict) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=2) + "\n")
    # Update the file cache so subsequent reads don't re-parse.
    try:
        mtime = store_path.stat().st_mtime
        _SESSION_FILE_CACHE[str(store_path)] = (mtime, store)
    except OSError:
        _SESSION_FILE_CACHE.pop(str(store_path), None)


def _ensure_session_entry(
    runtime: QclawRuntime,
    *,
    session_key: str,
    agent_id: str,
    title: Optional[str] = None,
) -> tuple[str, dict]:
    """Ensure sessions.json has an entry; return (session_id, entry)."""
    store_path = _sessions_store_path(runtime, agent_id)
    store = _load_session_store(store_path)
    entry = store.get(session_key)
    now_ms = int(time.time() * 1000)
    if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
        entry = dict(entry)
        entry["updatedAt"] = now_ms
        if title and not entry.get("label"):
            entry["label"] = title
        store[session_key] = entry
        _save_session_store(store_path, store)
        return entry["sessionId"], entry

    session_id = secrets.token_hex(16)
    entry = {
        "sessionId": session_id,
        "updatedAt": now_ms,
        "label": title or _new_session_title(),
    }
    store[session_key] = entry
    _save_session_store(store_path, store)
    return session_id, entry


def _append_transcript_message(
    runtime: QclawRuntime,
    *,
    agent_id: str,
    session_id: str,
    role: str,
    text: str,
) -> None:
    jsonl_path = (
        runtime.state_dir / "agents" / agent_id / "sessions" / f"{session_id}.jsonl"
    )
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "message",
        "id": f"m-{secrets.token_hex(8)}",
        "timestamp": _iso_timestamp(),
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

HERMES_SESSION_PREFIX = "hermes:"


def hermes_session_key(profile_name: str) -> str:
    """Dashboard session id when listing Hermes profiles as chat sessions."""
    slug = (profile_name or "").strip()
    return f"{HERMES_SESSION_PREFIX}{slug}"


def parse_hermes_session_key(session_key: str) -> Optional[str]:
    """Return Hermes profile slug from a hermes:… session key, or None."""
    key = (session_key or "").strip()
    if not key.startswith(HERMES_SESSION_PREFIX):
        return None
    slug = key[len(HERMES_SESSION_PREFIX) :].strip()
    return slug or None


def openclaw_key_for_hermes_profile(profile_name: str, default_agent: str) -> str:
    """Map a Hermes profile to a stable OpenClaw transcript key (for send/history)."""
    aid = (default_agent or "main").strip() or "main"
    slug = (profile_name or "").strip() or "default"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug).strip("-") or "default"
    return f"agent:{aid}:hermes-{safe}"


def related_session_keys_for_job_cancel(
    session_key: str,
    *,
    default_agent: str = "main",
) -> frozenset[str]:
    """Expand a UI or send session key to all aliases bound to the same chat."""
    key = (session_key or "").strip()
    if not key:
        return frozenset()
    keys: set[str] = {key}
    profile = parse_hermes_session_key(key)
    if profile:
        keys.add(openclaw_key_for_hermes_profile(profile, default_agent))
    else:
        match = re.match(r"^agent:[^:]+:hermes-(.+)$", key)
        if match:
            keys.add(f"{HERMES_SESSION_PREFIX}{match.group(1)}")
    return frozenset(keys)


def default_session_key(chat_cfg: OpenclawChatConfig, runtime: QclawRuntime) -> str:
    """Return configured default or derive a monitor-specific session key."""
    if chat_cfg.default_session_key:
        return chat_cfg.default_session_key
    return f"agent:{chat_cfg.default_agent}:gois-dashboard"


def new_session_key(agent_id: str = "main") -> str:
    """Generate a fresh OpenClaw session key (persisted on first send)."""
    aid = (agent_id or "main").strip() or "main"
    conv_id = secrets.token_hex(8)  # 16-hex conversation hash (unique per thread)
    return f"agent:{aid}:conv-{conv_id}"


def list_openclaw_agents(runtime: QclawRuntime) -> list[str]:
    """Agent ids from openclaw.json and on-disk agents/*/sessions."""
    found: set[str] = set()
    agents_root = runtime.state_dir / "agents"
    if agents_root.is_dir():
        for agent_dir in agents_root.iterdir():
            if agent_dir.is_dir():
                found.add(agent_dir.name)

    try:
        cfg = json.loads(runtime.config_path.read_text())
    except (OSError, json.JSONDecodeError):
        cfg = {}
    if isinstance(cfg, dict):
        agents = cfg.get("agents")
        if isinstance(agents, dict):
            agent_list = agents.get("list")
            if isinstance(agent_list, list):
                for entry in agent_list:
                    if isinstance(entry, dict):
                        aid = entry.get("id")
                        if isinstance(aid, str) and aid.strip():
                            found.add(aid.strip())
            defaults = agents.get("defaults")
            if isinstance(defaults, dict):
                default_id = defaults.get("id")
                if isinstance(default_id, str) and default_id.strip():
                    found.add(default_id.strip())

    if not found:
        return ["main"]
    return sorted(found)


def create_openclaw_session(
    runtime: QclawRuntime,
    *,
    agent_id: str = "main",
    title: Optional[str] = None,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """Create a new session in SQLite (primary) and OpenClaw sessions.json."""
    aid = (agent_id or "main").strip() or "main"
    key = new_session_key(aid)
    resolved_title = (title or "").strip() or _new_session_title()
    tid = (team_id or "").strip() or None
    if persistence is not None:
        created = persistence.history.create_session(
            agent_id=aid,
            title=resolved_title,
            session_key=key,
            source="dashboard",
            user_id=user_id,
            team_id=tid,
        )
        key = created["session_key"]
    session_id, entry = _ensure_session_entry(
        runtime,
        session_key=key,
        agent_id=aid,
        title=resolved_title,
    )
    clear_session_list_cache()
    return {
        "ok": True,
        "session_key": key,
        "sessionId": session_id,
        "agentId": aid,
        "title": entry.get("label") or resolved_title,
        "kind": "webchat",
        "source": "history" if persistence is not None else "openclaw",
        "team_id": tid,
    }


def _locate_session_store_entry(
    runtime: QclawRuntime,
    session_key: str,
    *,
    agent_id: Optional[str] = None,
) -> Optional[tuple[str, Path, dict]]:
    """Find (agent_id, store_path, entry) for a session key across agent stores."""
    key = (session_key or "").strip()
    if not key:
        return None
    hinted = (agent_id or _agent_from_session_key(key, "main") or "main").strip() or "main"
    agents_root = runtime.state_dir / "agents"
    if not agents_root.is_dir():
        return None

    search_order: list[str] = []
    if hinted:
        search_order.append(hinted)
    for agent_dir in sorted(agents_root.iterdir()):
        if agent_dir.is_dir() and agent_dir.name not in search_order:
            search_order.append(agent_dir.name)

    for aid in search_order:
        store_path = _sessions_store_path(runtime, aid)
        store = _load_session_store(store_path)
        entry = store.get(key)
        if isinstance(entry, dict):
            return aid, store_path, entry
    return None


def ensure_session_persisted(
    runtime: QclawRuntime,
    persistence: ChatPersistence,
    *,
    session_key: str,
    user_id: Optional[str] = None,
) -> bool:
    """Ensure a dashboard history row exists for an OpenClaw session key."""
    key = (session_key or "").strip()
    if not key:
        return False
    if persistence.history.get_session_by_key(key) is not None:
        return True
    located = _locate_session_store_entry(runtime, key)
    if located:
        aid, _, entry = located
        persistence.history.create_session(
            agent_id=aid,
            title=str(entry.get("label") or key),
            session_key=key,
            source="openclaw",
            user_id=user_id,
        )
        return True
    aid = _agent_from_session_key(key, "main")
    persistence.history.create_session(
        agent_id=aid,
        title=key,
        session_key=key,
        source="dashboard",
        user_id=user_id,
    )
    return True


def delete_openclaw_session_entry(
    runtime: QclawRuntime,
    *,
    session_key: str,
    agent_id: str,
) -> bool:
    """Remove one session from every matching OpenClaw sessions.json store."""
    key = (session_key or "").strip()
    if not key:
        return False
    agents_root = runtime.state_dir / "agents"
    if not agents_root.is_dir():
        return False

    hinted = (agent_id or _agent_from_session_key(key, "main") or "main").strip() or "main"
    search_order: list[str] = []
    if hinted:
        search_order.append(hinted)
    for agent_dir in sorted(agents_root.iterdir()):
        if agent_dir.is_dir() and agent_dir.name not in search_order:
            search_order.append(agent_dir.name)

    removed = False
    for aid in search_order:
        store_path = _sessions_store_path(runtime, aid)
        store = _load_session_store(store_path)
        entry = store.get(key)
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        del store[key]
        _save_session_store(store_path, store)
        if isinstance(session_id, str) and session_id:
            jsonl_path = store_path.parent / f"{session_id}.jsonl"
            try:
                jsonl_path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete transcript %s: %s", jsonl_path, e)
        removed = True
    return removed


def _session_still_present(
    runtime: QclawRuntime,
    session_key: str,
    *,
    agent_id: str,
    persistence: Optional[ChatPersistence] = None,
) -> bool:
    """True when the session still exists in Mongo history and/or OpenClaw store."""
    key = (session_key or "").strip()
    if not key:
        return False
    if persistence is not None and persistence.history.get_session_by_key(key) is not None:
        return True
    return _locate_session_store_entry(runtime, key, agent_id=agent_id) is not None


def delete_openclaw_session(
    runtime: QclawRuntime,
    *,
    session_key: str,
    agent_id: Optional[str] = None,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    protected_keys: Optional[set[str]] = None,
    kind: str = "",
) -> dict[str, Any]:
    """Delete one OpenClaw dashboard session (sessions.json + SQLite history)."""
    key = (session_key or "").strip()
    if not key:
        return {"ok": False, "error": "session_key is required"}

    blocked = protected_session_delete_reason(
        key,
        kind=kind,
        protected_keys=protected_keys,
    )
    if blocked:
        return {"ok": False, "error": blocked, "protected": True, "session_key": key}

    aid = (agent_id or _agent_from_session_key(key, "main") or "main").strip() or "main"
    cancelled_jobs = cancel_jobs_for_session(
        key,
        also_keys=related_session_keys_for_job_cancel(key, default_agent=aid),
    )

    removed_disk = delete_openclaw_session_entry(
        runtime,
        session_key=key,
        agent_id=aid,
    )
    removed_db = False
    if persistence is not None:
        removed_db = persistence.history.delete_session(key, user_id=user_id)

    if _session_still_present(runtime, key, agent_id=aid, persistence=persistence):
        if persistence is not None:
            removed_db = (
                persistence.history.delete_session(key, user_id=user_id) or removed_db
            )
        if _locate_session_store_entry(runtime, key, agent_id=aid) is not None:
            removed_disk = delete_openclaw_session_entry(
                runtime,
                session_key=key,
                agent_id=aid,
            ) or removed_disk

    deleted = not _session_still_present(
        runtime, key, agent_id=aid, persistence=persistence
    )
    clear_session_list_cache()
    out: dict[str, Any] = {
        "ok": deleted,
        "session_key": key,
        "deleted": deleted,
        "removed_disk": bool(removed_disk),
        "removed_db": bool(removed_db),
        "cancelled_jobs": cancelled_jobs,
        "source": "openclaw",
    }
    if not deleted:
        if _session_still_present(runtime, key, agent_id=aid, persistence=persistence):
            out["error"] = "não foi possível remover a conversa de todas as fontes"
        else:
            out["error"] = "session not found"
    return out


def purge_openclaw_sessions(
    runtime: QclawRuntime,
    *,
    mode: str,
    older_than_days: int = 30,
    protect_keys: Optional[set[str]] = None,
    agent_id: Optional[str] = None,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    limit: int = 2000,
) -> dict[str, Any]:
    """Delete dashboard chat sessions (SQLite + OpenClaw store). mode: old | all."""
    from .openclaw_chat import list_sessions

    mode_norm = (mode or "").strip().lower()
    if mode_norm not in {"old", "all"}:
        return {"ok": False, "error": "mode must be 'old' or 'all'"}

    days = max(1, min(int(older_than_days or 30), 3650))
    protected = {str(k).strip() for k in (protect_keys or set()) if str(k).strip()}
    # List sessions from ALL agents so purge covers the full sidebar.
    # The caller's agent_id is only used as a hint for individual
    # session deletion, not to restrict the scan.
    listing = list_sessions(
        runtime,
        limit=max(1, min(int(limit), 2000)),
        agent_id=None,
        cache_seconds=0,
        persistence=persistence,
        user_id=user_id,
        skip_cache=True,
    )
    if not listing.get("ok"):
        return listing

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - days * 86400 * 1000
    deleted: list[str] = []
    skipped: list[str] = []

    for row in listing.get("sessions") or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        if key in protected:
            skipped.append(key)
            continue
        kind = str(row.get("kind") or "")
        if kind in {"whatsapp", "cron", "main"} or key.startswith("hermes:"):
            skipped.append(key)
            continue
        if mode_norm == "old":
            try:
                updated_at = int(row.get("updatedAt") or 0)
            except (TypeError, ValueError):
                updated_at = 0
            if updated_at >= cutoff_ms:
                skipped.append(key)
                continue

        aid = str(row.get("agentId") or _agent_from_session_key(key, "main"))
        removed_disk = delete_openclaw_session_entry(
            runtime, session_key=key, agent_id=aid
        )
        removed_db = False
        if persistence is not None:
            removed_db = persistence.history.delete_session(key, user_id=user_id)
        if removed_disk or removed_db:
            deleted.append(key)

    clear_session_list_cache()
    return {
        "ok": True,
        "mode": mode_norm,
        "deleted_count": len(deleted),
        "deleted": deleted[:50],
        "skipped_count": len(skipped),
        "older_than_days": days if mode_norm == "old" else None,
    }

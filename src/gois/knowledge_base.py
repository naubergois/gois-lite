"""Project knowledge base extracted automatically from chat history.

Stores extractions per chat session under ``.knowledge-base/`` (JSON) and
exposes helpers to (a) trigger an LLM-powered extraction over the sessions
persisted by :class:`ChatHistoryStore` and (b) aggregate the per-session
results into a flat ``projects + requirements`` view consumed by the
``/conhecimento`` dashboard page.

The extraction is incremental: a session is re-processed only when its
most recent message timestamp differs from the stored ``last_message_at``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .chat_history import ChatHistoryStore
from .chat_models import (
    anthropic_messages_create,
    build_openai_client,
    completion_extra_kwargs,
    resolve_chat_model,
)
from .config import OpenclawChatConfig

log = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = ".knowledge-base"
_INDEX_FILE = "index.json"
_LOCK_FILE = "extract.lock"

_REQUIREMENT_TYPES = {"functional", "nonfunctional", "todo", "bug", "improvement", "decision"}
_PRIORITIES = {"high", "medium", "low"}
_STATUSES = {"open", "in-progress", "done", "blocked"}

_SYSTEM_PROMPT = (
    "Você é um analista de requisitos. Receberá uma transcrição de chat entre "
    "um usuário e um assistente de IA discutindo um projeto de software. "
    "Sua tarefa é extrair, em JSON estrito (UTF-8, sem comentários, sem texto "
    "fora do JSON), os dados estruturados do projeto.\n\n"
    "Use EXATAMENTE este schema:\n"
    "{\n"
    '  "project_name": string|null,            // nome curto do projeto (ex: "gois")\n'
    '  "project_summary": string|null,         // 1-3 frases descrevendo o projeto/tema\n'
    '  "topics": [string],                     // 3-8 tópicos curtos\n'
    '  "entities": [string],                   // arquivos, módulos, serviços mencionados\n'
    '  "people": [                             // pessoas mencionadas na conversa\n'
    "    {\n"
    '      "name": string,                     // nome completo ou apelido\n'
    '      "role": string,                     // papel/função (ex: "desenvolvedor", "cliente")\n'
    '      "notes": string                     // observações curtas (até 160 chars)\n'
    "    }\n"
    "  ],\n"
    '  "videos": [                             // vídeos, gravações ou links de vídeo mencionados\n'
    "    {\n"
    '      "title": string,                    // título ou descrição do vídeo\n'
    '      "url": string|null,                 // URL se disponível\n'
    '      "platform": string|null,            // ex: "YouTube", "Loom", "Google Meet"\n'
    '      "notes": string                     // contexto curto (até 160 chars)\n'
    "    }\n"
    "  ],\n"
    '  "requirements": [\n'
    "    {\n"
    '      "title": string,                    // título curto e acionável\n'
    '      "description": string,              // 1-3 frases\n'
    '      "type": "functional"|"nonfunctional"|"todo"|"bug"|"improvement"|"decision",\n'
    '      "priority": "high"|"medium"|"low",\n'
    '      "status": "open"|"in-progress"|"done"|"blocked",\n'
    '      "source_quote": string              // trecho curto (até 240 chars) da conversa\n'
    "    }\n"
    "  ],\n"
    '  "decisions": [string],                  // decisões técnicas tomadas\n'
    '  "open_questions": [string],             // dúvidas em aberto\n'
    '  "next_actions": [string]                // próximos passos sugeridos\n'
    "}\n\n"
    "Regras: responda APENAS o JSON; nunca invente dados que não estão "
    "embasados na conversa; deduplique itens repetidos; se um campo não tiver "
    "dados, devolva [] ou null."
)

_USER_PROMPT_PREFIX = (
    "Transcrição da conversa (mais antigas no topo, mais recentes no fim).\n"
    "Cada linha começa com [role]:\n\n"
)

_MAX_TRANSCRIPT_CHARS = 28000
_MAX_MESSAGES_PER_SESSION = 200
_LLM_TIMEOUT_SECONDS = 90.0
_PROJECT_NAME_MIN_LEN = 2
_PROJECT_NAME_MAX_LEN = 80


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionResult:
    session_key: str
    extraction: dict[str, Any]
    last_message_at: Optional[str]
    extracted_at: float
    model_id: str


class KnowledgeStore:
    """JSON-backed persistence for per-session extractions."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / _INDEX_FILE
        self.lock_path = self.root / _LOCK_FILE
        self._lock = threading.Lock()

    # ----- raw IO --------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "sessions": {}, "updated_at": 0.0}
        try:
            with self.index_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("knowledge_base: failed to load index (%s); resetting", exc)
            return {"version": 1, "sessions": {}, "updated_at": 0.0}
        if not isinstance(data, dict):
            return {"version": 1, "sessions": {}, "updated_at": 0.0}
        data.setdefault("version", 1)
        data.setdefault("sessions", {})
        data.setdefault("updated_at", 0.0)
        if not isinstance(data["sessions"], dict):
            data["sessions"] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        tmp = self.index_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        tmp.replace(self.index_path)

    # ----- public --------------------------------------------------------
    def get_session_state(self, session_key: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
        sess = data["sessions"].get(session_key)
        return sess if isinstance(sess, dict) else {}

    def upsert_extraction(
        self,
        session_key: str,
        extraction: dict[str, Any],
        *,
        last_message_at: Optional[str],
        model_id: str,
        session_meta: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            data = self._load()
            entry = data["sessions"].get(session_key) or {}
            entry["session_key"] = session_key
            entry["extraction"] = extraction
            entry["last_message_at"] = last_message_at
            entry["extracted_at"] = time.time()
            entry["model_id"] = model_id
            if session_meta:
                entry["session_meta"] = session_meta
            data["sessions"][session_key] = entry
            self._save(data)

    def remove_missing(self, present_keys: Iterable[str]) -> int:
        keep = set(present_keys)
        with self._lock:
            data = self._load()
            dropped = [k for k in list(data["sessions"]) if k not in keep]
            for k in dropped:
                del data["sessions"][k]
            if dropped:
                self._save(data)
        return len(dropped)

    def all_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
        out: list[dict[str, Any]] = []
        for entry in data["sessions"].values():
            if isinstance(entry, dict):
                out.append(entry)
        return out

    def delete_sessions(self, session_keys: Iterable[str]) -> int:
        keys = {str(k) for k in session_keys if k}
        if not keys:
            return 0
        with self._lock:
            data = self._load()
            removed = 0
            for k in list(data["sessions"]):
                if k in keys:
                    del data["sessions"][k]
                    removed += 1
            if removed:
                self._save(data)
        return removed

    def delete_project(self, project_key: str) -> dict[str, Any]:
        pkey = (project_key or "").strip().lower()
        if not pkey:
            return {"removed": 0, "session_keys": []}
        affected: list[str] = []
        with self._lock:
            data = self._load()
            for skey, entry in list(data["sessions"].items()):
                if not isinstance(entry, dict):
                    continue
                extraction = entry.get("extraction") or {}
                meta = entry.get("session_meta") or {}
                name = (
                    extraction.get("project_name")
                    or meta.get("title")
                    or "Sem projeto"
                )
                if _project_key(name) == pkey:
                    affected.append(skey)
            for skey in affected:
                del data["sessions"][skey]
            if affected:
                self._save(data)
        return {"removed": len(affected), "session_keys": affected}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
        return {
            "sessions": len(data["sessions"]),
            "updated_at": data.get("updated_at", 0.0),
            "store_path": str(self.index_path),
            "extraction_running": self.lock_path.exists(),
        }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _normalize_string(value: Any, *, max_len: int = 500) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _normalize_string_list(value: Any, *, max_items: int = 16, max_len: int = 200) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        s = _normalize_string(item, max_len=max_len)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max_items:
            break
    return out


def _normalize_requirement(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _normalize_string(item.get("title"), max_len=160)
    description = _normalize_string(item.get("description"), max_len=600)
    if not title and not description:
        return None
    if not title:
        title = description[:80] + ("…" if len(description) > 80 else "")
    rtype = _normalize_string(item.get("type"), max_len=20).lower() or "functional"
    if rtype not in _REQUIREMENT_TYPES:
        rtype = "functional"
    priority = _normalize_string(item.get("priority"), max_len=10).lower() or "medium"
    if priority not in _PRIORITIES:
        priority = "medium"
    status = _normalize_string(item.get("status"), max_len=20).lower() or "open"
    if status not in _STATUSES:
        status = "open"
    quote = _normalize_string(item.get("source_quote"), max_len=240)
    return {
        "title": title,
        "description": description,
        "type": rtype,
        "priority": priority,
        "status": status,
        "source_quote": quote,
    }


def _normalize_person(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    name = _normalize_string(item.get("name"), max_len=120)
    if not name:
        return None
    return {
        "name": name,
        "role": _normalize_string(item.get("role"), max_len=80),
        "notes": _normalize_string(item.get("notes"), max_len=160),
    }


def _normalize_video(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _normalize_string(item.get("title"), max_len=200)
    if not title:
        return None
    return {
        "title": title,
        "url": _normalize_string(item.get("url"), max_len=500) or None,
        "platform": _normalize_string(item.get("platform"), max_len=60) or None,
        "notes": _normalize_string(item.get("notes"), max_len=160),
    }


def _normalize_extraction(raw: Any) -> dict[str, Any]:
    """Coerce LLM output into the canonical schema, dropping junk."""
    base: dict[str, Any] = {
        "project_name": None,
        "project_summary": None,
        "topics": [],
        "entities": [],
        "people": [],
        "videos": [],
        "requirements": [],
        "decisions": [],
        "open_questions": [],
        "next_actions": [],
    }
    if not isinstance(raw, dict):
        return base
    name = _normalize_string(raw.get("project_name"), max_len=_PROJECT_NAME_MAX_LEN)
    if len(name) >= _PROJECT_NAME_MIN_LEN:
        base["project_name"] = name
    summary = _normalize_string(raw.get("project_summary"), max_len=600)
    if summary:
        base["project_summary"] = summary
    base["topics"] = _normalize_string_list(raw.get("topics"), max_items=10, max_len=80)
    base["entities"] = _normalize_string_list(raw.get("entities"), max_items=20, max_len=120)
    base["decisions"] = _normalize_string_list(raw.get("decisions"), max_items=12, max_len=300)
    base["open_questions"] = _normalize_string_list(
        raw.get("open_questions"), max_items=12, max_len=300
    )
    base["next_actions"] = _normalize_string_list(
        raw.get("next_actions"), max_items=12, max_len=300
    )
    people_raw = raw.get("people")
    if isinstance(people_raw, list):
        out_people: list[dict[str, Any]] = []
        seen_people: set[str] = set()
        for item in people_raw[:20]:
            norm = _normalize_person(item)
            if not norm:
                continue
            sig = norm["name"].lower()
            if sig in seen_people:
                continue
            seen_people.add(sig)
            out_people.append(norm)
        base["people"] = out_people
    videos_raw = raw.get("videos")
    if isinstance(videos_raw, list):
        out_videos: list[dict[str, Any]] = []
        seen_videos: set[str] = set()
        for item in videos_raw[:20]:
            norm = _normalize_video(item)
            if not norm:
                continue
            sig = norm["title"].lower()
            if sig in seen_videos:
                continue
            seen_videos.add(sig)
            out_videos.append(norm)
        base["videos"] = out_videos
    reqs = raw.get("requirements")
    if isinstance(reqs, list):
        out_reqs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in reqs[:30]:
            norm = _normalize_requirement(item)
            if not norm:
                continue
            sig = norm["title"].lower()
            if sig in seen:
                continue
            seen.add(sig)
            out_reqs.append(norm)
        base["requirements"] = out_reqs
    return base


def _build_transcript(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"[{role}] {text}")
    transcript = "\n\n".join(parts)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        # keep the most recent context, which usually has the actionable items
        transcript = "…[truncated]…\n\n" + transcript[-_MAX_TRANSCRIPT_CHARS:]
    return transcript


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_json_response(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    # Strip ```json fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def _call_llm(
    chat_cfg: OpenclawChatConfig,
    transcript: str,
    *,
    model_id: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    resolved, err = resolve_chat_model(chat_cfg, model_id)
    if err or resolved is None:
        raise RuntimeError(err or "modelo de chat indisponível")
    user_text = _USER_PROMPT_PREFIX + transcript
    if resolved.entry.provider == "anthropic":
        reply = anthropic_messages_create(
            resolved,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
            timeout=_LLM_TIMEOUT_SECONDS,
        )
        return _parse_json_response(reply), resolved.entry.id
    # openai-compatible (DeepSeek, OpenAI, Gemini compat)
    client = build_openai_client(resolved, timeout=_LLM_TIMEOUT_SECONDS)
    kwargs: dict[str, Any] = {
        "model": resolved.entry.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        **completion_extra_kwargs(resolved.entry),
    }
    # Best-effort JSON mode for providers that support it.
    try:
        kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
    except Exception:
        kwargs.pop("response_format", None)
        resp = client.chat.completions.create(**kwargs)
    text = ""
    if resp and resp.choices:
        text = resp.choices[0].message.content or ""
    return _parse_json_response(text), resolved.entry.id


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _acquire_lock(store: KnowledgeStore) -> bool:
    try:
        fd = store.lock_path.open("x")
    except FileExistsError:
        # If the lock is older than 30 min, assume stale and steal it.
        try:
            age = time.time() - store.lock_path.stat().st_mtime
        except OSError:
            age = 0.0
        if age > 1800:
            try:
                store.lock_path.unlink()
            except OSError:
                pass
            return _acquire_lock(store)
        return False
    fd.write(str(time.time()))
    fd.close()
    return True


def _release_lock(store: KnowledgeStore) -> None:
    try:
        store.lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("knowledge_base: could not release lock: %s", exc)


def run_extraction(
    *,
    chat_history: ChatHistoryStore,
    store: KnowledgeStore,
    chat_cfg: OpenclawChatConfig,
    sessions_limit: int = 50,
    messages_limit: int = _MAX_MESSAGES_PER_SESSION,
    force: bool = False,
    model_id: Optional[str] = None,
    prune_missing: bool = True,
) -> dict[str, Any]:
    """Walk recent chat sessions and (re)extract requirements via LLM."""
    if not _acquire_lock(store):
        return {
            "ok": False,
            "error": "extração já em execução (lock ativo)",
            "lock_path": str(store.lock_path),
        }
    started_at = time.time()
    processed = 0
    skipped = 0
    failures: list[dict[str, Any]] = []
    used_model: Optional[str] = None
    try:
        sessions = chat_history.list_sessions(limit=sessions_limit)
        for sess in sessions:
            key = sess.get("key")
            if not key:
                continue
            messages = chat_history.list_messages(key, limit=messages_limit)
            if not messages:
                skipped += 1
                continue
            latest_ts = messages[-1].get("timestamp")
            state = store.get_session_state(key)
            if not force and state and state.get("last_message_at") == latest_ts:
                skipped += 1
                continue
            transcript = _build_transcript(messages)
            if not transcript.strip():
                skipped += 1
                continue
            try:
                raw, model_used = _call_llm(chat_cfg, transcript, model_id=model_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                log.warning("knowledge_base: extract failed for %s: %s", key, exc)
                failures.append({"session_key": key, "error": str(exc)[:240]})
                continue
            extraction = _normalize_extraction(raw)
            used_model = model_used
            store.upsert_extraction(
                key,
                extraction,
                last_message_at=latest_ts,
                model_id=model_used,
                session_meta={
                    "title": sess.get("title"),
                    "agent_id": sess.get("agentId"),
                    "updated_at": sess.get("updatedAt"),
                    "kind": sess.get("kind"),
                },
            )
            processed += 1
        if prune_missing:
            store.remove_missing(s.get("key") for s in sessions if s.get("key"))
    finally:
        _release_lock(store)
    return {
        "ok": True,
        "processed": processed,
        "skipped": skipped,
        "failures": failures,
        "model_id": used_model,
        "duration_seconds": round(time.time() - started_at, 2),
        "total_sessions": len(sessions),
    }


# ---------------------------------------------------------------------------
# Aggregation for the UI
# ---------------------------------------------------------------------------


def _project_key(name: Optional[str]) -> str:
    if not name:
        return "__sem_projeto__"
    return re.sub(r"\s+", " ", name.strip()).lower()


def aggregate_knowledge(store: KnowledgeStore) -> dict[str, Any]:
    """Flatten per-session extractions into projects + requirements lists."""
    entries = store.all_entries()
    projects: dict[str, dict[str, Any]] = {}
    requirements: list[dict[str, Any]] = []
    sessions_view: list[dict[str, Any]] = []

    for entry in entries:
        extraction = entry.get("extraction") or {}
        meta = entry.get("session_meta") or {}
        session_key = entry.get("session_key") or ""
        project_name = extraction.get("project_name") or meta.get("title") or "Sem projeto"
        pkey = _project_key(project_name)
        proj = projects.setdefault(
            pkey,
            {
                "key": pkey,
                "name": project_name,
                "summary": extraction.get("project_summary"),
                "topics": [],
                "entities": [],
                "decisions": [],
                "open_questions": [],
                "next_actions": [],
                "session_keys": [],
                "requirement_count": 0,
                "updated_at": 0,
            },
        )
        # Prefer the longest non-empty summary we see.
        new_summary = extraction.get("project_summary")
        if new_summary and (not proj["summary"] or len(new_summary) > len(proj["summary"])):
            proj["summary"] = new_summary
        proj["topics"] = _merge_lists(proj["topics"], extraction.get("topics") or [])
        proj["entities"] = _merge_lists(proj["entities"], extraction.get("entities") or [])
        proj["decisions"] = _merge_lists(proj["decisions"], extraction.get("decisions") or [])
        proj["open_questions"] = _merge_lists(
            proj["open_questions"], extraction.get("open_questions") or []
        )
        proj["next_actions"] = _merge_lists(
            proj["next_actions"], extraction.get("next_actions") or []
        )
        if session_key and session_key not in proj["session_keys"]:
            proj["session_keys"].append(session_key)
        upd = meta.get("updated_at") or 0
        try:
            upd = int(upd)
        except (TypeError, ValueError):
            upd = 0
        if upd > proj["updated_at"]:
            proj["updated_at"] = upd

        for idx, req in enumerate(extraction.get("requirements") or []):
            requirements.append(
                {
                    "id": f"{session_key}#{idx}",
                    "project": project_name,
                    "project_key": pkey,
                    "title": req.get("title"),
                    "description": req.get("description"),
                    "type": req.get("type"),
                    "priority": req.get("priority"),
                    "status": req.get("status"),
                    "source_quote": req.get("source_quote"),
                    "session_key": session_key,
                    "session_title": meta.get("title"),
                    "updated_at": upd,
                }
            )
            proj["requirement_count"] += 1

        sessions_view.append(
            {
                "session_key": session_key,
                "title": meta.get("title"),
                "agent_id": meta.get("agent_id"),
                "updated_at": meta.get("updated_at"),
                "extracted_at": entry.get("extracted_at"),
                "model_id": entry.get("model_id"),
                "project": project_name,
                "requirement_count": len(extraction.get("requirements") or []),
            }
        )

    projects_list = sorted(
        projects.values(),
        key=lambda p: (-p["updated_at"], p["name"].lower()),
    )
    requirements.sort(
        key=lambda r: (
            -(r.get("updated_at") or 0),
            _priority_rank(r.get("priority")),
            r.get("title") or "",
        )
    )
    sessions_view.sort(key=lambda s: -(s.get("extracted_at") or 0))

    return {
        "ok": True,
        "stats": store.stats(),
        "projects": projects_list,
        "requirements": requirements,
        "sessions": sessions_view,
    }


def _merge_lists(existing: list[str], incoming: list[str]) -> list[str]:
    seen = {x.lower() for x in existing}
    out = list(existing)
    for item in incoming:
        if not isinstance(item, str):
            continue
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
        if len(out) >= 25:
            break
    return out


_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _priority_rank(value: Any) -> int:
    return _PRIORITY_RANK.get(str(value or "").lower(), 3)


# ---------------------------------------------------------------------------
# Entity database — aggregate people, entities, projects, videos across all sessions
# ---------------------------------------------------------------------------


def aggregate_entity_db(store: KnowledgeStore) -> dict[str, Any]:
    """Aggregate all people, entities, projects, and videos across sessions."""
    entries = store.all_entries()

    people_map: dict[str, dict[str, Any]] = {}
    entities_map: dict[str, dict[str, Any]] = {}
    projects_map: dict[str, dict[str, Any]] = {}
    videos_map: dict[str, dict[str, Any]] = {}

    for entry in entries:
        extraction = entry.get("extraction") or {}
        meta = entry.get("session_meta") or {}
        session_key = entry.get("session_key") or ""
        project_name = extraction.get("project_name") or meta.get("title") or "Sem projeto"
        pkey = _project_key(project_name)
        upd = meta.get("updated_at") or 0
        try:
            upd = int(upd)
        except (TypeError, ValueError):
            upd = 0

        # --- projects ---
        if pkey not in projects_map:
            projects_map[pkey] = {
                "key": pkey,
                "name": project_name,
                "summary": extraction.get("project_summary"),
                "topics": list(extraction.get("topics") or []),
                "session_keys": [],
                "updated_at": upd,
            }
        proj = projects_map[pkey]
        if new_summary := extraction.get("project_summary"):
            if not proj["summary"] or len(new_summary) > len(proj["summary"]):
                proj["summary"] = new_summary
        proj["topics"] = _merge_lists(proj["topics"], extraction.get("topics") or [])
        if session_key and session_key not in proj["session_keys"]:
            proj["session_keys"].append(session_key)
        if upd > proj["updated_at"]:
            proj["updated_at"] = upd

        # --- people ---
        for person in extraction.get("people") or []:
            sig = (person.get("name") or "").strip().lower()
            if not sig:
                continue
            if sig not in people_map:
                people_map[sig] = {
                    "name": person.get("name"),
                    "role": person.get("role") or "",
                    "notes": person.get("notes") or "",
                    "projects": [],
                    "session_keys": [],
                }
            p = people_map[sig]
            if person.get("role") and not p["role"]:
                p["role"] = person["role"]
            if person.get("notes") and len(person["notes"]) > len(p["notes"]):
                p["notes"] = person["notes"]
            if project_name not in p["projects"]:
                p["projects"].append(project_name)
            if session_key and session_key not in p["session_keys"]:
                p["session_keys"].append(session_key)

        # --- entities ---
        for ent in extraction.get("entities") or []:
            sig = (ent or "").strip().lower()
            if not sig:
                continue
            if sig not in entities_map:
                entities_map[sig] = {
                    "name": ent.strip(),
                    "projects": [],
                    "session_keys": [],
                    "mention_count": 0,
                }
            e = entities_map[sig]
            e["mention_count"] += 1
            if project_name not in e["projects"]:
                e["projects"].append(project_name)
            if session_key and session_key not in e["session_keys"]:
                e["session_keys"].append(session_key)

        # --- videos ---
        for video in extraction.get("videos") or []:
            sig = (video.get("title") or "").strip().lower()
            if not sig:
                continue
            if sig not in videos_map:
                videos_map[sig] = {
                    "title": video.get("title"),
                    "url": video.get("url"),
                    "platform": video.get("platform"),
                    "notes": video.get("notes") or "",
                    "projects": [],
                    "session_keys": [],
                }
            v = videos_map[sig]
            if video.get("url") and not v["url"]:
                v["url"] = video["url"]
            if video.get("platform") and not v["platform"]:
                v["platform"] = video["platform"]
            if project_name not in v["projects"]:
                v["projects"].append(project_name)
            if session_key and session_key not in v["session_keys"]:
                v["session_keys"].append(session_key)

    return {
        "ok": True,
        "stats": store.stats(),
        "projects": sorted(
            projects_map.values(),
            key=lambda p: (-p["updated_at"], p["name"].lower()),
        ),
        "people": sorted(people_map.values(), key=lambda p: (p.get("name") or "").lower()),
        "entities": sorted(
            entities_map.values(),
            key=lambda e: (-e["mention_count"], e["name"].lower()),
        ),
        "videos": sorted(videos_map.values(), key=lambda v: (v.get("title") or "").lower()),
    }


# ---------------------------------------------------------------------------
# Helpers used by Monitor / CLI
# ---------------------------------------------------------------------------


def default_store_path(history_db_path: str | Path) -> Path:
    """Return the standard knowledge-base directory next to the history DB."""
    base = Path(history_db_path).expanduser().resolve().parent
    return base / _DEFAULT_STORE_DIR


# ---------------------------------------------------------------------------
# Prompt block (used by the RuFlo chat to inject project memory)
# ---------------------------------------------------------------------------


def _score_project_against_query(proj: dict[str, Any], query_terms: list[str]) -> int:
    if not query_terms:
        return 0
    haystack = " ".join(
        [
            str(proj.get("name") or ""),
            str(proj.get("summary") or ""),
            " ".join(proj.get("topics") or []),
            " ".join(proj.get("entities") or []),
            " ".join(proj.get("decisions") or []),
            " ".join(proj.get("open_questions") or []),
            " ".join(proj.get("next_actions") or []),
        ]
    ).lower()
    return sum(1 for term in query_terms if term and term in haystack)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_projects_knowledge_block(
    snapshot: dict[str, Any],
    *,
    query: str = "",
    max_projects: int = 4,
    max_chars: int = 4000,
) -> str:
    """Render a compact textual block with the most relevant project memories.

    Selection mixes (a) projects that match terms in ``query`` and (b) the most
    recently updated projects as fallback. Each project gets a short summary,
    topics, decisions and up to 5 open requirements.
    """
    projects = (snapshot or {}).get("projects") or []
    requirements = (snapshot or {}).get("requirements") or []
    if not projects:
        return ""

    terms = [
        t.lower()
        for t in re.split(r"[^\wÀ-ÖØ-öø-ÿ]+", (query or "").strip())
        if len(t) >= 3
    ]

    scored = sorted(
        projects,
        key=lambda p: (
            -_score_project_against_query(p, terms),
            -(int(p.get("updated_at") or 0)),
            str(p.get("name") or "").lower(),
        ),
    )[: max(1, max_projects)]

    open_reqs_by_proj: dict[str, list[dict[str, Any]]] = {}
    for req in requirements:
        if str(req.get("status") or "").lower() in {"done"}:
            continue
        pkey = req.get("project_key") or _project_key(req.get("project"))
        open_reqs_by_proj.setdefault(pkey, []).append(req)

    parts: list[str] = [
        "Memória de projetos (extraída dos chats anteriores). "
        "Use APENAS se relevante para a tarefa atual; cite o projeto quando aplicável."
    ]
    for proj in scored:
        name = proj.get("name") or "Sem projeto"
        summary = _truncate(str(proj.get("summary") or ""), 320)
        topics = ", ".join((proj.get("topics") or [])[:6])
        decisions = (proj.get("decisions") or [])[:3]
        next_actions = (proj.get("next_actions") or [])[:3]
        open_qs = (proj.get("open_questions") or [])[:3]
        pkey = proj.get("key") or _project_key(name)
        reqs = open_reqs_by_proj.get(pkey, [])[:5]

        block_lines = [f"\n## {name}"]
        if summary:
            block_lines.append(f"Resumo: {summary}")
        if topics:
            block_lines.append(f"Tópicos: {topics}")
        if decisions:
            block_lines.append("Decisões: " + "; ".join(_truncate(d, 140) for d in decisions))
        if next_actions:
            block_lines.append(
                "Próximas ações: " + "; ".join(_truncate(a, 140) for a in next_actions)
            )
        if open_qs:
            block_lines.append(
                "Perguntas em aberto: " + "; ".join(_truncate(q, 140) for q in open_qs)
            )
        if reqs:
            block_lines.append("Requisitos abertos:")
            for req in reqs:
                title = _truncate(str(req.get("title") or "(sem título)"), 90)
                pr = req.get("priority") or "-"
                st = req.get("status") or "open"
                tp = req.get("type") or "-"
                block_lines.append(f"  - [{tp}/{pr}/{st}] {title}")
        parts.append("\n".join(block_lines))

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


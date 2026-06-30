"""Chat send jobs for non-blocking dashboard requests (memory + MongoDB)."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .mongo import get_collection

log = logging.getLogger(__name__)

CHAT_SEND_JOBS_COLLECTION = "chat_send_jobs"

_MAX_JOBS = 200
_JOB_TTL_SECONDS = 3600.0
_STALE_CHAT_JOB_SECONDS = 900.0  # 15 min - libertar conversas presas no async_send
_STALE_BATCH_JOB_SECONDS = 900.0  # 15 min - PDF/slides em background
_STALE_BATCH_PARTIAL_SECONDS = 300.0  # 5 min - image_batch parado a meio (ex.: 1/3)
_STALE_SINGLE_IMAGE_SECONDS = 300.0  # 5 min - uma imagem presa em "A gerar…"
_IMAGE_BATCH_MAX_CONSECUTIVE_FAILURES = 10
_BATCH_JOB_KINDS = frozenset({"image_batch", "pdf_preview", "book_pipeline", "swarm_run"})
BATCH_JOB_KINDS = _BATCH_JOB_KINDS
# Long-running batch jobs (Hermes/cron) must keep executing without a dashboard tab.
_PRESENCE_EXEMPT_JOB_KINDS = frozenset({"swarm_run", "book_pipeline"})

_lock = threading.Lock()
_jobs: dict[str, ChatSendJob] = {}
_image_batch_failure_streak: dict[str, int] = {}
_persist_path: Optional[Path] = None
_indexes_created = False
_STREAM_QUEUES: dict[str, list[queue.Queue]] = {}


def _image_batch_session_key(job: ChatSendJob) -> str:
    return (job.display_key or job.session_key or "").strip()


def image_batch_failure_limit() -> int:
    return _IMAGE_BATCH_MAX_CONSECUTIVE_FAILURES


def image_batch_failure_streak(session_key: str) -> int:
    key = (session_key or "").strip()
    if not key:
        return 0
    with _lock:
        return int(_image_batch_failure_streak.get(key, 0))


def image_batch_failure_limit_reached(session_key: str) -> bool:
    return image_batch_failure_streak(session_key) >= _IMAGE_BATCH_MAX_CONSECUTIVE_FAILURES


def image_batch_failure_limit_message(*, streak: Optional[int] = None) -> str:
    count = streak if streak is not None else _IMAGE_BATCH_MAX_CONSECUTIVE_FAILURES
    return (
        f"Lote de imagens interrompido após {count} tentativa(s) consecutiva(s) "
        "sem sucesso. Verifique credenciais, prompts ou provider e reenvie o pedido."
    )


def record_image_batch_outcome(
    session_key: str,
    *,
    had_success: bool,
) -> int:
    """Track consecutive image-batch failures per session (metrics/logging only)."""
    key = (session_key or "").strip()
    if not key:
        return 0
    with _lock:
        if had_success:
            _image_batch_failure_streak.pop(key, None)
            return 0
        streak = int(_image_batch_failure_streak.get(key, 0)) + 1
        _image_batch_failure_streak[key] = streak
        return streak


def stop_image_batch_job_for_failure_limit(
    job_id: str,
    *,
    streak: Optional[int] = None,
) -> bool:
    """Deprecated: batches run to completion while the conversation exists."""
    _ = (job_id, streak)
    return False


@dataclass
class ChatSendJob:
    id: str
    session_key: str
    status: str  # running | done | error
    kind: str = "chat"  # chat | image_batch | pdf_preview | swarm_run
    display_key: str = ""
    message_text: str = ""
    agent_id: Optional[str] = None
    profile: Optional[str] = None
    user_id: Optional[str] = None
    model_id: Optional[str] = None
    attachments_json: Optional[str] = None
    progress: list[str] = field(default_factory=list)
    last_progress: str = ""
    tool_turn: int = 0
    tool_max: int = 0
    block_turn: int = 0
    block_max: int = 0
    partial_media: Optional[dict[str, Any]] = None
    partial_reply: str = ""
    partial_reasoning: str = ""
    model_attempts: list[dict[str, Any]] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


def _scope_for_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _current_scope() -> Optional[str]:
    if _persist_path is None:
        return None
    return _scope_for_path(_persist_path)


def _collection():
    return get_collection(CHAT_SEND_JOBS_COLLECTION)


def _ensure_indexes() -> None:
    global _indexes_created
    if _indexes_created:
        return
    try:
        col = _collection()
        col.create_index([("_scope", 1), ("id", 1)], unique=True)
        col.create_index([("_scope", 1), ("status", 1), ("started_at", 1)])
        _indexes_created = True
    except Exception as exc:  # pragma: no cover - index race/perm
        log.debug("chat_send_jobs index setup skipped: %s", exc)


def migrate_sqlite_to_mongo(path: Path) -> int:
    """Copy rows from a legacy send_jobs.sqlite3 into MongoDB. Non-destructive."""
    from .mongo_sqlite_bridge import import_sqlite_rows, scope_from_path

    resolved = Path(path).expanduser().resolve()
    return import_sqlite_rows(
        resolved,
        table="chat_send_jobs",
        collection=CHAT_SEND_JOBS_COLLECTION,
        scope=scope_from_path(resolved),
        row_to_doc=lambda row: {k: row[k] for k in row.keys()},
    )


def init_chat_jobs_persistence(path: Optional[Path]) -> None:
    """Enable MongoDB persistence and load interrupted running jobs into memory."""
    global _persist_path
    if path is None:
        _persist_path = None
        with _lock:
            _jobs.clear()
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _persist_path = resolved
    _ensure_indexes()
    loaded = _load_running_from_db(resolved)
    with _lock:
        for job in loaded:
            _jobs[job.id] = job
    if loaded:
        log.info("loaded %d interrupted chat send job(s) from %s", len(loaded), resolved)


def _parse_progress(doc: dict[str, Any]) -> list[str]:
    progress = doc.get("progress")
    if isinstance(progress, list):
        return [str(p) for p in progress]
    progress_raw = doc.get("progress_json") or "[]"
    try:
        parsed = json.loads(progress_raw)
        if isinstance(parsed, list):
            return [str(p) for p in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _parse_partial_media(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    media = doc.get("partial_media")
    if isinstance(media, dict):
        return media
    raw = doc.get("partial_media_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def _parse_model_attempts(doc: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = doc.get("model_attempts")
    if isinstance(attempts, list):
        return [a for a in attempts if isinstance(a, dict)]
    raw = doc.get("model_attempts_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _parse_result(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    result = doc.get("result")
    if isinstance(result, dict):
        return result
    result_raw = doc.get("result_json")
    if not result_raw:
        return None
    try:
        parsed = json.loads(result_raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def _doc_to_job(doc: dict[str, Any]) -> ChatSendJob:
    finished_at = doc.get("finished_at")
    return ChatSendJob(
        id=doc.get("id", ""),
        session_key=doc.get("session_key", ""),
        display_key=doc.get("display_key") or "",
        message_text=doc.get("message_text") or "",
        agent_id=doc.get("agent_id"),
        profile=doc.get("profile"),
        user_id=doc.get("user_id"),
        model_id=doc.get("model_id"),
        attachments_json=doc.get("attachments_json"),
        status=doc.get("status", "running"),
        kind=str(doc.get("kind") or "chat"),
        progress=_parse_progress(doc),
        last_progress=doc.get("last_progress") or "",
        tool_turn=int(doc.get("tool_turn") or 0),
        tool_max=int(doc.get("tool_max") or 0),
        block_turn=int(doc.get("block_turn") or 0),
        block_max=int(doc.get("block_max") or 0),
        partial_media=_parse_partial_media(doc),
        partial_reply=doc.get("partial_reply") or "",
        partial_reasoning=doc.get("partial_reasoning") or "",
        model_attempts=_parse_model_attempts(doc),
        result=_parse_result(doc),
        error=doc.get("error"),
        started_at=float(doc.get("started_at") or time.time()),
        finished_at=float(finished_at) if finished_at is not None else None,
    )


def _job_to_doc(job: ChatSendJob, scope: str) -> dict[str, Any]:
    from .openclaw_chat_media_tools import sanitize_chat_job_snapshot_for_persistence

    partial_media, result = sanitize_chat_job_snapshot_for_persistence(
        partial_media=job.partial_media,
        result=job.result,
        kind=job.kind or "chat",
    )
    progress = list(job.progress)
    doc: dict[str, Any] = {
        "_scope": scope,
        "id": job.id,
        "session_key": job.session_key,
        "display_key": job.display_key,
        "message_text": job.message_text,
        "agent_id": job.agent_id,
        "profile": job.profile,
        "user_id": job.user_id,
        "model_id": job.model_id,
        "attachments_json": job.attachments_json,
        "status": job.status,
        "kind": job.kind or "chat",
        "progress": progress,
        "last_progress": job.last_progress,
        "tool_turn": job.tool_turn,
        "tool_max": job.tool_max,
        "block_turn": job.block_turn,
        "block_max": job.block_max,
        "partial_media": partial_media,
        "partial_reply": job.partial_reply or "",
        "partial_reasoning": job.partial_reasoning or "",
        "model_attempts": list(job.model_attempts),
        "result": result,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    # Dict fields are authoritative; JSON mirrors bloat BSON (~2x) on large batches.
    if progress:
        doc["progress_json"] = json.dumps(progress, ensure_ascii=False)
    if partial_media is not None:
        doc["partial_media_json"] = json.dumps(partial_media, default=str)
    if result is not None:
        doc["result_json"] = json.dumps(result, default=str)
    model_attempts = list(job.model_attempts)
    if model_attempts:
        doc["model_attempts_json"] = json.dumps(model_attempts, ensure_ascii=False)
    return doc


def _is_document_too_large(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "DocumentTooLarge":
        return True
    msg = str(exc).lower()
    return "document too large" in msg or "bsonobj size" in msg


def _persist_job(job: ChatSendJob) -> None:
    scope = _current_scope()
    if scope is None:
        return
    from .openclaw_chat_media_tools import ensure_job_doc_fits_bson

    doc = ensure_job_doc_fits_bson(_job_to_doc(job, scope))
    try:
        _collection().replace_one(
            {"_scope": scope, "id": job.id},
            doc,
            upsert=True,
        )
    except Exception as exc:
        if not _is_document_too_large(exc):
            raise
        from .openclaw_chat_media_tools import _sanitize_nested_data_urls

        log.warning(
            "chat job %s document too large - retrying with stripped inline media",
            job.id,
        )
        retry_doc = ensure_job_doc_fits_bson(_sanitize_nested_data_urls(doc))
        _collection().replace_one(
            {"_scope": scope, "id": job.id},
            retry_doc,
            upsert=True,
        )


def _load_running_from_db(path: Path) -> list[ChatSendJob]:
    scope = _scope_for_path(path)
    cursor = _collection().find(
        {"_scope": scope, "status": "running"},
        sort=[("started_at", 1)],
    )
    return [_doc_to_job(doc) for doc in cursor]


def _load_job_from_db(job_id: str) -> Optional[ChatSendJob]:
    scope = _current_scope()
    if scope is None:
        return None
    doc = _collection().find_one({"_scope": scope, "id": job_id})
    return _doc_to_job(doc) if doc is not None else None


def _prune_locked(now: float) -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    stale = [
        jid
        for jid, job in _jobs.items()
        if job.finished_at is not None and (now - job.finished_at) > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)
    if len(_jobs) <= _MAX_JOBS:
        return
    finished = sorted(
        ((jid, j) for jid, j in _jobs.items() if j.finished_at is not None),
        key=lambda x: x[1].finished_at or 0,
    )
    for jid, _ in finished[: max(0, len(_jobs) - _MAX_JOBS)]:
        _jobs.pop(jid, None)


def create_job(
    session_key: str,
    *,
    kind: str = "chat",
    display_key: str = "",
    message_text: str = "",
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    user_id: Optional[str] = None,
    model_id: Optional[str] = None,
    attachments: Optional[list] = None,
) -> ChatSendJob:
    attachments_json = (
        json.dumps(attachments, ensure_ascii=False) if attachments else None
    )
    job = ChatSendJob(
        id=uuid.uuid4().hex[:16],
        session_key=session_key,
        kind=(kind or "chat").strip() or "chat",
        display_key=(display_key or session_key).strip(),
        message_text=message_text,
        agent_id=agent_id,
        profile=profile,
        user_id=user_id,
        model_id=model_id,
        attachments_json=attachments_json,
        status="running",
    )
    with _lock:
        _jobs[job.id] = job
        _prune_locked(time.time())
    _persist_job(job)
    return job


_BLOCK_PROGRESS_RE = re.compile(
    r"(?:"
    r"QCLAW_BLOCK\s+(\d+)\s*/\s*(\d+)|"
    r"QCLAW_BLOCK\s+(\d+)\s+(\d+)|"
    r"[Bb]loco\s+(\d+)\s*/\s*(\d+)|"
    r"[Bb]loco\s+(\d+)\s+de\s+(\d+)|"
    r"[Ss]lides?\s+(\d+)\s*/\s*(\d+)|"
    r"[Ss]lide\s+(\d+)\s*/\s*(\d+)|"
    r"[Ss]lides?\s+(\d+)\s*[-]\s*(\d+)\s+(?:de|/\s*)\s*(\d+)|"
    r"(\d+)\s*/\s*(\d+)\s+slides?|"
    r"(\d{1,4})\s+de\s+(\d{1,4})\s+slides?"
    r")",
    re.IGNORECASE,
)


def _coerce_progress_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SLIDE_NUM_FN_RE = re.compile(r"slide[_-]?(\d{1,3})|(\d{1,3})[_-]slide", re.IGNORECASE)
_TOTAL_SLIDES_CTX_RE = re.compile(
    r"(\d{1,4})\s+slides?|"
    r"(\d{1,4})\s+imagens?|"
    r"slides?\s*(?:total|:)?\s*(\d{1,4})|"
    r"imagens?\s*(?:total|:)?\s*(\d{1,4})|"
    r"de\s+(\d{1,4})\s+slides?|"
    r"de\s+(\d{1,4})\s+imagens?|"
    r"lotes?\s+de\s+(\d{1,4})|"
    r"\d{1,4}\s+de\s+(\d{1,4})|"
    r"(\d{1,4})\s*/\s*(\d{1,4})|"
    r"\d{1,4}\s*[-]\s*(\d{1,4})",
    re.IGNORECASE,
)
_SLIDE_PAIR_RE = re.compile(
    r"(\d{1,4})\s+de\s+(\d{1,4})|"
    r"(\d{1,4})\s*/\s*(\d{1,4})|"
    r"(\d{1,4})\s*[-]\s*(\d{1,4})",
    re.IGNORECASE,
)
_SLIDE_BATCH_CTX_RE = re.compile(
    r"slides?|slide[_\-.]|prompts[_-]|image_batch|slides_batch|"
    r"imagens?\b|imagem\b|lote\s+de\s+slides?|gera(?:ç|c)[ãa]o\s+de\s+slides?|"
    r"slides?\s+prontos?|faltam\s+slides?|batch.*imag|\.png\b",
    re.IGNORECASE,
)
_NON_SLIDE_NUMERIC_RE = re.compile(
    r"testes?\b|build\s+ok|commits?\b|pytest|unit\s+tests?\b|specs?\b|"
    r"arquivos?|ficheiros?|files?\b|zip\b|compact|export|anex|file_count|"
    r"nota[s]?\b|/\s*10\b|escala\s*:|alunos?\b|trabalhos?\b|entreg|"
    r"entre\s+\d+\s*[-–]\s*\d+",
    re.IGNORECASE,
)
_SLIDE_PROGRESS_LINE_RE = re.compile(
    r"\b(?:slide|bloco)\b|image_batch|slides_batch|prompts[_-]",
    re.IGNORECASE,
)
# Agent status dashboards (markdown tables, fabricated batch progress) — never
# treat as ground truth for slide totals or auto-continue.
_UNTRUSTED_AGENT_BATCH_STATUS_RE = re.compile(
    r"(?:\*+)?batch\s+de\s+\d{1,4}\s+slides?(?:\*+)?\s+est[aá]\s+ativo|"
    r"\|\s*\*\*(?:progresso|progress)\*\*|"
    r"turno\s+ferramentas|"
    r"\bjob\s+id\s*:`|"
    r"estimativa\s*:\s*~|"
    r"imagens\s+prontas\s*:\s*\d+|"
    r"tempo\s+decorrid|"
    r"passo\s+\d+\s*/\s*\d+",
    re.IGNORECASE,
)


def _line_for_match(blob: str, match: re.Match[str]) -> str:
    start = blob.rfind("\n", 0, match.start()) + 1
    end = blob.find("\n", match.end())
    if end < 0:
        end = len(blob)
    return blob[start:end].strip()


def _slide_numeric_line_ok(line: str, a: int, b: int) -> bool:
    text = (line or "").strip()
    if (
        not text
        or _SLIDE_AUTO_CONTINUE_MARKER in text
        or _NON_SLIDE_NUMERIC_RE.search(text)
        or _UNTRUSTED_AGENT_BATCH_STATUS_RE.search(text)
    ):
        return False
    if _SLIDE_BATCH_CTX_RE.search(text):
        return True
    if _SLIDE_PROGRESS_LINE_RE.search(text):
        return True
    # Bare X/Y (e.g. 0/199 arquivos) is ambiguous — never treat as slide progress.
    del a, b
    return False


def _infer_slide_number_from_filename(filename: str) -> Optional[int]:
    name = (filename or "").strip()
    if not name:
        return None
    m = _SLIDE_NUM_FN_RE.search(name)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _infer_total_slides_from_text(text: str) -> Optional[int]:
    blob = (text or "").strip()
    if not blob:
        return None
    for m in _SLIDE_PAIR_RE.finditer(blob):
        groups = [g for g in m.groups() if g is not None]
        if len(groups) >= 2:
            try:
                a, b = int(groups[-2]), int(groups[-1])
            except (TypeError, ValueError):
                continue
            if 1 <= b <= 500 and b >= a and _slide_numeric_line_ok(
                _line_for_match(blob, m), a, b
            ):
                return b
    for m in _TOTAL_SLIDES_CTX_RE.finditer(blob):
        line = _line_for_match(blob, m)
        if _NON_SLIDE_NUMERIC_RE.search(line):
            continue
        groups = [g for g in m.groups() if g is not None]
        if len(groups) >= 2:
            try:
                a, b = int(groups[-2]), int(groups[-1])
            except (TypeError, ValueError):
                a, b = 0, 0
            if not _slide_numeric_line_ok(line, a, b):
                continue
        for raw in m.groups():
            if not raw:
                continue
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 500 and _slide_numeric_line_ok(line, 0, n):
                return n
    return None


def _infer_slide_pair_from_text(text: str) -> Optional[tuple[int, int]]:
    """Return (completed_turn, total) from phrases like '20 de 100' or '21/100'."""
    blob = (text or "").strip()
    if not blob:
        return None
    parsed = parse_block_progress(blob)
    if parsed is not None:
        return parsed
    for m in _SLIDE_PAIR_RE.finditer(blob):
        groups = [g for g in m.groups() if g is not None]
        if len(groups) < 2:
            continue
        matched = m.group(0)
        # Hyphen ranges like "21-25" or "96-100" are batch windows, never progress.
        if (
            re.search(r"\d+\s*[-]\s*\d+", matched)
            and "/" not in matched
            and " de " not in matched.lower()
        ):
            continue
        try:
            a, b = int(groups[-2]), int(groups[-1])
        except (TypeError, ValueError):
            continue
        if not _slide_numeric_line_ok(_line_for_match(blob, m), a, b):
            continue
        turn, total = a, b
        if 1 <= total <= 500 and total >= turn:
            return turn, total
    return None


_SLIDE_CONTINUE_RE = re.compile(
    r"^(?:sim|s|yes|y|continue|continuar|continua|prossiga|prosseguir|segue|seguir|"
    r"ok|vai|pode|podes|go|start|inicia|comeca|proximo|proximos)[.!?\\s]*$",
    re.IGNORECASE,
)


def _job_matches_client_key(job: ChatSendJob, client_key: str) -> bool:
    """Match a UI conversation key without cross-talk via shared backend send_key."""
    key = (client_key or "").strip()
    if not key:
        return False
    display = (job.display_key or "").strip()
    bound = (job.session_key or "").strip()
    # When display_key is set, match only that UI conversation — never a
    # shared send_key from another thread.
    if display:
        return display == key
    return bound == key


def _conversation_history_key(display_key: str, send_key: str = "") -> str:
    """Return the single SQLite key for one UI conversation's transcript."""
    dk = (display_key or "").strip()
    sk = (send_key or "").strip()
    if sk and (not dk or sk == dk):
        return sk
    if dk and sk:
        # Hermes UI key (hermes:…) vs OpenClaw backend — messages live under backend.
        return sk
    return dk or sk


def _session_text_blob(
    persistence: Any,
    session_key: str,
    message_text: str = "",
    *,
    limit: int = 80,
    history_keys: Optional[list[str]] = None,
) -> str:
    parts: list[str] = [(message_text or "").strip()]
    keys: list[str] = []
    for raw in [session_key, *(history_keys or [])]:
        key = (raw or "").strip()
        if key and key not in keys:
            keys.append(key)
    if persistence is not None and keys:
        try:
            hist = getattr(persistence, "history", None)
            if hist is not None and hasattr(hist, "list_messages"):
                for key in keys:
                    for row in hist.list_messages(key, limit=limit):
                        if isinstance(row, dict):
                            parts.append(str(row.get("text") or ""))
        except Exception:
            pass
    blob = "\n".join(part for part in parts if part)
    return _strip_slide_auto_continue_blob(blob)


def infer_slide_progress_from_blob(blob: str) -> Optional[tuple[int, int]]:
    """Return (completed_turn, total) inferred from session/history text."""
    text = _strip_slide_auto_continue_blob(blob or "")
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pair: Optional[tuple[int, int]] = None
    for line in reversed(lines):
        parsed = parse_block_progress(line) or _infer_slide_pair_from_text(line)
        if parsed is not None:
            pair = parsed
            break
    if pair is not None:
        turn, total = pair
        if total <= 0:
            return None
        return max(0, int(turn)), max(0, int(total))
    for line in reversed(lines):
        total = _infer_total_slides_from_text(line)
        if total is not None:
            return 0, total
    return None


def find_running_image_batch_job(session_key: str) -> Optional[ChatSendJob]:
    """Return the active background image_batch job for a UI/send session key."""
    key = (session_key or "").strip()
    if not key:
        return None
    best: Optional[ChatSendJob] = None
    for job in list_running_jobs():
        if job.kind != "image_batch":
            continue
        if job.block_max <= 0 and not _image_batch_job_looks_in_progress(job):
            continue
        display = (job.display_key or "").strip()
        bound = (job.session_key or "").strip()
        # When display_key is set, match only that UI conversation — never a
        # shared send_key from another thread.
        if display:
            if display != key:
                continue
        elif bound != key:
            continue
        if best is None or job.block_turn > best.block_turn:
            best = job
    return best


def has_running_chat_job_for_session(session_key: str) -> bool:
    """True when a chat send job is already in flight for this session."""
    return find_running_chat_job_for_client_key(session_key) is not None


def find_running_chat_job_for_client_key(
    client_key: str,
    *,
    kind: str = "chat",
) -> Optional[ChatSendJob]:
    """Return the in-flight chat job for a dashboard/Hermes session key."""
    key = (client_key or "").strip()
    if not key:
        return None
    want = (kind or "chat").strip() or "chat"
    for job in list_running_jobs():
        if (job.kind or "chat") != want:
            continue
        if _job_matches_client_key(job, key):
            return job
    return None


def messages_awaiting_reply_fields(
    messages: list[dict[str, Any]],
    *,
    client_key: str,
) -> dict[str, Any]:
    """Annotate a messages payload when the user is waiting on the assistant."""
    if not messages:
        return {}
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return {}
    running = find_running_chat_job_for_client_key(client_key)
    if running is None:
        return {}
    return {"awaitingReply": True, "runningJobId": running.id}


def resolve_slide_batch_progress(
    session_key: str,
    blob: str = "",
) -> Optional[tuple[int, int]]:
    """Prefer live image_batch job counters; fall back to session text."""
    batch = find_running_image_batch_job(session_key)
    if batch is not None and batch.block_max > 0:
        return max(0, int(batch.block_turn)), max(0, int(batch.block_max))
    return infer_slide_progress_from_blob(blob)


def is_slide_continuation_message(message_text: str) -> bool:
    return bool(_SLIDE_CONTINUE_RE.match((message_text or "").strip()))


_SLIDE_AUTO_CONTINUE_MARKER = "[Continuação automática de lote de slides"


def public_message_text(message_text: str) -> str:
    """Return user-visible text, stripping internal slide-auto-continue instructions."""
    text = (message_text or "").strip()
    if not text or _SLIDE_AUTO_CONTINUE_MARKER not in text:
        return text
    head = text.split(_SLIDE_AUTO_CONTINUE_MARKER, 1)[0].strip()
    return head or text


def _strip_slide_auto_continue_blob(blob: str) -> str:
    """Remove recycled auto-continue blocks before inferring slide progress."""
    text = (blob or "").strip()
    if not text or _SLIDE_AUTO_CONTINUE_MARKER not in text:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        if _SLIDE_AUTO_CONTINUE_MARKER in line:
            break
        kept.append(line)
    return "\n".join(ln.strip() for ln in kept if ln.strip())


def _session_has_slide_batch_context(blob: str) -> bool:
    """True when history shows a real slide image batch, not stray year ranges."""
    text = _strip_slide_auto_continue_blob(blob or "")
    if not text:
        return False
    if re.search(
        r"qclaw_slides_batch|slides_batch_images|\[image_batch\]|image_batch",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"prompts[_\-.][\w-]+\.jsonl", text, re.IGNORECASE):
        return True
    for line in text.splitlines():
        ln = line.strip()
        if not ln or _UNTRUSTED_AGENT_BATCH_STATUS_RE.search(ln):
            continue
        if _SLIDE_PROGRESS_LINE_RE.search(ln):
            return True
        if re.search(r"\d+\s*(?:de|/)\s*\d+\s+slides?\b", ln, re.IGNORECASE):
            return True
        if re.search(r"slides?\s+prontos?", ln, re.IGNORECASE):
            return True
    return False


def enrich_slide_continuation_message(
    message_text: str,
    persistence: Any,
    session_key: str,
    *,
    limit: int = 80,
    batch_size: int = 5,
    history_key: str = "",
) -> str:
    """Expand short continue replies so the model calls image tools immediately."""
    text = (message_text or "").strip()
    if not text:
        return text
    if not is_slide_continuation_message(text):
        return text
    hist_key = _conversation_history_key(session_key, history_key)
    blob = _session_text_blob(
        persistence,
        hist_key,
        text,
        limit=limit,
    )
    progress = resolve_slide_batch_progress(session_key, blob)
    if progress is None:
        return text
    completed, total = progress
    if completed >= total:
        return text
    batch = find_running_image_batch_job(session_key)
    if batch is None and not _session_has_slide_batch_context(blob):
        return text
    if batch is not None and batch_complete_on_disk(batch):
        return text
    next_slide = completed + 1
    batch_end = min(total, next_slide + max(1, int(batch_size)) - 1)
    return (
        f"{text}\n\n"
        f"{_SLIDE_AUTO_CONTINUE_MARKER} — executar já, sem pedir confirmação]\n"
        f"- Progresso actual: {completed}/{total} slides prontos.\n"
        f"- Faltam slides {next_slide} a {total}.\n"
        f"- USE `qclaw_slides_batch_images` com `resume=true` no mesmo prompts_file/deck.\n"
        "  Isso gera APENAS os slides que faltam no output_dir (skip dos existentes).\n"
        "- NÃO use `qclaw_grok_imagine_generate` individual — salva na pasta errada.\n"
        "- Não pare para perguntar ao utilizador; chame a ferramenta imediatamente."
    )


def _is_explicit_slide_batch_args(args: dict[str, Any], *, filename: str = "") -> bool:
    """True when tool args denote a multi-slide deck batch (not a single dashboard PNG)."""
    if _coerce_progress_int(
        args.get("slide_index") or args.get("slide_number") or args.get("slide")
    ):
        return True
    if args.get("prompts_file") or args.get("deck_path"):
        return True
    name = (filename or str(args.get("filename") or "")).strip()
    if _infer_slide_number_from_filename(name) is not None:
        return True
    slides_val = args.get("slides")
    if isinstance(slides_val, (list, dict)) and slides_val:
        return True
    return False


def _resolve_slide_progress_context(
    job_id: Optional[str],
    args: dict[str, Any],
    *,
    filename: str = "",
) -> dict[str, Any]:
    """Fill slide_index / slide_total from args, filename, or the chat job message."""
    out = dict(args)
    slide_turn = _coerce_progress_int(
        out.get("slide_index") or out.get("slide_number") or out.get("slide")
    )
    if slide_turn is None and filename:
        slide_turn = _infer_slide_number_from_filename(filename)
        if slide_turn is not None:
            out["slide_index"] = slide_turn

    slide_max = _coerce_progress_int(
        out.get("slide_total") or out.get("total_slides") or out.get("max_slides")
    )
    # Agents often pass slide_total=N for N kanban cards in one PNG — not a slide deck.
    if slide_max is not None and not _is_explicit_slide_batch_args(args, filename=filename):
        slide_max = None
        for key in ("slide_total", "total_slides", "max_slides"):
            out.pop(key, None)
    if slide_max is None and job_id:
        job = get_job(job_id)
        if job is not None:
            if job.block_max > 0:
                slide_max = job.block_max
            elif job.message_text:
                slide_max = _infer_total_slides_from_text(job.message_text)
            if slide_max is None:
                for line in reversed(job.progress or []):
                    slide_max = _infer_total_slides_from_text(line)
                    if slide_max is not None:
                        break
            if slide_max is not None:
                out["slide_total"] = slide_max
    return out


def apply_batch_progress_from_tool_args(
    job_id: Optional[str],
    args: dict[str, Any],
    *,
    detail: str = "",
    filename: str = "",
    in_progress: bool = False,
) -> None:
    """Update block slider from slide/block counters passed to image tools."""
    if not job_id:
        return
    if in_progress:
        set_block_progress_detail(job_id, detail or "A gerar slide")
        return
    ctx = _resolve_slide_progress_context(job_id, args, filename=filename)
    block_turn = _coerce_progress_int(
        ctx.get("block_index")
        or ctx.get("block_current")
        or ctx.get("block_turn")
    )
    block_max = _coerce_progress_int(
        ctx.get("block_total")
        or ctx.get("block_max")
        or ctx.get("block_max_turns")
    )
    slide_turn = _coerce_progress_int(
        ctx.get("slide_index")
        or ctx.get("slide_number")
        or ctx.get("slide")
    )
    slide_max = _coerce_progress_int(
        ctx.get("slide_total")
        or ctx.get("total_slides")
        or ctx.get("max_slides")
    )
    msg = (detail or "").strip()
    if slide_max is not None and slide_max > 0:
        turn = slide_turn if slide_turn is not None else 0
        if turn <= 0:
            job = get_job(job_id)
            if job is not None:
                turn = job.block_turn
        if not msg and slide_turn is not None:
            msg = f"Slide {slide_turn}/{slide_max}"
        set_block_progress(
            job_id,
            max(0, int(turn or 0)),
            slide_max,
            message=msg or None,
        )
    elif block_turn is not None and block_max is not None and block_max > 0:
        if not msg and slide_turn is not None and slide_max is not None:
            msg = f"Slide {slide_turn}/{slide_max}"
        set_block_progress(job_id, block_turn, block_max, message=msg or None)
    else:
        job = get_job(job_id)
        if job is not None and job.block_max > 0:
            turn = slide_turn
            if turn is None and filename:
                turn = _infer_slide_number_from_filename(filename)
            if turn is None:
                turn = job.block_turn
            set_block_progress(
                job_id,
                max(0, int(turn or 0)),
                job.block_max,
                message=msg or f"Slide {turn or job.block_turn}/{job.block_max}",
            )


def prime_slide_job_from_message(job_id: str, message_text: str) -> None:
    """Pre-set block slider when the user message mentions a slide count."""
    visible = public_message_text(message_text or "")
    pair = _infer_slide_pair_from_text(visible)
    total = pair[1] if pair is not None else _infer_total_slides_from_text(visible)
    if total is not None:
        turn = pair[0] if pair is not None else 0
        set_block_progress(
            job_id,
            turn,
            total,
            message=f"Slide {turn}/{total}  a iniciar",
        )


def prime_slide_job_from_session(
    persistence: Any,
    job_id: str,
    session_key: str,
    *,
    limit: int = 80,
    history_key: str = "",
) -> None:
    """Infer slide total (and current turn) from recent session history."""
    job = get_job(job_id)
    if job is None or job.block_max > 0:
        return
    hist_key = _conversation_history_key(session_key, history_key)
    blob = _session_text_blob(
        persistence,
        hist_key,
        job.message_text or "",
        limit=limit,
    )
    progress = resolve_slide_batch_progress(session_key, blob)
    if progress is None:
        return
    if find_running_image_batch_job(session_key) is None and not _session_has_slide_batch_context(
        blob
    ):
        return
    turn, total = progress
    set_block_progress(
        job_id,
        turn,
        total,
        message=f"Slide {turn}/{total}  a iniciar",
    )


def parse_block_progress(message: str) -> Optional[tuple[int, int]]:
    """Return (turn, max) when *message* reports block batch progress."""
    text = (message or "").strip()
    if not text:
        return None
    for line in text.splitlines():
        m = _BLOCK_PROGRESS_RE.search(line.strip())
        if not m:
            continue
        groups = [g for g in m.groups() if g is not None]
        if len(groups) >= 2:
            if len(groups) >= 3:
                start = max(0, int(groups[-3]))
                end = max(0, int(groups[-2]))
                total = max(0, int(groups[-1]))
                if total > 0:
                    line_lower = line.strip().lower()
                    pending = bool(
                        re.search(
                            r"n[a]o\s+gerad|faltando|pr[o]ximo|pendente|aguard|a\s+iniciar",
                            line_lower,
                        )
                    )
                    in_progress = pending or bool(
                        re.search(
                            r"em\s+gera|aguardando|iniciando|gerando",
                            line_lower,
                        )
                    )
                    if in_progress:
                        turn = max(0, min(start, end) - 1)
                    else:
                        turn = max(start, end)
                    if _slide_numeric_line_ok(line.strip(), turn, total):
                        return turn, total
                    continue
            turn = max(0, int(groups[-2]))
            total = max(0, int(groups[-1]))
            if total > 0 and _slide_numeric_line_ok(line.strip(), turn, total):
                return turn, total
    return None


def format_block_progress_message(turn: int, max_turns: int, detail: str = "") -> str:
    head = f"Bloco {max(0, int(turn))}/{max(1, int(max_turns))}"
    tail = (detail or "").strip()
    return f"{head}  {tail}" if tail else head


def set_block_progress_detail(job_id: str, message: str) -> None:
    """Update progress text without changing the slide counter."""
    text = (message or "").strip()
    if not text:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.last_progress = text
        job.progress.append(text)
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def set_block_progress(
    job_id: str,
    turn: int,
    max_turns: int,
    *,
    message: Optional[str] = None,
) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.block_turn = max(0, int(turn))
        job.block_max = max(0, int(max_turns))
        text = (message or "").strip()
        if not text and job.block_max > 0:
            text = format_block_progress_message(job.block_turn, job.block_max)
        if text:
            job.last_progress = text
            job.progress.append(text)
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def append_progress(job_id: str, message: str) -> None:
    text = (message or "").strip()
    if not text:
        return
    block = parse_block_progress(text)
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.progress.append(text)
        job.last_progress = text
        if block is not None:
            new_turn, new_max = block
            if new_max > 0:
                job.block_max = max(job.block_max, new_max)
            if new_turn > job.block_turn:
                job.block_turn = new_turn
        elif job.block_max <= 0 and not re.search(
            r"Ferramentas:\s*passo\s+\d+\s*/\s*\d+",
            text,
            re.IGNORECASE,
        ):
            inferred_total = _infer_total_slides_from_text(text)
            if inferred_total is not None:
                job.block_max = inferred_total
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def record_model_attempt(
    job_id: str,
    *,
    model_id: str,
    model_label: str = "",
    provider: str = "",
    ok: bool,
    error: str = "",
    kind: str = "llm",
) -> None:
    """Append one model/provider attempt (LLM or image) to the running chat job."""
    mid = str(model_id or "").strip()
    if not mid or mid == "none":
        return
    entry: dict[str, Any] = {
        "model_id": mid,
        "model_label": str(model_label or mid).strip(),
        "provider": str(provider or "").strip(),
        "ok": bool(ok),
        "error": str(error or "")[:300],
        "kind": str(kind or "llm").strip() or "llm",
        "at": time.time(),
    }
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.model_attempts.append(entry)
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def record_model_attempts_batch(job_id: str, attempts: list[dict[str, Any]]) -> None:
    """Append image/LLM fallback attempt rows produced by tool pipelines."""
    rows: list[dict[str, Any]] = []
    for raw in attempts:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("model") or raw.get("model_id") or "").strip()
        if not mid:
            continue
        rows.append(
            {
                "model_id": mid,
                "model_label": str(raw.get("model_label") or mid).strip(),
                "provider": str(raw.get("provider") or "").strip(),
                "ok": bool(raw.get("ok")),
                "error": str(raw.get("error") or "")[:300],
                "kind": str(raw.get("kind") or "image").strip() or "image",
                "at": time.time(),
            }
        )
    if not rows:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.model_attempts.extend(rows)
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def materialized_image_count(job: ChatSendJob) -> int:
    """Count batch images whose files exist on disk (not just progress counters)."""
    sources: list[Any] = []
    if isinstance(job.partial_media, dict):
        sources.extend(job.partial_media.get("images") or [])
    if isinstance(job.result, dict):
        sources.extend(job.result.get("images") or [])
    if not sources:
        return 0
    seen: set[str] = set()
    count = 0
    for item in sources:
        if not isinstance(item, dict):
            continue
        path = str(item.get("image_path") or item.get("path") or "").strip()
        if not path:
            continue
        try:
            resolved = str(Path(path).expanduser().resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        if Path(resolved).is_file():
            seen.add(resolved)
            count += 1
    return count


def _max_slide_from_media(images: list[Any]) -> int:
    max_n = 0
    for img in images:
        if not isinstance(img, dict):
            continue
        caption = str(img.get("caption") or img.get("title") or "")
        m = re.search(r"Slide\s+(\d+)", caption, re.IGNORECASE)
        if not m:
            continue
        try:
            max_n = max(max_n, int(m.group(1)))
        except (TypeError, ValueError):
            continue
    return max_n


def dedupe_job_images_by_slide(images: list[Any]) -> list[dict[str, Any]]:
    """Keep one preview per slide number — latest path wins (batch streaming)."""
    ordered: list[dict[str, Any]] = []
    by_slide: dict[int, int] = {}
    by_path: dict[str, int] = {}
    for item in images:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        path = str(entry.get("image_path") or entry.get("path") or "").strip()
        slide_no = _coerce_progress_int(entry.get("slide"))
        if slide_no is None and path:
            slide_no = _infer_slide_number_from_filename(Path(path).name)
        if slide_no is not None:
            idx = by_slide.get(slide_no)
            if idx is None:
                by_slide[slide_no] = len(ordered)
                ordered.append(entry)
            else:
                ordered[idx] = entry
            continue
        if path:
            idx = by_path.get(path)
            if idx is None:
                by_path[path] = len(ordered)
                ordered.append(entry)
            else:
                ordered[idx] = entry
            continue
        ordered.append(entry)
    return ordered


def _reconcile_block_turn_from_media(job: ChatSendJob) -> None:
    """Clamp block_turn when job counter drifts above images already materialized."""
    if job.block_max <= 0 or not job.partial_media:
        return
    images = job.partial_media.get("images") or []
    if not isinstance(images, list) or not images:
        return
    from_slides = _max_slide_from_media(images)
    media_done = from_slides if from_slides > 0 else len(images)
    if media_done <= 0:
        return
    if job.block_turn > media_done + 1:
        job.block_turn = media_done


def update_job_media(job_id: str, media_extra: dict[str, Any]) -> None:
    """Expose generated images/videos while the chat job is still running."""
    if not media_extra:
        return
    images = media_extra.get("images") or []
    videos = media_extra.get("videos") or []
    preview_fallback = media_extra.get("preview_fallback")
    creative_widget = media_extra.get("creativeWidget")
    if not images and not videos and not preview_fallback and not creative_widget:
        return
    image_list = list(images) if isinstance(images, list) else []
    payload: dict[str, Any] = {
        "images": image_list,
        "videos": list(videos),
    }
    if isinstance(creative_widget, dict):
        payload["creativeWidget"] = creative_widget
    if preview_fallback:
        payload["preview_fallback"] = True
        for key in ("image_path", "video_path", "caption", "message"):
            if media_extra.get(key):
                payload[key] = media_extra[key]
    from .openclaw_chat_media_tools import cap_streaming_job_media, sanitize_media_payload_for_persistence

    payload = sanitize_media_payload_for_persistence(payload) or payload
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        if (job.kind or "").strip() == "image_batch" and isinstance(payload.get("images"), list):
            payload = dict(payload)
            payload["images"] = dedupe_job_images_by_slide(payload["images"])
        if (job.kind or "").strip() in ("image_batch", "pdf_preview"):
            payload = cap_streaming_job_media(payload, kind=job.kind or "")
        job.partial_media = payload
        _reconcile_block_turn_from_media(job)
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def set_tool_progress(
    job_id: str,
    turn: int,
    max_turns: int,
    *,
    message: Optional[str] = None,
) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.tool_turn = max(0, int(turn))
        job.tool_max = max(0, int(max_turns))
        text = (message or "").strip()
        if text:
            job.last_progress = text
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)


def update_partial_reply(job_id: str, text: str) -> None:
    """Update partial streaming reply in memory only (no MongoDB persist)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.partial_reply = text


def append_partial_reasoning(job_id: str, text: str, *, max_chars: int = 6000) -> None:
    """Accumulate orchestration/model reasoning for the live user bubble."""
    chunk = (text or "").strip()
    if not chunk or not job_id:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        prev = (job.partial_reasoning or "").strip()
        if prev and (chunk in prev or prev.endswith(chunk)):
            return
        merged = f"{prev}\n\n{chunk}".strip() if prev else chunk
        if len(merged) > max_chars:
            merged = merged[-max_chars:]
        job.partial_reasoning = merged


def _log_job_failure(
    job: ChatSendJob,
    error: str,
    result: Optional[dict[str, Any]],
) -> None:
    """Mirror every job failure into the persistent DB error log (best-effort)."""
    try:
        from .chat_error_log import log_error

        detail: Optional[str] = None
        extra: dict[str, Any] = {}
        if isinstance(result, dict):
            detail = (
                str(result.get("stderr") or "").strip()
                or str(result.get("stdout") or "").strip()
                or None
            )
            for key in ("missing_credentials", "failed", "failed_count", "provider"):
                if key in result and result.get(key):
                    extra[key] = result[key]
        log_error(
            source="chat_job",
            kind=job.kind or "chat",
            error=error,
            session_key=job.session_key,
            job_id=job.id,
            detail=detail,
            extra=extra or None,
            scope=_current_scope(),
        )
    except Exception:  # noqa: BLE001 — logging the failure must not raise
        log.warning("chat_error_log mirror skipped for job %s", job.id, exc_info=True)


def _image_batch_preview_media(
    job: ChatSendJob,
    safe_result: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Path-only image previews for the dashboard after batch completion."""
    from .openclaw_chat_media_tools import cap_streaming_job_media

    images: list[Any] = []
    if isinstance(safe_result.get("images"), list):
        images = [dict(item) for item in safe_result["images"] if isinstance(item, dict)]
    if not images and isinstance(job.partial_media, dict):
        raw = job.partial_media.get("images") or []
        if isinstance(raw, list):
            images = [dict(item) for item in raw if isinstance(item, dict)]
    if not images:
        return None
    return cap_streaming_job_media({"images": images, "videos": []}, kind="image_batch")


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    from .openclaw_chat_media_tools import sanitize_job_result_for_persistence

    safe_result = sanitize_job_result_for_persistence(result) or result
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            log.warning("complete_job: job %s not found (pruned?), result discarded", job_id)
            return
        job.status = "done"
        job.result = safe_result
        kind = (job.kind or "").strip()
        if kind in ("image_batch", "pdf_preview"):
            if kind == "image_batch":
                preview = _image_batch_preview_media(job, safe_result)
                if preview is not None:
                    job.partial_media = preview
                    count = len(preview.get("images") or [])
                    job.last_progress = f"✓ {count} imagem(ns) pronta(s)"
                else:
                    job.partial_media = None
            else:
                job.partial_media = None
            if job.block_max > 0:
                if kind == "image_batch":
                    completed = int(safe_result.get("completed_slides") or 0)
                    materialized = materialized_image_count(job)
                    if materialized > 0:
                        job.block_turn = materialized
                    elif completed > 0:
                        job.block_turn = min(completed, job.block_max)
                    # Never inflate to block_max unless every image exists on disk.
                else:
                    shown = int(safe_result.get("shown_pages") or 0)
                    job.block_turn = max(job.block_turn, shown or job.block_max)
        job.finished_at = time.time()
        snapshot = ChatSendJob(**job.__dict__)
    if (snapshot.kind or "").strip() == "image_batch":
        generated = int((safe_result or {}).get("generated_count") or 0)
        result_images = (safe_result or {}).get("images") or []
        had_success = (
            generated > 0
            or snapshot.block_turn > 0
            or (isinstance(result_images, list) and len(result_images) > 0)
        )
        record_image_batch_outcome(_image_batch_session_key(snapshot), had_success=had_success)
    _persist_job(snapshot)


def fail_job(job_id: str, error: str, *, result: Optional[dict[str, Any]] = None) -> None:
    from .openclaw_chat_media_tools import sanitize_job_result_for_persistence

    safe_result = (
        sanitize_job_result_for_persistence(result) if isinstance(result, dict) else result
    )
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            log.warning("fail_job: job %s not found (pruned?), error discarded: %s", job_id, error)
            return
        job.status = "error"
        job.error = error
        job.result = safe_result
        if (job.kind or "").strip() == "image_batch":
            job.partial_media = None
        job.finished_at = time.time()
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)
    _log_job_failure(snapshot, error, safe_result if isinstance(safe_result, dict) else result)


def cancel_job(job_id: str) -> bool:
    """Mark a running job as cancelled. Returns True if the job was running."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        if job.status != "running":
            return False
        job.status = "cancelled"
        job.error = "Execuo cancelada pelo usurio"
        job.finished_at = time.time()
        snapshot = ChatSendJob(**job.__dict__)
    _persist_job(snapshot)
    return True


def cancel_jobs_for_session(
    session_key: str,
    *,
    also_keys: Optional[frozenset[str]] = None,
) -> list[str]:
    """Cancel all running chat jobs bound to a session key (delete/purge safety)."""
    key = (session_key or "").strip()
    if not key and not also_keys:
        return []
    match_keys: set[str] = {key} if key else set()
    if also_keys:
        match_keys.update(k.strip() for k in also_keys if (k or "").strip())
    cancelled: list[str] = []
    seen: set[str] = set()
    for job in list_running_jobs():
        if job.status != "running":
            continue
        bound = (job.session_key or "").strip()
        display = (job.display_key or "").strip()
        if bound not in match_keys and display not in match_keys:
            continue
        if job.id in seen:
            continue
        if cancel_job(job.id):
            cancelled.append(job.id)
            seen.add(job.id)
    return cancelled


def job_history_session_key(job_id: str) -> str:
    """UI/history key where a job's progress messages should appear."""
    job = get_job(job_id)
    if job is None:
        return ""
    return (job.display_key or job.session_key).strip()


def _session_still_in_history(persistence: Any, session_key: str) -> bool:
    """True when persistence is missing or the dashboard session still exists."""
    key = (session_key or "").strip()
    if not key or persistence is None:
        return True
    hist = getattr(persistence, "history", None)
    if hist is None or not hasattr(hist, "get_session_by_key"):
        return True
    try:
        return hist.get_session_by_key(key) is not None
    except Exception:
        return True


def job_client_session_key(job: ChatSendJob) -> str:
    """UI conversation key bound to a job (display_key preferred)."""
    return (job.display_key or job.session_key or "").strip()


def recover_jobs_for_missing_sessions(persistence: Any) -> list[str]:
    """Cancel running jobs whose UI conversation was deleted from the sidebar."""
    if persistence is None:
        return []
    cancelled: list[str] = []
    seen: set[str] = set()
    for job in list_running_jobs():
        key = job_client_session_key(job)
        if not key or _session_still_in_history(persistence, key):
            continue
        if job.id in seen:
            continue
        if cancel_job_if_session_missing(
            job.id,
            persistence,
            key,
            reason="Conversa removida — geração interrompida",
        ):
            cancelled.append(job.id)
            seen.add(job.id)
            if (job.kind or "").strip() == "image_batch":
                try:
                    from .chat_image_background import clear_session_image_job

                    clear_session_image_job(key)
                except Exception:
                    pass
    return cancelled


def cancel_job_if_session_missing(
    job_id: str,
    persistence: Any,
    session_key: str,
    *,
    reason: str = "Conversa removida — job cancelado",
) -> bool:
    """Cancel a running job when its target chat session no longer exists."""
    key = (session_key or job_history_session_key(job_id) or "").strip()
    if not key or persistence is None:
        return False
    hist = getattr(persistence, "history", None)
    if hist is None or not hasattr(hist, "get_session_by_key"):
        return False
    try:
        if hist.get_session_by_key(key) is not None:
            return False
    except Exception:
        return False
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return False
    cancel_job(job_id)
    log.info(
        "cancelled job %s — session %s no longer exists (%s)",
        job_id,
        key,
        reason,
    )
    return True


class BackgroundJobAborted(Exception):
    """Cooperative stop for long-running background workers."""

    def __init__(self, job_id: str = "") -> None:
        self.job_id = (job_id or "").strip()
        label = self.job_id or "job"
        super().__init__(f"Background job aborted: {label}")


def is_job_cancelled(job_id: str) -> bool:
    """Check if a job has been cancelled (used by worker threads to bail out)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        return job.status == "cancelled"


def should_abort_background_job(job_id: str) -> bool:
    """True when a background worker should stop (cancelled, failed, or done)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        return job.status != "running"


def should_defer_image_batch_chat_preview(job: ChatSendJob) -> bool:
    """Hide image-batch previews in chat until every slide finishes."""
    return (job.kind or "").strip() == "image_batch" and job.status == "running"


def _hydrated_job_media_payload(
    job: ChatSendJob,
    hydrate: Any,
) -> Optional[dict[str, Any]]:
    media_payload: Optional[dict[str, Any]] = None
    if job.partial_media:
        media_payload = hydrate(job.partial_media)
    elif job.status == "done" and isinstance(job.result, dict):
        result_images = job.result.get("images") or []
        if result_images:
            media_payload = hydrate({"images": list(result_images), "videos": []})
    return media_payload


def get_job(job_id: str) -> Optional[ChatSendJob]:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            return ChatSendJob(**job.__dict__)
    loaded = _load_job_from_db(job_id)
    if loaded is not None:
        with _lock:
            _jobs[job_id] = loaded
        return ChatSendJob(**loaded.__dict__)
    return None


def job_to_dict(job: ChatSendJob, *, light: bool = False) -> dict[str, Any]:
    client_key = (job.display_key or job.session_key).strip()
    preview = (public_message_text(job.message_text) or job.last_progress or "").strip()
    if len(preview) > 200:
        preview = preview[:200]
    out: dict[str, Any] = {
        "ok": True,
        "jobId": job.id,
        "sessionKey": client_key,
        "sendSessionKey": job.session_key,
        "kind": job.kind or "chat",
        "status": job.status,
        "lastProgress": job.last_progress,
        "startedAt": job.started_at,
        "preview": preview,
    }
    if not light:
        out["progress"] = list(job.progress)
    if job.agent_id:
        out["agentId"] = job.agent_id
    if job.model_id:
        out["modelId"] = job.model_id
    if job.finished_at is not None:
        out["finishedAt"] = job.finished_at
    if job.tool_max > 0:
        out["toolTurn"] = job.tool_turn
        out["toolMax"] = job.tool_max
    if job.block_max > 0:
        out["blockTurn"] = job.block_turn
        out["blockMax"] = job.block_max
        pct = min(100, round(100.0 * max(0, job.block_turn) / max(1, job.block_max)))
        out["progressPercent"] = pct
        if getattr(job, "progress_detail", None):
            out["progressDetail"] = job.progress_detail
    out["elapsedSeconds"] = max(0, int(time.time() - job.started_at))
    from .openclaw_chat_media_tools import (
        hydrate_media_payload_from_paths,
        hydrate_media_payload_light,
    )

    hydrate = hydrate_media_payload_light if light else hydrate_media_payload_from_paths

    if should_defer_image_batch_chat_preview(job):
        out["deferChatPreview"] = True
    media_payload = _hydrated_job_media_payload(job, hydrate)
    if media_payload:
        out["media"] = media_payload
    if isinstance(job.result, dict):
        widget = job.result.get("creativeWidget")
        if isinstance(widget, dict):
            out["creativeWidget"] = widget
            if isinstance(out.get("media"), dict):
                out["media"]["creativeWidget"] = widget
    if job.partial_reply:
        out["partialReply"] = job.partial_reply
    if job.partial_reasoning:
        out["partialReasoning"] = job.partial_reasoning
    if job.model_attempts:
        out["modelAttempts"] = list(job.model_attempts)
    if job.error:
        out["error"] = job.error
    if job.result is not None:
        result_out = dict(job.result)
        result_media = result_out.get("media")
        if isinstance(result_media, dict):
            hydrated_result_media = hydrate(result_media)
            if isinstance(hydrated_result_media, dict):
                result_out["media"] = hydrated_result_media
        out["result"] = result_out
    return out


def _batch_output_dir_from_job(job: ChatSendJob) -> Optional[Path]:
    for source in (job.partial_media, job.result):
        if not isinstance(source, dict):
            continue
        raw = str(source.get("output_dir") or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_dir():
            return path.resolve()
    return None


def batch_complete_on_disk(job: ChatSendJob) -> bool:
    """True when the batch output_dir already contains every expected slide PNG."""
    total = int(job.block_max or 0)
    if total <= 0:
        return False
    root = _batch_output_dir_from_job(job)
    if root is None:
        return False
    from .slides_batch_artifacts import _missing_slides, list_slide_pngs

    slides = list_slide_pngs(root)
    if not slides:
        return False
    existing = [slide_no for slide_no, _ in slides]
    return not _missing_slides(existing, total)


def slide_job_needs_auto_continue(job: ChatSendJob, *, min_total: int = 2) -> bool:
    """True when a slide batch job finished before all slides were generated."""
    client_key = (job.display_key or job.session_key).strip()
    if has_running_chat_job_for_session(client_key):
        return False
    batch = find_running_image_batch_job(client_key)
    if batch is not None and batch.block_max >= min_total:
        if batch_complete_on_disk(batch):
            return False
        return batch.block_turn < batch.block_max
    if job.block_max < min_total:
        return False
    if batch_complete_on_disk(job):
        return False
    return job.block_turn < job.block_max


def list_running_jobs() -> list[ChatSendJob]:
    with _lock:
        running = [
            ChatSendJob(**j.__dict__)
            for j in _jobs.values()
            if j.status == "running"
        ]
    if running or _persist_path is None:
        return running
    for job in _load_running_from_db(_persist_path):
        with _lock:
            if job.id not in _jobs:
                _jobs[job.id] = job
    with _lock:
        return [
            ChatSendJob(**j.__dict__)
            for j in _jobs.values()
            if j.status == "running"
        ]


def list_running_chat_jobs() -> list[ChatSendJob]:
    """Running chat send jobs (excludes background image batches)."""
    return [j for j in list_running_jobs() if (j.kind or "chat") == "chat"]


def list_all_recent_jobs(*, limit: int = 50) -> list[ChatSendJob]:
    """All in-memory jobs (running + done + error) sorted by recency.

    Used by the chat queue panel to show which messages were
    delivered and which weren't.
    """
    with _lock:
        snapshot = [ChatSendJob(**j.__dict__) for j in _jobs.values()]
    snapshot.sort(key=lambda j: -(j.started_at or 0.0))
    return snapshot[:limit]


def recover_stale_chat_jobs(
    *,
    max_age_seconds: float = _STALE_CHAT_JOB_SECONDS,
    kinds: Optional[frozenset[str]] = None,
) -> list[ChatSendJob]:
    """Fail chat send jobs stuck in ``running`` so sessions unlock."""
    want = kinds if kinds is not None else frozenset({"chat"})
    now = time.time()
    limit = max(60.0, float(max_age_seconds))
    stale: list[ChatSendJob] = []
    for job in list_running_jobs():
        kind = (job.kind or "chat").strip() or "chat"
        if kind not in want:
            continue
        age = now - float(job.started_at or now)
        if age < limit:
            continue
        err = (
            f"Job expirado apos {int(age)}s  - conversa libertada. "
            "Pode enviar outra mensagem ou repetir o pedido."
        )
        fail_job(job.id, err, result={"ok": False, "error": err, "stale": True})
        stale.append(job)
        log.warning(
            "recovered stale chat job id=%s session=%s kind=%s age=%ds",
            job.id,
            job.session_key,
            kind,
            int(age),
        )
    return stale


def _image_batch_job_looks_in_progress(job: ChatSendJob) -> bool:
    """True when the queue shows a single-image job still generating."""
    text = (job.last_progress or "").strip().lower()
    if not text:
        return False
    return text.startswith("a gerar") or "em background" in text


def recover_stale_batch_jobs(
    *,
    max_age_seconds: float = _STALE_BATCH_JOB_SECONDS,
    partial_max_age_seconds: float = _STALE_SINGLE_IMAGE_SECONDS,
) -> list[ChatSendJob]:
    """Fail background batch jobs (PDF preview, slides) stuck in ``running``."""
    now = time.time()
    limit = max(120.0, float(max_age_seconds))
    partial_limit = max(120.0, float(partial_max_age_seconds))
    stale: list[ChatSendJob] = []
    for job in list_running_jobs():
        kind = (job.kind or "chat").strip() or "chat"
        if kind not in _BATCH_JOB_KINDS:
            continue
        age = now - float(job.started_at or now)
        turn = max(0, int(job.block_turn or 0))
        total = max(0, int(job.block_max or 0))
        if (
            kind == "image_batch"
            and total == 0
            and _image_batch_job_looks_in_progress(job)
            and age >= partial_limit
        ):
            err = (
                f"Geração de imagem interrompida após {int(age)}s sem resposta do provider "
                f"({(job.last_progress or 'em curso')[:80]}). "
                "Verifique créditos Gemini/Grok ou reenvie com outro modelo."
            )
            fail_job(job.id, err, result={"ok": False, "error": err, "stale": True})
            stale.append(job)
            log.warning(
                "recovered stale single-image job id=%s session=%s age=%ds",
                job.id,
                job.session_key,
                int(age),
            )
            continue
        if (
            kind == "image_batch"
            and total > 1
            and turn == 0
            and materialized_image_count(job) == 0
            and age >= partial_limit
        ):
            err = (
                f"Geração de imagem interrompida após {int(age)}s "
                f"(0/{total} — progresso não avançou). "
                "Se slide_total era contagem de cards, reenvie sem slide_total."
            )
            fail_job(job.id, err, result={"ok": False, "error": err, "stale": True})
            stale.append(job)
            log.warning(
                "recovered stale zero-progress batch job id=%s session=%s age=%ds total=%s",
                job.id,
                job.session_key,
                int(age),
                total,
            )
            continue
        if (
            kind == "image_batch"
            and total > 0
            and 0 < turn < total
            and age >= partial_limit
        ):
            err = (
                f"Job de imagens interrompido apos {int(age)}s "
                f"({turn}/{total} slides). "
                "Repita com resume=true ou reenvie o pedido."
            )
            fail_job(job.id, err, result={"ok": False, "error": err, "stale": True})
            stale.append(job)
            log.warning(
                "recovered stale partial batch job id=%s session=%s kind=%s age=%ds",
                job.id,
                job.session_key,
                kind,
                int(age),
            )
            continue
        if age < limit:
            continue
        err = (
            f"Job de preview expirado apos {int(age)}s "
            f"({turn}/{total or '?'} paginas). "
            "Repita com intervalo menor ou dpi mais baixo."
        )
        fail_job(job.id, err, result={"ok": False, "error": err, "stale": True})
        stale.append(job)
        log.warning(
            "recovered stale batch job id=%s session=%s kind=%s age=%ds",
            job.id,
            job.session_key,
            kind,
            int(age),
        )
    return stale


def cancel_unattended_chat_jobs(
    *,
    persistence: Any = None,
    presence_ttl_seconds: float = 90.0,
    grace_seconds: float = 60.0,
    protected_keys: Optional[frozenset[str]] = None,
) -> list[ChatSendJob]:
    """Cancel async chat jobs when no dashboard tab is watching that conversation."""
    from .chat_send_queue import cancel_queued_jobs_for_keys
    from .chat_session_presence import is_presence_exempt_session, is_session_watched
    from .chat_tool_background import chat_session_exists

    now = time.time()
    grace = max(15.0, float(grace_seconds))
    ttl = max(15.0, float(presence_ttl_seconds))
    cancelled: list[ChatSendJob] = []
    unwatched_queue_keys: set[str] = set()

    for job in list_running_jobs():
        kind = (job.kind or "chat").strip() or "chat"
        if kind in _PRESENCE_EXEMPT_JOB_KINDS:
            continue
        ui_key = (job.display_key or job.session_key or "").strip()
        send_key = (job.session_key or "").strip()
        if not ui_key:
            continue
        if is_presence_exempt_session(ui_key, protected_keys=protected_keys):
            continue
        if is_presence_exempt_session(send_key, protected_keys=protected_keys):
            continue

        age = now - float(job.started_at or now)
        if age < grace:
            continue

        if persistence is not None and not chat_session_exists(persistence, ui_key):
            err = "Conversa removida — processo cancelado."
            fail_job(job.id, err, result={"ok": False, "error": err, "orphan": True})
            cancelled.append(job)
            unwatched_queue_keys.add(ui_key)
            log.info(
                "cancelled orphan chat job id=%s session=%s (conversation deleted)",
                job.id,
                ui_key,
            )
            continue

        if is_session_watched(ui_key, ttl_seconds=ttl):
            continue

        err = (
            "Conversa sem painel ativo — processo removido da fila. "
            "Abra a conversa e reenvie se precisar."
        )
        fail_job(job.id, err, result={"ok": False, "error": err, "unattended": True})
        cancelled.append(job)
        unwatched_queue_keys.add(ui_key)
        log.info(
            "cancelled unattended chat job id=%s session=%s kind=%s age=%ds",
            job.id,
            ui_key,
            job.kind or "chat",
            int(age),
        )

    if unwatched_queue_keys:
        for jid in cancel_queued_jobs_for_keys(unwatched_queue_keys):
            with _lock:
                live = _jobs.get(jid)
                if live is None or live.status != "running":
                    continue
                snapshot = ChatSendJob(**live.__dict__)
            if snapshot.id in {j.id for j in cancelled}:
                continue
            queued_kind = (snapshot.kind or "chat").strip() or "chat"
            if queued_kind in _PRESENCE_EXEMPT_JOB_KINDS:
                continue
            err = (
                "Conversa sem painel ativo — pedido removido da fila. "
                "Abra a conversa e reenvie se precisar."
            )
            fail_job(jid, err, result={"ok": False, "error": err, "unattended": True})
            cancelled.append(snapshot)
            log.info("dropped queued chat job id=%s (unwatched session)", jid)

    return cancelled


def parse_job_attachments(job: ChatSendJob) -> Optional[list]:
    raw = job.attachments_json
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None

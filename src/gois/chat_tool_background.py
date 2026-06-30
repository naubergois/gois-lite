"""Background threads for long-running chat tools (PDF preview, swarm runs, …)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_BATCH_JOB_KINDS = frozenset({"image_batch", "pdf_preview", "swarm_run", "book_pipeline"})

_DEFAULT_CHAT_MIRROR_INTERVAL = 12.0
_MIN_ELAPSED_STATUS_SECONDS = 20


def chat_session_exists(persistence: Any, session_key: str) -> bool:
    """True when the dashboard history still has this session."""
    key = (session_key or "").strip()
    if persistence is None or not key:
        return False
    hist = getattr(persistence, "history", None)
    if hist is None or not hasattr(hist, "get_session_by_key"):
        return True
    try:
        return hist.get_session_by_key(key) is not None
    except Exception:
        return True


def mirror_chat_status_throttled(
    persistence: Any,
    session_key: str,
    text: str,
    *,
    last_posted: Optional[dict[str, Any]] = None,
    min_interval: float = 6.0,
    force: bool = False,
    job_id: Optional[str] = None,
) -> bool:
    """Append a chat status line when the message changes (throttled)."""
    key = (session_key or "").strip()
    msg = (text or "").strip()
    if persistence is None or not key or not msg:
        return False
    if not chat_session_exists(persistence, key):
        if job_id:
            from .chat_jobs import cancel_job_if_session_missing

            cancel_job_if_session_missing(job_id, persistence, key)
        return False
    state = last_posted if last_posted is not None else {}
    if not force and msg == str(state.get("text") or ""):
        return False
    now = time.time()
    last_at = float(state.get("at") or 0.0)
    if (
        not force
        and last_at > 0
        and now - last_at < max(1.0, float(min_interval)) * 0.75
    ):
        return False
    try:
        persistence.history.append_message(key, role="status", text=msg)
        state["text"] = msg
        state["at"] = now
        return True
    except Exception as exc:
        log.warning("chat status mirror failed session=%s: %s", key, exc)
        return False


def _has_chat_context(
    *,
    progress_job_id: Optional[str] = None,
    session_key: str = "",
) -> bool:
    return bool((progress_job_id or "").strip() or (session_key or "").strip())


def start_chat_progress_mirror(
    job_id: str,
    *,
    persistence: Any = None,
    session_key: str = "",
    interval_seconds: float = _DEFAULT_CHAT_MIRROR_INTERVAL,
    mirror_to_chat: bool = True,
) -> threading.Event:
    """Watch a background job; optionally mirror progress into chat status lines."""
    stop = threading.Event()
    key = (session_key or "").strip()
    if persistence is None or not key:
        return stop

    last_posted: dict[str, Any] = {"text": "", "at": 0.0}
    started_at = time.time()
    interval = max(1.0, float(interval_seconds))

    def _post(text: str, *, force: bool = False) -> None:
        if not mirror_to_chat:
            return
        if not chat_session_exists(persistence, key):
            from .chat_jobs import cancel_job_if_session_missing

            cancel_job_if_session_missing(job_id, persistence, key)
            stop.set()
            return
        mirror_chat_status_throttled(
            persistence,
            key,
            text,
            last_posted=last_posted,
            min_interval=interval,
            force=force,
            job_id=job_id,
        )

    def _loop() -> None:
        from .chat_jobs import cancel_job_if_session_missing, get_job, is_job_cancelled

        progress_index = 0
        while True:
            if is_job_cancelled(job_id):
                break
            if not chat_session_exists(persistence, key):
                cancel_job_if_session_missing(job_id, persistence, key)
                break
            job = get_job(job_id)
            if job is None or job.status != "running":
                break
            if mirror_to_chat:
                lines = [str(x).strip() for x in (job.progress or []) if str(x).strip()]
                while progress_index < len(lines):
                    line = lines[progress_index]
                    progress_index += 1
                    _post(line, force=(progress_index == 1 and not last_posted.get("text")))
                elapsed = int(time.time() - started_at)
                text = (job.last_progress or "").strip()
                if not lines and not text and elapsed >= _MIN_ELAPSED_STATUS_SECONDS:
                    _post(f"Ainda a processar… ({elapsed}s)")
                elif text and progress_index >= len(lines):
                    suffix = f" ({elapsed}s)" if elapsed >= 60 else ""
                    _post(f"{text}{suffix}")
            if stop.wait(interval):
                break

    threading.Thread(
        target=_loop,
        name=f"chat-mirror-{job_id}",
        daemon=True,
    ).start()
    return stop


def _swarm_card_progress_line(preview: Optional[dict[str, Any]]) -> str:
    if not isinstance(preview, dict):
        return ""
    card = preview.get("next_card")
    if not isinstance(card, dict):
        return ""
    card_id = str(card.get("id") or "").strip()
    title = str(card.get("title") or "").strip()
    if card_id and title:
        return f"Card a resolver: **{card_id}** — {title}"
    if card_id or title:
        return f"Card a resolver: **{card_id or title}**"
    return ""


def prime_swarm_run_job_progress(
    job_id: str,
    *,
    persistence: Any = None,
    session_key: str = "",
    swarm_label: str,
    preview: Optional[dict[str, Any]] = None,
) -> None:
    """Announce the target kanban card and swarm start in job + chat status lines."""
    from .chat_jobs import append_progress

    lines: list[str] = []
    card_line = _swarm_card_progress_line(preview)
    if card_line:
        lines.append(card_line)
    label = str(swarm_label or "").strip() or "swarm"
    lines.append(f"Swarm **{label}** a iniciar…")

    key = str(session_key or "").strip()
    last_posted: dict[str, Any] = {"text": "", "at": 0.0}
    for idx, line in enumerate(lines):
        append_progress(job_id, line)
        if persistence is not None and key:
            mirror_chat_status_throttled(
                persistence,
                key,
                line,
                last_posted=last_posted,
                min_interval=0.0,
                force=True,
            )


def is_background_tool_result(result: Any) -> bool:
    """True when a tool delegated work to a daemon thread and freed the chat."""
    return isinstance(result, dict) and bool(result.get("background"))


def background_tool_reply(result: dict[str, Any]) -> str:
    """User-facing announcement for a background tool spawn."""
    msg = str(result.get("message") or "").strip()
    if msg:
        return msg
    label = str(result.get("kind") or "tarefa").strip() or "tarefa"
    job_id = str(result.get("batchJobId") or result.get("jobId") or "").strip()
    if job_id:
        return (
            f"A executar {label} em background (job `{job_id}`). "
            "O chat continua livre."
        )
    return f"A executar {label} em background. O chat continua livre."


def background_tool_meta(result: dict[str, Any]) -> dict[str, Any]:
    """Extract background job ids for the chat send response."""
    meta: dict[str, Any] = {"background": True}
    for key in ("batchJobId", "jobId", "sessionKey", "kind", "maxSlides"):
        value = result.get(key)
        if value not in (None, ""):
            meta[key] = value
    return meta


def should_background_chat_tool(
    args: dict[str, Any],
    *,
    progress_job_id: Optional[str] = None,
    session_key: str = "",
) -> bool:
    """True when the tool should return immediately and run in a daemon thread."""
    del args  # sync/foreground/background flags are ignored — chat is always async
    return _has_chat_context(progress_job_id=progress_job_id, session_key=session_key)


def is_batch_job_kind(kind: str) -> bool:
    return (kind or "").strip() in _BATCH_JOB_KINDS


def spawn_chat_tool_background(
    *,
    kind: str,
    session_key: str,
    message_text: str,
    label: str,
    run_fn: Callable[[str], dict[str, Any]],
    persistence: Any = None,
    on_start: Optional[Callable[[str], None]] = None,
    format_success: Optional[Callable[[dict[str, Any]], str]] = None,
    format_failure: Optional[Callable[[dict[str, Any]], str]] = None,
) -> dict[str, Any]:
    """Start a long-running tool in a daemon thread; return immediately."""
    from .chat_jobs import (
        append_progress,
        complete_job,
        create_job,
        fail_job,
        is_job_cancelled,
    )

    job = create_job(
        session_key,
        kind=(kind or "chat").strip() or "chat",
        display_key=session_key,
        message_text=message_text,
    )
    append_progress(job.id, label)
    if on_start is not None:
        try:
            on_start(job.id)
        except Exception as exc:
            log.warning("batch job on_start failed job=%s: %s", job.id, exc)

    mirror_stop = start_chat_progress_mirror(
        job.id,
        persistence=persistence,
        session_key=session_key,
    )

    def _worker() -> None:
        try:
            result = run_fn(job.id)
            if is_job_cancelled(job.id):
                return
            if result.get("cancelled"):
                return
            if result.get("ok"):
                complete_job(job.id, result)
                history_key = (session_key or "").strip()
                if persistence is not None and history_key and chat_session_exists(
                    persistence, history_key
                ):
                    text = (
                        format_success(result)
                        if format_success is not None
                        else _default_success_message(kind, result)
                    )
                    extras: dict[str, Any] = {}
                    media: dict[str, Any] = {}
                    images = result.get("images") or []
                    if images:
                        media["images"] = images
                    attachments = result.get("attachments") or []
                    if attachments:
                        extras["attachments"] = attachments
                    widget = result.get("creative_widget") or result.get("creativeWidget")
                    if isinstance(widget, dict):
                        extras["creativeWidget"] = widget
                    if media:
                        extras["media"] = media
                    try:
                        persistence.history.append_message(
                            history_key,
                            role="assistant",
                            text=text,
                            extras=extras or None,
                        )
                    except Exception as exc:
                        log.warning(
                            "could not persist batch completion for %s: %s",
                            history_key,
                            exc,
                        )
            else:
                err = str(result.get("error") or "operation failed")
                fail_job(job.id, err, result=result)
                history_key = (session_key or "").strip()
                if persistence is not None and history_key and chat_session_exists(
                    persistence, history_key
                ):
                    text = (
                        format_failure(result)
                        if format_failure is not None
                        else f"⚠️ **Erro:** {err[:800]}"
                    )
                    try:
                        persistence.history.append_message(
                            history_key,
                            role="assistant",
                            text=text,
                        )
                    except Exception as exc:
                        log.warning(
                            "could not persist batch failure for %s: %s",
                            history_key,
                            exc,
                        )
        except Exception as exc:
            from .chat_jobs import BackgroundJobAborted

            if isinstance(exc, BackgroundJobAborted) or is_job_cancelled(job.id):
                return
            log.exception("background chat tool failed job=%s kind=%s", job.id, kind)
            if not is_job_cancelled(job.id):
                fail_job(job.id, f"{type(exc).__name__}: {exc}")
        finally:
            mirror_stop.set()

    threading.Thread(
        target=_worker,
        name=f"chat-{kind}-{job.id}",
        daemon=True,
    ).start()

    spawn_msg = (
        f"{label} — job `{job.id}` em background. "
        "O chat continua livre; acompanhe a barra de progresso ou "
        "`qclaw_chat_generation_status`."
    )
    if kind == "swarm_run":
        spawn_msg = (
            f"🐝 {label} em background (job `{job.id}`). "
            "O card e o progresso dos agentes aparecem nas mensagens abaixo."
        )

    return {
        "ok": True,
        "background": True,
        "batchJobId": job.id,
        "jobId": job.id,
        "sessionKey": session_key,
        "kind": kind,
        "message": spawn_msg,
    }


def _default_success_message(kind: str, result: dict[str, Any]) -> str:
    if kind == "pdf_preview":
        count = int(result.get("shown_pages") or 0)
        total = result.get("total_pages")
        total_label = total if total is not None else "?"
        return f"PDF renderizado — **{count}** página(s) de **{total_label}**."
    if kind == "swarm_run":
        team = str(result.get("team_name") or result.get("team_id") or "time").strip()
        swarm = str(result.get("swarm_name") or "").strip()
        agents = int(result.get("agents_run") or len(result.get("outputs") or {}))
        card_id = str(result.get("selected_card_id") or "").strip()
        card_part = f", card **{card_id}**" if card_id else ""
        if swarm:
            return (
                f"Swarm **{swarm}** ({team}) concluído — "
                f"**{agents}** agente(s){card_part}."
            )
        return f"Swarm do time **{team}** concluído — **{agents}** agente(s){card_part}."
    if kind == "book_pipeline":
        book_id = str(result.get("book_id") or "").strip()
        if book_id:
            return f"Livro concluído — **book_id:** `{book_id}`."
        return "Pipeline de livro concluído."
    return "Operação em background concluída."

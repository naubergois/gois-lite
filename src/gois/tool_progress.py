"""Track in-flight LLM tool loops (chat + other jobs) for UI and chat status."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolRun:
    job_id: str
    session_key: str
    max_turns: int
    turn: int = 0
    kind: str = "chat"
    label: str = ""
    model_label: str = ""
    started_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_runs: dict[str, ToolRun] = {}


def _short_session_key(session_key: str) -> str:
    key = (session_key or "").strip()
    if key.startswith("hermes:"):
        return key.split(":", 1)[-1] or key
    if ":" in key:
        return key.rsplit(":", 1)[-1][:24] or key
    return key[:24] if key else "?"


def with_model_prefix(message: str, model_label: str) -> str:
    label = (model_label or "").strip()
    text = (message or "").strip()
    if not label or not text:
        return text or label
    return f"{label} · {text}"


def format_tool_step(
    turn: int,
    max_turns: int,
    *,
    hit_limit: bool = False,
    model_label: str = "",
) -> str:
    t = max(0, int(turn))
    m = max(1, int(max_turns))
    if hit_limit:
        body = (
            f"Limite de ferramentas atingido ({t}/{m}) — "
            "a responder com o que já foi recolhido."
        )
    else:
        body = f"Ferramentas: passo {t}/{m}"
    return with_model_prefix(body, model_label)


def format_tool_limit_reply(max_turns: int) -> str:
    m = max(1, int(max_turns))
    return (
        f"(limite de ferramentas atingido — {m}/{m} turnos; "
        "responda com o que já coletou ou peça mais detalhes ao usuário.)"
    )


def _format_other_jobs_line(run: ToolRun) -> str:
    label = (run.label or "").strip() or _short_session_key(run.session_key)
    model = (run.model_label or "").strip()
    who = f"{label} ({model})" if model else label
    return f"· {who}: {run.turn}/{run.max_turns}"


def format_status_with_peers(
    run: ToolRun,
    *,
    hit_limit: bool = False,
    tool_name: Optional[str] = None,
) -> str:
    head = format_tool_step(
        run.turn,
        run.max_turns,
        hit_limit=hit_limit,
        model_label=run.model_label,
    )
    if tool_name and not hit_limit:
        head += f" — {tool_name}"
    with _lock:
        others = [r for jid, r in _runs.items() if jid != run.job_id]
    if not others:
        return head
    peer_lines = "\n".join(_format_other_jobs_line(r) for r in others)
    return head + "\nOutros jobs com ferramentas:\n" + peer_lines


def format_availability_message(
    remaining: list[ToolRun],
    *,
    finished_label: str = "",
) -> str:
    if not remaining:
        who = f" ({finished_label})" if finished_label else ""
        return f"Ferramentas disponíveis{who} — nenhum outro job a usar ferramentas agora."
    lines = [
        "Ferramentas libertadas"
        + (f" ({finished_label})" if finished_label else "")
        + f" — {len(remaining)} job(s) ainda em execução:",
    ]
    lines.extend(_format_other_jobs_line(r) for r in remaining)
    return "\n".join(lines)


def begin_tool_run(
    session_key: str,
    max_turns: int,
    *,
    job_id: Optional[str] = None,
    kind: str = "chat",
    label: str = "",
    model_label: str = "",
) -> ToolRun:
    jid = (job_id or "").strip() or uuid.uuid4().hex[:16]
    run = ToolRun(
        job_id=jid,
        session_key=(session_key or "").strip(),
        max_turns=max(1, int(max_turns)),
        kind=(kind or "chat").strip() or "chat",
        label=(label or "").strip(),
        model_label=(model_label or "").strip(),
    )
    with _lock:
        _runs[jid] = run
    return run


def set_tool_turn(job_id: str, turn: int) -> Optional[ToolRun]:
    with _lock:
        run = _runs.get(job_id)
        if run is None:
            return None
        run.turn = max(0, int(turn))
        return ToolRun(**run.__dict__)


def end_tool_run(job_id: str) -> list[ToolRun]:
    with _lock:
        _runs.pop(job_id, None)
        return [ToolRun(**r.__dict__) for r in _runs.values()]


def get_tool_run(job_id: str) -> Optional[ToolRun]:
    with _lock:
        run = _runs.get(job_id)
        return None if run is None else ToolRun(**run.__dict__)


def list_active_tool_runs() -> list[dict[str, Any]]:
    with _lock:
        rows = sorted(_runs.values(), key=lambda r: r.started_at)
    return [
        {
            "jobId": r.job_id,
            "sessionKey": r.session_key,
            "kind": r.kind,
            "label": r.label or _short_session_key(r.session_key),
            "modelLabel": r.model_label,
            "turn": r.turn,
            "maxTurns": r.max_turns,
            "startedAt": r.started_at,
        }
        for r in rows
    ]


def active_tool_runs_payload() -> dict[str, Any]:
    jobs = list_active_tool_runs()
    return {"ok": True, "jobs": jobs, "activeCount": len(jobs)}

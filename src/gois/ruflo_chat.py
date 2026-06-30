"""RuFlo orchestration chat for the gois dashboard."""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from contextlib import nullcontext

from .config import AgentConfig, OpenclawChatConfig, OpenclawDoctorConfig, RufloChatConfig
from .local_paths import _repo_root
from .cache_paths import cache_subprocess_env
from .ruflo_memory_guard import ensure_memory_db_ready, memory_cli_lock_context
from .ruflo_memory_repair import resolve_ruflo_memory_db_path
from .openclaw_chat import (
    ChatPersistence,
    QclawRuntime,
    delete_openclaw_session_entry,
    publish_chat_status,
    read_messages,
    send_chat_message,
)


def _emit_progress(on_progress: Any, message: str) -> None:
    if not message or on_progress is None:
        return
    try:
        on_progress(message)
    except Exception:  # pragma: no cover - defensive
        log.debug("ruflo on_progress callback failed", exc_info=True)

log = logging.getLogger(__name__)

RUFLO_AGENT_ID = "ruflo"
_PENDING_BASH_APPROVALS: dict[str, dict[str, Any]] = {}


def _ruflo_cli_error_detail(stderr: str, stdout: str, code: int) -> str:
    """Prefer actionable CLI errors over ONNX embedder progress noise."""
    blob = "\n".join(part for part in (stderr, stdout) if part).strip()
    if not blob:
        return f"exit={code}"
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    for ln in lines:
        if "[ERROR]" in ln:
            return ln.replace("[ERROR]", "ERROR:").strip()[:160]
        low = ln.lower()
        if "malformed" in low or "database disk image" in low:
            return ln[:160]
    for ln in reversed(lines):
        low = ln.lower()
        if low.startswith("loading onnx") or "onnx embedder ready" in low:
            continue
        if ln.startswith("Disk cache hit:"):
            continue
        return ln[:160]
    return lines[-1][:160] if lines else f"exit={code}"


_BASH_BLOCK_RE = re.compile(
    r"<(bash|sh|shell)>([\s\S]*?)</\1>|```(?:bash|sh|shell)\s*\n?([\s\S]*?)```",
    re.IGNORECASE,
)


def _extract_bash_script(text: str) -> Optional[str]:
    src = str(text or "")
    match = _BASH_BLOCK_RE.search(src)
    if not match:
        return None
    script = (match.group(2) or match.group(3) or "").strip()
    if not script:
        return None
    # Some responses include a copy label line.
    lines = [ln.rstrip() for ln in script.splitlines()]
    if lines and lines[0].strip().lower() in {"copiar", "copy"}:
        lines = lines[1:]
    cleaned = "\n".join(lines).strip()
    return cleaned or None


def _is_allowed_bash_script(script: str) -> bool:
    allowed_prefixes = (
        "hermes ",
        "ruflo ",
        "echo ",
        "printf ",
        "date",
        "pwd",
        "ls",
    )
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):
            line = line[1:].strip()
        if not line:
            continue
        low = line.lower()
        if any(low == p.strip() or low.startswith(p) for p in allowed_prefixes):
            continue
        return False
    return True


def _run_bash_script(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    script: str,
    *,
    timeout: float = 40.0,
    bypass_allowlist: bool = False,
) -> dict[str, Any]:
    cmd = script.replace("\r\n", "\n").strip()
    if not cmd:
        return {"ok": False, "error": "script vazio"}
    if (not bypass_allowlist) and (not _is_allowed_bash_script(cmd)):
        return {
            "ok": False,
            "error": "script bloqueado por política de segurança (allowlist)",
        }
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=str(_project_dir(cfg, runtime)),
            capture_output=True,
            text=True,
            timeout=max(5.0, timeout),
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout após {int(timeout)}s"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _parse_json_stdout(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _payload_list(payload: Optional[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _short_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _workspace_from_runtime(runtime: QclawRuntime) -> Optional[Path]:
    try:
        data = json.loads(runtime.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    ws = defaults.get("workspace")
    if isinstance(ws, str) and ws.strip():
        return Path(ws).expanduser().resolve()
    lst = agents.get("list")
    if isinstance(lst, list):
        for entry in lst:
            if not isinstance(entry, dict):
                continue
            ws2 = entry.get("workspace")
            if isinstance(ws2, str) and ws2.strip():
                return Path(ws2).expanduser().resolve()
    return None


def _project_dir(cfg: RufloChatConfig, runtime: QclawRuntime) -> Path:
    if cfg.project_dir:
        return Path(cfg.project_dir).expanduser().resolve()
    ws = _workspace_from_runtime(runtime)
    if ws is not None:
        return ws
    return Path.cwd().resolve()


def run_ruflo(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    subargs: list[str],
    *,
    timeout: Optional[float] = None,
    swarm_memory_cfg: Any = None,
) -> tuple[int, str, str]:
    """Run ruflo CLI; return (exit_code, stdout, stderr)."""
    cmd = [cfg.ruflo_bin, *cfg.ruflo_args, *subargs]
    lim = float(timeout if timeout is not None else cfg.command_timeout_seconds)
    db_path = resolve_ruflo_memory_db_path(
        swarm_memory_cfg=swarm_memory_cfg,
        ruflo_chat_cfg=cfg,
        repo_root=_repo_root(),
    )
    lock_enabled = True
    min_free_mb = 512
    if swarm_memory_cfg is not None:
        lock_enabled = bool(getattr(swarm_memory_cfg, "db_lock_enabled", True))
        min_free_mb = int(getattr(swarm_memory_cfg, "db_min_free_mb", 512) or 512)
    try:
        with memory_cli_lock_context(db_path, subargs, lock_enabled=lock_enabled):
            if db_path and subargs and subargs[0] == "memory":
                ready = ensure_memory_db_ready(
                    db_path,
                    min_free_mb=min_free_mb,
                    auto_repair=True,
                )
                if not ready.get("ok"):
                    err = ready.get("error") or "memory_db_not_ready"
                    return 1, "", f"[ERROR] {err}"
            proc = subprocess.run(
                cmd,
                cwd=str(_project_dir(cfg, runtime)),
                capture_output=True,
                text=True,
                timeout=max(5.0, lim),
                env=cache_subprocess_env(),
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {lim:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)


def ruflo_status(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "hosted_ui_url": cfg.hosted_ui_url,
        "local_ui_url": cfg.local_ui_url,
        "project_dir": str(_project_dir(cfg, runtime)),
    }
    _emit_progress(
        on_progress,
        f"RuFlo: a consultar status em {out['project_dir']}…",
    )

    probe_timeout = float(getattr(cfg, "status_probe_timeout_seconds", 8.0) or 8.0)
    probes: dict[str, tuple[list[str], float]] = {
        "swarm": (["swarm", "status", "--format", "json"], probe_timeout),
    }
    if getattr(cfg, "status_probe_hive", True):
        probes["hive"] = (["hive-mind", "status", "--format", "json"], probe_timeout)
    probe_results: dict[str, tuple[int, str, str, int]] = {}

    def _run_probe(name: str, args: list[str], timeout: float) -> tuple[str, tuple[int, str, str, int]]:
        started = time.time()
        try:
            code_x, stdout_x, stderr_x = run_ruflo(cfg, runtime, args, timeout=timeout)
        except Exception as exc:  # pragma: no cover - defensive
            code_x, stdout_x, stderr_x = 127, "", str(exc)
        return name, (
            code_x,
            stdout_x,
            stderr_x,
            int((time.time() - started) * 1000),
        )

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
        futures = [
            pool.submit(_run_probe, name, args, timeout)
            for name, (args, timeout) in probes.items()
        ]
        for fut in as_completed(futures):
            name, result = fut.result()
            probe_results[name] = result

    code, stdout, stderr, elapsed_swarm = probe_results.get("swarm", (127, "", "missing", 0))
    swarm = _parse_json_stdout(stdout)
    out["swarm"] = swarm
    out["swarm_ok"] = code == 0 and swarm is not None
    if code != 0:
        out["swarm_error"] = (stderr or stdout or f"exit {code}").strip()[:500]
    _emit_progress(
        on_progress,
        f"RuFlo: swarm status em {elapsed_swarm} ms (ok={out['swarm_ok']})",
    )

    elapsed_hive = 0
    hive: Optional[dict[str, Any]] = None
    if "hive" in probes:
        code_hive, stdout_hive, stderr_hive, elapsed_hive = probe_results.get(
            "hive", (127, "", "missing", 0)
        )
        hive = _parse_json_stdout(stdout_hive)
        out["hive"] = hive
        out["hive_ok"] = code_hive == 0 and isinstance(hive, dict)
        if code_hive != 0:
            out["hive_error"] = (stderr_hive or stdout_hive or f"exit {code_hive}").strip()[:500]
        _emit_progress(
            on_progress,
            f"RuFlo: hive-mind status em {elapsed_hive} ms (ok={out['hive_ok']})",
        )
    else:
        out["hive"] = None
        out["hive_ok"] = None

    total_ms = int((time.time() - t0) * 1000)
    out["timings_ms"] = {
        "swarm": elapsed_swarm,
        "hive": elapsed_hive if "hive" in probes else None,
        "total": total_ms,
    }
    out["system"] = {
        "mode": "light",
        "derived_from": list(probes.keys()),
    }
    if "hive" in probes:
        out["system_ok"] = bool(out.get("swarm_ok") or out.get("hive_ok"))
    else:
        out["system_ok"] = bool(out.get("swarm_ok"))

    hive_workers_raw = _payload_list(hive, "workers", "agents", "items", "data") if hive else []
    hive_workers: list[dict[str, Any]] = []
    for worker in hive_workers_raw:
        worker_id = str(
            worker.get("id")
            or worker.get("agentId")
            or worker.get("name")
            or ""
        ).strip()
        hive_workers.append(
            {
                "id": worker_id,
                "name": str(worker.get("name") or worker_id or "?"),
                "type": str(worker.get("type") or worker.get("role") or "worker").strip(),
                "status": str(worker.get("status") or worker.get("state") or "unknown").strip(),
                "current_task": str(worker.get("currentTask") or worker.get("task") or "").strip(),
                "tasks_completed": int(worker.get("tasksCompleted") or 0),
            }
        )

    normalized_agents: list[dict[str, Any]] = []
    for worker in hive_workers:
        normalized_agents.append(
            {
                "id": str(worker.get("id") or "").strip(),
                "name": str(worker.get("name") or worker.get("id") or "?").strip(),
                "role": str(worker.get("type") or "worker").strip(),
                "status": str(worker.get("status") or "unknown").strip(),
                "last_result": "",
                "last_task_id": "",
                "last_task_status": "",
                "last_task_result": "",
                "last_task_updated_at": "",
            }
        )

    out["hive_workers"] = hive_workers
    out["hive_workers_total"] = len(hive_workers)
    out["agents"] = normalized_agents
    out["agents_total"] = len(normalized_agents)
    normalized_tasks: list[dict[str, Any]] = []
    out["tasks"] = normalized_tasks
    out["tasks_total"] = len(normalized_tasks)
    _emit_progress(
        on_progress,
        f"RuFlo: agentes {len(normalized_agents)} · hive workers {len(hive_workers)} · "
        f"tarefas {len(normalized_tasks)} "
        f"em {int((time.time()-t0)*1000)} ms",
    )
    return out


def _route_task(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    prompt: str,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    preview = prompt.strip().splitlines()[0][:100] if prompt.strip() else ""
    _emit_progress(
        on_progress,
        f"RuFlo: hooks route a analisar “{preview}”…" if preview else "RuFlo: hooks route…",
    )
    t0 = time.time()
    code, stdout, stderr = run_ruflo(
        cfg,
        runtime,
        ["hooks", "route", "-t", prompt, "--format", "json"],
        timeout=cfg.command_timeout_seconds,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    data = _parse_json_stdout(stdout)
    if data is None:
        _emit_progress(
            on_progress,
            f"RuFlo: hooks route falhou em {elapsed_ms} ms",
        )
        return {
            "ok": False,
            "error": (stderr or stdout or f"exit {code}").strip()[:400],
        }
    _emit_progress(
        on_progress, f"RuFlo: hooks route respondeu em {elapsed_ms} ms"
    )
    return {"ok": True, "routing": data}


def _memory_hits(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    query: str,
    *,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    if not cfg.memory_search_enabled or not query.strip():
        return []
    q_short = query.strip()[:500]
    limit = max(1, min(int(cfg.memory_search_limit), 20))
    # Inform the user precisely what we are searching and where.
    preview = q_short if len(q_short) <= 100 else q_short[:97] + "…"
    _emit_progress(
        on_progress,
        f"RuFlo: a pesquisar memória vetorial (RuFlo MCP) por “{preview}” "
        f"[top {limit}]…",
    )
    t0 = time.time()
    code, stdout, stderr = run_ruflo(
        cfg,
        runtime,
        [
            "memory",
            "search",
            "-q",
            q_short,
            "-l",
            str(limit),
            "--format",
            "json",
        ],
        timeout=cfg.command_timeout_seconds,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    data = _parse_json_stdout(stdout)
    if not isinstance(data, dict):
        detail = _ruflo_cli_error_detail(stderr, stdout, code)
        _emit_progress(
            on_progress,
            f"RuFlo: busca na memória falhou em {elapsed_ms} ms ({detail})",
        )
        log.debug("ruflo memory search failed: %s", stderr or stdout or code)
        return []
    results = data.get("results") or data.get("memories") or data.get("items")
    if isinstance(results, list):
        hits = [r for r in results if isinstance(r, dict)][: cfg.memory_search_limit]
        _emit_progress(
            on_progress,
            f"RuFlo: memória vetorial respondeu em {elapsed_ms} ms "
            f"({len(hits)} resultado(s))",
        )
        return hits
    _emit_progress(
        on_progress,
        f"RuFlo: memória vetorial respondeu em {elapsed_ms} ms (sem resultados)",
    )
    return []


def format_orchestration_block(
    *,
    routing: Optional[dict[str, Any]],
    memory_hits: list[dict[str, Any]],
    status: Optional[dict[str, Any]] = None,
) -> str:
    lines = [
        "## RuFlo orchestration (live)",
        "Use this routing and memory context when answering. Mention recommended agents when useful.",
    ]
    if isinstance(routing, dict):
        primary = (
            routing.get("primaryAgent")
            or routing.get("agent")
            or routing.get("recommendedAgent")
        )
        if isinstance(primary, dict):
            agent = primary.get("type") or primary.get("name") or primary.get("id")
            conf = primary.get("confidence")
            reason = primary.get("reason") or primary.get("rationale")
        else:
            agent = primary
            conf = routing.get("confidence")
            reason = routing.get("reason")
        if agent:
            line = f"- Primary agent: **{agent}**"
            if conf is not None:
                line += f" (confidence {conf})"
            lines.append(line)
        if reason:
            lines.append(f"- Routing reason: {reason}")
        alts = routing.get("alternatives") or routing.get("alternativeAgents")
        if isinstance(alts, list) and alts:
            names = []
            for alt in alts[:3]:
                if isinstance(alt, dict):
                    names.append(str(alt.get("type") or alt.get("name") or alt))
                else:
                    names.append(str(alt))
            if names:
                lines.append(f"- Alternatives: {', '.join(names)}")
        swarm_rec = routing.get("swarmRecommendation")
        if isinstance(swarm_rec, dict):
            topo = swarm_rec.get("topology")
            agents = swarm_rec.get("agents")
            if topo:
                lines.append(f"- Swarm topology: {topo}")
            if isinstance(agents, list) and agents:
                lines.append(f"- Swarm agents: {', '.join(str(a) for a in agents[:6])}")

    if isinstance(status, dict):
        active = status.get("hasActiveSwarm")
        if active is not None:
            lines.append(f"- Active swarm: {active}")

    if memory_hits:
        lines.append("- Memory hits:")
        for hit in memory_hits[:5]:
            content = str(
                hit.get("content") or hit.get("text") or hit.get("value") or ""
            ).strip()
            if len(content) > 220:
                content = content[:219].rstrip() + "…"
            score = hit.get("score") or hit.get("similarity")
            prefix = f"  - ({score:.2f}) " if isinstance(score, (int, float)) else "  - "
            if content:
                lines.append(prefix + content)

    return "\n".join(lines)


def format_ruflo_reasoning(
    *,
    routing: Optional[dict[str, Any]],
    routing_error: Optional[str] = None,
    memory_hits: Optional[list[dict[str, Any]]] = None,
) -> str:
    """User-facing summary of RuFlo routing + memory for the chat UI."""
    lines: list[str] = []
    if routing_error:
        lines.append(f"Routing indisponível: {routing_error}")

    if isinstance(routing, dict):
        primary = (
            routing.get("primaryAgent")
            or routing.get("agent")
            or routing.get("recommendedAgent")
        )
        if isinstance(primary, dict):
            agent = primary.get("type") or primary.get("name") or primary.get("id")
            conf = primary.get("confidence")
            reason = primary.get("reason") or primary.get("rationale")
        else:
            agent = primary
            conf = routing.get("confidence")
            reason = None
        if not reason:
            reason = routing.get("reason") or routing.get("rationale")
        if agent:
            line = f"Agente recomendado: {agent}"
            if conf is not None:
                line += f" (confiança {conf})"
            lines.append(line)
        if reason:
            lines.append(f"Motivo: {reason}")
        alts = routing.get("alternatives") or routing.get("alternativeAgents")
        if isinstance(alts, list) and alts:
            names: list[str] = []
            for alt in alts[:3]:
                if isinstance(alt, dict):
                    names.append(str(alt.get("type") or alt.get("name") or alt))
                else:
                    names.append(str(alt))
            if names:
                lines.append(f"Alternativas: {', '.join(names)}")
        swarm_rec = routing.get("swarmRecommendation")
        if isinstance(swarm_rec, dict):
            topo = swarm_rec.get("topology")
            agents = swarm_rec.get("agents")
            if topo:
                lines.append(f"Topologia swarm: {topo}")
            if isinstance(agents, list) and agents:
                lines.append(
                    "Agentes swarm: " + ", ".join(str(a) for a in agents[:6])
                )

    hits = memory_hits or []
    if hits:
        lines.append("Memória RuFlo:")
        for hit in hits[:5]:
            content = str(
                hit.get("content") or hit.get("text") or hit.get("value") or ""
            ).strip()
            if len(content) > 220:
                content = content[:219].rstrip() + "…"
            score = hit.get("score") or hit.get("similarity")
            prefix = f"  • ({score:.2f}) " if isinstance(score, (int, float)) else "  • "
            if content:
                lines.append(prefix + content)

    return "\n".join(lines).strip()


def build_swarm_system_prompt(*parts: str) -> str:
    """Merge ordered system-prompt fragments, skipping blanks.

    Used by both the RuFlo chat and the QClaw "Modo Swarm" send path so the
    orchestration block is injected consistently into the model prompt.
    """
    cleaned = [str(part or "").strip() for part in parts]
    return "\n\n".join(part for part in cleaned if part)


def handle_pending_bash_approval(
    upper_text: str,
    *,
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    session_key: str,
    persistence: Optional[ChatPersistence] = None,
    backend: str = "ruflo+deepseek",
    on_status: Any = None,
) -> Optional[dict[str, Any]]:
    """Handle an outstanding bash approval (ALLOW/CANCEL) for a session.

    Returns a reply dict to short-circuit the send when an approval is pending,
    or ``None`` when there is nothing to approve and the normal flow should run.
    """
    pending = _PENDING_BASH_APPROVALS.get(session_key)
    if not pending:
        return None

    def _reply(reply: str, meta: dict[str, Any]) -> dict[str, Any]:
        if persistence is not None and reply:
            try:
                persistence.history.append_message(
                    session_key, role="assistant", text=reply
                )
            except Exception as e:  # pragma: no cover - defensive
                log.warning(
                    "could not persist bash approval reply for %s: %s",
                    session_key,
                    e,
                )
        return {"ok": True, "reply": reply, "backend": backend, "ruflo": meta}

    if upper_text in {"ALLOW", "ALLOW!"}:
        script = str(pending.get("script") or "").strip()
        _PENDING_BASH_APPROVALS.pop(session_key, None)
        if not script:
            return _reply(
                "Não encontrei comando pendente para autorizar.",
                {"bash_approval": "missing"},
            )
        _emit_progress(
            on_status, "Swarm: autorização ALLOW recebida; a executar bash agora…"
        )
        exec_result = _run_bash_script(
            cfg,
            runtime,
            script,
            timeout=min(float(cfg.command_timeout_seconds), 60.0),
            bypass_allowlist=True,
        )
        if exec_result.get("ok"):
            out_text = str(exec_result.get("stdout") or "").strip()
            err_text = str(exec_result.get("stderr") or "").strip()
            if err_text:
                out_text = (out_text + "\n" + err_text).strip() if out_text else err_text
            if not out_text:
                out_text = "(sem saída)"
            reply = (
                "Execução autorizada (ALLOW):\n"
                "```bash\n"
                f"{script}\n"
                "```\n"
                "Saída:\n"
                "```text\n"
                f"{out_text[:4000]}\n"
                "```"
            )
            return _reply(
                reply,
                {"auto_bash_executed": True, "bash_approval": "granted"},
            )
        detail = str(exec_result.get("error") or exec_result.get("stderr") or "falha")
        exit_code = exec_result.get("exit_code")
        suffix = f" (exit {exit_code})" if exit_code is not None else ""
        return _reply(
            (
                "Execução autorizada (ALLOW), mas falhou:\n"
                "```bash\n"
                f"{script}\n"
                "```\n"
                f"Erro: {detail[:1200]}{suffix}"
            ),
            {"auto_bash_executed": False, "bash_approval": "granted-error"},
        )
    if upper_text in {"NEGAR", "DENY", "CANCELAR", "CANCEL"}:
        _PENDING_BASH_APPROVALS.pop(session_key, None)
        return _reply(
            "Execução bash cancelada. Não rodei o comando pendente.",
            {"bash_approval": "denied"},
        )
    return _reply(
        (
            "Há um comando bash pendente de aprovação. "
            "Responda ALLOW para executar, ou CANCELAR para não executar."
        ),
        {"bash_approval": "waiting"},
    )


def maybe_autoexec_bash(
    reply_text: str,
    *,
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    session_key: str,
    persistence: Optional[ChatPersistence] = None,
    on_status: Any = None,
) -> str:
    """Detect and run a bash block in an assistant reply.

    Allowlisted scripts run immediately; others are queued in
    ``_PENDING_BASH_APPROVALS`` for an ALLOW reply. Returns the summary text to
    append to the reply (empty when there is no bash block).
    """
    script = _extract_bash_script(reply_text)
    if not script:
        return ""
    _emit_progress(
        on_status, "Swarm: comando bash detectado na resposta; vou executar agora…"
    )
    exec_result = _run_bash_script(
        cfg,
        runtime,
        script,
        timeout=min(float(cfg.command_timeout_seconds), 60.0),
    )
    auto_exec_summary = ""
    if exec_result.get("ok"):
        _emit_progress(
            on_status, "Swarm: bash executado com sucesso; a anexar saída ao chat…"
        )
        out_text = str(exec_result.get("stdout") or "").strip()
        err_text = str(exec_result.get("stderr") or "").strip()
        if err_text:
            out_text = (out_text + "\n" + err_text).strip() if out_text else err_text
        if not out_text:
            out_text = "(sem saída)"
        auto_exec_summary = (
            "Execução automática (bash):\n"
            "```bash\n"
            f"{script}\n"
            "```\n"
            "Saída:\n"
            "```text\n"
            f"{out_text[:4000]}\n"
            "```"
        )
    else:
        detail = str(exec_result.get("error") or exec_result.get("stderr") or "falha")
        if "allowlist" in detail.lower():
            _emit_progress(
                on_status, "Swarm: comando fora da allowlist; aguardando sua aprovação…"
            )
            _PENDING_BASH_APPROVALS[session_key] = {
                "script": script,
                "created_at": time.time(),
            }
            auto_exec_summary = (
                "Comando bash detectado, mas fora da allowlist automática.\n"
                "```bash\n"
                f"{script}\n"
                "```\n"
                "Responda ALLOW para executar mesmo assim, ou CANCELAR para não executar."
            )
        else:
            _emit_progress(on_status, "Swarm: bash falhou; a anexar diagnóstico ao chat…")
            exit_code = exec_result.get("exit_code")
            suffix = f" (exit {exit_code})" if exit_code is not None else ""
            auto_exec_summary = (
                "Execução automática (bash) falhou:\n"
                "```bash\n"
                f"{script}\n"
                "```\n"
                f"Erro: {detail[:1200]}{suffix}"
            )
    if persistence is not None and auto_exec_summary:
        try:
            persistence.history.append_message(
                session_key, role="assistant", text=auto_exec_summary
            )
        except Exception as e:  # pragma: no cover - defensive
            log.warning(
                "could not persist auto bash output for %s: %s", session_key, e
            )
    return auto_exec_summary


def gather_orchestration_context(
    cfg: RufloChatConfig,
    runtime: QclawRuntime,
    prompt: str,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    _emit_progress(
        on_progress,
        "RuFlo: route + memória + status em paralelo…",
    )
    t0 = time.time()
    routed: dict[str, Any] = {}
    memory_hits: list[dict[str, Any]] = []
    st: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_route_task, cfg, runtime, prompt, on_progress=on_progress): "route",
            pool.submit(ruflo_status, cfg, runtime, on_progress=on_progress): "status",
        }
        if cfg.memory_search_enabled and prompt.strip():
            futures[
                pool.submit(_memory_hits, cfg, runtime, prompt, on_progress=on_progress)
            ] = "memory"
        for fut in as_completed(futures):
            kind = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("ruflo gather %s failed: %s", kind, exc)
                if kind == "route":
                    routed = {"ok": False, "error": str(exc)}
                elif kind == "memory":
                    memory_hits = []
                else:
                    st = {"ok": False, "swarm_error": str(exc)}
                continue
            if kind == "route":
                routed = result if isinstance(result, dict) else {}
            elif kind == "memory":
                memory_hits = result if isinstance(result, list) else []
            else:
                st = result if isinstance(result, dict) else {}

    elapsed_ms = int((time.time() - t0) * 1000)
    _emit_progress(on_progress, f"RuFlo: contexto reunido em {elapsed_ms} ms")

    routing = routed.get("routing") if routed.get("ok") else None
    if routing:
        primary = (
            routing.get("primaryAgent")
            or routing.get("agent")
            or routing.get("recommendedAgent")
        )
        if isinstance(primary, dict):
            agent_label = primary.get("type") or primary.get("name") or primary.get("id")
        else:
            agent_label = primary
        if agent_label:
            _emit_progress(
                on_progress, f"RuFlo: agente sugerido → {agent_label}"
            )
    elif routed.get("error"):
        _emit_progress(
            on_progress,
            f"RuFlo: routing indisponível ({str(routed.get('error'))[:120]})",
        )

    if memory_hits:
        _emit_progress(
            on_progress, f"RuFlo: {len(memory_hits)} memória(s) relevante(s)"
        )
    elif cfg.memory_search_enabled:
        _emit_progress(on_progress, "RuFlo: sem memória relevante")

    swarm = st.get("swarm") if isinstance(st.get("swarm"), dict) else None
    if isinstance(swarm, dict):
        active = swarm.get("hasActiveSwarm")
        if active is not None:
            _emit_progress(on_progress, f"RuFlo: swarm ativo={active}")
    elif st.get("swarm_error"):
        _emit_progress(
            on_progress,
            f"RuFlo: swarm status erro ({str(st.get('swarm_error'))[:120]})",
        )

    block = format_orchestration_block(
        routing=routing if isinstance(routing, dict) else None,
        memory_hits=memory_hits,
        status=swarm,
    )
    return {
        "ok": True,
        "routing": routing,
        "routing_error": routed.get("error"),
        "memory_hits": memory_hits,
        "orchestration_block": block,
        "status": st,
    }


def new_ruflo_session_key() -> str:
    return f"ruflo:orchestrator:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def create_ruflo_session(
    runtime: QclawRuntime,
    cfg: RufloChatConfig,
    *,
    title: Optional[str] = None,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict[str, Any]:
    key = new_ruflo_session_key()
    resolved_title = (title or "").strip() or f"RuFlo {time.strftime('%H:%M')}"
    tid = (team_id or "").strip() or None
    if persistence is None:
        return {
            "ok": True,
            "session_key": key,
            "agentId": RUFLO_AGENT_ID,
            "title": resolved_title,
            "source": "ruflo",
            "team_id": tid,
        }
    created = persistence.history.create_session(
        agent_id=RUFLO_AGENT_ID,
        title=resolved_title,
        session_key=key,
        source="ruflo",
        user_id=user_id,
        team_id=tid,
    )
    return {
        "ok": True,
        "session_key": created["session_key"],
        "sessionId": created.get("id"),
        "agentId": RUFLO_AGENT_ID,
        "title": created.get("title") or resolved_title,
        "source": "ruflo",
        "team_id": tid,
    }


def list_ruflo_sessions(
    *,
    persistence: Optional[ChatPersistence],
    user_id: Optional[str] = None,
    limit: int = 80,
) -> dict[str, Any]:
    if persistence is None:
        return {"ok": True, "sessions": [], "default_session_key": None, "agent": RUFLO_AGENT_ID}
    sessions = persistence.history.list_sessions(
        agent_id=RUFLO_AGENT_ID,
        user_id=user_id,
        limit=limit,
    )
    for row in sessions:
        row["source"] = "ruflo"
    default_key = sessions[0]["key"] if sessions else None
    return {
        "ok": True,
        "sessions": sessions,
        "default_session_key": default_key,
        "agent": RUFLO_AGENT_ID,
        "backend": "ruflo+deepseek",
    }


def delete_ruflo_session(
    runtime: QclawRuntime,
    *,
    session_key: str,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    key = (session_key or "").strip()
    if not key:
        return {"ok": False, "error": "session_key is required"}
    removed_disk = delete_openclaw_session_entry(
        runtime, session_key=key, agent_id=RUFLO_AGENT_ID
    )
    removed_db = False
    if persistence is not None:
        removed_db = persistence.history.delete_session(key, user_id=user_id)
    if not (removed_disk or removed_db):
        return {"ok": False, "error": "session not found", "session_key": key}
    return {
        "ok": True,
        "session_key": key,
        "removed_disk": removed_disk,
        "removed_db": removed_db,
    }


def read_ruflo_messages(
    runtime: QclawRuntime,
    *,
    session_key: str,
    limit: int = 120,
    persistence: Optional[ChatPersistence] = None,
) -> dict[str, Any]:
    result = read_messages(
        runtime,
        session_key=session_key,
        agent_id=RUFLO_AGENT_ID,
        limit=limit,
        persistence=persistence,
    )
    if result.get("ok"):
        result["source"] = "ruflo"
        result["backend"] = "ruflo+deepseek"
    return result


def send_ruflo_message(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    ruflo_cfg: RufloChatConfig,
    openclaw_cfg: OpenclawChatConfig,
    agent_cfg: AgentConfig,
    *,
    session_key: str,
    message: str,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    on_progress: Any = None,
    user_already_saved: bool = False,
    progress_job_id: Optional[str] = None,
    project_knowledge_block: Optional[str] = None,
    project_store: Optional[Any] = None,
    model_id: Optional[str] = None,
    qclaw_tools: Optional[Any] = None,
) -> dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "message required"}

    def _status(msg: str) -> None:
        publish_chat_status(
            msg,
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
        )

    upper_text = text.upper()
    approval = handle_pending_bash_approval(
        upper_text,
        cfg=ruflo_cfg,
        runtime=runtime,
        session_key=session_key,
        persistence=persistence,
        backend="ruflo+deepseek",
        on_status=_status,
    )
    if approval is not None:
        return approval

    _status("RuFlo: a orquestrar (routing, memória e swarm)…")
    ctx = gather_orchestration_context(
        ruflo_cfg, runtime, text, on_progress=_status
    )
    block = str(ctx.get("orchestration_block") or "").strip()
    memory_hits = ctx.get("memory_hits") or []
    routing = ctx.get("routing") if isinstance(ctx.get("routing"), dict) else None
    reasoning = format_ruflo_reasoning(
        routing=routing,
        routing_error=ctx.get("routing_error"),
        memory_hits=memory_hits if isinstance(memory_hits, list) else [],
    )
    if persistence is not None and reasoning:
        try:
            persistence.history.append_message(
                session_key, role="reasoning", text=reasoning
            )
        except Exception as e:
            log.warning("could not persist ruflo reasoning for %s: %s", session_key, e)

    knowledge = (project_knowledge_block or "").strip()
    merged_prompt = build_swarm_system_prompt(
        ruflo_cfg.system_prompt,
        knowledge,
        block,
        openclaw_cfg.system_prompt,
    )

    patched_cfg = openclaw_cfg.model_copy(update={"system_prompt": merged_prompt})

    result = send_chat_message(
        runtime,
        doctor_cfg,
        patched_cfg,
        agent_cfg,
        session_key=session_key,
        message=text,
        agent_id=RUFLO_AGENT_ID,
        model_id=model_id,
        persistence=persistence,
        project_store=project_store,
        user_id=user_id,
        on_progress=on_progress,
        user_already_saved=user_already_saved,
        progress_job_id=progress_job_id,
        qclaw_tools=qclaw_tools,
    )
    if result.get("ok"):
        reply_text = str(result.get("reply") or "")
        auto_exec_summary = maybe_autoexec_bash(
            reply_text,
            cfg=ruflo_cfg,
            runtime=runtime,
            session_key=session_key,
            persistence=persistence,
            on_status=_status,
        )
        if auto_exec_summary:
            result["reply"] = (reply_text + "\n\n" + auto_exec_summary).strip()

        result["backend"] = "ruflo+deepseek"
        result["ruflo"] = {
            "routing": routing,
            "routing_error": ctx.get("routing_error"),
            "memory_hits": len(memory_hits) if isinstance(memory_hits, list) else 0,
            "reasoning": reasoning,
            "project_knowledge_chars": len(knowledge),
            "auto_bash_executed": bool(auto_exec_summary),
        }
    return result

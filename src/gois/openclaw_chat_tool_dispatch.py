"""OpenClaw chat tool context, guards, and the main tool dispatcher."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .chat_history import ChatPersistence
from .config import OpenclawChatConfig, OpenclawDoctorConfig
from .openclaw_chat_media_tools import (
    _run_show_media,
    _run_show_slides_pdf,
    spawn_slides_pdf_background,
)
from .slides_batch_images import _run_slides_batch_images, spawn_slides_batch_background
from .slides_narration import _run_slides_narration
from .elevenlabs_voice import dispatch_elevenlabs_narrate
from .gemini_music import dispatch_gemini_music_generate
from .gemini_computer_use import dispatch_gemini_computer_use
from .curso_notebooks_docker import _run_curso_notebooks_docker
from .roteiro_books import dispatch_roteiro_book_tool
from .roteiro_book_sync import dispatch_roteiro_book_sync_tool
from .roteiro_courses import dispatch_roteiro_course_tool
from .modulo_portal import _run_modulo_portal
from .slides_corner_decor import _run_slides_corner_decor
from .slides_replace_didactic import _run_slides_replace_didactic
from .openclaw_chat_runtime import QclawRuntime
from .openclaw_chat_tool_runners import (
    _resolve_group_jid_from_memory,
    _run_agent_evaluate_tool,
    _run_agent_fix_tool,
    _run_ai_kb_tool,
    _run_aulas_memoria_tool,
    _run_calendar_tool,
    _run_chat_personality_tool,
    _run_desktop_control_tool,
    _run_email_memoria_tool,
    _run_email_team_pdf_tool,
    _run_gmail_attachments_tool,
    _run_gmail_tool,
    _run_google_photos_tool,
    _run_article_images_tool,
    _run_index_team_articles_tool,
    _run_team_article_pdf_tool,
    _run_local_photos_tool,
    _run_notion_tool,
    _run_overleaf_template_tool,
    _run_remove_team_articles_tool,
    _run_ruflo_swarm_tool,
    _run_jobs_manage_tool,
    _run_token_mode_tool,
    _run_monitor_update_tool,
    _run_kanban_ide_handoff_tool,
    _run_app_passwords_tool,
    _run_google_oauth_tool,
    _run_aws_manage_tool,
    _run_budget_tool,
    _run_team_payments_tool,
    _run_team_files_search_tool,
    _run_team_files_download_tool,
    _run_team_files_send_tool,
    _run_team_rules_tool,
    _run_team_facts_tool,
    _run_legal_evaluation_tool,
    _run_journal_rules_tool,
    _run_swarm_manage_tool,
    _run_team_articles_tool,
    _run_team_git_tool,
    _run_team_whatsapp_tool,
    _run_visual_memory_tool,
    _run_character_tool,
    _run_thumbnail_style_tool,
    _run_user_personal_data_tool,
    _run_identidade_civil_tool,
    _run_curriculum_tool,
    _run_minicurriculum_tool,
    _run_teams_calendar_tool,
    _run_tool_learning_tool,
    _run_trello_tool,
    _run_whatsapp_busca_tool,
    _run_whatsapp_groups_sync_tool,
    _run_whatsapp_memoria_tool,
    _send_to_group_jid_direct,
    _wacli_resolve_group_jid,
)
from .openclaw_chat_whatsapp_groups import _team_context_search, _wacli_group_numbers
from .recovery import Recovery

log = logging.getLogger(__name__)

_QCLAW_TOOL_SESSION = "agent:main:gois-tool-bridge"

# Names models sometimes emit instead of qclaw_run_shell.
_SHELL_TOOL_NAMES = frozenset(
    {
        "qclaw_run_shell",
        "bash",
        "run_shell",
        "shell",
        "execute_bash",
        "run_bash",
        "terminal",
        "run_terminal_cmd",
    }
)

@dataclass(frozen=True)
class QclawChatToolContext:
    """Live QClaw recovery + monitor snapshot for dashboard chat tools."""

    recovery: Recovery
    doctor_cfg: OpenclawDoctorConfig
    status_snapshot: Callable[[], dict]
    # Allowlisted WhatsApp recipient (JID or bare digits). When set, chat-agent
    # shell commands that try to send WhatsApp to any other number are blocked.
    whatsapp_recipient: Optional[str] = None
    # Hermes cron: jobs.json snapshot and optional per-job last run (markdown).
    hermes_cron_snapshot: Optional[Callable[[], dict]] = None
    hermes_cron_job_result: Optional[Callable[[str], dict]] = None
    jobs_cancel: Optional[Callable[[dict], dict]] = None
    jobs_cron_action: Optional[Callable[[str, str], dict]] = None
    jobs_cron_create: Optional[Callable[[dict], dict]] = None
    jobs_cron_edit: Optional[Callable[[str, dict], dict]] = None
    wacli_auth_qr: Optional[Callable[[], dict]] = None
    wacli_unlock: Optional[Callable[[dict], dict]] = None
    whatsapp_status: Optional[Callable[[], dict]] = None
    # Enfileira texto para o destinatário allowlisted (não bloqueia o event loop).
    whatsapp_send: Optional[Callable[[str, bool], dict]] = None
    whatsapp_send_to: Optional[Callable[[str, str, bool], dict]] = None
    hermes_agent_create: Optional[Callable[[dict], dict]] = None
    openai_swarm_create: Optional[Callable[[dict], dict]] = None
    kanban_create_card: Optional[Callable[[dict], dict]] = None
    kanban_attachment_upload: Optional[Callable[[dict], dict]] = None
    kanban_attachment_move: Optional[Callable[[dict], dict]] = None
    kanban_attachment_copy: Optional[Callable[[dict], dict]] = None
    kanban_ide_handoff: Optional[Callable[[dict], dict]] = None
    team_kanban_get: Optional[Callable[[str], dict]] = None
    team_swarm_run: Optional[Callable[[str, dict], dict]] = None
    swarm_graph_run: Optional[Callable[[dict], dict]] = None
    swarm_graph_preview: Optional[Callable[[dict], dict]] = None
    kanban_requirements: Optional[Callable[[dict], dict]] = None
    team_create: Optional[Callable[[dict], dict]] = None
    team_list: Optional[Callable[[], dict]] = None
    # Team WhatsApp CRUD via chat (direct store access, bypasses HTTP auth)
    team_whatsapp_numbers_get: Optional[Callable[[str], dict]] = None
    team_whatsapp_numbers_set: Optional[Callable[[str, dict], dict]] = None
    team_whatsapp_number_add: Optional[Callable[[str, dict], dict]] = None
    team_whatsapp_number_remove: Optional[Callable[[str, dict], dict]] = None
    team_whatsapp_send: Optional[Callable[[str, dict], dict]] = None
    team_whatsapp_broadcast: Optional[Callable[[str, dict], dict]] = None
    team_whatsapp_send_to_group: Optional[Callable[[str, dict], dict]] = None
    # Team notify_emails CRUD via chat
    team_notify_emails_get: Optional[Callable[[str], dict]] = None
    team_notify_emails_set: Optional[Callable[[str, dict], dict]] = None
    # Team contacts (members) CRUD via chat
    team_contacts_get: Optional[Callable[[str], dict]] = None
    team_contacts_upsert: Optional[Callable[[str, dict], dict]] = None
    team_contacts_remove: Optional[Callable[[str, dict], dict]] = None
    # Allowlist CRUD via chat
    allowlist_list: Optional[Callable[[], dict]] = None
    allowlist_add: Optional[Callable[[dict], dict]] = None
    allowlist_remove: Optional[Callable[[dict], dict]] = None
    allowlist_toggle: Optional[Callable[[dict], dict]] = None
    model_quotas_get: Optional[Callable[[], dict]] = None
    swarm_model: Optional[Callable[[str, dict], dict]] = None
    swarm_robot_update: Optional[Callable[[str, dict], dict]] = None
    swarm_robots_snapshot: Optional[Callable[[], dict]] = None
    # HermesAgentCreateConfig instance for kanban attachment path resolution.
    hermes_agent_create_cfg: Optional[Any] = None


def _swarm_run_max_cards(args: dict[str, Any], *, default: int = 1) -> int:
    """Resolve max_cards from chat tool args (all_cards=true → 0 = todos)."""
    if bool(args.get("all_cards")):
        return 0
    raw = args.get("max_cards")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _swarm_run_payload_extras(args: dict[str, Any], run_payload: dict[str, Any]) -> None:
    if bool(args.get("force")):
        run_payload["force"] = True
    if "open_ide" in args:
        run_payload["open_ide"] = bool(args.get("open_ide", True))


def _make_swarm_run_on_start(
    *,
    persistence: Any,
    session_key: str,
    swarm_label: str,
    run_payload: dict[str, Any],
    preview_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> Callable[[str], None]:
    """Prime job progress + chat status lines with the next kanban card (if any)."""
    from .chat_tool_background import prime_swarm_run_job_progress

    payload = dict(run_payload)

    def _prime(job_id: str) -> None:
        preview: Optional[dict[str, Any]] = None
        if preview_fn is not None:
            try:
                preview = preview_fn(payload)
            except Exception:
                preview = None
        prime_swarm_run_job_progress(
            job_id,
            persistence=persistence,
            session_key=str(session_key or "").strip(),
            swarm_label=str(swarm_label or "").strip() or "swarm",
            preview=preview if isinstance(preview, dict) else None,
        )

    return _prime


# Match the *send* entrypoints only — `wacli status` / `wacli stop` are fine.
# Anchored to non-identifier chars on the left so it doesn't trip on e.g.
# "swaclient". The `wacli-send.sh` wrapper from the digest pipeline is always
# a send, so any invocation of it triggers the guard.
_WHATSAPP_TRIGGER_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])"
    r"(?:"
    r"wacli\s+send\b|"
    r"wacli-send(?:\.sh)?\b|"
    r"wa\s+send\b"
    r")",
    re.IGNORECASE,
)

# Phone-like tokens: optional `+`, 10–15 digits with optional spaces/dashes/parens,
# optionally followed by a WhatsApp JID host (`@s.whatsapp.net`, `@c.us`, `@g.us`).
_WHATSAPP_MESSAGE_RE = re.compile(
    r"""--message\s+(?P<q>['"])(?P<msg>.*?)(?P=q)""",
    re.DOTALL,
)

# Any wacli CLI in shell except send — blocks for minutes on store lock.
_WACLI_SHELL_PROBE_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])wacli\b",
    re.IGNORECASE,
)

# Read-only wacli subcommands (local DB) — safe to run in shell without redirect.
_WACLI_SHELL_READ_ONLY_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])wacli\s+messages\s+(?:list|search|show|context)\b",
    re.IGNORECASE,
)

_WACLI_SHELL_MEDIA_DOWNLOAD_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])wacli\s+media\s+download\b",
    re.IGNORECASE,
)


def _parse_wacli_shell_flag(command: str, flag: str) -> Optional[str]:
    text = command or ""
    patterns = (
        rf"--{re.escape(flag)}=(['\"]?)([^'\"\s]+)\1",
        rf"--{re.escape(flag)}\s+(['\"])(.+?)\1",
        rf"--{re.escape(flag)}\s+(\S+)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return (m.group(m.lastindex) or "").strip()
    return None


def _extract_whatsapp_message_from_command(command: str) -> Optional[str]:
    """Parse --message \"...\" from a wacli send shell command."""
    m = _WHATSAPP_MESSAGE_RE.search(command or "")
    if not m:
        return None
    return (m.group("msg") or "").strip() or None


def _try_redirect_wacli_media_download_shell(command: str) -> Optional[dict[str, Any]]:
    """Redirect ``wacli media download`` shell to serialized Python download."""
    cmd = (command or "").strip()
    if not cmd or not _WACLI_SHELL_MEDIA_DOWNLOAD_RE.search(cmd):
        return None
    msg_id = _parse_wacli_shell_flag(cmd, "id")
    if not msg_id:
        return {
            "ok": False,
            "error": "wacli media download requer --id <message-id>",
            "hint": "Use qclaw_whatsapp_media_download com msg_id e chat (JID do grupo).",
            "next_tool": "qclaw_whatsapp_media_download",
        }
    from .whatsapp_busca import download_message_media

    chat = _parse_wacli_shell_flag(cmd, "chat") or ""
    out = download_message_media(msg_id, chat=chat)
    out["via"] = "shell_redirect"
    if out.get("ok"):
        out.setdefault(
            "hint",
            "Download concluído — da próxima vez use qclaw_whatsapp_media_download.",
        )
    else:
        out.setdefault(
            "hint",
            "Use qclaw_whatsapp_media_download (chat + msg_id) ou "
            "qclaw_whatsapp_messages_search para achar o msg_id.",
        )
        out.setdefault("next_tool", "qclaw_whatsapp_media_download")
    return out


def _try_redirect_wacli_probe_shell(
    command: str,
    ctx: QclawChatToolContext,
) -> Optional[dict[str, Any]]:
    """Redirect non-send wacli shell probes to qclaw_whatsapp_status (instant)."""
    cmd = (command or "").strip()
    if not cmd or not _WACLI_SHELL_PROBE_RE.search(cmd):
        return None
    if _WHATSAPP_TRIGGER_RE.search(cmd):
        return None
    if _WACLI_SHELL_READ_ONLY_RE.search(cmd):
        return None
    media_dl = _try_redirect_wacli_media_download_shell(cmd)
    if media_dl is not None:
        return media_dl
    hint = (
        "Comando wacli no shell redirecionado — use qclaw_whatsapp_status "
        "(fila, lock, sync), qclaw_wacli_unlock se lock, qclaw_send_whatsapp para enviar, "
        "qclaw_whatsapp_media_download para baixar anexos. "
        "Nunca wacli doctor/sync no shell: trava o chat aguardando lock."
    )
    if ctx.whatsapp_status is None:
        return {
            "ok": False,
            "error": "Comando wacli no shell bloqueado (store lock trava o chat).",
            "hint": hint,
            "blocked": True,
            "next_tool": "qclaw_whatsapp_status",
        }
    out = dict(ctx.whatsapp_status())
    out["via"] = "shell_redirect"
    out.setdefault("hint", hint)
    return out


_WHATSAPP_NUMBER_RE = re.compile(
    r"(?P<num>\+?\d[\d\s\-().]{8,18})(?P<jid>@[A-Za-z0-9.\-]+)?"
)


def _normalize_whatsapp_recipient(value: Any) -> str:
    """Reduce a recipient (JID, formatted number, etc.) to bare digits.

    >>> _normalize_whatsapp_recipient("558591736779@s.whatsapp.net")
    '558591736779'
    >>> _normalize_whatsapp_recipient("+55 (85) 9173-6779")
    '558591736779'
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        text = text.split("@", 1)[0]
    return re.sub(r"\D+", "", text)


def _extract_whatsapp_recipients(command: str) -> tuple[bool, list[str]]:
    """Inspect *command* for WhatsApp-send invocations.

    Returns ``(triggered, recipients)`` where ``triggered`` is True when the
    command appears to call a wacli-style send and ``recipients`` is the
    deduplicated list of normalized phone-number tokens found in it.
    """
    if not command:
        return False, []
    if not _WHATSAPP_TRIGGER_RE.search(command):
        return False, []
    found: list[str] = []
    for m in _WHATSAPP_NUMBER_RE.finditer(command):
        norm = _normalize_whatsapp_recipient(m.group("num"))
        if 10 <= len(norm) <= 15 and norm not in found:
            found.append(norm)
    return True, found


def _resolve_session_team_id(
    persistence: Optional[ChatPersistence],
    session_key: str,
) -> str:
    if persistence is None or not session_key:
        return ""
    try:
        sess_row = persistence.history.get_session_by_key(session_key)
    except Exception:
        return ""
    if sess_row is None:
        return ""
    return str(getattr(sess_row, "team_id", None) or "").strip()


def _enforce_session_team_scope(
    team_id: str,
    *,
    persistence: Optional[ChatPersistence],
    session_key: str,
) -> Optional[dict[str, Any]]:
    """Block team WhatsApp actions when *team_id* diverges from the chat session."""
    session_team = _resolve_session_team_id(persistence, session_key)
    if not session_team:
        return None
    tid = (team_id or "").strip()
    if not tid:
        return {
            "ok": False,
            "error": (
                f"envio bloqueado: esta sessão pertence ao time '{session_team}'. "
                "Informe team_id correto ou abra o chat do time."
            ),
            "blocked": True,
            "session_team_id": session_team,
        }
    if tid != session_team:
        return {
            "ok": False,
            "error": (
                f"envio bloqueado: team_id '{tid}' não corresponde ao time da sessão "
                f"('{session_team}')."
            ),
            "blocked": True,
            "session_team_id": session_team,
        }
    return None


_TEAM_CREATE_SHELL_BLOCKED = re.compile(
    r"""
    \bmkdir\b[^\n|;&]*\bteams\b
    |
    \b(?:cp|mv)\b[^\n|;&]*\bteams/
    |
    (?:>>|>|tee)\s+[^\s]*\bteams/
    |
    (?:>>|>|tee|\bcp\b|\bmv\b)[^\n|;&]*\bteam\.json\b
    |
    (?:>>|>|tee|\bcp\b|\bmv\b)[^\n|;&]*\.team-name\b
    |
    # CLI: `gois team create` / `gois teams create` (qualquer ordem de flags)
    \bgois\b[^\n|;&]*\bteams?\b[^\n|;&]*\bcreate\b
    |
    \bgois\b[^\n|;&]*\bcreate\b[^\n|;&]*\bteams?\b
    |
    # HTTP POST a endpoints de time (curl/requests/httpx/fetch/wget)
    requests\.(?:post|put)\s*\([^)\n]*\bteams?\b
    |
    httpx\.(?:post|put)\s*\([^)\n]*\bteams?\b
    |
    -X\s*(?:POST|PUT)\b[^\n]*\bteams?\b
    |
    fetch\s*\([^)\n]*\bteams?\b[^\n]*(?:POST|PUT)
    """,
    re.I | re.X,
)


def _check_manual_team_creation_shell(command: str) -> Optional[dict[str, Any]]:
    """Block shell commands that provision teams on disk without MongoDB."""
    cmd = str(command or "").strip()
    if not cmd or not _TEAM_CREATE_SHELL_BLOCKED.search(cmd):
        return None
    return {
        "ok": False,
        "error": (
            "criação de time bloqueada: NÃO crie times via shell (gois team create, "
            "POST a /teams, curl, requests.post) nem manipulando pastas em teams/. "
            "Esses caminhos NÃO persistem e o time não aparecerá no chat. "
            "Use OBRIGATORIAMENTE a ferramenta qclaw_create_team — ela persiste no "
            "MongoDB e provisiona workspace/kanban. NÃO afirme que o time foi criado "
            "a menos que qclaw_create_team retorne ok=true."
        ),
        "blocked_command": cmd[:500],
        "blocked": True,
    }


def _check_whatsapp_guard(
    command: str, allowed_recipient: Optional[str]
) -> Optional[dict[str, Any]]:
    """Return an error dict when *command* targets a non-allowed WhatsApp number.

    Returns ``None`` when the command is not a WhatsApp send or every detected
    destination matches the allowlist. The error dict is shaped like other tool
    results so the chat loop can surface it directly to the model.
    """
    from .whatsapp_allowlist import (
        check_whatsapp_recipient,
        load_allowed_recipient_digits,
    )
    from .whatsapp_allowlist_resolve import extract_whatsapp_destinations_from_command
    from .whatsapp_send_policy import check_outbound_policy

    triggered, destinations = extract_whatsapp_destinations_from_command(command)
    if not triggered:
        return None

    blocked_command = (command or "")[:500]
    if destinations:
        for dest in destinations:
            hours_err = check_outbound_policy(recipient=dest)
            if hours_err:
                return {
                    "ok": False,
                    "error": hours_err,
                    "blocked_command": blocked_command,
                    "blocked": True,
                }
    else:
        hours_err = check_outbound_policy()
        if hours_err:
            return {
                "ok": False,
                "error": hours_err,
                "blocked_command": blocked_command,
                "blocked": True,
            }

    allowed = load_allowed_recipient_digits()
    allowed_norm = _normalize_whatsapp_recipient(allowed_recipient)
    if not allowed and allowed_norm:
        allowed = frozenset({allowed_norm})

    if not destinations:
        allowed_list = ", ".join(sorted(allowed)) if allowed else "(nenhum DM — grupos de time permitidos)"
        return {
            "ok": False,
            "error": (
                "envio de WhatsApp bloqueado: comando 'wacli' detectado, mas "
                f"nenhum destinatário pôde ser identificado. Permitidos: {allowed_list}."
            ),
            "blocked_command": blocked_command,
            "detected_recipients": [],
            "allowed_recipient": allowed_norm,
            "allowed_recipients": sorted(allowed),
        }

    bad: list[str] = []
    for dest in destinations:
        err = check_whatsapp_recipient(dest, allowed_digits=allowed)
        if err:
            bad.append(dest)
    if bad:
        allowed_list = ", ".join(sorted(allowed)) if allowed else "(grupos de time / allowlist)"
        return {
            "ok": False,
            "error": (
                f"envio de WhatsApp bloqueado: destinatário(s) {bad} fora da allowlist. "
                f"Permitidos: {allowed_list}."
            ),
            "blocked_command": blocked_command,
            "detected_recipients": destinations,
            "allowed_recipient": allowed_norm,
            "allowed_recipients": sorted(allowed),
        }
    return None


def _tool_result_string(result: Any) -> str:
    """Serialize a tool result for the LLM, stripping large data URLs.

    Data URLs (base64-encoded images/videos) are useful for the front-end but
    would overflow the LLM context window.  We replace them with a short
    placeholder so the model knows media was produced without consuming tokens.
    """
    if isinstance(result, (dict, list)):
        cleaned = _strip_data_urls_for_llm(result)
        return json.dumps(cleaned, default=str, ensure_ascii=False)
    return str(result)


_DATA_URL_THRESHOLD = 256  # data URLs longer than this are stripped


def _strip_data_urls_for_llm(obj: Any) -> Any:
    """Recursively strip large data: URLs from a JSON-serializable object."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("data:") and len(v) > _DATA_URL_THRESHOLD:
                # Keep a short descriptor so the LLM knows the field exists
                out[k] = f"[data_url: {len(v)} chars, omitted for context]"
            else:
                out[k] = _strip_data_urls_for_llm(v)
        return out
    if isinstance(obj, list):
        return [_strip_data_urls_for_llm(item) for item in obj]
    return obj


def _run_shell_command(
    command: str,
    *,
    cwd: Optional[str],
    timeout: float,
    max_chars: int,
) -> dict[str, Any]:
    """Execute a shell command locally (dashboard chat tool)."""
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command is required"}
    work_dir = Path(cwd).expanduser() if cwd else Path.home()
    if not work_dir.is_dir():
        return {"ok": False, "error": f"working directory not found: {work_dir}"}
    # Cross-OS shell selection: prefer bash on POSIX (consistent semantics for
    # the LLM tool), fall back to platform default. On Windows let cmd.exe
    # handle it via shell=True. This is intentionally a shell — the tool is an
    # authenticated user-side console; callers must enforce auth/allowlist.
    import shutil as _shutil
    executable: Optional[str] = None
    if os.name != "nt":
        executable = _shutil.which("bash") or _shutil.which("sh")
    shell_env = {**os.environ, "QCLAW_CHAT_SESSION": "1"}
    try:
        completed = subprocess.run(  # noqa: S602 - intentional shell tool
            cmd,
            shell=True,
            executable=executable,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=max(1.0, float(timeout)),
            check=False,
            env=shell_env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"command timed out after {int(timeout)}s",
            "command": cmd[:500],
            "cwd": str(work_dir),
        }
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "command": cmd[:500]}

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout
    if stderr:
        combined = (combined + ("\n" if combined else "") + "--- stderr ---\n" + stderr)
    truncated = False
    if len(combined) > max_chars:
        combined = combined[: max_chars - 40] + "\n…(output truncated)…"
        truncated = True

    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": stdout[:max_chars] if stdout else "",
        "stderr": stderr[:max_chars] if stderr else "",
        "output": combined,
        "truncated": truncated,
        "cwd": str(work_dir),
        "command": cmd[:500],
    }


from .image_generation_fallback import (
    DEFAULT_GROK_IMAGINE_MODEL,
    DEFAULT_IMAGEN_MODEL,
    generate_image_with_fallback,
)


def _image_register_meta(session_key: str, source: str) -> dict[str, Any]:
    return {
        "session_key": str(session_key or "").strip(),
        "source": str(source or "").strip(),
    }


def _run_nano_banana_generate(
    *,
    prompt: str,
    filename: str,
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
    cwd: Optional[str],
    timeout: float,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return generate_image_with_fallback(
        prompt=prompt,
        filename=filename,
        primary_provider="nano",
        cwd=cwd,
        timeout=timeout,
        resolution=resolution,
        input_image_path=input_image_path,
        register_meta=register_meta,
    )


def _apply_image_tool_batch_progress(
    progress_job_id: Optional[str],
    args: dict[str, Any],
    *,
    ok: bool = True,
    filename: str = "",
    starting: bool = False,
) -> None:
    if not progress_job_id:
        return
    # Even when a slide fails we must advance block_turn so the UI
    # reflects how many slides have been attempted (not just successes).
    # Without this the progress bar stays stuck at e.g. "1/100".
    from .chat_jobs import apply_batch_progress_from_tool_args

    slide_n = args.get("slide_index") or args.get("slide_number") or args.get("slide")
    slide_t = args.get("slide_total") or args.get("total_slides") or args.get("max_slides")
    detail = ""
    if starting:
        if slide_n and slide_t:
            detail = f"A gerar slide {slide_n}/{slide_t}…"
        else:
            detail = f"A gerar {filename or 'slide'}…"
        apply_batch_progress_from_tool_args(
            progress_job_id,
            args,
            detail=detail,
            filename=filename,
            in_progress=True,
        )
        return
    elif slide_n and slide_t:
        detail = f"Slide {slide_n}/{slide_t}"
        if filename:
            detail += f" — {filename}"
    elif filename:
        detail = filename
    apply_batch_progress_from_tool_args(
        progress_job_id,
        args,
        detail=detail,
        filename=filename,
    )


def _run_image_tool_with_optional_background(
    args: dict[str, Any],
    *,
    progress_job_id: Optional[str],
    session_key: str,
    filename: str,
    run_fn: Callable[[], dict[str, Any]],
    persistence: Any = None,
) -> dict[str, Any]:
    from .chat_image_background import (
        should_background_image_tool,
        spawn_chat_image_tool_background,
    )

    if should_background_image_tool(
        args,
        progress_job_id=progress_job_id,
        session_key=session_key,
    ):
        return spawn_chat_image_tool_background(
            args=args,
            session_key=session_key,
            progress_job_id=progress_job_id,
            run_fn=run_fn,
            filename=filename,
            apply_progress=_apply_image_tool_batch_progress,
            persistence=persistence,
        )
    _apply_image_tool_batch_progress(
        progress_job_id,
        args,
        filename=filename,
        starting=True,
    )
    result = run_fn()
    if isinstance(result, dict):
        _apply_image_tool_batch_progress(
            progress_job_id,
            args,
            ok=bool(result.get("ok")),
            filename=filename,
        )
        attempts = result.get("fallback_attempts")
        if progress_job_id and isinstance(attempts, list) and attempts:
            from .chat_jobs import record_model_attempts_batch

            record_model_attempts_batch(progress_job_id, attempts)
    return result if isinstance(result, dict) else {"ok": False, "error": "invalid tool result"}


def _maybe_attach_image_to_card(
    result: dict[str, Any],
    *,
    args: dict[str, Any],
    ctx: "QclawChatToolContext",
    team_id: Optional[str] = None,
    session_key: str = "",
    persistence: Any = None,
) -> dict[str, Any]:
    """Anexa a imagem gerada a um card kanban quando card_id/task_id é informado.

    Funciona tanto no caminho síncrono quanto no background, pois é chamado
    dentro do ``run_fn`` da geração de imagem. Nunca falha a geração: erros de
    anexação são reportados em ``result['card_attachment']``.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    raw_card = args.get("card_id") or args.get("task_id") or args.get("attach_to_card")
    task_id = str(raw_card or "").strip()
    if not task_id or task_id.lower() in ("0", "false", "no", "none"):
        return result
    upload_fn = getattr(ctx, "kanban_attachment_upload", None)
    if upload_fn is None:
        result.setdefault(
            "card_attachment", {"ok": False, "error": "Kanban indisponível no contexto atual"}
        )
        return result
    out_path = str(result.get("output_path") or result.get("image_path") or "").strip()
    if not out_path:
        result.setdefault(
            "card_attachment", {"ok": False, "error": "imagem gerada sem caminho para anexar"}
        )
        return result
    fp = Path(out_path).expanduser()
    if not fp.is_file():
        result.setdefault(
            "card_attachment", {"ok": False, "error": f"imagem não encontrada: {out_path}"}
        )
        return result
    import mimetypes as _mt

    file_name = fp.name
    mime_type = _mt.guess_type(file_name)[0] or "image/png"
    tid = str(team_id or args.get("team_id") or "").strip()
    workdir = str(args.get("workdir") or "").strip()
    if not tid and persistence is not None and session_key:
        try:
            _sess = persistence.history.get_session_by_key(session_key)
            if _sess is not None and getattr(_sess, "team_id", None):
                tid = _sess.team_id
        except Exception:
            pass
    if not tid and not workdir:
        result.setdefault(
            "card_attachment",
            {"ok": False, "error": "team_id ou workdir necessário para anexar ao card"},
        )
        return result
    payload: dict[str, Any] = {
        "task_id": task_id,
        "file_name": file_name,
        "name": file_name,
        "mime_type": mime_type,
        "file_path": str(fp),
    }
    if tid:
        payload["team_id"] = tid
    if workdir:
        payload["workdir"] = workdir
    try:
        att = upload_fn(payload)
    except Exception as e:  # noqa: BLE001 - nunca derruba a geração
        att = {"ok": False, "error": f"erro ao anexar ao card: {e}"}
    result["card_attachment"] = att
    if isinstance(att, dict) and att.get("ok"):
        result["attached_to_card"] = task_id
    return result


def _run_grok_imagine_generate(
    *,
    prompt: str,
    filename: str,
    model: str = DEFAULT_GROK_IMAGINE_MODEL,
    resolution: str = "2k",
    aspect_ratio: str = "auto",
    input_image_path: Optional[str] = None,
    cwd: Optional[str],
    timeout: float,
    skip_existing: bool = False,
    allow_fallback: bool = True,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return generate_image_with_fallback(
        prompt=prompt,
        filename=filename,
        primary_provider="grok",
        allow_fallback=allow_fallback,
        cwd=cwd,
        timeout=timeout,
        grok_model=model,
        grok_resolution=resolution,
        grok_aspect_ratio=aspect_ratio,
        input_image_path=input_image_path,
        skip_existing=skip_existing,
        register_meta=register_meta,
    )


def _run_imagen_generate(
    *,
    prompt: str,
    filename: str,
    model: str = DEFAULT_IMAGEN_MODEL,
    aspect_ratio: str = "16:9",
    style: Optional[str] = None,
    cwd: Optional[str],
    timeout: float,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return generate_image_with_fallback(
        prompt=prompt,
        filename=filename,
        primary_provider="imagen",
        cwd=cwd,
        timeout=timeout,
        model=model,
        aspect_ratio=aspect_ratio,
        style=style,
        register_meta=register_meta,
    )


def _run_openrouter_image_generate(
    *,
    prompt: str,
    filename: str,
    model: str,
    aspect_ratio: str = "16:9",
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
    allow_fallback: bool = False,
    cwd: Optional[str],
    timeout: float,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return generate_image_with_fallback(
        prompt=prompt,
        filename=filename,
        primary_provider="openrouter",
        allow_fallback=allow_fallback,
        cwd=cwd,
        timeout=timeout,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        input_image_path=input_image_path,
        register_meta=register_meta,
    )


def _compact_monitor_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Trim status payload for LLM tool results."""
    out: dict[str, Any] = {
        "up": snap.get("up"),
        "name": snap.get("name"),
        "health_url": snap.get("health_url"),
        "consecutive_failures": snap.get("consecutive_failures"),
        "failure_threshold": snap.get("failure_threshold"),
        "last_health_ok_ts": snap.get("last_health_ok_ts"),
        "last_health_fail_ts": snap.get("last_health_fail_ts"),
        "last_failure_summary": (snap.get("last_failure_summary") or "")[:800],
        "last_recovery_report": (snap.get("last_recovery_report") or "")[:800],
    }
    hermes = snap.get("hermes")
    if isinstance(hermes, dict):
        out["hermes"] = {
            "up": hermes.get("up") or hermes.get("active"),
            "dashboard_url": hermes.get("dashboard_url"),
            "dashboard_up": hermes.get("dashboard_up"),
        }
    agents = snap.get("agents")
    if isinstance(agents, list):
        out["agents"] = agents[:12]
    procs = snap.get("qclaw_processes")
    if isinstance(procs, list):
        out["qclaw_processes"] = procs[:8]
    mcp = snap.get("mcp_servers")
    if isinstance(mcp, list) and mcp:
        out["mcp_servers"] = mcp
    return out





def _run_qclaw_chat_tool(
    name: str,
    args: dict[str, Any],
    *,
    ctx: QclawChatToolContext,
    runtime: QclawRuntime,
    chat_cfg: OpenclawChatConfig,
    persistence: Optional[ChatPersistence] = None,
    project_store: Optional[Any] = None,
    session_key: str = "",
    progress_job_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Any:
    from .whatsapp_send_policy import CONTEXT_CHAT, whatsapp_outbound_scope

    with whatsapp_outbound_scope(CONTEXT_CHAT):
        return _run_qclaw_chat_tool_impl(
            name,
            args,
            ctx=ctx,
            runtime=runtime,
            chat_cfg=chat_cfg,
            persistence=persistence,
            project_store=project_store,
            session_key=session_key,
            progress_job_id=progress_job_id,
            model_id=model_id,
        )


def _run_qclaw_chat_tool_impl(
    name: str,
    args: dict[str, Any],
    *,
    ctx: QclawChatToolContext,
    runtime: QclawRuntime,
    chat_cfg: OpenclawChatConfig,
    persistence: Optional[ChatPersistence] = None,
    project_store: Optional[Any] = None,
    session_key: str = "",
    progress_job_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Any:
    from .gois_lite import is_gois_lite, lite_chat_tool_allowed, lite_resolve_chat_tool_name

    if is_gois_lite():
        if not lite_chat_tool_allowed(name):
            return {"ok": False, "error": f"tool not available in gois-lite: {name}"}
        name = lite_resolve_chat_tool_name(name)

    recovery = ctx.recovery
    if name == "qclaw_save_project_note":
        if project_store is None:
            return {"ok": False, "error": "project memory is disabled"}
        content = str(args.get("content") or "").strip()
        if not content:
            return {"ok": False, "error": "content is required"}
        from .app_passwords_ops import looks_like_app_password_fact

        if looks_like_app_password_fact(content):
            return {
                "ok": False,
                "error": (
                    "senhas de app não vão para project memory — use "
                    "qclaw_app_passwords_store (MongoDB env_keys em /chaves)"
                ),
            }
        kind = str(args.get("kind") or "note").strip() or "note"
        try:
            fact = project_store.add_fact(content, kind=kind)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if persistence is not None and persistence.memory is not None:
            project_store.index_in_chroma(persistence.memory)
        return {"ok": True, "fact": fact}
    if name == "qclaw_health_check":
        return asyncio.run(recovery.health_check())
    if name == "qclaw_process_status":
        return asyncio.run(recovery.process_status())
    if name == "qclaw_read_log_tail":
        path = str(args.get("path") or "")
        lines = int(args.get("lines") or 120)
        return asyncio.run(recovery.read_log_tail(path, lines))
    if name == "qclaw_monitor_snapshot":
        return _compact_monitor_snapshot(ctx.status_snapshot())
    if name.startswith("qclaw_cards_") or name in ("qclaw_get_errors", "qclaw_errors_to_cards"):
        from .cards_mcp import (
            enrich_cards_args,
            resolve_chat_tool_name,
        )

        mcp_tool = resolve_chat_tool_name(name)
        if not mcp_tool:
            return {"ok": False, "error": f"unknown cards tool: {name}"}
        enriched = enrich_cards_args(
            args,
            tool_name=mcp_tool,
            persistence=persistence,
            session_key=session_key,
        )
        from .cards_mcp import dispatch_cards_tool

        return dispatch_cards_tool(mcp_tool, enriched)
    if name == "qclaw_list_teams":
        if ctx.team_list is None:
            return {"ok": False, "error": "listagem de times indisponível"}
        return ctx.team_list()
    if name == "qclaw_team_kanban":
        if ctx.team_kanban_get is None:
            return {"ok": False, "error": "kanban de times indisponível"}
        team_id = str(args.get("team_id") or "").strip()
        return ctx.team_kanban_get(team_id)
    if name == "qclaw_kanban_requirements":
        if ctx.kanban_requirements is None:
            return {"ok": False, "error": "agente de requisitos indisponível"}
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id é obrigatório"}
        team_id = str(args.get("team_id") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    team_id = _sess.team_id
            except Exception:
                pass
        if not team_id:
            return {
                "ok": False,
                "error": "team_id é obrigatório (use qclaw_team_kanban para descobrir)",
            }
        return ctx.kanban_requirements({"team_id": team_id, "task_id": task_id})
    if name == "qclaw_kanban_ide_handoff":
        if ctx.kanban_ide_handoff is None:
            return {"ok": False, "error": "handoff IDE indisponível no contexto atual"}
        task_id = str(args.get("task_id") or args.get("card_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id é obrigatório"}
        team_id = str(args.get("team_id") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    team_id = _sess.team_id
            except Exception:
                pass
        payload = dict(args)
        payload["task_id"] = task_id
        if team_id:
            payload["team_id"] = team_id
        return ctx.kanban_ide_handoff(payload)
    if name == "qclaw_team_whatsapp":
        action = str(args.get("action") or "").strip()
        team_id = str(args.get("team_id") or "").strip()
        if action in {"broadcast", "send", "send_to_group", "add", "set", "remove"}:
            scope_err = _enforce_session_team_scope(
                team_id,
                persistence=persistence,
                session_key=session_key,
            )
            if scope_err is not None:
                return scope_err
        if action in {"add", "set"}:
            from .whatsapp_send_policy import agent_destination_mutation_block_message

            return {
                "ok": False,
                "error": agent_destination_mutation_block_message(),
                "blocked": True,
            }
        return _run_team_whatsapp_tool(args, ctx=ctx)
    if name == "qclaw_team_git":
        return _run_team_git_tool(args, ctx=ctx)
    if name == "qclaw_team_whatsapp_individual":
        action = str(args.get("action") or "list").strip()
        team_id = str(args.get("team_id") or "").strip()
        if action in {"add", "remove"}:
            scope_err = _enforce_session_team_scope(
                team_id,
                persistence=persistence,
                session_key=session_key,
            )
            if scope_err is not None:
                return scope_err
        if action == "list":
            if not team_id:
                return {"ok": False, "error": "team_id é obrigatório para action=list"}
            if ctx.team_whatsapp_numbers_get is not None:
                return ctx.team_whatsapp_numbers_get(team_id)
            return {"ok": False, "error": "team_whatsapp_numbers_get not configured"}
        if not team_id:
            return {"ok": False, "error": "team_id é obrigatório"}
        number = str(args.get("number") or "").strip()
        if not number:
            return {"ok": False, "error": "number é obrigatório"}
        if action == "add":
            from .whatsapp_send_policy import agent_destination_mutation_block_message

            return {
                "ok": False,
                "error": agent_destination_mutation_block_message(),
                "blocked": True,
            }
        if action == "remove":
            if ctx.team_whatsapp_number_remove is not None:
                return ctx.team_whatsapp_number_remove(team_id, {"number": number})
            return {"ok": False, "error": "team_whatsapp_number_remove not configured"}
        return {"ok": False, "error": f"action desconhecida: {action}"}
    if name == "qclaw_team_contacts":
        action = str(args.get("action") or "get").strip()
        team_id = str(args.get("team_id") or "").strip()
        if not team_id:
            return {"ok": False, "error": "team_id é obrigatório"}
        if action == "get":
            if ctx.team_contacts_get is None:
                return {"ok": False, "error": "contacts indisponível no contexto atual"}
            return ctx.team_contacts_get(team_id)
        if action == "upsert":
            if ctx.team_contacts_upsert is None:
                return {"ok": False, "error": "contacts indisponível no contexto atual"}
            contact = args.get("contact")
            if not isinstance(contact, dict):
                return {"ok": False, "error": "contact é obrigatório para action=upsert"}
            return ctx.team_contacts_upsert(team_id, {"contact": contact})
        if action == "remove":
            if ctx.team_contacts_remove is None:
                return {"ok": False, "error": "contacts indisponível no contexto atual"}
            uid = str(args.get("id") or args.get("email") or "").strip()
            if not uid:
                return {"ok": False, "error": "id ou email é obrigatório para action=remove"}
            return ctx.team_contacts_remove(team_id, {"id": uid})
        return {"ok": False, "error": f"action desconhecida: {action}"}
    if name == "qclaw_hermes_cron_health":
        if ctx.hermes_cron_snapshot is None:
            return {
                "ok": False,
                "error": "Hermes não configurado neste gois",
            }
        from .hermes_cron import compact_cron_snapshot_for_chat

        snap = ctx.hermes_cron_snapshot()
        out = compact_cron_snapshot_for_chat(snap)
        job_id = str(args.get("job_id") or "").strip()
        if job_id and ctx.hermes_cron_job_result is not None:
            detail = ctx.hermes_cron_job_result(job_id)
            out["job_detail"] = {
                "job_id": job_id,
                "result": detail,
            }
        return out
    if name == "qclaw_list_chat_models":
        models = list_chat_models(chat_cfg)
        return {
            "ok": True,
            "default_model_id": effective_chat_default_model_id(
                chat_cfg.default_model_id
            ),
            "models": models,
        }
    if name == "qclaw_list_image_models":
        from .image_models_store import format_models_markdown, list_image_models

        fmt = str(args.get("format") or "markdown").strip().lower()
        payload = list_image_models(
            provider=str(args.get("provider") or "").strip() or None,
            quality=str(args.get("quality") or "").strip() or None,
            use_case=str(args.get("use_case") or "").strip() or None,
        )
        if fmt == "json":
            return payload
        payload["markdown"] = format_models_markdown(payload)
        return payload
    if name == "qclaw_model_quotas":
        if ctx.model_quotas_get is None:
            return {"ok": False, "error": "consulta de cotas indisponível"}
        return ctx.model_quotas_get()
    if name == "qclaw_show_media":
        return _run_show_media(
            path_or_url=str(args.get("path_or_url") or "").strip(),
            kind=str(args.get("kind") or "auto").strip().lower(),
            caption=str(args.get("caption") or "").strip(),
            poster=str(args.get("poster_path_or_url") or "").strip(),
        )
    if name == "qclaw_show_slides_pdf":
        pdf_kwargs = dict(
            path=str(args.get("path") or "").strip(),
            pages=str(args.get("pages") or "").strip(),
            max_pages=int(args.get("max_pages") or 12),
            dpi=int(args.get("dpi") or 150),
            whatsapp_to=str(args.get("whatsapp_to") or args.get("to") or "").strip(),
            whatsapp_caption=str(args.get("whatsapp_caption") or args.get("caption") or "").strip(),
        )
        if not pdf_kwargs["path"]:
            return {"ok": False, "error": "path is required"}
        from .chat_tool_background import should_background_chat_tool

        if should_background_chat_tool(
            args,
            progress_job_id=progress_job_id,
            session_key=str(session_key or "").strip(),
        ):
            return spawn_slides_pdf_background(
                persistence=persistence,
                session_key=str(session_key or "").strip(),
                **pdf_kwargs,
            )
        return _run_show_slides_pdf(
            progress_job_id=progress_job_id,
            **pdf_kwargs,
        )
    if name == "qclaw_nano_banana_generate":
        prompt = str(args.get("prompt") or "").strip()
        filename = str(args.get("filename") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        if not filename:
            return {"ok": False, "error": "filename is required"}
        resolution = str(args.get("resolution") or "1K").strip()
        input_image = args.get("input_image_path") or args.get("input_image")
        # Resolve team workspace figures/ as output directory when team is active.
        cwd: Optional[str] = None
        _team_id_img = str(args.get("team_id") or "").strip()
        if not _team_id_img and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    _team_id_img = _sess.team_id
            except Exception:
                pass
        if _team_id_img:
            # Resolve the team's actual workdir (respects local_path if set).
            _team_base_dir: Optional[Path] = None
            if ctx.team_list is not None:
                try:
                    _tl_resp = ctx.team_list()
                    _teams_list = (_tl_resp or {}).get("teams") or []
                    for _t in _teams_list:
                        if _t.get("id") == _team_id_img:
                            _lp = (_t.get("local_path") or "").strip()
                            if _lp:
                                _team_base_dir = Path(_lp).expanduser().resolve()
                            break
                except Exception:
                    pass
            if _team_base_dir is None:
                from .local_paths import project_stack_root as _psr
                _team_base_dir = _psr() / "accounts" / "teams" / _team_id_img
            _figures_dir = _team_base_dir / "workspace" / "figures"
            _figures_dir.mkdir(parents=True, exist_ok=True)
            cwd = str(_figures_dir)
        if not cwd:
            cwd = chat_cfg.shell_working_dir or str(Path.cwd())
        return _run_image_tool_with_optional_background(
            args,
            progress_job_id=progress_job_id,
            session_key=session_key,
            filename=filename,
            persistence=persistence,
            run_fn=lambda: _maybe_attach_image_to_card(
                _run_nano_banana_generate(
                    prompt=prompt,
                    filename=filename,
                    resolution=resolution,
                    input_image_path=str(input_image).strip() if input_image else None,
                    cwd=str(cwd).strip() if cwd else None,
                    timeout=max(chat_cfg.shell_timeout_seconds, 180.0),
                    register_meta=_image_register_meta(session_key, name),
                ),
                args=args,
                ctx=ctx,
                team_id=_team_id_img,
                session_key=session_key,
                persistence=persistence,
            ),
        )
    if name == "qclaw_grok_imagine_generate":
        prompt = str(args.get("prompt") or "").strip()
        filename = str(args.get("filename") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        if not filename:
            return {"ok": False, "error": "filename is required"}
        model = str(args.get("model") or DEFAULT_GROK_IMAGINE_MODEL).strip()
        resolution = str(args.get("resolution") or "2k").strip()
        aspect_ratio = str(args.get("aspect_ratio") or "auto").strip()
        input_image = args.get("input_image_path") or args.get("input_image")
        cwd_grok: Optional[str] = None
        _team_id_grok = str(args.get("team_id") or "").strip()
        if not _team_id_grok and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    _team_id_grok = _sess.team_id
            except Exception:
                pass
        if _team_id_grok:
            _team_base_dir_grok: Optional[Path] = None
            if ctx.team_list is not None:
                try:
                    _tl_resp = ctx.team_list()
                    _teams_list = (_tl_resp or {}).get("teams") or []
                    for _t in _teams_list:
                        if _t.get("id") == _team_id_grok:
                            _lp = (_t.get("local_path") or "").strip()
                            if _lp:
                                _team_base_dir_grok = Path(_lp).expanduser().resolve()
                            break
                except Exception:
                    pass
            if _team_base_dir_grok is None:
                from .local_paths import project_stack_root as _psr_grok

                _team_base_dir_grok = _psr_grok() / "accounts" / "teams" / _team_id_grok
            _figures_dir_grok = _team_base_dir_grok / "workspace" / "figures"
            _figures_dir_grok.mkdir(parents=True, exist_ok=True)
            cwd_grok = str(_figures_dir_grok)
        if not cwd_grok:
            cwd_grok = chat_cfg.shell_working_dir or str(Path.cwd())
        resume = not bool(args.get("force") or args.get("regenerate"))
        if args.get("resume") is False:
            resume = False
        elif args.get("resume") or args.get("skip_existing"):
            resume = True
        # Auto-disable fallback when user explicitly requests a single model
        # or passes no_fallback=true
        _explicit_model = str(args.get("model") or "").strip()
        _no_fb = bool(args.get("no_fallback"))
        _allow_fb = not _no_fb
        if _allow_fb and _explicit_model and _explicit_model != DEFAULT_GROK_IMAGINE_MODEL:
            _allow_fb = False
        return _run_image_tool_with_optional_background(
            args,
            progress_job_id=progress_job_id,
            session_key=session_key,
            filename=filename,
            persistence=persistence,
            run_fn=lambda: _maybe_attach_image_to_card(
                _run_grok_imagine_generate(
                    prompt=prompt,
                    filename=filename,
                    model=model,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    input_image_path=str(input_image).strip() if input_image else None,
                    cwd=str(cwd_grok).strip() if cwd_grok else None,
                    timeout=max(chat_cfg.shell_timeout_seconds, 180.0),
                    skip_existing=resume,
                    allow_fallback=_allow_fb,
                    register_meta=_image_register_meta(session_key, name),
                ),
                args=args,
                ctx=ctx,
                team_id=_team_id_grok,
                session_key=session_key,
                persistence=persistence,
            ),
        )
    if name == "qclaw_imagen_generate":
        prompt = str(args.get("prompt") or "").strip()
        filename = str(args.get("filename") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        if not filename:
            return {"ok": False, "error": "filename is required"}
        model = str(args.get("model") or DEFAULT_IMAGEN_MODEL).strip()
        aspect_ratio = str(args.get("aspect_ratio") or "16:9").strip()
        style = str(args.get("style") or "").strip() or None
        cwd_imagen: Optional[str] = None
        _team_id_imagen = str(args.get("team_id") or "").strip()
        if not _team_id_imagen and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    _team_id_imagen = _sess.team_id
            except Exception:
                pass
        if _team_id_imagen:
            _team_base_dir_imagen: Optional[Path] = None
            if ctx.team_list is not None:
                try:
                    _tl_resp = ctx.team_list()
                    _teams_list = (_tl_resp or {}).get("teams") or []
                    for _t in _teams_list:
                        if _t.get("id") == _team_id_imagen:
                            _lp = (_t.get("local_path") or "").strip()
                            if _lp:
                                _team_base_dir_imagen = Path(_lp).expanduser().resolve()
                            break
                except Exception:
                    pass
            if _team_base_dir_imagen is None:
                from .local_paths import project_stack_root as _psr_imagen

                _team_base_dir_imagen = _psr_imagen() / "accounts" / "teams" / _team_id_imagen
            _figures_dir_imagen = _team_base_dir_imagen / "workspace" / "figures"
            _figures_dir_imagen.mkdir(parents=True, exist_ok=True)
            cwd_imagen = str(_figures_dir_imagen)
        if not cwd_imagen:
            cwd_imagen = chat_cfg.shell_working_dir or str(Path.cwd())
        return _run_image_tool_with_optional_background(
            args,
            progress_job_id=progress_job_id,
            session_key=session_key,
            filename=filename,
            persistence=persistence,
            run_fn=lambda: _maybe_attach_image_to_card(
                _run_imagen_generate(
                    prompt=prompt,
                    filename=filename,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    style=style,
                    cwd=str(cwd_imagen).strip() if cwd_imagen else None,
                    timeout=max(chat_cfg.shell_timeout_seconds, 180.0),
                    register_meta=_image_register_meta(session_key, name),
                ),
                args=args,
                ctx=ctx,
                team_id=_team_id_imagen,
                session_key=session_key,
                persistence=persistence,
            ),
        )
    if name == "qclaw_openrouter_image_generate":
        prompt = str(args.get("prompt") or "").strip()
        filename = str(args.get("filename") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        if not filename:
            return {"ok": False, "error": "filename is required"}
        from .openrouter_image_generate import DEFAULT_OPENROUTER_IMAGE_MODEL

        model = str(
            args.get("model") or args.get("model_id") or DEFAULT_OPENROUTER_IMAGE_MODEL
        ).strip()
        aspect_ratio = str(args.get("aspect_ratio") or "16:9").strip()
        resolution = str(args.get("resolution") or "1K").strip()
        input_image = args.get("input_image_path") or args.get("input_image")
        cwd_or: Optional[str] = None
        _team_id_or = str(args.get("team_id") or "").strip()
        if not _team_id_or and persistence is not None and session_key:
            try:
                _sess = persistence.history.get_session_by_key(session_key)
                if _sess is not None and getattr(_sess, "team_id", None):
                    _team_id_or = _sess.team_id
            except Exception:
                pass
        if _team_id_or:
            _team_base_dir_or: Optional[Path] = None
            if ctx.team_list is not None:
                try:
                    _tl_resp = ctx.team_list()
                    _teams_list = (_tl_resp or {}).get("teams") or []
                    for _t in _teams_list:
                        if _t.get("id") == _team_id_or:
                            _lp = (_t.get("local_path") or "").strip()
                            if _lp:
                                _team_base_dir_or = Path(_lp).expanduser().resolve()
                            break
                except Exception:
                    pass
            if _team_base_dir_or is None:
                from .local_paths import project_stack_root as _psr_or

                _team_base_dir_or = _psr_or() / "accounts" / "teams" / _team_id_or
            _figures_dir_or = _team_base_dir_or / "workspace" / "figures"
            _figures_dir_or.mkdir(parents=True, exist_ok=True)
            cwd_or = str(_figures_dir_or)
        if not cwd_or:
            cwd_or = chat_cfg.shell_working_dir or str(Path.cwd())
        from .image_generation_fallback import parse_allow_fallback_flag

        _explicit_or = bool(str(args.get("model") or args.get("model_id") or "").strip())
        if _explicit_or and "allow_fallback" not in args and not (
            args.get("no_fallback") or args.get("disable_fallback")
        ):
            _allow_or = False
        else:
            _allow_or = parse_allow_fallback_flag(args, default=False)
        return _run_image_tool_with_optional_background(
            args,
            progress_job_id=progress_job_id,
            session_key=session_key,
            filename=filename,
            persistence=persistence,
            run_fn=lambda: _maybe_attach_image_to_card(
                _run_openrouter_image_generate(
                    prompt=prompt,
                    filename=filename,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    input_image_path=str(input_image).strip() if input_image else None,
                    allow_fallback=_allow_or,
                    cwd=str(cwd_or).strip() if cwd_or else None,
                    timeout=max(chat_cfg.shell_timeout_seconds, 180.0),
                    register_meta=_image_register_meta(session_key, name),
                ),
                args=args,
                ctx=ctx,
                team_id=_team_id_or,
                session_key=session_key,
                persistence=persistence,
            ),
        )
    if name == "qclaw_replicate_generate":
        from .replicate_generate import dispatch_replicate_generate

        prompt = str(args.get("prompt") or "").strip()
        model = str(args.get("model") or args.get("model_id") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        if not model:
            return {"ok": False, "error": "model is required"}
        run_args = dict(args)
        run_args["model"] = model
        run_args["timeout_seconds"] = max(chat_cfg.shell_timeout_seconds, 600.0)
        from .chat_tool_background import should_background_chat_tool, spawn_chat_tool_background

        if should_background_chat_tool(
            args,
            progress_job_id=progress_job_id,
            session_key=str(session_key or "").strip(),
        ):
            label = f"Replicate — {model}"

            def _run(_job_id: str) -> dict[str, Any]:
                return dispatch_replicate_generate(run_args)

            return spawn_chat_tool_background(
                kind="replicate",
                session_key=str(session_key or "").strip(),
                message_text=f"[replicate] {model}: {prompt[:120]}",
                label=label,
                run_fn=_run,
                persistence=persistence,
            )
        return dispatch_replicate_generate(run_args)
    if name == "qclaw_slides_corner_decor":
        path = str(args.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        analyze = bool(args.get("analyze"))
        prompt_corner = str(args.get("prompt") or "").strip()
        if not analyze and not prompt_corner:
            return {"ok": False, "error": "prompt is required unless analyze=true"}
        try:
            size_ratio = float(args.get("size_ratio") or 0.18)
        except (TypeError, ValueError):
            size_ratio = 0.18
        try:
            margin_ratio = float(args.get("margin_ratio") or 0.025)
        except (TypeError, ValueError):
            margin_ratio = 0.025
        try:
            max_overlap = float(args.get("max_overlap") or 0.12)
        except (TypeError, ValueError):
            max_overlap = 0.12
        output_path = str(args.get("output_path") or args.get("output") or "").strip() or None
        assets_dir = str(args.get("assets_dir") or "").strip() or None
        return _run_slides_corner_decor(
            path=path,
            prompt=prompt_corner,
            analyze=analyze,
            output_path=output_path,
            provider=str(args.get("provider") or "imagen").strip(),
            corner=str(args.get("corner") or "auto").strip(),
            slides=str(args.get("slides") or "all").strip(),
            size_ratio=size_ratio,
            margin_ratio=margin_ratio,
            max_overlap=max_overlap,
            assets_dir=assets_dir,
            timeout=max(chat_cfg.shell_timeout_seconds, 900.0),
        )
    if name == "qclaw_slides_replace_didactic":
        path = str(args.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        slides = str(args.get("slides") or "").strip()
        if not slides:
            return {"ok": False, "error": "slides is required (e.g. 3, 2-5)"}
        analyze_replace = bool(args.get("analyze"))
        output_path = str(args.get("output_path") or args.get("output") or "").strip() or None
        assets_dir = str(args.get("assets_dir") or "").strip() or None
        from .image_generation_fallback import parse_allow_fallback_flag

        return _run_slides_replace_didactic(
            path=path,
            slides=slides,
            analyze=analyze_replace,
            prompt=str(args.get("prompt") or "").strip(),
            output_path=output_path,
            provider=str(args.get("provider") or "imagen").strip(),
            style=str(args.get("style") or "").strip(),
            keep_title=bool(args.get("keep_title")),
            assets_dir=assets_dir,
            timeout=max(chat_cfg.shell_timeout_seconds, 900.0),
            progress_job_id=progress_job_id,
            allow_fallback=parse_allow_fallback_flag(args),
            model=str(args.get("model") or args.get("grok_model") or "").strip(),
        )
    if name == "qclaw_slides_batch_images":
        prompts_file = str(args.get("prompts_file") or args.get("prompts") or "").strip() or None
        deck_path = str(args.get("path") or args.get("deck") or "").strip() or None
        if not prompts_file and not deck_path:
            return {"ok": False, "error": "prompts_file or path (deck) is required"}
        try:
            max_slides = int(args.get("max_slides") or 200)
        except (TypeError, ValueError):
            max_slides = 200
        try:
            preview_count = int(args.get("preview_count") or 3)
        except (TypeError, ValueError):
            preview_count = 3
        try:
            delay = float(args.get("delay_seconds") or args.get("delay") or 2.0)
        except (TypeError, ValueError):
            delay = 2.0
        try:
            workers = int(args.get("workers") or 1)
        except (TypeError, ValueError):
            workers = 1
        batch_timeout = max(chat_cfg.shell_timeout_seconds, 7200.0)
        from .image_generation_fallback import parse_allow_fallback_flag
        from .slides_batch_prompts_fix import infer_card_id

        inferred_card = infer_card_id(
            card_id=str(args.get("card_id") or "").strip() or None,
            task_id=str(args.get("task_id") or "").strip() or None,
            prompts_file=prompts_file,
        )
        explicit_card = str(args.get("card_id") or args.get("task_id") or "").strip() or None
        card_id = explicit_card or inferred_card

        batch_kwargs = dict(
            prompts_file=prompts_file,
            deck_path=deck_path,
            slides=str(args.get("slides") or "all").strip(),
            analyze=bool(args.get("analyze")),
            prompt=str(args.get("prompt") or "").strip(),
            style=str(args.get("style") or "").strip(),
            provider=str(args.get("provider") or "nano").strip(),
            resolution=str(args.get("resolution") or "2K").strip(),
            output_dir=str(args.get("output_dir") or "").strip() or None,
            zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
            max_slides=max_slides,
            resume=bool(args.get("resume")),
            delay=delay,
            preview_count=preview_count,
            workers=workers,
            session_key=str(session_key or "").strip(),
            timeout=batch_timeout,
            allow_fallback=parse_allow_fallback_flag(args),
            model=str(args.get("model") or args.get("grok_model") or "").strip(),
            card_id=card_id,
            task_id=str(args.get("task_id") or args.get("card_id") or card_id or "").strip() or None,
            workdir=str(args.get("workdir") or "").strip() or None,
            auto_fix_prompts=not bool(args.get("no_auto_fix_prompts")),
        )
        if batch_kwargs["analyze"]:
            return _run_slides_batch_images(
                progress_job_id=progress_job_id,
                **batch_kwargs,
            )
        batch_display_key: Optional[str] = None
        if progress_job_id:
            from .chat_jobs import get_job

            parent = get_job(progress_job_id)
            if parent is not None:
                batch_display_key = (parent.display_key or "").strip() or None
        return spawn_slides_batch_background(
            persistence=persistence,
            parent_job_id=progress_job_id,
            display_key=batch_display_key,
            **batch_kwargs,
        )
    if name == "qclaw_slides_narration":
        slides_file = str(args.get("slides_file") or "").strip() or None
        deck_path = str(args.get("path") or args.get("deck") or "").strip() or None
        if not slides_file and not deck_path:
            return {"ok": False, "error": "slides_file or path (deck) is required"}
        try:
            max_slides = int(args.get("max_slides") or 200)
        except (TypeError, ValueError):
            max_slides = 200
        try:
            preview_count = int(args.get("preview_count") or 3)
        except (TypeError, ValueError):
            preview_count = 3
        try:
            workers = int(args.get("workers") or 4)
        except (TypeError, ValueError):
            workers = 4
        try:
            delay = float(args.get("delay_seconds") or args.get("delay") or 0.5)
        except (TypeError, ValueError):
            delay = 0.5
        try:
            target_seconds = float(args.get("target_seconds") or 25.0)
        except (TypeError, ValueError):
            target_seconds = 25.0
        try:
            wpm = float(args.get("wpm") or 140.0)
        except (TypeError, ValueError):
            wpm = 140.0
        narr_timeout = max(chat_cfg.shell_timeout_seconds, 7200.0)
        return _run_slides_narration(
            slides_file=slides_file,
            deck_path=deck_path,
            slides=str(
                args.get("slides_filter")
                or args.get("slide_filter")
                or (args.get("slides") if deck_path else "all")
                or "all"
            ).strip(),
            analyze=bool(args.get("analyze")),
            lesson_title=str(args.get("lesson_title") or "").strip(),
            lesson_context=str(args.get("lesson_context") or "").strip(),
            tone=str(args.get("tone") or "didático").strip(),
            language=str(args.get("language") or "pt-BR").strip(),
            target_seconds=target_seconds,
            wpm=wpm,
            model=str(args.get("model") or "gemini-3.5-flash").strip(),
            output_dir=str(args.get("output_dir") or "").strip() or None,
            zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
            max_slides=max_slides,
            workers=workers,
            resume=bool(args.get("resume")),
            delay=delay,
            preview_count=preview_count,
            session_key=str(session_key or "").strip(),
            timeout=narr_timeout,
            progress_job_id=progress_job_id,
        )
    if name == "qclaw_elevenlabs_narrate":
        voice_timeout = max(chat_cfg.shell_timeout_seconds, 600.0)
        payload = dict(args)
        payload["session_key"] = str(session_key or "").strip()
        payload["timeout_seconds"] = voice_timeout
        return dispatch_elevenlabs_narrate(payload)
    if name == "qclaw_gemini_music_generate":
        music_timeout = max(chat_cfg.shell_timeout_seconds, 300.0)
        payload = dict(args)
        payload["session_key"] = str(session_key or "").strip()
        payload["timeout_seconds"] = music_timeout
        return dispatch_gemini_music_generate(payload)
    if name == "qclaw_gemini_computer_use":
        cu_timeout = max(chat_cfg.shell_timeout_seconds, 600.0)
        payload = dict(args)
        payload["timeout_seconds"] = cu_timeout
        return dispatch_gemini_computer_use(payload)
    if name == "qclaw_curso_notebooks_docker":
        lessons_file = str(args.get("lessons_file") or args.get("lessons") or "").strip() or None
        plano_path = str(args.get("plano") or args.get("plano_path") or "").strip() or None
        if not lessons_file and not plano_path:
            return {"ok": False, "error": "lessons_file or plano is required"}
        try:
            max_lessons = int(args.get("max_lessons") or 200)
        except (TypeError, ValueError):
            max_lessons = 200
        try:
            workers = int(args.get("workers") or 4)
        except (TypeError, ValueError):
            workers = 4
        notebooks_timeout = max(chat_cfg.shell_timeout_seconds, 3600.0)
        return _run_curso_notebooks_docker(
            lessons_file=lessons_file,
            plano_path=plano_path,
            course_slug=str(args.get("course_slug") or "").strip(),
            course_title=str(args.get("course_title") or "").strip(),
            stack=str(args.get("stack") or "python-data").strip(),
            level=str(args.get("level") or "iniciante").strip(),
            analyze=bool(args.get("analyze")),
            output_dir=str(args.get("output_dir") or "").strip() or None,
            zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
            max_lessons=max_lessons,
            workers=workers,
            jupyter_port=int(args.get("jupyter_port") or 8888),
            token=str(args.get("token") or "").strip(),
            hide_solutions=bool(args.get("hide_solutions")),
            resume=bool(args.get("resume")),
            session_key=str(session_key or "").strip(),
            timeout=notebooks_timeout,
            progress_job_id=progress_job_id,
        )
    if name == "qclaw_roteiro_book_generate":
        return dispatch_roteiro_book_tool(
            {**args, "session_key": session_key},
            progress_job_id=progress_job_id,
            persistence=persistence,
        )
    if name == "qclaw_roteiro_book_sync":
        return dispatch_roteiro_book_sync_tool({**args, "session_key": session_key})
    if name == "qclaw_roteiro_course_sync":
        from .roteiro_course_sync import dispatch_roteiro_course_sync_tool

        return dispatch_roteiro_course_sync_tool({**args, "session_key": session_key})
    if name == "qclaw_roteiro_book_latex":
        from .roteiro_book_latex import dispatch_roteiro_book_latex_tool

        return dispatch_roteiro_book_latex_tool(args)
    if name == "qclaw_roteiro_course_generate":
        return dispatch_roteiro_course_tool(
            {**args, "session_key": session_key},
            progress_job_id=progress_job_id,
            persistence=persistence,
        )
    if name == "qclaw_modulo_portal":
        assets_dir = str(args.get("assets_dir") or args.get("assets") or "").strip()
        if not assets_dir:
            return {"ok": False, "error": "assets_dir is required"}
        portal_timeout = max(chat_cfg.shell_timeout_seconds, 600.0)
        return _run_modulo_portal(
            assets_dir=assets_dir,
            manifest=str(args.get("manifest") or args.get("manifest_file") or "").strip() or None,
            module_id=str(args.get("module_id") or "").strip(),
            module_title=str(args.get("module_title") or args.get("title") or "").strip(),
            auto_scan=bool(args.get("auto_scan")),
            analyze=bool(args.get("analyze")),
            output_dir=str(args.get("output_dir") or "").strip() or None,
            zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
            session_key=str(session_key or "").strip(),
            timeout=portal_timeout,
            progress_job_id=progress_job_id,
        )
    if name == "qclaw_send_whatsapp":
        if ctx.whatsapp_send is None:
            return {
                "ok": False,
                "error": (
                    "WhatsApp não configurado "
                    "(whatsapp_digest.recipient no config.yaml)"
                ),
            }
        msg = str(args.get("message") or "").strip()
        if not msg:
            return {"ok": False, "error": "message is required"}
        wait = bool(args.get("wait", False))
        if isinstance(args.get("wait"), str):
            wait = args.get("wait", "").strip().lower() in ("1", "true", "yes")
        from .whatsapp_send_policy import check_outbound_policy

        to = str(args.get("to") or args.get("recipient") or "").strip()
        policy_recipient = ctx.whatsapp_recipient or ""
        if to:
            from .whatsapp_allowlist_resolve import resolve_whatsapp_send_target

            jid, _, resolve_err = resolve_whatsapp_send_target(to)
            policy_recipient = jid if jid and not resolve_err else to
        hours_err = check_outbound_policy(recipient=policy_recipient or None)
        if hours_err:
            return {"ok": False, "error": hours_err, "blocked": True}
        session_team = _resolve_session_team_id(persistence, session_key)
        if session_team and to:
            if ctx.team_whatsapp_send is not None:
                from .whatsapp_outbound import enrich_whatsapp_send_result

                return enrich_whatsapp_send_result(
                    ctx.team_whatsapp_send(
                        session_team,
                        {"recipient": to, "message": msg, "wait": wait},
                    )
                )
            return {
                "ok": False,
                "error": (
                    f"envio bloqueado: conversa do time '{session_team}' — "
                    "use qclaw_team_whatsapp ou adicione o destino à allowlist do time."
                ),
                "blocked": True,
                "session_team_id": session_team,
            }
        if to:
            if ctx.whatsapp_send_to is not None:
                from .whatsapp_outbound import enrich_whatsapp_send_result

                return enrich_whatsapp_send_result(ctx.whatsapp_send_to(to, msg, wait))
            from .whatsapp_allowlist import (
                check_whatsapp_recipient,
                load_allowed_recipient_digits,
            )
            from .whatsapp_allowlist_resolve import resolve_whatsapp_send_target
            from .config import WhatsappDigestConfig
            from .whatsapp_outbound import enqueue_whatsapp, enrich_whatsapp_send_result

            jid, label, resolve_err = resolve_whatsapp_send_target(to)
            if resolve_err:
                return {"ok": False, "error": resolve_err}
            allowed = load_allowed_recipient_digits()
            allow_err = check_whatsapp_recipient(jid or "", allowed_digits=allowed)
            if allow_err:
                return {
                    "ok": False,
                    "error": allow_err,
                    "blocked": True,
                    "hint": "Use qclaw_allowlist_list para ver destinos permitidos.",
                }
            base = WhatsappDigestConfig(
                recipient=ctx.whatsapp_recipient or jid or "",
            )
            wd = base.model_copy(
                update={"recipient": jid, "skip_context_guard": True},
            )
            out = enrich_whatsapp_send_result(enqueue_whatsapp(wd, msg, wait=wait))
            if label:
                out["resolved_name"] = label
            return out
        raw = ctx.whatsapp_send(msg, wait)
        from .whatsapp_outbound import enrich_whatsapp_send_result

        return enrich_whatsapp_send_result(raw if isinstance(raw, dict) else {"ok": False})
    if name == "qclaw_send_whatsapp_file":
        from .whatsapp_file_send import send_whatsapp_file

        fpath = str(args.get("file_path") or args.get("path") or "").strip()
        if not fpath:
            return {"ok": False, "error": "file_path is required"}
        to = str(args.get("to") or args.get("recipient") or "").strip()
        caption = str(args.get("caption") or args.get("message") or "").strip()
        mode = str(args.get("mode") or "auto").strip().lower()
        pages = str(args.get("pages") or "").strip() or None
        try:
            max_pages = int(args.get("max_pages") or 10)
        except (TypeError, ValueError):
            max_pages = 10
        try:
            dpi = int(args.get("dpi") or 150)
        except (TypeError, ValueError):
            dpi = 150
        session_team = _resolve_session_team_id(persistence, session_key)
        team_group = bool(args.get("team_group") or args.get("send_to_team_group"))
        if isinstance(args.get("team_group"), str):
            team_group = args.get("team_group", "").strip().lower() in ("1", "true", "yes")
        if session_team and team_group:
            from .storage import get_account_store
            from .whatsapp_team_guard import team_whatsapp_send_to_group

            store = get_account_store()
            wait = bool(args.get("wait", False))
            if isinstance(args.get("wait"), str):
                wait = args.get("wait", "").strip().lower() in ("1", "true", "yes")
            out = team_whatsapp_send_to_group(
                store=store,
                team_id=session_team,
                user_id="system",
                message=caption,
                file_path=fpath,
                caption=caption or None,
                wait=wait,
            )
            out["session_team_id"] = session_team
            return out
        if session_team and to:
            from .storage import get_account_store
            from .whatsapp_allowlist_resolve import resolve_whatsapp_send_target
            from .whatsapp_team_guard import validate_team_whatsapp_send

            store = get_account_store()
            try:
                team = store.get_team(session_team, "system")
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            jid, label, resolve_err = resolve_whatsapp_send_target(to)
            if resolve_err:
                return {"ok": False, "error": resolve_err}
            recipient = jid or to
            scope_err = validate_team_whatsapp_send(team=team, recipient=recipient)
            if scope_err:
                return {"ok": False, "error": scope_err, "blocked": True}
            out = send_whatsapp_file(
                file_path=fpath,
                to=recipient,
                caption=caption,
                mode=mode,
                pages=pages,
                max_pages=max_pages,
                dpi=dpi,
                default_recipient=None,
                skip_allowlist=True,
            )
            if label:
                out["resolved_name"] = label
            out["session_team_id"] = session_team
            return out
        return send_whatsapp_file(
            file_path=fpath,
            to=to,
            caption=caption,
            mode=mode,
            pages=pages,
            max_pages=max_pages,
            dpi=dpi,
            default_recipient=ctx.whatsapp_recipient,
        )
    if name == "qclaw_allowlist_list":
        if ctx.allowlist_list is None:
            return {"ok": False, "error": "allowlist não configurada"}
        return ctx.allowlist_list()
    if name == "qclaw_allowlist_add":
        from .whatsapp_send_policy import agent_destination_mutation_block_message

        return {
            "ok": False,
            "error": agent_destination_mutation_block_message(),
            "blocked": True,
            "hint": (
                "Grupos/números já na allowlist podem receber envio via "
                "qclaw_send_whatsapp (parâmetro to). Confira com qclaw_allowlist_list."
            ),
        }
    if name == "qclaw_allowlist_remove":
        from .whatsapp_send_policy import agent_destination_mutation_block_message

        return {
            "ok": False,
            "error": agent_destination_mutation_block_message(),
            "blocked": True,
        }
    if name == "qclaw_allowlist_toggle":
        from .whatsapp_send_policy import agent_destination_mutation_block_message

        return {
            "ok": False,
            "error": agent_destination_mutation_block_message(),
            "blocked": True,
        }
    if name == "qclaw_wacli_auth_qr":
        if ctx.wacli_auth_qr is None:
            return {
                "ok": False,
                "error": "wacli não configurado (whatsapp_digest no config.yaml)",
            }
        return ctx.wacli_auth_qr()
    if name == "qclaw_wacli_unlock":
        if ctx.wacli_unlock is None:
            return {
                "ok": False,
                "error": "wacli não configurado (whatsapp_digest no config.yaml)",
            }
        kill_sync = args.get("kill_sync", True)
        if isinstance(kill_sync, str):
            kill_sync = kill_sync.strip().lower() not in ("0", "false", "no")
        return ctx.wacli_unlock({"kill_sync": bool(kill_sync)})
    if name == "qclaw_whatsapp_status":
        if ctx.whatsapp_status is None:
            return {
                "ok": False,
                "error": "WhatsApp não configurado (whatsapp_digest no config.yaml)",
            }
        return ctx.whatsapp_status()
    if name == "qclaw_wacli_group_numbers":
        group = str(args.get("group") or "").strip()
        if not group:
            return {"ok": False, "error": "group is required (nome ou JID)"}
        return _wacli_group_numbers(group, ctx)
    if name == "qclaw_whatsapp_groups_sync":
        return _run_whatsapp_groups_sync_tool(args)
    if name == "qclaw_team_context_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        scope = str(args.get("scope") or "all").strip().lower()
        return _team_context_search(query, scope=scope)
    if name == "qclaw_team_articles":
        return _run_team_articles_tool(args, ctx)
    if name == "qclaw_article_images":
        action = str(args.get("action") or "list").strip().lower()
        if action == "generate":
            return _run_image_tool_with_optional_background(
                args,
                progress_job_id=progress_job_id,
                session_key=session_key,
                filename=str(args.get("figure") or "figure"),
                persistence=persistence,
                run_fn=lambda: _run_article_images_tool(args),
            )
        return _run_article_images_tool(args)
    if name == "qclaw_team_article_pdf":
        return _run_team_article_pdf_tool(args, ctx)
    if name == "qclaw_index_team_articles":
        return _run_index_team_articles_tool(args, ctx)
    if name == "qclaw_remove_team_articles":
        return _run_remove_team_articles_tool(args, ctx)
    if name == "qclaw_create_hermes_agent":
        if ctx.hermes_agent_create is None:
            return {
                "ok": False,
                "error": "criação de agentes Hermes desabilitada no config",
            }
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        payload: dict[str, Any] = {"text": text}
        for key in ("mode", "schedule", "workdir", "team_id"):
            val = args.get(key)
            if val is not None and str(val).strip():
                payload[key] = str(val).strip()
        if args.get("schedule_enabled") is not None:
            payload["schedule_enabled"] = bool(args.get("schedule_enabled"))
        return ctx.hermes_agent_create(payload)
    if name == "qclaw_create_openai_swarm":
        if ctx.openai_swarm_create is None:
            return {
                "ok": False,
                "error": "criação de swarm desabilitada no config",
            }
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        swarm_payload: dict[str, Any] = {"text": text}
        for key in ("workdir", "schedule", "team_id", "preset_id", "name"):
            val = args.get(key)
            if val is not None and str(val).strip():
                swarm_payload[key] = str(val).strip()
        if not swarm_payload.get("team_id") and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    swarm_payload["team_id"] = str(sess_row.team_id or "").strip()
            except Exception:
                pass
        return ctx.openai_swarm_create(swarm_payload)
    if name == "qclaw_run_team_swarm":
        if ctx.team_swarm_run is None:
            return {
                "ok": False,
                "error": "execução de swarm por time desabilitada no config",
            }
        team_id = str(args.get("team_id") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = str(sess_row.team_id or "").strip()
            except Exception:
                pass
        if not team_id:
            return {"ok": False, "error": "team_id is required"}
        run_payload: dict[str, Any] = {}
        objective = str(args.get("objective") or args.get("text") or "").strip()
        if objective:
            run_payload["objective"] = objective
        _swarm_run_payload_extras(args, run_payload)
        run_payload["max_cards"] = _swarm_run_max_cards(args, default=1)
        from .chat_tool_background import should_background_chat_tool, spawn_chat_tool_background

        if should_background_chat_tool(
            args,
            progress_job_id=progress_job_id,
            session_key=str(session_key or "").strip(),
        ):
            team_label = team_id
            team_run_payload = dict(run_payload)
            team_run_payload.setdefault("team_id", team_id)

            return spawn_chat_tool_background(
                kind="swarm_run",
                session_key=str(session_key or "").strip(),
                message_text=f"[swarm_run] team={team_id}",
                label=f"Swarm do time {team_label}",
                run_fn=lambda job_id: ctx.team_swarm_run(
                    team_id,
                    {**run_payload, "progress_job_id": job_id},
                ),
                persistence=persistence,
                on_start=_make_swarm_run_on_start(
                    persistence=persistence,
                    session_key=str(session_key or "").strip(),
                    swarm_label=f"time {team_label}",
                    run_payload=team_run_payload,
                    preview_fn=ctx.swarm_graph_preview,
                ),
                format_success=lambda r: (
                    f"Swarm do time **{r.get('team_name') or team_label}** concluído — "
                    f"**{int(r.get('agents_run') or len(r.get('outputs') or {}))}** "
                    f"agente(s) executado(s)"
                    + (
                        f", **{len(r.get('selected_card_ids') or [])}** card(s)"
                        if isinstance(r.get("selected_card_ids"), list)
                        and len(r.get("selected_card_ids") or []) > 1
                        else (
                            f", card **{r.get('selected_card_id')}**"
                            if r.get("selected_card_id")
                            else ""
                        )
                    )
                    + "."
                ),
                format_failure=lambda r: (
                    f"⚠️ **Erro no swarm:** {str(r.get('error') or 'execução falhou')[:800]}"
                ),
            )
        return ctx.team_swarm_run(team_id, run_payload)
    if name == "qclaw_run_swarm":
        if ctx.swarm_graph_run is None:
            return {
                "ok": False,
                "error": "execução de swarm desabilitada no config",
            }
        swarm_name = str(args.get("swarm_name") or args.get("name") or "").strip()
        if not swarm_name:
            return {"ok": False, "error": "swarm_name is required"}
        team_id = str(args.get("team_id") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = str(sess_row.team_id or "").strip()
            except Exception:
                pass
        run_payload: dict[str, Any] = {
            "swarm_name": swarm_name,
            "run_all": True,
            "use_team_cards": bool(args.get("use_team_cards", True)),
            "conditional_handoff": False,
            "use_langgraph": False,
        }
        objective = str(args.get("objective") or args.get("text") or "").strip()
        if objective:
            run_payload["objective"] = objective
        if team_id:
            run_payload["team_id"] = team_id
        run_payload["max_cards"] = _swarm_run_max_cards(args, default=1)
        _swarm_run_payload_extras(args, run_payload)

        from .chat_tool_background import should_background_chat_tool, spawn_chat_tool_background

        if should_background_chat_tool(
            args,
            progress_job_id=progress_job_id,
            session_key=str(session_key or "").strip(),
        ):
            swarm_label = swarm_name

            return spawn_chat_tool_background(
                kind="swarm_run",
                session_key=str(session_key or "").strip(),
                message_text=f"[swarm_run] swarm={swarm_name}",
                label=f"Swarm {swarm_label}",
                run_fn=lambda job_id: ctx.swarm_graph_run(
                    {**run_payload, "progress_job_id": job_id},
                ),
                persistence=persistence,
                on_start=_make_swarm_run_on_start(
                    persistence=persistence,
                    session_key=str(session_key or "").strip(),
                    swarm_label=swarm_label,
                    run_payload=dict(run_payload),
                    preview_fn=ctx.swarm_graph_preview,
                ),
                format_success=lambda r: (
                    f"Swarm **{r.get('swarm_name') or swarm_label}** concluído — "
                    f"**{int(r.get('agents_run') or len(r.get('outputs') or {}))}** "
                    f"agente(s)"
                    + (
                        f", **{len(r.get('selected_card_ids') or [])}** card(s)"
                        if isinstance(r.get("selected_card_ids"), list)
                        and len(r.get("selected_card_ids") or []) > 1
                        else (
                            f", card **{r.get('selected_card_id')}**"
                            if r.get("selected_card_id")
                            else ""
                        )
                    )
                    + "."
                ),
                format_failure=lambda r: (
                    f"⚠️ **Erro no swarm:** {str(r.get('error') or 'execução falhou')[:800]}"
                ),
            )
        result = ctx.swarm_graph_run(run_payload)
        if isinstance(result, dict):
            team_ctx = result.get("team_ctx") if isinstance(result.get("team_ctx"), dict) else {}
            if team_ctx.get("selected_card_id"):
                result.setdefault("selected_card_id", team_ctx["selected_card_id"])
            if team_ctx.get("selected_card_ids"):
                result.setdefault("selected_card_ids", team_ctx["selected_card_ids"])
        return result
    if name == "qclaw_create_kanban_card":
        if ctx.kanban_create_card is None:
            return {
                "ok": False,
                "error": "Kanban indisponível no contexto atual",
            }
        title = str(args.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}
        task: dict[str, Any] = {"title": title}
        description = str(args.get("description") or "").strip()
        if description:
            task["description"] = description
        column = str(args.get("column") or "").strip().lower()
        if column:
            task["column"] = column
        if args.get("priority") is not None and str(args.get("priority")).strip():
            try:
                task["priority"] = int(args.get("priority"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "priority must be an integer"}

        raw_assignees = args.get("assignees")
        if isinstance(raw_assignees, list):
            task["assignees"] = [
                str(s).strip() for s in raw_assignees if str(s).strip()
            ]

        raw_skills = args.get("skills")
        if isinstance(raw_skills, list):
            task["skills"] = [str(s).strip() for s in raw_skills if str(s).strip()]

        impl_location = str(
            args.get("implementation_location") or args.get("app_location") or ""
        ).strip()
        if impl_location:
            task["implementation_location"] = impl_location

        task_id = str(
            args.get("task_id") or args.get("card_id") or args.get("id") or ""
        ).strip()
        if task_id:
            task["id"] = task_id

        payload = {"task": task}
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        # Fallback: resolve team_id from active session if LLM did not provide it.
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if team_id:
            try:
                from .storage import get_account_store

                store = get_account_store()
                actor = store.ensure_local_user()
                team_id = store.get_team(team_id, actor.id).id
            except Exception:
                try:
                    from .accounts_team_guard import resolve_team_identifier
                    from .storage import get_account_store

                    store = get_account_store()
                    team_id = resolve_team_identifier(
                        store.data_dir, team_id, store=store
                    )
                except Exception:
                    pass
        if not team_id:
            return {
                "ok": False,
                "error": (
                    "team_id é obrigatório — selecione o time Artigo Haron na sessão "
                    "ou passe team_id (ex.: artigo-haron / dc57de4fe46b)."
                ),
            }
        payload["team_id"] = team_id
        if workdir:
            payload["workdir"] = workdir
        return ctx.kanban_create_card(payload)
    if name == "qclaw_create_card_with_article":
        if ctx.kanban_create_card is None or ctx.kanban_attachment_upload is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        title = str(args.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}
        raw_paths = args.get("article_paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        article_paths = [str(p).strip() for p in (raw_paths or []) if str(p).strip()]
        if not article_paths:
            return {"ok": False, "error": "article_paths is required (ao menos um ficheiro do artigo)"}
        # Validate files exist before creating the card.
        missing = [p for p in article_paths if not Path(p).expanduser().is_file()]
        if missing:
            return {"ok": False, "error": f"Arquivo(s) não encontrado(s): {', '.join(missing)}"}

        task: dict[str, Any] = {"title": title}
        description = str(args.get("description") or "").strip()
        if description:
            task["description"] = description
        column = str(args.get("column") or "").strip().lower()
        if column:
            task["column"] = column
        if args.get("priority") is not None and str(args.get("priority")).strip():
            try:
                task["priority"] = int(args.get("priority"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "priority must be an integer"}
        raw_assignees = args.get("assignees")
        if isinstance(raw_assignees, list):
            task["assignees"] = [str(s).strip() for s in raw_assignees if str(s).strip()]
        raw_skills = args.get("skills")
        if isinstance(raw_skills, list):
            task["skills"] = [str(s).strip() for s in raw_skills if str(s).strip()]

        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        create_payload: dict[str, Any] = {"task": task}
        if team_id:
            create_payload["team_id"] = team_id
        if workdir:
            create_payload["workdir"] = workdir
        created = ctx.kanban_create_card(create_payload)
        if not created.get("ok") or not created.get("task_id"):
            return {"ok": False, "stage": "create_card", "response": created}

        new_task_id = str(created.get("task_id"))
        resolved_team_id = str(created.get("team_id") or team_id or "").strip()

        import base64 as _b64
        import mimetypes as _mt

        attach_results: list[dict[str, Any]] = []
        for raw_path in article_paths:
            fp = Path(raw_path).expanduser()
            file_name = fp.name
            mime_type = _mt.guess_type(file_name)[0] or "application/octet-stream"
            upload_payload: dict[str, Any] = {
                "task_id": new_task_id,
                "file_name": file_name,
                "name": file_name,
                "mime_type": mime_type,
                "file_path": str(fp),
            }
            try:
                upload_payload["data_base64"] = _b64.b64encode(fp.read_bytes()).decode("ascii")
            except OSError as e:
                attach_results.append({"ok": False, "file": str(fp), "error": f"Erro ao ler arquivo: {e}"})
                continue
            if resolved_team_id:
                upload_payload["team_id"] = resolved_team_id
            if workdir:
                upload_payload["workdir"] = workdir
            res = ctx.kanban_attachment_upload(upload_payload)
            res["file"] = str(fp)
            attach_results.append(res)

        ok_count = sum(1 for r in attach_results if r.get("ok"))
        fail_count = len(attach_results) - ok_count
        return {
            "ok": fail_count == 0,
            "task_id": new_task_id,
            "team_id": resolved_team_id,
            "title": title,
            "attached": ok_count,
            "failed": fail_count,
            "attachments": [r.get("name") for r in attach_results if r.get("ok")],
            "results": attach_results,
        }
    if name == "qclaw_kanban_list_attachments":
        if ctx.team_kanban_get is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        team_id = str(args.get("team_id") or "").strip()
        task_id = str(args.get("task_id") or "").strip()
        result = ctx.team_kanban_get(team_id)
        if result.get("ok") is not False:
            # handle_chat_team_kanban returns tasks at top level (single team)
            # or {"boards": [...]} for all teams
            boards = []
            if "tasks" in result:
                boards = [(team_id or "?", result)]
            elif "boards" in result:
                for entry in result.get("boards") or []:
                    if isinstance(entry, dict) and "tasks" in entry:
                        boards.append((entry.get("team_id", "?"), entry))
            attachments: list[dict] = []
            for tid, board in boards:
                for task in board.get("tasks") or []:
                    if task_id and task.get("id") != task_id:
                        continue
                    for att in task.get("attachments") or []:
                        attachments.append({
                            "task_id": task.get("id"),
                            "task_title": task.get("title", ""),
                            "file_name": att.get("name") or att.get("file_name", ""),
                            "safe_name": att.get("safe_name", ""),
                            "mime_type": att.get("mime_type", ""),
                            "size": att.get("size", 0),
                            "stored_path": att.get("stored_path", ""),
                            "uploaded_at": att.get("uploaded_at", ""),
                        })
            return {"ok": True, "attachments": attachments, "count": len(attachments)}
        return result
    if name == "qclaw_kanban_attach_upload":
        if ctx.kanban_attachment_upload is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        file_name = str(args.get("file_name") or args.get("name") or "").strip()
        file_path = str(args.get("file_path") or "").strip()
        if not file_name and file_path:
            file_name = Path(file_path).name
        if not file_name:
            return {"ok": False, "error": "file_name is required"}
        data_b64 = str(args.get("data_base64") or "").strip()
        if file_path and not data_b64:
            fp = Path(file_path).expanduser()
            if not fp.is_file():
                return {"ok": False, "error": f"Arquivo não encontrado: {file_path}"}
            try:
                import base64 as _b64

                data_b64 = _b64.b64encode(fp.read_bytes()).decode("ascii")
            except OSError as e:
                return {"ok": False, "error": f"Erro ao ler arquivo: {e}"}
        mime_type = str(args.get("mime_type") or "").strip()
        if not mime_type:
            import mimetypes as _mt

            mime_type = _mt.guess_type(file_name)[0] or "application/octet-stream"
        upload_payload: dict[str, Any] = {
            "task_id": task_id,
            "file_name": file_name,
            "name": file_name,
            "mime_type": mime_type,
        }
        if data_b64:
            upload_payload["data_base64"] = data_b64
        if file_path:
            upload_payload["file_path"] = file_path
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if team_id:
            upload_payload["team_id"] = team_id
        if workdir:
            upload_payload["workdir"] = workdir
        if not team_id and not workdir:
            return {
                "ok": False,
                "error": "team_id ou workdir é necessário para localizar o kanban",
            }
        if not data_b64 and not file_path:
            return {"ok": False, "error": "data_base64 ou file_path é obrigatório"}
        return ctx.kanban_attachment_upload(upload_payload)
    if name == "qclaw_kanban_attach_move":
        if ctx.kanban_attachment_move is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        source_task_id = str(args.get("source_task_id") or "").strip()
        dest_task_id = str(args.get("dest_task_id") or "").strip()
        safe_name = str(args.get("safe_name") or "").strip()
        if not source_task_id or not dest_task_id or not safe_name:
            return {"ok": False, "error": "source_task_id, dest_task_id e safe_name são obrigatórios"}
        payload: dict[str, Any] = {
            "source_task_id": source_task_id,
            "dest_task_id": dest_task_id,
            "safe_name": safe_name,
        }
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if team_id:
            payload["team_id"] = team_id
        if workdir:
            payload["workdir"] = workdir
        if not team_id and not workdir:
            return {
                "ok": False,
                "error": "team_id ou workdir é necessário para localizar o kanban",
            }
        return ctx.kanban_attachment_move(payload)
    if name == "qclaw_kanban_attach_copy":
        if ctx.kanban_attachment_copy is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        source_task_id = str(args.get("source_task_id") or "").strip()
        dest_task_id = str(args.get("dest_task_id") or "").strip()
        safe_name = str(args.get("safe_name") or "").strip()
        if not source_task_id or not dest_task_id or not safe_name:
            return {"ok": False, "error": "source_task_id, dest_task_id e safe_name são obrigatórios"}
        payload = {
            "source_task_id": source_task_id,
            "dest_task_id": dest_task_id,
            "safe_name": safe_name,
        }
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if team_id:
            payload["team_id"] = team_id
        if workdir:
            payload["workdir"] = workdir
        if not team_id and not workdir:
            return {
                "ok": False,
                "error": "team_id ou workdir é necessário para localizar o kanban",
            }
        return ctx.kanban_attachment_copy(payload)
    if name == "qclaw_kanban_attach_project_zip":
        if ctx.kanban_attachment_upload is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if not team_id and not workdir:
            return {
                "ok": False,
                "error": "team_id ou workdir é necessário para localizar o projeto/kanban",
            }
        from .kanban_project_zip import _parse_max_bytes, zip_project_and_attach

        return zip_project_and_attach(
            task_id=task_id,
            team_id=team_id,
            team_name=str(args.get("team_name") or "").strip(),
            workdir=workdir,
            path=str(args.get("path") or args.get("project_path") or "").strip(),
            subdir=str(args.get("subdir") or args.get("relative_path") or "").strip(),
            zip_name=str(args.get("zip_name") or args.get("name") or "").strip(),
            include_hidden=bool(args.get("include_hidden")),
            max_bytes=_parse_max_bytes(args.get("max_bytes")),
            upload_fn=ctx.kanban_attachment_upload,
        )
    if name == "qclaw_kanban_attach_latex_zip":
        if ctx.kanban_attachment_upload is None:
            return {"ok": False, "error": "Kanban indisponível no contexto atual"}
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return {"ok": False, "error": "task_id is required"}
        team_id = str(args.get("team_id") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        if not team_id and persistence is not None and session_key:
            try:
                sess_row = persistence.history.get_session_by_key(session_key)
                if sess_row is not None and getattr(sess_row, "team_id", None):
                    team_id = sess_row.team_id
            except Exception:
                pass
        if not team_id and not workdir and not str(args.get("path") or "").strip():
            return {
                "ok": False,
                "error": "team_id, workdir ou path é necessário para localizar o artigo",
            }
        from .kanban_latex_zip import zip_latex_and_attach
        from .kanban_project_zip import _parse_max_bytes as _zip_parse_max_bytes

        include_pdf = args.get("include_pdf", True)
        if isinstance(include_pdf, str):
            include_pdf = include_pdf.lower() not in {"0", "false", "no"}
        include_styles = args.get("include_styles", True)
        if isinstance(include_styles, str):
            include_styles = include_styles.lower() not in {"0", "false", "no"}

        return zip_latex_and_attach(
            task_id=task_id,
            team_id=team_id,
            team_name=str(args.get("team_name") or "").strip(),
            workdir=workdir,
            path=str(args.get("path") or args.get("project_path") or "").strip(),
            subdir=str(args.get("subdir") or args.get("relative_path") or "").strip(),
            article=str(args.get("article") or args.get("article_id") or args.get("tex") or "").strip(),
            zip_name=str(args.get("zip_name") or args.get("name") or "").strip(),
            include_pdf=bool(include_pdf),
            include_styles=bool(include_styles),
            max_bytes=_zip_parse_max_bytes(args.get("max_bytes")),
            upload_fn=ctx.kanban_attachment_upload,
        )
    if name == "qclaw_create_team":
        if ctx.team_create is None:
            return {
                "ok": False,
                "error": "criação de times indisponível no contexto atual",
            }
        team_name = str(args.get("name") or "").strip()
        if not team_name:
            return {"ok": False, "error": "name is required"}
        payload: dict[str, Any] = {"name": team_name}
        for key in (
            "id",
            "description",
            "project_source",
            "github_url",
            "github_branch",
            "local_path",
            "app_url",
            "preset_id",
        ):
            val = args.get(key)
            if val is not None and str(val).strip():
                payload[key] = str(val).strip()
        raw_site_links = args.get("site_links")
        if isinstance(raw_site_links, list):
            payload["site_links"] = raw_site_links
        raw_profiles = args.get("profile_slugs")
        if isinstance(raw_profiles, list):
            payload["profile_slugs"] = [
                str(s).strip() for s in raw_profiles if str(s).strip()
            ]
        if args.get("seed_kanban") is not None:
            payload["seed_kanban"] = bool(args.get("seed_kanban"))
        else:
            payload["seed_kanban"] = True
        result = ctx.team_create(payload)
        if isinstance(result, dict) and result.get("ok"):
            if not result.get("persisted_in_mongo"):
                team = result.get("team") if isinstance(result.get("team"), dict) else {}
                tid = str(team.get("id") or "").strip() or "?"
                return {
                    "ok": False,
                    "error": (
                        f"time '{tid}' não persistiu no MongoDB — "
                        "tente novamente ou verifique o serviço MongoDB"
                    ),
                    "team": team,
                }
        return result
    if name.strip().lower() in _SHELL_TOOL_NAMES:
        if not chat_cfg.shell_enabled:
            return {"ok": False, "error": "shell execution is disabled in openclaw_chat"}
        command = str(
            args.get("command") or args.get("cmd") or args.get("script") or ""
        ).strip()
        blocked = _check_manual_team_creation_shell(command)
        if blocked is not None:
            log.warning(
                "team shell guard blocked chat tool: %s",
                blocked.get("blocked_command"),
            )
            return blocked
        blocked = _check_whatsapp_guard(command, ctx.whatsapp_recipient)
        if blocked is not None:
            log.warning(
                "whatsapp guard blocked chat tool: detected=%s allowed=%s",
                blocked.get("detected_recipients"),
                blocked.get("allowed_recipient"),
            )
            return blocked
        if ctx.whatsapp_send is not None:
            from .whatsapp_allowlist import (
                check_whatsapp_recipient,
                load_allowed_recipient_digits,
            )
            from .whatsapp_allowlist_resolve import (
                extract_whatsapp_destinations_from_command,
            )

            triggered, destinations = extract_whatsapp_destinations_from_command(
                command
            )
            allowed = load_allowed_recipient_digits()
            allowed_norm = _normalize_whatsapp_recipient(ctx.whatsapp_recipient)
            if not allowed and allowed_norm:
                allowed = frozenset({allowed_norm})
            all_allowed = (
                triggered
                and allowed
                and destinations
                and all(
                    check_whatsapp_recipient(dest, allowed_digits=allowed) is None
                    for dest in destinations
                )
            )
            if all_allowed:
                shell_msg = _extract_whatsapp_message_from_command(command)
                if shell_msg:
                    out = ctx.whatsapp_send(shell_msg, False)
                    out["via"] = "shell_redirect"
                    out["hint"] = (
                        "Comando wacli redirecionado para fila assíncrona — "
                        "use qclaw_send_whatsapp da próxima vez."
                    )
                    return out
        redirected = _try_redirect_wacli_probe_shell(command, ctx)
        if redirected is not None:
            log.info("wacli shell probe redirected: %s", command[:120])
            return redirected
        cwd = args.get("cwd") or chat_cfg.shell_working_dir
        return _run_shell_command(
            command,
            cwd=str(cwd).strip() if cwd else None,
            timeout=chat_cfg.shell_timeout_seconds,
            max_chars=chat_cfg.shell_max_output_chars,
        )
    if name.startswith("qclaw_tool_learning_"):
        return _run_tool_learning_tool(name, args, chat_cfg=chat_cfg)
    if name == "qclaw_chat_generation_status":
        from .chat_generation_status import dispatch_chat_generation_status

        return dispatch_chat_generation_status(args)
    if name == "qclaw_slides_batch_artifacts":
        from .slides_batch_artifacts import dispatch_slides_batch_artifacts

        return dispatch_slides_batch_artifacts(args)
    if name == "qclaw_chat_failures":
        from .chat_failures import collect_chat_failures

        try:
            since_minutes = float(args.get("since_minutes") or 120.0)
        except (TypeError, ValueError):
            since_minutes = 120.0
        try:
            limit = int(args.get("limit") or 40)
        except (TypeError, ValueError):
            limit = 40
        query = str(args.get("query") or "").strip()
        sk = str(args.get("session_key") or session_key or "").strip()
        raw_sources = args.get("sources")
        sources: list[str] | None = None
        if isinstance(raw_sources, list):
            sources = [str(s).strip() for s in raw_sources if str(s).strip()]
        history_path = None
        if chat_cfg.history_enabled:
            from .local_paths import project_stack_root

            history_path = Path(chat_cfg.history_db_path).expanduser()
            if not history_path.is_absolute():
                history_path = (project_stack_root().parent / history_path).resolve()
        return collect_chat_failures(
            since_minutes=since_minutes,
            limit=limit,
            query=query,
            session_key=sk,
            sources=sources,
            history_path=history_path,
        )
    if name.startswith("qclaw_ai_kb_"):
        return _run_ai_kb_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_ruflo_swarm_"):
        return _run_ruflo_swarm_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_swarm_"):
        return _run_swarm_manage_tool(name, args, ctx=ctx)
    if name == "qclaw_token_mode":
        return _run_token_mode_tool(args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_jobs_"):
        return _run_jobs_manage_tool(name, args, ctx=ctx)
    if name == "qclaw_monitor_update":
        return _run_monitor_update_tool(args)
    if name.startswith("qclaw_app_passwords_"):
        return _run_app_passwords_tool(name, args)
    if name.startswith("qclaw_google_oauth_"):
        return _run_google_oauth_tool(name, args)
    if name == "qclaw_team_files_search":
        return _run_team_files_search_tool(args)
    if name == "qclaw_team_files_download":
        return _run_team_files_download_tool(args)
    if name == "qclaw_team_files_send":
        return _run_team_files_send_tool(
            args,
            default_whatsapp_recipient=ctx.whatsapp_recipient,
        )
    if name.startswith("qclaw_team_rule_") or name == "qclaw_team_rules_summary":
        return _run_team_rules_tool(name, args)
    if name.startswith("qclaw_team_fact_") or name in (
        "qclaw_team_facts_summary",
        "qclaw_team_facts_for_roteiro",
    ):
        return _run_team_facts_tool(name, args)
    if name.startswith("qclaw_email_team_pdf_"):
        enriched = dict(args)
        if not str(enriched.get("team_id") or "").strip() and not str(
            enriched.get("team_name") or ""
        ).strip():
            session_team = _resolve_session_team_id(persistence, session_key)
            if session_team:
                enriched["team_id"] = session_team
        return _run_email_team_pdf_tool(name, enriched)
    if name.startswith("qclaw_gmail_attachments_"):
        enriched = dict(args)
        if not str(enriched.get("output_dir") or "").strip():
            if not str(enriched.get("team_id") or "").strip() and not str(
                enriched.get("team_name") or ""
            ).strip():
                session_team = _resolve_session_team_id(persistence, session_key)
                if session_team:
                    enriched["team_id"] = session_team
        return _run_gmail_attachments_tool(name, enriched)
    if name.startswith("qclaw_trello_"):
        return _run_trello_tool(name, args)
    if name.startswith("qclaw_legal_"):
        return _run_legal_evaluation_tool(name, args)
    if name.startswith("qclaw_journal_rules_"):
        return _run_journal_rules_tool(name, args)
    if name.startswith("qclaw_budget_"):
        return _run_budget_tool(name, args)
    if name.startswith("qclaw_team_payment"):
        return _run_team_payments_tool(name, args)
    if name.startswith("qclaw_aws_"):
        return _run_aws_manage_tool(name, args)
    if name == "qclaw_evaluate_agents":
        return _run_agent_evaluate_tool(args, chat_cfg=chat_cfg)
    if name == "qclaw_fix_agents":
        return _run_agent_fix_tool(args, chat_cfg=chat_cfg, ctx=ctx)
    if (
        name.startswith("qclaw_aula_")
        or name.startswith("qclaw_nota_")
        or name.startswith("qclaw_study_skill_")
        or name == "qclaw_aulas_memoria_summary"
        or name == "qclaw_aulas_memoria_show"
    ):
        return _run_aulas_memoria_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_memclaw_memoria_"):
        return _run_memclaw_memoria_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_chat_personality_"):
        return _run_chat_personality_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_visual_memory_") or name == "qclaw_profile_from_photo":
        return _run_visual_memory_tool(name, args)
    if name.startswith("qclaw_character_"):
        return _run_character_tool(name, args)
    if name.startswith("qclaw_thumbnail_style_"):
        return _run_thumbnail_style_tool(name, args)
    if name.startswith("qclaw_user_data_"):
        return _run_user_personal_data_tool(name, args)
    if name.startswith("qclaw_identidade_"):
        return _run_identidade_civil_tool(name, args)
    if name.startswith("qclaw_curriculum_"):
        return _run_curriculum_tool(name, args)
    if name.startswith("qclaw_minicurriculum_"):
        return _run_minicurriculum_tool(name, args)
    if name.startswith("qclaw_email_memoria_"):
        return _run_email_memoria_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_whatsapp_memoria_"):
        return _run_whatsapp_memoria_tool(name, args, chat_cfg=chat_cfg)
    if name.startswith("qclaw_whatsapp_agenda_") or name in (
        "qclaw_whatsapp_messages_search",
        "qclaw_whatsapp_contacts_search",
        "qclaw_whatsapp_media_download",
    ):
        return _run_whatsapp_busca_tool(name, args)
    if name.startswith("qclaw_gmail_"):
        return _run_gmail_tool(name, args)
    if name.startswith("qclaw_notion_"):
        return _run_notion_tool(name, args)
    if name.startswith("qclaw_overleaf_template_"):
        return _run_overleaf_template_tool(name, args)
    if name.startswith("qclaw_teams_calendar_"):
        return _run_teams_calendar_tool(name, args)
    if name.startswith("qclaw_calendar_"):
        return _run_calendar_tool(name, args)
    if name.startswith("qclaw_google_photos_"):
        return _run_google_photos_tool(name, args)
    if name.startswith("qclaw_local_photos_"):
        return _run_local_photos_tool(name, args)
    if name.startswith("qclaw_skills_"):
        from .skills_mcp import dispatch_chat_tool

        return dispatch_chat_tool(name, args)
    if name.startswith("qclaw_mcp__"):
        from .mcp_external_chat import dispatch_external_mcp_tool

        return dispatch_external_mcp_tool(name, args)
    if name.startswith("qclaw_virtual_band_"):
        from .virtual_band import dispatch_chat_tool as dispatch_vb_tool

        return dispatch_vb_tool(name, args)
    if name.startswith("qclaw_rv_mongo_"):
        from .roteiro_mongo import dispatch_chat_tool as dispatch_rv_mongo_tool

        return dispatch_rv_mongo_tool(name, args)
    if name.startswith("qclaw_rv_scripts_"):
        from .roteiro_scripts_mongo import dispatch_chat_tool as dispatch_rv_scripts_read_tool
        from .roteiro_scripts_write import dispatch_chat_tool as dispatch_rv_scripts_write_tool

        write_names = {
            "qclaw_rv_scripts_insert",
            "qclaw_rv_scripts_update",
            "qclaw_rv_scripts_update_act",
            "qclaw_rv_scripts_insert_acts",
        }
        if name in write_names:
            return dispatch_rv_scripts_write_tool(name, args)
        return dispatch_rv_scripts_read_tool(name, args)
    if name.startswith("qclaw_rv_api_") or name in ("qclaw_rv_job_status", "qclaw_rv_jobs_list"):
        from .roteiro_api_ops import dispatch_chat_tool as dispatch_rv_api_tool

        return dispatch_rv_api_tool(name, args)
    if name == "qclaw_roteiro_book_cover_portrait":
        from .roteiro_book_cover import dispatch_chat_tool as dispatch_book_cover_tool

        return dispatch_book_cover_tool(name, args)

    if name.startswith("qclaw_book_cover_studio_"):
        from .book_cover_studio import dispatch_chat_tool as dispatch_cover_studio_tool

        return dispatch_cover_studio_tool(name, args)
    if name.startswith("qclaw_chat_widget_"):
        from .chat_creative_widgets import dispatch_show_widget

        return dispatch_show_widget(name, args)
    if name == "qclaw_rv_screens_list":
        from .chat_rv_screens import dispatch_list_rv_screens

        return dispatch_list_rv_screens(args)
    if name.startswith("qclaw_thumbnail_"):
        from .roteiro_thumbnails import dispatch_chat_tool as dispatch_thumb_tool

        return dispatch_thumb_tool(name, args)
    if name.startswith("qclaw_desktop_"):
        return _run_desktop_control_tool(name, args, chat_cfg=chat_cfg)
    if name in (
        "ask_qclaw_agent",
        "qclaw_heygen_via_openclaw",
        "qclaw_suno_via_openclaw",
        "qclaw_seedance_via_openclaw",
        "qclaw_runway_via_openclaw",
    ):
        if not bool(getattr(chat_cfg, "openclaw_connection_enabled", True)):
            return {
                "ok": False,
                "error": "A conexão com o OpenClaw está desligada (modo web).",
            }
        if name == "ask_qclaw_agent":
            question = str(args.get("question") or "").strip()
            if not question:
                return {"ok": False, "error": "question is required"}
        elif name == "qclaw_heygen_via_openclaw":
            from .heygen_mcp import build_heygen_openclaw_question

            task = str(args.get("task") or "").strip()
            if not task:
                return {"ok": False, "error": "task is required"}
            session_id = str(args.get("session_id") or "").strip() or None
            video_id = str(args.get("video_id") or "").strip() or None
            try:
                question = build_heygen_openclaw_question(
                    task,
                    session_id=session_id,
                    video_id=video_id,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        elif name == "qclaw_seedance_via_openclaw":
            from .seedance_mcp import build_seedance_openclaw_question

            task = str(args.get("task") or "").strip()
            if not task:
                return {"ok": False, "error": "task is required"}
            task_id = str(args.get("task_id") or "").strip() or None
            model = str(args.get("model") or "").strip() or None
            try:
                question = build_seedance_openclaw_question(
                    task,
                    task_id=task_id,
                    model=model,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        elif name == "qclaw_runway_via_openclaw":
            from .runway_mcp import build_runway_openclaw_question

            task = str(args.get("task") or "").strip()
            if not task:
                return {"ok": False, "error": "task is required"}
            asset_id = str(args.get("asset_id") or "").strip() or None
            try:
                question = build_runway_openclaw_question(task, asset_id=asset_id)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        else:
            from .suno_mcp import build_suno_openclaw_question

            task = str(args.get("task") or "").strip()
            if not task:
                return {"ok": False, "error": "task is required"}
            task_id = str(args.get("task_id") or "").strip() or None
            audio_id = str(args.get("audio_id") or "").strip() or None
            try:
                question = build_suno_openclaw_question(
                    task,
                    task_id=task_id,
                    audio_id=audio_id,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        target_agent = str(args.get("agent_id") or chat_cfg.default_agent or "main").strip()
        bin_path, _env = _openclaw_cli_env(runtime, ctx.doctor_cfg)
        if not bin_path:
            return {
                "ok": False,
                "error": "openclaw CLI not available (configure openclaw_doctor paths)",
            }
        from .openclaw_chat import send_message

        out = send_message(
            runtime,
            ctx.doctor_cfg,
            chat_cfg,
            session_key=_QCLAW_TOOL_SESSION,
            message=question,
            agent_id=target_agent or None,
        )
        if name == "qclaw_heygen_via_openclaw" and isinstance(out, dict):
            out = dict(out)
            out["via"] = "heygen_mcp_openclaw"
        if name == "qclaw_suno_via_openclaw" and isinstance(out, dict):
            out = dict(out)
            out["via"] = "suno_mcp_openclaw"
        if name == "qclaw_seedance_via_openclaw" and isinstance(out, dict):
            out = dict(out)
            out["via"] = "seedance_mcp_openclaw"
        if name == "qclaw_runway_via_openclaw" and isinstance(out, dict):
            out = dict(out)
            out["via"] = "runway_mcp_openclaw"
        return out
    if name == "qclaw_roteiro_scene_setup":
        from .roteiro_scene_ops import dispatch_roteiro_scene_setup

        return dispatch_roteiro_scene_setup(
            args,
            chat_cfg=chat_cfg,
            model_id=model_id,
        )
    from .mcp_chat_aliases import dispatch_chat_mcp_alias

    mcp_out = dispatch_chat_mcp_alias(name, args)
    if mcp_out is not None:
        return mcp_out
    return {"ok": False, "error": f"unknown tool: {name}"}

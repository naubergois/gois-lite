"""Individual tool runners invoked by the OpenClaw chat tool dispatcher."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .config import OpenclawChatConfig
from .openclaw_chat_tool_catalog import _desktop_control_enabled_for_host
from .openclaw_chat_whatsapp_groups import _GROUP_MEMORY, _load_group_memory, _wacli_group_numbers

log = logging.getLogger(__name__)

def _run_local_photos_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from . import local_photos

    if name == "qclaw_local_photos_recent":
        return local_photos.recent_photos(
            folder=str(args.get("folder") or ""),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_local_photos_search":
        return local_photos.search_photos(
            folder=str(args.get("folder") or ""),
            query=str(args.get("query") or ""),
            after=str(args.get("after") or ""),
            before=str(args.get("before") or ""),
            media_type=str(args.get("media_type") or ""),
            limit=int(args.get("limit") or 30),
        )
    if name == "qclaw_local_photos_get":
        path = str(args.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path é obrigatório"}
        return local_photos.get_photo(
            path,
            with_image=bool(args.get("with_image", True)),
            max_width=int(args.get("max_width") or 1200),
        )
    if name == "qclaw_local_photos_roots":
        return local_photos.list_roots()
    return {"ok": False, "error": f"tool desconhecida: {name}"}


def _run_gmail_tool(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute Gmail IMAP/SMTP tools using the bundled script."""
    script = Path(__file__).resolve().parents[2] / "skills" / "qclaw-gmail-imap" / "scripts" / "gmail_imap.py"
    if not script.is_file():
        return {"ok": False, "error": f"gmail script não encontrado: {script}"}

    import subprocess as _sp

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_gmail_send":
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        if not to or not subject or not body:
            return {"ok": False, "error": "to, subject e body são obrigatórios"}
        cmd = base_cmd + ["send", "--to", to, "--subject", subject, "--body", body]
        # Anexos: lista de caminhos absolutos
        attachments = args.get("attachments") or []
        if isinstance(attachments, str):
            attachments = [attachments]
        for att_path in attachments:
            att_path = str(att_path).strip()
            if att_path:
                cmd += ["--attachment", att_path]

    elif name == "qclaw_gmail_list":
        cmd = base_cmd + ["list"]
        limit = args.get("limit")
        if limit:
            cmd += ["--limit", str(int(limit))]
        if args.get("unread"):
            cmd += ["--unread"]
        from_filter = str(args.get("from_filter") or "").strip()
        subject_filter = str(args.get("subject_filter") or "").strip()
        if from_filter or subject_filter:
            cmd = base_cmd + ["search"]
            if from_filter:
                cmd += ["--from", from_filter]
            if subject_filter:
                cmd += ["--subject", subject_filter]
            if limit:
                cmd += ["--limit", str(int(limit))]

    elif name == "qclaw_gmail_read":
        uid = str(args.get("uid") or "").strip()
        if not uid:
            return {"ok": False, "error": "uid é obrigatório"}
        cmd = base_cmd + ["read", "--uid", uid]

    else:
        return {"ok": False, "error": f"tool desconhecida: {name}"}

    try:
        result = _sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            return {"ok": False, "error": output.strip()[:2000]}
        return {"ok": True, "output": output.strip()[:4000]}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "timeout ao acessar Gmail (30s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_calendar_tool(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute Google Calendar CRUD tools using calendar_db.py."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "calendar_db.py"
    if not script.is_file():
        return {"ok": False, "error": f"calendar_db.py não encontrado: {script}"}

    import subprocess as _sp

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_calendar_sync":
        days = str(args.get("days") or 30)
        cmd = base_cmd + ["sync", days]

    elif name == "qclaw_calendar_today":
        cmd = base_cmd + ["today"]

    elif name == "qclaw_calendar_week":
        cmd = base_cmd + ["week"]

    elif name == "qclaw_calendar_search":
        term = str(args.get("term") or "").strip()
        if not term:
            return {"ok": False, "error": "term é obrigatório"}
        cmd = base_cmd + ["search", term]

    elif name == "qclaw_calendar_create":
        summary = str(args.get("summary") or "").strip()
        start = str(args.get("start") or "").strip()
        end = str(args.get("end") or "").strip()
        if not summary or not start or not end:
            return {"ok": False, "error": "summary, start e end são obrigatórios"}
        cmd = base_cmd + ["create", summary, start, end]
        description = str(args.get("description") or "").strip()
        location = str(args.get("location") or "").strip()
        if description:
            cmd.append(description)
        if location:
            if not description:
                cmd.append("")  # placeholder for description
            cmd.append(location)

    elif name == "qclaw_calendar_meet_create":
        from .calendar_meet_ops import dispatch_calendar_meet_create

        return dispatch_calendar_meet_create(args)

    elif name == "qclaw_google_flights_search":
        from .google_flights_ops import dispatch_google_flights_search

        return dispatch_google_flights_search(args)

    elif name == "qclaw_flights_search":
        from .flights_search_ops import dispatch_flights_search

        return dispatch_flights_search(args)

    elif name == "qclaw_flights_providers_status":
        from .flights_search_ops import dispatch_flights_providers_status

        return dispatch_flights_providers_status(args)

    elif name == "qclaw_youtube_metadata_generate":
        from .youtube_metadata_ops import dispatch_youtube_metadata_generate

        return dispatch_youtube_metadata_generate(args)

    elif name == "qclaw_calendar_delete":
        event_id = str(args.get("event_id") or "").strip()
        if not event_id:
            return {"ok": False, "error": "event_id é obrigatório"}
        cmd = base_cmd + ["delete", event_id]

    elif name == "qclaw_calendar_update":
        event_id = str(args.get("event_id") or "").strip()
        if not event_id:
            return {"ok": False, "error": "event_id é obrigatório"}
        # Find first non-empty field to update
        for field in ("summary", "start", "end", "description", "location"):
            val = str(args.get(field) or "").strip()
            if val:
                cmd = base_cmd + ["update", event_id, field, val]
                break
        else:
            return {"ok": False, "error": "nenhum campo para atualizar fornecido"}

    elif name == "qclaw_calendar_status":
        cmd = base_cmd + ["status"]

    elif name == "qclaw_calendar_notion_sync":
        days = str(args.get("days") or 30)
        notion_script = Path(__file__).resolve().parents[2] / "scripts" / "notion_calendar_sync.py"
        if not notion_script.is_file():
            return {"ok": False, "error": f"notion_calendar_sync.py não encontrado: {notion_script}"}
        cmd = [sys.executable, str(notion_script), "full", days]

    elif name == "qclaw_calendar_notion_status":
        notion_script = Path(__file__).resolve().parents[2] / "scripts" / "notion_calendar_sync.py"
        if not notion_script.is_file():
            return {"ok": False, "error": f"notion_calendar_sync.py não encontrado: {notion_script}"}
        cmd = [sys.executable, str(notion_script), "status"]

    elif name == "qclaw_calendar_notion_configure":
        db_id = str(args.get("database_id") or "").strip()
        if not db_id:
            return {"ok": False, "error": "database_id é obrigatório"}
        notion_script = Path(__file__).resolve().parents[2] / "scripts" / "notion_calendar_sync.py"
        if not notion_script.is_file():
            return {"ok": False, "error": f"notion_calendar_sync.py não encontrado: {notion_script}"}
        cmd = [sys.executable, str(notion_script), "configure", db_id]

    else:
        return {"ok": False, "error": f"tool desconhecida: {name}"}

    try:
        result = _sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            return {"ok": False, "error": output.strip()[:2000]}
        return {"ok": True, "output": output.strip()[:4000]}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "timeout ao acessar Google Calendar (60s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_teams_calendar_tool(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute Microsoft Teams/Outlook calendar tools using teams_calendar_db.py."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "teams_calendar_db.py"
    if not script.is_file():
        return {"ok": False, "error": f"teams_calendar_db.py não encontrado: {script}"}

    import subprocess as _sp

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_teams_calendar_sync":
        days = str(args.get("days") or 30)
        cmd = base_cmd + ["sync", days]
    elif name == "qclaw_teams_calendar_today":
        cmd = base_cmd + ["today"]
    elif name == "qclaw_teams_calendar_week":
        cmd = base_cmd + ["week"]
    elif name == "qclaw_teams_calendar_search":
        term = str(args.get("term") or "").strip()
        if not term:
            return {"ok": False, "error": "term é obrigatório"}
        cmd = base_cmd + ["search", term]
    elif name == "qclaw_teams_calendar_status":
        cmd = base_cmd + ["status"]
    else:
        return {"ok": False, "error": f"tool desconhecida: {name}"}

    try:
        result = _sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            return {"ok": False, "error": output.strip()[:2000]}
        return {"ok": True, "output": output.strip()[:4000]}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "timeout ao acessar calendário Teams (60s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_notion_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    script = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-chat-notion"
        / "scripts"
        / "notion.py"
    )
    if not script.is_file():
        return {"ok": False, "error": f"script não encontrado: {script}"}

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_notion_status":
        cmd = base_cmd + ["status"]
    elif name == "qclaw_notion_configure":
        cmd = base_cmd + ["configure"]
        api_key = str(args.get("api_key") or "").strip()
        if api_key:
            cmd += ["--api-key", api_key]
        tasks_db = str(args.get("tasks_database_id") or "").strip()
        if tasks_db:
            cmd += ["--tasks-database-id", tasks_db]
        cal_db = str(args.get("calendar_database_id") or "").strip()
        if cal_db:
            cmd += ["--calendar-database-id", cal_db]
    elif name == "qclaw_notion_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query é obrigatório"}
        cmd = base_cmd + ["search", query]
        filter_type = str(args.get("filter_type") or "").strip()
        if filter_type:
            cmd += ["--filter-type", filter_type]
        limit = args.get("limit")
        if limit is not None:
            cmd += ["--limit", str(limit)]
    elif name == "qclaw_notion_query":
        db_id = str(args.get("database_id") or "").strip()
        cmd = base_cmd + ["query"]
        if db_id:
            cmd.append(db_id)
        filt = str(args.get("filter") or "").strip()
        if filt:
            cmd += ["--filter", filt]
        sorts = str(args.get("sorts") or "").strip()
        if sorts:
            cmd += ["--sorts", sorts]
        limit = args.get("limit")
        if limit is not None:
            cmd += ["--limit", str(limit)]
    elif name == "qclaw_notion_get_page":
        page_id = str(args.get("page_id") or "").strip()
        if not page_id:
            return {"ok": False, "error": "page_id é obrigatório"}
        cmd = base_cmd + ["get-page", page_id]
    elif name == "qclaw_notion_get_database":
        db_id = str(args.get("database_id") or "").strip()
        if not db_id:
            return {"ok": False, "error": "database_id é obrigatório"}
        cmd = base_cmd + ["get-database", db_id]
    elif name == "qclaw_notion_create_row":
        props = args.get("properties")
        if not isinstance(props, dict) or not props:
            return {"ok": False, "error": "properties é obrigatório (dict)"}
        db_id = str(args.get("database_id") or "").strip()
        cmd = base_cmd + ["create-row"]
        if db_id:
            cmd.append(db_id)
        cmd += ["--properties", json.dumps(props, ensure_ascii=False)]
    elif name == "qclaw_notion_create_page":
        cmd = base_cmd + ["create-page"]
        parent = str(args.get("parent_page_id") or "").strip()
        db_id = str(args.get("database_id") or "").strip()
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "").strip()
        if parent:
            cmd += ["--parent-page-id", parent]
        if db_id:
            cmd += ["--database-id", db_id]
        if title:
            cmd += ["--title", title]
        if content:
            cmd += ["--content", content]
    elif name == "qclaw_notion_update_page":
        page_id = str(args.get("page_id") or "").strip()
        props = args.get("properties")
        if not page_id:
            return {"ok": False, "error": "page_id é obrigatório"}
        if not isinstance(props, dict) or not props:
            return {"ok": False, "error": "properties é obrigatório (dict)"}
        cmd = base_cmd + ["update-page", page_id, "--properties", json.dumps(props, ensure_ascii=False)]
    else:
        return {"ok": False, "error": f"operação desconhecida: {name}"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        stdout = result.stdout.strip()
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or stdout or f"exit code {result.returncode}"}
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"ok": True, "output": stdout}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout ao executar notion (60s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_OVERLEAF_TEMPLATE_DISPATCH: dict[str, str] = {
    "qclaw_overleaf_template_sync": "sync",
    "qclaw_overleaf_template_preview": "preview",
    "qclaw_overleaf_template_discover": "discover",
    "qclaw_overleaf_template_fetch": "fetch",
    "qclaw_overleaf_template_catalog_search": "catalog_search",
    "qclaw_overleaf_template_catalog_get": "catalog_get",
    "qclaw_overleaf_template_catalog_sync": "catalog_sync",
    "qclaw_overleaf_template_pipeline": "pipeline",
}


def _run_overleaf_template_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    dispatch_action = _OVERLEAF_TEMPLATE_DISPATCH.get(name)
    if dispatch_action:
        from gois.overleaf_templates_ops import dispatch_overleaf_template_action

        payload = dict(args or {})
        if name == "qclaw_overleaf_template_pipeline":
            wid = str(payload.get("workspace_id") or "").strip()
            target = str(payload.get("target") or "").strip()
            if wid and not target:
                from gois.latex_articles import _resolve_workspace_root

                root, err = _resolve_workspace_root(wid)
                if root is None:
                    return {"ok": False, "error": err or "workspace inválido"}
                aid = str(payload.get("article_id") or "").strip()
                if aid:
                    from gois.latex_articles import _resolve_article_tex

                    _, tex, tex_err = _resolve_article_tex(wid, aid)
                    if tex is None:
                        return {"ok": False, "error": tex_err or "artigo inválido"}
                    payload["target"] = str(tex.parent)
                else:
                    payload["target"] = str(root)
        return dispatch_overleaf_template_action(dispatch_action, payload)

    script = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-chat-overleaf-template"
        / "scripts"
        / "overleaf_template.py"
    )
    if not script.is_file():
        return {"ok": False, "error": f"script não encontrado: {script}"}

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_overleaf_template_list":
        cmd = base_cmd + ["list"]
        tag = str(args.get("tag") or "").strip()
        if tag:
            cmd += ["--tag", tag]
        query = str(args.get("query") or "").strip()
        if query:
            cmd += ["--query", query]
    elif name == "qclaw_overleaf_template_get":
        template_id = str(args.get("template_id") or "").strip()
        if not template_id:
            return {"ok": False, "error": "template_id é obrigatório"}
        cmd = base_cmd + ["get", template_id]
    elif name == "qclaw_overleaf_template_extract":
        zip_path = str(args.get("zip_path") or args.get("zip") or "").strip()
        folder = str(args.get("folder") or "").strip()
        slug = str(args.get("name") or "").strip()
        if zip_path:
            from gois.latex_templates_api import upload_template_from_path

            return upload_template_from_path(zip_path=zip_path, name=slug)
        if not folder or not slug:
            return {"ok": False, "error": "folder+name ou zip_path são obrigatórios"}
        cmd = base_cmd + ["extract", folder, "--name", slug]
    elif name == "qclaw_overleaf_template_apply":
        from gois.chat_template_apply import dispatch_template_apply_tool

        return dispatch_template_apply_tool(args)
    else:
        return {"ok": False, "error": f"operação desconhecida: {name}"}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stdout = result.stdout.strip()
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or stdout or f"exit code {result.returncode}"}
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"ok": True, "output": stdout}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout ao executar overleaf template (120s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_google_photos_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute Google Photos tools using google_photos_db.py."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "google_photos_db.py"
    if not script.is_file():
        return {"ok": False, "error": f"google_photos_db.py não encontrado: {script}"}

    import subprocess as _sp

    base_cmd = [sys.executable, str(script), "--json"]

    timeout_s = 90
    if name in ("qclaw_google_photos_sync", "qclaw_google_photos_picker_start"):
        max_items = str(int(args.get("max_items") or args.get("limit") or 50))
        cmd = base_cmd + ["picker", "start", "--max", max_items]

    elif name == "qclaw_google_photos_picker_poll":
        cmd = base_cmd + ["picker", "poll"]
        sid = str(args.get("session_id") or "").strip()
        if sid:
            cmd.append(sid)
        cmd.extend(["--timeout", str(int(args.get("timeout") or 120))])
        timeout_s = int(args.get("timeout") or 120) + 15

    elif name == "qclaw_google_photos_picker_list":
        cmd = base_cmd + ["picker", "list"]
        sid = str(args.get("session_id") or "").strip()
        if sid:
            cmd.append(sid)
        cmd.extend(["--limit", str(int(args.get("limit") or 50))])

    elif name == "qclaw_google_photos_recent":
        cmd = base_cmd + ["recent", "--limit", str(int(args.get("limit") or 20))]

    elif name == "qclaw_google_photos_get":
        media_id = str(args.get("media_id") or "").strip()
        if not media_id:
            return {"ok": False, "error": "media_id é obrigatório"}
        cmd = base_cmd + ["get", media_id]
        sid = str(args.get("session_id") or "").strip()
        if sid:
            cmd.extend(["--session", sid])
        if args.get("with_image", True):
            cmd.append("--with-image")
            cmd.extend(["--max-width", str(int(args.get("max_width") or 1200))])

    elif name == "qclaw_google_photos_status":
        cmd = base_cmd + ["status"]

    else:
        return {"ok": False, "error": f"tool desconhecida: {name}"}

    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        stdout = (result.stdout or "").strip()
        if result.returncode != 0:
            err = stdout or (result.stderr or "").strip()
            return {"ok": False, "error": err[:2000]}
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"ok": True, "output": stdout[:4000]}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "timeout ao acessar Google Fotos (90s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_tool_learning_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    script = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-chat-autoaprimorar"
        / "scripts"
        / "tool_learnings.py"
    )
    if not script.is_file():
        return {"ok": False, "error": f"script não encontrado: {script}"}

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_tool_learning_record":
        tool = str(args.get("tool") or "").strip()
        error = str(args.get("error") or "").strip()
        fix = str(args.get("fix") or "").strip()
        if not tool or not error or not fix:
            return {"ok": False, "error": "tool, error e fix são obrigatórios"}
        cmd = base_cmd + ["record", "--tool", tool, "--error", error, "--fix", fix]
        skill = str(args.get("skill") or "").strip()
        if skill:
            cmd += ["--skill", skill]
        error_kind = str(args.get("error_kind") or "").strip()
        if error_kind:
            cmd += ["--error-kind", error_kind]
        fix_kind = str(args.get("fix_kind") or "").strip()
        if fix_kind:
            cmd += ["--fix-kind", fix_kind]
        attempted = args.get("attempted_args")
        if attempted:
            cmd += ["--attempted-args", json.dumps(attempted, ensure_ascii=False)]
        fix_args = args.get("fix_args")
        if fix_args:
            cmd += ["--fix-args", json.dumps(fix_args, ensure_ascii=False)]
        if args.get("success"):
            cmd += ["--success"]

    elif name == "qclaw_tool_learning_search":
        query = str(args.get("query") or "").strip()
        tool = str(args.get("tool") or "").strip()
        if not query and not tool:
            return {"ok": False, "error": "query ou tool é obrigatório"}
        cmd = base_cmd + ["search"]
        if query:
            cmd.append(query)
        if tool:
            cmd += ["--tool", tool]
        limit = args.get("limit")
        if limit is not None:
            cmd += ["--limit", str(int(limit))]

    elif name == "qclaw_tool_learning_list":
        cmd = base_cmd + ["list"]
        limit = args.get("limit")
        if limit is not None:
            cmd += ["--limit", str(int(limit))]

    elif name == "qclaw_tool_learning_stats":
        cmd = base_cmd + ["stats"]

    else:
        return {"ok": False, "error": f"operação desconhecida: {name}"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = result.stdout.strip()
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or f"exit code {result.returncode}"}
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"ok": True, "output": stdout}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout ao executar tool_learnings"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_aulas_memoria_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .aulas_memoria import (
        list_aulas,
        list_notas,
        list_skills,
        memoria_show,
        memoria_summary,
        search_aulas,
        search_notas,
        search_skills,
        store_aula,
        store_nota,
        store_skill,
    )

    if name == "qclaw_aula_store":
        return store_aula(
            title=str(args.get("title") or ""),
            discipline=str(args.get("discipline") or ""),
            professor=str(args.get("professor") or ""),
            course=str(args.get("course") or ""),
            institution=str(args.get("institution") or ""),
            description=str(args.get("description") or ""),
            tags=args.get("tags"),
            aula_id=int(args["aula_id"]) if args.get("aula_id") is not None else None,
        )
    if name == "qclaw_aula_search":
        return search_aulas(
            str(args.get("query") or ""),
            discipline=str(args.get("discipline") or ""),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_aula_list":
        return list_aulas(
            discipline=str(args.get("discipline") or ""),
            limit=int(args.get("limit") or 30),
            offset=int(args.get("offset") or 0),
        )
    if name == "qclaw_nota_store":
        return store_nota(
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            aula_id=args.get("aula_id"),
            note_type=str(args.get("note_type") or "anotacao"),
            grade=args.get("grade"),
            max_grade=args.get("max_grade"),
            tags=args.get("tags"),
            nota_id=int(args["nota_id"]) if args.get("nota_id") is not None else None,
        )
    if name == "qclaw_nota_search":
        return search_notas(
            str(args.get("query") or ""),
            note_type=str(args.get("note_type") or ""),
            aula_id=args.get("aula_id"),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_nota_list":
        return list_notas(
            aula_id=args.get("aula_id"),
            note_type=str(args.get("note_type") or ""),
            limit=int(args.get("limit") or 30),
            offset=int(args.get("offset") or 0),
        )
    if name == "qclaw_study_skill_store":
        return store_skill(
            name=str(args.get("name") or ""),
            aula_id=args.get("aula_id"),
            category=str(args.get("category") or ""),
            level=str(args.get("level") or "iniciante"),
            description=str(args.get("description") or ""),
            proficiency=float(args.get("proficiency") or 0.0),
            tags=args.get("tags"),
            skill_id=int(args["skill_id"]) if args.get("skill_id") is not None else None,
        )
    if name == "qclaw_study_skill_search":
        return search_skills(
            str(args.get("query") or ""),
            category=str(args.get("category") or ""),
            level=str(args.get("level") or ""),
            aula_id=args.get("aula_id"),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_study_skill_list":
        return list_skills(
            aula_id=args.get("aula_id"),
            category=str(args.get("category") or ""),
            level=str(args.get("level") or ""),
            limit=int(args.get("limit") or 30),
            offset=int(args.get("offset") or 0),
        )
    if name == "qclaw_aulas_memoria_summary":
        return memoria_summary()
    if name == "qclaw_aulas_memoria_show":
        return memoria_show(limit=int(args.get("limit") or 50))
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_memclaw_memoria_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .memclaw_memoria import (
        memclaw_memoria_search,
        memclaw_memoria_show,
        memclaw_memoria_summary,
    )

    scope = str(args.get("scope") or "agent")
    if name == "qclaw_memclaw_memoria_summary":
        return memclaw_memoria_summary(scope=scope)
    if name == "qclaw_memclaw_memoria_search":
        return memclaw_memoria_search(
            str(args.get("query") or ""),
            limit=int(args.get("limit") or 20),
            scope=scope,
            include_brief=bool(args.get("include_brief", True)),
        )
    if name == "qclaw_memclaw_memoria_show":
        return memclaw_memoria_show(
            limit=int(args.get("limit") or 30),
            scope=scope,
            query=str(args.get("query") or ""),
        )
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_ai_kb_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .ai_engineer_kb import kb_summary, list_entries, search_entries, store_entry

    if name == "qclaw_ai_kb_store":
        return store_entry(
            title=str(args.get("title") or ""),
            category=str(args.get("category") or ""),
            content=str(args.get("content") or ""),
            summary=str(args.get("summary") or ""),
            tags=args.get("tags"),
            source_url=str(args.get("source_url") or ""),
            source_type=str(args.get("source_type") or "internal"),
            confidence=float(args.get("confidence") or 0.8),
        )
    if name == "qclaw_ai_kb_search":
        return search_entries(
            str(args.get("query") or ""),
            category=str(args.get("category") or ""),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_ai_kb_list":
        return list_entries(
            category=str(args.get("category") or ""),
            tag=str(args.get("tag") or ""),
            limit=int(args.get("limit") or 30),
            offset=int(args.get("offset") or 0),
        )
    if name == "qclaw_ai_kb_summary":
        return kb_summary()
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_swarm_manage_tool(
    name: str,
    args: dict[str, Any],
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    from .swarm_manage_ops import dispatch_swarm_action, set_swarm_model_report

    if name == "qclaw_swarm_set_model":
        hooks: dict[str, Any] = {}
        if ctx is not None:
            if getattr(ctx, "swarm_model", None) is not None:
                hooks["swarm_model_fn"] = ctx.swarm_model
            if getattr(ctx, "swarm_robot_update", None) is not None:
                hooks["swarm_robot_update_fn"] = ctx.swarm_robot_update
            if getattr(ctx, "swarm_robots_snapshot", None) is not None:
                hooks["robots_snapshot_fn"] = ctx.swarm_robots_snapshot
        return set_swarm_model_report(args, **hooks)

    action_map = {
        "qclaw_swarm_list": "list",
        "qclaw_swarm_get": "get",
        "qclaw_swarm_create": "create",
        "qclaw_swarm_update": "update",
        "qclaw_swarm_delete": "delete",
        "qclaw_swarm_health": "health",
        "qclaw_swarm_topology": "topology",
        "qclaw_swarm_design": "design",
    }
    action = action_map.get(name)
    if not action:
        return {"ok": False, "error": f"operação desconhecida: {name}"}
    if action == "update":
        payload = dict(args)
        swarm_name = str(payload.pop("name", "") or "").strip()
        if not swarm_name:
            return {"ok": False, "error": "name is required"}
        return dispatch_swarm_action("update", {"name": swarm_name, **payload})
    return dispatch_swarm_action(action, args)


def _jobs_hooks_from_ctx(ctx: Any) -> dict[str, Any]:
    hooks: dict[str, Any] = {}
    if ctx is None:
        return hooks
    cancel = getattr(ctx, "jobs_cancel", None)
    if cancel is not None:
        hooks["cancel"] = cancel
    cron_action = getattr(ctx, "jobs_cron_action", None)
    if cron_action is not None:
        hooks["cron_action"] = cron_action
    cron_create = getattr(ctx, "jobs_cron_create", None)
    if cron_create is not None:
        hooks["cron_create"] = cron_create
    cron_edit = getattr(ctx, "jobs_cron_edit", None)
    if cron_edit is not None:
        hooks["cron_edit"] = cron_edit
    return hooks


def _run_token_mode_tool(
    args: dict[str, Any],
    *,
    chat_cfg: Any,
) -> dict[str, Any]:
    from .token_consumption_ops import dispatch_token_mode_action

    action = str(args.get("action") or "status").strip().lower()
    return dispatch_token_mode_action(action, args, chat_cfg=chat_cfg)


def _run_jobs_manage_tool(
    name: str,
    args: dict[str, Any],
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    from .jobs_manage_ops import dispatch_jobs_action

    action_map = {
        "qclaw_jobs_health": "health",
        "qclaw_jobs_list_running": "list_running",
        "qclaw_jobs_get_running": "get_running",
        "qclaw_jobs_cancel": "cancel",
        "qclaw_jobs_cancel_all_batches": "cancel_all_batches",
        "qclaw_jobs_cron_list": "cron_list",
        "qclaw_jobs_cron_get": "cron_get",
        "qclaw_jobs_cron_action": "cron_action",
        "qclaw_jobs_cron_create": "cron_create",
        "qclaw_jobs_cron_edit": "cron_edit",
    }
    action = action_map.get(name)
    if not action:
        return {"ok": False, "error": f"operação desconhecida: {name}"}
    payload = dict(args)
    if action == "cron_edit":
        job_id = str(payload.pop("job_id", "") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        return dispatch_jobs_action(
            "cron_edit",
            {"job_id": job_id, **payload},
            hooks=_jobs_hooks_from_ctx(ctx),
        )
    return dispatch_jobs_action(action, payload, hooks=_jobs_hooks_from_ctx(ctx))


def _run_kanban_ide_handoff_tool(args: dict[str, Any]) -> dict[str, Any]:
    from .kanban_ide_handoff_dispatch import dispatch_kanban_ide_handoff

    return dispatch_kanban_ide_handoff(args)


def _run_monitor_update_tool(args: dict[str, Any]) -> dict[str, Any]:
    from .monitor_self_update import dispatch_monitor_update

    return dispatch_monitor_update(args)


def _run_app_passwords_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .app_passwords_ops import dispatch_app_passwords_tool

    return dispatch_app_passwords_tool(name, args)


def _run_google_oauth_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .google_oauth_ops import dispatch_google_oauth_tool

    return dispatch_google_oauth_tool(name, args)


def _run_team_files_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    from .team_files_search import dispatch_team_files_search

    return dispatch_team_files_search(args)


def _run_team_files_download_tool(args: dict[str, Any]) -> dict[str, Any]:
    from .team_files_download import dispatch_team_files_download

    return dispatch_team_files_download(args)


def _run_team_files_send_tool(
    args: dict[str, Any],
    *,
    default_whatsapp_recipient: str | None = None,
) -> dict[str, Any]:
    from .team_files_send import dispatch_team_files_send

    payload = dict(args)
    if default_whatsapp_recipient and not payload.get("default_whatsapp_recipient"):
        payload["default_whatsapp_recipient"] = default_whatsapp_recipient
    return dispatch_team_files_send(payload)


def _run_email_team_pdf_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .email_team_normas_ops import (
        dispatch_email_team_pdf_list,
        dispatch_email_team_pdf_save,
    )

    if name == "qclaw_email_team_pdf_list":
        return dispatch_email_team_pdf_list(args)
    if name == "qclaw_email_team_pdf_save":
        return dispatch_email_team_pdf_save(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_gmail_attachments_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .gmail_attachments_ops import (
        dispatch_gmail_attachments_download,
        dispatch_gmail_attachments_list,
    )

    if name == "qclaw_gmail_attachments_list":
        return dispatch_gmail_attachments_list(args)
    if name == "qclaw_gmail_attachments_download":
        return dispatch_gmail_attachments_download(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_trello_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .trello_ops import (
        dispatch_trello_board_detail,
        dispatch_trello_boards,
        dispatch_trello_card_create,
        dispatch_trello_card_move,
        dispatch_trello_connect,
        dispatch_trello_json_import,
        dispatch_trello_kanban_sync,
    )

    if name == "qclaw_trello_connect":
        return dispatch_trello_connect(args)
    if name == "qclaw_trello_boards":
        return dispatch_trello_boards(args)
    if name == "qclaw_trello_board_detail":
        return dispatch_trello_board_detail(args)
    if name == "qclaw_trello_card_create":
        return dispatch_trello_card_create(args)
    if name == "qclaw_trello_card_move":
        return dispatch_trello_card_move(args)
    if name == "qclaw_trello_kanban_sync":
        return dispatch_trello_kanban_sync(args)
    if name == "qclaw_trello_json_import":
        return dispatch_trello_json_import(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_team_rules_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .team_rules import (
        dispatch_team_rule_delete,
        dispatch_team_rule_list,
        dispatch_team_rule_search,
        dispatch_team_rule_store,
        dispatch_team_rules_summary,
    )

    if name == "qclaw_team_rule_store":
        return dispatch_team_rule_store(args)
    if name == "qclaw_team_rule_list":
        return dispatch_team_rule_list(args)
    if name == "qclaw_team_rule_search":
        return dispatch_team_rule_search(args)
    if name == "qclaw_team_rule_delete":
        return dispatch_team_rule_delete(args)
    if name == "qclaw_team_rules_summary":
        return dispatch_team_rules_summary(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_team_facts_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .team_facts import (
        dispatch_team_fact_delete,
        dispatch_team_fact_import,
        dispatch_team_fact_list,
        dispatch_team_fact_search,
        dispatch_team_fact_store,
        dispatch_team_facts_for_roteiro,
        dispatch_team_facts_summary,
    )

    if name == "qclaw_team_fact_store":
        return dispatch_team_fact_store(args)
    if name == "qclaw_team_fact_import":
        return dispatch_team_fact_import(args)
    if name == "qclaw_team_fact_list":
        return dispatch_team_fact_list(args)
    if name == "qclaw_team_fact_search":
        return dispatch_team_fact_search(args)
    if name == "qclaw_team_fact_delete":
        return dispatch_team_fact_delete(args)
    if name == "qclaw_team_facts_summary":
        return dispatch_team_facts_summary(args)
    if name == "qclaw_team_facts_for_roteiro":
        return dispatch_team_facts_for_roteiro(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_legal_evaluation_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .legal_evaluation_ops import (
        dispatch_legal_evaluation_get,
        dispatch_legal_evaluation_list,
        dispatch_legal_evaluation_save,
        dispatch_legal_evaluation_search,
        dispatch_legal_norma_extract,
        dispatch_legal_normas_list,
        dispatch_legal_pdf_extract,
    )

    if name == "qclaw_legal_pdf_extract":
        return dispatch_legal_pdf_extract(args)
    if name == "qclaw_legal_normas_list":
        return dispatch_legal_normas_list(args)
    if name == "qclaw_legal_norma_extract":
        return dispatch_legal_norma_extract(args)
    if name == "qclaw_legal_evaluation_save":
        return dispatch_legal_evaluation_save(args)
    if name == "qclaw_legal_evaluation_get":
        return dispatch_legal_evaluation_get(args)
    if name == "qclaw_legal_evaluation_list":
        return dispatch_legal_evaluation_list(args)
    if name == "qclaw_legal_evaluation_search":
        return dispatch_legal_evaluation_search(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_journal_rules_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .journal_rules_ops import (
        dispatch_journal_rules_delete,
        dispatch_journal_rules_extract,
        dispatch_journal_rules_get,
        dispatch_journal_rules_list,
        dispatch_journal_rules_save,
        dispatch_journal_rules_search,
    )

    if name == "qclaw_journal_rules_save":
        return dispatch_journal_rules_save(args)
    if name == "qclaw_journal_rules_get":
        return dispatch_journal_rules_get(args)
    if name == "qclaw_journal_rules_list":
        return dispatch_journal_rules_list(args)
    if name == "qclaw_journal_rules_search":
        return dispatch_journal_rules_search(args)
    if name == "qclaw_journal_rules_extract":
        return dispatch_journal_rules_extract(args)
    if name == "qclaw_journal_rules_delete":
        return dispatch_journal_rules_delete(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_budget_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .budget_ops import (
        dispatch_budget_analyze_csv,
        dispatch_budget_get,
        dispatch_budget_save,
        dispatch_budget_summary,
    )

    if name == "qclaw_budget_get":
        return dispatch_budget_get(args)
    if name == "qclaw_budget_save":
        return dispatch_budget_save(args)
    if name == "qclaw_budget_summary":
        return dispatch_budget_summary(args)
    if name == "qclaw_budget_analyze_csv":
        return dispatch_budget_analyze_csv(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_team_payments_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .team_payments_ops import (
        dispatch_team_payment_delete,
        dispatch_team_payment_get,
        dispatch_team_payment_list,
        dispatch_team_payment_save,
        dispatch_team_payment_search,
        dispatch_team_payments_summary,
    )

    if name == "qclaw_team_payment_save":
        return dispatch_team_payment_save(args)
    if name == "qclaw_team_payment_get":
        return dispatch_team_payment_get(args)
    if name == "qclaw_team_payment_list":
        return dispatch_team_payment_list(args)
    if name == "qclaw_team_payment_search":
        return dispatch_team_payment_search(args)
    if name == "qclaw_team_payment_delete":
        return dispatch_team_payment_delete(args)
    if name == "qclaw_team_payments_summary":
        return dispatch_team_payments_summary(args)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_aws_manage_tool(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    from .aws_manage_ops import dispatch_aws_action

    action_map = {
        "qclaw_aws_overview": "overview",
        "qclaw_aws_cost": "cost",
        "qclaw_aws_ce_get_cost_and_usage": "ce_get_cost_and_usage",
        "qclaw_aws_machines": "machines",
        "qclaw_aws_waste": "waste",
        "qclaw_aws_env": "env",
    }
    action = action_map.get(name)
    if not action:
        return {"ok": False, "error": f"operação AWS desconhecida: {name}"}
    return dispatch_aws_action(action, dict(args))


def _run_ruflo_swarm_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .ruflo_swarm_ops import (
        architecture_report,
        audit_ruflo_scripts,
        plan_swarm,
        scaffold_swarm_script,
        validate_swarm_script,
    )

    if name == "qclaw_ruflo_swarm_architecture":
        include = args.get("include_doctor")
        if include is None:
            include = True
        return architecture_report(include_doctor=bool(include))
    if name == "qclaw_ruflo_swarm_audit":
        root = str(args.get("root") or "").strip() or None
        return audit_ruflo_scripts(root)
    if name == "qclaw_ruflo_swarm_validate":
        path = str(args.get("path") or "").strip() or None
        content = str(args.get("content") or "").strip() or None
        return validate_swarm_script(path=path, content=content)
    if name == "qclaw_ruflo_swarm_plan":
        task = str(args.get("task") or "").strip()
        task_type = str(args.get("task_type") or "").strip() or None
        return plan_swarm(task, task_type=task_type)
    if name == "qclaw_ruflo_swarm_scaffold":
        slug = str(args.get("name") or "").strip()
        return scaffold_swarm_script(
            slug,
            task_type=str(args.get("task_type") or "development").strip(),
            task=str(args.get("task") or "").strip(),
            output_dir=str(args.get("output_dir") or "").strip() or None,
            overwrite=bool(args.get("overwrite")),
        )
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_agent_evaluate_tool(
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .agent_evaluate import evaluate_agents_report, evaluate_single_agent

    slug = str(args.get("slug") or "").strip()
    host = str(args.get("host") or "").strip()
    if not host and chat_cfg.qclaw_tools_enabled:
        host = "http://127.0.0.1:9101"
    try:
        if slug:
            return evaluate_single_agent(slug, host=host)
        return evaluate_agents_report(host=host)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run_agent_fix_tool(
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
    ctx: Any,
) -> dict[str, Any]:
    from .agent_fix import fix_agent, fix_agents_report
    from .config import HermesAgentCreateConfig

    slug = str(args.get("slug") or "").strip()
    swarm_name = str(args.get("swarm_name") or "").strip() or None
    host = str(args.get("host") or "").strip()
    if not host and chat_cfg.qclaw_tools_enabled:
        host = "http://127.0.0.1:9101"
    dry_run = bool(args.get("dry_run"))
    schedule = str(args.get("schedule") or "").strip() or None
    workdir = str(args.get("workdir") or "").strip() or None
    create_cfg = getattr(ctx, "hermes_agent_create_cfg", None) or HermesAgentCreateConfig()
    try:
        if slug:
            return fix_agent(
                slug,
                host=host,
                create_cfg=create_cfg,
                dry_run=dry_run,
                schedule=schedule,
                workdir=workdir,
            )
        return fix_agents_report(
            host=host,
            create_cfg=create_cfg,
            dry_run=dry_run,
            swarm_name=swarm_name,
            workdir=workdir,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run_chat_personality_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from .chat_personality import (
        collect_user_messages,
        deactivate_profile,
        get_profile,
        profile_summary,
        save_profile,
    )

    if name == "qclaw_chat_personality_get":
        return get_profile(str(args.get("user_id") or ""))
    if name == "qclaw_chat_personality_samples":
        hist_path = Path(chat_cfg.history_db_path).expanduser()
        return collect_user_messages(
            str(args.get("user_id") or ""),
            history_db_path=hist_path,
            limit=int(args.get("limit") or 60),
        )
    if name == "qclaw_chat_personality_save":
        uid = str(args.get("user_id") or "").strip()
        if not uid:
            return {"ok": False, "error": "user_id é obrigatório"}
        return save_profile(
            uid,
            display_name=str(args.get("display_name") or ""),
            tone=str(args.get("tone") or ""),
            verbosity=str(args.get("verbosity") or ""),
            language=str(args.get("language") or "pt-BR"),
            expertise=str(args.get("expertise") or ""),
            format_preference=str(args.get("format_preference") or ""),
            traits=args.get("traits"),
            style_guide=str(args.get("style_guide") or ""),
            system_prompt_addon=str(args.get("system_prompt_addon") or ""),
            sample_phrases=args.get("sample_phrases"),
            topics=args.get("topics"),
            avoid=args.get("avoid"),
            confidence=float(args.get("confidence") or 0.7),
            active=bool(args.get("active", True)),
            message_samples_count=int(args.get("message_samples_count") or 0),
            source=str(args.get("source") or "inferred"),
            reason=str(args.get("reason") or ""),
        )
    if name == "qclaw_chat_personality_deactivate":
        return deactivate_profile(str(args.get("user_id") or ""))
    if name == "qclaw_chat_personality_summary":
        return profile_summary()
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_visual_memory_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .visual_memory import (
        add_photo,
        deactivate_profile,
        get_profile,
        list_profiles,
        profile_summary,
        remove_photo,
        resolve_for_thumbnail,
        save_profile,
        set_primary_photo,
    )

    if name == "qclaw_visual_memory_list":
        return list_profiles(
            kind=str(args.get("kind") or ""),
            user_id=str(args.get("user_id") or ""),
            query=str(args.get("query") or ""),
            limit=int(args.get("limit") or 30),
            active_only=bool(args.get("active_only", True)),
        )
    if name == "qclaw_visual_memory_get":
        return get_profile(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
            user_id=str(args.get("user_id") or ""),
        )
    if name == "qclaw_visual_memory_save":
        visual_analysis = args.get("visual_analysis")
        if not isinstance(visual_analysis, dict) and isinstance(args.get("visual_analysis_json"), str):
            from .character_visual import parse_visual_analysis

            visual_analysis = parse_visual_analysis(args.get("visual_analysis_json"))
        return save_profile(
            profile_id=str(args.get("profile_id") or ""),
            user_id=str(args.get("user_id") or ""),
            name=str(args.get("name") or ""),
            kind=str(args.get("kind") or "user"),
            display_name=str(args.get("display_name") or ""),
            persona_prompt=str(args.get("persona_prompt") or ""),
            visual_traits=args.get("visual_traits"),
            style_notes=str(args.get("style_notes") or ""),
            visual_analysis=visual_analysis if isinstance(visual_analysis, dict) else None,
            tags=args.get("tags"),
            active=bool(args.get("active", True)),
        )
    if name == "qclaw_visual_memory_add_photo":
        return add_photo(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
            source_path=str(args.get("source_path") or args.get("path") or ""),
            caption=str(args.get("caption") or ""),
            role=str(args.get("role") or "reference"),
            set_primary=bool(args.get("set_primary")),
            analyze=bool(args.get("analyze", True)),
            api_key=str(args.get("api_key") or "").strip() or None,
            model=str(args.get("model") or "").strip() or None,
        )
    if name == "qclaw_visual_memory_remove_photo":
        return remove_photo(
            profile_id=str(args.get("profile_id") or ""),
            photo_id=str(args.get("photo_id") or ""),
        )
    if name == "qclaw_visual_memory_set_primary":
        return set_primary_photo(
            profile_id=str(args.get("profile_id") or ""),
            photo_id=str(args.get("photo_id") or ""),
        )
    if name == "qclaw_visual_memory_resolve":
        return resolve_for_thumbnail(
            profile_id=str(args.get("profile_id") or args.get("visual_memory_id") or ""),
            name=str(args.get("name") or args.get("character_name") or ""),
            user_id=str(args.get("user_id") or args.get("visual_user_id") or ""),
        )
    if name == "qclaw_visual_memory_deactivate":
        return deactivate_profile(str(args.get("profile_id") or ""))
    if name == "qclaw_visual_memory_summary":
        return profile_summary()
    if name == "qclaw_visual_memory_merge_photos":
        from .visual_memory_lora import merge_all_profile_photos

        return merge_all_profile_photos(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
            api_key=str(args.get("api_key") or "").strip() or None,
            model=str(args.get("model") or "").strip() or None,
        )
    if name == "qclaw_visual_memory_lora_train":
        from .visual_memory_lora import train_lora

        return train_lora(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
            trigger_word=str(args.get("trigger_word") or ""),
            training_steps=int(args.get("training_steps") or 1000),
            wait=bool(args.get("wait", True)),
            evaluate=bool(args.get("evaluate", True)),
            api_key=str(args.get("api_key") or "").strip() or None,
            timeout=float(args.get("timeout_seconds") or args.get("timeout") or 3600.0),
        )
    if name == "qclaw_visual_memory_lora_status":
        from .visual_memory_lora import get_lora_status

        return get_lora_status(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
        )
    if name == "qclaw_visual_memory_lora_evaluate":
        from .visual_memory_lora import evaluate_lora

        return evaluate_lora(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or ""),
            api_key=str(args.get("api_key") or "").strip() or None,
        )
    if name == "qclaw_profile_from_photo":
        from .profile_from_photo import (
            register_owner_profile_from_photo_path,
            register_owner_profile_from_upload,
        )

        sync = bool(args.get("sync_conversations", True))
        photo_path = str(args.get("photo_path") or args.get("path") or "").strip()
        if photo_path:
            uid = str(args.get("user_id") or "").strip()
            return register_owner_profile_from_photo_path(
                photo_path,
                name=str(args.get("name") or ""),
                user_id=uid,
                display_name=str(args.get("display_name") or ""),
                api_key=str(args.get("api_key") or "").strip() or None,
                model=str(args.get("model") or "").strip() or None,
                sync_conversations=sync,
            )
        raw_b64 = str(args.get("data_base64") or args.get("base64") or "").strip()
        if raw_b64:
            payload = dict(args)
            payload["data_base64"] = raw_b64
            return register_owner_profile_from_upload(payload, sync_conversations=sync)
        return {"ok": False, "error": "photo_path ou data_base64 é obrigatório"}
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_character_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .character_preview import get_character_preview, search_characters
    from .character_psychology import normalize_psych_profile
    from .visual_memory import save_psych_profile

    if name == "qclaw_character_search":
        return search_characters(
            query=str(args.get("query") or args.get("q") or ""),
            kind=str(args.get("kind") or ""),
            limit=int(args.get("limit") or 24),
        )
    if name == "qclaw_character_preview":
        return get_character_preview(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or args.get("character_name") or ""),
            user_id=str(args.get("user_id") or ""),
        )
    if name == "qclaw_character_psych_save":
        psych = args.get("psych_profile")
        if not isinstance(psych, dict):
            psych = {
                k: args.get(k)
                for k in (
                    "archetype",
                    "mbti",
                    "traits",
                    "motivations",
                    "fears",
                    "values",
                    "speech_style",
                    "backstory_summary",
                    "emotional_range",
                    "relationships",
                    "psych_prompt",
                    "source",
                )
                if args.get(k) is not None
            }
        return save_psych_profile(
            profile_id=str(args.get("profile_id") or ""),
            name=str(args.get("name") or args.get("character_name") or ""),
            psych_profile=normalize_psych_profile(psych),
            merge=bool(args.get("merge", True)),
        )
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_thumbnail_style_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .thumbnail_styles import (
        deactivate_preset,
        get_preset,
        list_presets,
        preset_summary,
        resolve_for_thumbnail,
        save_from_thumbnail_args,
        save_preset,
    )

    if name == "qclaw_thumbnail_style_list":
        return list_presets(
            query=str(args.get("query") or ""),
            limit=int(args.get("limit") or 30),
        )
    if name == "qclaw_thumbnail_style_get":
        return get_preset(
            style_id=str(args.get("style_id") or args.get("thumbnail_style_id") or ""),
            name=str(args.get("name") or args.get("thumbnail_style_name") or ""),
        )
    if name == "qclaw_thumbnail_style_save":
        return save_preset(
            style_id=str(args.get("style_id") or ""),
            name=str(args.get("name") or ""),
            description=str(args.get("description") or ""),
            styles=args.get("styles"),
            image_style_name=str(args.get("image_style_name") or ""),
            neuro_boost=bool(args.get("neuro_boost", True)),
            include_mascot=bool(args.get("include_mascot")),
            maintain_persona=bool(args.get("maintain_persona", True)),
            auto_optimize_viral=bool(args.get("auto_optimize_viral", True)),
            text_overlay_hints=str(args.get("text_overlay_hints") or ""),
            thumbnail_format=args.get("thumbnail_format"),
            reference_image_path=str(args.get("reference_image_path") or ""),
            character_name=str(args.get("character_name") or ""),
            visual_memory_id=str(args.get("visual_memory_id") or ""),
            notes=str(args.get("notes") or ""),
            tags=args.get("tags"),
        )
    if name == "qclaw_thumbnail_style_capture":
        capture_args = args.get("args")
        if not isinstance(capture_args, dict):
            return {"ok": False, "error": "args (object) é obrigatório"}
        return save_from_thumbnail_args(
            name=str(args.get("name") or ""),
            args=capture_args,
            description=str(args.get("description") or ""),
            tags=args.get("tags"),
        )
    if name == "qclaw_thumbnail_style_resolve":
        return resolve_for_thumbnail(
            style_id=str(args.get("style_id") or args.get("thumbnail_style_id") or ""),
            name=str(args.get("name") or args.get("thumbnail_style_name") or ""),
        )
    if name == "qclaw_thumbnail_style_deactivate":
        return deactivate_preset(
            style_id=str(args.get("style_id") or ""),
            name=str(args.get("name") or ""),
        )
    if name == "qclaw_thumbnail_style_summary":
        return preset_summary()
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_user_personal_data_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .user_personal_data import (
        build_profile_preview,
        delete_field,
        export_profile,
        get_profile,
        profile_summary,
        resolve_user_id,
        save_profile,
        search_profiles,
    )

    uid = resolve_user_id(str(args.get("user_id") or ""))

    if name == "qclaw_user_data_get":
        return get_profile(uid)
    if name == "qclaw_user_data_preview":
        return build_profile_preview(
            uid,
            photo_path=str(args.get("photo_path") or ""),
        )
    if name == "qclaw_user_data_save":
        payload = {k: v for k, v in args.items() if k != "user_id" and v is not None}
        return save_profile(
            uid,
            tags=payload.pop("tags", None),
            relationships=payload.pop("relationships", None),
            custom_fields=payload.pop("custom_fields", None),
            reason=str(args.get("reason") or "chat"),
            **payload,
        )
    if name == "qclaw_user_data_delete_field":
        return delete_field(
            uid,
            str(args.get("field") or ""),
            custom_key=str(args.get("custom_key") or ""),
        )
    if name == "qclaw_user_data_search":
        return search_profiles(
            str(args.get("query") or ""),
            limit=int(args.get("limit") or 20),
        )
    if name == "qclaw_user_data_export":
        return export_profile(uid)
    if name == "qclaw_user_data_summary":
        return profile_summary()
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_identidade_civil_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .identity_civil_ops import dispatch_identidade_tool

    return dispatch_identidade_tool(name, args)


def _run_curriculum_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .curriculum_ops import dispatch_curriculum_tool

    return dispatch_curriculum_tool(name, args)


def _run_minicurriculum_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from .mini_curriculum import dispatch_minicurriculum_tool

    return dispatch_minicurriculum_tool(name, args)


def _run_email_memoria_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[2] / "skills" / "qclaw-chat-email-memoria" / "scripts" / "email_memoria.py"
    if not script.is_file():
        return {"ok": False, "error": f"script não encontrado: {script}"}

    base_cmd = [sys.executable, str(script)]

    if name == "qclaw_email_memoria_index":
        team = args.get("team")
        if not team:
            return {"ok": False, "error": "team é obrigatório"}
        cmd = base_cmd + ["index", "--team", json.dumps(team)]
        threads = args.get("threads")
        if threads:
            cmd += ["--threads", json.dumps(threads)]
        if args.get("full"):
            cmd += ["--full"]

    elif name == "qclaw_email_memoria_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query é obrigatório"}
        cmd = base_cmd + ["search", query]

    elif name == "qclaw_email_memoria_list":
        cmd = base_cmd + ["list"]
        member_email = str(args.get("member_email") or "").strip()
        if member_email:
            cmd += ["--member", member_email]

    elif name == "qclaw_email_memoria_summary":
        cmd = base_cmd + ["summary"]

    else:
        return {"ok": False, "error": f"operação desconhecida: {name}"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        stdout = result.stdout.strip()
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or f"exit code {result.returncode}"}
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return {"ok": True, "output": stdout}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout ao executar email_memoria"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_whatsapp_memoria_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    """Handle qclaw_whatsapp_memoria_* tool calls via MongoDB store."""
    from .whatsapp_memoria_store import (
        count_messages,
        db_path,
        list_messages as wa_list_messages,
        search_messages,
        summary as wa_summary,
        upsert_group,
        upsert_message,
    )

    _WA_DB_PATH = db_path()
    _load_group_memory()
    _KNOWN_GROUPS = [
        {"jid": jid, "name": name}
        for name, jid in _GROUP_MEMORY.values()
    ]
    _seen_jids: set[str] = set()
    _KNOWN_GROUPS_DEDUPED: list[dict[str, str]] = []
    for g in _KNOWN_GROUPS:
        if g["jid"] not in _seen_jids:
            _seen_jids.add(g["jid"])
            _KNOWN_GROUPS_DEDUPED.append(g)
    _KNOWN_GROUPS = _KNOWN_GROUPS_DEDUPED

    def _fetch_messages(jid, limit=200):
        from .wacli_runner import run_wacli

        try:
            proc = run_wacli(
                ["messages", "list", "--chat", jid, "--limit", str(limit)],
                timeout=60.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("data"), dict):
                inner = data["data"]
                msgs = inner.get("messages")
                if isinstance(msgs, list):
                    return msgs
            return data.get("messages", data.get("chats", []))
        return []

    def _parse_msg(raw, group_jid, group_name):
        msg_id = (
            raw.get("id")
            or raw.get("ID")
            or raw.get("MsgID")
            or raw.get("message_id")
            or raw.get("key", {}).get("id", "")
        )
        sender_jid = (
            raw.get("sender")
            or raw.get("from")
            or raw.get("participant")
            or raw.get("SenderJID")
            or raw.get("key", {}).get("participant", "")
            or raw.get("key", {}).get("remoteJid", "")
        )
        sender_name = (
            raw.get("push_name")
            or raw.get("pushName")
            or raw.get("sender_name")
            or raw.get("SenderName")
            or ""
        )
        body = (
            raw.get("body")
            or raw.get("text")
            or raw.get("Text")
            or raw.get("DisplayText")
            or raw.get("message", {}).get("conversation", "")
            or raw.get("message", {}).get("extendedTextMessage", {}).get("text", "")
            or ""
        )
        msg_type = raw.get("type") or raw.get("message_type") or raw.get("MediaType") or "text"
        raw_ts = raw.get("timestamp") or raw.get("ts") or raw.get("time") or raw.get("Timestamp")
        timestamp_ts = None
        if raw_ts:
            try:
                if isinstance(raw_ts, str) and "T" in raw_ts:
                    from datetime import datetime

                    ts_norm = raw_ts.replace("Z", "+00:00")
                    timestamp_ts = datetime.fromisoformat(ts_norm).timestamp()
                else:
                    ts_val = float(raw_ts)
                    timestamp_ts = ts_val / 1000.0 if ts_val > 1e10 else ts_val
            except (ValueError, TypeError):
                pass
        is_from_me = int(
            bool(raw.get("from_me") or raw.get("fromMe") or raw.get("FromMe") or False)
        )
        quoted_id = (raw.get("quoted_id")
                     or raw.get("context_info", {}).get("quoted_message_id", "") or "")
        return {
            "msg_id": str(msg_id), "group_jid": group_jid, "group_name": group_name,
            "sender_jid": str(sender_jid), "sender_name": str(sender_name),
            "body": str(body), "msg_type": str(msg_type),
            "timestamp_ts": timestamp_ts, "is_from_me": is_from_me,
            "quoted_id": str(quoted_id),
        }

    if name == "qclaw_whatsapp_memoria_index":
        all_known = bool(args.get("all_known"))
        groups = args.get("groups") or ([] if not all_known else _KNOWN_GROUPS)
        if not groups and not all_known:
            return {"ok": False, "error": "Forneça groups ou all_known=true"}
        limit = int(args.get("limit") or 200)
        messages_map = args.get("messages") or {}
        totals = []
        for g in groups:
            jid = str(g.get("jid") or g.get("id") or "").strip()
            gname = str(g.get("name") or g.get("subject") or jid)
            if not jid:
                continue
            upsert_group(jid, gname, path=_WA_DB_PATH)
            raw_msgs = messages_map.get(jid, []) if messages_map else _fetch_messages(jid, limit)
            new_c = upd_c = 0
            for raw in raw_msgs:
                msg = _parse_msg(raw, jid, gname)
                if not msg["msg_id"]:
                    continue
                if upsert_message(msg, path=_WA_DB_PATH):
                    new_c += 1
                else:
                    upd_c += 1
            totals.append({
                "group": gname, "jid": jid, "new": new_c,
                "updated": upd_c, "total_in_db": count_messages(jid, path=_WA_DB_PATH),
                "fetched": len(raw_msgs),
            })
        return {
            "ok": True,
            "groups": totals,
            "total_messages": count_messages(path=_WA_DB_PATH),
        }

    if name == "qclaw_whatsapp_memoria_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query é obrigatório"}
        results = search_messages(
            query,
            group_filter=str(args.get("group") or "").strip(),
            path=_WA_DB_PATH,
        )
        return {"ok": True, "results": results, "count": len(results)}

    if name == "qclaw_whatsapp_memoria_list":
        messages = wa_list_messages(
            group_filter=str(args.get("group") or "").strip(),
            sender_filter=str(args.get("sender") or "").strip(),
            path=_WA_DB_PATH,
        )
        return {"ok": True, "messages": messages, "count": len(messages)}

    if name == "qclaw_whatsapp_memoria_summary":
        out = wa_summary(path=_WA_DB_PATH)
        return {"ok": True, **out}

    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_whatsapp_groups_sync_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Sync WhatsApp groups from wacli into MongoDB (collection wa_groups)."""
    from .openclaw_chat_whatsapp_groups import sync_groups_to_mongo
    from .whatsapp_memoria_store import list_all_groups

    refresh = bool(args.get("refresh"))
    force = bool(args.get("force"))
    out = sync_groups_to_mongo(refresh=refresh, force=force)
    if not out.get("ok"):
        return out
    groups = list_all_groups()
    return {
        **out,
        "groups": groups[:100],
        "truncated": len(groups) > 100,
    }


def _run_whatsapp_busca_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Handle qclaw_whatsapp_agenda_*, contacts_search, and messages_search."""
    from .whatsapp_busca import (
        agenda_stats,
        list_contacts,
        search_contacts,
        search_messages_wacli,
        sync_contacts,
    )

    if name == "qclaw_whatsapp_contacts_search":
        query = str(args.get("query") or args.get("q") or args.get("name") or "").strip()
        if not query:
            return {"ok": False, "error": "query é obrigatório (nome, telefone ou parte do nome)"}
        return search_contacts(query=query, limit=int(args.get("limit") or 50))
    if name == "qclaw_whatsapp_agenda_sync":
        refresh = bool(args.get("refresh", False))
        if isinstance(args.get("refresh"), str):
            refresh = args.get("refresh", "").strip().lower() in ("1", "true", "yes")
        full = bool(args.get("full", False))
        if isinstance(args.get("full"), str):
            full = args.get("full", "").strip().lower() in ("1", "true", "yes")
        force = bool(args.get("force", False))
        if isinstance(args.get("force"), str):
            force = args.get("force", "").strip().lower() in ("1", "true", "yes")
        return sync_contacts(refresh=refresh, full=full, force=force)
    if name == "qclaw_whatsapp_agenda_stats":
        return agenda_stats()
    if name == "qclaw_whatsapp_agenda_list":
        return list_contacts(
            query=str(args.get("query") or "").strip(),
            chat_type=str(args.get("chat_type") or "").strip(),
            limit=int(args.get("limit") or 50),
        )
    if name == "qclaw_whatsapp_messages_search":
        from .whatsapp_busca import resolve_chat_jid

        query = str(args.get("query") or args.get("q") or "").strip()
        chat = str(args.get("chat") or args.get("group") or "").strip()
        if chat and "@" not in chat:
            resolved = resolve_chat_jid(chat)
            if resolved:
                chat = resolved
            else:
                return {
                    "ok": False,
                    "error": f'Grupo/chat "{chat}" não encontrado na memória nem via wacli.',
                    "hint": (
                        "Use JID (ex.: 120363...@g.us), nome parcial, ou "
                        "qclaw_whatsapp_groups_sync refresh=true antes de buscar."
                    ),
                }
        return search_messages_wacli(
            query=query,
            chat=chat,
            limit=int(args.get("limit") or 30),
            after=str(args.get("after") or "").strip(),
            before=str(args.get("before") or "").strip(),
        )
    if name == "qclaw_whatsapp_media_download":
        from .whatsapp_busca import download_message_media, resolve_chat_jid

        msg_id = str(args.get("msg_id") or args.get("id") or "").strip()
        if not msg_id:
            return {"ok": False, "error": "msg_id é obrigatório"}
        chat = str(args.get("chat") or args.get("group") or "").strip()
        if chat and "@" not in chat:
            resolved = resolve_chat_jid(chat)
            if resolved:
                chat = resolved
            elif chat:
                return {
                    "ok": False,
                    "error": f'Grupo/chat "{chat}" não encontrado.',
                    "hint": "Use JID completo (ex.: 120363...@g.us) ou nome indexado.",
                }
        return download_message_media(msg_id, chat=chat)
    return {"ok": False, "error": f"operação desconhecida: {name}"}


def _run_desktop_control_tool(
    name: str,
    args: dict[str, Any],
    *,
    chat_cfg: OpenclawChatConfig,
) -> dict[str, Any]:
    from . import desktop_control
    from .chat_models import resolve_attachments_dir

    if not _desktop_control_enabled_for_host(chat_cfg):
        return {
            "ok": False,
            "error": "desktop control is disabled or not available on this host",
        }
    if name == "qclaw_desktop_screen_info":
        return desktop_control.get_screen_info()
    if name == "qclaw_desktop_list_windows":
        app = str(args.get("app") or "").strip() or None
        return desktop_control.list_windows(app=app)
    if name == "qclaw_desktop_open_app":
        app = str(args.get("app") or "").strip()
        focus = bool(args.get("focus", True))
        return desktop_control.open_app(app, focus=focus)
    if name == "qclaw_desktop_screenshot":
        region: Optional[tuple[int, int, int, int]] = None
        if all(k in args for k in ("x", "y", "width", "height")):
            region = (
                int(args["x"]),
                int(args["y"]),
                int(args["width"]),
                int(args["height"]),
            )
        out_dir = resolve_attachments_dir(chat_cfg) / "desktop"
        return desktop_control.take_screenshot(
            output_dir=out_dir,
            region=region,
            app=str(args.get("app") or "").strip() or None,
            window_title=str(args.get("window_title") or "").strip() or None,
            max_width=chat_cfg.desktop_screenshot_max_width,
            quality=chat_cfg.desktop_screenshot_quality,
        )
    if name == "qclaw_desktop_click":
        return desktop_control.click_at(
            int(args["x"]),
            int(args["y"]),
            button=str(args.get("button") or "left"),
            double=bool(args.get("double")),
        )
    if name == "qclaw_desktop_type":
        return desktop_control.type_text(
            str(args.get("text") or ""),
            app=str(args.get("app") or "").strip() or None,
        )
    if name == "qclaw_desktop_key":
        return desktop_control.press_key(
            str(args.get("keys") or ""),
            app=str(args.get("app") or "").strip() or None,
        )
    if name == "qclaw_desktop_scroll":
        return desktop_control.scroll_at(
            int(args["x"]),
            int(args["y"]),
            direction=str(args.get("direction") or "down"),
            amount=int(args.get("amount") or 3),
        )
    return {"ok": False, "error": f"unknown desktop tool: {name}"}


def _resolve_group_jid_from_memory(query: str) -> Optional[str]:
    """Resolve a group name/team_id to a WhatsApp JID using _GROUP_MEMORY.

    Tries partial and exact matches in memory, then wacli groups/chats lists.
    """
    from .openclaw_chat_whatsapp_groups import _register_group_memory

    _load_group_memory()
    q = (query or "").strip().lower()
    if not q:
        return None
    # Exact key match
    if q in _GROUP_MEMORY:
        return _GROUP_MEMORY[q][1]
    # Normalize: replace hyphens/underscores with spaces
    q_clean = q.replace("-", " ").replace("_", " ").strip()
    if q_clean in _GROUP_MEMORY:
        return _GROUP_MEMORY[q_clean][1]
    # Partial match in loaded memory
    for key, (name, gid) in _GROUP_MEMORY.items():
        if q in key or key in q or q in name.lower():
            return gid

    # --- Fallback: query wacli groups/chats in real-time ---
    jid, gname = _wacli_resolve_group_jid(q)
    if jid:
        _register_group_memory(gname or query.strip(), jid)
    return jid


def _wacli_resolve_group_jid(query: str) -> tuple[Optional[str], str]:
    """Find a group JID by partial name match in wacli groups/chats lists."""
    from .openclaw_chat_whatsapp_groups import _wacli_group_entries_from_store

    q = query.lower().strip()
    if not q:
        return None, ""

    def _match(entries: list[dict[str, str]]) -> tuple[Optional[str], str]:
        for entry in entries:
            gname = entry["name"]
            gname_lower = gname.lower().strip()
            gjid = entry["jid"]
            if q == gname_lower or q in gname_lower or gname_lower in q:
                return gjid, gname
        return None, ""

    try:
        jid, gname = _match(_wacli_group_entries_from_store(sources=("groups",)))
        if jid:
            return jid, gname
        return _match(_wacli_group_entries_from_store(sources=("chats",)))
    except Exception as exc:
        log.warning("_wacli_resolve_group_jid: exceção: %s: %s", type(exc).__name__, exc)
    return None, ""


def _send_to_group_jid_direct(
    group_jid: str, query: str, message: str
) -> dict[str, Any]:
    """Send a WhatsApp message directly to a group JID (fallback path)."""
    from .config import WhatsappDigestConfig
    from .whatsapp_allowlist import check_whatsapp_recipient, load_allowed_recipient_digits
    from .whatsapp_outbound import enqueue_whatsapp

    jid = (group_jid or "").strip().lower()
    allowed = load_allowed_recipient_digits()
    allow_err = check_whatsapp_recipient(jid, allowed_digits=allowed)
    if allow_err:
        return {"ok": False, "error": allow_err, "blocked": True, "group_jid": jid}

    cfg = WhatsappDigestConfig(recipient=jid, skip_context_guard=True)
    result = enqueue_whatsapp(cfg, message)
    result["group_jid"] = jid
    result["resolved_from"] = "group_memory_fallback"
    result["query"] = query
    return result


def _run_team_whatsapp_tool(
    args: dict[str, Any],
    *,
    ctx: "QclawChatToolContext",
) -> dict[str, Any]:
    """Handle qclaw_team_whatsapp tool calls via direct store access."""
    action = str(args.get("action") or "").strip()
    team_id = str(args.get("team_id") or "").strip()

    if action == "list":
        # Use team_list from context (direct store), then enrich with whatsapp numbers
        if ctx.team_list is None:
            return {"ok": False, "error": "team_list not configured"}
        teams_resp = ctx.team_list()
        teams = teams_resp.get("teams", [])
        if isinstance(teams, dict):
            teams = list(teams.values())
        results = []
        for t in teams:
            tid = t.get("id", "")
            tname = t.get("name", tid)
            # Get WhatsApp numbers directly from store
            nums: list = []
            if ctx.team_whatsapp_numbers_get is not None:
                wa_resp = ctx.team_whatsapp_numbers_get(tid)
                nums = wa_resp.get("whatsapp_numbers", []) if wa_resp.get("ok") else []
            else:
                # Fallback: check inline team data
                nums = t.get("whatsapp_numbers", [])
            results.append({"team_id": tid, "team_name": tname, "whatsapp_numbers": nums})
        return {"ok": True, "teams": results, "count": len(results)}

    if not team_id:
        return {"ok": False, "error": "team_id é obrigatório para esta ação"}

    if action == "get":
        if ctx.team_whatsapp_numbers_get is not None:
            return ctx.team_whatsapp_numbers_get(team_id)
        return {"ok": False, "error": "team_whatsapp_numbers_get not configured"}

    if action == "add":
        number = str(args.get("number") or "").strip()
        if not number:
            return {"ok": False, "error": "number é obrigatório para action=add"}
        if ctx.team_whatsapp_number_add is not None:
            return ctx.team_whatsapp_number_add(team_id, {"number": number})
        return {"ok": False, "error": "team_whatsapp_number_add not configured"}

    if action == "remove":
        number = str(args.get("number") or "").strip()
        if not number:
            return {"ok": False, "error": "number é obrigatório para action=remove"}
        if ctx.team_whatsapp_number_remove is not None:
            return ctx.team_whatsapp_number_remove(team_id, {"number": number})
        return {"ok": False, "error": "team_whatsapp_number_remove not configured"}

    if action == "set":
        numbers = args.get("numbers")
        if not isinstance(numbers, list):
            return {"ok": False, "error": "numbers (lista) é obrigatório para action=set"}
        if ctx.team_whatsapp_numbers_set is not None:
            return ctx.team_whatsapp_numbers_set(team_id, {"numbers": numbers})
        return {"ok": False, "error": "team_whatsapp_numbers_set not configured"}

    if action == "broadcast":
        message = str(args.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "message é obrigatório para action=broadcast"}
        if ctx.team_whatsapp_broadcast is not None:
            return ctx.team_whatsapp_broadcast(team_id, {"message": message})
        return {"ok": False, "error": "team_whatsapp_broadcast not configured"}

    if action == "send_to_group":
        message = str(args.get("message") or "").strip()
        file_path = str(args.get("file_path") or "").strip()
        caption = str(args.get("caption") or "").strip()
        if not message and not file_path:
            return {"ok": False, "error": "message ou file_path é obrigatório para action=send_to_group"}
        if ctx.team_whatsapp_send_to_group is not None:
            payload: dict[str, Any] = {}
            if message:
                payload["message"] = message
            if file_path:
                payload["file_path"] = file_path
            if caption:
                payload["caption"] = caption
            result = ctx.team_whatsapp_send_to_group(team_id, payload)
            return result
        return {"ok": False, "error": "team_whatsapp_send_to_group not configured"}

    if action == "get_emails":
        if ctx.team_notify_emails_get is not None:
            return ctx.team_notify_emails_get(team_id)
        return {"ok": False, "error": "team_notify_emails_get not configured"}

    if action == "set_emails":
        emails = args.get("emails")
        if not isinstance(emails, list):
            return {"ok": False, "error": "emails (lista) é obrigatório para action=set_emails"}
        if ctx.team_notify_emails_set is not None:
            return ctx.team_notify_emails_set(team_id, {"emails": emails})
        return {"ok": False, "error": "team_notify_emails_set not configured"}

    return {"ok": False, "error": f"action desconhecida: {action}"}


def _run_team_git_tool(
    args: dict[str, Any],
    *,
    ctx: "QclawChatToolContext",
) -> dict[str, Any]:
    """Handle qclaw_team_git tool calls (pull/push/status for team repos)."""
    from .team_git import run_team_git_action

    action = str(args.get("action") or "").strip()
    team_id = str(args.get("team_id") or "").strip()
    base_dir = str(args.get("base_dir") or "").strip() or None
    commit_message = str(args.get("commit_message") or "").strip() or None
    repo_name = str(args.get("repo_name") or "").strip() or None
    dry_run = bool(args.get("dry_run"))

    if ctx.team_list is None:
        return {"ok": False, "error": "team_list not configured"}

    teams_resp = ctx.team_list()
    teams = teams_resp.get("teams", [])
    if isinstance(teams, dict):
        teams = list(teams.values())

    return run_team_git_action(
        action,
        teams=teams,
        team_id=team_id,
        base_dir=base_dir,
        commit_message=commit_message,
        repo_name=repo_name,
        dry_run=dry_run,
    )


def _run_team_articles_tool(
    args: dict[str, Any],
    ctx: QclawChatToolContext,
) -> dict[str, Any]:
    """Lista ou busca artigos LaTeX indexados no ChromaDB do time."""
    from .team_context_store import TeamContextStore
    from .config import QclawConfig

    team_id = str(args.get("team_id") or "").strip()
    query = str(args.get("query") or "").strip()
    try:
        n_results = int(args.get("n_results") or 10)
    except (TypeError, ValueError):
        n_results = 10

    if not team_id:
        # Try to resolve from the recovery config (first team, or the only one)
        try:
            if ctx.team_list is not None:
                teams_resp = ctx.team_list()
                teams = (teams_resp or {}).get("teams") or []
                if teams:
                    team_id = str(teams[0].get("id") or "").strip()
        except Exception:
            pass

    if not team_id:
        return {
            "ok": True,
            "articles": [],
            "count": 0,
            "message": (
                "Nenhum time selecionado. Por favor, selecione um time no chat "
                "ou informe o team_id para buscar os artigos do projeto."
            ),
        }

    try:
        store = TeamContextStore()
        if query:
            items = store.query(
                team_id,
                query,
                n_results=n_results,
                where={"source": "latex_article"},
            )
            if not items:
                return {
                    "ok": True,
                    "team_id": team_id,
                    "query": query,
                    "articles": [],
                    "count": 0,
                    "message": (
                        "Nenhum artigo encontrado para essa busca. "
                        "Os artigos podem não ter sido indexados ainda — "
                        "use 'indexar artigos do time' primeiro."
                    ),
                }
            articles = []
            for item in items:
                meta = item.get("metadata") or {}
                articles.append({
                    "article_id": meta.get("article_id", item.get("id", "?")),
                    "title": meta.get("title", "?"),
                    "workspace_id": meta.get("workspace_id", ""),
                    "workspace_name": meta.get("workspace_name", ""),
                    "has_pdf": meta.get("has_pdf", False),
                    "tex_path": meta.get("tex_path", ""),
                    "relevance_score": round(1 - float(item.get("distance") or 0), 3),
                    "excerpt": str(item.get("document") or "")[:300],
                })
            return {"ok": True, "team_id": team_id, "query": query, "articles": articles, "count": len(articles)}
        else:
            items = store.list_documents(
                team_id,
                limit=n_results,
                where={"source": "latex_article"},
            )
            if not items:
                return {
                    "ok": True,
                    "team_id": team_id,
                    "articles": [],
                    "count": 0,
                    "message": (
                        "Nenhum artigo indexado para este time. "
                        "Use 'indexar artigos do workspace X para o time Y' para indexar."
                    ),
                }
            articles = []
            for item in items:
                meta = item.get("metadata") or {}
                articles.append({
                    "article_id": meta.get("article_id", item.get("id", "?")),
                    "title": meta.get("title", "?"),
                    "workspace_id": meta.get("workspace_id", ""),
                    "workspace_name": meta.get("workspace_name", ""),
                    "has_pdf": meta.get("has_pdf", False),
                    "tex_path": meta.get("tex_path", ""),
                })
            return {"ok": True, "team_id": team_id, "articles": articles, "count": len(articles)}
    except Exception as exc:
        return {"ok": False, "error": f"Falha ao acessar artigos do time: {exc}"}


def _run_remove_team_articles_tool(
    args: dict[str, Any],
    ctx: QclawChatToolContext,
) -> dict[str, Any]:
    """Remove artigos LaTeX de um workspace do ChromaDB do time."""
    from .latex_articles import remove_team_articles

    workspace_id = str(args.get("workspace_id") or "").strip()
    team_id = str(args.get("team_id") or "").strip()

    if not workspace_id:
        return {"ok": False, "error": "workspace_id é obrigatório"}

    if not team_id:
        try:
            if ctx.team_list is not None:
                teams_resp = ctx.team_list()
                teams = (teams_resp or {}).get("teams") or []
                if teams:
                    team_id = str(teams[0].get("id") or "").strip()
        except Exception:
            pass

    if not team_id:
        return {"ok": False, "error": "team_id é obrigatório — informe o time"}

    try:
        result = remove_team_articles(workspace_id=workspace_id, team_id=team_id)
        if result.get("ok"):
            removed = result.get("removed", 0)
            result["message"] = f"{removed} artigo(s) desvinculado(s) do time."
        return result
    except Exception as exc:
        return {"ok": False, "error": f"Falha ao desvincular artigos: {exc}"}


def _run_article_images_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Lista, exibe ou altera figuras de artigos LaTeX."""
    from .article_images import dispatch_article_images

    try:
        return dispatch_article_images(args)
    except Exception as exc:
        return {"ok": False, "error": f"Falha em article_images: {exc}"}


def _run_team_article_pdf_tool(
    args: dict[str, Any],
    ctx: QclawChatToolContext,
) -> dict[str, Any]:
    """Compila artigo LaTeX e salva PDF na pasta do time."""
    from .team_article_pdf import dispatch_team_article_pdf

    team_id = str(args.get("team_id") or "").strip()
    if not team_id:
        try:
            if ctx.team_list is not None:
                teams_resp = ctx.team_list()
                teams = (teams_resp or {}).get("teams") or []
                if teams:
                    team_id = str(teams[0].get("id") or "").strip()
        except Exception:
            pass
    if team_id and not args.get("team_id"):
        args = {**args, "team_id": team_id}

    try:
        return dispatch_team_article_pdf(args)
    except Exception as exc:
        return {"ok": False, "error": f"Falha em team_article_pdf: {exc}"}


def _run_index_team_articles_tool(
    args: dict[str, Any],
    ctx: QclawChatToolContext,
) -> dict[str, Any]:
    """Indexa artigos LaTeX de um workspace no ChromaDB do time."""
    from .latex_articles import store_team_articles

    workspace_id = str(args.get("workspace_id") or "").strip()
    team_id = str(args.get("team_id") or "").strip()

    if not workspace_id:
        return {"ok": False, "error": "workspace_id é obrigatório"}

    if not team_id:
        try:
            if ctx.team_list is not None:
                teams_resp = ctx.team_list()
                teams = (teams_resp or {}).get("teams") or []
                if teams:
                    team_id = str(teams[0].get("id") or "").strip()
        except Exception:
            pass

    if not team_id:
        return {"ok": False, "error": "team_id é obrigatório — informe o time destino"}

    try:
        result = store_team_articles(workspace_id=workspace_id, team_id=team_id)
        if result.get("ok"):
            added = result.get("added", 0)
            total = result.get("total", 0)
            errors = result.get("errors") or []
            msg = f"{added} artigo(s) indexado(s) de {total} encontrado(s) no workspace."
            if errors:
                msg += f" {len(errors)} erro(s): {'; '.join(errors[:3])}"
            result["message"] = msg
        return result
    except Exception as exc:
        return {"ok": False, "error": f"Falha ao indexar artigos: {exc}"}


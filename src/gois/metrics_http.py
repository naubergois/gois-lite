"""HTTP/WSGI server and dashboard routes for gois metrics."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
from datetime import datetime
from socketserver import ThreadingMixIn
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import parse_qsl, quote, unquote, unquote_plus
from wsgiref.simple_server import WSGIServer
from prometheus_client import CollectorRegistry, Counter, Gauge, make_wsgi_app
from .gois_lite import is_gois_lite, lite_redirect_for_html_path

if is_gois_lite():
    from .dashboard import build_chat_html, build_kanban_html, build_login_html
    from .mcp_cards_page import build_mcp_cards_html
else:
    from .a2a_server import build_agent_card, parse_and_handle as a2a_parse_and_handle
    from .agent_create_page import build_agent_create_html
    from .cron_create_page import build_cron_create_html
    from .ide_page import build_ide_html
    from . import ide_runtime
    from .entity_db_page import build_entity_db_html
    from .knowledge_page import build_knowledge_html
    from .project_memory_page import build_project_memory_html
    from .projects_page import build_projects_html
    from .mcp_cards_page import build_mcp_cards_html
    from .mcp_servers_page import build_mcp_servers_html
    from .calendar_api import (
        calendar_config,
        calendar_status,
        create_event as calendar_create_event,
        list_events as calendar_list_events,
        notion_configure,
        notion_status,
        notion_sync,
        sync_google as calendar_sync_google,
    )
    from .calendar_page import build_calendar_html
    from .dashboard import (
        build_active_agents_html,
        build_allowlist_html,
        build_env_keys_html,
        build_agenda_html,
        build_chat_html,
        build_dashboard_html,
        build_cron_costs_html,
        build_cron_slots_html,
        build_errors_html,
        build_health_html,
        build_kanban_html,
        build_login_html,
        build_metrics_html,
        build_roles_html,
        build_users_html,
        build_ruflo_chat_html,
        build_perguntas_html,
        build_skills_html,
        build_swarm_html,
        build_swarm_profiles_html,
        build_team_create_html,
        build_teams_html,
    )
    from .latex_dashboard import build_latex_html
    from .manage_delete_page import build_manage_delete_html
    from .model_usage_page import build_model_costs_html, build_model_quotas_html
    from .team_detail_page import build_team_detail_html
    from .team_messages_page import build_team_messages_html
    from .ruflo_engines_page import build_ruflo_engines_html
    from .article_quality_page import build_article_quality_html
    from .team_file_preview_popup_page import build_team_file_preview_popup_html

if TYPE_CHECKING:
    from .metrics import Metrics

log = logging.getLogger(__name__)


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _wants_metrics_html(environ: dict) -> bool:
    """True when the client is a browser (not a Prometheus scraper)."""
    accept = (environ.get("HTTP_ACCEPT") or "").lower()
    if not accept:
        return False
    if "application/openmetrics-text" in accept:
        return False
    if "text/plain;version=0.0.4" in accept and "text/html" not in accept:
        return False
    return "text/html" in accept


_COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
    "application/openmetrics-text",
)
_GZIP_MIN_BYTES = 512
_ETAG_MAX_BYTES = 2 * 1024 * 1024


def _format_content_disposition(filename: str, *, inline: bool = False) -> str:
    """Build a WSGI-safe Content-Disposition value (RFC 5987 filename*)."""
    name = re.sub(r"[\r\n\x00]", "", str(filename or "file").strip()) or "file"
    ascii_name = name.encode("ascii", "replace").decode("ascii")
    ascii_name = re.sub(r'["\\]', "'", ascii_name)
    utf8_name = quote(name, safe="")
    kind = "inline" if inline else "attachment"
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _is_download_preview(qp: dict[str, str]) -> bool:
    return str(qp.get("preview") or "").strip().lower() in ("1", "true", "yes")


def _team_file_download_headers(
    data_bytes: bytes, mime: str, fname: str, *, preview: bool
) -> list[tuple[str, str]]:
    disp = _format_content_disposition(fname, inline=preview)
    ctype = (mime or "application/octet-stream") if preview else "application/octet-stream"
    return [
        ("Content-Type", ctype),
        ("Content-Length", str(len(data_bytes))),
        ("Content-Disposition", disp),
        ("Cache-Control", "private, max-age=3600"),
    ]


def _artifact_download_headers(
    data_bytes: bytes, mime: str, fname: str, *, preview: bool
) -> list[tuple[str, str]]:
    disp = _format_content_disposition(fname, inline=preview)
    ctype = (mime or "application/octet-stream") if preview else "application/octet-stream"
    return [
        ("Content-Type", ctype),
        ("Content-Length", str(len(data_bytes))),
        ("Content-Disposition", disp),
        ("Cache-Control", "private, max-age=86400"),
    ]


def _wsgi_path_segment(segment: str) -> str:
    """Decode a PATH_INFO segment that may carry UTF-8 bytes as latin-1.

    PEP 3333 servers (wsgiref included) unquote the request path and decode
    it as latin-1, so a slug like "ítalo" arrives as mojibake ("Ã­talo").
    Recover the original UTF-8 text; fall back to the raw value.
    """
    text = unquote(segment)
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _gzip_etag_middleware(app):
    """Add gzip compression and ETag/304 to a WSGI app.

    Buffers each response, so it is intended for the small JSON/HTML/metrics
    payloads served here, not for streaming endpoints.
    """

    def wrapped(environ, start_response):
        captured: dict = {}

        def _capture(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(headers)
            return lambda data: None

        body_iter = app(environ, _capture)

        # Streaming passthrough: SSE (text/event-stream) endpoints call
        # start_response synchronously and then return a generator that yields
        # frames over time. Buffering the whole body (below) would defeat the
        # purpose, so forward the iterator unbuffered, dropping hop-by-hop and
        # Content-Length headers (wsgiref flushes each yielded chunk).
        headers0 = captured.get("headers", [])
        ct0 = ""
        for k, v in headers0:
            if k.lower() == "content-type":
                ct0 = v.lower()
                break
        if ct0.startswith("text/event-stream"):
            _hop = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
                "content-length",
            }
            stream_headers = [(k, v) for k, v in headers0 if k.lower() not in _hop]
            start_response(captured.get("status", "200 OK"), stream_headers)
            return body_iter

        try:
            body = b"".join(body_iter)
        finally:
            close = getattr(body_iter, "close", None)
            if close:
                close()

        status = captured.get("status", "200 OK")
        headers = captured.get("headers", [])

        ct = ""
        for k, v in headers:
            if k.lower() == "content-type":
                ct = v.lower()
                break

        compressible = any(ct.startswith(p) for p in _COMPRESSIBLE_PREFIXES)
        accepts_gzip = "gzip" in (environ.get("HTTP_ACCEPT_ENCODING") or "").lower()
        is_2xx = status[:1] == "2"
        # Dynamic JSON APIs and binary downloads must not short-circuit to 304 —
        # browsers may fail to rehydrate the body (Safari: "Load failed" / empty JSON).
        skip_etag = ct.startswith("application/json") or ct.startswith((
            "application/epub",
            "application/octet-stream",
            "application/zip",
            "application/pdf",
        ))

        etag = None
        if not skip_etag and is_2xx and compressible and len(body) <= _ETAG_MAX_BYTES:
            etag = '"' + hashlib.sha1(body).hexdigest() + '"'
            inm = environ.get("HTTP_IF_NONE_MATCH") or ""
            if inm and etag in (s.strip() for s in inm.split(",")):
                headers_304 = [(k, v) for k, v in headers if k.lower() not in {
                    "content-length", "content-encoding", "content-type"
                }]
                headers_304.append(("ETag", etag))
                start_response("304 Not Modified", headers_304)
                return [b""]

        if (
            is_2xx
            and compressible
            and accepts_gzip
            and len(body) >= _GZIP_MIN_BYTES
        ):
            body = gzip.compress(body, compresslevel=5)
            headers = [
                (k, v) for k, v in headers
                if k.lower() not in {"content-length", "content-encoding"}
            ]
            headers.append(("Content-Encoding", "gzip"))
            headers.append(("Vary", "Accept-Encoding"))

        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(len(body))))
        if etag is not None:
            headers = [(k, v) for k, v in headers if k.lower() != "etag"]
            headers.append(("ETag", etag))

        start_response(status, headers)
        return [body]

    return wrapped


def run_http_server(
    host: str,
    port: int,
    metrics: "Metrics",
    status_provider: Callable[[], dict],
    auth_bootstrap: Optional[Callable[[], dict]] = None,
    auth_user_from_token: Optional[Callable[[Optional[str]], object]] = None,
    auth_register: Optional[Callable[[dict], dict]] = None,
    auth_login: Optional[Callable[[dict], dict]] = None,
    auth_me: Optional[Callable[[object], dict]] = None,
    teams_list: Optional[Callable[[object], dict]] = None,
    teams_presets: Optional[Callable[[object], dict]] = None,
    team_create: Optional[Callable[[dict, object], dict]] = None,
    team_update: Optional[Callable[[str, dict, object], dict]] = None,
    team_delete: Optional[Callable[[str, object], dict]] = None,
    team_members_add: Optional[Callable[[str, str, object], dict]] = None,
    team_members_remove: Optional[Callable[[str, str, object], dict]] = None,
    team_members_list: Optional[Callable[[str, object], dict]] = None,
    team_contacts_get: Optional[Callable[[str, object], dict]] = None,
    team_contacts_upsert: Optional[Callable[[str, dict, object], dict]] = None,
    team_contacts_remove: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_get: Optional[Callable[[str, object], dict]] = None,
    team_normas_upload: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_delete: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_download: Optional[Callable[[str, dict, object], object]] = None,
    team_normas_from_file: Optional[Callable[[str, dict, object], dict]] = None,
    team_card_from_file: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_from_whatsapp: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_whatsapp_preview: Optional[Callable[[str, dict, object], dict]] = None,
    team_normas_preview: Optional[Callable[[str, dict, object], dict]] = None,
    team_legal_evaluations_list: Optional[Callable[[str, dict, object], dict]] = None,
    team_legal_evaluations_search: Optional[Callable[[str, dict, object], dict]] = None,
    team_legal_evaluation_get: Optional[Callable[[str, str, object], dict]] = None,
    team_legal_evaluation_save: Optional[Callable[[str, dict, object], dict]] = None,
    team_articles_folder_upload: Optional[Callable[[str, dict, object], dict]] = None,
    team_kanban_get: Optional[Callable[[str, object], dict]] = None,
    team_kanban_save: Optional[Callable[[str, dict, object], dict]] = None,
    team_swarm_run: Optional[Callable[[str, dict, object], dict]] = None,
    team_context_get: Optional[Callable[[str, object], dict]] = None,
    auth_logout: Optional[Callable[[Optional[str]], dict]] = None,
    users_list: Optional[Callable[[object], dict]] = None,
    users_create: Optional[Callable[[dict, object], dict]] = None,
    users_delete: Optional[Callable[[str, object], dict]] = None,
    users_update: Optional[Callable[[str, dict, object], dict]] = None,
    agent_action: Optional[Callable[[str, str], dict]] = None,
    hermes_dashboard_url: Optional[str] = None,
    dashboard_render_config: Optional[dict] = None,
    ruflo_user_checkin_interval_seconds: float = 5.0,
    hermes_agent_create: Optional[Callable[[dict, object], dict]] = None,
    openai_swarm_create: Optional[Callable[[dict, object], dict]] = None,
    openai_swarm_list: Optional[Callable[[], list]] = None,
    swarm_presets: Optional[Callable[[object], dict]] = None,
    swarm_graph_run: Optional[Callable[[dict, object], dict]] = None,
    swarm_graph_preview: Optional[Callable[[dict, object], dict]] = None,
    swarm_graph_resume: Optional[Callable[[dict, object], dict]] = None,
    swarm_graph_overview: Optional[Callable[[object], dict]] = None,
    swarm_executions: Optional[Callable[[dict, object], dict]] = None,
    swarm_robots_snapshot: Optional[Callable[..., dict]] = None,
    swarm_robots_snapshot_stream: Optional[Callable[[object], Any]] = None,
    swarm_activity_changes: Optional[Callable[[dict, object], dict]] = None,
    swarm_agent_history: Optional[Callable[..., dict]] = None,
    swarm_skills_list: Optional[Callable[[], dict]] = None,
    swarm_teams_board: Optional[Callable[[object], dict]] = None,
    swarm_teams_claim: Optional[Callable[[dict, object], dict]] = None,
    swarm_robot_create: Optional[Callable[[dict, object], dict]] = None,
    swarm_robot_get: Optional[Callable[[str, object], dict]] = None,
    swarm_robot_source: Optional[Callable[[str, object], dict]] = None,
    swarm_robot_update: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_robot_delete: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_execution_cancel: Optional[Callable[[dict], dict]] = None,
    swarm_create: Optional[Callable[[dict, object], dict]] = None,
    swarm_update: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_delete: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_health: Optional[Callable[[str, object], dict]] = None,
    swarm_health_fix: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_schedule: Optional[Callable[[str, dict, object], dict]] = None,
    swarm_model: Optional[Callable[[str, dict, object], dict]] = None,
    hermes_profiles_list: Optional[Callable[..., dict]] = None,
    hermes_profile_delete: Optional[Callable[[str], dict]] = None,
    hermes_role_presets: Optional[Callable[[], dict]] = None,
    hermes_profile_generate_personality: Optional[Callable[[dict, object], dict]] = None,
    hermes_roles_seed: Optional[Callable[[dict, object], dict]] = None,
    hermes_roles_seed_status: Optional[Callable[[], dict]] = None,
    hermes_mascots_list: Optional[Callable[[], dict]] = None,
    hermes_local_folders: Optional[Callable[[dict], dict]] = None,
    hermes_skills_list: Optional[Callable[[], dict]] = None,
    hermes_kanban_projects: Optional[Callable[[object], dict]] = None,
    hermes_kanban_get: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_source: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_action: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_schedule: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_schedule_status: Optional[Callable[[dict], dict]] = None,
    hermes_kanban_pm_analysis: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_task_history: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_requirements: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_attachment_upload: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_attachment_upload_binary: Optional[
        Callable[[dict, bytes, object], dict]
    ] = None,
    hermes_kanban_attachment_get: Optional[Callable[[dict, object], Any]] = None,
    hermes_kanban_attachments_zip_get: Optional[Callable[[dict, object], Any]] = None,
    hermes_kanban_attachment_delete: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_attachment_move: Optional[Callable[[dict, object], dict]] = None,
    hermes_kanban_attachment_copy: Optional[Callable[[dict, object], dict]] = None,
    hermes_cron_list: Optional[Callable[[dict], dict]] = None,
    hermes_cron_swarm_timeline: Optional[Callable[[dict], dict]] = None,
    hermes_cron_catch_up: Optional[Callable[[dict], dict]] = None,
    hermes_cron_resume_all: Optional[Callable[[], dict]] = None,
    hermes_cron_force_remove: Optional[Callable[[dict], dict]] = None,
    hermes_cron_force_remove_all: Optional[Callable[[], dict]] = None,
    hermes_cron_get: Optional[Callable[[str], dict]] = None,
    hermes_cron_edit: Optional[Callable[[str, dict], dict]] = None,
    hermes_cron_action: Optional[Callable[[str, str], dict]] = None,
    hermes_cron_result: Optional[Callable[..., dict]] = None,
    hermes_cron_source: Optional[Callable[[str], dict]] = None,
    hermes_cron_create: Optional[Callable[[dict], dict]] = None,
    hermes_cron_preview: Optional[Callable[[dict], dict]] = None,
    knowledge_get: Optional[Callable[[dict], dict]] = None,
    entity_db_get: Optional[Callable[[dict], dict]] = None,
    knowledge_extract: Optional[Callable[[dict], dict]] = None,
    knowledge_delete_project: Optional[Callable[[dict], dict]] = None,
    knowledge_delete_session: Optional[Callable[[dict], dict]] = None,
    hermes_recovery_status: Optional[Callable[[], dict]] = None,
    hermes_recovery_action: Optional[Callable[[dict], dict]] = None,
    hermes_cron_diagnose: Optional[Callable[[dict], dict]] = None,
    hermes_cron_diagnose_action: Optional[Callable[[dict], dict]] = None,
    openclaw_sessions_list: Optional[Callable[[dict], dict]] = None,
    openclaw_session_new: Optional[Callable[[dict, object], dict]] = None,
    openclaw_session_delete: Optional[Callable[[dict, object], dict]] = None,
    openclaw_sessions_purge: Optional[Callable[[dict, object], dict]] = None,
    chat_assign_team: Optional[Callable[[dict, object], dict]] = None,
    chat_session_swarm: Optional[Callable[[dict, object], dict]] = None,
    openclaw_messages_get: Optional[Callable[[dict], dict]] = None,
    openclaw_skills_list: Optional[Callable[[dict], dict]] = None,
    openclaw_integration_get: Optional[Callable[[dict], dict]] = None,
    openclaw_tools_list: Optional[Callable[[dict], dict]] = None,
    openclaw_skill_dirs_add: Optional[Callable[[dict], dict]] = None,
    openclaw_chat_failures: Optional[Callable[[dict], dict]] = None,
    openclaw_models_list: Optional[Callable[[Optional[dict]], dict]] = None,
    openclaw_models_catalog: Optional[Callable[[dict], dict]] = None,
    openclaw_models_sync: Optional[Callable[[], dict]] = None,
    openclaw_replicate_models: Optional[Callable[[dict], dict]] = None,
    openclaw_image_models_catalog: Optional[Callable[[dict], dict]] = None,
    openclaw_replicate_generate: Optional[Callable[[dict], dict]] = None,
    openclaw_chat_widgets_action: Optional[Callable[[dict], dict]] = None,
    openclaw_chat_epub_reader: Optional[Callable[[dict], dict]] = None,
    openclaw_chat_book_image: Optional[Callable[[dict], Any]] = None,
    openclaw_chat_course_image: Optional[Callable[[dict], Any]] = None,
    codex_auth_status: Optional[Callable[[], dict]] = None,
    codex_login_start: Optional[Callable[[], dict]] = None,
    codex_login_wait: Optional[Callable[[dict], dict]] = None,
    openclaw_connection_get: Optional[Callable[[], dict]] = None,
    openclaw_connection_set: Optional[Callable[[dict], dict]] = None,
    openclaw_fastpath_get: Optional[Callable[[], dict]] = None,
    openclaw_fastpath_set: Optional[Callable[[dict], dict]] = None,
    openclaw_token_mode_get: Optional[Callable[[], dict]] = None,
    openclaw_token_mode_set: Optional[Callable[[dict], dict]] = None,
    openclaw_tool_limit_get: Optional[Callable[[], dict]] = None,
    openclaw_tool_limit_set: Optional[Callable[[dict], dict]] = None,
    openclaw_tools_cap_get: Optional[Callable[[], dict]] = None,
    openclaw_tools_cap_set: Optional[Callable[[dict], dict]] = None,
    openclaw_attachment_upload: Optional[Callable[[dict, object], dict]] = None,
    openclaw_visual_memory_from_photo: Optional[Callable[[dict, object], dict]] = None,
    openclaw_profile_from_photo: Optional[Callable[[dict, object], dict]] = None,
    openclaw_visual_memory_profiles: Optional[Callable[[dict, object], dict]] = None,
    openclaw_visual_memory_preview: Optional[Callable[[dict, object], dict]] = None,
    openclaw_generated_images_list: Optional[Callable[[dict, object], dict]] = None,
    openclaw_generated_images_delete: Optional[Callable[[dict, object], dict]] = None,
    openclaw_minicurriculum_html: Optional[Callable[[dict, object], tuple]] = None,
    openclaw_context_documents: Optional[Callable[[dict, object], dict]] = None,
    openclaw_session_context_get: Optional[Callable[[dict, object], dict]] = None,
    openclaw_artifact_download: Optional[Callable[[dict, object], Any]] = None,
    chat_tts: Optional[Callable[[dict], dict]] = None,
    chat_stt: Optional[Callable[[dict], dict]] = None,
    openclaw_send: Optional[Callable[[dict], dict]] = None,
    openclaw_send_status: Optional[Callable[[dict], dict]] = None,
    openclaw_send_cancel: Optional[Callable[[dict], dict]] = None,
    openclaw_send_pending: Optional[Callable[[], dict]] = None,
    openclaw_send_queue: Optional[Callable[[], dict]] = None,
    openclaw_session_presence: Optional[Callable[[dict], dict]] = None,
    openclaw_tool_jobs: Optional[Callable[[dict], dict]] = None,
    openclaw_jobs_running: Optional[Callable[[], dict]] = None,
    openclaw_jobs_cancel: Optional[Callable[[dict], dict]] = None,
    openclaw_books_library: Optional[Callable[[dict], dict]] = None,
    openclaw_articles_library: Optional[Callable[[dict], dict]] = None,
    openclaw_article_delete: Optional[Callable[..., dict]] = None,
    openclaw_book_structure: Optional[Callable[[dict], dict]] = None,
    openclaw_book_cover: Optional[Callable[[str], dict]] = None,
    openclaw_book_epub: Optional[Callable[[str], dict]] = None,
    openclaw_book_delete: Optional[Callable[..., dict]] = None,
    openclaw_courses_library: Optional[Callable[[dict], dict]] = None,
    openclaw_course_structure: Optional[Callable[[dict], dict]] = None,
    openclaw_course_html_site: Optional[Callable[[str], dict]] = None,
    openclaw_course_delete: Optional[Callable[..., dict]] = None,
    openclaw_sagas_library: Optional[Callable[[dict], dict]] = None,
    openclaw_saga_structure: Optional[Callable[[dict], dict]] = None,
    openclaw_saga_delete: Optional[Callable[..., dict]] = None,
    openclaw_story_delete: Optional[Callable[..., dict]] = None,
    openclaw_page_delete: Optional[Callable[..., dict]] = None,
    openclaw_magazine_delete: Optional[Callable[..., dict]] = None,
    openclaw_scripts_library: Optional[Callable[[dict], dict]] = None,
    openclaw_user_profile_dock: Optional[Callable[[dict], dict]] = None,
    openclaw_user_profile_dock_generate: Optional[Callable[[dict], dict]] = None,
    openclaw_user_profile_dock_remove_photo: Optional[Callable[[dict], dict]] = None,
    openclaw_script_structure: Optional[Callable[[dict], dict]] = None,
    openclaw_script_delete: Optional[Callable[..., dict]] = None,
    openclaw_script_act_delete: Optional[Callable[..., dict]] = None,
    openclaw_script_act_image: Optional[Callable[[dict], dict]] = None,
    openclaw_gallery_image_route: Optional[Callable[[dict], dict]] = None,
    active_agents_snapshot: Optional[Callable[[], dict]] = None,
    openclaw_memory_list: Optional[Callable[[dict, object], dict]] = None,
    openclaw_project_memory_get: Optional[Callable[[dict, object], dict]] = None,
    openclaw_project_memory_put: Optional[Callable[[dict, object], dict]] = None,
    ruflo_status: Optional[Callable[[], dict]] = None,
    ruflo_sessions_list: Optional[Callable[[dict, object], dict]] = None,
    ruflo_session_new: Optional[Callable[[dict, object], dict]] = None,
    ruflo_session_delete: Optional[Callable[[dict, object], dict]] = None,
    ruflo_messages_get: Optional[Callable[[dict, object], dict]] = None,
    ruflo_route_preview: Optional[Callable[[dict], dict]] = None,
    ruflo_send: Optional[Callable[[dict, object], dict]] = None,
    ruflo_motores_status: Optional[Callable[[], dict]] = None,
    ruflo_motores_start: Optional[Callable[[], dict]] = None,
    ruflo_motores_stop: Optional[Callable[[], dict]] = None,
    ruflo_motores_rebuild: Optional[Callable[[], dict]] = None,
    errors_list: Optional[Callable[[dict], dict]] = None,
    cron_concurrency_status: Optional[Callable[[dict], dict]] = None,
    cron_concurrency_release: Optional[Callable[[dict], dict]] = None,
    model_quotas_get: Optional[Callable[[object], dict]] = None,
    model_quotas_update: Optional[Callable[[dict, object], dict]] = None,
    env_keys_get: Optional[Callable[[object], dict]] = None,
    env_keys_update: Optional[Callable[[dict, object], dict]] = None,
    env_keys_import: Optional[Callable[[dict, object], dict]] = None,
    env_keys_import_roteiro_viral: Optional[Callable[[dict, object], dict]] = None,
    env_keys_prune: Optional[Callable[[dict, object], dict]] = None,
    wacli_auth_status: Optional[Callable[[], dict]] = None,
    wacli_auth_qr: Optional[Callable[[], dict]] = None,
    wacli_auth_wait: Optional[Callable[[dict], dict]] = None,
    wacli_unlock: Optional[Callable[[dict], dict]] = None,
    whatsapp_send: Optional[Callable[[dict], dict]] = None,
    whatsapp_queue: Optional[Callable[[], dict]] = None,
    whatsapp_inbound: Optional[Callable[[dict, bytes, Optional[str]], dict]] = None,
    whatsapp_status: Optional[Callable[[], dict]] = None,
    team_whatsapp_numbers_get: Optional[Callable[[str, object], dict]] = None,
    team_whatsapp_numbers_set: Optional[Callable[[str, dict, object], dict]] = None,
    team_whatsapp_number_add: Optional[Callable[[str, dict, object], dict]] = None,
    team_whatsapp_number_remove: Optional[Callable[[str, dict, object], dict]] = None,
    team_whatsapp_send: Optional[Callable[[str, dict, object], dict]] = None,
    team_whatsapp_broadcast: Optional[Callable[[str, dict, object], dict]] = None,
    team_whatsapp_send_to_group: Optional[Callable[[str, dict, object], dict]] = None,
    skill_suggestions_list: Optional[Callable[[], dict]] = None,
    skill_suggestions_scan: Optional[Callable[[], dict]] = None,
    skill_suggestions_dismiss: Optional[Callable[[dict], dict]] = None,
    mcp_skills: Optional[Callable[..., dict]] = None,
    mcp_cards: Optional[Callable[..., dict]] = None,
    whatsapp_allowlist_list: Optional[Callable[[], dict]] = None,
    whatsapp_allowlist_add: Optional[Callable[[dict], dict]] = None,
    whatsapp_allowlist_update: Optional[Callable[[dict], dict]] = None,
    whatsapp_allowlist_remove: Optional[Callable[[dict], dict]] = None,
    whatsapp_allowlist_toggle: Optional[Callable[[dict], dict]] = None,
    whatsapp_allowlist_contacts: Optional[Callable[[str], dict]] = None,
    whatsapp_agenda_list: Optional[Callable[[dict], dict]] = None,
    whatsapp_agenda_sync: Optional[Callable[[], dict]] = None,
    whatsapp_agenda_stats: Optional[Callable[[], dict]] = None,
    whatsapp_messages_search: Optional[Callable[[dict], dict]] = None,
    whatsapp_norma_candidates: Optional[Callable[[dict], dict]] = None,
    team_group_messages_get: Optional[Callable[[str, dict, object], dict]] = None,
    team_email_messages_get: Optional[Callable[[str, dict, object], dict]] = None,
    team_email_sync: Optional[Callable[[str, dict, object], dict]] = None,
    team_email_send: Optional[Callable[[str, dict, object], dict]] = None,
    latex_workspaces_list: Optional[Callable[[object], dict]] = None,
    latex_workspace_register: Optional[Callable[[dict, object], dict]] = None,
    latex_workspace_remove: Optional[Callable[[dict, object], dict]] = None,
    latex_articles_list: Optional[Callable[[dict, object], dict]] = None,
    latex_files_list: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_files_list: Optional[Callable[[dict, object], dict]] = None,
    latex_bib_files_list: Optional[Callable[[dict, object], dict]] = None,
    latex_file_delete: Optional[Callable[[dict, object], dict]] = None,
    latex_file_upload: Optional[Callable[[dict, object], dict]] = None,
    latex_file_get: Optional[Callable[[dict, object], dict]] = None,
    latex_file_save: Optional[Callable[[dict, object], dict]] = None,
    latex_improve_ops: Optional[Callable[[dict, object], dict]] = None,
    latex_improvements_list: Optional[Callable[[dict, object], dict]] = None,
    latex_improvements_history: Optional[Callable[[dict, object], dict]] = None,
    latex_improvements_resolve: Optional[Callable[[dict, object], dict]] = None,
    latex_improvements_evaluate: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_improve: Optional[Callable[[dict, object], dict]] = None,
    latex_compile: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_get: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_save: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_version_save: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_version_list: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_version_get: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_version_restore: Optional[Callable[[dict, object], dict]] = None,
    latex_objectives_md: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_chat: Optional[Callable[[dict, object], dict]] = None,
    latex_objectives_suggest: Optional[Callable[[dict, object], dict]] = None,
    latex_objectives_get: Optional[Callable[[dict, object], dict]] = None,
    latex_objectives_save: Optional[Callable[[dict, object], dict]] = None,
    attendance_get: Optional[Callable[[dict, object], dict]] = None,
    attendance_save: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_apply_template: Optional[Callable[[dict, object], dict]] = None,
    latex_tex_restore_template: Optional[Callable[[dict, object], dict]] = None,
    latex_pdf_path: Optional[Callable[[dict, object], tuple]] = None,
    latex_figure_path: Optional[Callable[[dict, object], tuple]] = None,
    latex_quality_list: Optional[Callable[[dict, object], dict]] = None,
    latex_quality_detail: Optional[Callable[[dict, object], dict]] = None,
    latex_templates_list: Optional[Callable[[dict, object], dict]] = None,
    latex_template_upload: Optional[Callable[[dict, object], dict]] = None,
    latex_template_delete: Optional[Callable[[dict, object], dict]] = None,
    latex_template_apply: Optional[Callable[[dict, object], dict]] = None,
    latex_template_get: Optional[Callable[[dict, object], dict]] = None,
    latex_template_preview: Optional[Callable[[dict, object], tuple]] = None,
    alerting_clear: Optional[Callable[[], dict]] = None,
    priority_queue_get: Optional[Callable[[], dict]] = None,
    priority_queue_post: Optional[Callable[[dict, object], dict]] = None,
    **_extra_kwargs,
) -> Optional[_ThreadingWSGIServer]:
    prom_app = make_wsgi_app(metrics.registry)

    if is_gois_lite():
        login_body = build_login_html().encode("utf-8")
        kanban_body = build_kanban_html(hermes_dashboard_url).encode("utf-8")
        html_body = login_body
        _empty = b""
        ruflo_monitor_body = _empty
        ruflo_chat_body = _empty
        perguntas_body = _empty
        errors_body = _empty
        health_body = _empty
        status_panel_body = _empty
        roles_body = _empty
        agent_create_body = _empty
        cron_create_body = _empty
        ide_body = _empty
        knowledge_body = _empty
        entity_db_body = _empty
        project_memory_body = _empty
        teams_ui_body = _empty
        team_create_body = _empty
        team_messages_body = _empty
        team_detail_body = _empty
        priority_queue_body = _empty
        skills_body = _empty
        allowlist_body = _empty
        env_keys_body = _empty
        agenda_body = _empty
        users_body = _empty
        metrics_ui_body = _empty
        latex_body = _empty
        article_quality_body = _empty
        active_agents_body = _empty
        swarm_body = _empty
        swarm_profiles_body = _empty
        cron_slots_body = _empty
        cron_costs_body = _empty
        model_quotas_body = _empty
        model_costs_body = _empty
        manage_delete_body = _empty
        projects_body = _empty
        mcp_cards_body = build_mcp_cards_html(hermes_dashboard_url).encode("utf-8")
        mcp_servers_body = _empty
        calendar_body = _empty
        ruflo_engines_body = _empty
    else:
        html_body = build_dashboard_html(
            hermes_dashboard_url,
            dashboard_render_config=dashboard_render_config,
        ).encode("utf-8")
        ruflo_monitor_body = build_dashboard_html(
            hermes_dashboard_url,
            active_nav="ruflo_results",
            default_hermes_tab="ruflo",
            dashboard_render_config=dashboard_render_config,
        ).encode("utf-8")
        ruflo_chat_body = build_ruflo_chat_html(
            hermes_dashboard_url,
            user_checkin_interval_seconds=ruflo_user_checkin_interval_seconds,
        ).encode("utf-8")
        perguntas_body = build_perguntas_html(hermes_dashboard_url).encode("utf-8")
        errors_body = build_errors_html(hermes_dashboard_url).encode("utf-8")
        health_body = build_health_html(hermes_dashboard_url).encode("utf-8")
        from .status_panel_page import build_status_panel_html

        status_panel_body = build_status_panel_html(
            monitor_base=f"http://127.0.0.1:{port}",
        ).encode("utf-8")
        login_body = build_login_html().encode("utf-8")
        roles_body = build_roles_html(hermes_dashboard_url).encode("utf-8")
        agent_create_body = build_agent_create_html(hermes_dashboard_url).encode("utf-8")
        cron_create_body = build_cron_create_html(hermes_dashboard_url).encode("utf-8")
        ide_body = build_ide_html(hermes_dashboard_url).encode("utf-8")
        knowledge_body = build_knowledge_html(hermes_dashboard_url).encode("utf-8")
        entity_db_body = build_entity_db_html(hermes_dashboard_url).encode("utf-8")
        project_memory_body = build_project_memory_html(hermes_dashboard_url).encode("utf-8")
        teams_ui_body = build_teams_html(hermes_dashboard_url).encode("utf-8")
        team_create_body = build_team_create_html(hermes_dashboard_url).encode("utf-8")
        team_messages_body = build_team_messages_html(hermes_dashboard_url).encode("utf-8")
        team_detail_body = build_team_detail_html(hermes_dashboard_url).encode("utf-8")
        kanban_body = build_kanban_html(hermes_dashboard_url).encode("utf-8")
        from .priority_queue_page import build_priority_queue_html
        priority_queue_body = build_priority_queue_html(hermes_dashboard_url).encode("utf-8")
        skills_body = build_skills_html(hermes_dashboard_url).encode("utf-8")
        allowlist_body = build_allowlist_html(hermes_dashboard_url).encode("utf-8")
        env_keys_body = build_env_keys_html(hermes_dashboard_url).encode("utf-8")
        agenda_body = build_agenda_html(hermes_dashboard_url).encode("utf-8")
        users_body = build_users_html(hermes_dashboard_url).encode("utf-8")
        metrics_ui_body = build_metrics_html(hermes_dashboard_url).encode("utf-8")
        latex_body = build_latex_html(hermes_dashboard_url).encode("utf-8")
        article_quality_body = build_article_quality_html(hermes_dashboard_url).encode("utf-8")
        active_agents_body = build_active_agents_html(hermes_dashboard_url).encode("utf-8")
        swarm_body = build_swarm_html(
            hermes_dashboard_url,
            dashboard_render_config=dashboard_render_config,
        ).encode("utf-8")
        swarm_profiles_body = build_swarm_profiles_html(
            hermes_dashboard_url,
            dashboard_render_config=dashboard_render_config,
        ).encode("utf-8")
        cron_slots_body = build_cron_slots_html(hermes_dashboard_url).encode("utf-8")
        cron_costs_body = build_cron_costs_html(hermes_dashboard_url).encode("utf-8")
        model_quotas_body = build_model_quotas_html(hermes_dashboard_url).encode("utf-8")
        model_costs_body = build_model_costs_html(hermes_dashboard_url).encode("utf-8")
        manage_delete_body = build_manage_delete_html(hermes_dashboard_url).encode("utf-8")
        projects_body = build_projects_html(hermes_dashboard_url).encode("utf-8")
        mcp_cards_body = build_mcp_cards_html(hermes_dashboard_url).encode("utf-8")
        mcp_servers_body = build_mcp_servers_html(hermes_dashboard_url).encode("utf-8")
        calendar_body = build_calendar_html(hermes_dashboard_url).encode("utf-8")
        ruflo_engines_body = build_ruflo_engines_html(hermes_dashboard_url).encode("utf-8")

    def _parse_query(environ) -> dict:
        query = environ.get("QUERY_STRING") or ""
        out: dict = {}
        for chunk in query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            if key:
                out[unquote(key)] = unquote(value)
        return out

    def _parse_cookies(environ) -> dict[str, str]:
        raw = environ.get("HTTP_COOKIE") or ""
        out: dict[str, str] = {}
        for chunk in raw.split(";"):
            key, _, val = chunk.strip().partition("=")
            if key:
                out[key] = val
        return out

    def _current_user(environ):
        if auth_user_from_token is None:
            return None
        # Cookie-based auth (browser)
        cookies = _parse_cookies(environ)
        token = cookies.get("qcm_session")
        if not token:
            # Bearer token auth (mobile / API clients)
            auth_header = environ.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
        return auth_user_from_token(token)

    # Endpoints exempt from same-origin enforcement (signed webhooks,
    # auth bootstrap, etc.). Everything else that mutates state must
    # come from our own origin or carry no Origin/Referer at all
    # (non-browser clients such as curl/scripts).
    _CSRF_EXEMPT_PATHS = frozenset(
        {
            "/whatsapp/inbound",  # HMAC-signed by wacli
            "/auth/login",
            "/auth/register",
            "/auth/logout",
        }
    )
    _CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def _expected_origins(environ) -> set[str]:
        # Accept any of the addresses the user might reach us by.
        h = (environ.get("HTTP_HOST") or f"{host}:{port}").strip()
        return {
            f"http://{h}",
            f"https://{h}",
            f"http://{host}:{port}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _origin_ok(environ, request_path: str) -> bool:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method not in _CSRF_METHODS:
            return True
        if request_path in _CSRF_EXEMPT_PATHS:
            return True
        origin = (environ.get("HTTP_ORIGIN") or "").strip()
        referer = (environ.get("HTTP_REFERER") or "").strip()
        # No Origin/Referer at all → not a browser-driven request
        # (curl, native clients, server-to-server). Allow.
        if not origin and not referer:
            return True
        allowed = _expected_origins(environ)
        if origin and origin in allowed:
            return True
        if referer:
            # Trim path/query, keep scheme://host[:port]
            try:
                from urllib.parse import urlsplit

                parts = urlsplit(referer)
                base = f"{parts.scheme}://{parts.netloc}"
                if base in allowed:
                    return True
            except Exception:
                pass
        return False

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()

        # Session cookie is host-scoped: login on 127.0.0.1 does not apply on localhost.
        host_hdr = (environ.get("HTTP_HOST") or "").strip().lower()
        if method in {"GET", "HEAD"} and host_hdr.startswith("localhost"):
            port_suffix = host_hdr.split(":", 1)[1] if ":" in host_hdr else str(port)
            target = f"http://127.0.0.1:{port_suffix}{path}"
            qs = environ.get("QUERY_STRING") or ""
            if qs:
                target += "?" + qs
            start_response(
                "302 Found",
                [("Location", target), ("Content-Length", "0")],
            )
            return [b""]

        # CSRF / cross-origin defense for state-changing requests.
        if not _origin_ok(environ, path):
            body = b'{"ok":false,"error":"cross-origin request blocked"}'
            start_response(
                "403 Forbidden",
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("X-Content-Type-Options", "nosniff"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        # ── A2A (Agent-to-Agent) protocol ────────────────────────────────
        if method == "GET" and path in (
            "/.well-known/agent.json",
            "/.well-known/agent-card.json",
        ):
            if openclaw_send is None:
                body = b'{"error":"A2A agent not configured"}'
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            scheme = environ.get("wsgi.url_scheme") or "http"
            host_hdr = (environ.get("HTTP_HOST") or f"{host}:{port}").strip()
            card = build_agent_card(f"{scheme}://{host_hdr}")
            body = json.dumps(card, ensure_ascii=False).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/a2a":
            if (
                openclaw_send is None
                or openclaw_send_status is None
                or openclaw_send_cancel is None
            ):
                body = (
                    b'{"jsonrpc":"2.0","id":null,"error":'
                    b'{"code":-32603,"message":"A2A agent not configured"}}'
                )
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError:
                length = 0
            raw = environ["wsgi.input"].read(length) if length else b""
            user = _current_user(environ)
            response = a2a_parse_and_handle(
                raw,
                send=lambda p: openclaw_send(p, user),
                send_status=openclaw_send_status,
                send_cancel=openclaw_send_cancel,
            )
            body = json.dumps(response, ensure_ascii=False, default=str).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/auth/bootstrap":
            result = auth_bootstrap() if auth_bootstrap else {"ok": True, "enabled": False}
            body = json.dumps(result, default=str).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path in {"/auth/register", "/auth/login"}:
            handler = auth_register if path.endswith("/register") else auth_login
            if handler is None:
                body = b'{"ok":false,"error":"auth not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = handler(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            headers = [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
            ]
            tok = result.get("session_token")
            if tok:
                headers.append(("Set-Cookie", f"qcm_session={tok}; Path=/; HttpOnly; SameSite=Lax"))
            body = json.dumps(result, default=str).encode()
            headers.append(("Content-Length", str(len(body))))
            start_response(code, headers)
            return [body]

        if method == "GET" and path == "/auth/me":
            user = _current_user(environ)
            result = auth_me(user) if auth_me else {"ok": False, "error": "auth not configured"}
            code = "200 OK" if result.get("ok", True) else "401 Unauthorized"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/auth/logout":
            cookies = _parse_cookies(environ)
            token = cookies.get("qcm_session")
            result = auth_logout(token) if auth_logout else {"ok": True}
            body = json.dumps(result, default=str).encode()
            headers = [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Set-Cookie", "qcm_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
                ("Content-Length", str(len(body))),
            ]
            start_response("200 OK", headers)
            return [body]

        if path == "/api/users" and method in {"GET", "POST"}:
            user = _current_user(environ)
            if method == "GET":
                result = (
                    users_list(user)
                    if users_list
                    else {"ok": False, "error": "users not configured"}
                )
                ok = result.get("ok", True)
                if not ok and "admin" in str(result.get("error") or "").lower():
                    code = "403 Forbidden"
                elif not ok and "auth" in str(result.get("error") or "").lower():
                    code = "401 Unauthorized"
                else:
                    code = "200 OK" if ok else "400 Bad Request"
            else:
                if users_create is None:
                    result = {"ok": False, "error": "users not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = users_create(payload, user)
                        ok = result.get("ok", True)
                        if not ok and "admin" in str(result.get("error") or "").lower():
                            code = "403 Forbidden"
                        else:
                            code = "200 OK" if ok else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/api/users/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "users":
                user = _current_user(environ)
                user_id = parts[2]
                if method == "DELETE":
                    if users_delete is None:
                        result = {"ok": False, "error": "users not configured"}
                        code = "503 Service Unavailable"
                    else:
                        result = users_delete(user_id, user)
                        ok = result.get("ok", True)
                        if not ok and "admin" in str(result.get("error") or "").lower():
                            code = "403 Forbidden"
                        else:
                            code = "200 OK" if ok else "400 Bad Request"
                elif method in {"PATCH", "POST"}:
                    if users_update is None:
                        result = {"ok": False, "error": "users not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = users_update(user_id, payload, user)
                            ok = result.get("ok", True)
                            code = "200 OK" if ok else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "POST" and path == "/auth/logout":
            result = {"ok": True}
            body = json.dumps(result, default=str).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                (
                    "Set-Cookie",
                    "qcm_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
                ),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/teams/presets" and method == "GET":
            user = _current_user(environ)
            result = (
                teams_presets(user)
                if teams_presets
                else {"ok": False, "error": "teams presets not configured"}
            )
            code = "200 OK" if result.get("ok", True) else "401 Unauthorized"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/teams" and method in {"GET", "POST"}:
            user = _current_user(environ)
            if method == "GET":
                if teams_list is None:
                    result = {"ok": False, "error": "teams not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = teams_list(user)
                        code = "200 OK" if result.get("ok", True) else "401 Unauthorized"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
            else:
                if team_create is None:
                    result = {"ok": False, "error": "teams not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = team_create(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/teams/") and "/kanban" not in path and "/whatsapp" not in path and "/emails" not in path and "/members" not in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "teams":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "PATCH":
                    if team_update is None:
                        result = {"ok": False, "error": "teams not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_update(team_id, payload, user)
                            code = (
                                "200 OK" if result.get("ok", True) else "400 Bad Request"
                            )
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "DELETE":
                    if team_delete is None:
                        result = {"ok": False, "error": "teams not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_delete(team_id, user)
                            code = (
                                "200 OK" if result.get("ok", True) else "400 Bad Request"
                            )
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/members routes
        if path.startswith("/teams/") and "/members" in path:
            parts = [p for p in path.split("/") if p]
            # /teams/<id>/members (GET, POST, DELETE)
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "members":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "GET":
                    if team_members_list is None:
                        result = {"ok": False, "error": "team members not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_members_list(team_id, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "POST":
                    if team_members_add is None:
                        result = {"ok": False, "error": "team members not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            member_user_id = str(
                                payload.get("user_id")
                                or payload.get("username")
                                or ""
                            ).strip()
                            if not member_user_id:
                                raise ValueError("user_id ou username é obrigatório")
                            result = team_members_add(team_id, member_user_id, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "DELETE":
                    if team_members_remove is None:
                        result = {"ok": False, "error": "team members not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            member_user_id = str(payload.get("user_id") or "").strip()
                            if not member_user_id:
                                raise ValueError("user_id é obrigatório")
                            result = team_members_remove(team_id, member_user_id, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/contacts (GET, POST, DELETE)
        if path.startswith("/teams/") and "/contacts" in path and "/context" not in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "contacts":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "GET":
                    if team_contacts_get is None:
                        result = {"ok": False, "error": "team contacts not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_contacts_get(team_id, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "POST":
                    if team_contacts_upsert is None:
                        result = {"ok": False, "error": "team contacts not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_contacts_upsert(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "DELETE":
                    if team_contacts_remove is None:
                        result = {"ok": False, "error": "team contacts not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_contacts_remove(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/articles/folder (POST upload folder from browser)
        if path.startswith("/teams/") and "/articles/folder" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4 and parts[0] == "teams" and parts[2] == "articles" and parts[3] == "folder":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "POST":
                    if team_articles_folder_upload is None:
                        result = {"ok": False, "error": "team articles folder upload not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_articles_folder_upload(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/cards/from-file (POST create card + attach team file)
        if path.startswith("/teams/") and "/cards/from-file" in path:
            parts = [p for p in path.split("/") if p]
            if (
                len(parts) == 4
                and parts[0] == "teams"
                and parts[2] == "cards"
                and parts[3] == "from-file"
            ):
                user = _current_user(environ)
                team_id = parts[1]
                if method == "POST":
                    if team_card_from_file is None:
                        result = {"ok": False, "error": "team card from file not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_card_from_file(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/normas/* (GET list, POST upload, DELETE, download, whatsapp)
        if path.startswith("/teams/") and "/normas" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "normas":
                user = _current_user(environ)
                team_id = parts[1]
                sub = parts[3] if len(parts) >= 4 else ""

                if sub == "download" and method == "GET":
                    if team_normas_download is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "404 Not Found"
                    else:
                        try:
                            qs = environ.get("QUERY_STRING") or ""
                            qp: dict[str, str] = {}
                            for part in qs.split("&"):
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    from urllib.parse import unquote_plus

                                    qp[unquote_plus(k)] = unquote_plus(v)
                            result = team_normas_download(team_id, qp, user)
                            if isinstance(result, tuple):
                                data_bytes, mime, fname = result
                                headers = _team_file_download_headers(
                                    data_bytes, mime, fname, preview=_is_download_preview(qp)
                                )
                                start_response("200 OK", headers)
                                return [data_bytes]
                            code = "200 OK" if result.get("ok", True) else "404 Not Found"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if sub == "from-file" and method == "POST":
                    if team_normas_from_file is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_from_file(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if sub == "from-whatsapp" and method == "POST":
                    if team_normas_from_whatsapp is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_from_whatsapp(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if sub == "preview" and method == "POST":
                    if team_normas_preview is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_preview(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if sub == "whatsapp-preview" and method == "POST":
                    if team_normas_whatsapp_preview is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_whatsapp_preview(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if sub:
                    result = {"ok": False, "error": "not found"}
                    code = "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if method == "GET":
                    if team_normas_get is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_normas_get(team_id, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "POST":
                    if team_normas_upload is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_upload(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif method == "DELETE":
                    if team_normas_delete is None:
                        result = {"ok": False, "error": "team normas not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_normas_delete(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/legal-evaluations/* (GET list/search/detail, POST save)
        if path.startswith("/teams/") and "/legal-evaluations" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "legal-evaluations":
                user = _current_user(environ)
                team_id = parts[1]
                sub = parts[3] if len(parts) >= 4 else ""
                qs = _parse_query(environ)

                if sub == "search" and method == "GET":
                    if team_legal_evaluations_search is None:
                        result = {"ok": False, "error": "legal evaluations not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_legal_evaluations_search(team_id, qs, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif sub and method == "GET":
                    if team_legal_evaluation_get is None:
                        result = {"ok": False, "error": "legal evaluations not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_legal_evaluation_get(team_id, sub, user)
                            code = "200 OK" if result.get("ok", True) else "404 Not Found"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif not sub and method == "GET":
                    if qs.get("q") or qs.get("query"):
                        if team_legal_evaluations_search is None:
                            result = {"ok": False, "error": "legal evaluations not configured"}
                            code = "503 Service Unavailable"
                        else:
                            try:
                                result = team_legal_evaluations_search(team_id, qs, user)
                                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                            except Exception as e:
                                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                                code = "500 Internal Server Error"
                    elif team_legal_evaluations_list is None:
                        result = {"ok": False, "error": "legal evaluations not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            result = team_legal_evaluations_list(team_id, qs, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                elif not sub and method == "POST":
                    if team_legal_evaluation_save is None:
                        result = {"ok": False, "error": "legal evaluations not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            result = team_legal_evaluation_save(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/context (GET) — full team context for the chat panel
        if path.startswith("/teams/") and "/context" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "context":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "GET":
                    if team_context_get is None:
                        result = {"ok": False, "error": "team context not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            from urllib.parse import parse_qs

                            qs = parse_qs(environ.get("QUERY_STRING") or "")
                            quick = (qs.get("quick") or ["0"])[0].lower() in (
                                "1",
                                "true",
                                "yes",
                            )
                            result = team_context_get(team_id, user, quick=quick)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/whatsapp/* routes
        if path.startswith("/teams/") and "/whatsapp" in path:
            parts = [p for p in path.split("/") if p]
            # /teams/<id>/whatsapp/numbers (GET, POST)
            # /teams/<id>/whatsapp/numbers/add (POST)
            # /teams/<id>/whatsapp/numbers/remove (POST)
            # /teams/<id>/whatsapp/send (POST)
            # /teams/<id>/whatsapp/broadcast (POST)
            # /teams/<id>/whatsapp/send_to_group (POST)
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "whatsapp":
                user = _current_user(environ)
                team_id = parts[1]
                sub = parts[3] if len(parts) > 3 else ""
                sub2 = parts[4] if len(parts) > 4 else ""
                result: dict = {"ok": False, "error": "not found"}
                code = "404 Not Found"

                if sub == "numbers" and not sub2:
                    if method == "GET":
                        if team_whatsapp_numbers_get is not None:
                            try:
                                result = team_whatsapp_numbers_get(team_id, user)
                                code = "200 OK" if result.get("ok") else "400 Bad Request"
                            except Exception as e:
                                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                                code = "500 Internal Server Error"
                        else:
                            result = {"ok": False, "error": "team whatsapp not configured"}
                            code = "503 Service Unavailable"
                    elif method == "POST":
                        if team_whatsapp_numbers_set is not None:
                            try:
                                length = int(environ.get("CONTENT_LENGTH") or "0")
                                raw = environ["wsgi.input"].read(length) if length else b""
                                payload = json.loads(raw.decode("utf-8") or "{}")
                                if not isinstance(payload, dict):
                                    raise ValueError("body must be a JSON object")
                                result = team_whatsapp_numbers_set(team_id, payload, user)
                                code = "200 OK" if result.get("ok") else "400 Bad Request"
                            except json.JSONDecodeError:
                                result = {"ok": False, "error": "invalid JSON body"}
                                code = "400 Bad Request"
                            except Exception as e:
                                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                                code = "500 Internal Server Error"
                        else:
                            result = {"ok": False, "error": "team whatsapp not configured"}
                            code = "503 Service Unavailable"

                elif sub == "numbers" and sub2 == "add" and method == "POST":
                    if team_whatsapp_number_add is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_whatsapp_number_add(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team whatsapp not configured"}
                        code = "503 Service Unavailable"

                elif sub == "numbers" and sub2 == "remove" and method == "POST":
                    if team_whatsapp_number_remove is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_whatsapp_number_remove(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team whatsapp not configured"}
                        code = "503 Service Unavailable"

                elif sub == "send" and method == "POST":
                    if team_whatsapp_send is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_whatsapp_send(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "403 Forbidden"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team whatsapp not configured"}
                        code = "503 Service Unavailable"

                elif sub == "broadcast" and method == "POST":
                    if team_whatsapp_broadcast is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_whatsapp_broadcast(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "403 Forbidden"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team whatsapp not configured"}
                        code = "503 Service Unavailable"

                elif sub == "send_to_group" and method == "POST":
                    if team_whatsapp_send_to_group is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_whatsapp_send_to_group(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "403 Forbidden"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team whatsapp not configured"}
                        code = "503 Service Unavailable"

                elif sub == "messages" and method == "GET":
                    if team_group_messages_get is not None:
                        try:
                            query = _parse_query(environ)
                            result = team_group_messages_get(team_id, query, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team group messages not configured"}
                        code = "503 Service Unavailable"

                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # /teams/<id>/emails/* routes
        if path.startswith("/teams/") and "/emails" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "teams" and parts[2] == "emails":
                user = _current_user(environ)
                team_id = parts[1]
                sub = parts[3] if len(parts) > 3 else ""
                result: dict = {"ok": False, "error": "not found"}
                code = "404 Not Found"

                if sub == "messages" and method == "GET":
                    if team_email_messages_get is not None:
                        try:
                            query = _parse_query(environ)
                            result = team_email_messages_get(team_id, query, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team email messages not configured"}
                        code = "503 Service Unavailable"

                elif sub == "sync" and method == "POST":
                    if team_email_sync is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_email_sync(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team email sync not configured"}
                        code = "503 Service Unavailable"

                elif sub == "send" and method == "POST":
                    if team_email_send is not None:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_email_send(team_id, payload, user)
                            code = "200 OK" if result.get("ok") else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                    else:
                        result = {"ok": False, "error": "team email send not configured"}
                        code = "503 Service Unavailable"

                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if path.startswith("/teams/") and path.endswith("/run"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "run":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "POST":
                    if team_swarm_run is None:
                        result = {"ok": False, "error": "team swarm run not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_swarm_run(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if path.startswith("/teams/") and "/kanban" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "kanban":
                user = _current_user(environ)
                team_id = parts[1]
                if method == "GET":
                    result = (
                        team_kanban_get(team_id, user)
                        if team_kanban_get
                        else {"ok": False, "error": "kanban teams not configured"}
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                elif method == "POST":
                    if team_kanban_save is None:
                        result = {"ok": False, "error": "kanban teams not configured"}
                        code = "503 Service Unavailable"
                    else:
                        try:
                            length = int(environ.get("CONTENT_LENGTH") or "0")
                            raw = environ["wsgi.input"].read(length) if length else b""
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                            result = team_kanban_save(team_id, payload, user)
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                        except json.JSONDecodeError:
                            result = {"ok": False, "error": "invalid JSON body"}
                            code = "400 Bad Request"
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            code = "500 Internal Server Error"
                else:
                    result = {"ok": False, "error": "method not allowed"}
                    code = "405 Method Not Allowed"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        # POST /agents/<name>/<action>  where action ∈ {toggle, enable, disable}
        if method == "POST" and path.startswith("/agents/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and agent_action is not None:
                _, name, action = parts
                try:
                    result = agent_action(name, action)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            body = b'{"ok":false,"error":"bad request"}'
            start_response("400 Bad Request", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/agents/create":
            if hermes_agent_create is None:
                body = b'{"ok":false,"error":"hermes agent create not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_agent_create(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/presets":
            if swarm_presets is None:
                body = b'{"ok":false,"error":"swarm presets not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                qs = environ.get("QUERY_STRING") or ""
                query: dict[str, str] = {}
                if qs:
                    for chunk in qs.split("&"):
                        key, _, value = chunk.partition("=")
                        if key:
                            query[key] = unquote(value)
                result = swarm_presets(_current_user(environ), query)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/create":
            if openai_swarm_create is None:
                body = b'{"ok":false,"error":"openai swarm not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = openai_swarm_create(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/graph":
            if swarm_graph_overview is None:
                body = b'{"ok":false,"error":"swarm graph not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = swarm_graph_overview(_current_user(environ))
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/executions":
            if swarm_executions is None:
                body = b'{"ok":false,"error":"swarm executions not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = swarm_executions(
                    _parse_query(environ),
                    _current_user(environ),
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/api/swarm/integration/status":
            try:
                from pathlib import Path

                from .config import Config
                from .ruflo_swarm_integration import build_integration_status

                cfg = Config.load(Path("config.yaml"))
                result = build_integration_status(cfg)
                code = "200 OK"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/api/swarm/a2a/delegate":
            try:
                from pathlib import Path

                from .config import Config
                from .ruflo_swarm_a2a import build_a2a_bridge

                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                cfg = Config.load(Path("config.yaml"))
                bridge = build_a2a_bridge(getattr(cfg, "swarm_ruflo_a2a", None))
                if bridge is None:
                    result = {"ok": False, "error": "swarm_ruflo_a2a disabled"}
                    code = "400 Bad Request"
                else:
                    result = bridge.delegate_after_run(
                        swarm_name=str(payload.get("swarm_name") or ""),
                        objective=str(payload.get("objective") or ""),
                        outputs=dict(payload.get("outputs") or {}),
                    )
                    code = "200 OK" if result.get("ok") else "502 Bad Gateway"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/graph/preview":
            if swarm_graph_preview is None:
                body = b'{"ok":false,"error":"swarm graph not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_graph_preview(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/graph/run":
            if swarm_graph_run is None:
                body = b'{"ok":false,"error":"swarm graph not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_graph_run(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/graph/resume":
            if swarm_graph_resume is None:
                body = b'{"ok":false,"error":"swarm graph not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_graph_resume(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/list":
            if openai_swarm_list is None:
                body = b'{"ok":false,"error":"openai swarm not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                swarms = openai_swarm_list()
                result = {"ok": True, "swarms": swarms, "count": len(swarms)}
                code = "200 OK"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/robots/stream":
            if swarm_robots_snapshot_stream is None:
                body = b'{"ok":false,"error":"swarm robots stream not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            user = _current_user(environ)

            def _sse_body():
                yield b": connected\n\n"
                try:
                    for event in swarm_robots_snapshot_stream(user):
                        payload = json.dumps(event, default=str, ensure_ascii=False)
                        yield f"data: {payload}\n\n".encode("utf-8")
                except Exception as e:
                    err = json.dumps(
                        {"ok": False, "phase": "error", "error": f"{type(e).__name__}: {e}"},
                        ensure_ascii=False,
                    )
                    yield f"data: {err}\n\n".encode("utf-8")

            start_response("200 OK", [
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Connection", "keep-alive"),
                ("X-Accel-Buffering", "no"),
            ])
            return _sse_body()

        if method == "GET" and path == "/swarm/robots":
            if swarm_robots_snapshot is None:
                body = b'{"ok":false,"error":"swarm robots not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                query = _parse_query(environ)
                result = swarm_robots_snapshot(_current_user(environ), query)
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/activity/changes":
            if swarm_activity_changes is None:
                body = b'{"ok":false,"error":"swarm activity changes not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = swarm_activity_changes(
                    _parse_query(environ), _current_user(environ)
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/skills":
            if swarm_skills_list is None:
                body = b'{"ok":false,"error":"swarm skills not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = swarm_skills_list()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/swarm/teams-board":
            if swarm_teams_board is None:
                body = b'{"ok":false,"error":"swarm teams board not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = swarm_teams_board(_current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/teams-board/claim":
            if swarm_teams_claim is None:
                body = b'{"ok":false,"error":"swarm teams claim not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_teams_claim(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/execution/cancel":
            if swarm_execution_cancel is None:
                body = b'{"ok":false,"error":"swarm execution cancel not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_execution_cancel(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/swarm/robots/create":
            if swarm_robot_create is None:
                body = b'{"ok":false,"error":"swarm robot create not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_robot_create(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/swarm/robots/"):
            parts = [p for p in path.split("/") if p]
            source_slug: Optional[str] = None
            if method == "GET":
                query_map = _parse_query(environ)
                if (
                    len(parts) == 3
                    and parts[0] == "swarm"
                    and parts[1] == "robots"
                    and parts[2] == "source"
                ):
                    source_slug = _wsgi_path_segment(
                        str(query_map.get("slug") or query_map.get("profile") or "")
                    )
                elif (
                    len(parts) == 4
                    and parts[0] == "swarm"
                    and parts[1] == "robots"
                    and parts[3] == "source"
                ):
                    source_slug = _wsgi_path_segment(parts[2])
            if source_slug is not None:
                if not source_slug:
                    body = b'{"ok":false,"error":"slug is required"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                if swarm_robot_source is None:
                    body = b'{"ok":false,"error":"swarm robot source not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                query_map = _parse_query(environ)
                want_raw = str(query_map.get("raw") or "").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                try:
                    result = swarm_robot_source(source_slug, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                if want_raw and result.get("ok"):
                    content = str(
                        result.get("explained_markdown")
                        or result.get("content")
                        or ""
                    )
                    body = content.encode("utf-8", errors="replace")
                    filename = f"{source_slug}-source.md"
                    start_response("200 OK", [
                        ("Content-Type", "text/markdown; charset=utf-8"),
                        ("Cache-Control", "no-cache"),
                        (
                            "Content-Disposition",
                            f'inline; filename="{filename}"',
                        ),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if (
                len(parts) == 4
                and parts[0] == "swarm"
                and parts[1] == "robots"
                and parts[3] == "history"
                and method == "GET"
            ):
                robot_slug = _wsgi_path_segment(parts[2])
                if swarm_agent_history is None:
                    body = b'{"ok":false,"error":"swarm agent history not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = swarm_agent_history(
                        robot_slug,
                        _current_user(environ),
                        query=_parse_query(environ),
                    )
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if len(parts) == 3 and parts[0] == "swarm" and parts[1] == "robots":
                robot_slug = _wsgi_path_segment(parts[2])
                if method == "GET":
                    if swarm_robot_get is None:
                        body = b'{"ok":false,"error":"swarm robot get not configured"}'
                        start_response("503 Service Unavailable", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        result = swarm_robot_get(robot_slug, _current_user(environ))
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                if method in {"PATCH", "PUT", "POST"}:
                    if swarm_robot_update is None:
                        body = b'{"ok":false,"error":"swarm robot update not configured"}'
                        start_response("503 Service Unavailable", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_robot_update(
                            robot_slug, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                if method == "DELETE":
                    if swarm_robot_delete is None:
                        body = b'{"ok":false,"error":"swarm robot delete not configured"}'
                        start_response("503 Service Unavailable", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if payload and not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_robot_delete(
                            robot_slug, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

        if method == "POST" and path == "/swarm/swarms/create":
            if swarm_create is None:
                body = b'{"ok":false,"error":"swarm create not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = swarm_create(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/swarm/swarms/"):
            parts = [p for p in path.split("/") if p]
            if (
                len(parts) == 5
                and parts[0] == "swarm"
                and parts[1] == "swarms"
                and parts[3] == "health"
                and parts[4] == "fix"
                and method == "POST"
            ):
                swarm_id = _wsgi_path_segment(parts[2])
                if swarm_health_fix is None:
                    result = {"ok": False, "error": "swarm health fix not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_health_fix(
                            swarm_id, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if (
                len(parts) == 4
                and parts[0] == "swarm"
                and parts[1] == "swarms"
                and parts[3] == "schedule"
                and method == "POST"
            ):
                swarm_id = _wsgi_path_segment(parts[2])
                if swarm_schedule is None:
                    result = {"ok": False, "error": "swarm schedule not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_schedule(
                            swarm_id, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if (
                len(parts) == 4
                and parts[0] == "swarm"
                and parts[1] == "swarms"
                and parts[3] == "model"
                and method == "POST"
            ):
                swarm_id = _wsgi_path_segment(parts[2])
                if swarm_model is None:
                    result = {"ok": False, "error": "swarm model update not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_model(
                            swarm_id, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if (
                len(parts) == 4
                and parts[0] == "swarm"
                and parts[1] == "swarms"
                and parts[3] == "health"
                and method == "GET"
            ):
                swarm_id = _wsgi_path_segment(parts[2])
                if swarm_health is None:
                    result = {"ok": False, "error": "swarm health not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = swarm_health(swarm_id, _current_user(environ))
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if len(parts) == 3 and parts[0] == "swarm" and parts[1] == "swarms":
                swarm_id = _wsgi_path_segment(parts[2])
                if method in {"PATCH", "PUT", "POST"}:
                    if swarm_update is None:
                        body = b'{"ok":false,"error":"swarm update not configured"}'
                        start_response("503 Service Unavailable", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_update(
                            swarm_id, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                if method == "DELETE":
                    if swarm_delete is None:
                        body = b'{"ok":false,"error":"swarm delete not configured"}'
                        start_response("503 Service Unavailable", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}") if length else {}
                        if payload and not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = swarm_delete(
                            swarm_id, payload, _current_user(environ)
                        )
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

        if method == "GET" and path == "/hermes/skills":
            if hermes_skills_list is None:
                body = b'{"ok":false,"error":"hermes skills not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_skills_list()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/recovery/status":
            if hermes_recovery_status is None:
                body = b'{"ok":false,"error":"hermes recovery not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_recovery_status()
                code = "200 OK" if result.get("ok", True) else "503 Service Unavailable"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/recovery":
            if hermes_recovery_action is None:
                body = b'{"ok":false,"error":"hermes recovery not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if auth_user_from_token is not None and _current_user(environ) is None:
                body = b'{"ok":false,"error":"authentication required"}'
                start_response("401 Unauthorized", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_recovery_action(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/cron/diagnose":
            if hermes_cron_diagnose is None:
                body = b'{"ok":false,"error":"hermes cron diagnose not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_cron_diagnose(_parse_query(environ))
                code = "200 OK" if result.get("ok", True) else "503 Service Unavailable"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/cron/diagnose":
            if hermes_cron_diagnose_action is None:
                body = b'{"ok":false,"error":"hermes cron diagnose not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if auth_user_from_token is not None and _current_user(environ) is None:
                body = b'{"ok":false,"error":"authentication required"}'
                start_response("401 Unauthorized", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_cron_diagnose_action(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/profiles/generate-personality":
            if hermes_profile_generate_personality is None:
                body = b'{"ok":false,"error":"hermes profile generate not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_profile_generate_personality(
                    payload, _current_user(environ)
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/roles/presets":
            if hermes_role_presets is None:
                body = b'{"ok":false,"error":"hermes roles not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_role_presets()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/roles/seed/status":
            if hermes_roles_seed_status is None:
                body = b'{"ok":false,"error":"hermes roles seed status not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_roles_seed_status()
                code = "200 OK"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/roles/seed":
            if hermes_roles_seed is None:
                body = b'{"ok":false,"error":"hermes roles seed not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_roles_seed(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/mascots":
            if hermes_mascots_list is None:
                body = b'{"ok":false,"error":"hermes mascots not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_mascots_list()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/local-folders":
            if hermes_local_folders is None:
                body = b'{"ok":false,"error":"hermes local folders not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_local_folders(_parse_query(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/attendance":
            user = _current_user(environ)
            query = _parse_query(environ)

            if method == "GET":
                if attendance_get is None:
                    result = {"ok": False, "error": "attendance not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = attendance_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST":
                if attendance_save is None:
                    result = {"ok": False, "error": "attendance not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = attendance_save(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if path.startswith("/latex/"):
            user = _current_user(environ)
            query = _parse_query(environ)

            if method == "GET" and path == "/latex/workspaces":
                if latex_workspaces_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_workspaces_list(user)
                        code = "200 OK" if result.get("ok", True) else "401 Unauthorized"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/workspaces":
                if latex_workspace_register is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_workspace_register(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/workspaces/remove":
                if latex_workspace_remove is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_workspace_remove(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/articles":
                if latex_articles_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_articles_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/files":
                if latex_files_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_files_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/tex-files":
                if latex_tex_files_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_tex_files_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/bib-files":
                if latex_bib_files_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_bib_files_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/files/delete":
                if latex_file_delete is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_file_delete(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/file":
                if latex_file_get is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_file_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/improve/ops":
                if latex_improve_ops is None:
                    result = {"ok": False, "error": "latex improve not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_improve_ops(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/improve":
                if latex_tex_improve is None:
                    result = {"ok": False, "error": "latex improve not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_improve(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path in (
                "/latex/improvements/list",
                "/latex/improvements/history",
            ):
                handler = (
                    latex_improvements_list
                    if path.endswith("/list")
                    else latex_improvements_history
                )
                if handler is None:
                    result = {"ok": False, "error": "latex improvements not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = handler(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path in (
                "/latex/improvements/resolve",
                "/latex/improvements/evaluate",
            ):
                handler = (
                    latex_improvements_resolve
                    if path.endswith("/resolve")
                    else latex_improvements_evaluate
                )
                if handler is None:
                    result = {"ok": False, "error": "latex improvements not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = handler(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/files/upload":
                if latex_file_upload is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_file_upload(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/file":
                if latex_file_save is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_file_save(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/compile":
                if latex_compile is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_compile(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/tex":
                if latex_tex_get is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_tex_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex":
                if latex_tex_save is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_save(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/tex/versions":
                if latex_tex_version_list is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_tex_version_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/tex/versions/detail":
                if latex_tex_version_get is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_tex_version_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/versions":
                if latex_tex_version_save is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_version_save(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/objectives/md":
                if latex_objectives_md is None:
                    result = {"ok": False, "error": "latex objectives not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_objectives_md(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/versions/restore":
                if latex_tex_version_restore is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_version_restore(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/chat/stream":
                if latex_tex_chat is None:
                    body = b'{"ok":false,"error":"latex not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                user = _current_user(environ)
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                except Exception as e:
                    err = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})
                    body = err.encode()
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                import queue as _queue
                import threading as _threading

                q: "_queue.Queue[tuple]" = _queue.Queue()

                def _on_token(text: str) -> None:
                    q.put(("token", text))

                def _on_status(text: str) -> None:
                    q.put(("status", text))

                def _worker() -> None:
                    try:
                        result = latex_tex_chat(payload, user, on_token=_on_token, on_status=_on_status)
                        q.put(("done", result))
                    except Exception as e:  # pragma: no cover - defensive
                        q.put(("done", {"ok": False, "error": f"{type(e).__name__}: {e}"}))

                worker = _threading.Thread(target=_worker, daemon=True)
                worker.start()

                def _sse_body():
                    yield b": connected\n\n"
                    while True:
                        try:
                            kind, data = q.get(timeout=180.0)
                        except Exception:
                            err = json.dumps({"type": "done", "ok": False, "error": "timeout"})
                            yield f"data: {err}\n\n".encode("utf-8")
                            return
                        if kind == "token":
                            frame = json.dumps({"type": "delta", "text": data}, ensure_ascii=False)
                            yield f"data: {frame}\n\n".encode("utf-8")
                            continue
                        if kind == "status":
                            frame = json.dumps({"type": "status", "text": data}, ensure_ascii=False)
                            yield f"data: {frame}\n\n".encode("utf-8")
                            continue
                        result = data if isinstance(data, dict) else {"ok": False}
                        result = {"type": "done", **result}
                        frame = json.dumps(result, default=str, ensure_ascii=False)
                        yield f"data: {frame}\n\n".encode("utf-8")
                        return

                start_response("200 OK", [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("X-Accel-Buffering", "no"),
                ])
                return _sse_body()

            if method == "POST" and path == "/latex/tex/chat":
                if latex_tex_chat is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_chat(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/objectives/suggest":
                if latex_objectives_suggest is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_objectives_suggest(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/objectives":
                if latex_objectives_get is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_objectives_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/objectives":
                if latex_objectives_save is None:
                    result = {"ok": False, "error": "latex not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_objectives_save(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/apply-template":
                if latex_tex_apply_template is None:
                    result = {"ok": False, "error": "latex template apply not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_apply_template(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/tex/restore-template":
                if latex_tex_restore_template is None:
                    result = {"ok": False, "error": "latex template restore not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_tex_restore_template(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/pdf":
                if latex_pdf_path is None:
                    body = b'{"ok":false,"error":"latex not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    pdf_path, err = latex_pdf_path(query, user)
                    if pdf_path is None:
                        body = json.dumps({"ok": False, "error": err or "not found"}, default=str).encode()
                        code = "404 Not Found" if err else "404 Not Found"
                        if err and "authenticated" in err.lower():
                            code = "401 Unauthorized"
                        start_response(code, [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    data = pdf_path.read_bytes()
                    start_response("200 OK", [
                        ("Content-Type", "application/pdf"),
                        ("Content-Disposition", f'inline; filename="{pdf_path.name}"'),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(data))),
                    ])
                    return [data]
                except Exception as e:
                    body = json.dumps(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        default=str,
                    ).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "GET" and path == "/latex/figure":
                if latex_figure_path is None:
                    body = b'{"ok":false,"error":"latex not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    img_path, err = latex_figure_path(query, user)
                    if img_path is None:
                        body = json.dumps({"ok": False, "error": err or "not found"}, default=str).encode()
                        code = "404 Not Found"
                        if err and "authenticated" in err.lower():
                            code = "401 Unauthorized"
                        start_response(code, [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    data = img_path.read_bytes()
                    mime = mimetypes.guess_type(img_path.name)[0] or "application/octet-stream"
                    start_response("200 OK", [
                        ("Content-Type", mime),
                        ("Content-Disposition", f'inline; filename="{img_path.name}"'),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(data))),
                    ])
                    return [data]
                except Exception as e:
                    body = json.dumps(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        default=str,
                    ).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "GET" and path == "/latex/quality":
                if latex_quality_list is None:
                    result = {"ok": False, "error": "latex quality not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_quality_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/quality/detail":
                if latex_quality_detail is None:
                    result = {"ok": False, "error": "latex quality not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_quality_detail(query, user)
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/templates":
                if latex_templates_list is None:
                    result = {"ok": False, "error": "latex templates not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_templates_list(query, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/templates/detail":
                if latex_template_get is None:
                    result = {"ok": False, "error": "latex templates not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        result = latex_template_get(query, user)
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/latex/templates/preview":
                if latex_template_preview is None:
                    body = b'{"ok":false,"error":"latex templates not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    img_path, err = latex_template_preview(query, user)
                    if img_path is None:
                        body = json.dumps({"ok": False, "error": err or "not found"}, default=str).encode()
                        code = "401 Unauthorized" if err and "authenticated" in err.lower() else "404 Not Found"
                        start_response(code, [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    data = img_path.read_bytes()
                    start_response("200 OK", [
                        ("Content-Type", "image/png"),
                        ("Content-Disposition", f'inline; filename="{img_path.name}"'),
                        ("Cache-Control", "public, max-age=300"),
                        ("Content-Length", str(len(data))),
                    ])
                    return [data]
                except Exception as e:
                    body = json.dumps(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        default=str,
                    ).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "POST" and path == "/latex/templates/upload":
                if latex_template_upload is None:
                    result = {"ok": False, "error": "latex templates not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_template_upload(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/templates/delete":
                if latex_template_delete is None:
                    result = {"ok": False, "error": "latex templates not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_template_delete(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/latex/templates/apply":
                if latex_template_apply is None:
                    result = {"ok": False, "error": "latex templates not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = latex_template_apply(payload, user)
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "GET" and path == "/hermes/profiles":
            if hermes_profiles_list is None:
                body = b'{"ok":false,"error":"hermes profiles not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            profiles_query: dict[str, str] = {}
            qs_profiles = environ.get("QUERY_STRING") or ""
            if qs_profiles:
                for chunk in qs_profiles.split("&"):
                    qkey, _, value = chunk.partition("=")
                    if qkey:
                        profiles_query[qkey] = unquote(value)
            try:
                result = hermes_profiles_list(
                    _current_user(environ), profiles_query
                )
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "DELETE" and path.startswith("/hermes/profile/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "hermes" and parts[1] == "profile":
                profile_name = unquote(parts[2])
                if hermes_profile_delete is None:
                    body = b'{"ok":false,"error":"hermes profile delete not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = hermes_profile_delete(profile_name)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "GET" and path == "/hermes/kanban/projects":
            if hermes_kanban_projects is None:
                body = b'{"ok":false,"error":"hermes kanban not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                qs = environ.get("QUERY_STRING") or ""
                quick = any(
                    part in ("quick=1", "quick=true", "quick=yes", "local=1")
                    for part in qs.split("&")
                    if part
                )
                result = hermes_kanban_projects(_current_user(environ), quick=quick)
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/kanban":
            if hermes_kanban_get is None:
                body = b'{"ok":false,"error":"hermes kanban not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_kanban_get(_parse_query(environ), _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/kanban/task-history":
            if hermes_kanban_task_history is None:
                body = b'{"ok":false,"error":"hermes kanban not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_kanban_task_history(
                    _parse_query(environ), _current_user(environ)
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/kanban/source":
            if hermes_kanban_source is None:
                body = b'{"ok":false,"error":"hermes kanban not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_kanban_source(
                    _parse_query(environ), _current_user(environ)
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban":
            if hermes_kanban_action is None:
                body = b'{"ok":false,"error":"hermes kanban not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_action(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/schedule":
            if hermes_kanban_schedule is None:
                body = b'{"ok":false,"error":"hermes kanban scheduling not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_schedule(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/kanban/schedule/status":
            if hermes_kanban_schedule_status is None:
                body = b'{"ok":false,"error":"hermes kanban scheduling not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            query = {}
            qs = environ.get("QUERY_STRING") or ""
            if qs:
                for chunk in qs.split("&"):
                    qkey, _, value = chunk.partition("=")
                    if qkey:
                        query[qkey] = unquote(value)
            try:
                result = hermes_kanban_schedule_status(query)
                code = "200 OK" if result.get("ok", True) else "404 Not Found"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/pm-analysis":
            if hermes_kanban_pm_analysis is None:
                body = b'{"ok":false,"error":"pm analysis not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_pm_analysis(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/requirements":
            if hermes_kanban_requirements is None:
                body = b'{"ok":false,"error":"requirements agent not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_requirements(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # --- Kanban Attachments ---
        if method == "POST" and path == "/hermes/kanban/attachments/upload":
            if hermes_kanban_attachment_upload is None:
                body = b'{"ok":false,"error":"kanban attachments not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_attachment_upload(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/attachments/upload-binary":
            if hermes_kanban_attachment_upload_binary is None:
                body = b'{"ok":false,"error":"kanban binary attachments not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                from urllib.parse import parse_qs, unquote_plus

                qs = environ.get("QUERY_STRING") or ""
                qp: dict[str, str] = {}
                for k, values in parse_qs(qs, keep_blank_values=True).items():
                    if values:
                        qp[unquote_plus(k)] = unquote_plus(values[-1])
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                result = hermes_kanban_attachment_upload_binary(
                    qp, raw, _current_user(environ)
                )
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/kanban/attachments/download":
            if hermes_kanban_attachment_get is None:
                body = b'{"ok":false,"error":"kanban attachments not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                qs = environ.get("QUERY_STRING") or ""
                qp: dict[str, str] = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        from urllib.parse import unquote_plus
                        qp[unquote_plus(k)] = unquote_plus(v)
                result = hermes_kanban_attachment_get(qp, _current_user(environ))
                if isinstance(result, tuple):
                    data_bytes, mime = result
                    fname = qp.get("name") or qp.get("safe_name", "file")
                    disp = _format_content_disposition(fname)
                    start_response("200 OK", [
                        ("Content-Type", mime),
                        ("Content-Length", str(len(data_bytes))),
                        ("Content-Disposition", disp),
                        ("Cache-Control", "private, max-age=86400"),
                    ])
                    return [data_bytes]
                code = "200 OK" if result.get("ok", True) else "404 Not Found"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "GET" and path == "/hermes/teams/download":
            try:
                qs = environ.get("QUERY_STRING") or ""
                qp: dict[str, str] = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        from urllib.parse import unquote_plus

                        qp[unquote_plus(k)] = unquote_plus(v)
                from .team_files_download import serve_team_file_download

                result = serve_team_file_download(qp)
                if isinstance(result, tuple):
                    data_bytes, mime, fname = result
                    headers = _team_file_download_headers(
                        data_bytes, mime, fname, preview=_is_download_preview(qp)
                    )
                    start_response("200 OK", headers)
                    return [data_bytes]
                code = "200 OK" if result.get("ok", True) else "404 Not Found"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "GET" and path == "/hermes/kanban/attachments/download-zip":
            if hermes_kanban_attachments_zip_get is None:
                body = b'{"ok":false,"error":"kanban attachments zip not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                qs = environ.get("QUERY_STRING") or ""
                qp: dict[str, str] = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        from urllib.parse import unquote_plus
                        qp[unquote_plus(k)] = unquote_plus(v)
                result = hermes_kanban_attachments_zip_get(qp, _current_user(environ))
                if isinstance(result, tuple):
                    data_bytes, fname = result
                    disp = _format_content_disposition(fname)
                    start_response("200 OK", [
                        ("Content-Type", "application/zip"),
                        ("Content-Length", str(len(data_bytes))),
                        ("Content-Disposition", disp),
                        ("Cache-Control", "private, max-age=86400"),
                    ])
                    return [data_bytes]
                code = "200 OK" if result.get("ok", True) else "404 Not Found"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "POST" and path == "/hermes/download/save-to-disk":
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                qp = payload if isinstance(payload, dict) else {}
                from .file_save_to_disk import save_href_to_downloads

                result = save_href_to_downloads(
                    str(qp.get("url") or qp.get("href") or ""),
                    str(qp.get("display_name") or qp.get("filename") or ""),
                    artifact_get=openclaw_artifact_download,
                    normas_get=team_normas_download,
                    book_epub_get=openclaw_book_epub,
                    user=_current_user(environ),
                )
                body = json.dumps(result, default=str).encode()
                start_response("200 OK", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except ValueError as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("400 Bad Request", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "POST" and path == "/hermes/kanban/attachments/save-to-disk":
            if hermes_kanban_attachment_get is None:
                body = b'{"ok":false,"error":"kanban attachments not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                qp = payload if isinstance(payload, dict) else {}
                result = hermes_kanban_attachment_get(qp, _current_user(environ))
                if not isinstance(result, tuple):
                    code = "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                data_bytes, mime = result
                display_name = str(qp.get("display_name") or qp.get("safe_name") or "file").strip()
                import pathlib
                downloads_dir = pathlib.Path.home() / "Downloads"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                dest = downloads_dir / display_name
                if dest.exists():
                    stem = dest.stem
                    suffix = dest.suffix
                    i = 1
                    while dest.exists():
                        dest = downloads_dir / f"{stem} ({i}){suffix}"
                        i += 1
                dest.write_bytes(data_bytes)
                resp_data = {"ok": True, "path": str(dest)}
                body = json.dumps(resp_data).encode()
                start_response("200 OK", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "POST" and path == "/hermes/open-path":
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                fpath = str(payload.get("path") or "").strip()
                if not fpath:
                    body = b'{"ok":false,"error":"path required"}'
                    start_response("400 Bad Request", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                    return [body]
                import subprocess, pathlib
                p = pathlib.Path(fpath).expanduser().resolve()
                if not p.exists():
                    body = json.dumps({"ok": False, "error": f"arquivo não encontrado: {p}"}).encode()
                    start_response("404 Not Found", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                    return [body]
                if payload.get("reveal"):
                    subprocess.Popen(["open", "-R", str(p)])
                else:
                    subprocess.Popen(["open", str(p)])
                body = json.dumps({"ok": True, "path": str(p)}).encode()
                start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                return [body]
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                start_response("500 Internal Server Error", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                return [body]

        if method == "DELETE" and path == "/hermes/kanban/attachments/delete":
            if hermes_kanban_attachment_delete is None:
                body = b'{"ok":false,"error":"kanban attachments not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_attachment_delete(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/attachments/move":
            if hermes_kanban_attachment_move is None:
                body = b'{"ok":false,"error":"kanban attachment move not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_attachment_move(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/kanban/attachments/copy":
            if hermes_kanban_attachment_copy is None:
                body = b'{"ok":false,"error":"kanban attachment copy not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_kanban_attachment_copy(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # --- Priority Queue API ---
        if path == "/priority-queue" and method == "GET":
            if priority_queue_get is None:
                body = b'{"ok":false,"error":"priority queue not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = priority_queue_get()
                code = "200 OK"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/priority-queue" and method == "POST":
            if priority_queue_post is None:
                body = b'{"ok":false,"error":"priority queue not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = priority_queue_post(payload, _current_user(environ))
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/send" and method == "POST":
            if whatsapp_send is None:
                result = {"ok": False, "error": "whatsapp send not configured"}
                code = "503 Service Unavailable"
            elif auth_user_from_token is not None and _current_user(environ) is None:
                result = {"ok": False, "error": "authentication required"}
                code = "401 Unauthorized"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = whatsapp_send(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/queue" and method == "GET":
            if whatsapp_queue is None:
                result = {"ok": False, "error": "whatsapp queue not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_queue()
                    code = "200 OK"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/status" and method == "GET":
            if whatsapp_status is None:
                result = {"ok": False, "error": "whatsapp status not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_status()
                    code = "200 OK" if result.get("ok", True) else "503 Service Unavailable"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/inbound" and method == "POST":
            if whatsapp_inbound is None:
                result = {"ok": False, "error": "whatsapp inbound not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    sig = environ.get("HTTP_X_WACLI_SIGNATURE")
                    result = whatsapp_inbound(
                        payload, raw_body=raw, signature=sig
                    )
                    code = "200 OK" if result.get("ok", True) else "403 Forbidden"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    log.exception("whatsapp inbound handler failed")
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist/contacts" and method == "GET":
            if whatsapp_allowlist_contacts is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    qp = _parse_query(environ)
                    team_id = str(qp.get("team_id") or "").strip()
                    result = whatsapp_allowlist_contacts(team_id)
                    code = "200 OK"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist" and method == "GET":
            if whatsapp_allowlist_list is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_allowlist_list()
                    code = "200 OK"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist" and method == "POST":
            if whatsapp_allowlist_add is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = whatsapp_allowlist_add(payload)
                    code = "200 OK" if result.get("ok") else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist/update" and method == "POST":
            if whatsapp_allowlist_update is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = whatsapp_allowlist_update(payload)
                    code = "200 OK" if result.get("ok") else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist/remove" and method == "POST":
            if whatsapp_allowlist_remove is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = whatsapp_allowlist_remove(payload)
                    code = "200 OK" if result.get("ok") else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/allowlist/toggle" and method == "POST":
            if whatsapp_allowlist_toggle is None:
                result = {"ok": False, "error": "allowlist not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = whatsapp_allowlist_toggle(payload)
                    code = "200 OK" if result.get("ok") else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/agenda" and method == "GET":
            if whatsapp_agenda_list is None:
                result = {"ok": False, "error": "agenda not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_agenda_list(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/agenda/stats" and method == "GET":
            if whatsapp_agenda_stats is None:
                result = {"ok": False, "error": "agenda not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_agenda_stats()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/agenda/sync" and method == "POST":
            if whatsapp_agenda_sync is None:
                result = {"ok": False, "error": "agenda not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_agenda_sync()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/messages/search" and method == "GET":
            if whatsapp_messages_search is None:
                result = {"ok": False, "error": "message search not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_messages_search(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/whatsapp/messages/norma-candidates" and method == "GET":
            if whatsapp_norma_candidates is None:
                result = {"ok": False, "error": "norma candidates not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = whatsapp_norma_candidates(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/wacli/auth/status" and method == "GET":
            if wacli_auth_status is None:
                result = {"ok": False, "error": "wacli auth not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = wacli_auth_status()
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/wacli/auth/qr" and method in {"GET", "POST"}:
            if wacli_auth_qr is None:
                result = {"ok": False, "error": "wacli auth not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = wacli_auth_qr()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/wacli/auth/wait" and method == "GET":
            if wacli_auth_wait is None:
                result = {"ok": False, "error": "wacli auth not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    result = wacli_auth_wait(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "408 Request Timeout"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/wacli/unlock" and method in {"GET", "POST"}:
            if wacli_unlock is None:
                result = {"ok": False, "error": "wacli unlock not configured"}
                code = "503 Service Unavailable"
            else:
                payload: dict = {}
                if method == "POST":
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    if raw:
                        try:
                            parsed = json.loads(raw.decode("utf-8"))
                            if isinstance(parsed, dict):
                                payload = parsed
                        except json.JSONDecodeError:
                            payload = {}
                merged = {**_parse_query(environ), **payload}
                try:
                    result = wacli_unlock(merged)
                    code = "200 OK" if result.get("ok", True) else "409 Conflict"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/chat/tts":
            if chat_tts is None:
                body = b'{"ok":false,"error":"chat TTS not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = chat_tts(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/chat/stt":
            if chat_stt is None:
                body = b'{"ok":false,"error":"chat STT not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = chat_stt(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/api/cron/concurrency"):
            query = _parse_query(environ)

            def _cron_json(result: dict, *, fail: str = "502 Bad Gateway") -> list[bytes]:
                code = "200 OK" if result.get("ok", True) else fail
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/api/cron/concurrency":
                if cron_concurrency_status is None:
                    return _cron_json(
                        {"ok": False, "error": "cron concurrency not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    return _cron_json(cron_concurrency_status(query))
                except Exception as e:
                    return _cron_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "POST" and path == "/api/cron/concurrency/release":
                if cron_concurrency_release is None:
                    return _cron_json(
                        {"ok": False, "error": "cron concurrency not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    return _cron_json(cron_concurrency_release(payload))
                except json.JSONDecodeError:
                    return _cron_json(
                        {"ok": False, "error": "invalid JSON body"},
                        fail="400 Bad Request",
                    )
                except Exception as e:
                    return _cron_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            body = b'{"ok":false,"error":"unknown cron concurrency route"}'
            start_response("404 Not Found", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/api/errors":
            if errors_list is None:
                body = b'{"ok":false,"error":"error log not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            query = _parse_query(environ)
            try:
                result = errors_list(query)
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/ruflo/motores":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(ruflo_engines_body))),
                ],
            )
            return [ruflo_engines_body]

        if path.startswith("/ruflo/"):
            query = _parse_query(environ)
            user = _current_user(environ)

            def _ruflo_json(result: dict, *, fail: str = "502 Bad Gateway") -> list[bytes]:
                code = "200 OK" if result.get("ok", True) else fail
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/ruflo/status":
                if ruflo_status is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    return _ruflo_json(ruflo_status())
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "GET" and path == "/ruflo/sessions":
                if ruflo_sessions_list is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    return _ruflo_json(ruflo_sessions_list(query, user))
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method in {"GET", "POST"} and path == "/ruflo/session/new":
                if ruflo_session_new is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    merged = dict(query)
                    if method == "POST":
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        if raw:
                            payload = json.loads(raw.decode("utf-8") or "{}")
                            if isinstance(payload, dict):
                                merged.update(payload)
                    return _ruflo_json(ruflo_session_new(merged, user))
                except json.JSONDecodeError:
                    return _ruflo_json(
                        {"ok": False, "error": "invalid JSON body"},
                        fail="400 Bad Request",
                    )
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "POST" and path == "/ruflo/session/delete":
                if ruflo_session_delete is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload: dict = {}
                    if raw:
                        parsed = json.loads(raw.decode("utf-8") or "{}")
                        if isinstance(parsed, dict):
                            payload = parsed
                    merged = dict(query)
                    merged.update(payload)
                    return _ruflo_json(ruflo_session_delete(merged, user))
                except json.JSONDecodeError:
                    return _ruflo_json(
                        {"ok": False, "error": "invalid JSON body"},
                        fail="400 Bad Request",
                    )
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "GET" and path == "/ruflo/messages":
                if ruflo_messages_get is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    return _ruflo_json(ruflo_messages_get(query, user))
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "POST" and path == "/ruflo/route/preview":
                if ruflo_route_preview is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    return _ruflo_json(ruflo_route_preview(payload))
                except json.JSONDecodeError:
                    return _ruflo_json(
                        {"ok": False, "error": "invalid JSON body"},
                        fail="400 Bad Request",
                    )
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            if method == "POST" and path == "/ruflo/send":
                if ruflo_send is None:
                    return _ruflo_json(
                        {"ok": False, "error": "ruflo chat not configured"},
                        fail="503 Service Unavailable",
                    )
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    return _ruflo_json(ruflo_send(payload, user))
                except json.JSONDecodeError:
                    return _ruflo_json(
                        {"ok": False, "error": "invalid JSON body"},
                        fail="400 Bad Request",
                    )
                except Exception as e:
                    return _ruflo_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        fail="500 Internal Server Error",
                    )

            body = b'{"ok":false,"error":"unknown ruflo route"}'
            start_response("404 Not Found", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/api/ruflo/motores/"):
            def _motores_json(data: dict, status: str = "200 OK") -> list[bytes]:
                body_bytes = json.dumps(data, default=str).encode("utf-8")
                start_response(status, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body_bytes))),
                ])
                return [body_bytes]

            if method == "GET" and path == "/api/ruflo/motores/status":
                if ruflo_motores_status is None:
                    return _motores_json(
                        {"ok": False, "error": "ruflo motores not configured"},
                        status="503 Service Unavailable",
                    )
                try:
                    return _motores_json(ruflo_motores_status())
                except Exception as e:
                    return _motores_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        status="500 Internal Server Error",
                    )

            if method == "POST" and path == "/api/ruflo/motores/start":
                if ruflo_motores_start is None:
                    return _motores_json(
                        {"ok": False, "error": "ruflo motores not configured"},
                        status="503 Service Unavailable",
                    )
                try:
                    return _motores_json(ruflo_motores_start())
                except Exception as e:
                    return _motores_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        status="500 Internal Server Error",
                    )

            if method == "POST" and path == "/api/ruflo/motores/stop":
                if ruflo_motores_stop is None:
                    return _motores_json(
                        {"ok": False, "error": "ruflo motores not configured"},
                        status="503 Service Unavailable",
                    )
                try:
                    return _motores_json(ruflo_motores_stop())
                except Exception as e:
                    return _motores_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        status="500 Internal Server Error",
                    )

            if method == "POST" and path == "/api/ruflo/motores/rebuild":
                if ruflo_motores_rebuild is None:
                    return _motores_json(
                        {"ok": False, "error": "ruflo motores not configured"},
                        status="503 Service Unavailable",
                    )
                try:
                    return _motores_json(ruflo_motores_rebuild())
                except Exception as e:
                    return _motores_json(
                        {"ok": False, "error": f"{type(e).__name__}: {e}"},
                        status="500 Internal Server Error",
                    )

            body_err = b'{"ok":false,"error":"unknown method or route"}'
            start_response("404 Not Found", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body_err))),
            ])
            return [body_err]

        if path.startswith("/openclaw/"):
            query = _parse_query(environ)

            if method == "GET" and path == "/openclaw/sessions":
                if openclaw_sessions_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_sessions_list(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method in {"GET", "POST"} and path == "/openclaw/session/new":
                if openclaw_session_new is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    payload: dict = {}
                    if method == "POST":
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        if raw:
                            payload = json.loads(raw.decode("utf-8"))
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                    params = {**query, **payload}
                    result = openclaw_session_new(params, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/session/delete":
                if openclaw_session_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload: dict = {}
                    if raw:
                        parsed = json.loads(raw.decode("utf-8") or "{}")
                        if isinstance(parsed, dict):
                            payload = parsed
                    params = {**query, **payload}
                    result = openclaw_session_delete(
                        params, _current_user(environ)
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/sessions/purge":
                if openclaw_sessions_purge is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_sessions_purge(
                        payload, _current_user(environ)
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/session/assign-team":
                if chat_assign_team is None:
                    body = b'{"ok":false,"error":"chat assign-team not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = chat_assign_team(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/sessions/presence":
                if openclaw_session_presence is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_session_presence(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/session/swarm":
                if chat_session_swarm is None:
                    body = b'{"ok":false,"error":"chat swarm toggle not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = chat_session_swarm(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/memory":
                if openclaw_memory_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_memory_list(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/project-memory":
                if openclaw_project_memory_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_project_memory_get(
                        query, _current_user(environ)
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "PUT" and path == "/openclaw/project-memory":
                if openclaw_project_memory_put is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_project_memory_put(
                        payload, _current_user(environ)
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/messages":
                if openclaw_messages_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_messages_get(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/skills":
                if openclaw_skills_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_skills_list(query)
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/chat/failures":
                if openclaw_chat_failures is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_chat_failures(query)
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/models":
                if openclaw_models_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_models_list(query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/models/catalog":
                if openclaw_models_catalog is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_models_catalog(query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/models/sync":
                if openclaw_models_sync is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_models_sync()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/replicate/models":
                if openclaw_replicate_models is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_replicate_models(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/image-models/catalog":
                if openclaw_image_models_catalog is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_image_models_catalog(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/replicate/generate":
                if openclaw_replicate_generate is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_replicate_generate(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/chat-widgets/action":
                if openclaw_chat_widgets_action is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_chat_widgets_action(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/chat-widgets/book-image":
                if openclaw_chat_book_image is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_chat_book_image(dict(parse_qsl(environ.get("QUERY_STRING") or "")))
                    if isinstance(result, tuple):
                        data_bytes, mime, fname = result
                        start_response("200 OK", [
                            ("Content-Type", mime),
                            ("Content-Length", str(len(data_bytes))),
                            ("Content-Disposition", f'inline; filename="{fname}"'),
                            ("Cache-Control", "public, max-age=300"),
                        ])
                        return [data_bytes]
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                except Exception as e:
                    body = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "GET" and path == "/openclaw/chat-widgets/course-image":
                if openclaw_chat_course_image is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_chat_course_image(dict(parse_qsl(environ.get("QUERY_STRING") or "")))
                    if isinstance(result, tuple):
                        data_bytes, mime, fname = result
                        start_response("200 OK", [
                            ("Content-Type", mime),
                            ("Content-Length", str(len(data_bytes))),
                            ("Content-Disposition", f'inline; filename="{fname}"'),
                            ("Cache-Control", "public, max-age=300"),
                        ])
                        return [data_bytes]
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                except Exception as e:
                    body = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "GET" and path == "/openclaw/chat-widgets/epub-reader":
                if openclaw_chat_epub_reader is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_chat_epub_reader(dict(parse_qsl(environ.get("QUERY_STRING") or "")))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/codex/auth/status":
                if codex_auth_status is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = codex_auth_status()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if path == "/openclaw/codex/login/start" and method in {"GET", "POST"}:
                if codex_login_start is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = codex_login_start()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/codex/login/wait":
                if codex_login_wait is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = codex_login_wait(_parse_query(environ))
                    code = "200 OK" if result.get("ok", True) else "408 Request Timeout"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/integration":
                if openclaw_integration_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_integration_get(query)
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/tools":
                if openclaw_tools_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_tools_list(query)
                    code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/skill-dirs":
                if openclaw_skill_dirs_add is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_skill_dirs_add(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/connection":
                if openclaw_connection_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_connection_get()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/connection":
                if openclaw_connection_set is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_connection_set(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/fastpath":
                if openclaw_fastpath_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_fastpath_get()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/fastpath":
                if openclaw_fastpath_set is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_fastpath_set(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/token-mode":
                if openclaw_token_mode_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_token_mode_get()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/token-mode":
                if openclaw_token_mode_set is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_token_mode_set(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/tools-cap":
                if openclaw_tools_cap_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_tools_cap_get()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/tools-cap":
                if openclaw_tools_cap_set is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_tools_cap_set(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/tool-limit":
                if openclaw_tool_limit_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_tool_limit_get()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/tool-limit":
                if openclaw_tool_limit_set is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_tool_limit_set(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/media/preview":
                # Open a local media file in macOS Preview (fallback when inline fails)
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    file_path = str(payload.get("path") or "").strip()
                    if not file_path:
                        result = {"ok": False, "error": "path é obrigatório"}
                    else:
                        import subprocess as _sp
                        from pathlib import Path as _P
                        resolved = _P(file_path).expanduser().resolve()
                        if not resolved.is_file():
                            result = {"ok": False, "error": f"arquivo não encontrado: {resolved}"}
                        else:
                            _sp.Popen(["open", str(resolved)])
                            result = {"ok": True, "opened": str(resolved)}
                    code = "200 OK" if result.get("ok") else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/sessions/context":
                if openclaw_session_context_get is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_session_context_get(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method in {"GET", "POST"} and path == "/openclaw/sessions/context-documents":
                if openclaw_context_documents is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    payload: dict = {}
                    if method == "POST":
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        if raw:
                            payload = json.loads(raw.decode("utf-8"))
                            if not isinstance(payload, dict):
                                raise ValueError("body must be a JSON object")
                    params = {**query, **payload}
                    if method == "GET" and "action" not in params:
                        params["action"] = "list"
                    result = openclaw_context_documents(params, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/visual-memory/from-photo":
                if openclaw_visual_memory_from_photo is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_visual_memory_from_photo(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/profile/from-photo":
                if openclaw_profile_from_photo is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_profile_from_photo(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/visual-memory/profiles":
                if openclaw_visual_memory_profiles is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_visual_memory_profiles(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/visual-memory/preview":
                if openclaw_visual_memory_preview is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_visual_memory_preview(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/generated-images":
                if openclaw_generated_images_list is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_generated_images_list(query, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/generated-images/delete":
                if openclaw_generated_images_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_generated_images_delete(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/gallery-image/route":
                if openclaw_gallery_image_route is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_gallery_image_route(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/minicurriculum/html":
                if openclaw_minicurriculum_html is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    html_bytes, err = openclaw_minicurriculum_html(query, _current_user(environ))
                    if err or html_bytes is None:
                        body = json.dumps({"ok": False, "error": err or "not found"}).encode()
                        start_response("404 Not Found", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    start_response("200 OK", [
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(html_bytes))),
                    ])
                    return [html_bytes]
                except Exception as e:
                    body = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "POST" and path == "/openclaw/attachments/upload":
                if openclaw_attachment_upload is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_attachment_upload(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/artifacts/download":
                if openclaw_artifact_download is None:
                    body = b'{"ok":false,"error":"artifact download not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    qs = environ.get("QUERY_STRING") or ""
                    qp: dict[str, str] = {}
                    for part in qs.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            from urllib.parse import unquote_plus
                            qp[unquote_plus(k)] = unquote_plus(v)
                    result = openclaw_artifact_download(qp, _current_user(environ))
                    if isinstance(result, tuple):
                        data_bytes, mime, fname = result
                        headers = _artifact_download_headers(
                            data_bytes, mime, fname, preview=_is_download_preview(qp)
                        )
                        start_response("200 OK", headers)
                        return [data_bytes]
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "GET" and (
                path == "/openclaw/team-files/download"
                or path.startswith("/team-files/download/")
            ):
                try:
                    from urllib.parse import unquote_plus

                    qs = environ.get("QUERY_STRING") or ""
                    qp: dict[str, str] = {}
                    for part in qs.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            qp[unquote_plus(k)] = unquote_plus(v)
                    if path.startswith("/team-files/download/"):
                        slug = path[len("/team-files/download/") :].strip("/")
                        if slug and not qp.get("team_id"):
                            qp["team_id"] = slug.split("/", 1)[0]
                    from .team_files_download import serve_team_file_download

                    result = serve_team_file_download(qp)
                    if isinstance(result, tuple):
                        data_bytes, mime, fname = result
                        headers = _team_file_download_headers(
                            data_bytes, mime, fname, preview=_is_download_preview(qp)
                        )
                        start_response("200 OK", headers)
                        return [data_bytes]
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode()
                    start_response("500 Internal Server Error", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

            if method == "POST" and path == "/openclaw/send":
                if openclaw_send is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_send(payload, _current_user(environ))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/send/status":
                if openclaw_send_status is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            query[qkey] = unquote(value)
                try:
                    result = openclaw_send_status(query)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/send/cancel":
                if openclaw_send_cancel is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_send_cancel(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/send/pending":
                if openclaw_send_pending is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_send_pending()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/send/queue":
                if openclaw_send_queue is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_send_queue()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/tool-jobs":
                if openclaw_tool_jobs is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                query = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            query[qkey] = unquote(value)
                try:
                    result = openclaw_tool_jobs(query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/jobs/running":
                if openclaw_jobs_running is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_jobs_running()
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/jobs/cancel":
                if openclaw_jobs_cancel is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = openclaw_jobs_cancel(payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/books/library":
                if openclaw_books_library is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            query[qkey] = unquote(value)
                try:
                    result = openclaw_books_library(query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/articles/library":
                if openclaw_articles_library is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                articles_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            articles_query[qkey] = unquote(value)
                try:
                    result = openclaw_articles_library(articles_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "DELETE" and path == "/openclaw/articles/delete":
                if openclaw_article_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                delete_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            delete_query[qkey] = unquote(value)
                workspace_id = str(
                    delete_query.get("workspace_id") or delete_query.get("ws") or ""
                ).strip()
                article_id = str(
                    delete_query.get("article_id") or delete_query.get("id") or ""
                ).strip()
                title_fallback = str(delete_query.get("title") or "").strip()
                if not workspace_id or not article_id:
                    body = b'{"ok":false,"error":"workspace_id and article_id are required"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_article_delete(
                        workspace_id,
                        article_id,
                        title_fallback=title_fallback,
                    )
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/books/structure":
                if openclaw_book_structure is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                struct_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            struct_query[qkey] = unquote(value)
                try:
                    result = openclaw_book_structure(struct_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path.startswith("/openclaw/books/") and path.endswith("/cover"):
                if openclaw_book_cover is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                book_id = path[len("/openclaw/books/") : -len("/cover")].strip("/")
                if not book_id:
                    body = b'{"ok":false,"error":"book_id is required"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_book_cover(book_id)
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if not result.get("ok"):
                    err = str(result.get("error") or "not found")
                    body = json.dumps({"ok": False, "error": err}, default=str).encode()
                    start_response("404 Not Found", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                data = result.get("content") or b""
                if isinstance(data, str):
                    data = data.encode()
                media_type = str(result.get("media_type") or "image/jpeg")
                start_response("200 OK", [
                    ("Content-Type", media_type),
                    ("Content-Disposition", 'inline; filename="cover.jpg"'),
                    ("Cache-Control", "public, max-age=300"),
                    ("Content-Length", str(len(data))),
                ])
                return [data]

            if method == "GET" and path.startswith("/openclaw/books/") and path.endswith("/epub"):
                if openclaw_book_epub is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                book_id = path[len("/openclaw/books/") : -len("/epub")].strip("/")
                if not book_id:
                    body = b'{"ok":false,"error":"book_id is required"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_book_epub(book_id)
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if not result.get("ok"):
                    err = str(result.get("error") or "not found")
                    body = json.dumps({"ok": False, "error": err}, default=str).encode()
                    start_response("404 Not Found", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                data = result.get("content") or b""
                if isinstance(data, str):
                    data = data.encode()
                filename = str(result.get("filename") or "livro.epub")
                safe_name = filename.replace('"', "")
                start_response("200 OK", [
                    ("Content-Type", "application/epub+zip"),
                    ("Content-Disposition", f'attachment; filename="{safe_name}"'),
                    ("Cache-Control", "no-store"),
                    ("Content-Length", str(len(data))),
                ])
                return [data]

            if method == "DELETE" and path.startswith("/openclaw/books/"):
                if openclaw_book_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                rest = path[len("/openclaw/books/"):].strip("/")
                if not rest or rest in ("library", "structure") or rest.endswith("/cover") or rest.endswith("/epub"):
                    body = b'{"ok":false,"error":"invalid book delete path"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                book_id = unquote(rest.split("/")[0])
                title_fallback = ""
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey == "title":
                            title_fallback = unquote(value)
                try:
                    result = openclaw_book_delete(book_id, title_fallback=title_fallback)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/courses/library":
                if openclaw_courses_library is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                course_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            course_query[qkey] = unquote(value)
                try:
                    result = openclaw_courses_library(course_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/courses/structure":
                if openclaw_course_structure is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                course_struct_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            course_struct_query[qkey] = unquote(value)
                try:
                    result = openclaw_course_structure(course_struct_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path.startswith("/openclaw/courses/") and path.endswith("/export/html-site"):
                if openclaw_course_html_site is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                course_id = path[len("/openclaw/courses/") : -len("/export/html-site")].strip("/")
                if not course_id:
                    body = b'{"ok":false,"error":"course_id is required"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_course_html_site(course_id)
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if not result.get("ok"):
                    err = str(result.get("error") or "not found")
                    body = json.dumps({"ok": False, "error": err}, default=str).encode()
                    start_response("404 Not Found", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                data = result.get("content") or b""
                if isinstance(data, str):
                    data = data.encode()
                filename = str(result.get("filename") or "curso_site_html.zip")
                safe_name = filename.replace('"', "")
                start_response("200 OK", [
                    ("Content-Type", "application/zip"),
                    ("Content-Disposition", f'attachment; filename="{safe_name}"'),
                    ("Cache-Control", "no-store"),
                    ("Content-Length", str(len(data))),
                ])
                return [data]

            if method == "DELETE" and path.startswith("/openclaw/courses/"):
                if openclaw_course_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                rest = path[len("/openclaw/courses/"):].strip("/")
                if not rest or rest in ("library", "structure") or rest.endswith("/export/html-site"):
                    body = b'{"ok":false,"error":"invalid course delete path"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                course_id = unquote(rest.split("/")[0])
                title_fallback = ""
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey == "title":
                            title_fallback = unquote(value)
                try:
                    result = openclaw_course_delete(course_id, title_fallback=title_fallback)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/sagas/library":
                if openclaw_sagas_library is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                saga_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            saga_query[qkey] = unquote(value)
                try:
                    result = openclaw_sagas_library(saga_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/sagas/structure":
                if openclaw_saga_structure is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                saga_struct_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            saga_struct_query[qkey] = unquote(value)
                try:
                    result = openclaw_saga_structure(saga_struct_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "DELETE" and path.startswith("/openclaw/sagas/"):
                rest = path[len("/openclaw/sagas/"):].strip("/")
                parts = [unquote(p) for p in rest.split("/") if p]

                if len(parts) >= 5 and parts[1] == "stories" and parts[3] == "pages" and openclaw_page_delete is not None:
                    saga_id, _, story_id, _, page_id = parts[0], parts[1], parts[2], parts[3], parts[4]
                    page_number = 0
                    qs = environ.get("QUERY_STRING") or ""
                    if qs:
                        for chunk in qs.split("&"):
                            qkey, _, value = chunk.partition("=")
                            if qkey == "page_number":
                                try:
                                    page_number = int(unquote(value))
                                except (TypeError, ValueError):
                                    page_number = 0
                    try:
                        result = openclaw_page_delete(
                            page_id,
                            story_id=story_id,
                            saga_id=saga_id,
                            page_number=page_number,
                        )
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if len(parts) == 3 and parts[1] == "stories" and openclaw_story_delete is not None:
                    saga_id, _, story_id = parts[0], parts[1], parts[2]
                    title_fallback = ""
                    qs = environ.get("QUERY_STRING") or ""
                    if qs:
                        for chunk in qs.split("&"):
                            qkey, _, value = chunk.partition("=")
                            if qkey == "title":
                                title_fallback = unquote(value)
                    try:
                        result = openclaw_story_delete(
                            story_id,
                            saga_id=saga_id,
                            title_fallback=title_fallback,
                        )
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if len(parts) >= 3 and parts[1] in ("magazines", "revistas") and openclaw_magazine_delete is not None:
                    saga_id, _, magazine_ref = parts[0], parts[1], parts[2]
                    magazine_index = -1
                    magazine_id = ""
                    job_id = ""
                    qs = environ.get("QUERY_STRING") or ""
                    if qs:
                        for chunk in qs.split("&"):
                            qkey, _, value = chunk.partition("=")
                            if qkey == "index":
                                try:
                                    magazine_index = int(unquote(value))
                                except (TypeError, ValueError):
                                    magazine_index = -1
                            elif qkey in ("magazine_id", "job_id", "revista_id"):
                                magazine_id = unquote(value)
                                job_id = magazine_id
                    if magazine_ref.isdigit():
                        magazine_index = int(magazine_ref)
                    elif magazine_ref:
                        magazine_id = magazine_ref
                        job_id = magazine_ref
                    try:
                        result = openclaw_magazine_delete(
                            saga_id,
                            magazine_index=magazine_index,
                            magazine_id=magazine_id,
                            job_id=job_id,
                        )
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if openclaw_saga_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                rest = path[len("/openclaw/sagas/"):].strip("/")
                if not rest or rest in ("library", "structure"):
                    body = b'{"ok":false,"error":"invalid saga delete path"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                saga_id = unquote(rest.split("/")[0])
                title_fallback = ""
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey == "title":
                            title_fallback = unquote(value)
                try:
                    result = openclaw_saga_delete(saga_id, title_fallback=title_fallback)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "DELETE" and path.startswith("/openclaw/scripts/"):
                rest = path[len("/openclaw/scripts/"):].strip("/")
                parts = [unquote(p) for p in rest.split("/") if p]

                if len(parts) >= 3 and parts[1] == "acts" and openclaw_script_act_delete is not None:
                    job_id, _, act_ref = parts[0], parts[1], parts[2]
                    try:
                        act_number = int(act_ref)
                    except (TypeError, ValueError):
                        body = b'{"ok":false,"error":"invalid act_number"}'
                        start_response("400 Bad Request", [
                            ("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                        ])
                        return [body]
                    try:
                        result = openclaw_script_act_delete(job_id, act_number)
                        code = "200 OK" if result.get("ok", True) else "404 Not Found"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
                    body = json.dumps(result, default=str).encode()
                    start_response(code, [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]

                if openclaw_script_delete is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                if not rest or rest in ("library", "structure"):
                    body = b'{"ok":false,"error":"invalid script delete path"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                job_id = parts[0] if parts else ""
                title_fallback = ""
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey == "title":
                            title_fallback = unquote(value)
                try:
                    result = openclaw_script_delete(job_id, title_fallback=title_fallback)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/scripts/library":
                if openclaw_scripts_library is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                script_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            script_query[qkey] = unquote(value)
                try:
                    result = openclaw_scripts_library(script_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/user/profile-dock":
                if openclaw_user_profile_dock is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                profile_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            profile_query[qkey] = unquote(value)
                try:
                    result = openclaw_user_profile_dock(profile_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/user/profile-dock/generate-image":
                if openclaw_user_profile_dock_generate is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    raw = environ.get("wsgi.input").read(int(environ.get("CONTENT_LENGTH") or 0))
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    body = b'{"ok":false,"error":"invalid JSON body"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_user_profile_dock_generate(payload if isinstance(payload, dict) else {})
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/user/profile-dock/remove-photo":
                if openclaw_user_profile_dock_remove_photo is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    raw = environ.get("wsgi.input").read(int(environ.get("CONTENT_LENGTH") or 0))
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    body = b'{"ok":false,"error":"invalid JSON body"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_user_profile_dock_remove_photo(
                        payload if isinstance(payload, dict) else {}
                    )
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "GET" and path == "/openclaw/scripts/structure":
                if openclaw_script_structure is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                script_struct_query: dict[str, str] = {}
                qs = environ.get("QUERY_STRING") or ""
                if qs:
                    for chunk in qs.split("&"):
                        qkey, _, value = chunk.partition("=")
                        if qkey:
                            script_struct_query[qkey] = unquote(value)
                try:
                    result = openclaw_script_structure(script_struct_query)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            if method == "POST" and path == "/openclaw/scripts/act-image":
                if openclaw_script_act_image is None:
                    body = b'{"ok":false,"error":"openclaw chat not configured"}'
                    start_response("503 Service Unavailable", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    raw = environ.get("wsgi.input").read(int(environ.get("CONTENT_LENGTH") or 0))
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    body = b'{"ok":false,"error":"invalid JSON body"}'
                    start_response("400 Bad Request", [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = openclaw_script_act_image(payload if isinstance(payload, dict) else {})
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if method == "GET" and path == "/skills/suggestions":
            if skill_suggestions_list is None:
                body = b'{"ok":false,"error":"skill discovery not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = skill_suggestions_list()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/skills/suggestions/scan":
            if skill_suggestions_scan is None:
                body = b'{"ok":false,"error":"skill discovery not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = skill_suggestions_scan()
                code = "200 OK" if result.get("ok", True) else "502 Bad Gateway"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/skills/suggestions/dismiss":
            if skill_suggestions_dismiss is None:
                body = b'{"ok":false,"error":"skill discovery not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                result = skill_suggestions_dismiss(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/active-agents":
            if active_agents_snapshot is None:
                body = b'{"ok":false,"error":"active agents snapshot not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = active_agents_snapshot()
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path in ("/hermes/cron/create", "/hermes/cron/preview"):
            handler = (
                hermes_cron_create if path == "/hermes/cron/create" else hermes_cron_preview
            )
            if handler is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = handler(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/knowledge" or path.startswith("/knowledge/"):
            if path == "/knowledge" and method == "GET":
                handler = knowledge_get
                payload_query = True
            elif path == "/knowledge/extract" and method == "POST":
                handler = knowledge_extract
                payload_query = False
            elif path == "/knowledge/delete_project" and method == "POST":
                handler = knowledge_delete_project
                payload_query = False
            elif path == "/knowledge/delete_session" and method == "POST":
                handler = knowledge_delete_session
                payload_query = False
            else:
                body = json.dumps({"ok": False, "error": "method not allowed"}).encode()
                start_response("405 Method Not Allowed", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            if handler is None:
                body = json.dumps(
                    {"ok": False, "error": "knowledge base não configurada"}
                ).encode()
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                if payload_query:
                    qs = environ.get("QUERY_STRING") or ""
                    q: dict[str, str] = {}
                    if qs:
                        for chunk in qs.split("&"):
                            if "=" in chunk:
                                k, v = chunk.split("=", 1)
                                q[unquote_plus(k)] = unquote_plus(v)
                    result = handler(q)
                else:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = handler(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/entity-db" and method == "GET":
            if entity_db_get is None:
                body = json.dumps(
                    {"ok": False, "error": "knowledge base não configurada"}
                ).encode()
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                qs = environ.get("QUERY_STRING") or ""
                q: dict[str, str] = {}
                if qs:
                    for chunk in qs.split("&"):
                        if "=" in chunk:
                            k, v = chunk.split("=", 1)
                            q[unquote_plus(k)] = unquote_plus(v)
                result = entity_db_get(q)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/cron/swarm-timeline":
            if hermes_cron_swarm_timeline is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            query_tl: dict[str, str] = {}
            qs = environ.get("QUERY_STRING") or ""
            if qs:
                for chunk in qs.split("&"):
                    key, _, value = chunk.partition("=")
                    if key:
                        query_tl[key] = unquote(value)
            try:
                result = hermes_cron_swarm_timeline(query_tl)
                code = "200 OK" if result.get("ok", True) else "503 Service Unavailable"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "GET" and path == "/hermes/cron":
            if hermes_cron_list is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            query: dict[str, str] = {}
            qs = environ.get("QUERY_STRING") or ""
            if qs:
                for chunk in qs.split("&"):
                    key, _, value = chunk.partition("=")
                    if key:
                        query[key] = unquote(value)
            try:
                result = hermes_cron_list(query)
                code = "200 OK" if result.get("ok", True) else "503 Service Unavailable"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/cron/catch-up":
            if hermes_cron_catch_up is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            query_catch: dict[str, str] = {}
            qs = environ.get("QUERY_STRING") or ""
            if qs:
                for chunk in qs.split("&"):
                    key, _, value = chunk.partition("=")
                    if key:
                        query_catch[key] = unquote(value)
            try:
                result = hermes_cron_catch_up(query_catch)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/cron/resume-all":
            if hermes_cron_resume_all is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_cron_resume_all()
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/cron/force-remove":
            if hermes_cron_force_remove is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                result = hermes_cron_force_remove(payload)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except json.JSONDecodeError:
                result = {"ok": False, "error": "invalid JSON body"}
                code = "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if method == "POST" and path == "/hermes/cron/force-remove-all":
            if hermes_cron_force_remove_all is None:
                body = json.dumps(
                    {"ok": False, "error": "hermes cron is not configured"}
                ).encode()
                start_response("404 Not Found", [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            try:
                result = hermes_cron_force_remove_all()
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(code, [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith("/hermes/cron/"):
            parts = [p for p in path.split("/") if p]
            query = environ.get("QUERY_STRING") or ""
            run_file = None
            if query:
                for chunk in query.split("&"):
                    key, _, value = chunk.partition("=")
                    if key == "run" and value:
                        run_file = unquote(value)
                        break

            # GET /hermes/cron/{job_id}/result
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "hermes"
                and parts[1] == "cron"
                and parts[3] == "result"
            ):
                if hermes_cron_result is None:
                    body = json.dumps(
                        {"ok": False, "error": "hermes cron is not configured"}
                    ).encode()
                    start_response("404 Not Found", [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                try:
                    result = hermes_cron_result(unquote(parts[2]), run_file=run_file)
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            # GET /hermes/cron/{job_id}/source
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "hermes"
                and parts[1] == "cron"
                and parts[3] == "source"
            ):
                if hermes_cron_source is None:
                    body = json.dumps(
                        {"ok": False, "error": "hermes cron is not configured"}
                    ).encode()
                    start_response("404 Not Found", [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                query_map: dict[str, str] = {}
                if query:
                    for chunk in query.split("&"):
                        key, _, value = chunk.partition("=")
                        if key:
                            query_map[unquote(key)] = unquote(value)
                want_raw = str(query_map.get("raw") or "").lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                try:
                    result = hermes_cron_source(unquote(parts[2]))
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                if want_raw and result.get("ok"):
                    content = str(result.get("content") or "")
                    body = content.encode("utf-8", errors="replace")
                    source_name = str(result.get("source_path") or "source.txt").split("/")[-1]
                    start_response("200 OK", [
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Cache-Control", "no-cache"),
                        (
                            "Content-Disposition",
                            f'inline; filename="{source_name}"',
                        ),
                        ("Content-Length", str(len(body))),
                    ])
                    return [body]
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            # GET /hermes/cron/{job_id}
            if (
                method == "GET"
                and len(parts) == 3
                and parts[0] == "hermes"
                and parts[1] == "cron"
                and hermes_cron_get is not None
            ):
                try:
                    result = hermes_cron_get(unquote(parts[2]))
                    code = "200 OK" if result.get("ok", True) else "404 Not Found"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            # POST /hermes/cron/{job_id}/edit
            if (
                method == "POST"
                and len(parts) == 4
                and parts[0] == "hermes"
                and parts[1] == "cron"
                and parts[3] == "edit"
                and hermes_cron_edit is not None
            ):
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = hermes_cron_edit(unquote(parts[2]), payload)
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            # POST /hermes/cron/{job_id}/{action}  action ∈ pause, resume, run, remove
            if (
                method == "POST"
                and len(parts) == 4
                and parts[0] == "hermes"
                and parts[1] == "cron"
                and hermes_cron_action is not None
            ):
                try:
                    result = hermes_cron_action(unquote(parts[2]), unquote(parts[3]))
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

        if path in ("/metricas", "/metrics/ui"):
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(metrics_ui_body))),
                ],
            )
            return [metrics_ui_body]
        if path == "/metrics":
            if method == "GET" and _wants_metrics_html(environ):
                start_response(
                    "200 OK",
                    [
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Cache-Control", "no-cache"),
                        ("Content-Length", str(len(metrics_ui_body))),
                    ],
                )
                return [metrics_ui_body]
            return prom_app(environ, start_response)
        if path == "/ready":
            body = b'{"ok":true}'
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        if path == "/status":
            body = json.dumps(status_provider(), default=str, indent=2).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        if path == "/status/painel.json":
            from .services_panel_status import (
                build_services_panel_from_snapshot,
                build_services_panel_offline,
            )

            try:
                snap = status_provider()
                payload = build_services_panel_from_snapshot(snap)
            except Exception as exc:
                from pathlib import Path

                payload = build_services_panel_offline(
                    error=str(exc),
                    project_dir=Path.cwd(),
                )
            body = json.dumps(payload, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        if path == "/status/painel":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(status_panel_body))),
                ],
            )
            return [status_panel_body]
        if path == "/api/health":
            from gois.health_check import build_health_report

            report = build_health_report()
            status_code = (
                "200 OK"
                if report["status"] == "ok"
                else "503 Service Unavailable" if report["status"] == "down"
                else "200 OK"
            )
            body = json.dumps(report, default=str, indent=2).encode()
            start_response(
                status_code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        _lite_redirect = lite_redirect_for_html_path(path, method=method)
        if _lite_redirect:
            start_response(
                "302 Found",
                [
                    ("Location", _lite_redirect),
                    ("Content-Length", "0"),
                ],
            )
            return []
        if path == "/":
            # Chat is the default landing screen (one of the three main screens).
            # Build fresh per request (like /chat) so chat.html edits are picked up
            # without restarting the server, and forbid caching of the stale HTML.
            chat_live = build_chat_html(hermes_dashboard_url).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Pragma", "no-cache"),
                    ("Content-Length", str(len(chat_live))),
                ],
            )
            return [chat_live]
        if path == "/ui":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(html_body))),
                ],
            )
            return [html_body]
        if path == "/ruflo":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(ruflo_monitor_body))),
                ],
            )
            return [ruflo_monitor_body]
        if path == "/popup/team-file-preview":
            popup_live = build_team_file_preview_popup_html().encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Pragma", "no-cache"),
                    ("Content-Length", str(len(popup_live))),
                ],
            )
            return [popup_live]
        if path == "/chat":
            chat_live = build_chat_html(hermes_dashboard_url).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Pragma", "no-cache"),
                    ("Content-Length", str(len(chat_live))),
                ],
            )
            return [chat_live]
        if path == "/chat/ruflo":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(ruflo_chat_body))),
                ],
            )
            return [ruflo_chat_body]
        if path == "/chat/perguntas":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(perguntas_body))),
                ],
            )
            return [perguntas_body]
        if path == "/erros":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(errors_body))),
                ],
            )
            return [errors_body]
        if path == "/saude":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(health_body))),
                ],
            )
            return [health_body]
        if path == "/cron/slots":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(cron_slots_body))),
                ],
            )
            return [cron_slots_body]
        if path in ("/cron/custos", "/cron/costs"):
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(cron_costs_body))),
                ],
            )
            return [cron_costs_body]
        if path in ("/modelos", "/modelos/quotas"):
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(model_quotas_body))),
                ],
            )
            return [model_quotas_body]

        if path in ("/modelos/custos", "/modelos/costs"):
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(model_costs_body))),
                ],
            )
            return [model_costs_body]

        if path == "/modelos/cotas" and method in {"GET", "POST"}:
            user = _current_user(environ)
            if method == "GET":
                result = (
                    model_quotas_get(user)
                    if model_quotas_get
                    else {"ok": False, "error": "model quotas not configured"}
                )
                err = str(result.get("error") or "").lower()
                if "auth" in err:
                    code = "401 Unauthorized"
                else:
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            else:
                if model_quotas_update is None:
                    result = {"ok": False, "error": "model quotas not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = model_quotas_update(payload, user)
                        err = str(result.get("error") or "").lower()
                        if "admin" in err:
                            code = "403 Forbidden"
                        elif "auth" in err:
                            code = "401 Unauthorized"
                        else:
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(
                code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/config/chaves" and method in {"GET", "POST"}:
            user = _current_user(environ)
            if method == "GET":
                result = (
                    env_keys_get(user)
                    if env_keys_get
                    else {"ok": False, "error": "env keys not configured"}
                )
                err = str(result.get("error") or "").lower()
                if "admin" in err:
                    code = "403 Forbidden"
                elif "auth" in err:
                    code = "401 Unauthorized"
                else:
                    code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            else:
                if env_keys_update is None:
                    result = {"ok": False, "error": "env keys not configured"}
                    code = "503 Service Unavailable"
                else:
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        raw = environ["wsgi.input"].read(length) if length else b""
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("body must be a JSON object")
                        result = env_keys_update(payload, user)
                        err = str(result.get("error") or "").lower()
                        if "admin" in err:
                            code = "403 Forbidden"
                        elif "auth" in err:
                            code = "401 Unauthorized"
                        else:
                            code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                    except json.JSONDecodeError:
                        result = {"ok": False, "error": "invalid JSON body"}
                        code = "400 Bad Request"
                    except Exception as e:
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(
                code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/config/chaves/import" and method == "POST":
            user = _current_user(environ)
            if env_keys_import is None:
                result = {"ok": False, "error": "env keys not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = env_keys_import(payload, user)
                    err = str(result.get("error") or "").lower()
                    if "admin" in err:
                        code = "403 Forbidden"
                    elif "auth" in err:
                        code = "401 Unauthorized"
                    else:
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(
                code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/config/chaves/import-roteiro-viral" and method == "POST":
            user = _current_user(environ)
            if env_keys_import_roteiro_viral is None:
                result = {"ok": False, "error": "env keys not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = env_keys_import_roteiro_viral(payload, user)
                    err = str(result.get("error") or "").lower()
                    if "admin" in err:
                        code = "403 Forbidden"
                    elif "auth" in err:
                        code = "401 Unauthorized"
                    else:
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(
                code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/config/chaves/prune-env" and method == "POST":
            user = _current_user(environ)
            if env_keys_prune is None:
                result = {"ok": False, "error": "env keys not configured"}
                code = "503 Service Unavailable"
            else:
                try:
                    length = int(environ.get("CONTENT_LENGTH") or "0")
                    raw = environ["wsgi.input"].read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("body must be a JSON object")
                    result = env_keys_prune(payload, user)
                    err = str(result.get("error") or "").lower()
                    if "admin" in err:
                        code = "403 Forbidden"
                    elif "auth" in err:
                        code = "401 Unauthorized"
                    else:
                        code = "200 OK" if result.get("ok", True) else "400 Bad Request"
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "invalid JSON body"}
                    code = "400 Bad Request"
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = "500 Internal Server Error"
            body = json.dumps(result, default=str).encode()
            start_response(
                code,
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/agentes":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(active_agents_body))),
                ],
            )
            return [active_agents_body]
        if path == "/swarm":
            swarm_page = build_swarm_html(
                hermes_dashboard_url,
                dashboard_render_config=dashboard_render_config,
            ).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(swarm_page))),
                ],
            )
            return [swarm_page]
        if path == "/swarm/perfis":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(swarm_profiles_body))),
                ],
            )
            return [swarm_profiles_body]
        if path == "/gerenciar/apagar":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(manage_delete_body))),
                ],
            )
            return [manage_delete_body]
        if path == "/login":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(login_body))),
                ],
            )
            return [login_body]
        if path == "/skills":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(skills_body))),
                ],
            )
            return [skills_body]
        if path == "/allowlist":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(allowlist_body))),
                ],
            )
            return [allowlist_body]
        if path == "/chaves":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(env_keys_body))),
                ],
            )
            return [env_keys_body]
        if path == "/agenda":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(agenda_body))),
                ],
            )
            return [agenda_body]
        if path == "/roles":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(roles_body))),
                ],
            )
            return [roles_body]
        if path == "/agents/novo":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(agent_create_body))),
                ],
            )
            return [agent_create_body]
        if path == "/jobs/novo":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(cron_create_body))),
                ],
            )
            return [cron_create_body]
        if path == "/ide":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(ide_body))),
                ],
            )
            return [ide_body]
        if path.startswith("/ide/"):
            query = _parse_query(environ)

            def _ide_json(result: dict, *, fail: str = "400 Bad Request") -> list[bytes]:
                code = "200 OK" if result.get("ok", True) else fail
                body = json.dumps(result, default=str).encode()
                start_response(code, [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]

            def _ide_body() -> dict:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                raw = environ["wsgi.input"].read(length) if length else b""
                payload = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                return payload

            try:
                if method == "GET" and path == "/ide/cli":
                    return _ide_json(ide_runtime.cli_availability())
                if method == "GET" and path == "/ide/tree":
                    return _ide_json(ide_runtime.list_tree(query.get("path", "")))
                if method == "GET" and path == "/ide/file":
                    return _ide_json(ide_runtime.read_file(query.get("path", "")))
                if method == "POST" and path == "/ide/file":
                    payload = _ide_body()
                    return _ide_json(ide_runtime.write_file(
                        payload.get("path", ""), payload.get("content", "")
                    ))
                if method == "POST" and path == "/ide/run":
                    return _ide_json(ide_runtime.start_run(_ide_body()))
                if method == "GET" and path == "/ide/run":
                    return _ide_json(ide_runtime.run_status(query))
                if method == "POST" and path == "/ide/run/cancel":
                    return _ide_json(ide_runtime.run_cancel(_ide_body()))
            except ValueError as exc:
                return _ide_json({"ok": False, "error": str(exc)})
            except json.JSONDecodeError:
                return _ide_json({"ok": False, "error": "invalid JSON body"})
            except Exception as exc:  # pragma: no cover - defensive
                return _ide_json(
                    {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                    fail="500 Internal Server Error",
                )

            return _ide_json({"ok": False, "error": "unknown ide route"},
                             fail="404 Not Found")
        if path == "/conhecimento":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(knowledge_body))),
                ],
            )
            return [knowledge_body]
        if path == "/entidades":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(entity_db_body))),
                ],
            )
            return [entity_db_body]
        if path == "/memoria":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(project_memory_body))),
                ],
            )
            return [project_memory_body]
        if path == "/times":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Content-Length", str(len(teams_ui_body))),
                ],
            )
            return [teams_ui_body]
        if path == "/times/novo":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(team_create_body))),
                ],
            )
            return [team_create_body]
        if path == "/times/mensagens":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(team_messages_body))),
                ],
            )
            return [team_messages_body]
        if path == "/times/detalhes":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(team_detail_body))),
                ],
            )
            return [team_detail_body]
        if path == "/kanban":
            kanban_live = build_kanban_html(hermes_dashboard_url).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate"),
                    ("Content-Length", str(len(kanban_live))),
                ],
            )
            return [kanban_live]

        if path == "/projetos":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(projects_body))),
                ],
            )
            return [projects_body]
        if path == "/mcp-cards":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(mcp_cards_body))),
                ],
            )
            return [mcp_cards_body]
        if path == "/mcp":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(mcp_servers_body))),
                ],
            )
            return [mcp_servers_body]
        if path == "/api/mcp/catalog" and method == "GET":
            from .mcp_catalog import build_mcp_catalog

            snap = status_provider() if status_provider else {}
            try:
                result = build_mcp_catalog(mcp_status=snap.get("mcp_servers"))
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/mcp/servers" and method == "GET":
            from .mcp_servers_store import list_servers

            try:
                result = list_servers()
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "servers": []}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/mcp/servers" and method == "POST":
            from .mcp_external_chat import sync_server_tools
            from .mcp_servers_store import create_server

            try:
                body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                body_data = json.loads(body_raw.decode("utf-8") or "{}")
                result = create_server(body_data)
                if result.get("ok") and body_data.get("sync_tools", True):
                    server = result.get("server") or {}
                    sync = sync_server_tools(str(server.get("id") or ""))
                    result["sync"] = sync
                    if sync.get("ok"):
                        from .mcp_servers_store import get_server

                        refreshed = get_server(str(server.get("id") or ""))
                        if refreshed.get("ok"):
                            result["server"] = refreshed.get("server")
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path.startswith("/api/mcp/servers/") and method in {"PUT", "DELETE", "POST"}:
            from .mcp_external_chat import sync_server_tools
            from .mcp_servers_store import delete_server, get_server, update_server

            parts = [p for p in path.split("/") if p]
            server_id = parts[3] if len(parts) >= 4 else ""
            action = parts[4] if len(parts) >= 5 else ""
            try:
                if method == "DELETE" and not action:
                    result = delete_server(server_id)
                elif method == "PUT" and not action:
                    body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                    body_data = json.loads(body_raw.decode("utf-8") or "{}")
                    result = update_server(server_id, body_data)
                    if result.get("ok") and body_data.get("sync_tools"):
                        sync = sync_server_tools(server_id)
                        result["sync"] = sync
                        refreshed = get_server(server_id)
                        if refreshed.get("ok"):
                            result["server"] = refreshed.get("server")
                elif method == "POST" and action == "sync":
                    result = sync_server_tools(server_id)
                    refreshed = get_server(server_id)
                    if refreshed.get("ok"):
                        result["server"] = refreshed.get("server")
                else:
                    result = {"ok": False, "error": "unsupported MCP server action"}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/calendario":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(calendar_body))),
                ],
            )
            return [calendar_body]
        if path == "/api/calendar/config":
            try:
                result = calendar_config()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/events":
            params = _parse_query(environ)
            year = int(params.get("year") or datetime.now().year)
            month = int(params.get("month") or datetime.now().month)
            try:
                result = calendar_list_events(year, month)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/status" and method == "GET":
            try:
                result = calendar_status()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/sync" and method == "POST":
            try:
                body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                body_data = json.loads(body_raw.decode("utf-8") or "{}")
                days = int(body_data.get("days") or 30)
                result = calendar_sync_google(days=days)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/create":
            if method == "POST":
                try:
                    body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                    body_data = json.loads(body_raw.decode("utf-8"))
                    result = calendar_create_event(body_data)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
            else:
                result = {"ok": False, "error": "POST required"}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/notion/status" and method == "GET":
            try:
                result = notion_status()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/notion/sync" and method == "POST":
            try:
                body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                body_data = json.loads(body_raw.decode("utf-8") or "{}")
                days = int(body_data.get("days") or 30)
                result = notion_sync(days=days, full=True)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path == "/api/calendar/notion/configure" and method == "POST":
            try:
                body_raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH", 0) or 0))
                body_data = json.loads(body_raw.decode("utf-8") or "{}")
                db_id = str(body_data.get("database_id") or "").strip()
                if not db_id:
                    result = {"ok": False, "error": "database_id obrigatório"}
                else:
                    result = notion_configure(db_id)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            resp_json = json.dumps(result, default=str).encode()
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(resp_json))),
                ],
            )
            return [resp_json]
        if path in ("/fila-prioridades", "/priority-queue-ui"):
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(priority_queue_body))),
                ],
            )
            return [priority_queue_body]
        if path == "/users":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(users_body))),
                ],
            )
            return [users_body]
        if path == "/latex":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(latex_body))),
                ],
            )
            return [latex_body]
        if path == "/artigos/qualidade":
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(article_quality_body))),
                ],
            )
            return [article_quality_body]
        if method == "POST" and path == "/api/alerting/clear":
            if alerting_clear is None:
                body = b'{"ok":false,"error":"alerting not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            result = alerting_clear()
            body = json.dumps(result, default=str).encode()
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # ── MCP Skills API ─────────────────────────────────────────────────
        if path == "/mcp/skills" and method in ("GET", "POST"):
            query = _parse_query(environ)
            if mcp_skills is None:
                body = b'{"ok":false,"error":"mcp skills not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            payload: dict = {}
            if method == "POST":
                try:
                    raw = environ["wsgi.input"].read(
                        int(environ.get("CONTENT_LENGTH") or 0)
                    )
                    payload = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                except (json.JSONDecodeError, OSError, ValueError):
                    payload = {}
            try:
                result = mcp_skills(query, payload if method == "POST" else None)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            start_response(code, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # ── MCP Cards API ──────────────────────────────────────────────────
        if path == "/mcp/cards" and method in ("GET", "POST"):
            query = _parse_query(environ)
            if mcp_cards is None:
                body = b'{"ok":false,"error":"mcp cards not configured"}'
                start_response("503 Service Unavailable", [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ])
                return [body]
            payload_cards: dict = {}
            if method == "POST":
                try:
                    raw = environ["wsgi.input"].read(
                        int(environ.get("CONTENT_LENGTH") or 0)
                    )
                    payload_cards = json.loads(raw.decode("utf-8") or "{}") if raw else {}
                except (json.JSONDecodeError, OSError, ValueError):
                    payload_cards = {}
            try:
                result = mcp_cards(query, payload_cards if method == "POST" else None)
                code = "200 OK" if result.get("ok", True) else "400 Bad Request"
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                code = "500 Internal Server Error"
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            start_response(code, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # ── MCP Cards API (legacy UI paths) ────────────────────────────────
        if path == "/api/mcp-cards/boards" and method == "GET":
            from .mcp_cards_server import _handle_list_kanban_boards

            params = _parse_query(environ)
            quick = params.get("quick", "1").lower() not in {"0", "false", "no"}
            try:
                result = _handle_list_kanban_boards({"quick": quick})
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "boards": [], "count": 0}
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            code = "200 OK" if result.get("ok", True) else "500 Internal Server Error"
            start_response(code, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store, no-cache, must-revalidate"),
                ("Pragma", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/api/mcp-cards/cards" and method == "GET":
            from .mcp_cards_server import _handle_get_cards

            params = _parse_query(environ)
            try:
                result = _handle_get_cards({
                    "workdir": params.get("workdir", ""),
                    "column": params.get("column", ""),
                    "assignee": params.get("assignee", ""),
                })
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "cards": [], "count": 0}
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            code = "200 OK" if result.get("ok", True) else "500 Internal Server Error"
            start_response(code, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store, no-cache, must-revalidate"),
                ("Pragma", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/api/mcp-cards/move" and method == "POST":
            from .mcp_cards_server import _handle_move_card
            try:
                raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            result = _handle_move_card(payload)
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            status = "200 OK" if result.get("ok") else "400 Bad Request"
            start_response(status, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path == "/api/mcp-cards/boards" and method == "DELETE":
            from .mcp_cards_server import _handle_delete_board

            params = _parse_query(environ)
            try:
                raw = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            result = _handle_delete_board({
                "workdir": payload.get("workdir") or params.get("workdir", ""),
                "team_id": payload.get("team_id") or params.get("team_id", ""),
            })
            body = json.dumps(result, default=str, ensure_ascii=False).encode()
            status = "200 OK" if result.get("ok") else "400 Bad Request"
            start_response(status, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if path.startswith(
            ("/swarm/", "/hermes/", "/auth/", "/teams/", "/api/", "/status")
        ):
            body = json.dumps(
                {"ok": False, "error": "not found", "path": path},
                default=str,
            ).encode()
            start_response(
                "404 Not Found",
                [
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-cache"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        body = b"not found\n"
        start_response(
            "404 Not Found",
            [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))],
        )
        return [body]

    wrapped_app = _gzip_etag_middleware(app)

    from . import metrics as metrics_mod

    try:
        server = metrics_mod.make_server(
            host, port, wrapped_app, server_class=_ThreadingWSGIServer
        )
    except OSError as e:
        log.error("could not bind HTTP server on %s:%d: %s", host, port, e)
        return None

    thread = threading.Thread(
        target=server.serve_forever, name="qclaw-http", daemon=True
    )
    thread.start()
    log.info(
        "HTTP server listening on http://%s:%d (/, /ruflo, /agentes, /agents/novo, /chat, /chat/ruflo, /chat/perguntas, /erros, /cron/slots, /roles, /times, /times/novo, /skills, /metrics/ui, /status, /metrics)",
        host,
        port,
    )
    return server

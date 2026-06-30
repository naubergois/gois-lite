"""OpenClaw chat status and native tools cache for /status."""

from __future__ import annotations

import logging
import time
from typing import Any

from .chat_models import effective_chat_default_model_id, list_chat_models
from .openclaw_tools import openclaw_native_tools_status

log = logging.getLogger(__name__)

_OPENCLAW_TOOLS_CACHE_SECONDS = 120.0
_CHAT_TOOLS_CACHE_SECONDS = 120.0
_CHAT_MODELS_CACHE_SECONDS = 120.0


class MonitorOpenclawChatStatusMixin:
    def _cached_openclaw_tools_counts(self) -> dict[str, Any]:
        """OpenClaw native tool catalog (cached; refreshed on /status poll)."""
        from .gois_lite import is_gois_lite

        if is_gois_lite():
            return {
                "agent_id": "",
                "tools_profile": "gois-lite",
                "tools_profile_source": "gois-lite",
                "catalog_total": 0,
                "tools_enabled": 0,
                "tools_available": 0,
                "gateway_catalog": False,
                "scan_error": None,
            }
        now = time.time()
        if (
            now < self._openclaw_tools_cache_expires_at
            and self._openclaw_tools_cache is not None
        ):
            return dict(self._openclaw_tools_cache)

        cc = self.cfg.openclaw_chat
        snap: dict[str, Any] = {
            "agent_id": cc.default_agent,
            "tools_profile": "full",
            "tools_profile_source": "padrão",
            "catalog_total": 0,
            "tools_enabled": 0,
            "tools_available": 0,
            "gateway_catalog": False,
            "scan_error": None,
        }
        if cc.enabled:
            try:
                runtime = self._openclaw_runtime()
                snap = openclaw_native_tools_status(
                    runtime,
                    self.cfg.openclaw_doctor,
                    agent_id=cc.default_agent,
                )
            except Exception as e:
                snap["scan_error"] = f"{type(e).__name__}: {e}"
                log.debug("openclaw native tools status failed: %s", e)
        self._openclaw_tools_cache = dict(snap)
        self._openclaw_tools_cache_expires_at = now + _OPENCLAW_TOOLS_CACHE_SECONDS
        return dict(snap)

    def _cached_chat_tools_stats(self) -> dict[str, Any]:
        """QClaw chat tool catalogue counts (cached; refreshed on /status poll)."""
        now = time.time()
        if (
            now < getattr(self, "_chat_tools_cache_expires_at", 0.0)
            and getattr(self, "_chat_tools_cache", None) is not None
        ):
            return dict(self._chat_tools_cache)

        cc = self.cfg.openclaw_chat
        snap: dict[str, Any] = {
            "catalog_total": 0,
            "catalog_lazy_base": 0,
            "catalog_mcp_linked": 0,
            "error": None,
        }
        if cc.enabled and cc.qclaw_tools_enabled:
            try:
                from .chat_tools_status import build_chat_tools_status
                from .openclaw_chat import _openclaw_cli_env

                runtime = self._openclaw_runtime()
                bin_path, _env = _openclaw_cli_env(runtime, self.cfg.openclaw_doctor)
                wa = self.cfg.whatsapp_digest.recipient
                snap = build_chat_tools_status(
                    self.recovery,
                    cc,
                    cli_available=bool(bin_path)
                    and bool(getattr(cc, "openclaw_connection_enabled", True)),
                    hermes_agent_create_enabled=bool(
                        self.cfg.hermes_agent_create.enabled
                    ),
                    kanban_create_card_enabled=bool(
                        self.cfg.hermes_agent_create.enabled
                    ),
                    whatsapp_send_enabled=bool(wa),
                    allowlist_enabled=True,
                )
            except Exception as e:
                snap["error"] = f"{type(e).__name__}: {e}"
                log.debug("chat tools stats failed: %s", e)
        self._chat_tools_cache = dict(snap)
        self._chat_tools_cache_expires_at = now + _CHAT_TOOLS_CACHE_SECONDS
        return dict(snap)

    def _cached_chat_models_list(self) -> list[dict[str, Any]]:
        """Full chat model catalogue (cached; expensive Mongo/catalog scan)."""
        now = time.time()
        if (
            now < getattr(self, "_chat_models_cache_expires_at", 0.0)
            and getattr(self, "_chat_models_cache", None) is not None
        ):
            return list(self._chat_models_cache)

        cc = self.cfg.openclaw_chat
        models = list_chat_models(cc) if cc.enabled else []
        self._chat_models_cache = list(models)
        self._chat_models_cache_expires_at = now + _CHAT_MODELS_CACHE_SECONDS
        return list(models)

    def _openclaw_chat_ui_payload(self, *, include_models: bool = True) -> dict[str, Any]:
        """Chat config + cached tool catalogue stats (no OpenClaw CLI tool scan)."""
        cc = self.cfg.openclaw_chat
        from .chat_prompt_policy import resolve_token_limits, token_mode_status

        limits = resolve_token_limits(cc)
        agent_iters = int(self.cfg.agent.max_tool_iterations)
        chat_iters = int(cc.max_tool_iterations)
        token_status = token_mode_status(cc)
        payload: dict[str, Any] = {
            "enabled": cc.enabled,
            "backend": (cc.backend or "deepseek").strip().lower(),
            "default_model_id": effective_chat_default_model_id(cc.default_model_id),
            "token_usage_mode": token_status.get("mode"),
            "token_limits": token_status.get("limits"),
            "max_attachments": cc.max_attachments,
            "max_attachment_bytes": cc.max_attachment_bytes,
            "max_context_doc_bytes": cc.max_context_doc_bytes,
            "qclaw_tools_enabled": cc.qclaw_tools_enabled,
            "heygen_mcp_enabled": cc.heygen_mcp_enabled,
            "suno_mcp_enabled": cc.suno_mcp_enabled,
            "seedance_mcp_enabled": cc.seedance_mcp_enabled,
            "external_mcp_enabled": cc.external_mcp_enabled,
            "virtual_band_enabled": cc.virtual_band_enabled,
            "roteiro_thumbnails_enabled": cc.roteiro_thumbnails_enabled,
            "roteiro_mongo_enabled": cc.roteiro_mongo_enabled,
            "roteiro_api_enabled": cc.roteiro_api_enabled,
            "shell_enabled": cc.shell_enabled,
            "max_tool_iterations": chat_iters,
            "agent_max_tool_iterations": agent_iters,
            "max_tool_iterations_effective": limits.max_tool_iterations,
            "tools_cap_override": getattr(cc, "tools_cap_override", None),
            "max_tools_cap_effective": limits.max_tools_cap,
            "chat_failures_enabled": bool(cc.enabled and cc.qclaw_tools_enabled),
            "chat_tools": self._cached_chat_tools_stats(),
        }
        from .gois_lite import is_gois_lite

        if is_gois_lite():
            payload.update(
                {
                    "gois_lite": True,
                    "openclaw_connection_enabled": False,
                    "heygen_mcp_enabled": False,
                    "suno_mcp_enabled": False,
                    "seedance_mcp_enabled": False,
                    "external_mcp_enabled": False,
                    "virtual_band_enabled": False,
                    "roteiro_thumbnails_enabled": False,
                    "roteiro_mongo_enabled": False,
                    "roteiro_api_enabled": False,
                    "shell_enabled": False,
                    "chat_failures_enabled": False,
                }
            )
        try:
            from .chat_rv_screens import rv_frontend_base

            payload["rv_frontend_base"] = rv_frontend_base() or ""
        except Exception:
            payload["rv_frontend_base"] = ""
        if include_models:
            payload["models"] = self._cached_chat_models_list()
        return payload

    def _openclaw_chat_status(self) -> dict[str, Any]:
        """Public chat/tool settings for /status (incl. OpenClaw native tool scan)."""
        return {
            **self._openclaw_chat_ui_payload(),
            **self._cached_openclaw_tools_counts(),
        }

    def handle_openclaw_chat_failures(self, query: dict) -> dict[str, Any]:
        """Unified chat failures for /openclaw/chat/failures and qclaw_chat_failures tool."""
        from pathlib import Path

        from .chat_failures import collect_chat_failures
        from .local_paths import project_stack_root

        try:
            since_minutes = float(query.get("since_minutes") or 120.0)
        except (TypeError, ValueError):
            since_minutes = 120.0
        try:
            limit = int(query.get("limit") or 40)
        except (TypeError, ValueError):
            limit = 40
        text_query = str(query.get("query") or "").strip()
        session_key = str(query.get("session_key") or "").strip()
        raw_sources = query.get("sources")
        sources: list[str] | None = None
        if isinstance(raw_sources, str) and raw_sources.strip():
            sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
        elif isinstance(raw_sources, list):
            sources = [str(s).strip() for s in raw_sources if str(s).strip()]

        history_path = None
        cc = self.cfg.openclaw_chat
        if cc.history_enabled:
            history_path = Path(cc.history_db_path).expanduser()
            if not history_path.is_absolute():
                history_path = (project_stack_root().parent / history_path).resolve()

        return collect_chat_failures(
            since_minutes=since_minutes,
            limit=limit,
            query=text_query,
            session_key=session_key,
            sources=sources,
            history_path=history_path,
        )


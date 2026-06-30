"""OpenClaw chat send HTTP handlers (async queue + attachments + TTS)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from .accounts import UserRecord
from .chat_jobs import (
    append_progress as chat_job_append_progress,
    complete_job as chat_job_complete,
    create_job as chat_job_create,
    enrich_slide_continuation_message,
    fail_job as chat_job_fail,
    find_running_chat_job_for_client_key,
    get_job as chat_job_get,
    has_running_chat_job_for_session,
    batch_complete_on_disk,
    is_job_cancelled as chat_job_is_cancelled,
    job_to_dict,
    prime_slide_job_from_message,
    prime_slide_job_from_session,
    slide_job_needs_auto_continue,
)
from .chat_models import (
    effective_chat_default_model_id,
    parse_attachments,
    persist_attachments,
    resolve_attachments_dir,
    resolve_chat_model,
)
from .model_router import resolve_effective_model_id
from .openclaw_chat import (
    _normalize_chat_attachments,
    default_session_key,
    openclaw_key_for_hermes_profile,
    parse_hermes_session_key,
    send_chat_message,
)
from .openclaw_chat_media_tools import media_payload_from_job_id
from .chat_send_queue import enqueue_session_chat_send
from .ruflo_chat import (
    build_swarm_system_prompt,
    format_ruflo_reasoning,
    gather_orchestration_context,
    handle_pending_bash_approval,
    maybe_autoexec_bash,
)
from .chat_tool_background import (
    mirror_chat_status_throttled,
    start_chat_progress_mirror,
)
from .tool_progress import with_model_prefix

log = logging.getLogger(__name__)


def _merge_job_media_into_result(job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Keep tool-generated media on the final job result when the LLM text fails."""
    if not isinstance(result, dict):
        return result
    if result.get("media"):
        return result
    media = media_payload_from_job_id(job_id)
    if not media:
        return result
    merged = dict(result)
    merged["media"] = media
    return merged


class MonitorOpenclawSendHandlersMixin:
    _SLIDE_AUTO_CONTINUE_MAX_DEPTH = 25

    def _spawn_slide_auto_continue_job(
        self,
        *,
        parent_job,
        send_key: str,
        display_key: str,
        send_agent: Optional[str],
        profile: Optional[str],
        user: Optional[UserRecord],
        model_id: Optional[str],
        depth: int,
    ) -> None:
        from .chat_jobs import (
            enrich_slide_continuation_message,
            prime_slide_job_from_message,
            prime_slide_job_from_session,
        )
        from .chat_tool_background import chat_session_exists

        if not chat_session_exists(self.chat_persistence, display_key):
            log.info(
                "slide auto-continue skipped for %s — conversation removed",
                display_key,
            )
            return

        send_text = enrich_slide_continuation_message(
            "continuar",
            self.chat_persistence,
            display_key,
            history_key=send_key,
        )
        if send_text.strip().lower() == "continuar":
            return
        job = chat_job_create(
            send_key,
            display_key=display_key,
            message_text="continuar",
            agent_id=send_agent,
            profile=profile,
            user_id=self._chat_user_id(user),
            model_id=str(model_id).strip() if model_id else parent_job.model_id,
        )
        prime_slide_job_from_message(job.id, send_text)
        prime_slide_job_from_session(
            self.chat_persistence, job.id, display_key, history_key=send_key
        )
        resolved, _ = resolve_chat_model(
            self.cfg.openclaw_chat,
            str(model_id).strip() if model_id else parent_job.model_id,
        )
        model_label = resolved.entry.label if resolved else ""
        chat_job_append_progress(
            job.id,
            with_model_prefix(
                f"Continuação automática do lote ({parent_job.block_turn}/{parent_job.block_max})…",
                model_label,
            ),
        )
        self._schedule_openclaw_send_job(
            display_key,
            job.id,
            send_key=send_key,
            display_key=display_key,
            text=send_text,
            send_agent=send_agent,
            profile=profile,
            user=user,
            model_id=str(model_id).strip() if model_id else parent_job.model_id,
            brief_target=None,
            attachments=None,
            auto_continue_depth=depth,
        )
        log.info(
            "auto-continuing slide batch job=%s session=%s progress=%s/%s depth=%d",
            job.id,
            display_key,
            parent_job.block_turn,
            parent_job.block_max,
            depth,
        )

    def _schedule_openclaw_send_job(self, queue_key: str, job_id: str, **kwargs: Any) -> None:
        """Enqueue a chat send; serializes per UI conversation (display_key)."""
        client_key = (kwargs.get("display_key") or queue_key or "").strip()
        payload = {"job_id": job_id, **kwargs}
        enqueue_session_chat_send(
            client_key,
            job_id,
            payload,
            self._run_openclaw_send_job,
        )

    def _run_openclaw_send_job(
        self,
        job_id: str,
        *,
        send_key: str,
        display_key: str,
        text: str,
        send_agent: Optional[str],
        profile: Optional[str],
        user: Optional[UserRecord],
        model_id: Optional[str] = None,
        brief_target: Optional[str] = None,
        image_model: Optional[str] = None,
        attachments: Optional[list] = None,
        auto_continue_depth: int = 0,
        swarm: bool = False,
        team_id: Optional[str] = None,
    ) -> None:
        cc = self.cfg.openclaw_chat
        runtime = self._openclaw_runtime()
        history_key = (display_key or send_key or "").strip()
        mirror_last: dict[str, Any] = {"text": "", "at": 0.0}

        def on_progress(message: str) -> None:
            chat_job_append_progress(job_id, message)
            if self.chat_persistence is not None and history_key:
                mirror_chat_status_throttled(
                    self.chat_persistence,
                    history_key,
                    message,
                    last_posted=mirror_last,
                    min_interval=6.0,
                )

        chat_job_append_progress(job_id, "Agente a iniciar…")
        if self.chat_persistence is not None and history_key:
            mirror_chat_status_throttled(
                self.chat_persistence,
                history_key,
                "Agente a iniciar…",
                last_posted=mirror_last,
                force=True,
            )
        from .long_task_hints import (
            format_long_task_warning,
            long_task_hint_for_user_message,
        )

        user_long_hint = long_task_hint_for_user_message(text)
        if user_long_hint:
            warning = format_long_task_warning(user_long_hint)
            chat_job_append_progress(job_id, warning)
            if self.chat_persistence is not None and history_key:
                mirror_chat_status_throttled(
                    self.chat_persistence,
                    history_key,
                    warning,
                    last_posted=mirror_last,
                    force=True,
                )
        mirror_stop = start_chat_progress_mirror(
            job_id,
            persistence=self.chat_persistence,
            session_key=history_key,
            interval_seconds=12.0,
        )
        team_info = self._resolve_team_info_for_session(
            send_key, user, team_id=team_id
        )
        # "Modo Swarm": gather RuFlo orchestration (routing + memory + swarm/hive
        # status), surface the reasoning, and augment the model system prompt.
        send_cc = cc
        swarm_active = bool(
            swarm
            and getattr(self.cfg, "ruflo_chat", None)
            and self.cfg.ruflo_chat.enabled
        )
        preloaded_catalog = None
        if swarm_active:
            ruflo_cfg = self.cfg.ruflo_chat
            # Parallel: load skills + gather orchestration context
            from .openclaw_skills import list_openclaw_skills
            try:
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pre-llm") as pool:
                    ruflo_future = pool.submit(
                        gather_orchestration_context,
                        ruflo_cfg, runtime, text, on_progress=on_progress,
                    )
                    skills_future = pool.submit(
                        list_openclaw_skills,
                        runtime,
                        agent_id=send_agent,
                        gois_extra_dirs=list(
                            getattr(self.cfg.openclaw_chat, "extra_skill_dirs", None) or []
                        ),
                    )
                    ctx = ruflo_future.result(timeout=45)
                    preloaded_catalog = skills_future.result(timeout=30)
            except Exception as exc:
                log.warning("parallel pre-LLM failed for %s: %s", send_key, exc)
                ctx = {}
                preloaded_catalog = None
            block = str(ctx.get("orchestration_block") or "").strip()
            routing = ctx.get("routing") if isinstance(ctx.get("routing"), dict) else None
            memory_hits = ctx.get("memory_hits") or []
            reasoning = format_ruflo_reasoning(
                routing=routing,
                routing_error=ctx.get("routing_error"),
                memory_hits=memory_hits if isinstance(memory_hits, list) else [],
            )
            if self.chat_persistence is not None and reasoning:
                try:
                    self.chat_persistence.history.append_message(
                        send_key, role="reasoning", text=reasoning
                    )
                    from .chat_jobs import append_partial_reasoning

                    append_partial_reasoning(job_id, reasoning)
                except Exception as e:  # pragma: no cover - defensive
                    log.warning("could not persist swarm reasoning for %s: %s", send_key, e)
            if block:
                merged_prompt = build_swarm_system_prompt(
                    cc.system_prompt,
                    ruflo_cfg.system_prompt,
                    block,
                )
                send_cc = cc.model_copy(update={"system_prompt": merged_prompt})
        try:
            result = send_chat_message(
                runtime,
                self.cfg.openclaw_doctor,
                send_cc,
                self.cfg.agent,
                session_key=send_key,
                message=text,
                agent_id=send_agent,
                model_id=model_id,
                brief_target=brief_target,
                image_model=image_model,
                attachments=attachments,
                qclaw_tools=self._openclaw_chat_tool_context(user),
                persistence=self.chat_persistence,
                project_store=self.project_memory,
                user_id=self._chat_user_id(user),
                team_info=team_info,
                on_progress=on_progress,
                user_already_saved=True,
                progress_job_id=job_id,
                preloaded_skills_catalog=preloaded_catalog,
            )
            if swarm_active and isinstance(result, dict) and result.get("ok"):
                reply_text = str(result.get("reply") or "")
                summary = maybe_autoexec_bash(
                    reply_text,
                    cfg=self.cfg.ruflo_chat,
                    runtime=runtime,
                    session_key=send_key,
                    persistence=self.chat_persistence,
                    on_status=on_progress,
                )
                if summary:
                    result["reply"] = (reply_text + "\n\n" + summary).strip()
            result = self._apply_hermes_chat_result(result, profile, display_key)
            result = _merge_job_media_into_result(job_id, result)
            if chat_job_is_cancelled(job_id):
                # User clicked stop — do not overwrite cancelled status.
                return
            if result.get("ok"):
                reply = (result.get("reply") or result.get("text") or "").strip()
                if not reply:
                    fail_result = _merge_job_media_into_result(
                        job_id,
                        {
                            **result,
                            "ok": False,
                            "error": "modelo concluiu sem texto de resposta",
                        },
                    )
                    chat_job_fail(
                        job_id,
                        "modelo concluiu sem texto de resposta",
                        result=fail_result,
                    )
                    # Persist error as a visible message in chat history
                    if self.chat_persistence is not None:
                        try:
                            persist_extras = (
                                {"media": fail_result["media"]}
                                if fail_result.get("media")
                                else None
                            )
                            self.chat_persistence.history.append_message(
                                send_key,
                                role="assistant",
                                text="⚠️ **Erro:** modelo concluiu sem texto de resposta",
                                extras=persist_extras,
                            )
                        except Exception as _persist_exc:
                            log.warning("could not persist error message to chat history for %s: %s", send_key, _persist_exc)
                    return
                self._mirror_assistant_reply_to_whatsapp(send_key, reply)
                loaded = chat_job_get(job_id)
                chat_job_complete(job_id, result)
                # Check auto-continue AFTER completing the chat job, so
                # has_running_chat_job_for_session won't find this job itself.
                needs_continue = (
                    loaded is not None
                    and slide_job_needs_auto_continue(loaded)
                    and auto_continue_depth < self._SLIDE_AUTO_CONTINUE_MAX_DEPTH
                )
                if (
                    needs_continue
                    and loaded is not None
                    and not has_running_chat_job_for_session(display_key)
                ):
                    from .chat_tool_background import chat_session_exists

                    if not chat_session_exists(self.chat_persistence, display_key):
                        log.info(
                            "slide auto-continue blocked for %s — conversation removed",
                            display_key,
                        )
                    else:
                        self._spawn_slide_auto_continue_job(
                            parent_job=loaded,
                            send_key=send_key,
                            display_key=display_key,
                            send_agent=send_agent,
                            profile=profile,
                            user=user,
                            model_id=model_id,
                            depth=auto_continue_depth + 1,
                        )
            else:
                err_msg = str(result.get("error") or "envio falhou")
                fail_result = _merge_job_media_into_result(
                    job_id,
                    result if isinstance(result, dict) else {"ok": False, "error": err_msg},
                )
                chat_job_fail(
                    job_id,
                    err_msg,
                    result=fail_result,
                )
                # Persist error as a visible message in chat history
                if self.chat_persistence is not None:
                    try:
                        persist_extras = (
                            {"media": fail_result["media"]}
                            if isinstance(fail_result, dict) and fail_result.get("media")
                            else None
                        )
                        self.chat_persistence.history.append_message(
                            send_key,
                            role="assistant",
                            text=f"⚠️ **Erro:** {err_msg}",
                            extras=persist_extras,
                        )
                    except Exception as _persist_exc:
                        log.warning("could not persist error message to chat history for %s: %s", send_key, _persist_exc)
        except Exception as e:
            log.exception("async chat send failed job=%s: %s", job_id, e)
            if not chat_job_is_cancelled(job_id):
                err_msg = f"{type(e).__name__}: {e}"
                chat_job_fail(job_id, err_msg)
                # Persist error as a visible message in chat history
                if self.chat_persistence is not None:
                    try:
                        self.chat_persistence.history.append_message(
                            send_key,
                            role="assistant",
                            text=f"⚠️ **Erro:** {err_msg}",
                        )
                    except Exception as _persist_exc:
                        log.warning("could not persist error message to chat history for %s: %s", send_key, _persist_exc)
        finally:
            mirror_stop.set()

    def handle_openclaw_models_list(self, query: Optional[dict] = None) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .llm_models_catalog import catalog_meta, get_catalog_doc, maybe_schedule_catalog_sync, serialize_catalog_doc
        from .model_router import auto_model_catalog_entry

        cc = self.cfg.openclaw_chat
        maybe_schedule_catalog_sync()
        q = query or {}
        slim = str(q.get("slim") or "").lower() in ("1", "true", "yes")
        base = {
            "ok": True,
            "default_model_id": effective_chat_default_model_id(cc.default_model_id),
            "max_attachments": cc.max_attachments,
            "max_attachment_bytes": cc.max_attachment_bytes,
            "max_context_doc_bytes": cc.max_context_doc_bytes,
            "attachments_temp_dir": str(resolve_attachments_dir(cc)),
            "catalog_meta": catalog_meta(),
        }
        if slim:
            from .chat_models import model_entry_available, resolve_llm_api_key
            from .llm_models_catalog import _doc_to_entry

            models: list[dict[str, Any]] = [auto_model_catalog_entry()]
            want_id = str(q.get("model_id") or "").strip()
            if want_id and want_id != "auto":
                doc = get_catalog_doc(want_id)
                if doc:
                    entry = _doc_to_entry(doc)
                    if entry.provider == "codex_cli":
                        avail = model_entry_available(entry)
                    else:
                        avail = bool(resolve_llm_api_key(entry.api_key_env))
                    models.append(serialize_catalog_doc(doc, available=avail))
            return {**base, "models": models}
        return {**base, "models": self._cached_chat_models_list()}

    def handle_openclaw_models_catalog(self, query: Optional[dict] = None) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .llm_models_catalog import search_catalog

        q = query or {}
        try:
            limit = int(q.get("limit") or 60)
        except (TypeError, ValueError):
            limit = 60
        try:
            offset = int(q.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        return search_catalog(
            query=str(q.get("q") or q.get("query") or ""),
            group=str(q.get("group") or "").strip(),
            tools_only=str(q.get("tools") or "").lower() in ("1", "true", "yes"),
            free_only=str(q.get("free") or "").lower() in ("1", "true", "yes"),
            coding_only=str(q.get("coding") or "").lower() in ("1", "true", "yes"),
            image_only=str(q.get("images") or q.get("image") or "").lower()
            in ("1", "true", "yes"),
            available_only=str(q.get("available") or "").lower() in ("1", "true", "yes"),
            limit=limit,
            offset=offset,
        )

    def handle_openclaw_models_sync(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .llm_models_catalog import sync_llm_models_catalog

        return sync_llm_models_catalog(force_openrouter=True)

    def handle_openclaw_replicate_models(self, query: Optional[dict] = None) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .replicate_generate import list_replicate_models_payload

        q = query or {}
        return list_replicate_models_payload(
            modality=str(q.get("modality") or q.get("kind") or "").strip(),
            query=str(q.get("q") or q.get("query") or "").strip(),
        )

    def handle_openclaw_image_models_catalog(self, query: Optional[dict] = None) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .image_models_store import catalog_for_chat_picker

        q = query or {}
        try:
            limit = int(q.get("limit") or 120)
        except (TypeError, ValueError):
            limit = 120
        return catalog_for_chat_picker(
            query=str(q.get("q") or q.get("query") or ""),
            provider=str(q.get("provider") or "").strip(),
            limit=limit,
        )

    def handle_openclaw_replicate_generate(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .replicate_generate import dispatch_replicate_generate

        return dispatch_replicate_generate(body if isinstance(body, dict) else {})

    def handle_openclaw_chat_widgets_action(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_creative_widgets import dispatch_widget_action

        payload = dict(body if isinstance(body, dict) else {})
        payload["_persistence"] = self.chat_persistence
        return dispatch_widget_action(payload)

    def handle_openclaw_chat_epub_reader(self, query: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_creative_widgets import dispatch_epub_reader_get

        return dispatch_epub_reader_get(query if isinstance(query, dict) else {})

    def handle_openclaw_chat_book_image(self, query: dict[str, Any]):
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_epub_editor import dispatch_book_image_get

        return dispatch_book_image_get(query if isinstance(query, dict) else {})

    def handle_openclaw_chat_course_image(self, query: dict[str, Any]):
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_html_editor import dispatch_course_image_get

        return dispatch_course_image_get(query if isinstance(query, dict) else {})

    def handle_codex_auth_status(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .codex_cli import codex_binary, codex_login_status_text

        if not codex_binary():
            return {
                "ok": False,
                "error": "Codex CLI não encontrado no PATH",
                "authenticated": False,
            }
        return {"ok": True, **codex_login_status_text()}

    def handle_codex_login_start(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .codex_cli import codex_login_start

        return codex_login_start()

    def handle_codex_login_wait(self, query: Optional[dict] = None) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .codex_cli import codex_login_wait

        q = query or {}
        try:
            timeout = float(q.get("timeout") or 180.0)
        except (TypeError, ValueError):
            timeout = 180.0
        return codex_login_wait(timeout=timeout)

    def handle_openclaw_connection_get(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        return {
            "ok": True,
            "enabled": bool(
                getattr(self.cfg.openclaw_chat, "openclaw_connection_enabled", True)
            ),
        }

    def handle_openclaw_connection_set(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        raw = payload.get("enabled")
        if isinstance(raw, str):
            enabled = raw.strip().lower() in ("1", "true", "on", "yes")
        else:
            enabled = bool(raw)
        # Update the live config so the change takes effect immediately.
        self.cfg.openclaw_chat.openclaw_connection_enabled = enabled
        persisted = True
        try:
            from .openclaw_integration import set_openclaw_connection

            result = set_openclaw_connection(self.cfg.openclaw_chat, enabled, persist=True)
            persisted = bool(result.get("persisted"))
        except Exception as exc:
            persisted = False
            log.warning(
                "could not persist openclaw_connection_enabled to MongoDB: %s", exc
            )
        log.info(
            "openclaw connection %s via dashboard (persisted=%s)",
            "enabled" if enabled else "disabled",
            persisted,
        )
        return {"ok": True, "enabled": enabled, "persisted": persisted}

    def handle_openclaw_fastpath_get(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        return {
            "ok": True,
            "enabled": bool(getattr(self.cfg.openclaw_chat, "fastpath_enabled", True)),
        }

    def handle_openclaw_fastpath_set(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        raw = payload.get("enabled")
        if isinstance(raw, str):
            enabled = raw.strip().lower() in ("1", "true", "on", "yes")
        else:
            enabled = bool(raw)
        self.cfg.openclaw_chat.fastpath_enabled = enabled
        persisted = True
        try:
            from .openclaw_integration import set_fastpath_enabled

            result = set_fastpath_enabled(self.cfg.openclaw_chat, enabled, persist=True)
            persisted = bool(result.get("persisted"))
        except Exception as exc:
            persisted = False
            log.warning("could not persist fastpath_enabled to MongoDB: %s", exc)
        log.info(
            "chat fastpath %s via dashboard (persisted=%s)",
            "enabled" if enabled else "disabled",
            persisted,
        )
        return {"ok": True, "enabled": enabled, "persisted": persisted}

    def handle_openclaw_token_mode_get(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_prompt_policy import token_mode_status

        return token_mode_status(self.cfg.openclaw_chat)

    def handle_openclaw_token_mode_set(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .token_consumption_ops import set_token_usage_mode

        mode = str(payload.get("mode", payload.get("token_usage_mode")) or "").strip()
        return set_token_usage_mode(self.cfg.openclaw_chat, mode, persist=True)

    def handle_openclaw_tool_limit_get(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .token_consumption_ops import tool_iterations_status

        return tool_iterations_status(self.cfg.openclaw_chat)

    def handle_openclaw_tool_limit_set(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .token_consumption_ops import set_max_tool_iterations

        raw = payload.get(
            "max_tool_iterations",
            payload.get("limit", payload.get("value")),
        )
        return set_max_tool_iterations(self.cfg.openclaw_chat, raw, persist=True)

    def handle_openclaw_tools_cap_get(self) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .token_consumption_ops import tools_cap_status

        return tools_cap_status(self.cfg.openclaw_chat)

    def handle_openclaw_tools_cap_set(self, payload: dict) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .token_consumption_ops import set_max_tools_cap

        raw = payload.get(
            "max_tools_cap",
            payload.get("tools_cap_override", payload.get("limit", payload.get("value"))),
        )
        return set_max_tools_cap(self.cfg.openclaw_chat, raw, persist=True)

    def handle_openclaw_visual_memory_from_photo(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .visual_memory_register import register_from_upload_payload

        body = dict(payload)
        uid = str(body.get("user_id") or "").strip()
        if not uid and user and getattr(user, "user_id", None):
            uid = str(user.user_id or "").strip()
        if uid:
            body["user_id"] = uid
        try:
            return register_from_upload_payload(body)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_openclaw_profile_from_photo(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        """Analyze photo, save visual memory, and sync appearance to all conversations."""
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .profile_from_photo import register_owner_profile_from_upload

        body = dict(payload)
        uid = str(body.get("user_id") or "").strip()
        if not uid and user and getattr(user, "user_id", None):
            uid = str(user.user_id or "").strip()
        if uid:
            body["user_id"] = uid
        body.setdefault("owner", True)
        sync = body.get("sync_conversations", True)
        try:
            return register_owner_profile_from_upload(
                body,
                sync_conversations=bool(sync),
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_openclaw_visual_memory_profiles(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .character_preview import search_characters

        try:
            limit = int(query.get("limit") or 24)
        except (TypeError, ValueError):
            limit = 24
        return search_characters(
            query=str(query.get("query") or query.get("q") or ""),
            kind=str(query.get("kind") or ""),
            limit=limit,
        )

    def handle_openclaw_visual_memory_preview(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .character_preview import get_character_preview

        uid = str(query.get("user_id") or "").strip()
        if not uid and user and getattr(user, "user_id", None):
            uid = str(user.user_id or "").strip()
        return get_character_preview(
            profile_id=str(query.get("profile_id") or ""),
            name=str(query.get("name") or ""),
            user_id=uid,
        )

    def handle_openclaw_generated_images_list(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .generated_images_store import list_generated_images

        try:
            limit = int(query.get("limit") or 60)
        except (TypeError, ValueError):
            limit = 60
        try:
            offset = int(query.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        uid = str(query.get("user_id") or "").strip()
        if not uid and user and getattr(user, "user_id", None):
            uid = str(user.user_id or "").strip()
        include_missing = str(query.get("include_missing") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        return list_generated_images(
            limit=limit,
            offset=offset,
            user_id=uid,
            session_key=str(query.get("session_key") or query.get("key") or "").strip(),
            include_missing=include_missing,
        )

    def handle_openclaw_generated_images_delete(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .generated_images_store import (
            soft_delete_all_generated_images,
            soft_delete_generated_image,
        )

        if payload.get("all") in (True, "true", "1", 1):
            uid = str(payload.get("user_id") or "").strip()
            if not uid and user and getattr(user, "user_id", None):
                uid = str(user.user_id or "").strip()
            return soft_delete_all_generated_images(
                user_id=uid,
                session_key=str(
                    payload.get("session_key") or payload.get("key") or ""
                ).strip(),
            )

        image_id = str(payload.get("id") or payload.get("image_id") or "").strip()
        return soft_delete_generated_image(image_id)

    def handle_openclaw_generated_images_register(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .generated_images_store import register_image_path

        path = str(payload.get("path") or payload.get("image_path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        uid = str(payload.get("user_id") or "").strip()
        if not uid and user and getattr(user, "user_id", None):
            uid = str(user.user_id or "").strip()
        return register_image_path(
            path,
            prompt=str(payload.get("prompt") or "").strip(),
            caption=str(payload.get("caption") or payload.get("title") or "").strip(),
            session_key=str(
                payload.get("session_key") or payload.get("key") or ""
            ).strip(),
            user_id=uid,
            source=str(payload.get("source") or "chat_manual").strip(),
            provider=str(payload.get("provider") or "").strip(),
            model=str(payload.get("model") or "").strip(),
        )

    def handle_openclaw_gallery_image_route(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        from .chat_gallery_image import dispatch_gallery_image_route

        try:
            return dispatch_gallery_image_route(payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_openclaw_minicurriculum_html(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> tuple[Optional[bytes], Optional[str]]:
        if not self.cfg.openclaw_chat.enabled:
            return None, "openclaw chat is disabled"
        from .mini_curriculum import resolve_mini_curriculum_html

        return resolve_mini_curriculum_html(str(query.get("path") or ""))

    def handle_openclaw_attachment_upload(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        cc = self.cfg.openclaw_chat
        runtime = self._openclaw_runtime()
        key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not key:
            key = default_session_key(cc, runtime)
        attachments_dir = resolve_attachments_dir(cc)
        raw = payload.get("attachments")
        if raw is None:
            raw = [payload]
        parsed, err = parse_attachments(
            raw if isinstance(raw, list) else [raw],
            max_count=cc.max_attachments,
            max_bytes=cc.max_attachment_bytes,
            attachments_dir=attachments_dir,
        )
        if err:
            return {"ok": False, "error": err}
        if not parsed:
            return {"ok": False, "error": "nenhum anexo válido"}
        parsed, transcribe_err, transcribed = _normalize_chat_attachments(
            parsed,
            timeout=cc.send_timeout_seconds,
        )
        if transcribe_err:
            return {"ok": False, "error": transcribe_err}
        saved = persist_attachments(
            parsed,
            session_key=key,
            attachments_dir=attachments_dir,
        )
        row = saved[0]
        return {
            "ok": True,
            "session_key": key,
            "name": row.name,
            "mime_type": row.mime_type,
            "path": str(row.path) if row.path else None,
            "size": len(row.data),
            "transcribed": transcribed,
        }

    def handle_openclaw_artifact_download(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> Any:
        """Serve a staged chat artifact (ZIP, etc.) by validated absolute path."""
        from .chat_artifacts import read_artifact_bytes

        path = str(query.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required"}
        try:
            data, mime, fname = read_artifact_bytes(path)
            return data, mime, fname
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc)}

    def handle_openclaw_team_file_download(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> Any:
        """Serve a team file (local_path, workspace, docs…) after path validation."""
        from .team_files_download import serve_team_file_download

        try:
            result = serve_team_file_download(query)
            if isinstance(result, tuple):
                return result
            return result
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_chat_tts(self, payload: dict) -> dict:
        """Synthesize chat reply audio via edge-tts (neural, pt-BR)."""
        cc = self.cfg.openclaw_chat
        if not cc.enabled or not cc.tts_edge_enabled:
            return {"ok": False, "error": "edge TTS disabled"}
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            return {"ok": False, "error": "text is required"}
        from .tts import edge_tts_available, synthesize_edge_tts_base64

        if not edge_tts_available():
            return {
                "ok": False,
                "error": "edge-tts not installed (.venv/bin/pip install edge-tts)",
            }
        voice = str(payload.get("voice") or cc.tts_edge_voice or "").strip()
        if not voice:
            voice = "pt-BR-FranciscaNeural"
        try:
            out = synthesize_edge_tts_base64(
                text,
                voice=voice,
                max_chars=int(cc.tts_max_chars),
            )
            return {"ok": True, **out}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_chat_stt(self, payload: dict) -> dict:
        """Transcribe short voice clips for chat dictation (Whisper)."""
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        raw_b64 = str(payload.get("data_base64") or "").strip()
        if not raw_b64:
            return {"ok": False, "error": "data_base64 is required"}
        try:
            import base64

            audio = base64.b64decode(raw_b64, validate=True)
        except Exception:
            return {"ok": False, "error": "invalid data_base64"}
        if not audio:
            return {"ok": False, "error": "empty audio payload"}
        if len(audio) > 12 * 1024 * 1024:
            return {"ok": False, "error": "audio too large (max 12 MB)"}

        from .chat_models import transcribe_audio_bytes
        from .openclaw_chat import _audio_transcription_model, _audio_transcription_timeout
        from .secrets_fallback import resolve_llm_api_key

        api_key = resolve_llm_api_key("OPENAI_API_KEY")
        if not api_key:
            return {
                "ok": False,
                "error": "OPENAI_API_KEY é obrigatório para transcrever áudio",
            }
        mime = str(payload.get("mime_type") or "audio/webm").strip() or "audio/webm"
        name = str(payload.get("name") or "ditado.webm").strip() or "ditado.webm"
        try:
            text = transcribe_audio_bytes(
                data=audio,
                name=name,
                mime_type=mime,
                api_key=api_key,
                model=_audio_transcription_model(),
                timeout=_audio_transcription_timeout(90.0),
            )
            return {"ok": True, "text": text}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_openclaw_send(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict:
        if not self.cfg.openclaw_chat.enabled:
            return {"ok": False, "error": "openclaw chat is disabled"}
        text = str(payload.get("message") or payload.get("text") or "").strip()
        attachments = payload.get("attachments")
        if attachments is not None and not isinstance(attachments, list):
            return {"ok": False, "error": "attachments must be a list"}
        if not text and not attachments:
            return {"ok": False, "error": "message or attachments required"}
        from .chat_file_mention import check_file_upload_required

        upload_check = check_file_upload_required(
            text,
            attachments if isinstance(attachments, list) else None,
        )
        if upload_check.get("required"):
            return {
                "ok": False,
                "error": str(upload_check.get("message") or "Anexe o arquivo antes de enviar."),
                "upload_required": True,
                "mentioned_files": upload_check.get("files") or [],
            }
        cc = self.cfg.openclaw_chat
        runtime = self._openclaw_runtime()
        key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not key:
            key = default_session_key(cc, runtime)
        from .chat_session_presence import touch_session_presence

        touch_session_presence(key)
        agent_id = payload.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            return {"ok": False, "error": "agent must be a string"}
        model_id = payload.get("model_id") or payload.get("modelId")
        if model_id is not None and not isinstance(model_id, str):
            return {"ok": False, "error": "model_id must be a string"}
        effective_model_id, route = resolve_effective_model_id(
            cc,
            str(model_id).strip() if model_id else None,
            message=text,
            attachments=attachments if isinstance(attachments, list) else None,
        )
        model_id = effective_model_id
        brief_target = payload.get("brief_target") or payload.get("briefTarget")
        if brief_target is not None and not isinstance(brief_target, str):
            return {"ok": False, "error": "brief_target must be a string"}
        image_model = payload.get("image_model") or payload.get("imageModel")
        if image_model is not None and not isinstance(image_model, str):
            return {"ok": False, "error": "image_model must be a string"}
        team_id = payload.get("team_id") or payload.get("teamId")
        if team_id is not None and not isinstance(team_id, str):
            return {"ok": False, "error": "team_id must be a string"}
        # Assign team only when the session has none yet (do not overwrite).
        if team_id and self.chat_persistence is not None:
            tid = str(team_id).strip()
            if tid:
                sess_key = str(
                    payload.get("session_key") or payload.get("key") or ""
                ).strip()
                if sess_key:
                    existing = self.chat_persistence.history.get_session_by_key(
                        sess_key
                    )
                    if existing is None or not existing.team_id:
                        self.chat_persistence.history.assign_team(sess_key, tid)
        profile = parse_hermes_session_key(key)
        send_key = key
        send_agent = agent_id
        if profile:
            send_key = openclaw_key_for_hermes_profile(profile, cc.default_agent)
            send_agent = cc.default_agent

        # "Modo Swarm" (orquestração RuFlo) — toggle persistido por sessão.
        swarm_available = bool(
            getattr(self.cfg, "ruflo_chat", None)
            and self.cfg.ruflo_chat.enabled
        )
        swarm_override = payload.get("swarm")
        if swarm_override is None:
            swarm_override = payload.get("swarm_mode") or payload.get("swarmMode")
        swarm_enabled = False
        if swarm_available:
            if swarm_override is not None and self.chat_persistence is not None:
                try:
                    self.chat_persistence.history.set_session_swarm(
                        send_key, bool(swarm_override)
                    )
                except Exception:
                    log.debug("could not persist swarm flag for %s", send_key)
            if swarm_override is not None:
                swarm_enabled = bool(swarm_override)
            elif self.chat_persistence is not None:
                sess = self.chat_persistence.history.get_session_by_key(send_key)
                swarm_enabled = bool(getattr(sess, "swarm_mode", False)) if sess else False

        # In swarm mode, ALLOW/CANCELAR replies resolve a pending bash command
        # synchronously instead of starting a new model turn.
        if swarm_enabled and text:
            approval = handle_pending_bash_approval(
                text.upper(),
                cfg=self.cfg.ruflo_chat,
                runtime=runtime,
                session_key=send_key,
                persistence=self.chat_persistence,
                backend="qclaw+swarm",
            )
            if approval is not None:
                self._persist_chat_user_turn(
                    send_key, text, agent_id=send_agent, attachments=attachments
                )
                approval["async"] = False
                approval["sessionKey"] = key
                return approval

        existing = find_running_chat_job_for_client_key(key)
        queued_behind = existing is not None and (existing.kind or "chat") == "chat"
        if queued_behind:
            log.info(
                "queue chat send for session %s (existing job %s still running)",
                key,
                existing.id,
            )

        retry_last = bool(
            payload.get("retry")
            or payload.get("retry_last")
            or payload.get("retryLast")
        )
        send_text = enrich_slide_continuation_message(
            text,
            self.chat_persistence,
            key,
            history_key=send_key,
        )
        if not retry_last:
            self._persist_chat_user_turn(
                send_key, text, agent_id=send_agent, attachments=attachments
            )
        elif not text:
            pers = self.chat_persistence
            if pers is not None:
                msgs = pers.history.list_messages(send_key, limit=8)
                if msgs and msgs[-1].get("role") == "user":
                    text = str(msgs[-1].get("text") or "").strip()
                    send_text = enrich_slide_continuation_message(
                        text,
                        self.chat_persistence,
                        key,
                        history_key=send_key,
                    )
        if not send_text and not attachments:
            return {"ok": False, "error": "message or attachments required"}
        job = chat_job_create(
            send_key,
            display_key=key,
            message_text=text,
            agent_id=send_agent,
            profile=profile,
            user_id=self._chat_user_id(user),
            model_id=str(model_id).strip() if model_id else None,
            attachments=attachments if isinstance(attachments, list) else None,
        )
        prime_slide_job_from_message(job.id, send_text)
        prime_slide_job_from_session(
            self.chat_persistence,
            job.id,
            key,
            history_key=send_key,
        )
        resolved, _ = resolve_chat_model(
            cc,
            str(model_id).strip() if model_id else None,
        )
        model_label = resolved.entry.label if resolved else ""
        start_label = (
            f"Auto → {route.label} ({route.reason}) — a iniciar…"
            if route
            else "Pedido recebido — a iniciar…"
        )
        if queued_behind:
            start_label = "Na fila — o pedido anterior ainda está a correr…"
        chat_job_append_progress(
            job.id,
            with_model_prefix(start_label, model_label),
        )
        self._schedule_openclaw_send_job(
            key,
            job.id,
            send_key=send_key,
            display_key=key,
            text=send_text,
            send_agent=send_agent,
            profile=profile,
            user=user,
            model_id=str(model_id).strip() if model_id else None,
            brief_target=str(brief_target).strip() if brief_target else None,
            image_model=str(image_model).strip() if image_model else None,
            attachments=attachments,
            swarm=swarm_enabled,
            team_id=str(team_id).strip() if team_id else None,
        )
        out: dict[str, Any] = {
            "ok": True,
            "async": True,
            "jobId": job.id,
            "sessionKey": key,
            "status": "running",
        }
        if queued_behind:
            out["queued"] = True
        loaded = chat_job_get(job.id)
        if loaded is not None and loaded.block_max > 0:
            status = job_to_dict(loaded)
            out["blockTurn"] = status.get("blockTurn", loaded.block_turn)
            out["blockMax"] = status.get("blockMax", loaded.block_max)
            out["lastProgress"] = status.get("lastProgress") or loaded.last_progress
        if profile:
            out["source"] = "hermes"
            out["agentId"] = profile
        return out

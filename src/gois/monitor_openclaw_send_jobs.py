"""OpenClaw async chat send jobs and tool context."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

from .accounts import UserRecord
from .chat_jobs import (
    append_progress as chat_job_append_progress,
    cancel_job as chat_job_cancel,
    complete_job as chat_job_complete,
    create_job as chat_job_create,
    enrich_slide_continuation_message,
    fail_job as chat_job_fail,
    get_job as chat_job_get,
    is_job_cancelled as chat_job_is_cancelled,
    job_to_dict,
    list_all_recent_jobs as chat_jobs_list_all_recent,
    list_running_chat_jobs as chat_jobs_list_running,
    list_running_jobs as chat_jobs_list_all_running,
    parse_job_attachments,
)
from .chat_models import resolve_chat_model
from .chat_send_queue import queue_snapshot as _queue_snapshot
from .openclaw_chat import QclawChatToolContext, send_chat_message
from .openclaw_chat_sessions import _new_session_title
from .tool_progress import active_tool_runs_payload

log = logging.getLogger(__name__)


def _normalize_buffer_snapshot(
    buffer: dict[str, list[dict[str, Any]]],
    jobs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map internal queue keys to UI session keys (display_key)."""
    send_to_client: dict[str, str] = {}
    for row in jobs:
        client = str(row.get("sessionKey") or "").strip()
        send = str(row.get("sendSessionKey") or "").strip()
        if client and send:
            send_to_client[send] = client
    normalized: dict[str, list[dict[str, Any]]] = {}
    for qkey, items in buffer.items():
        for item in items:
            client = str(item.get("sessionKey") or "").strip()
            if not client:
                client = send_to_client.get(qkey, qkey)
            normalized.setdefault(client, []).append(item)
    return normalized


class MonitorOpenclawSendJobsMixin:
    def _mirror_assistant_reply_to_whatsapp(
        self, session_key: str, text: str
    ) -> None:
        if not self._is_whatsapp_session_key(session_key):
            return
        reply = (text or "").strip()
        if not reply:
            return
        if not self.cfg.whatsapp_digest.recipient:
            return
        from .whatsapp_send_policy import CONTEXT_WHATSAPP_INBOUND, whatsapp_outbound_scope

        with whatsapp_outbound_scope(CONTEXT_WHATSAPP_INBOUND):
            out = self.handle_whatsapp_send(reply, wait=False)
        if not out.get("ok"):
            log.warning("whatsapp mirror outbound failed: %s", out)
    def _openclaw_chat_tool_context(
        self, user: Optional[UserRecord] = None
    ) -> Optional[QclawChatToolContext]:
        cc = self.cfg.openclaw_chat
        if not cc.qclaw_tools_enabled:
            return None
        return QclawChatToolContext(
            recovery=self.recovery,
            doctor_cfg=self.cfg.openclaw_doctor,
            status_snapshot=self.status_snapshot,
            whatsapp_recipient=self.cfg.whatsapp_digest.recipient,
            hermes_cron_snapshot=(
                self._cached_hermes_cron_snapshot if self.cfg.hermes else None
            ),
            hermes_cron_job_result=(
                (lambda jid: self.handle_hermes_cron_result(jid))
                if self.cfg.hermes
                else None
            ),
            jobs_cancel=self.handle_swarm_execution_cancel,
            jobs_cron_action=(
                self.handle_hermes_cron_action if self.cfg.hermes else None
            ),
            jobs_cron_create=(
                self.handle_hermes_cron_create if self.cfg.hermes else None
            ),
            jobs_cron_edit=(
                (lambda jid, p: self.handle_hermes_cron_edit(jid, p))
                if self.cfg.hermes
                else None
            ),
            wacli_auth_qr=self.handle_wacli_auth_qr,
            wacli_unlock=self.handle_wacli_unlock,
            whatsapp_status=(
                self.handle_whatsapp_status
                if self.cfg.whatsapp_digest.recipient
                else None
            ),
            whatsapp_send=(
                (lambda msg, wait=False: self.handle_whatsapp_send(msg, wait))
                if self.cfg.whatsapp_digest.recipient
                else None
            ),
            whatsapp_send_to=(
                (lambda to, msg, wait=False: self.handle_whatsapp_send_to(to, msg, wait))
                if self.cfg.whatsapp_digest.recipient
                else None
            ),
            hermes_agent_create=(
                (lambda p: self.handle_hermes_agent_create(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            openai_swarm_create=(
                (lambda p: self.handle_openai_swarm_create(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_create_card=(
                (lambda p: self.handle_chat_kanban_create_card(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_attachment_upload=(
                (lambda p: self.handle_hermes_kanban_attachment_upload(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_attachment_move=(
                (lambda p: self.handle_hermes_kanban_attachment_move(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_attachment_copy=(
                (lambda p: self.handle_hermes_kanban_attachment_copy(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            team_kanban_get=(lambda tid: self.handle_chat_team_kanban(tid, user)),
            team_swarm_run=(
                (lambda tid, p: self.handle_team_swarm_run(tid, p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_run=(
                (lambda p: self.handle_swarm_graph_run(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_graph_preview=(
                (lambda p: self.handle_swarm_graph_preview(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_requirements=(
                (lambda p: self.handle_hermes_kanban_requirements(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            kanban_ide_handoff=(
                (lambda p: self.handle_kanban_ide_handoff(p, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            team_create=(lambda p: self.handle_team_create(p, user)),
            team_list=(lambda: self.handle_chat_list_teams(user)),
            team_whatsapp_numbers_get=(
                lambda tid: self.handle_team_whatsapp_numbers_get(tid, user)
            ),
            team_whatsapp_numbers_set=(
                lambda tid, p: self.handle_team_whatsapp_numbers_set(tid, p, user)
            ),
            team_whatsapp_number_add=(
                lambda tid, p: self.handle_team_whatsapp_number_add(tid, p, user)
            ),
            team_whatsapp_number_remove=(
                lambda tid, p: self.handle_team_whatsapp_number_remove(tid, p, user)
            ),
            team_whatsapp_send=(
                lambda tid, p: self.handle_team_whatsapp_send(tid, p, user)
            ),
            team_whatsapp_broadcast=(
                lambda tid, p: self.handle_team_whatsapp_broadcast(tid, p, user)
            ),
            team_whatsapp_send_to_group=(
                lambda tid, p: self.handle_team_whatsapp_send_to_group(tid, p, user)
            ),
            team_notify_emails_get=(
                lambda tid: self.handle_team_notify_emails_get(tid, user)
            ),
            team_notify_emails_set=(
                lambda tid, p: self.handle_team_notify_emails_set(tid, p, user)
            ),
            team_contacts_get=(
                lambda tid: self.handle_team_contacts_get(tid, user)
            ),
            team_contacts_upsert=(
                lambda tid, p: self.handle_team_contacts_upsert(tid, p, user)
            ),
            team_contacts_remove=(
                lambda tid, p: self.handle_team_contacts_remove(tid, p, user)
            ),
            allowlist_list=self.handle_whatsapp_allowlist_list,
            allowlist_add=self.handle_whatsapp_allowlist_add,
            allowlist_remove=self.handle_whatsapp_allowlist_remove,
            allowlist_toggle=self.handle_whatsapp_allowlist_toggle,
            model_quotas_get=self.handle_model_quotas_get,
            swarm_model=(
                (lambda swarm_name, payload: self.handle_swarm_model(swarm_name, payload, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robot_update=(
                (lambda slug, payload: self.handle_swarm_robot_update(slug, payload, user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            swarm_robots_snapshot=(
                (lambda: self.handle_swarm_robots_snapshot(user))
                if self.cfg.hermes_agent_create.enabled
                else None
            ),
            hermes_agent_create_cfg=self.cfg.hermes_agent_create,
        )

    def _user_from_job_id(self, user_id: Optional[str]) -> Optional[UserRecord]:
        if not user_id or not self.cfg.auth.enabled:
            return None
        row = (self.accounts._load().get("users") or {}).get(user_id)
        if isinstance(row, dict):
            return self.accounts._user_from_row(row)
        return None

    def _chat_job_already_answered(self, send_key: str) -> bool:
        pers = self.chat_persistence
        if pers is None:
            return False
        msgs = pers.history.list_messages(send_key, limit=30)
        if not msgs:
            return False
        last = msgs[-1]
        return last.get("role") == "assistant"

    def _bootstrap_skills_mcp(self) -> None:
        from .gois_lite import is_gois_lite

        if is_gois_lite():
            return
        from .skills_mcp_bootstrap import bootstrap_all

        runtime = self._openclaw_runtime()
        config_path = runtime.config_path if runtime.config_path.is_file() else None
        result = bootstrap_all(config_path=config_path)
        if result.get("openclaw_mcp_changed"):
            log.info("registered qclaw-skills MCP in %s", config_path)
        hermes = result.get("hermes_profiles") or {}
        updated = len(hermes.get("updated_soul") or []) + len(
            hermes.get("updated_tools") or []
        )
        if updated:
            log.info(
                "skills mcp bootstrap: %d Hermes profile(s) updated",
                updated,
            )

    def _recover_stale_chat_jobs_on_startup(self) -> None:
        from .chat_jobs import (
            recover_jobs_for_missing_sessions,
            recover_stale_batch_jobs,
            recover_stale_chat_jobs,
        )

        orphan_ids = recover_jobs_for_missing_sessions(self.chat_persistence)
        if orphan_ids:
            log.info(
                "cancelled %d chat job(s) for deleted conversation(s): %s",
                len(orphan_ids),
                ", ".join(orphan_ids[:8]),
            )

        stale = recover_stale_chat_jobs()
        stale.extend(recover_stale_batch_jobs())
        if not stale:
            return
        pers = self.chat_persistence
        for job in stale:
            key = (job.display_key or job.session_key or "").strip()
            if pers is None or not key:
                continue
            try:
                kind = (job.kind or "chat").strip() or "chat"
                err = (job.error or "").strip()
                if kind == "image_batch" and err:
                    text = f"⚠️ Geração de imagens interrompida: {err[:500]}"
                else:
                    text = (
                        "⏱️ Pedido anterior expirou — o chat foi libertado. "
                        "Pode continuar ou reenviar a mensagem."
                    )
                pers.history.append_message(
                    key,
                    role="status",
                    text=text,
                )
            except Exception as exc:
                log.warning("could not persist stale-job notice for %s: %s", key, exc)
        log.info("recovered %d stale chat job(s) on startup", len(stale))

    def _recover_stale_chat_jobs(self) -> None:
        """Fail expired async chat jobs (startup + periodic)."""
        self._recover_stale_chat_jobs_on_startup()
        self._cancel_unattended_chat_jobs()

    def _cancel_unattended_chat_jobs(self) -> None:
        cc = self.cfg.openclaw_chat
        if not cc.inactive_job_cancel_enabled:
            return
        from .chat_jobs import cancel_unattended_chat_jobs

        protected = frozenset(
            k.strip()
            for k in (
                cc.default_session_key,
                self._whatsapp_inbound_session_key(),
            )
            if k and str(k).strip()
        )
        cancelled = cancel_unattended_chat_jobs(
            persistence=self.chat_persistence,
            presence_ttl_seconds=cc.presence_ttl_seconds,
            grace_seconds=cc.inactive_job_grace_seconds,
            protected_keys=protected,
        )
        if not cancelled:
            return
        pers = self.chat_persistence
        for job in cancelled:
            key = (job.display_key or job.session_key or "").strip()
            if pers is None or not key:
                continue
            err = (job.error or "").strip()
            if not err:
                continue
            try:
                pers.history.append_message(key, role="status", text=f"⏹️ {err}")
            except Exception as exc:
                log.warning("could not persist unattended-job notice for %s: %s", key, exc)
        log.info("cancelled %d unattended chat job(s)", len(cancelled))

    def handle_openclaw_session_presence(self, payload: dict) -> dict:
        """Heartbeat from /chat — which conversations have an active browser tab."""
        from .chat_session_presence import touch_many_session_presence

        raw = payload.get("active_keys") or payload.get("activeKeys") or []
        if not isinstance(raw, list):
            return {"ok": False, "error": "active_keys must be a list"}
        keys = [str(k).strip() for k in raw if str(k).strip()]
        touch_many_session_presence(keys)
        return {"ok": True, "count": len(keys)}

    async def _stale_chat_jobs_loop(self) -> None:
        await asyncio.sleep(120.0)
        while True:
            try:
                self._recover_stale_chat_jobs()
            except Exception as exc:
                log.warning("stale chat job recovery loop failed: %s", exc)
            await asyncio.sleep(120.0)

    def _resume_interrupted_chat_jobs(self) -> None:
        if not self.cfg.openclaw_chat.enabled or not self.cfg.openclaw_chat.async_send:
            return
        self._recover_stale_chat_jobs_on_startup()
        from .chat_tool_background import chat_session_exists

        for job in chat_jobs_list_all_running():
            if (job.kind or "chat") != "chat":
                continue
            client_key = (job.display_key or job.session_key or "").strip()
            if client_key and not chat_session_exists(self.chat_persistence, client_key):
                chat_job_cancel(job.id)
                log.info(
                    "skipped resume for job=%s — conversation %s removed",
                    job.id,
                    client_key,
                )
                continue
            if self._chat_job_already_answered(job.session_key):
                chat_job_complete(
                    job.id,
                    {
                        "ok": True,
                        "reply": "",
                        "sessionKey": job.display_key or job.session_key,
                        "recovered": True,
                    },
                )
                continue
            chat_job_append_progress(
                job.id, "Retomando após reinício do monitor…"
            )
            resume_text = enrich_slide_continuation_message(
                job.message_text,
                self.chat_persistence,
                job.display_key or job.session_key,
                history_key=job.session_key,
            )
            self._schedule_openclaw_send_job(
                job.display_key or job.session_key,
                job.id,
                send_key=job.session_key,
                display_key=job.display_key or job.session_key,
                text=resume_text,
                send_agent=job.agent_id,
                profile=job.profile,
                user=self._user_from_job_id(job.user_id),
                model_id=job.model_id,
                attachments=parse_job_attachments(job),
            )
            log.info(
                "resumed chat send job id=%s session=%s",
                job.id,
                job.session_key,
            )

    def _persist_chat_user_turn(
        self,
        send_key: str,
        text: str,
        *,
        agent_id: Optional[str],
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        pers = self.chat_persistence
        if pers is None:
            return
        cc = self.cfg.openclaw_chat
        aid = (agent_id or cc.default_agent or "main").strip() or "main"
        if pers.history.get_session_by_key(send_key) is None:
            pers.history.create_session(
                agent_id=aid,
                title=_new_session_title(),
                session_key=send_key,
                source="dashboard",
                user_id=None,
            )
        from .openclaw_chat_media_tools import attachments_meta_for_persistence

        extras = None
        meta = attachments_meta_for_persistence(attachments or [])
        if meta:
            extras = {"attachments": meta}
        pers.history.append_message(
            send_key,
            role="user",
            text=text,
            extras=extras,
        )

    def _apply_hermes_chat_result(
        self, result: dict, profile: Optional[str], display_key: str
    ) -> dict:
        if profile and result.get("ok"):
            result = dict(result)
            result["sessionKey"] = display_key
            result["source"] = "hermes"
            result["agentId"] = profile
        return result

    def _enrich_chat_job_status(self, out: dict[str, Any], job) -> None:
        resolved, _ = resolve_chat_model(
            self.cfg.openclaw_chat,
            job.model_id,
        )
        if resolved is None:
            return
        out["modelId"] = resolved.entry.id
        out["modelLabel"] = resolved.entry.label

    def _session_title_for_job(self, job) -> str:
        key = (job.display_key or job.session_key or "").strip()
        if not key:
            return "?"
        pers = self.chat_persistence
        if pers is not None:
            try:
                sess = pers.history.get_session_by_key(key)
                title = getattr(sess, "title", None) if sess is not None else None
                if title:
                    return str(title).strip() or key
            except Exception:
                pass
        return key if len(key) <= 56 else key[:53] + "…"

    def _chat_job_status_dict(self, job, *, light: bool = False) -> dict[str, Any]:
        out = job_to_dict(job, light=light)
        self._enrich_chat_job_status(out, job)
        out["elapsedSeconds"] = max(0, int(time.time() - job.started_at))
        if not light:
            out["sessionTitle"] = self._session_title_for_job(job)
        return out

    def handle_openclaw_send_status(self, query: dict) -> dict:
        job_id = str(query.get("job_id") or query.get("jobId") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        job = chat_job_get(job_id)
        if job is None:
            return {"ok": False, "error": "job not found"}
        no_tool_jobs = str(query.get("no_tool_jobs") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        out = self._chat_job_status_dict(job, light=True)
        if not no_tool_jobs:
            out["toolJobs"] = active_tool_runs_payload().get("jobs") or []
        return out

    def handle_openclaw_send_cancel(self, payload: dict) -> dict:
        """Cancel a running chat send job."""
        job_id = str(payload.get("job_id") or payload.get("jobId") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        cancelled = chat_job_cancel(job_id)
        if not cancelled:
            job = chat_job_get(job_id)
            if job is None:
                return {"ok": False, "error": "job not found"}
            return {"ok": False, "error": f"job is not running (status: {job.status})"}
        return {"ok": True, "jobId": job_id, "status": "cancelled"}

    def handle_swarm_activity_changes(
        self,
        query: dict[str, Any],
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        """Return generated/changed files for one activity-board agent run."""
        from .swarm_robots import handle_swarm_activity_changes

        return handle_swarm_activity_changes(self, query, user)

    def handle_swarm_agent_history(
        self,
        slug: str,
        user: Optional[UserRecord] = None,
        *,
        query: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Return recent execution history for one swarm robot / Hermes profile."""
        from .swarm_robots import handle_swarm_agent_history

        return handle_swarm_agent_history(self, slug, user, query=query)

    def handle_swarm_execution_cancel(self, payload: dict) -> dict:
        """Cancel a running swarm/card execution (chat, kanban, fila ou teste de swarm)."""
        job_id = str(payload.get("job_id") or payload.get("jobId") or "").strip()
        task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
        source = str(payload.get("source") or payload.get("kind") or "").strip().lower()
        if source in {"kanban", "kanban_card"}:
            source = "kanban_schedule"

        if not job_id and task_id:
            for job in kanban_schedule_list_running():
                if str(job.task_id or "").strip() == task_id:
                    job_id = str(job.id or "").strip()
                    source = source or "kanban_schedule"
                    break
            if not job_id:
                handler = getattr(self, "_priority_queue_handler", None)
                if handler is not None:
                    try:
                        queue = handler.engine.get_queue()
                        for row in (queue.get("running") or []) if isinstance(queue, dict) else []:
                            if not isinstance(row, dict):
                                continue
                            if str(row.get("task_id") or "").strip() == task_id:
                                job_id = str(row.get("id") or "").strip()
                                source = source or "priority_queue"
                                break
                    except Exception:
                        pass

        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        if source == "kanban":
            source = "kanban_schedule"

        if source in {"", "swarm_test"} or job_id.startswith("swarm-graph:"):
            from .swarm_graph import request_swarm_cancel

            swarm_name = str(payload.get("swarm_name") or "").strip()
            if not swarm_name and job_id.startswith("swarm-graph:"):
                swarm_name = job_id.split(":", 1)[-1].strip()
            if swarm_name and request_swarm_cancel(swarm_name):
                self._invalidate_swarm_robots_cache()
                return {
                    "ok": True,
                    "jobId": job_id,
                    "source": "swarm_test",
                    "status": "cancelled",
                }

        if source in {"", "priority_queue"}:
            handler = getattr(self, "_priority_queue_handler", None)
            if handler is not None:
                try:
                    handler.engine.cancel_running(job_id)
                    self._invalidate_swarm_robots_cache()
                    return {
                        "ok": True,
                        "jobId": job_id,
                        "source": "priority_queue",
                        "status": "cancelled",
                    }
                except ValueError as exc:
                    if source == "priority_queue":
                        return {"ok": False, "error": str(exc)}

        if source in {"", "chat"}:
            if chat_job_cancel(job_id):
                self._invalidate_swarm_robots_cache()
                return {"ok": True, "jobId": job_id, "source": "chat", "status": "cancelled"}
            chat_job = chat_job_get(job_id)
            if chat_job is not None:
                return {
                    "ok": False,
                    "error": f"job is not running (status: {chat_job.status})",
                }

        if source in {"", "kanban_schedule"}:
            if kanban_schedule_cancel(job_id):
                self._invalidate_swarm_robots_cache()
                return {
                    "ok": True,
                    "jobId": job_id,
                    "source": "kanban_schedule",
                    "status": "cancelled",
                }
            kanban_job = kanban_schedule_get(job_id)
            if kanban_job is not None:
                return {
                    "ok": False,
                    "error": f"job is not running (status: {kanban_job.status})",
                }

        if source in {"", "roteiro_viral"}:
            from .jobs_manage_ops import cancel_rv_job

            rv_result = cancel_rv_job(job_id)
            if rv_result.get("ok"):
                self._invalidate_swarm_robots_cache()
                return rv_result

        if source == "tool":
            if chat_job_cancel(job_id):
                self._invalidate_swarm_robots_cache()
                return {"ok": True, "jobId": job_id, "source": "tool", "status": "cancelled"}

        return {"ok": False, "error": "job not found"}

    def handle_openclaw_send_pending(self) -> dict:
        """Running async chat sends (survives monitor restart when persisted)."""
        jobs = chat_jobs_list_running()
        return {
            "ok": True,
            "jobs": [self._chat_job_status_dict(j) for j in jobs],
        }

    def handle_openclaw_send_queue(self) -> dict:
        """All recent chat jobs (running + done + error) for queue panel."""
        jobs = chat_jobs_list_all_recent(limit=50)
        rows = [self._chat_job_status_dict(j) for j in jobs]

        # Group jobs by session key for thread-level display
        thread_queues: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            sk = row.get("sessionKey") or row.get("sendSessionKey") or "?"
            thread_queues.setdefault(sk, []).append(row)

        # Include live buffer snapshot (jobs still in the internal deque)
        buffer_snapshot = _normalize_buffer_snapshot(_queue_snapshot(), rows)

        return {
            "ok": True,
            "jobs": rows,
            "threadQueues": thread_queues,
            "bufferSnapshot": buffer_snapshot,
        }

    def handle_openclaw_tool_jobs(self, query: dict) -> dict:
        light = str(query.get("light") or query.get("no_media_hydrate") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        payload = active_tool_runs_payload()
        rows: list[dict[str, Any]] = []
        for j in chat_jobs_list_all_running():
            row: dict[str, Any] = {
                "jobId": j.id,
                "sessionKey": j.display_key or j.session_key,
                "sendSessionKey": j.session_key,
                "kind": j.kind or "chat",
                "status": j.status,
                "toolTurn": j.tool_turn,
                "toolMax": j.tool_max,
                "blockTurn": j.block_turn,
                "blockMax": j.block_max,
                "lastProgress": j.last_progress,
            }
            if (j.kind or "chat") in ("image_batch", "pdf_preview", "book_pipeline"):
                from .chat_jobs import should_defer_image_batch_chat_preview
                from .openclaw_chat_media_tools import (
                    hydrate_media_payload_from_paths,
                    hydrate_media_payload_light,
                )

                hydrate = (
                    hydrate_media_payload_light if light else hydrate_media_payload_from_paths
                )
                if should_defer_image_batch_chat_preview(j):
                    row["deferChatPreview"] = True
                media_src = j.partial_media
                if not media_src and (j.kind or "") == "image_batch" and isinstance(j.result, dict):
                    result_images = j.result.get("images") or []
                    if result_images:
                        media_src = {"images": list(result_images), "videos": []}
                if media_src:
                    row["media"] = hydrate(media_src)
            self._enrich_chat_job_status(row, j)
            rows.append(row)
        payload["chatJobs"] = rows
        payload["imageBatchCount"] = sum(
            1
            for r in rows
            if (r.get("kind") or "chat") in ("image_batch", "pdf_preview", "book_pipeline")
        )
        return payload

    def handle_openclaw_jobs_running(self) -> dict:
        """All in-flight jobs (chat, kanban, tools, cron) for the chat jobs dock."""
        from .jobs_manage_ops import list_running_jobs_report

        return list_running_jobs_report()

    def handle_openclaw_books_library(self, query: dict | None = None) -> dict:
        """Registered RV books for the chat books library dock."""
        from .chat_book_editor import books_library_report

        q = query or {}
        try:
            limit = int(q.get("limit") or 80)
        except (TypeError, ValueError):
            limit = 80
        return books_library_report(
            query=str(q.get("query") or q.get("q") or ""),
            status=str(q.get("status") or q.get("book_status") or ""),
            limit=limit,
        )

    def handle_openclaw_articles_library(self, query: dict | None = None) -> dict:
        """LaTeX articles from all teams for the chat articles library dock."""
        from .chat_articles_library import articles_library_report

        q = query or {}
        try:
            limit = int(q.get("limit") or 120)
        except (TypeError, ValueError):
            limit = 120
        return articles_library_report(
            query=str(q.get("query") or q.get("q") or ""),
            team_id=str(q.get("team_id") or q.get("team") or ""),
            status=str(q.get("status") or q.get("filter") or ""),
            limit=limit,
        )

    def handle_openclaw_article_delete(
        self,
        workspace_id: str,
        article_id: str,
        *,
        title_fallback: str = "",
    ) -> dict:
        """Delete a LaTeX article from the chat articles library dock."""
        from .chat_articles_library import article_delete_report

        return article_delete_report(
            workspace_id,
            article_id,
            title_fallback=title_fallback,
        )

    def handle_openclaw_book_structure(self, query: dict | None = None) -> dict:
        """Capítulos, secções e subsecções para o dock móvel de estrutura."""
        from .chat_book_editor import book_structure_report

        q = query or {}
        bid = str(q.get("book_id") or q.get("id") or "").strip()
        if not bid:
            return {"ok": False, "error": "book_id is required"}
        try:
            return book_structure_report(bid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openclaw_book_cover(self, book_id: str) -> dict:
        """Cover image bytes for the chat books library dock."""
        from .chat_book_editor import book_cover_payload

        return book_cover_payload(book_id)

    def handle_openclaw_book_epub(self, book_id: str) -> dict:
        """EPUB file bytes for the chat books library dock."""
        from .chat_book_editor import book_epub_payload

        return book_epub_payload(book_id)

    def handle_openclaw_book_delete(self, book_id: str, *, title_fallback: str = "") -> dict:
        """Delete a book from the chat books library dock."""
        from .chat_book_editor import book_delete_report

        return book_delete_report(book_id, title_fallback=title_fallback)

    def handle_openclaw_courses_library(self, query: dict | None = None) -> dict:
        """Registered RV courses for the chat courses library dock."""
        from .chat_course_manager import courses_library_report

        q = query or {}
        try:
            limit = int(q.get("limit") or 80)
        except (TypeError, ValueError):
            limit = 80
        return courses_library_report(
            query=str(q.get("query") or q.get("q") or ""),
            status=str(q.get("status") or q.get("course_status") or ""),
            limit=limit,
        )

    def handle_openclaw_course_structure(self, query: dict | None = None) -> dict:
        """Módulos e aulas para o dock móvel de estrutura do curso."""
        from .chat_course_manager import course_structure_report

        q = query or {}
        cid = str(q.get("course_id") or q.get("id") or "").strip()
        if not cid:
            return {"ok": False, "error": "course_id is required"}
        try:
            return course_structure_report(cid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openclaw_course_html_site(self, course_id: str) -> dict:
        """ZIP do site HTML completo do curso (dock móvel de estrutura)."""
        from .chat_course_manager import course_html_site_export_payload

        return course_html_site_export_payload(course_id)

    def handle_openclaw_course_delete(self, course_id: str, *, title_fallback: str = "") -> dict:
        """Delete a course from the chat courses library dock."""
        from .chat_course_manager import course_delete_report

        return course_delete_report(course_id, title_fallback=title_fallback)

    def handle_openclaw_sagas_library(self, query: dict | None = None) -> dict:
        """Registered comic sagas for the chat sagas library dock."""
        from .chat_comic_editor import sagas_library_report

        q = query or {}
        try:
            limit = int(q.get("limit") or 80)
        except (TypeError, ValueError):
            limit = 80
        return sagas_library_report(
            query=str(q.get("query") or q.get("q") or ""),
            limit=limit,
            universe_id=str(q.get("universe_id") or ""),
        )

    def handle_openclaw_saga_structure(self, query: dict | None = None) -> dict:
        """Histórias, páginas e painéis para o dock móvel de estrutura da saga."""
        from .chat_comic_editor import saga_structure_report

        q = query or {}
        sid = str(q.get("saga_id") or q.get("id") or "").strip()
        if not sid:
            return {"ok": False, "error": "saga_id is required"}
        try:
            return saga_structure_report(sid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openclaw_saga_delete(self, saga_id: str, *, title_fallback: str = "") -> dict:
        """Delete a saga from the chat sagas library dock."""
        from .chat_comic_editor import saga_delete_report

        return saga_delete_report(saga_id, title_fallback=title_fallback)

    def handle_openclaw_story_delete(
        self,
        story_id: str,
        *,
        saga_id: str = "",
        title_fallback: str = "",
    ) -> dict:
        """Delete a comic story from a saga."""
        from .chat_comic_editor import story_delete_report

        return story_delete_report(story_id, saga_id=saga_id, title_fallback=title_fallback)

    def handle_openclaw_page_delete(
        self,
        page_id: str,
        *,
        story_id: str = "",
        saga_id: str = "",
        page_number: int = 0,
    ) -> dict:
        """Delete a comic page from a story."""
        from .chat_comic_editor import page_delete_report

        return page_delete_report(
            page_id,
            story_id=story_id,
            saga_id=saga_id,
            page_number=page_number,
        )

    def handle_openclaw_magazine_delete(
        self,
        saga_id: str,
        *,
        magazine_index: int = -1,
        magazine_id: str = "",
        job_id: str = "",
    ) -> dict:
        """Delete a magazine/revista issue from a saga plan."""
        from .chat_comic_editor import magazine_delete_report

        return magazine_delete_report(
            saga_id,
            magazine_index=magazine_index,
            magazine_id=magazine_id,
            job_id=job_id,
        )

    def handle_openclaw_scripts_library(self, query: dict | None = None) -> dict:
        """Roteiros gerados para o dock móvel de biblioteca no chat."""
        from .chat_script_acts_editor import scripts_library_report

        q = query or {}
        try:
            limit = int(q.get("limit") or 80)
        except (TypeError, ValueError):
            limit = 80
        return scripts_library_report(
            query=str(q.get("query") or q.get("q") or ""),
            status=str(q.get("status") or q.get("script_status") or ""),
            limit=limit,
        )

    def handle_openclaw_user_profile_dock(self, query: dict | None = None) -> dict:
        """Perfil pessoal + cofre de documentos para o dock móvel do chat."""
        from .chat_user_profile_dock import user_profile_dock_report

        q = query or {}
        return user_profile_dock_report(user_id=str(q.get("user_id") or ""))

    def handle_openclaw_user_profile_dock_generate(self, payload: dict | None = None) -> dict:
        """Gera retrato Imagen a partir da aparência cadastrada no perfil pessoal."""
        from .chat_user_profile_dock import user_profile_dock_generate_portrait

        body = payload or {}
        return user_profile_dock_generate_portrait(
            user_id=str(body.get("user_id") or ""),
            prompt=str(body.get("prompt") or ""),
            style=str(body.get("style") or body.get("visual_style") or ""),
            aspect_ratio=str(body.get("aspect_ratio") or "1:1"),
            api_key=str(body.get("api_key") or "") or None,
        )

    def handle_openclaw_user_profile_dock_remove_photo(self, payload: dict | None = None) -> dict:
        """Remove foto de referência do perfil visual do utilizador."""
        from .chat_user_profile_dock import user_profile_dock_remove_photo

        body = payload or {}
        return user_profile_dock_remove_photo(
            user_id=str(body.get("user_id") or ""),
            photo_id=str(body.get("photo_id") or ""),
        )

    def handle_openclaw_script_delete(self, job_id: str, *, title_fallback: str = "") -> dict:
        """Delete a script from the chat scripts library dock."""
        from .chat_script_acts_editor import script_delete_report

        return script_delete_report(job_id, title_fallback=title_fallback)

    def handle_openclaw_script_act_delete(self, job_id: str, act_number: int) -> dict:
        """Remove an act from a script (structure dock)."""
        from .chat_script_acts_editor import script_act_delete_report

        return script_act_delete_report(job_id, act_number)

    def handle_openclaw_script_structure(self, query: dict | None = None) -> dict:
        """Atos de um roteiro para o dock móvel de estrutura no chat."""
        from .chat_script_acts_editor import script_structure_report

        q = query or {}
        jid = str(q.get("job_id") or q.get("id") or "").strip()
        if not jid:
            return {"ok": False, "error": "job_id is required"}
        try:
            return script_structure_report(jid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openclaw_script_act_image(self, payload: dict | None = None) -> dict:
        """Gera imagem Imagen para um ato (dock móvel de roteiros)."""
        from .chat_script_acts_editor import script_act_generate_image

        body = payload or {}
        jid = str(body.get("job_id") or body.get("id") or "").strip()
        try:
            act_num = int(body.get("act_number") or body.get("act") or 0)
        except (TypeError, ValueError):
            act_num = 0
        if not jid:
            return {"ok": False, "error": "job_id is required"}
        try:
            return script_act_generate_image(
                jid,
                act_num,
                prompt=str(body.get("prompt") or ""),
                style=str(body.get("style") or body.get("visual_style") or ""),
                aspect_ratio=str(body.get("aspect_ratio") or "16:9"),
                api_key=str(body.get("api_key") or "") or None,
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def handle_openclaw_jobs_cancel(self, payload: dict) -> dict:
        """Cancel any running job from the chat jobs dock."""
        from .jobs_manage_ops import cancel_running_job

        job_id = str(payload.get("job_id") or payload.get("jobId") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}
        return cancel_running_job(
            job_id,
            source=str(payload.get("source") or "").strip(),
            swarm_name=str(payload.get("swarm_name") or "").strip(),
            hooks={"cancel": self.handle_swarm_execution_cancel},
        )

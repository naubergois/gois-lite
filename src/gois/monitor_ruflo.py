"""RuFlo chat HTTP handlers for :class:`gois.monitor.GoisMonitor`."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

from .accounts import UserRecord
from .chat_jobs import (
    append_progress as chat_job_append_progress,
    complete_job as chat_job_complete,
    create_job as chat_job_create,
    fail_job as chat_job_fail,
)
from .knowledge_base import aggregate_knowledge, format_projects_knowledge_block
from .ruflo_chat import (
    create_ruflo_session,
    delete_ruflo_session,
    gather_orchestration_context,
    list_ruflo_sessions,
    read_ruflo_messages,
    ruflo_status,
    send_ruflo_message,
)
from .ruflo_motores import (
    motores_rebuild,
    motores_start,
    motores_status,
    motores_stop,
)

log = logging.getLogger(__name__)


class MonitorRufloMixin:
    """RuFlo session/message endpoints and async send jobs."""

    def init_ruflo_cache(self) -> None:
        self._ruflo_status_cache_expires_at = 0.0
        self._ruflo_status_cache_snapshot: Optional[dict[str, Any]] = None
        self._ruflo_health: dict[str, Any] = {
            "consecutive_failures": 0,
            "swarm_ok": None,
            "last_latency_ms": 0.0,
            "last_error": None,
            "threshold_latency_ms": 12000.0,
        }

    def ruflo_health_snapshot(self) -> dict[str, Any]:
        return dict(self._ruflo_health)

    def _record_ruflo_status_observation(
        self,
        snap: dict[str, Any],
        *,
        elapsed_s: float,
        cached: bool,
    ) -> None:
        metrics = getattr(self, "metrics", None)
        if metrics is not None:
            metrics.ruflo_status_duration_seconds.observe(elapsed_s)
        if cached:
            return
        elapsed_ms = elapsed_s * 1000.0
        threshold = float(
            getattr(self.cfg.ruflo_chat, "status_alert_latency_ms", 12000.0) or 12000.0
        )
        swarm_ok = bool(snap.get("swarm_ok"))
        healthy = swarm_ok and elapsed_ms < threshold
        if healthy:
            self._ruflo_health["consecutive_failures"] = 0
        else:
            self._ruflo_health["consecutive_failures"] = int(
                self._ruflo_health.get("consecutive_failures") or 0
            ) + 1
            if metrics is not None:
                metrics.ruflo_status_failures_total.inc()
        self._ruflo_health.update(
            {
                "swarm_ok": swarm_ok,
                "last_latency_ms": round(elapsed_ms, 1),
                "ruflo_latency_ms": round(elapsed_ms, 1),
                "last_error": snap.get("swarm_error"),
                "threshold_latency_ms": threshold,
                "cached": False,
            }
        )

    def _invalidate_ruflo_status_cache(self) -> None:
        self._ruflo_status_cache_expires_at = 0.0

    def _ruflo_chat_ready(self) -> Optional[str]:
        if not self.cfg.ruflo_chat.enabled:
            return "ruflo chat is disabled"
        if not self.cfg.openclaw_chat.enabled:
            return "openclaw chat (LLM) is disabled"
        return None

    def handle_ruflo_motores_status(self) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        return motores_status(
            self.cfg.ruflo_chat,
            self._openclaw_runtime(),
            memory_cfg=self.cfg.swarm_memory,
            engine_cfg=self.cfg.swarm_ruflo_engine,
        )

    def handle_ruflo_motores_start(self) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        out = motores_start(
            self.cfg.ruflo_chat,
            self._openclaw_runtime(),
            self.cfg.swarm_ruflo_engine,
        )
        self._invalidate_ruflo_status_cache()
        return out

    def handle_ruflo_motores_stop(self) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        out = motores_stop(
            self.cfg.ruflo_chat,
            self._openclaw_runtime(),
            self.cfg.swarm_ruflo_engine,
        )
        self._invalidate_ruflo_status_cache()
        return out

    def handle_ruflo_motores_rebuild(self) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        out = motores_rebuild(
            self.cfg.ruflo_chat,
            self._openclaw_runtime(),
            memory_cfg=self.cfg.swarm_memory,
            engine_cfg=self.cfg.swarm_ruflo_engine,
        )
        self._invalidate_ruflo_status_cache()
        return out

    def _ruflo_status_cache_ttl(self) -> float:
        return max(0.0, float(getattr(self.cfg.ruflo_chat, "status_cache_seconds", 5.0) or 5.0))

    def handle_ruflo_status(self, *, refresh: bool = True) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        t0 = time.perf_counter()
        now = time.time()
        if (
            self._ruflo_status_cache_snapshot is not None
            and now < self._ruflo_status_cache_expires_at
        ):
            snap = dict(self._ruflo_status_cache_snapshot)
            self._record_ruflo_status_observation(
                snap, elapsed_s=time.perf_counter() - t0, cached=True
            )
            return snap
        if not refresh:
            if self._ruflo_status_cache_snapshot is not None:
                stale = dict(self._ruflo_status_cache_snapshot)
                stale["stale"] = True
                self._record_ruflo_status_observation(
                    stale, elapsed_s=time.perf_counter() - t0, cached=True
                )
                return stale
            pending = {
                "ok": False,
                "stale": True,
                "error": "ruflo status refresh pending",
                "hosted_ui_url": self.cfg.ruflo_chat.hosted_ui_url,
                "local_ui_url": self.cfg.ruflo_chat.local_ui_url,
            }
            self._record_ruflo_status_observation(
                pending, elapsed_s=time.perf_counter() - t0, cached=True
            )
            return pending
        snap = ruflo_status(self.cfg.ruflo_chat, self._openclaw_runtime())
        self._ruflo_status_cache_snapshot = dict(snap)
        ttl = self._ruflo_status_cache_ttl()
        self._ruflo_status_cache_expires_at = now + ttl if ttl > 0 else 0.0
        self._record_ruflo_status_observation(
            snap, elapsed_s=time.perf_counter() - t0, cached=False
        )
        return snap

    async def _startup_warm_ruflo_status_snapshot(self) -> None:
        """Pre-warm /ruflo/status cache so the first UI poll is fast."""
        if self._ruflo_chat_ready():
            return
        try:
            await asyncio.to_thread(self.handle_ruflo_status, refresh=True)
        except Exception as exc:
            log.warning("ruflo status warmup failed: %s", exc)

    def handle_ruflo_sessions_list(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        rc = self.cfg.ruflo_chat
        try:
            limit = int(query.get("limit", rc.sessions_limit))
        except (TypeError, ValueError):
            limit = rc.sessions_limit
        out = list_ruflo_sessions(
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            limit=limit,
        )
        out["hosted_ui_url"] = rc.hosted_ui_url
        out["local_ui_url"] = rc.local_ui_url
        return out

    def handle_ruflo_session_new(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        title = query.get("title")
        if title is not None and not isinstance(title, str):
            return {"ok": False, "error": "title must be a string"}
        team_id = query.get("team_id")
        if team_id is not None and not isinstance(team_id, str):
            return {"ok": False, "error": "team_id must be a string"}
        out = create_ruflo_session(
            self._openclaw_runtime(),
            self.cfg.ruflo_chat,
            title=(title or "Nova conversa RuFlo").strip(),
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            team_id=team_id,
        )
        self._invalidate_ruflo_status_cache()
        return out

    def handle_ruflo_session_delete(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        key = str(
            payload.get("session_key")
            or payload.get("key")
            or payload.get("sessionKey")
            or ""
        ).strip()
        if not key:
            return {"ok": False, "error": "session_key is required"}
        out = delete_ruflo_session(
            self._openclaw_runtime(),
            session_key=key,
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
        )
        self._invalidate_ruflo_status_cache()
        return out

    def handle_ruflo_messages_get(
        self, query: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        del user
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        key = str(query.get("session_key") or query.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "session_key is required"}
        try:
            limit = int(query.get("limit", self.cfg.ruflo_chat.messages_limit))
        except (TypeError, ValueError):
            limit = self.cfg.ruflo_chat.messages_limit
        return read_ruflo_messages(
            self._openclaw_runtime(),
            session_key=key,
            limit=limit,
            persistence=self.chat_persistence,
        )

    def handle_ruflo_route_preview(self, payload: dict) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        text = str(payload.get("message") or payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "message required"}
        return gather_orchestration_context(
            self.cfg.ruflo_chat,
            self._openclaw_runtime(),
            text,
        )

    def _build_project_knowledge_block(
        self,
        prompt: str,
        on_progress: Optional[Callable[[str], None]],
    ) -> str:
        """Format a compact project-memory block from the knowledge base."""
        store = self._knowledge_store
        if store is None:
            return ""
        try:
            snapshot = aggregate_knowledge(store)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("ruflo project-knowledge load failed: %s", exc)
            return ""
        projects = snapshot.get("projects") or []
        if not projects:
            return ""
        block = format_projects_knowledge_block(snapshot, query=prompt)
        if not block:
            return ""
        if on_progress is not None:
            try:
                on_progress(
                    f"RuFlo: memória de {len(projects)} projeto(s) carregada "
                    f"({len(block)} chars)"
                )
            except Exception:
                pass
        return block

    def _run_ruflo_send_job(
        self,
        job_id: str,
        *,
        session_key: str,
        text: str,
        user: Optional[UserRecord],
        model_id: Optional[str] = None,
    ) -> None:
        rc = self.cfg.ruflo_chat
        cc = self.cfg.openclaw_chat
        runtime = self._openclaw_runtime()

        def on_progress(message: str) -> None:
            chat_job_append_progress(job_id, message)

        chat_job_append_progress(job_id, "RuFlo: routing e memória…")
        knowledge_block = self._build_project_knowledge_block(text, on_progress)
        try:
            result = send_ruflo_message(
                runtime,
                self.cfg.openclaw_doctor,
                rc,
                cc,
                self.cfg.agent,
                session_key=session_key,
                message=text,
                persistence=self.chat_persistence,
                user_id=self._chat_user_id(user),
                on_progress=on_progress,
                user_already_saved=True,
                progress_job_id=job_id,
                project_knowledge_block=knowledge_block,
                project_store=self.project_memory,
                model_id=model_id,
                qclaw_tools=self._openclaw_chat_tool_context(user),
            )
            if result.get("ok"):
                reply = (result.get("reply") or result.get("text") or "").strip()
                if not reply:
                    chat_job_fail(
                        job_id,
                        "ruflo concluiu sem texto de resposta",
                        result={
                            **result,
                            "ok": False,
                            "error": "ruflo concluiu sem texto de resposta",
                        },
                    )
                    if self.chat_persistence is not None:
                        try:
                            self.chat_persistence.history.append_message(
                                session_key,
                                role="assistant",
                                text="⚠️ **Erro:** ruflo concluiu sem texto de resposta",
                            )
                        except Exception:
                            pass
                    return
                chat_job_complete(job_id, result)
            else:
                err_msg = str(result.get("error") or "envio falhou")
                chat_job_fail(job_id, err_msg, result=result)
                if self.chat_persistence is not None:
                    try:
                        self.chat_persistence.history.append_message(
                            session_key,
                            role="assistant",
                            text=f"⚠️ **Erro:** {err_msg}",
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.exception("async ruflo chat send failed job=%s: %s", job_id, e)
            err_msg = f"{type(e).__name__}: {e}"
            chat_job_fail(job_id, err_msg)
            if self.chat_persistence is not None:
                try:
                    self.chat_persistence.history.append_message(
                        session_key,
                        role="assistant",
                        text=f"⚠️ **Erro:** {err_msg}",
                    )
                except Exception:
                    pass

    def handle_ruflo_send(
        self, payload: dict, user: Optional[UserRecord] = None
    ) -> dict[str, Any]:
        err = self._ruflo_chat_ready()
        if err:
            return {"ok": False, "error": err}
        text = str(payload.get("message") or payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "message required"}
        rc = self.cfg.ruflo_chat
        cc = self.cfg.openclaw_chat
        model_id = str(
            payload.get("model_id")
            or payload.get("modelId")
            or payload.get("model")
            or ""
        ).strip() or None
        key = str(payload.get("session_key") or payload.get("key") or "").strip()
        if not key:
            created = create_ruflo_session(
                self._openclaw_runtime(),
                rc,
                persistence=self.chat_persistence,
                user_id=self._chat_user_id(user),
            )
            if not created.get("ok"):
                return created
            key = str(created.get("session_key") or "").strip()
        use_async = cc.async_send
        if payload.get("async") is False:
            use_async = False
        elif payload.get("async") is True:
            use_async = True
        if use_async:
            self._persist_chat_user_turn(key, text, agent_id="ruflo")
            job = chat_job_create(
                key,
                display_key=key,
                message_text=text,
                agent_id="ruflo",
                profile=None,
                user_id=self._chat_user_id(user),
            )
            chat_job_append_progress(job.id, "Pedido RuFlo recebido…")
            thread = threading.Thread(
                target=self._run_ruflo_send_job,
                kwargs={
                    "job_id": job.id,
                    "session_key": key,
                    "text": text,
                    "user": user,
                    "model_id": model_id,
                },
                name=f"ruflo-chat-send-{job.id}",
                daemon=True,
            )
            thread.start()
            self._invalidate_ruflo_status_cache()
            return {
                "ok": True,
                "async": True,
                "jobId": job.id,
                "sessionKey": key,
                "status": "running",
                "backend": "ruflo+deepseek",
            }
        out = send_ruflo_message(
            self._openclaw_runtime(),
            self.cfg.openclaw_doctor,
            rc,
            cc,
            self.cfg.agent,
            session_key=key,
            message=text,
            persistence=self.chat_persistence,
            user_id=self._chat_user_id(user),
            project_knowledge_block=self._build_project_knowledge_block(text, None),
            project_store=self.project_memory,
            model_id=model_id,
            qclaw_tools=self._openclaw_chat_tool_context(user),
        )
        self._invalidate_ruflo_status_cache()
        return out

"""Knowledge base and entity DB from chat history."""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .knowledge_base import (
    aggregate_entity_db,
    aggregate_knowledge,
    run_extraction as knowledge_run_extraction,
)

log = logging.getLogger(__name__)


class MonitorKnowledgeMixin:
    def _knowledge_ready(self) -> Optional[str]:
        if self._knowledge_store is None:
            return (
                "knowledge base indisponível: habilite openclaw_chat.history_enabled"
            )
        if (
            self.chat_persistence is None
            or getattr(self.chat_persistence, "history", None) is None
        ):
            return "chat history não inicializado"
        if not self.cfg.openclaw_chat.enabled:
            return "openclaw_chat está desabilitado"
        return None

    def handle_knowledge_get(self, query: dict | None = None) -> dict:
        del query
        err = self._knowledge_ready()
        if err:
            return {"ok": False, "error": err}
        try:
            return aggregate_knowledge(self._knowledge_store)
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge_base: aggregate failed: %s", exc)
            return {"ok": False, "error": f"falha ao agregar conhecimento: {exc}"}

    def handle_entity_db_get(self, query: dict | None = None) -> dict:
        del query
        err = self._knowledge_ready()
        if err:
            return {"ok": False, "error": err}
        try:
            return aggregate_entity_db(self._knowledge_store)
        except Exception as exc:  # noqa: BLE001
            log.warning("entity_db: aggregate failed: %s", exc)
            return {"ok": False, "error": f"falha ao agregar entidades: {exc}"}

    def handle_knowledge_extract(self, payload: dict | None = None) -> dict:
        err = self._knowledge_ready()
        if err:
            return {"ok": False, "error": err}
        payload = payload or {}
        force = bool(payload.get("force"))
        try:
            sessions_limit = int(payload.get("sessions_limit") or 80)
        except (TypeError, ValueError):
            sessions_limit = 80
        sessions_limit = max(1, min(sessions_limit, 500))
        model_id = payload.get("model_id")
        if model_id is not None and not isinstance(model_id, str):
            return {"ok": False, "error": "model_id deve ser string"}
        model_id_resolved = (
            (model_id.strip() or None) if isinstance(model_id, str) else None
        )
        # Run synchronously when explicitly requested (e.g. CLI), otherwise
        # spawn a background thread so the HTTP request returns immediately
        # and the UI can poll /knowledge for progress.
        if bool(payload.get("sync")):
            try:
                return knowledge_run_extraction(
                    chat_history=self.chat_persistence.history,
                    store=self._knowledge_store,
                    chat_cfg=self.cfg.openclaw_chat,
                    sessions_limit=sessions_limit,
                    force=force,
                    model_id=model_id_resolved,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge_base: extraction failed: %s", exc)
                return {"ok": False, "error": f"falha na extração: {exc}"}

        import threading

        def _worker() -> None:
            try:
                knowledge_run_extraction(
                    chat_history=self.chat_persistence.history,
                    store=self._knowledge_store,
                    chat_cfg=self.cfg.openclaw_chat,
                    sessions_limit=sessions_limit,
                    force=force,
                    model_id=model_id_resolved,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge_base: background extraction failed: %s", exc)

        # Try to take the lock now so we can fail fast if another extraction is running.
        if self._knowledge_store.lock_path.exists():
            return {
                "ok": False,
                "error": "extração já em execução (lock ativo)",
                "lock_path": str(self._knowledge_store.lock_path),
            }
        threading.Thread(
            target=_worker,
            name="knowledge-extract",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "status": "started",
            "message": "extração iniciada em background — recarregue a página para ver o progresso",
            "sessions_limit": sessions_limit,
            "force": force,
        }

    def handle_knowledge_delete_project(self, payload: dict | None = None) -> dict:
        err = self._knowledge_ready()
        if err:
            return {"ok": False, "error": err}
        payload = payload or {}
        project_key = payload.get("project_key")
        if not isinstance(project_key, str) or not project_key.strip():
            return {"ok": False, "error": "project_key é obrigatório"}
        try:
            result = self._knowledge_store.delete_project(project_key.strip())
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge_base: delete project failed: %s", exc)
            return {"ok": False, "error": f"falha ao apagar projeto: {exc}"}
        return {
            "ok": True,
            "project_key": project_key.strip(),
            "removed": result.get("removed", 0),
            "session_keys": result.get("session_keys", []),
        }

    def handle_knowledge_delete_session(self, payload: dict | None = None) -> dict:
        err = self._knowledge_ready()
        if err:
            return {"ok": False, "error": err}
        payload = payload or {}
        session_key = payload.get("session_key")
        if not isinstance(session_key, str) or not session_key.strip():
            return {"ok": False, "error": "session_key é obrigatório"}
        try:
            removed = self._knowledge_store.delete_sessions([session_key.strip()])
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge_base: delete session failed: %s", exc)
            return {"ok": False, "error": f"falha ao apagar sessão: {exc}"}
        return {"ok": True, "removed": removed}


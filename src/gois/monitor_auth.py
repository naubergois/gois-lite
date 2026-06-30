"""HTTP auth and user-admin handlers for :class:`gois.monitor.GoisMonitor`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .accounts import SESSION_COOKIE, UserRecord

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .monitor import GoisMonitor


class MonitorAuthMixin:
    """Session auth and /users admin endpoints (mixed into GoisMonitor)."""

    def auth_user_from_token(self, token: Optional[str]) -> Optional[UserRecord]:
        if not self.cfg.auth.enabled:
            return None
        return self.accounts.user_from_token(token)

    def handle_auth_bootstrap(self) -> dict:
        return {
            "ok": True,
            "enabled": self.cfg.auth.enabled,
            "allow_open_registration": self.cfg.auth.allow_open_registration,
            "has_users": self.accounts.user_count() > 0,
            "session_cookie": SESSION_COOKIE,
        }

    def handle_auth_register(self, payload: dict) -> dict:
        if not self.cfg.auth.enabled:
            return {"ok": False, "error": "auth is disabled"}
        if not self.cfg.auth.allow_open_registration and self.accounts.user_count() > 0:
            return {"ok": False, "error": "registro aberto desabilitado"}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "").strip()
        user = self.accounts.register(username, password)
        self.accounts.ensure_default_kanban_team(user.id)
        session = self.accounts.create_session(user.id)
        return {"ok": True, "user": user.to_public(), "session_token": session.token}

    def handle_auth_login(self, payload: dict) -> dict:
        if not self.cfg.auth.enabled:
            return {"ok": False, "error": "auth is disabled"}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "").strip()
        try:
            user = self._authenticate_with_mongo_recovery(username, password)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        self.accounts.ensure_default_kanban_team(user.id)
        self.accounts.ensure_scheduling_team(user.id)
        session = self.accounts.create_session(user.id)
        return {"ok": True, "user": user.to_public(), "session_token": session.token}

    def _authenticate_with_mongo_recovery(
        self, username: str, password: str
    ):
        from .mongo import _mongo_connection_errors, ping
        from .mongo_autostart import restart_mongod

        try:
            return self.accounts.authenticate(username, password)
        except _mongo_connection_errors() as exc:
            log.warning("auth login: MongoDB down — tentando restart: %s", exc)
            if restart_mongod() and ping():
                return self.accounts.authenticate(username, password)
            raise ValueError(
                "MongoDB indisponível. O restart automático falhou — "
                "execute `brew services restart mongodb-community@7.0` "
                "ou `docker compose up -d mongo`."
            ) from exc

    def handle_auth_me(self, user: Optional[UserRecord]) -> dict:
        if not self.cfg.auth.enabled:
            return {"ok": True, "user": {"id": "local", "username": "local"}}
        if user is None:
            return {"ok": False, "error": "not authenticated"}
        return {"ok": True, "user": user.to_public()}

    def handle_auth_logout(self, token: Optional[str]) -> dict:
        if token:
            self.accounts.destroy_session(token)
        return {"ok": True}

    def handle_users_list(self, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            users = self.accounts.list_users(actor)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "users": [u.to_public() for u in users]}

    def handle_users_create(
        self, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        is_admin = bool(payload.get("is_admin"))
        try:
            created = self.accounts.admin_create_user(
                actor, username, password, is_admin=is_admin
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "user": created.to_public()}

    def handle_users_delete(
        self, user_id: str, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            self.accounts.delete_user(actor, user_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def handle_users_update(
        self, user_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            updated: Optional[UserRecord] = None
            if "is_admin" in payload:
                updated = self.accounts.set_admin(
                    actor, user_id, bool(payload["is_admin"])
                )
            if "password" in payload:
                self.accounts.change_password(
                    actor, user_id, str(payload.get("password") or "")
                )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if updated is not None:
            return {"ok": True, "user": updated.to_public()}
        return {"ok": True}

    def _accounts_actor(self, user: Optional[UserRecord]) -> Optional[UserRecord]:
        """Resolve session user; when auth is off, use the synthetic local owner."""
        if user is not None:
            return user
        if not self.cfg.auth.enabled:
            return self.accounts.ensure_local_user()
        return None

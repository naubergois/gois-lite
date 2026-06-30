"""HTTP handlers for MongoDB-backed env key configuration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .accounts import UserRecord
from .env_keys_catalog import (
    KEY_CATALOG,
    all_catalog_key_names,
    aws_key_env_vars,
    catalog_entry,
    llm_key_env_vars,
)
from .env_keys_mongo import (
    EnvKeysStore,
    apply_env_keys_cache_to_environ,
    migrate_env_keys_to_mongo,
    prune_managed_keys_from_env,
)
from .roteiro_viral_keys import (
    fetch_roteiro_viral_managed_keys,
    roteiro_viral_api_base,
    sync_roteiro_viral_keys_to_store,
)
from .secrets_fallback import is_placeholder, is_storable_env_key

log = logging.getLogger(__name__)


def mask_secret(value: str) -> str:
    val = (value or "").strip()
    if not val:
        return ""
    if len(val) <= 6:
        return "••••"
    return f"{val[:3]}…{val[-4:]}"


def _apply_store_to_environ(store: EnvKeysStore) -> None:
    apply_env_keys_cache_to_environ(store.doc_id)


class MonitorEnvKeysMixin:
    def _env_keys_admin(self, user: Optional[UserRecord]) -> Optional[dict[str, Any]]:
        actor = self._accounts_actor(user)
        if self.cfg.auth.enabled and actor is None:
            return {"ok": False, "error": "not authenticated"}
        if self.cfg.auth.enabled and actor is not None and not actor.is_admin:
            return {"ok": False, "error": "admin required"}
        return None

    def _env_keys_stats(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        llm_rows = []
        aws_rows = []
        env_rows = []
        configured_count = 0
        llm_configured = 0
        aws_configured = 0
        env_configured = 0

        for r in rows:
            if r.get("configured"):
                configured_count += 1
            if r.get("is_llm"):
                llm_rows.append(r)
                if r.get("configured"):
                    llm_configured += 1
            if r.get("is_aws"):
                aws_rows.append(r)
                if r.get("configured"):
                    aws_configured += 1
            if r.get("is_env"):
                env_rows.append(r)
                if r.get("configured"):
                    env_configured += 1

        return {
            "catalog": len(KEY_CATALOG),
            "configured": configured_count,
            "total_rows": len(rows),
            "llm_total": len(llm_rows),
            "llm_configured": llm_configured,
            "llm_catalog": len(llm_key_env_vars()),
            "aws_total": len(aws_rows),
            "aws_configured": aws_configured,
            "aws_catalog": len(aws_key_env_vars()),
            "env_total": len(env_rows),
            "env_configured": env_configured,
        }

    def _env_keys_rows(self, store: EnvKeysStore) -> list[dict[str, Any]]:
        stored = store.get_all()
        rv_fallback = fetch_roteiro_viral_managed_keys()
        names: list[str] = []
        seen: set[str] = set()
        for name in sorted(stored):
            if name not in seen:
                names.append(name)
                seen.add(name)
        for name in all_catalog_key_names():
            if name not in seen:
                names.append(name)
                seen.add(name)
        rows: list[dict[str, Any]] = []
        for name in names:
            entry = catalog_entry(name)
            value = stored.get(name, "")
            source = "mongodb" if value else ""
            if not value:
                value = rv_fallback.get(name, "")
                if value:
                    source = "roteiro_viral"
            rows.append(
                {
                    **entry,
                    "configured": bool(value),
                    "masked": mask_secret(value) if value else "",
                    "source": source,
                }
            )
        return rows

    def handle_env_keys_get(self, user: Optional[UserRecord] = None) -> dict[str, Any]:
        denied = self._env_keys_admin(user)
        if denied:
            return denied
        store = EnvKeysStore()
        rows = self._env_keys_rows(store)
        return {
            "ok": True,
            "keys": rows,
            "llm_keys": list(llm_key_env_vars()),
            "aws_keys": list(aws_key_env_vars()),
            "stats": self._env_keys_stats(rows),
        }

    def handle_env_keys_update(
        self,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        denied = self._env_keys_admin(user)
        if denied:
            return denied

        body = payload if isinstance(payload, dict) else {}
        store = EnvKeysStore()
        updates: dict[str, str] = {}
        raw_keys = body.get("keys")
        if isinstance(raw_keys, dict):
            for key_raw, val_raw in raw_keys.items():
                name = str(key_raw or "").strip()
                if not name or not is_storable_env_key(name):
                    continue
                val = str(val_raw or "").strip()
                if not val or is_placeholder(val):
                    continue
                updates[name] = val

        deleted: list[str] = []
        raw_delete = body.get("delete")
        if isinstance(raw_delete, list):
            for key_raw in raw_delete:
                name = str(key_raw or "").strip()
                if name and is_storable_env_key(name):
                    deleted.append(name)

        if updates:
            store.upsert(updates, merge=True, allow_all=True)
        if deleted:
            store.delete(deleted)

        if updates or deleted:
            _apply_store_to_environ(store)
            prune_managed_keys_from_env(Path(".env"))
            log.info(
                "env keys updated via dashboard (set=%d delete=%d)",
                len(updates),
                len(deleted),
            )

        rows = self._env_keys_rows(store)
        return {
            "ok": True,
            "updated": len(updates),
            "deleted": deleted,
            "keys": rows,
            "llm_keys": list(llm_key_env_vars()),
            "stats": self._env_keys_stats(rows),
        }

    def handle_env_keys_import(
        self,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        denied = self._env_keys_admin(user)
        if denied:
            return denied

        body = payload if isinstance(payload, dict) else {}
        env_path = Path(str(body.get("env_file") or ".env")).expanduser()
        result = migrate_env_keys_to_mongo(local_env=env_path)
        _apply_store_to_environ(EnvKeysStore())
        pruned = prune_managed_keys_from_env(env_path)
        rows = self._env_keys_rows(EnvKeysStore())
        return {
            "ok": True,
            "imported": result.get("imported", 0),
            "total": result.get("total", 0),
            "pruned": pruned.get("removed") or [],
            "keys": rows,
            "llm_keys": list(llm_key_env_vars()),
            "stats": self._env_keys_stats(rows),
        }

    def handle_env_keys_prune_env(
        self,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        denied = self._env_keys_admin(user)
        if denied:
            return denied

        body = payload if isinstance(payload, dict) else {}
        env_path = Path(str(body.get("env_file") or ".env")).expanduser()
        pruned = prune_managed_keys_from_env(env_path)
        rows = self._env_keys_rows(EnvKeysStore())
        return {
            "ok": True,
            "removed": pruned.get("removed") or [],
            "path": pruned.get("path"),
            "keys": rows,
            "llm_keys": list(llm_key_env_vars()),
            "stats": self._env_keys_stats(rows),
        }

    def handle_env_keys_import_roteiro_viral(
        self,
        payload: dict,
        user: Optional[UserRecord] = None,
    ) -> dict[str, Any]:
        denied = self._env_keys_admin(user)
        if denied:
            return denied

        body = payload if isinstance(payload, dict) else {}
        api_base = str(body.get("api_base") or "").strip() or None
        rv_keys = fetch_roteiro_viral_managed_keys(api_base=api_base)
        if not rv_keys:
            return {
                "ok": False,
                "error": (
                    "Nenhuma chave encontrada no Roteiro Viral "
                    f"({api_base or roteiro_viral_api_base()}). "
                    "Verifique ROTEIRO_VIRAL_PATH/.env ou API RV (/config/api-keys)."
                ),
            }

        result = sync_roteiro_viral_keys_to_store(api_base=api_base)
        if not result.get("ok"):
            return result
        written = int(result.get("imported") or 0)
        log.info(
            "env keys imported from roteiro viral (written=%d keys=%s)",
            written,
            sorted(rv_keys.keys()),
        )
        rows = self._env_keys_rows(EnvKeysStore())
        return {
            "ok": True,
            "imported": written,
            "imported_keys": result.get("imported_keys") or sorted(rv_keys.keys()),
            "source": api_base or roteiro_viral_api_base(),
            "keys": rows,
            "llm_keys": list(llm_key_env_vars()),
            "stats": self._env_keys_stats(rows),
        }

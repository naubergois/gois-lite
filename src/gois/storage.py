"""Unified persistence entry points for gois.

Runtime code should import stores from this module instead of wiring
``*_mongo`` / ``*_sqlite`` backends directly. Legacy SQLite files are
migration-only — seeded once at startup via :func:`mongo_persistence.bootstrap_stack_migrations`
or manually via ``scripts/migrate_all_to_mongo``.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

from .accounts_mongo import AccountStoreMongo, get_account_store, migrate_json_to_mongo
from .accounts_team_guard import ensure_protected_teams
from .config import AuthConfig
from .mongo_sqlite_bridge import resolve_scope_path
from .whatsapp_allowlist_mongo import (
    WhatsAppAllowlistStoreMongo,
    get_allowlist_store,
    migrate_allowlist_to_mongo,
)

log = logging.getLogger(__name__)

__all__ = [
    "AccountStoreMongo",
    "WhatsAppAllowlistStoreMongo",
    "bootstrap_accounts",
    "get_account_store",
    "get_allowlist_store",
    "get_chat_history_store",
    "get_email_memoria_store",
    "get_email_pessoas_store",
    "get_google_photos_store",
    "get_teams_calendar_store",
    "get_tool_learnings_store",
    "init_whatsapp_allowlist",
    "migrate_allowlist_to_mongo",
    "migrate_json_to_mongo",
    "sync_team_whatsapp_groups_to_allowlist",
    "repair_team_whatsapp_group_links",
]


def get_chat_history_store(
    scope_path: Path | str,
    *,
    db_path: Path | str | None = None,
):
    """Open chat history store (Mongo in full gois; SQL in gois-lite)."""
    try:
        from .gois_lite_storage import lite_uses_sql, open_lite_chat_history_store

        if lite_uses_sql():
            return open_lite_chat_history_store(scope_path, db_path=db_path)
    except ImportError:
        pass
    from .chat_history import ChatHistoryStore

    resolved = resolve_scope_path(scope_path, db_path=db_path)
    return ChatHistoryStore(resolved)


def get_email_memoria_store(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
):
    """Open email memoria store (Mongo-only runtime)."""
    from .email_memoria_store import EmailMemoriaStore, default_db_path

    resolved = resolve_scope_path(scope_path, db_path=db_path, default=default_db_path)
    return EmailMemoriaStore(resolved)


def get_email_pessoas_store(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
):
    """Open email pessoas store (Mongo-only runtime)."""
    from .email_pessoas_store import EmailPessoasStore, default_db_path

    resolved = resolve_scope_path(scope_path, db_path=db_path, default=default_db_path)
    return EmailPessoasStore(resolved)


def get_tool_learnings_store(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
):
    """Open tool learnings store (Mongo-only runtime)."""
    from .tool_learnings_store import ToolLearningsStore, default_db_path

    resolved = resolve_scope_path(scope_path, db_path=db_path, default=default_db_path)
    return ToolLearningsStore(resolved)


def get_google_photos_store(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
):
    """Open Google Photos picker store (Mongo-only runtime)."""
    from .google_photos_store import PhotosDB, default_db_path

    resolved = resolve_scope_path(scope_path, db_path=db_path, default=default_db_path)
    return PhotosDB(resolved)


def get_teams_calendar_store(
    scope_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
):
    """Open Teams calendar store (Mongo-only runtime)."""
    from .teams_calendar_store import TeamsCalendarDB, default_db_path

    resolved = resolve_scope_path(scope_path, db_path=db_path, default=default_db_path)
    return TeamsCalendarDB(resolved)


def init_whatsapp_allowlist(
    *,
    scope_dir: Optional[Path] = None,
    recipient: Optional[str] = None,
    allowed_recipients: Optional[list[str]] = None,
) -> WhatsAppAllowlistStoreMongo:
    """Return the allowlist store, seeding from config when empty."""
    if scope_dir is None:
        from .local_paths import project_stack_root

        scope_dir = project_stack_root() / "whatsapp"
    store = migrate_allowlist_to_mongo(scope_dir.expanduser().resolve())
    if not store.list_all():
        store.seed_from_config(
            recipient=recipient,
            allowed_recipients=list(allowed_recipients or []),
        )
    return store


def sync_team_whatsapp_groups_to_allowlist(
    accounts: AccountStoreMongo,
) -> int:
    """Ensure every enabled team WhatsApp group JID exists in the allowlist store."""
    from .whatsapp_allowlist import normalize_group_jid

    added = 0
    try:
        data = accounts._load()
    except Exception:
        return 0
    for row in (data.get("teams") or {}).values():
        if not isinstance(row, dict):
            continue
        team_name = str(row.get("name") or row.get("id") or "time")
        jids: list[str] = []
        wg = row.get("whatsapp_group")
        if isinstance(wg, dict) and wg.get("enabled", True):
            jid = normalize_group_jid(wg.get("group_jid"))
            if jid:
                jids.append(jid)
        for raw in row.get("whatsapp_numbers") or []:
            jid = normalize_group_jid(raw)
            if jid and jid not in jids:
                jids.append(jid)
        for jid in jids:
            try:
                store = get_allowlist_store()
                if jid.lower() in store.get_enabled_group_jids():
                    continue
                store.add(
                    name=team_name,
                    phone=jid,
                    label=f"auto:team:{team_name}",
                    enabled=True,
                )
                added += 1
            except ValueError:
                pass
            except Exception as exc:
                log.debug("sync team group to allowlist skipped: %s", exc)
    if added:
        log.info("Synced %d team WhatsApp group(s) to allowlist", added)
    return added


def repair_team_whatsapp_group_links(accounts: AccountStoreMongo) -> int:
    """Backfill ``whatsapp_group`` for teams that only have a group in numbers."""
    repaired = 0
    try:
        data = accounts._load()
    except Exception:
        return 0
    dirty = False
    for row in (data.get("teams") or {}).values():
        if not isinstance(row, dict):
            continue
        if accounts._ensure_whatsapp_group_link(row):
            repaired += 1
            dirty = True
    if dirty:
        accounts._save(data)
        log.info("Repaired whatsapp_group link for %d team(s)", repaired)
    return repaired


def bootstrap_accounts(auth: AuthConfig) -> AccountStoreMongo:
    """Open the account store and ensure default admin/local user + teams."""
    store = get_account_store(
        Path(auth.data_dir), session_ttl_seconds=auth.session_ttl_seconds
    )
    if auth.enabled:
        bootstrap_pwd = auth.bootstrap_admin_password
        random_generated = False
        if (
            not auth.reset_bootstrap_admin_password
            and bootstrap_pwd.strip().lower() in {"admin", "changeme", "password", ""}
            and store.find_user_by_username(auth.bootstrap_admin_username) is None
        ):
            bootstrap_pwd = secrets.token_urlsafe(18)
            random_generated = True
        admin = store.ensure_default_admin(
            auth.bootstrap_admin_username,
            bootstrap_pwd,
            reset_password=auth.reset_bootstrap_admin_password,
        )
        if admin is not None:
            if random_generated:
                cred_path = (
                    Path(auth.data_dir).expanduser().resolve() / "admin_password.txt"
                )
                try:
                    cred_path.parent.mkdir(parents=True, exist_ok=True)
                    cred_path.write_text(
                        f"username: {auth.bootstrap_admin_username}\n"
                        f"password: {bootstrap_pwd}\n",
                        encoding="utf-8",
                    )
                    try:
                        os.chmod(cred_path, 0o600)
                    except OSError:
                        pass
                except OSError as exc:
                    log.error("could not persist bootstrap admin password: %s", exc)
                log.warning(
                    "admin bootstrap criado (login=%s); senha aleatória em %s "
                    "(permissões 0600). Troque-a em /users no primeiro login.",
                    auth.bootstrap_admin_username,
                    cred_path,
                )
            elif auth.reset_bootstrap_admin_password:
                log.warning(
                    "senha do admin bootstrap reposta (login=%s) — "
                    "desative auth.reset_bootstrap_admin_password após entrar",
                    auth.bootstrap_admin_username,
                )
            else:
                log.warning(
                    "admin bootstrap criado (login=%s) com a senha definida em "
                    "auth.bootstrap_admin_password. Recomenda-se trocar via /users.",
                    auth.bootstrap_admin_username,
                )
            store.ensure_default_kanban_team(admin.id)
            store.ensure_scheduling_team(admin.id)
    else:
        local = store.ensure_local_user()
        store.ensure_default_kanban_team(local.id)
        store.ensure_scheduling_team(local.id)

    def _protected_teams_bootstrap() -> None:
        try:
            guard = ensure_protected_teams(store)
            if guard.get("restored"):
                log.info(
                    "protected teams restored on bootstrap: %s",
                    ", ".join(guard["restored"]),
                )
        except Exception as exc:
            log.warning("protected teams bootstrap skipped: %s", exc)

    # Kanban dedupe + attachment reconciliation can scan every team on disk
    # (slow on network volumes). Do not block HTTP startup on it.
    threading.Thread(
        target=_protected_teams_bootstrap,
        name="protected-teams-bootstrap",
        daemon=True,
    ).start()
    return store

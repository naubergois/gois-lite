"""MongoDB-backed AccountStore — drop-in replacement for the SQLite/JSON store.

Like ``AccountStoreSQLite``, this subclasses :class:`AccountStore` and overrides
only the persistence seam (``_load`` / ``_save``). All business logic (users,
teams, sessions, kanban) is inherited unchanged, so the public API is identical.

State is stored as a single document per ``data_dir`` scope in the ``accounts``
collection::

    { "_id": "<resolved data_dir>", "users": {...}, "teams": {...},
      "sessions": {...}, "_updated_at": <epoch> }

Scoping by ``data_dir`` keeps independent stores (and tests using temp dirs)
isolated within the same MongoDB database. Per-team kanban boards live in the
``kanban_boards`` collection (see ``kanban_mongo``); legacy YAML under
``data_dir/teams`` is imported on first read or via ``migrate_all_to_mongo``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .accounts import AccountStore
from .mongo import get_collection

log = logging.getLogger(__name__)

ACCOUNTS_COLLECTION = "accounts"
_EMPTY = {"users": {}, "teams": {}, "sessions": {}}

_STORES: dict[str, "AccountStoreMongo"] = {}
_STORES_LOCK = threading.Lock()


class AccountStoreMongo(AccountStore):
    """MongoDB-backed account store with the same public API as AccountStore."""

    _TEAMS_CACHE_TTL = 15.0

    def __init__(
        self, data_dir: Path, *, session_ttl_seconds: float = 604_800.0
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.session_ttl_seconds = session_ttl_seconds
        # Kept for compatibility (teams kanban dir + migration source path).
        self._store_path = self.data_dir / "store.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._scope = str(self.data_dir)
        self._kanban_cache: dict[str, tuple[float, dict]] = {}
        self._teams_cache: Optional[tuple[float, list]] = None
        self._load_cache: Optional[tuple[float, dict]] = None
        self._LOAD_CACHE_TTL = 2.0
        self._cache_lock = threading.RLock()
        self._cache_gen = 0

    def _collection(self):
        return get_collection(ACCOUNTS_COLLECTION)

    def invalidate_caches(self) -> None:
        """Drop in-memory team/load caches after writes."""
        with self._cache_lock:
            self._teams_cache = None
            self._load_cache = None
            self._cache_gen += 1

    def _load(self) -> dict[str, Any]:
        with self._cache_lock:
            now = time.time()
            if self._load_cache is not None and now < self._load_cache[0]:
                return dict(self._load_cache[1])
            gen = self._cache_gen
        doc = self._collection().find_one({"_id": self._scope})
        if not doc:
            result: dict[str, Any] = {"users": {}, "teams": {}, "sessions": {}}
        else:
            result = {}
            for key in ("users", "teams", "sessions"):
                value = doc.get(key)
                result[key] = value if isinstance(value, dict) else {}
        with self._cache_lock:
            if self._cache_gen == gen:
                self._load_cache = (time.time() + self._LOAD_CACHE_TTL, result)
        return dict(result)

    def _save(self, data: dict[str, Any]) -> None:
        payload = {
            "_id": self._scope,
            "users": data.get("users") or {},
            "teams": data.get("teams") or {},
            "sessions": data.get("sessions") or {},
            "_updated_at": time.time(),
        }
        self._collection().replace_one({"_id": self._scope}, payload, upsert=True)
        self.invalidate_caches()

    def list_all_teams(self, *, fresh: bool = False) -> list:
        if fresh:
            return self._list_all_teams_from_mongo(refresh_cache=True)
        with self._cache_lock:
            now = time.time()
            if self._teams_cache is not None and now < self._teams_cache[0]:
                return list(self._teams_cache[1])
            gen = self._cache_gen
        teams = super().list_all_teams()
        with self._cache_lock:
            if self._cache_gen == gen:
                self._teams_cache = (time.time() + self._TEAMS_CACHE_TTL, list(teams))
        return teams

    def _list_all_teams_from_mongo(self, *, refresh_cache: bool) -> list:
        """Read teams straight from MongoDB (bypass in-process caches)."""
        from .accounts import TeamRecord

        doc = self._collection().find_one({"_id": self._scope}, {"teams": 1})
        rows = (doc or {}).get("teams") or {}
        out: list = []
        for row in rows.values():
            if isinstance(row, dict):
                try:
                    out.append(self._team_from_row(row))
                except ValueError:
                    continue
        out.sort(key=lambda t: t.created_at, reverse=True)
        if refresh_cache:
            with self._cache_lock:
                self._teams_cache = (time.time() + self._TEAMS_CACHE_TTL, list(out))
                self._load_cache = None
        return out


def team_exists_in_mongo(store: AccountStoreMongo, team_id: str) -> bool:
    """Return True when ``team_id`` is present in the MongoDB accounts document."""
    tid = str(team_id or "").strip()
    if not tid:
        return False
    from .mongo import ping

    if not ping():
        return False
    doc = store._collection().find_one({"_id": store._scope}, {"teams": 1})
    teams = (doc or {}).get("teams") or {}
    row = teams.get(tid)
    return isinstance(row, dict) and str(row.get("id") or tid) == tid


def migrate_json_to_mongo(
    data_dir: Path,
    *,
    session_ttl_seconds: float = 604_800.0,
) -> AccountStoreMongo:
    """Seed MongoDB from store.json / accounts.db, then return a Mongo store.

    Order of precedence for existing data: an existing SQLite ``accounts.db``
    first (current production backend), then the legacy ``store.json``. Whatever
    is found is loaded once and written into MongoDB. Source files are left in
    place (non-destructive) so a rollback is always possible.
    """
    data_dir = data_dir.expanduser().resolve()
    store = AccountStoreMongo(data_dir, session_ttl_seconds=session_ttl_seconds)

    # Nothing to do if Mongo already has data for this scope.
    if store._collection().find_one({"_id": store._scope}):
        return store

    db_path = data_dir / "accounts.db"
    json_path = data_dir / "store.json"

    source_data: dict[str, Any] | None = None
    if db_path.is_file():
        try:
            from .accounts_sqlite import AccountStoreSQLite

            source_data = AccountStoreSQLite(data_dir)._load()
            log.info("Seeding MongoDB accounts from %s", db_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not read %s: %s", db_path, exc)
            source_data = None
    if source_data is None and json_path.is_file():
        import json

        try:
            source_data = json.loads(json_path.read_text(encoding="utf-8"))
            log.info("Seeding MongoDB accounts from %s", json_path)
        except Exception as exc:
            log.warning("could not read %s: %s", json_path, exc)
            source_data = None

    if source_data:
        store._save(
            {
                "users": source_data.get("users") or {},
                "teams": source_data.get("teams") or {},
                "sessions": source_data.get("sessions") or {},
            }
        )
        loaded = store._load()
        log.info(
            "MongoDB accounts seeded: %d users, %d teams",
            len(loaded.get("users") or {}),
            len(loaded.get("teams") or {}),
        )
    else:
        log.info("No existing account data — starting with empty MongoDB store")

    return store


def get_account_store(
    data_dir: Optional[Path] = None,
    *,
    session_ttl_seconds: float = 604_800.0,
) -> AccountStoreMongo:
    """Return the shared account store for the project stack."""
    if data_dir is None:
        from .local_paths import project_stack_root

        data_dir = project_stack_root() / "accounts"
    data_dir = data_dir.expanduser().resolve()
    try:
        from .gois_lite import is_gois_lite
        from .gois_lite_storage import get_lite_account_store, lite_uses_mongo

        if is_gois_lite() and not lite_uses_mongo():
            return get_lite_account_store(  # type: ignore[return-value]
                data_dir, session_ttl_seconds=session_ttl_seconds
            )
    except ImportError:
        pass
    scope = str(data_dir)
    with _STORES_LOCK:
        cached = _STORES.get(scope)
        if cached is not None:
            return cached
        from .mongo import ping

        if ping():
            store = migrate_json_to_mongo(
                data_dir, session_ttl_seconds=session_ttl_seconds
            )
        else:
            store = AccountStoreMongo(data_dir, session_ttl_seconds=session_ttl_seconds)
        _STORES[scope] = store
        return store


def reset_account_store_cache() -> None:
    """Clear the module-level store singleton (tests only)."""
    with _STORES_LOCK:
        _STORES.clear()

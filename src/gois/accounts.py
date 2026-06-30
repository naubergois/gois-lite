"""Multi-user accounts, teams, and per-team kanban boards."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .team_presets import (
    DEFAULT_KANBAN_COLUMNS,
    DEFAULT_PROJECT_TEAM_ID,
    DEFAULT_PROJECT_TEAM_NAME,
    SCHEDULING_TEAM_ID,
    SCHEDULING_TEAM_NAME,
    SCHEDULING_TEAM_DESCRIPTION,
    SCHEDULING_KANBAN_COLUMNS,
    TRELLO_KANBAN_COLUMNS,
    default_project_starter_tasks,
    get_team_preset,
    is_default_project_team_id,
    starter_kanban_tasks,
)
from .accounts_team_guard import provision_team_disk_artifacts
from .team_workspace import ensure_team_workspace

log = logging.getLogger(__name__)

SESSION_COOKIE = "qcm_session"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
_TEAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_PBKDF2_ROUNDS = 260_000


def _now() -> float:
    return time.time()


def _normalize_site_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    return text


def _parse_site_link_entry(entry: Any) -> Optional[dict[str, str]]:
    if isinstance(entry, str):
        url = _normalize_site_url(entry)
        if not url:
            return None
        return {"url": url, "label": url.rstrip("/").rsplit("/", 1)[-1], "description": ""}
    if not isinstance(entry, dict):
        return None
    url = _normalize_site_url(str(entry.get("url") or ""))
    if not url:
        return None
    label = str(entry.get("label") or entry.get("name") or "").strip()
    if not label:
        label = url.rstrip("/").rsplit("/", 1)[-1]
    description = str(entry.get("description") or "").strip()
    return {"url": url, "label": label, "description": description}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def normalize_username(raw: str) -> str:
    name = raw.strip().lower()
    if not _USERNAME_RE.match(name):
        raise ValueError(
            "username inválido — use 3–32 caracteres: letras, números, . _ -"
        )
    return name


def normalize_team_id(raw: Optional[str] = None) -> str:
    if raw and str(raw).strip():
        tid = str(raw).strip().lower()
        if not _TEAM_ID_RE.match(tid):
            raise ValueError("team id inválido")
        return tid
    return uuid.uuid4().hex[:12]


def _team_name_similarity(a: str, b: str) -> float:
    """Return a similarity ratio (0.0–1.0) between two team names.

    Uses normalized tokens + Jaccard + subsequence heuristics to catch
    near-duplicates like 'Queimadas Ceará' vs 'Gêmeo Digital Queimadas Ceará'.
    """
    import unicodedata

    def _strip_accents(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFKD", s)
            if unicodedata.category(c) != "Mn"
        )

    def _normalize(s: str) -> set[str]:
        s = _strip_accents(s.lower())
        s = re.sub(r"[^\w\s]", "", s)
        tokens = set()
        for t in s.split():
            if len(t) > 2:
                tokens.add(t)
                # Add stem (first 5 chars) to match plurals/variations
                if len(t) > 5:
                    tokens.add(t[:5])
        return tokens

    ta, tb = _normalize(a), _normalize(b)
    if not ta or not tb:
        return 0.0
    intersection = ta & tb
    union = ta | tb
    jaccard = len(intersection) / len(union) if union else 0.0
    # Boost if one name is a subset of the other (reduced weight to avoid
    # false positives from stem overlaps like 'asesi' matching 'asesiusj')
    subset_ratio = len(intersection) / min(len(ta), len(tb)) if min(len(ta), len(tb)) else 0.0
    return max(jaccard, subset_ratio * 0.70)


def team_name_key(name: str) -> str:
    """Canonical key for exact team-name comparison (case/accent/punctuation insensitive)."""
    import unicodedata

    text = unicodedata.normalize("NFKD", str(name or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", text)


def find_team_by_name_key(
    name: str,
    existing_teams: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find an existing team whose display name matches exactly (normalized)."""
    key = team_name_key(name)
    if not key:
        return None
    for team in existing_teams:
        if team_name_key(str(team.get("name") or "")) == key:
            return team
    return None


def find_similar_team(
    name: str,
    existing_teams: list[dict[str, Any]],
    *,
    threshold: float = 0.90,
) -> Optional[dict[str, Any]]:
    """Find an existing team with a similar name.

    Returns the best match above threshold, or None.
    Only considers a match when names are nearly identical (e.g. typos,
    casing, accents). Distinct names like 'ASESI' vs 'ASESIUSJ' will NOT match.
    """
    best_score = 0.0
    best_team: Optional[dict[str, Any]] = None
    for team in existing_teams:
        team_name = str(team.get("name") or "")
        score = _team_name_similarity(name, team_name)
        if score > best_score:
            best_score = score
            best_team = team
    if best_score >= threshold and best_team is not None:
        return best_team
    return None


@dataclass
class UserRecord:
    id: str
    username: str
    password_hash: str
    created_at: float
    is_admin: bool = False
    email: str = ""
    whatsapp: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "created_at": self.created_at,
            "is_admin": self.is_admin,
            "email": self.email,
            "whatsapp": self.whatsapp,
        }


def latex_workspace_ids_from_row(row: dict[str, Any]) -> list[str]:
    """Return ordered unique LaTeX workspace ids for a team row."""
    ids: list[str] = []
    raw = row.get("latex_workspace_ids")
    if isinstance(raw, list):
        for item in raw:
            s = str(item or "").strip()
            if s and s not in ids:
                ids.append(s)
    single = row.get("latex_workspace_id")
    if single and str(single).strip():
        s = str(single).strip()
        if s not in ids:
            ids.insert(0, s)
    return ids


def sync_latex_workspace_fields(row: dict[str, Any], ids: list[str]) -> None:
    """Persist both legacy primary id and the full workspace list."""
    unique: list[str] = []
    seen: set[str] = set()
    for item in ids:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        unique.append(s)
    row["latex_workspace_ids"] = unique
    row["latex_workspace_id"] = unique[0] if unique else None


@dataclass
class TeamRecord:
    id: str
    name: str
    owner_id: str
    created_at: float
    description: str = ""
    project_source: Optional[str] = None
    github_url: Optional[str] = None
    local_path: Optional[str] = None
    github_branch: str = "main"
    app_url: Optional[str] = None
    profile_slugs: list[str] = field(default_factory=list)
    whatsapp_numbers: list[str] = field(default_factory=list)
    artifacts_dir: Optional[str] = None
    whatsapp_group: Optional[dict[str, Any]] = None
    member_ids: list[str] = field(default_factory=list)
    github_repos: list[dict[str, Any]] = field(default_factory=list)
    site_links: list[dict[str, Any]] = field(default_factory=list)
    notify_emails: list[str] = field(default_factory=list)
    linked_emails: list[str] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    latex_workspace_id: Optional[str] = None
    latex_workspace_ids: list[str] = field(default_factory=list)
    swarm_name: Optional[str] = None
    github_autopush: bool = False

    def is_member(self, user_id: str) -> bool:
        """Return True if user_id is the owner or a member of this team."""
        return user_id == self.owner_id or user_id in self.member_ids

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "description": self.description,
            "project_source": self.project_source,
            "github_url": self.github_url,
            "local_path": self.local_path,
            "github_branch": self.github_branch,
            "app_url": self.app_url,
            "profile_slugs": list(self.profile_slugs),
            "whatsapp_numbers": list(self.whatsapp_numbers),
            "artifacts_dir": self.artifacts_dir,
            "whatsapp_group": self.whatsapp_group,
            "member_ids": list(self.member_ids),
            "github_repos": list(self.github_repos),
            "site_links": list(self.site_links),
            "notify_emails": list(self.notify_emails),
            "linked_emails": list(self.linked_emails),
            "contacts": list(self.contacts),
            "latex_workspace_id": self.latex_workspace_id,
            "latex_workspace_ids": list(self.latex_workspace_ids),
            "swarm_name": self.swarm_name,
            "github_autopush": self.github_autopush,
        }


@dataclass
class SessionRecord:
    token: str
    user_id: str
    expires_at: float

    def is_valid(self) -> bool:
        return _now() < self.expires_at


class AccountStore:
    """File-backed users, teams, sessions, and kanban YAML per team."""

    def __init__(self, data_dir: Path, *, session_ttl_seconds: float = 604_800.0) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.session_ttl_seconds = session_ttl_seconds
        self._store_path = self.data_dir / "store.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._kanban_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _kanban_cache_bucket(self) -> dict[str, tuple[float, dict[str, Any]]]:
        """Per-store kanban read cache (lazy for Mongo/SQLite subclasses)."""
        bucket = getattr(self, "_kanban_cache", None)
        if bucket is None:
            bucket = {}
            self._kanban_cache = bucket
        return bucket

    def _load(self) -> dict[str, Any]:
        if not self._store_path.is_file():
            return {"users": {}, "teams": {}, "sessions": {}}
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read account store: %s", e)
            return {"users": {}, "teams": {}, "sessions": {}}
        if not isinstance(data, dict):
            return {"users": {}, "teams": {}, "sessions": {}}
        for key in ("users", "teams", "sessions"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self._store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._store_path)

    def user_count(self) -> int:
        return len(self._load().get("users") or {})

    def register(self, username: str, password: str) -> UserRecord:
        return self._create_user(username, password, is_admin=False, min_password_len=6)

    def _create_user(
        self,
        username: str,
        password: str,
        *,
        is_admin: bool = False,
        min_password_len: int = 6,
    ) -> UserRecord:
        uname = normalize_username(username)
        if len(password) < min_password_len:
            raise ValueError(
                f"senha deve ter pelo menos {min_password_len} caracteres"
            )
        data = self._load()
        users: dict[str, Any] = data["users"]
        for row in users.values():
            if isinstance(row, dict) and row.get("username") == uname:
                raise ValueError("username já existe")
        user_id = uuid.uuid4().hex[:16]
        record = UserRecord(
            id=user_id,
            username=uname,
            password_hash=hash_password(password),
            created_at=_now(),
            is_admin=bool(is_admin),
        )
        users[user_id] = {
            "id": record.id,
            "username": record.username,
            "password_hash": record.password_hash,
            "created_at": record.created_at,
            "is_admin": record.is_admin,
            "email": record.email,
            "whatsapp": record.whatsapp,
        }
        self._save(data)
        return record

    def ensure_local_user(self) -> UserRecord:
        """Synthetic owner for kanban when auth is disabled."""
        data = self._load()
        users: dict[str, Any] = data.get("users") or {}
        for row in users.values():
            if isinstance(row, dict) and str(row.get("username") or "") == "local":
                return self._user_from_row(row)
        record = UserRecord(
            id="local",
            username="local",
            password_hash="!",
            created_at=_now(),
            is_admin=True,
        )
        users["local"] = {
            "id": record.id,
            "username": record.username,
            "password_hash": record.password_hash,
            "created_at": record.created_at,
            "is_admin": True,
        }
        data["users"] = users
        self._save(data)
        return record

    def _default_team_id_for_user(self, user_id: str) -> str:
        data = self._load()
        row = (data.get("teams") or {}).get(DEFAULT_PROJECT_TEAM_ID)
        if row is None or str(row.get("owner_id") or "") == user_id:
            return DEFAULT_PROJECT_TEAM_ID
        safe = re.sub(r"[^a-z0-9-]", "", user_id.lower())[:16] or "user"
        return f"{DEFAULT_PROJECT_TEAM_ID}-{safe}"

    def ensure_default_kanban_team(self, user_id: str) -> TeamRecord:
        """Create default project with Trello-style columns (one board per user).

        If the user already has any teams at all, skip creation — their
        existing teams are sufficient.
        """
        teams = self.list_teams(user_id)
        # If user already has teams, use the first one (no auto-creation)
        if teams:
            first = teams[0]
            self._ensure_team_kanban(
                first.id,
                seed_tasks=False,
                preset_id=None,
                trello_layout=True,
            )
            return first
        tid = self._default_team_id_for_user(user_id)
        return self.create_team(
            user_id,
            name=DEFAULT_PROJECT_TEAM_NAME,
            team_id=tid,
            description="Quadro inicial do gois (estilo Trello).",
            preset_id="squad-fullstack",
            seed_kanban=True,
            trello_layout=True,
        )

    def ensure_scheduling_team(self, user_id: str) -> "TeamRecord":
        """Ensure the 'Agendamentos' catch-all team exists for this user.

        If the user already has teams, skip auto-creation to avoid polluting
        their team list with an unwanted 'Agendamentos' entry.
        """
        teams = self.list_teams(user_id)
        if teams:
            # User already has teams — don't auto-create scheduling team.
            # Return first team as fallback (scheduling cards go to existing teams).
            return teams[0]
        tid = SCHEDULING_TEAM_ID
        data = self._load()
        row = (data.get("teams") or {}).get(tid)
        if row is not None and row.get("owner_id") == user_id:
            self._ensure_scheduling_kanban(tid)
            try:
                return self._team_from_row(row)
            except ValueError:
                pass  # fall through to re-create
        if row is not None and row.get("owner_id") != user_id:
            safe = re.sub(r"[^a-z0-9-]", "", user_id.lower())[:12] or "user"
            tid = f"{SCHEDULING_TEAM_ID}-{safe}"
            row2 = (data.get("teams") or {}).get(tid)
            if row2 is not None:
                self._ensure_scheduling_kanban(tid)
                try:
                    return self._team_from_row(row2)
                except ValueError:
                    pass  # fall through to re-create
        record = TeamRecord(
            id=tid,
            name=SCHEDULING_TEAM_NAME,
            owner_id=user_id,
            created_at=_now(),
            description=SCHEDULING_TEAM_DESCRIPTION,
            profile_slugs=[],
        )
        data["teams"][tid] = {
            "id": record.id,
            "name": record.name,
            "owner_id": record.owner_id,
            "created_at": record.created_at,
            "description": record.description,
            "project_source": None,
            "github_url": None,
            "local_path": None,
            "github_branch": "main",
            "profile_slugs": [],
        }
        self._save(data)
        self._ensure_scheduling_kanban(tid)
        return record

    def _ensure_scheduling_kanban(self, team_id: str) -> Path:
        path = self.team_kanban_path(team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            import yaml as _yaml
            default = {
                "columns": list(SCHEDULING_KANBAN_COLUMNS),
                "raias": list(SCHEDULING_KANBAN_COLUMNS),
                "tasks": [],
                "posts": [],
            }
            path.write_text(
                _yaml.safe_dump(default, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        return path

    def scheduling_team_kanban_path(self, team_id: str) -> Path:
        return self.team_kanban_path(team_id)

    def _seed_team_kanban_tasks(
        self, team_id: str, user_id: str, tasks: list[dict[str, Any]]
    ) -> None:
        if not tasks:
            return
        board = self.read_kanban(team_id, user_id)
        board["tasks"] = list(tasks)
        self.write_kanban(team_id, user_id, {"tasks": board["tasks"]})

    def find_user_by_username(self, username: str) -> Optional[UserRecord]:
        uname = normalize_username(username)
        for row in (self._load().get("users") or {}).values():
            if isinstance(row, dict) and row.get("username") == uname:
                return self._user_from_row(row)
        return None

    def get_user_by_id(self, user_id: str) -> Optional[UserRecord]:
        """Return a UserRecord by ID, or None if not found."""
        row = (self._load().get("users") or {}).get(user_id)
        if isinstance(row, dict):
            return self._user_from_row(row)
        return None

    def update_user_contact(self, user_id: str, email: str = "", whatsapp: str = "") -> UserRecord:
        """Update email and/or whatsapp for a user."""
        data = self._load()
        users = data.get("users") or {}
        row = users.get(user_id)
        if not isinstance(row, dict):
            raise ValueError("usuário não encontrado")
        if email is not None:
            row["email"] = email.strip()
        if whatsapp is not None:
            row["whatsapp"] = whatsapp.strip()
        self._save(data)
        return self._user_from_row(row)

    def _set_password(
        self,
        user_id: str,
        password: str,
        *,
        min_password_len: int = 6,
    ) -> UserRecord:
        if len(password) < min_password_len:
            raise ValueError(
                f"senha deve ter pelo menos {min_password_len} caracteres"
            )
        data = self._load()
        users = data.get("users") or {}
        row = users.get(user_id)
        if not isinstance(row, dict):
            raise ValueError("usuário não encontrado")
        row["password_hash"] = hash_password(password)
        self._save(data)
        return self._user_from_row(row)

    def ensure_default_admin(
        self,
        username: str = "admin",
        password: str = "admin",
        *,
        reset_password: bool = False,
    ) -> Optional[UserRecord]:
        """Create or optionally reset bootstrap admin (default admin/admin)."""
        existing = self.find_user_by_username(username)
        if existing is not None:
            if not reset_password:
                return None
            try:
                return self._set_password(
                    existing.id, password, min_password_len=1
                )
            except ValueError as e:
                log.warning("could not reset default admin password: %s", e)
                return None
        try:
            return self._create_user(
                username,
                password,
                is_admin=True,
                min_password_len=1,
            )
        except ValueError as e:
            log.warning("could not create default admin: %s", e)
            return None

    def admin_create_user(
        self,
        requester: "UserRecord",
        username: str,
        password: str,
        *,
        is_admin: bool = False,
    ) -> UserRecord:
        self._require_admin(requester)
        return self._create_user(username, password, is_admin=is_admin)

    def list_users(self, requester: "UserRecord") -> list[UserRecord]:
        self._require_admin(requester)
        data = self._load()
        out: list[UserRecord] = []
        for row in (data.get("users") or {}).values():
            if not isinstance(row, dict):
                continue
            out.append(self._user_from_row(row))
        out.sort(key=lambda u: u.created_at)
        return out

    def delete_user(self, requester: "UserRecord", user_id: str) -> None:
        self._require_admin(requester)
        if user_id == requester.id:
            raise ValueError("não é possível remover o próprio usuário admin")
        data = self._load()
        users = data.get("users") or {}
        if user_id not in users:
            raise ValueError("usuário não encontrado")
        admin_count = sum(
            1
            for row in users.values()
            if isinstance(row, dict) and bool(row.get("is_admin"))
        )
        target = users.get(user_id)
        if (
            isinstance(target, dict)
            and bool(target.get("is_admin"))
            and admin_count <= 1
        ):
            raise ValueError("não é possível remover o último admin")
        users.pop(user_id, None)
        sessions = data.get("sessions") or {}
        for tok in [
            t
            for t, row in sessions.items()
            if isinstance(row, dict) and row.get("user_id") == user_id
        ]:
            sessions.pop(tok, None)
        self._save(data)

    def set_admin(
        self, requester: "UserRecord", user_id: str, is_admin: bool
    ) -> UserRecord:
        self._require_admin(requester)
        data = self._load()
        users = data.get("users") or {}
        row = users.get(user_id)
        if not isinstance(row, dict):
            raise ValueError("usuário não encontrado")
        if not is_admin:
            admin_count = sum(
                1
                for r in users.values()
                if isinstance(r, dict) and bool(r.get("is_admin"))
            )
            if bool(row.get("is_admin")) and admin_count <= 1:
                raise ValueError("não é possível rebaixar o último admin")
        row["is_admin"] = bool(is_admin)
        self._save(data)
        return self._user_from_row(row)

    def change_password(
        self,
        requester: "UserRecord",
        user_id: str,
        new_password: str,
    ) -> None:
        if requester.id != user_id and not requester.is_admin:
            raise ValueError("acesso negado")
        if len(new_password) < 6:
            raise ValueError("senha deve ter pelo menos 6 caracteres")
        data = self._load()
        users = data.get("users") or {}
        row = users.get(user_id)
        if not isinstance(row, dict):
            raise ValueError("usuário não encontrado")
        row["password_hash"] = hash_password(new_password)
        self._save(data)

    def _require_admin(self, user: "UserRecord") -> None:
        if user is None or not user.is_admin:
            raise ValueError("acesso restrito a administradores")

    def _user_from_row(self, row: dict[str, Any]) -> UserRecord:
        return UserRecord(
            id=str(row["id"]),
            username=str(row["username"]),
            password_hash=str(row.get("password_hash") or ""),
            created_at=float(row.get("created_at") or 0),
            is_admin=bool(row.get("is_admin") or False),
            email=str(row.get("email") or ""),
            whatsapp=str(row.get("whatsapp") or ""),
        )

    def authenticate(self, username: str, password: str) -> UserRecord:
        uname = normalize_username(username.strip())
        password = password.strip()
        data = self._load()
        for row in (data.get("users") or {}).values():
            if not isinstance(row, dict):
                continue
            if row.get("username") != uname:
                continue
            if not verify_password(password, str(row.get("password_hash") or "")):
                raise ValueError("credenciais inválidas")
            return self._user_from_row(row)
        raise ValueError("credenciais inválidas")

    def create_session(self, user_id: str) -> SessionRecord:
        token = secrets.token_urlsafe(32)
        session = SessionRecord(
            token=token,
            user_id=user_id,
            expires_at=_now() + self.session_ttl_seconds,
        )
        data = self._load()
        data["sessions"][token] = {
            "token": session.token,
            "user_id": session.user_id,
            "expires_at": session.expires_at,
        }
        self._purge_expired_sessions(data)
        self._save(data)
        return session

    def destroy_session(self, token: str) -> None:
        if not token:
            return
        data = self._load()
        data["sessions"].pop(token, None)
        self._save(data)

    def _purge_expired_sessions(self, data: dict[str, Any]) -> None:
        now = _now()
        sessions = data.get("sessions") or {}
        expired = [
            tok
            for tok, row in sessions.items()
            if not isinstance(row, dict) or float(row.get("expires_at") or 0) <= now
        ]
        for tok in expired:
            sessions.pop(tok, None)

    def user_from_token(self, token: Optional[str]) -> Optional[UserRecord]:
        if not token:
            return None
        data = self._load()
        self._purge_expired_sessions(data)
        row = (data.get("sessions") or {}).get(token)
        if not isinstance(row, dict):
            return None
        if float(row.get("expires_at") or 0) <= _now():
            return None
        user_row = (data.get("users") or {}).get(str(row.get("user_id")))
        if not isinstance(user_row, dict):
            return None
        return self._user_from_row(user_row)

    def _team_from_row(self, row: dict[str, Any]) -> TeamRecord:
        profiles = row.get("profile_slugs") or []
        if not isinstance(profiles, list):
            profiles = []
        wa_numbers = row.get("whatsapp_numbers") or []
        if not isinstance(wa_numbers, list):
            wa_numbers = []
        member_ids_raw = row.get("member_ids") or []
        if not isinstance(member_ids_raw, list):
            member_ids_raw = []
        artifacts_dir_raw = row.get("artifacts_dir")
        artifacts_dir = str(artifacts_dir_raw).strip() if artifacts_dir_raw and str(artifacts_dir_raw).strip() else None
        app_url_raw = row.get("app_url")
        app_url = str(app_url_raw).strip() if app_url_raw and str(app_url_raw).strip() else None
        # Guard against incomplete rows (missing required keys)
        if "id" not in row:
            raise ValueError(
                f"registro de time incompleto (id={row.get('id')!r}, owner_id={row.get('owner_id')!r})"
            )
        if "owner_id" not in row or not row["owner_id"]:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "time %r sem owner_id — ignorando registro incompleto", row.get("id")
            )
            raise ValueError(
                f"registro de time incompleto (id={row.get('id')!r}, owner_id={row.get('owner_id')!r})"
            )
        return TeamRecord(
            id=str(row["id"]),
            name=str(row.get("name") or row["id"]),
            owner_id=str(row["owner_id"]),
            created_at=float(row.get("created_at") or 0),
            description=str(row.get("description") or ""),
            project_source=row.get("project_source"),
            github_url=row.get("github_url"),
            local_path=row.get("local_path"),
            github_branch=str(row.get("github_branch") or "main"),
            app_url=app_url,
            profile_slugs=[str(p) for p in profiles if str(p).strip()],
            whatsapp_numbers=[str(n) for n in wa_numbers if str(n).strip()],
            artifacts_dir=artifacts_dir,
            whatsapp_group=row.get("whatsapp_group") if isinstance(row.get("whatsapp_group"), dict) else None,
            member_ids=[str(m) for m in member_ids_raw if str(m).strip()],
            github_repos=[r for r in (row.get("github_repos") or []) if isinstance(r, dict)],
            site_links=[r for r in (row.get("site_links") or []) if isinstance(r, dict)],
            notify_emails=[str(e).strip() for e in (row.get("notify_emails") or []) if str(e).strip()],
            linked_emails=[
                str(e).strip().lower()
                for e in (row.get("linked_emails") or [])
                if str(e).strip()
            ],
            contacts=[c for c in (row.get("contacts") or []) if isinstance(c, dict)],
            latex_workspace_id=str(row["latex_workspace_id"]).strip() or None if row.get("latex_workspace_id") else None,
            latex_workspace_ids=latex_workspace_ids_from_row(row),
            swarm_name=str(row["swarm_name"]).strip() or None if row.get("swarm_name") else None,
            github_autopush=bool(row.get("github_autopush", False)),
        )

    def create_team(
        self,
        user_id: str,
        *,
        name: str = "",
        team_id: Optional[str] = None,
        description: str = "",
        project_source: Optional[str] = None,
        github_url: Optional[str] = None,
        local_path: Optional[str] = None,
        github_branch: str = "main",
        app_url: Optional[str] = None,
        site_links: Optional[list[Any]] = None,
        profile_slugs: Optional[list[str]] = None,
        preset_id: Optional[str] = None,
        seed_kanban: bool = False,
        trello_layout: bool = False,
        artifacts_dir: Optional[str] = None,
    ) -> TeamRecord:
        preset = get_team_preset(preset_id) if preset_id else None
        title = name.strip() or (
            str(preset.get("name") or "").strip() if preset else ""
        )
        if not title:
            raise ValueError("nome do time é obrigatório")
        tid = normalize_team_id(team_id or (preset.get("id") if preset else None))
        desc = description.strip() or (
            str(preset.get("description") or "").strip() if preset else ""
        )
        slugs: list[str] = []
        if profile_slugs:
            slugs = [str(s).strip() for s in profile_slugs if str(s).strip()]
        elif preset:
            slugs = [
                str(s).strip()
                for s in (preset.get("profile_slugs") or [])
                if str(s).strip()
            ]
        data = self._load()
        teams: dict[str, Any] = data["teams"]

        # --- Deduplicação: nome idêntico ou muito parecido → reutiliza ou bloqueia ---
        user_teams = [
            t for t in teams.values()
            if isinstance(t, dict) and t.get("owner_id") == user_id
        ]
        all_teams = [t for t in teams.values() if isinstance(t, dict)]
        exact = find_team_by_name_key(title, all_teams)
        if exact is not None:
            if str(exact.get("owner_id") or "") != user_id:
                raise ValueError("já existe um time com este nome")
            similar = exact
        else:
            similar = find_similar_team(title, user_teams)
        if similar is not None:
            existing_id = str(similar["id"])
            log.info(
                "create_team: nome '%s' similar a time existente '%s' (id=%s) — reusando",
                title, similar.get("name"), existing_id,
            )
            # Merge profile_slugs se tiver novos
            existing_slugs = list(similar.get("profile_slugs") or [])
            merged = False
            for s in slugs:
                if s and s not in existing_slugs:
                    existing_slugs.append(s)
                    merged = True
            if merged:
                teams[existing_id]["profile_slugs"] = existing_slugs
                self._save(data)
            record = self._team_from_row(teams[existing_id])
            self._finish_team_provision(
                record.id,
                record.name,
                seed_kanban=seed_kanban,
                preset_id=preset_id,
                trello_layout=trello_layout or is_default_project_team_id(record.id),
            )
            return record
        # --- Fim deduplicação ---

        if tid in teams:
            raise ValueError("time com este id já existe")
        parsed_site_links: list[dict[str, str]] = []
        if site_links:
            for entry in site_links:
                parsed = _parse_site_link_entry(entry)
                if parsed and not any(s.get("url") == parsed["url"] for s in parsed_site_links):
                    parsed_site_links.append(parsed)
        record = TeamRecord(
            id=tid,
            name=title,
            owner_id=user_id,
            created_at=_now(),
            description=desc,
            project_source=project_source,
            github_url=github_url,
            local_path=local_path,
            github_branch=github_branch.strip() or "main",
            app_url=str(app_url).strip() if app_url and str(app_url).strip() else None,
            profile_slugs=slugs,
            artifacts_dir=str(artifacts_dir).strip() if artifacts_dir and str(artifacts_dir).strip() else None,
            site_links=parsed_site_links,
        )
        teams[tid] = {
            "id": record.id,
            "name": record.name,
            "owner_id": record.owner_id,
            "created_at": record.created_at,
            "description": record.description,
            "project_source": record.project_source,
            "github_url": record.github_url,
            "local_path": record.local_path,
            "github_branch": record.github_branch,
            "app_url": record.app_url,
            "profile_slugs": list(slugs),
            "whatsapp_numbers": [],
            "artifacts_dir": record.artifacts_dir,
            "member_ids": [],
            "github_repos": [],
            "site_links": list(parsed_site_links),
        }
        self._save(data)
        use_trello = trello_layout or is_default_project_team_id(tid)
        self._finish_team_provision(
            tid,
            title,
            seed_kanban=seed_kanban,
            preset_id=preset_id,
            trello_layout=use_trello,
        )
        if use_trello and is_default_project_team_id(tid) and seed_kanban:
            board = self.read_kanban(tid, user_id)
            if not board.get("tasks"):
                self._seed_team_kanban_tasks(
                    tid, user_id, default_project_starter_tasks()
                )
        return record

    def list_teams(self, user_id: str) -> list[TeamRecord]:
        data = self._load()
        out: list[TeamRecord] = []
        for row in (data.get("teams") or {}).values():
            if isinstance(row, dict):
                if row.get("owner_id") == user_id or user_id in (row.get("member_ids") or []):
                    try:
                        out.append(self._team_from_row(row))
                    except ValueError:
                        continue
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out

    def list_all_teams(self, *, fresh: bool = False) -> list[TeamRecord]:
        """Return every team across all owners (admin/global views)."""
        del fresh  # only AccountStoreMongo uses fresh reads
        data = self._load()
        out: list[TeamRecord] = []
        for row in (data.get("teams") or {}).values():
            if isinstance(row, dict):
                try:
                    out.append(self._team_from_row(row))
                except ValueError:
                    continue
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out

    def get_team(self, team_id: str, user_id: str) -> TeamRecord:
        from .accounts_team_guard import resolve_team_identifier

        raw = str(team_id or "").strip()
        team_id = resolve_team_identifier(self.data_dir, raw, store=self)
        data = self._load()
        teams = data.get("teams") or {}
        row = teams.get(team_id)
        # Fallback: partial name/id match for legacy callers
        if not isinstance(row, dict):
            team_id_lower = team_id.lower().strip()
            for tid, t in teams.items():
                if not isinstance(t, dict):
                    continue
                t_name = (t.get("name") or "").lower().strip()
                t_swarm = (t.get("swarm_name") or "").lower().strip()
                tid_lower = tid.lower()
                if (
                    t_name == team_id_lower
                    or tid_lower == team_id_lower
                    or (t_swarm and team_id_lower in t_swarm)
                    or team_id_lower in t_name
                ):
                    row = t
                    team_id = tid
                    break
        if not isinstance(row, dict):
            raise ValueError("time não encontrado")
        if row.get("owner_id") != user_id and user_id not in (row.get("member_ids") or []):
            user = self.get_user_by_id(user_id)
            if user is None or not user.is_admin:
                raise ValueError("acesso negado a este time")
        return self._team_from_row(row)

    def update_team(self, team_id: str, user_id: str, fields: dict[str, Any]) -> TeamRecord:
        team = self.get_team(team_id, user_id)
        if team.owner_id != user_id:
            actor = self.get_user_by_id(user_id)
            if actor is None or not actor.is_admin:
                raise ValueError("apenas o dono do time pode editar configurações")
        data = self._load()
        row = data["teams"][team.id]
        if "name" in fields and str(fields["name"]).strip():
            new_name = str(fields["name"]).strip()
            name_key = team_name_key(new_name)
            if name_key:
                for tid, candidate in (data.get("teams") or {}).items():
                    if tid == team.id or not isinstance(candidate, dict):
                        continue
                    if team_name_key(str(candidate.get("name") or "")) == name_key:
                        raise ValueError("já existe um time com este nome")
            row["name"] = new_name
        if "description" in fields:
            row["description"] = str(fields["description"] or "").strip()
        for key in ("project_source", "github_url", "local_path", "github_branch", "app_url"):
            if key in fields:
                val = fields[key]
                row[key] = str(val).strip() if val is not None and str(val).strip() else None
        source = str(row.get("project_source") or "").strip().lower() or None
        if source is not None and source not in {"local", "github"}:
            raise ValueError('project_source deve ser "local" ou "github"')
        if source == "local" and not str(row.get("local_path") or "").strip():
            raise ValueError("local_path é obrigatório para project_source=local")
        if source == "github":
            if not str(row.get("github_url") or "").strip():
                raise ValueError("github_url é obrigatório para project_source=github")
            if not str(row.get("github_branch") or "").strip():
                row["github_branch"] = "main"
        if source in {"local", "github"}:
            row["project_source"] = source
        if "whatsapp_numbers" in fields:
            from .whatsapp_allowlist import normalize_whatsapp_recipient
            raw_nums = fields["whatsapp_numbers"]
            if isinstance(raw_nums, list):
                cleaned = []
                for n in raw_nums:
                    norm = normalize_whatsapp_recipient(n)
                    if norm and norm not in cleaned:
                        cleaned.append(norm)
                row["whatsapp_numbers"] = cleaned
        if "artifacts_dir" in fields:
            val = fields["artifacts_dir"]
            row["artifacts_dir"] = str(val).strip() if val is not None and str(val).strip() else None
        if "profile_slugs" in fields:
            raw_profiles = fields["profile_slugs"]
            if isinstance(raw_profiles, list):
                cleaned = [str(s).strip() for s in raw_profiles if str(s).strip()]
                row["profile_slugs"] = cleaned
        if "swarm_name" in fields:
            raw_swarm = fields["swarm_name"]
            if raw_swarm is None or raw_swarm == "":
                row.pop("swarm_name", None)
            else:
                row["swarm_name"] = str(raw_swarm).strip()
        if "whatsapp_group" in fields:
            val = fields["whatsapp_group"]
            if val is None or val == "":
                row.pop("whatsapp_group", None)
            elif isinstance(val, dict):
                # Validate group_jid format
                gid = str(val.get("group_jid") or "").strip()
                if gid and not gid.endswith("@g.us"):
                    raise ValueError("whatsapp_group.group_jid deve terminar com @g.us")
                if gid:
                    row["whatsapp_group"] = {
                        "group_jid": gid,
                        "enabled": bool(val.get("enabled", True)),
                        "notify_emails": [
                            str(e).strip() for e in (val.get("notify_emails") or [])
                            if str(e).strip()
                        ],
                        "auto_create_cards": bool(val.get("auto_create_cards", True)),
                        "auto_complete_cards": bool(val.get("auto_complete_cards", True)),
                        "echo_to_group": bool(val.get("echo_to_group", True)),
                        "smtp_host": str(val.get("smtp_host") or "smtp.gmail.com"),
                        "smtp_port": int(val.get("smtp_port") or 587),
                        "smtp_user": str(val.get("smtp_user") or ""),
                        "smtp_password_env": str(val.get("smtp_password_env") or "TEAM_SMTP_PASSWORD"),
                        "smtp_from": str(val.get("smtp_from") or ""),
                    }
                else:
                    row.pop("whatsapp_group", None)
        # --- github_repos: lista de repositórios associados ---
        if "github_repos" in fields:
            val = fields["github_repos"]
            if isinstance(val, list):
                repos: list[dict[str, Any]] = []
                for entry in val:
                    if isinstance(entry, dict):
                        url = str(entry.get("url") or "").strip()
                        if not url:
                            continue
                        repos.append({
                            "url": url,
                            "name": str(entry.get("name") or url.rstrip("/").split("/")[-1]).strip(),
                            "branch": str(entry.get("branch") or "main").strip(),
                            "description": str(entry.get("description") or "").strip(),
                        })
                    elif isinstance(entry, str) and entry.strip():
                        url = entry.strip()
                        repos.append({
                            "url": url,
                            "name": url.rstrip("/").split("/")[-1],
                            "branch": "main",
                            "description": "",
                        })
                row["github_repos"] = repos
        if "add_github_repo" in fields:
            entry = fields["add_github_repo"]
            existing = list(row.get("github_repos") or [])
            if isinstance(entry, dict):
                url = str(entry.get("url") or "").strip()
                if url and not any(r.get("url") == url for r in existing):
                    existing.append({
                        "url": url,
                        "name": str(entry.get("name") or url.rstrip("/").split("/")[-1]).strip(),
                        "branch": str(entry.get("branch") or "main").strip(),
                        "description": str(entry.get("description") or "").strip(),
                    })
            elif isinstance(entry, str) and entry.strip():
                url = entry.strip()
                if not any(r.get("url") == url for r in existing):
                    existing.append({
                        "url": url,
                        "name": url.rstrip("/").split("/")[-1],
                        "branch": "main",
                        "description": "",
                    })
            row["github_repos"] = existing
        if "remove_github_repo" in fields:
            url_to_remove = str(fields["remove_github_repo"] or "").strip()
            existing = list(row.get("github_repos") or [])
            row["github_repos"] = [r for r in existing if r.get("url") != url_to_remove]
        if "site_links" in fields:
            val = fields["site_links"]
            if isinstance(val, list):
                links: list[dict[str, str]] = []
                for entry in val:
                    parsed = _parse_site_link_entry(entry)
                    if parsed and not any(l.get("url") == parsed["url"] for l in links):
                        links.append(parsed)
                row["site_links"] = links
            elif val is None:
                row["site_links"] = []
        if "add_site_link" in fields:
            parsed = _parse_site_link_entry(fields["add_site_link"])
            existing = list(row.get("site_links") or [])
            if parsed and not any(l.get("url") == parsed["url"] for l in existing):
                existing.append(parsed)
            row["site_links"] = existing
        if "remove_site_link" in fields:
            url_to_remove = _normalize_site_url(str(fields["remove_site_link"] or ""))
            existing = list(row.get("site_links") or [])
            row["site_links"] = [
                l for l in existing if _normalize_site_url(str(l.get("url") or "")) != url_to_remove
            ]
        if "github_autopush" in fields:
            row["github_autopush"] = bool(fields["github_autopush"])
        if "notify_emails" in fields:
            raw_emails = fields["notify_emails"]
            if isinstance(raw_emails, list):
                row["notify_emails"] = [str(e).strip() for e in raw_emails if str(e).strip()]
            elif raw_emails is None or raw_emails == "":
                row["notify_emails"] = []
        if "linked_emails" in fields:
            raw_emails = fields["linked_emails"]
            if isinstance(raw_emails, list):
                clean: list[str] = []
                for e in raw_emails:
                    e_str = str(e).strip().lower()
                    if e_str and "@" in e_str:
                        if e_str not in clean:
                            clean.append(e_str)
                    elif e_str:
                        raise ValueError(f"Email inválido: {e_str}")
                row["linked_emails"] = clean
            elif raw_emails is None or raw_emails == "":
                row["linked_emails"] = []
        if "contacts" in fields:
            raw = fields["contacts"]
            if isinstance(raw, list):
                row["contacts"] = [c for c in raw if isinstance(c, dict)]
            elif raw is None:
                row["contacts"] = []
        if "upsert_contact" in fields:
            contact = fields["upsert_contact"]
            if isinstance(contact, dict):
                existing = list(row.get("contacts") or [])
                uid = str(contact.get("id") or contact.get("email") or "").strip()
                idx = next(
                    (i for i, c in enumerate(existing) if
                     (uid and (c.get("id") == uid or c.get("email") == uid))),
                    None,
                )
                entry: dict[str, Any] = {
                    k: str(v).strip() for k, v in contact.items()
                    if v is not None and str(v).strip()
                    and k not in {"add_to_allowlist"}
                }
                if not entry.get("id"):
                    import uuid as _uuid
                    entry["id"] = _uuid.uuid4().hex[:12]
                if idx is not None:
                    existing[idx] = {**existing[idx], **entry}
                else:
                    existing.append(entry)
                row["contacts"] = existing
                wa_dest = str(
                    contact.get("whatsapp") or contact.get("phone") or ""
                ).strip()
                if wa_dest:
                    from .whatsapp_allowlist import normalize_whatsapp_recipient

                    if wa_dest.lower().endswith("@g.us"):
                        stored = wa_dest.lower()
                    else:
                        norm = normalize_whatsapp_recipient(wa_dest)
                        stored = norm if norm else ""
                    if stored:
                        wa_nums = list(row.get("whatsapp_numbers") or [])
                        existing = {
                            str(n).strip().lower()
                            for n in wa_nums
                            if str(n).strip()
                        }
                        if stored not in existing:
                            wa_nums.append(stored)
                            row["whatsapp_numbers"] = wa_nums
                if bool(fields.get("add_to_allowlist")) and wa_dest:
                    self._sync_dest_to_allowlist(
                        wa_dest, str(row.get("name") or team_id)
                    )
        if "remove_contact" in fields:
            uid = str(fields["remove_contact"] or "").strip()
            existing = list(row.get("contacts") or [])
            row["contacts"] = [
                c for c in existing
                if c.get("id") != uid and c.get("email") != uid
            ]
        if "latex_workspace_ids" in fields:
            val = fields["latex_workspace_ids"]
            if val is None:
                sync_latex_workspace_fields(row, [])
            elif isinstance(val, list):
                sync_latex_workspace_fields(row, val)
            else:
                raise ValueError("latex_workspace_ids deve ser uma lista")
        if "latex_workspace_id" in fields:
            val = fields["latex_workspace_id"]
            if val and str(val).strip():
                ws_id = str(val).strip()
                ids = latex_workspace_ids_from_row(row)
                ids = [ws_id] + [x for x in ids if x != ws_id]
                sync_latex_workspace_fields(row, ids)
            else:
                sync_latex_workspace_fields(row, [])
        if "add_latex_workspace_id" in fields:
            ws_id = str(fields["add_latex_workspace_id"] or "").strip()
            if ws_id:
                ids = latex_workspace_ids_from_row(row)
                if ws_id not in ids:
                    ids.append(ws_id)
                sync_latex_workspace_fields(row, ids)
        if "remove_latex_workspace_id" in fields:
            ws_id = str(fields["remove_latex_workspace_id"] or "").strip()
            if ws_id:
                ids = [x for x in latex_workspace_ids_from_row(row) if x != ws_id]
                sync_latex_workspace_fields(row, ids)
        if fields.get("create_local_path") and str(row.get("local_path") or "").strip():
            self._ensure_local_project_path(str(row["local_path"]).strip())
        self._save(data)
        team = self._team_from_row(row)
        # Auto-sync whatsapp_numbers to send allowlist
        if "whatsapp_numbers" in fields:
            wa_nums = list(row.get("whatsapp_numbers") or [])
            if wa_nums:
                self._sync_numbers_to_allowlist(wa_nums, team.name)
        if "whatsapp_group" in fields:
            wg = row.get("whatsapp_group")
            if isinstance(wg, dict) and wg.get("enabled", True):
                self._sync_dest_to_allowlist(str(wg.get("group_jid") or ""), team.name)
        return team

    def delete_team(self, team_id: str, user_id: str) -> None:
        """Remove a team permanently. Only the owner can delete."""
        data = self._load()
        row = (data.get("teams") or {}).get(team_id)
        if not isinstance(row, dict):
            raise ValueError("time não encontrado")
        if row.get("owner_id") != user_id:
            raise ValueError("apenas o dono do time pode excluí-lo")
        teams = data.get("teams") or {}
        teams.pop(team_id, None)
        data["teams"] = teams
        self._save(data)

    def add_team_profile(self, team_id: str, user_id: str, profile_slug: str) -> TeamRecord:
        slug = profile_slug.strip()
        if not slug:
            raise ValueError("profile slug vazio")
        data = self._load()
        team = self.get_team(team_id, user_id)
        row = data["teams"][team.id]
        profiles = list(team.profile_slugs)
        if slug not in profiles:
            profiles.append(slug)
        row["profile_slugs"] = profiles
        self._save(data)
        return self._team_from_row(row)

    def remove_profile_from_all_teams(self, profile_slug: str) -> list[str]:
        """Unlink a Hermes profile slug from every team (admin/system cleanup)."""
        slug = str(profile_slug or "").strip()
        if not slug:
            return []
        removed: list[str] = []
        data = self._load()
        for tid, row in (data.get("teams") or {}).items():
            if not isinstance(row, dict):
                continue
            profiles = list(row.get("profile_slugs") or [])
            if slug not in profiles:
                continue
            row["profile_slugs"] = [p for p in profiles if p != slug]
            removed.append(str(tid))
        if removed:
            self._save(data)
        return removed

    # --- Team members ---

    def add_team_member(self, team_id: str, owner_id: str, member_user_id: str) -> TeamRecord:
        """Add a user as member of a team. Only the owner can add members."""
        data = self._load()
        row = (data.get("teams") or {}).get(team_id)
        if not isinstance(row, dict):
            raise ValueError("time não encontrado")
        if row.get("owner_id") != owner_id:
            raise ValueError("apenas o dono do time pode adicionar membros")
        member_uid = str(member_user_id).strip()
        if not member_uid:
            raise ValueError("user_id do membro é obrigatório")
        members = list(row.get("member_ids") or [])
        if member_uid not in members:
            members.append(member_uid)
        row["member_ids"] = members
        self._save(data)
        return self._team_from_row(row)

    def remove_team_member(self, team_id: str, owner_id: str, member_user_id: str) -> TeamRecord:
        """Remove a member from a team. Only the owner can remove members."""
        data = self._load()
        row = (data.get("teams") or {}).get(team_id)
        if not isinstance(row, dict):
            raise ValueError("time não encontrado")
        if row.get("owner_id") != owner_id:
            raise ValueError("apenas o dono do time pode remover membros")
        member_uid = str(member_user_id).strip()
        members = [m for m in (row.get("member_ids") or []) if m != member_uid]
        row["member_ids"] = members
        self._save(data)
        return self._team_from_row(row)

    def list_team_members(self, team_id: str, user_id: str) -> list[str]:
        """List all member user IDs (includes owner). Requires team access."""
        team = self.get_team(team_id, user_id)
        all_members = [team.owner_id] + [m for m in team.member_ids if m != team.owner_id]
        return all_members

    # --- WhatsApp numbers per team ---

    @staticmethod
    def _default_whatsapp_group_dict(group_jid: str) -> dict[str, Any]:
        from .whatsapp_allowlist import normalize_group_jid

        return {
            "group_jid": normalize_group_jid(group_jid),
            "enabled": True,
            "notify_emails": [],
            "auto_create_cards": True,
            "auto_complete_cards": True,
            "echo_to_group": True,
        }

    def _ensure_whatsapp_group_link(self, row: dict[str, Any]) -> bool:
        """Link ``whatsapp_group`` from the first group JID in ``whatsapp_numbers``."""
        from .whatsapp_allowlist import normalize_group_jid

        wg = row.get("whatsapp_group")
        if isinstance(wg, dict) and normalize_group_jid(wg.get("group_jid")):
            return False
        for raw in row.get("whatsapp_numbers") or []:
            jid = normalize_group_jid(raw)
            if jid:
                row["whatsapp_group"] = self._default_whatsapp_group_dict(jid)
                return True
        return False

    def ensure_team_whatsapp_group_link(
        self,
        team_id: str,
        user_id: str,
        group_jid: str,
    ) -> TeamRecord:
        """Persist ``whatsapp_group`` when only ``whatsapp_numbers`` had the JID."""
        from .whatsapp_allowlist import normalize_group_jid

        jid = normalize_group_jid(group_jid)
        if not jid:
            raise ValueError("group_jid inválido")
        team = self.get_team(team_id, user_id)
        data = self._load()
        row = data["teams"][team.id]
        current = row.get("whatsapp_group")
        if isinstance(current, dict) and normalize_group_jid(current.get("group_jid")):
            return self._team_from_row(row)
        row["whatsapp_group"] = self._default_whatsapp_group_dict(jid)
        self._save(data)
        team = self._team_from_row(row)
        self._sync_dest_to_allowlist(jid, team.name)
        return team

    def _sync_dest_to_allowlist(self, value: str, team_name: str) -> None:
        """Best-effort sync of a team WhatsApp destination (DM or group) to allowlist."""
        text = str(value or "").strip()
        if not text:
            return
        try:
            from .storage import get_allowlist_store

            store = get_allowlist_store()
            if text.lower().endswith("@g.us"):
                if text.lower() in store.get_enabled_group_jids():
                    return
                store.add(
                    name=f"[time:{team_name}] grupo",
                    phone=text,
                    label=f"auto:team:{team_name}",
                    enabled=True,
                )
                return
            from .whatsapp_allowlist import normalize_whatsapp_recipient

            digits = normalize_whatsapp_recipient(text)
            if digits and digits not in store.get_enabled_digits():
                store.add(
                    name=f"[time:{team_name}] {digits}",
                    phone=digits,
                    label=f"auto:team:{team_name}",
                    enabled=True,
                )
        except ValueError:
            pass
        except Exception:
            log.debug(
                "falha ao sincronizar destino WhatsApp do time com allowlist",
                exc_info=True,
            )

    def _sync_numbers_to_allowlist(self, numbers: list[str], team_name: str) -> None:
        """Auto-add team numbers/groups to the global WhatsApp send allowlist."""
        for raw in numbers:
            self._sync_dest_to_allowlist(str(raw), team_name)

    def set_team_whatsapp_numbers(
        self, team_id: str, user_id: str, numbers: list[str]
    ) -> TeamRecord:
        """Replace the entire list of WhatsApp numbers for a team."""
        from .whatsapp_allowlist import normalize_whatsapp_recipient

        team = self.get_team(team_id, user_id)
        data = self._load()
        row = data["teams"][team.id]
        cleaned: list[str] = []
        for raw in numbers:
            raw_str = str(raw).strip()
            if raw_str.lower().endswith("@g.us"):
                if raw_str not in cleaned:
                    cleaned.append(raw_str)
                continue
            norm = normalize_whatsapp_recipient(raw)
            if not norm:
                continue
            if norm not in cleaned:
                cleaned.append(norm)
        row["whatsapp_numbers"] = cleaned
        self._ensure_whatsapp_group_link(row)
        self._save(data)
        team = self._team_from_row(row)
        self._sync_numbers_to_allowlist(cleaned, team.name)
        return team

    def add_team_whatsapp_number(
        self, team_id: str, user_id: str, number: str
    ) -> TeamRecord:
        """Add a single WhatsApp number or group JID to the team's list."""
        from .whatsapp_allowlist import normalize_whatsapp_recipient

        raw = str(number or "").strip()
        if not raw:
            raise ValueError("número de WhatsApp inválido")

        if raw.lower().endswith("@g.us"):
            stored = raw.lower()
        else:
            norm = normalize_whatsapp_recipient(raw)
            if not norm:
                raise ValueError("número de WhatsApp inválido")
            stored = norm

        team = self.get_team(team_id, user_id)
        data = self._load()
        row = data["teams"][team.id]
        existing = list(row.get("whatsapp_numbers") or [])
        if stored not in [str(n).strip().lower() for n in existing]:
            existing.append(stored)
        row["whatsapp_numbers"] = existing
        self._ensure_whatsapp_group_link(row)
        self._save(data)
        team = self._team_from_row(row)
        self._sync_dest_to_allowlist(stored, team.name)
        return team

    def remove_team_whatsapp_number(
        self, team_id: str, user_id: str, number: str
    ) -> TeamRecord:
        """Remove a WhatsApp DM or group JID from the team's allowlist sources."""
        from .whatsapp_allowlist import normalize_group_jid, normalize_whatsapp_recipient
        from .whatsapp_inbound import _digits_equivalent

        raw = str(number or "").strip()
        if not raw:
            raise ValueError("número de WhatsApp inválido")

        group_jid = normalize_group_jid(raw)
        is_group = bool(group_jid) or raw.lower().endswith("@g.us")
        if is_group:
            target_jid = group_jid or raw.lower()
        else:
            target_jid = normalize_whatsapp_recipient(raw)
            if not target_jid:
                raise ValueError("número de WhatsApp inválido")

        team = self.get_team(team_id, user_id)
        data = self._load()
        row = data["teams"][team.id]
        existing = list(row.get("whatsapp_numbers") or [])

        def _stored_matches(stored: Any) -> bool:
            text = str(stored or "").strip()
            if not text:
                return False
            if is_group:
                return (normalize_group_jid(text) or text.lower()) == target_jid
            digits = normalize_whatsapp_recipient(text)
            if not digits:
                return False
            return digits == target_jid or _digits_equivalent(digits, target_jid)

        def _contact_field_matches(value: Any) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            if is_group:
                return (normalize_group_jid(text) or text.lower()) == target_jid
            digits = normalize_whatsapp_recipient(text)
            if not digits:
                return False
            return digits == target_jid or _digits_equivalent(digits, target_jid)

        row["whatsapp_numbers"] = [n for n in existing if not _stored_matches(n)]

        if is_group:
            wg = row.get("whatsapp_group")
            if isinstance(wg, dict):
                configured = normalize_group_jid(wg.get("group_jid"))
                if configured == target_jid:
                    row["whatsapp_group"] = None

        updated_contacts: list[Any] = []
        for contact in row.get("contacts") or []:
            if not isinstance(contact, dict):
                updated_contacts.append(contact)
                continue
            cleaned = dict(contact)
            for key in ("whatsapp", "phone", "jid"):
                if _contact_field_matches(cleaned.get(key)):
                    cleaned.pop(key, None)
            updated_contacts.append(cleaned)
        row["contacts"] = updated_contacts

        self._save(data)
        return self._team_from_row(row)

    def find_team_by_whatsapp_number(self, sender_digits: str) -> Optional[TeamRecord]:
        """Return the first team whose whatsapp_numbers contains sender_digits.

        Uses the same Brazilian mobile variant matching as whatsapp_inbound so
        numbers with/without the extra 9 are treated as equivalent.
        """
        from .whatsapp_inbound import _digits_equivalent

        if not sender_digits:
            return None
        for team in self.list_all_teams():
            for num in team.whatsapp_numbers:
                num_digits = re.sub(r"\D", "", str(num))
                if _digits_equivalent(sender_digits, num_digits):
                    return team
        return None

    def team_dir(self, team_id: str) -> Path:
        return self.data_dir / "teams" / team_id

    def team_kanban_path(self, team_id: str) -> Path:
        return self.team_dir(team_id) / "kanban.yaml"

    def team_repo_dir(self, team_id: str) -> Path:
        return self.team_dir(team_id) / "repo"

    def team_workdir(self, team: TeamRecord) -> Path:
        if team.project_source == "local" and team.local_path:
            return Path(team.local_path).expanduser().resolve()
        if team.project_source == "github":
            repo = self.team_repo_dir(team.id)
            if repo.is_dir():
                return repo
            if team.local_path:
                local = Path(team.local_path).expanduser().resolve()
                if local.is_dir():
                    return local
            return self.team_dir(team.id)
        return self.team_dir(team.id)

    def team_artifacts_dir(self, team: TeamRecord) -> Path:
        """Return the configured artifacts directory for a team.

        Falls back to the team workspace's artifacts/ subdirectory if not set.
        """
        if team.artifacts_dir and str(team.artifacts_dir).strip():
            p = Path(team.artifacts_dir).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        # Fallback: workspace/artifacts
        ws_artifacts = self.team_dir(team.id) / "workspace" / "artifacts"
        ws_artifacts.mkdir(parents=True, exist_ok=True)
        return ws_artifacts

    def _ensure_team_workspace(self, team_id: str) -> None:
        """Garante que o workspace (diretório de trabalho) do time existe."""
        teams_base = self.data_dir / "teams"
        try:
            ensure_team_workspace(teams_base, team_id)
        except Exception as e:
            log.warning("falha ao criar workspace do time %s: %s", team_id, e)

    def _ensure_local_project_path(self, local_path: str) -> Path:
        """Create external project folder and seed kanban.yaml when missing."""
        path = Path(local_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        kanban_path = path / "kanban.yaml"
        if not kanban_path.is_file():
            default = {
                "columns": list(DEFAULT_KANBAN_COLUMNS),
                "raias": list(DEFAULT_KANBAN_COLUMNS),
                "tasks": [],
            }
            kanban_path.write_text(
                yaml.safe_dump(default, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        return path

    def _finish_team_provision(
        self,
        team_id: str,
        team_name: str,
        *,
        seed_kanban: bool = False,
        preset_id: Optional[str] = None,
        trello_layout: bool = False,
    ) -> dict[str, Any]:
        """Ensure kanban, workspace and on-disk markers exist for a team."""
        use_trello = trello_layout or is_default_project_team_id(team_id)
        self._ensure_team_kanban(
            team_id,
            seed_tasks=seed_kanban and (bool(preset_id) or use_trello),
            preset_id=preset_id,
            trello_layout=use_trello,
        )
        self._ensure_team_workspace(team_id)
        disk = provision_team_disk_artifacts(self.data_dir, team_id, team_name)
        return {"kanban": True, "workspace": True, **disk}

    def _ensure_team_kanban(
        self,
        team_id: str,
        *,
        seed_tasks: bool = False,
        preset_id: Optional[str] = None,
        trello_layout: bool = False,
    ) -> Path:
        from .accounts_team_guard import resolve_team_id_alias

        team_id = resolve_team_id_alias(self.data_dir, str(team_id or "").strip())
        path = self.team_kanban_path(team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = (
            list(TRELLO_KANBAN_COLUMNS)
            if trello_layout or is_default_project_team_id(team_id)
            else list(DEFAULT_KANBAN_COLUMNS)
        )
        tasks: list[dict[str, Any]] = []
        if seed_tasks and is_default_project_team_id(team_id):
            tasks = default_project_starter_tasks()
        elif seed_tasks and preset_id:
            try:
                tasks = starter_kanban_tasks(preset_id)
            except ValueError:
                tasks = []

        from .kanban_mongo import ensure_team_board, mongo_kanban_enabled

        if mongo_kanban_enabled():
            ensure_team_board(
                scope=str(self.data_dir),
                team_id=team_id,
                columns=columns,
                tasks=tasks,
                fallback_path=path if path.is_file() else None,
            )
            return path

        if not path.is_file():
            default = {
                "columns": columns,
                "raias": columns,
                "tasks": tasks,
            }
            path.write_text(
                yaml.safe_dump(default, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        return path


    def read_kanban(self, team_id: str, user_id: str) -> dict[str, Any]:
        import time

        cache_key = f"{team_id}:{user_id}"
        now = time.time()
        kanban_cache = self._kanban_cache_bucket()

        # Check cache (valid for 5 seconds)
        if cache_key in kanban_cache:
            cached_ts, cached_data = kanban_cache[cache_key]
            if now - cached_ts < 5.0:
                return dict(cached_data)

        raw_team_id = str(team_id or "").strip()
        team = self.get_team(team_id, user_id)
        team_id = team.id
        path = self._ensure_team_kanban(team_id)
        from .kanban_mongo import load_team_board, mongo_kanban_enabled

        if mongo_kanban_enabled():
            from .kanban_board_dedupe import reconcile_team_board_if_needed

            reconcile_team_board_if_needed(
                str(self.data_dir),
                team_id,
                fallback_path=path,
                raw_team_id=raw_team_id,
            )
        team_wd = str(self.team_dir(team_id))
        if mongo_kanban_enabled():
            mongo_board = load_team_board(
                scope=str(self.data_dir),
                team_id=team_id,
                fallback_path=path,
            )
            if mongo_board is not None:
                data = {
                    "columns": mongo_board.get("columns") or list(DEFAULT_KANBAN_COLUMNS),
                    "tasks": mongo_board.get("tasks") if isinstance(mongo_board.get("tasks"), list) else [],
                    "team_id": team_id,
                    "path": str(path),
                    "workdir": team_wd,
                }
                kanban_cache[cache_key] = (now, data)
                return data
            data = {
                "columns": list(DEFAULT_KANBAN_COLUMNS),
                "tasks": [],
                "team_id": team_id,
                "path": str(path),
                "workdir": team_wd,
            }
            kanban_cache[cache_key] = (now, data)
            return data

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not data.get("columns"):
            data["columns"] = list(DEFAULT_KANBAN_COLUMNS)
        if not isinstance(data.get("tasks"), list):
            data["tasks"] = []
        data["team_id"] = team_id
        data["path"] = str(path)
        data["workdir"] = str(self.team_dir(team_id))
        kanban_cache[cache_key] = (now, data)
        return data

    def write_kanban(self, team_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Invalidate cache for this team
        cache_key = f"{team_id}:{user_id}"
        self._kanban_cache_bucket().pop(cache_key, None)

        team = self.get_team(team_id, user_id)
        team_id = team.id
        columns = payload.get("columns")
        tasks = payload.get("tasks")
        if columns is not None and not isinstance(columns, list):
            raise ValueError("columns deve ser uma lista")
        if tasks is not None and not isinstance(tasks, list):
            raise ValueError("tasks deve ser uma lista")
        current = self.read_kanban(team_id, user_id)
        if columns is not None:
            current["columns"] = columns
        if tasks is not None:
            current["tasks"] = tasks
        from .kanban_mongo import mongo_kanban_enabled, save_team_board

        if mongo_kanban_enabled():
            save_team_board(
                scope=str(self.data_dir),
                team_id=team_id,
                columns=current["columns"],
                tasks=current["tasks"],
                source_path=str(self.team_kanban_path(team_id)),
            )
            return self.read_kanban(team_id, user_id)

        to_save = {"columns": current["columns"], "tasks": current["tasks"]}
        path = self._ensure_team_kanban(team_id)
        path.write_text(
            yaml.safe_dump(to_save, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return self.read_kanban(team_id, user_id)

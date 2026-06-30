"""Keep every team registered and protected in the accounts store.

On each monitor bootstrap, ``ensure_protected_teams``:

1. Applies explicit overrides from ``protected_teams.json`` (optional).
2. Scans ``teams/*`` and registers every non-test orphan with team artifacts.
3. Creates friendly symlinks when a team has a human name but a hash id.
4. Ensures ``local`` and ``admin`` can see auto-restored teams.

All registered teams and all non-test team directories on disk are protected
from hygiene deletion (only pytest-style test teams remain deletable).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_PROTECTED_FILENAME = "protected_teams.json"
_HASH_TEAM_ID_RE = re.compile(r"^[0-9a-f]{12,}$")
_TEST_TEAM_ID_RE = re.compile(
    r"^(time-kanban|time-swarm|time-run|time-graph|time-perfis|time-light|"
    r"time-quick|team-sub|team-prep|projeto-padrao)-[0-9a-f]{6,}$"
)
_DEFAULT_MEMBER_USERNAMES = ("local", "admin")


def protected_teams_path(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / _PROTECTED_FILENAME


def load_protected_team_specs(data_dir: Path) -> list[dict[str, Any]]:
    path = protected_teams_path(data_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("team guard: could not read %s: %s", path, exc)
        return []
    rows = raw.get("teams") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("id") or "").strip()]


def _resolve_owner_id(store: Any) -> Optional[str]:
    for username in _DEFAULT_MEMBER_USERNAMES:
        user = store.find_user_by_username(username)
        if user is not None:
            return user.id
    for row in (store._load().get("users") or {}).values():
        if isinstance(row, dict) and row.get("is_admin"):
            return str(row.get("id") or "")
    for row in (store._load().get("users") or {}).values():
        if isinstance(row, dict) and row.get("id"):
            return str(row["id"])
    return None


def _default_member_ids(store: Any, spec: dict[str, Any]) -> list[str]:
    mids = [str(x).strip() for x in (spec.get("member_ids") or []) if str(x).strip()]
    for username in spec.get("member_usernames") or _DEFAULT_MEMBER_USERNAMES:
        user = store.find_user_by_username(str(username).strip())
        if user is not None and user.id not in mids:
            mids.append(user.id)
    return mids


def _load_yaml_board(yaml_path: Path) -> dict[str, Any] | None:
    if not yaml_path.is_file():
        return None
    try:
        import yaml as _yaml

        raw = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        tasks = raw.get("posts") if isinstance(raw.get("posts"), list) else []
    columns = raw.get("columns")
    if not isinstance(columns, list):
        columns = raw.get("raias") if isinstance(raw.get("raias"), list) else []
    return {"columns": columns, "tasks": tasks}


def _kanban_board(data_dir: Path, team_id: str) -> dict[str, Any] | None:
    yaml_path = data_dir / "teams" / team_id / "kanban.yaml"
    try:
        from .kanban_mongo import load_team_board, mongo_kanban_enabled

        if mongo_kanban_enabled():
            board = load_team_board(
                scope=str(data_dir),
                team_id=team_id,
            )
            if board is not None:
                return board
            return None
        # Mongo not available — fallback to YAML (read-only)
        return _load_yaml_board(yaml_path)
    except Exception as exc:
        log.debug("team guard: board load failed for %s: %s", team_id, exc)
        return _load_yaml_board(yaml_path)


def _attachment_folder_count(team_dir: Path) -> int:
    att = team_dir / ".kanban-attachments"
    if not att.is_dir():
        return 0
    try:
        return sum(1 for c in att.iterdir() if c.is_dir())
    except OSError:
        return 0


def _read_team_name_file(team_dir: Path) -> str:
    path = team_dir / ".team-name"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_team_json_metadata(team_dir: Path) -> dict[str, str]:
    """Optional sidecar written by chat/shell flows (outside ``create_team``)."""
    path = team_dir / "team.json"
    if not path.is_file():
        return {}
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("name", "description"):
        val = str(raw.get(key) or "").strip()
        if val:
            out[key] = val
    return out


def _slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:48]


def _friendly_alias(name: str, team_id: str) -> Optional[str]:
    slug = _slugify(name)
    if not slug or slug == team_id or _HASH_TEAM_ID_RE.match(slug):
        return None
    if _TEST_TEAM_ID_RE.match(slug):
        return None
    return slug


def _is_test_team(team_id: str, team_dir: Path) -> bool:
    if _TEST_TEAM_ID_RE.match(team_id):
        return True
    if team_id.startswith("projeto-padrao-") and len(team_id) > 20:
        return True
    name = _read_team_name_file(team_dir)
    if name in ("Time Alpha", "Time Restrito", "Time Kanban"):
        return True
    return False


def _has_team_artifacts(team_dir: Path) -> bool:
    """True when the directory looks like an intentional team workspace."""
    if (team_dir / "kanban.yaml").is_file():
        return True
    if (team_dir / "workspace").is_dir():
        return True
    if _attachment_folder_count(team_dir) >= 1:
        return True
    if (team_dir / ".kanban-recover").is_dir():
        return True
    return False


def _has_real_board(data_dir: Path, team_id: str, team_dir: Path) -> bool:
    """True when the team has cards, attachments, or a persisted board."""
    if _has_team_artifacts(team_dir) and not (team_dir / "kanban.yaml").is_file():
        return True
    board = _kanban_board(data_dir, team_id)
    tasks = board.get("tasks") if isinstance(board, dict) else None
    if isinstance(tasks, list) and len(tasks) >= 1:
        return True
    if _attachment_folder_count(team_dir) >= 1:
        return True
    if (team_dir / "kanban.yaml").is_file():
        return True
    return False


def _infer_display_name(
    team_id: str,
    team_dir: Path,
    aliases: list[str],
    board: dict[str, Any] | None,
) -> str:
    for alias in aliases:
        if alias and not _HASH_TEAM_ID_RE.match(alias) and alias != team_id:
            return alias.replace("-", " ").strip().title() if "-" in alias else alias
    name_file = _read_team_name_file(team_dir)
    if name_file:
        return name_file
    if not _HASH_TEAM_ID_RE.match(team_id):
        return team_id.replace("-", " ").title()
    tasks = (board or {}).get("tasks") if isinstance(board, dict) else None
    if isinstance(tasks, list) and tasks:
        title = str((tasks[0] or {}).get("title") or "").strip()
        if title:
            chunk = re.split(r"[—\-:|]", title, maxsplit=1)[0].strip()
            if len(chunk) >= 4:
                return chunk[:80]
    return team_id


_SCAN_TEAM_DIRS_TTL = 10.0
_scan_team_dirs_cache: dict[str, tuple[float, tuple[dict, dict]]] = {}


def _scan_team_dirs(data_dir: Path) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """Return (canonical_id -> dir, canonical_id -> symlink aliases)."""
    key = str(data_dir)
    now = time.time()
    cached = _scan_team_dirs_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]  # type: ignore[return-value]

    teams_root = data_dir / "teams"
    canonical: dict[str, Path] = {}
    aliases: dict[str, list[str]] = {}
    if not teams_root.is_dir():
        result: tuple[dict, dict] = (canonical, aliases)
        _scan_team_dirs_cache[key] = (now + _SCAN_TEAM_DIRS_TTL, result)
        return canonical, aliases
    for entry in sorted(teams_root.iterdir()):
        try:
            if entry.is_symlink():
                target = entry.resolve()
                if target.parent == teams_root.resolve() and target.is_dir():
                    tid = target.name
                    canonical.setdefault(tid, target)
                    aliases.setdefault(tid, []).append(entry.name)
            elif entry.is_dir():
                canonical.setdefault(entry.name, entry)
        except OSError:
            continue
    result = (canonical, aliases)
    _scan_team_dirs_cache[key] = (now + _SCAN_TEAM_DIRS_TTL, result)
    return canonical, aliases


def _blank_team_row(tid: str, owner_id: str, *, name: str, description: str) -> dict[str, Any]:
    return {
        "id": tid,
        "name": name,
        "owner_id": owner_id,
        "created_at": time.time(),
        "description": description,
        "project_source": None,
        "github_url": None,
        "local_path": None,
        "github_branch": "main",
        "app_url": None,
        "profile_slugs": [],
        "whatsapp_numbers": [],
        "artifacts_dir": None,
        "member_ids": [],
        "github_repos": [],
        "site_links": [],
        "notify_emails": [],
        "linked_emails": [],
        "contacts": [],
        "whatsapp_group": None,
        "swarm_name": None,
        "latex_workspace_id": None,
        "latex_workspace_ids": [],
    }


def _merge_spec_row(
    existing: dict[str, Any] | None,
    spec: dict[str, Any],
    *,
    owner_id: str,
    store: Any,
) -> dict[str, Any]:
    tid = str(spec["id"]).strip()
    row = dict(existing) if isinstance(existing, dict) else _blank_team_row(
        tid, owner_id, name=str(spec.get("name") or tid), description=""
    )
    row["id"] = tid
    row["owner_id"] = str(row.get("owner_id") or owner_id)
    row["created_at"] = float(row.get("created_at") or time.time())
    if spec.get("name"):
        row["name"] = str(spec["name"]).strip()
    row["description"] = str(spec.get("description") or row.get("description") or "").strip()
    for key in (
        "project_source",
        "github_url",
        "local_path",
        "github_branch",
        "app_url",
        "artifacts_dir",
        "latex_workspace_id",
        "latex_workspace_ids",
        "swarm_name",
    ):
        if spec.get(key) not in (None, ""):
            row[key] = spec[key]
        else:
            row.setdefault(key, "main" if key == "github_branch" else None)
    slugs = list(row.get("profile_slugs") or [])
    for slug in spec.get("profile_slugs") or []:
        s = str(slug).strip()
        if s and s not in slugs:
            slugs.append(s)
    row["profile_slugs"] = slugs
    row["member_ids"] = _default_member_ids(store, spec) or list(row.get("member_ids") or [])
    for key in ("whatsapp_numbers", "github_repos", "site_links", "notify_emails", "linked_emails", "contacts"):
        row.setdefault(key, [])
    row.setdefault("whatsapp_group", row.get("whatsapp_group"))
    return row


def provision_team_disk_artifacts(
    data_dir: Path,
    team_id: str,
    team_name: str,
    *,
    alias: Optional[str] = None,
) -> dict[str, Any]:
    """Create on-disk markers so hygiene/guard recognise a real team workspace."""
    tid = str(team_id or "").strip()
    name = str(team_name or "").strip()
    if not tid:
        return {"ok": False, "error": "team_id required"}

    team_dir = data_dir / "teams" / tid
    team_dir.mkdir(parents=True, exist_ok=True)

    name_written = False
    if name:
        name_path = team_dir / ".team-name"
        try:
            name_path.write_text(name + "\n", encoding="utf-8")
            name_written = True
        except OSError as exc:
            log.warning("team provision: could not write %s: %s", name_path, exc)

    symlink_alias = str(alias or "").strip() or _friendly_alias(name, tid) or ""
    symlink_created = False
    if symlink_alias and _ensure_symlink(data_dir, tid, symlink_alias):
        symlink_created = True

    return {
        "ok": True,
        "team_id": tid,
        "team_dir": str(team_dir),
        "name_file": name_written,
        "symlink": symlink_alias if symlink_created else None,
    }


def _ensure_symlink(data_dir: Path, team_id: str, alias: str) -> bool:
    alias = str(alias or "").strip()
    if not alias or alias == team_id:
        return False
    teams_dir = data_dir / "teams"
    teams_dir.mkdir(parents=True, exist_ok=True)
    link = teams_dir / alias
    target = teams_dir / team_id
    if not target.is_dir():
        return False
    try:
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return False
            link.unlink()
        elif link.exists():
            return False
        link.symlink_to(team_id)
        from .kanban_mongo import mongo_kanban_enabled

        if mongo_kanban_enabled():
            from .kanban_board_dedupe import reconcile_team_board_if_needed

            reconcile_team_board_if_needed(
                str(data_dir),
                team_id,
                raw_team_id=alias,
            )
        return True
    except OSError as exc:
        log.warning("team guard: symlink %s -> %s failed: %s", alias, team_id, exc)
        return False


def _team_canonical_score(data_dir: Path, team_id: str, row: dict[str, Any]) -> tuple[int, int, float]:
    """Higher is better: (task_count, slug_id_bonus, -created_at)."""
    team_dir = data_dir / "teams" / team_id
    board = _kanban_board(data_dir, team_id) if team_dir.is_dir() else None
    tasks = board.get("tasks") if isinstance(board, dict) else None
    task_count = len(tasks) if isinstance(tasks, list) else 0
    slug_bonus = 0 if _HASH_TEAM_ID_RE.match(team_id) else 1
    created = float(row.get("created_at") or 0.0)
    return (task_count, slug_bonus, -created)


def _merge_team_rows(canonical: dict[str, Any], duplicate: dict[str, Any]) -> None:
    for key in ("profile_slugs", "member_ids", "whatsapp_numbers", "notify_emails", "linked_emails"):
        merged = list(canonical.get(key) or [])
        for item in duplicate.get(key) or []:
            val = str(item).strip()
            if val and val not in merged:
                merged.append(val)
        canonical[key] = merged
    dup_ws = duplicate.get("latex_workspace_ids") or duplicate.get("latex_workspace_id")
    if dup_ws:
        from .accounts import latex_workspace_ids_from_row, sync_latex_workspace_fields

        can_ws = latex_workspace_ids_from_row(canonical)
        for wid in latex_workspace_ids_from_row(duplicate):
            if wid not in can_ws:
                can_ws.append(wid)
        sync_latex_workspace_fields(canonical, can_ws)
    for key in ("github_repos", "site_links", "contacts"):
        merged = list(canonical.get(key) or [])
        seen = {json.dumps(x, sort_keys=True) for x in merged if isinstance(x, dict)}
        for item in duplicate.get(key) or []:
            if not isinstance(item, dict):
                continue
            sig = json.dumps(item, sort_keys=True)
            if sig not in seen:
                merged.append(item)
                seen.add(sig)
        canonical[key] = merged
    for key in (
        "description",
        "project_source",
        "github_url",
        "local_path",
        "github_branch",
        "app_url",
        "artifacts_dir",
        "latex_workspace_id",
        "latex_workspace_ids",
        "swarm_name",
        "whatsapp_group",
    ):
        if not canonical.get(key) and duplicate.get(key):
            canonical[key] = duplicate[key]


def _merge_kanban_boards(data_dir: Path, canonical_id: str, duplicate_id: str) -> int:
    board_c = _kanban_board(data_dir, canonical_id) or {"columns": [], "tasks": []}
    board_d = _kanban_board(data_dir, duplicate_id) or {"columns": [], "tasks": []}
    tasks_c = [t for t in (board_c.get("tasks") or []) if isinstance(t, dict)]
    tasks_d = [t for t in (board_d.get("tasks") or []) if isinstance(t, dict)]
    ids_c = {str(t.get("id") or "") for t in tasks_c if str(t.get("id") or "")}
    merged_count = 0
    for task in tasks_d:
        task = dict(task)
        tid = str(task.get("id") or "").strip()
        if not tid or tid in ids_c:
            base = tid or "TASK"
            suffix = duplicate_id[:6]
            new_id = f"{base}-from-{suffix}"
            n = 0
            while new_id in ids_c:
                n += 1
                new_id = f"{base}-from-{suffix}-{n}"
            task["id"] = new_id
            tid = new_id
        ids_c.add(tid)
        tasks_c.append(task)
        merged_count += 1
    columns = board_c.get("columns") or board_d.get("columns") or []
    yaml_path = data_dir / "teams" / canonical_id / "kanban.yaml"
    try:
        from .kanban_mongo import delete_team_board, mongo_kanban_enabled, save_team_board

        if mongo_kanban_enabled():
            save_team_board(
                scope=str(data_dir),
                team_id=canonical_id,
                columns=columns,
                tasks=tasks_c,
                source_path=str(yaml_path),
            )
            delete_team_board(scope=str(data_dir), team_id=duplicate_id)
    except Exception as exc:
        log.warning("team merge: mongo kanban failed %s <- %s: %s", canonical_id, duplicate_id, exc)
    return merged_count


def _merge_team_workspace_dirs(data_dir: Path, canonical_id: str, duplicate_id: str) -> None:
    src_root = data_dir / "teams" / duplicate_id
    dst_root = data_dir / "teams" / canonical_id
    if not src_root.is_dir():
        return
    dst_root.mkdir(parents=True, exist_ok=True)
    for sub in ("workspace", ".kanban-attachments", ".kanban-recover"):
        src_sub = src_root / sub
        if not src_sub.is_dir():
            continue
        dst_sub = dst_root / sub
        dst_sub.mkdir(parents=True, exist_ok=True)
        for item in src_sub.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src_sub)
            target = dst_sub / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(item, target)
            except OSError as exc:
                log.warning("team merge: copy %s -> %s failed: %s", item, target, exc)


def merge_duplicate_teams(store: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Merge store teams that share the same normalized display name."""
    from .accounts import team_name_key

    data_dir: Path = store.data_dir
    data = store._load()
    teams: dict[str, Any] = data.get("teams") or {}
    groups: dict[str, list[str]] = {}
    for tid, row in teams.items():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or not team_has_display_name(tid, name):
            continue
        key = team_name_key(name)
        if not key:
            continue
        groups.setdefault(key, []).append(tid)

    merged: list[dict[str, str]] = []
    removed: list[str] = []

    for key, ids in sorted(groups.items()):
        if len(ids) < 2:
            continue
        ranked = sorted(
            ids,
            key=lambda tid: _team_canonical_score(
                data_dir, tid, teams[tid] if isinstance(teams.get(tid), dict) else {}
            ),
            reverse=True,
        )
        canonical_id = ranked[0]
        canonical = teams.get(canonical_id)
        if not isinstance(canonical, dict):
            continue
        for dup_id in ranked[1:]:
            dup_row = teams.get(dup_id)
            if not isinstance(dup_row, dict):
                continue
            merged.append({"canonical": canonical_id, "duplicate": dup_id, "name_key": key})
            if dry_run:
                continue
            _merge_team_rows(canonical, dup_row)
            _merge_kanban_boards(data_dir, canonical_id, dup_id)
            _merge_team_workspace_dirs(data_dir, canonical_id, dup_id)
            teams.pop(dup_id, None)
            _remove_team_disk(data_dir, dup_id)
            removed.append(dup_id)
            log.info(
                "team merge: %s (%s) absorbed into %s",
                dup_id,
                dup_row.get("name"),
                canonical_id,
            )

    if removed and not dry_run:
        data["teams"] = teams
        store._save(data)

    return {
        "ok": True,
        "dry_run": dry_run,
        "merged": merged,
        "removed": removed,
        "groups": {k: v for k, v in groups.items() if len(v) > 1},
    }


def register_disk_orphan_teams(
    store: Any,
    *,
    ensure_default_members: bool = True,
) -> dict[str, Any]:
    """Register ``teams/*`` directories that exist on disk but not in the store."""
    data_dir: Path = store.data_dir
    owner_id = _resolve_owner_id(store)
    if not owner_id:
        return {"ok": False, "error": "no owner user for team guard", "restored": []}

    data = store._load()
    teams: dict[str, Any] = data.setdefault("teams", {})
    restored: list[str] = []
    canonical_dirs, alias_map = _scan_team_dirs(data_dir)

    for tid, team_dir in sorted(canonical_dirs.items()):
        if tid in teams:
            continue
        if _is_test_team(tid, team_dir):
            continue
        if not _has_team_artifacts(team_dir):
            continue
        board = _kanban_board(data_dir, tid)
        task_count = len(board.get("tasks") or []) if board else 0
        att_count = _attachment_folder_count(team_dir)
        meta = _read_team_json_metadata(team_dir)
        display = meta.get("name") or _infer_display_name(
            tid, team_dir, alias_map.get(tid, []), board
        )
        description = meta.get("description") or (
            f"Auto-registrado ({task_count} cards"
            + (f", {att_count} anexos" if att_count else "")
            + ")."
        )
        teams[tid] = _blank_team_row(
            tid,
            owner_id,
            name=display,
            description=description,
        )
        teams[tid]["member_ids"] = _default_member_ids(store, {})
        restored.append(tid)
        log.info(
            "team guard: auto-registered %s as %r (%d tasks, %d attachments)",
            tid,
            display,
            task_count,
            att_count,
        )

    if ensure_default_members:
        for tid in restored:
            row = teams.get(tid)
            if not isinstance(row, dict):
                continue
            mids = list(row.get("member_ids") or [])
            for username in _DEFAULT_MEMBER_USERNAMES:
                user = store.find_user_by_username(username)
                if (
                    user is not None
                    and user.id not in mids
                    and user.id != row.get("owner_id")
                ):
                    mids.append(user.id)
            row["member_ids"] = mids

    if restored:
        store._save(data)

    return {"ok": True, "restored": restored}


def ensure_protected_teams(store: Any) -> dict[str, Any]:
    """Register protected overrides and every orphan team board on disk."""
    data_dir: Path = store.data_dir
    owner_id = _resolve_owner_id(store)
    if not owner_id:
        return {"ok": False, "error": "no owner user for team guard"}

    merge_result = merge_duplicate_teams(store, dry_run=False)

    kanban_dedupe: dict[str, Any] = {"ok": True, "skipped": "not_run"}
    try:
        from .kanban_board_dedupe import bootstrap_kanban_board_reconciliation

        kanban_dedupe = bootstrap_kanban_board_reconciliation(str(data_dir))
    except Exception as exc:
        log.warning("team guard: kanban board dedupe skipped: %s", exc)
        kanban_dedupe = {"ok": False, "error": str(exc)}

    data = store._load()
    teams: dict[str, Any] = data.setdefault("teams", {})
    restored: list[str] = []
    symlinks: list[str] = []

    # 1) Explicit overrides (optional — per-team tweaks)
    for spec in load_protected_team_specs(data_dir):
        tid = str(spec["id"]).strip()
        existing = teams.get(tid) if isinstance(teams.get(tid), dict) else None
        row = _merge_spec_row(existing, spec, owner_id=owner_id, store=store)
        if existing is None:
            restored.append(tid)
        teams[tid] = row
        alias = str(spec.get("symlink") or spec.get("alias") or "").strip()
        if alias and _ensure_symlink(data_dir, tid, alias):
            symlinks.append(alias)

    if restored:
        store._save(data)

    orphan_result = register_disk_orphan_teams(store, ensure_default_members=True)
    restored.extend(orphan_result.get("restored") or [])
    data = store._load()
    teams = data.setdefault("teams", {})

    # 3) Friendly symlinks for hash ids with human names (any team in store)
    spec_aliases = {
        str(spec.get("id") or ""): str(spec.get("symlink") or spec.get("alias") or "").strip()
        for spec in load_protected_team_specs(data_dir)
    }
    for tid, row in list(teams.items()):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name == tid:
            continue
        alias = spec_aliases.get(tid) or _friendly_alias(name, tid)
        if alias and _ensure_symlink(data_dir, tid, alias):
            symlinks.append(alias)

    changed = bool(restored or symlinks)
    if changed:
        store._save(data)

    return {
        "ok": True,
        "restored": restored,
        "symlinks": sorted(set(symlinks)),
        "protected_overrides": len(load_protected_team_specs(data_dir)),
        "registered_total": len(teams),
        "merged_duplicates": merge_result.get("removed") or [],
        "kanban_dedupe": kanban_dedupe,
    }


def _team_dir_for_id(data_dir: Path, team_id: str) -> Optional[Path]:
    teams_root = data_dir / "teams"
    direct = teams_root / team_id
    if direct.is_dir():
        return direct
    canonical, _ = _scan_team_dirs(data_dir)
    return canonical.get(team_id)


def team_aliases_for_id(data_dir: Path, canonical_id: str) -> list[str]:
    """Return symlink aliases (e.g. artigo-haron) for a canonical team directory id."""
    _, alias_map = _scan_team_dirs(data_dir)
    return sorted(alias_map.get(str(canonical_id or "").strip(), []))


def _ref_variants(ref: str) -> tuple[str, str, str]:
    tid = str(ref or "").strip()
    lower = tid.lower()
    compact = lower.replace(" ", "").replace("-", "").replace("_", "")
    return tid, lower, compact


def _team_row_matches_ref(row: dict[str, Any], ref: str) -> bool:
    """True when ref matches team id, display name, or linked swarm_name."""
    tid, tid_lower, tid_compact = _ref_variants(ref)
    rid = str(row.get("id") or "").strip()
    if rid == tid:
        return True
    name = str(row.get("name") or "").strip()
    name_lower = name.lower()
    name_compact = name_lower.replace(" ", "").replace("-", "").replace("_", "")
    if name_lower == tid_lower or name_compact == tid_compact:
        return True
    swarm = str(row.get("swarm_name") or "").strip()
    if not swarm:
        return False
    swarm_lower = swarm.lower()
    swarm_compact = swarm_lower.replace(" ", "").replace("-", "").replace("_", "")
    return swarm_lower == tid_lower or swarm_compact == tid_compact


def _team_record_to_row(team: Any) -> dict[str, Any]:
    return {
        "id": getattr(team, "id", ""),
        "name": getattr(team, "name", ""),
        "swarm_name": getattr(team, "swarm_name", ""),
    }


def resolve_team_identifier(
    data_dir: Path,
    team_ref: str,
    *,
    store: Any = None,
    teams: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Resolve symlink alias, team id, display name, or swarm_name to canonical id."""
    ref = str(team_ref or "").strip()
    if not ref:
        return ref

    canonical = resolve_team_id_alias(data_dir, ref)
    if canonical != ref:
        return canonical

    if store is not None:
        try:
            for team in store.list_all_teams():
                row = _team_record_to_row(team)
                rid = str(row.get("id") or "").strip()
                if rid == ref:
                    return rid
                if _team_row_matches_ref(row, ref):
                    return rid
        except Exception as exc:
            log.debug("resolve_team_identifier: store lookup failed for %s: %s", ref, exc)

    for row in teams or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        if rid == ref:
            return rid
        if _team_row_matches_ref(row, ref):
            return rid

    return ref


def resolve_team_ref(data_dir: Path, team_ref: str, *, store: Any = None, user_id: str = "") -> str:
    """Resolve symlink alias, team name, or swarm_name to the canonical registered team id."""
    ref = str(team_ref or "").strip()
    if not ref:
        return ref
    resolved = resolve_team_identifier(data_dir, ref, store=store)
    if resolved != ref:
        return resolved
    if store is not None and user_id:
        try:
            return store.get_team(ref, user_id).id
        except (ValueError, AttributeError):
            pass
    return ref


def resolve_team_id_alias(data_dir: Path, team_id: str) -> str:
    """Resolve a friendly symlink alias (e.g. ``artigo-haron``) to the canonical id."""
    tid = str(team_id or "").strip()
    if not tid:
        return tid
    teams_root = data_dir / "teams"
    entry = teams_root / tid
    if entry.is_symlink():
        try:
            target = entry.resolve()
            if target.parent == teams_root.resolve() and target.is_dir():
                return target.name
        except OSError:
            pass
    _, alias_map = _scan_team_dirs(data_dir)
    for canonical_id, aliases in alias_map.items():
        if tid in aliases:
            return canonical_id
    return tid


def _registered_team_ids(data_dir: Path) -> set[str]:
    try:
        from .storage import get_account_store

        store = get_account_store(data_dir)
        return {t.id for t in store.list_all_teams()}
    except Exception as exc:
        log.debug("team guard: could not load registered teams: %s", exc)
        return set()


def guarded_team_ids(data_dir: Path) -> set[str]:
    """Team ids that must not be deleted by hygiene scripts."""
    ids: set[str] = set(_registered_team_ids(data_dir))

    for spec in load_protected_team_specs(data_dir):
        tid = str(spec.get("id") or "").strip()
        if tid:
            ids.add(tid)
        alias = str(spec.get("symlink") or spec.get("alias") or "").strip()
        if alias:
            ids.add(alias)

    teams_root = data_dir / "teams"
    if not teams_root.is_dir():
        return ids

    canonical, alias_map = _scan_team_dirs(data_dir)
    for tid, team_dir in canonical.items():
        if _is_test_team(tid, team_dir):
            continue
        if tid not in ids and not _has_team_artifacts(team_dir):
            continue
        ids.add(tid)
        for alias in alias_map.get(tid, []):
            ids.add(alias)
    return ids


def protected_team_ids(data_dir: Path) -> set[str]:
    """Backward-compatible alias for hygiene scripts."""
    return guarded_team_ids(data_dir)


def team_has_display_name(
    team_id: str,
    name: str,
    *,
    team_dir: Optional[Path] = None,
) -> bool:
    """True when the team has a human-readable name (not just the hash id)."""
    tid = str(team_id or "").strip()
    display = str(name or "").strip()
    name_file = _read_team_name_file(team_dir) if team_dir is not None else ""
    if name_file:
        if not (_HASH_TEAM_ID_RE.match(tid) and name_file == tid):
            return True
    if not display:
        return False
    if _HASH_TEAM_ID_RE.match(tid) and display == tid:
        return False
    return True


def _team_row_is_empty(data_dir: Path, team_id: str, team_dir: Path) -> bool:
    """True when the team has no cards or kanban attachments."""
    board = _kanban_board(data_dir, team_id)
    tasks = board.get("tasks") if isinstance(board, dict) else None
    if isinstance(tasks, list) and tasks:
        return False
    if _attachment_folder_count(team_dir) >= 1:
        return False
    return True


def _protected_team_id_set(data_dir: Path) -> set[str]:
    ids: set[str] = set()
    for spec in load_protected_team_specs(data_dir):
        tid = str(spec.get("id") or "").strip()
        if tid:
            ids.add(tid)
        alias = str(spec.get("symlink") or spec.get("alias") or "").strip()
        if alias:
            ids.add(alias)
    return ids


def _registered_team_ids_with_aliases(data_dir: Path, store: Any) -> set[str]:
    """Store team ids plus symlink aliases on disk (valid kanban keys)."""
    ids = {t.id for t in store.list_all_teams()}
    _, alias_map = _scan_team_dirs(data_dir)
    for tid in list(ids):
        for alias in alias_map.get(tid, []):
            ids.add(alias)
    return ids


def _remove_team_disk(data_dir: Path, team_id: str) -> None:
    teams_root = data_dir / "teams"
    if not teams_root.is_dir():
        return
    _, alias_map = _scan_team_dirs(data_dir)
    for alias in alias_map.get(team_id, []):
        link = teams_root / alias
        try:
            if link.is_symlink() or link.is_file():
                link.unlink()
        except OSError as exc:
            log.warning("team purge: could not unlink %s: %s", link, exc)
    target = teams_root / team_id
    try:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    except OSError as exc:
        log.warning("team purge: could not remove %s: %s", target, exc)


def purge_unnamed_teams(
    store: Any,
    *,
    dry_run: bool = True,
    require_empty: bool = True,
) -> dict[str, Any]:
    """Remove store entries (and disk) for hash-id teams with no display name.

    Targets teams where ``name == team_id`` (typical auto-registration junk).
    Skips ``protected_teams.json`` entries and teams with kanban cards or
    attachments unless ``require_empty=False``.
    """
    data_dir: Path = store.data_dir
    protected = _protected_team_id_set(data_dir)
    data = store._load()
    teams: dict[str, Any] = data.get("teams") or {}
    candidates: list[str] = []
    skipped: dict[str, str] = {}

    for tid, row in sorted(teams.items()):
        if not isinstance(row, dict):
            continue
        if tid in protected:
            skipped[tid] = "protected"
            continue
        team_dir = data_dir / "teams" / tid
        name = str(row.get("name") or "").strip()
        if team_has_display_name(tid, name, team_dir=team_dir if team_dir.exists() else None):
            continue
        if require_empty and team_dir.is_dir() and not _team_row_is_empty(data_dir, tid, team_dir):
            skipped[tid] = "has_cards_or_attachments"
            continue
        candidates.append(tid)

    removed: list[str] = []
    if not dry_run:
        try:
            from .kanban_mongo import delete_team_board, mongo_kanban_enabled

            mongo_on = mongo_kanban_enabled()
        except Exception:
            mongo_on = False
            delete_team_board = None  # type: ignore[assignment,misc]

        for tid in candidates:
            teams.pop(tid, None)
            if mongo_on and delete_team_board is not None:
                try:
                    delete_team_board(scope=str(data_dir), team_id=tid)
                except Exception as exc:
                    log.warning("team purge: mongo board delete failed for %s: %s", tid, exc)
            _remove_team_disk(data_dir, tid)
            removed.append(tid)
        if removed:
            data["teams"] = teams
            store._save(data)

    return {
        "ok": True,
        "dry_run": dry_run,
        "candidates": candidates,
        "removed": removed,
        "skipped": skipped,
        "remaining": len(teams) - (0 if dry_run else len(removed)),
    }


def purge_orphan_team_dirs(
    store: Any,
    *,
    dry_run: bool = True,
    require_empty: bool = True,
) -> dict[str, Any]:
    """Remove ``teams/*`` entries and Mongo boards not linked to the store.

    Deletes:
    - directories with no store row (empty or pytest-style test teams)
    - broken symlinks and aliases pointing at missing/unregistered teams
    - orphan kanban boards in MongoDB
    """
    data_dir: Path = store.data_dir
    teams_root = data_dir / "teams"
    registered = _registered_team_ids_with_aliases(data_dir, store)
    protected = _protected_team_id_set(data_dir)
    removed_dirs: list[str] = []
    removed_symlinks: list[str] = []
    skipped: dict[str, str] = {}

    if teams_root.is_dir():
        for entry in sorted(teams_root.iterdir()):
            name = entry.name
            if entry.is_symlink():
                drop = False
                try:
                    if not entry.exists():
                        drop = True
                    else:
                        target = entry.resolve()
                        if (
                            target.parent != teams_root.resolve()
                            or target.name not in registered
                        ):
                            drop = True
                except OSError:
                    drop = True
                if drop:
                    removed_symlinks.append(name)
                    if not dry_run:
                        try:
                            entry.unlink(missing_ok=True)
                        except OSError as exc:
                            log.warning("orphan purge: symlink %s: %s", entry, exc)
                continue

            if not entry.is_dir():
                continue
            if name in registered:
                continue
            if name in protected:
                skipped[name] = "protected"
                continue
            if _is_test_team(name, entry):
                removed_dirs.append(name)
                if not dry_run:
                    _remove_team_disk(data_dir, name)
                continue
            if require_empty and not _team_row_is_empty(data_dir, name, entry):
                skipped[name] = "has_cards_or_attachments"
                continue
            removed_dirs.append(name)
            if not dry_run:
                _remove_team_disk(data_dir, name)

    mongo_orphans: list[str] = []
    mongo_removed = 0
    try:
        from .kanban_mongo import (
            delete_team_board,
            list_team_ids,
            mongo_kanban_enabled,
        )

        if mongo_kanban_enabled():
            mongo_orphans = sorted(
                set(list_team_ids(scope=str(data_dir))) - registered
            )
            if not dry_run:
                for tid in mongo_orphans:
                    if delete_team_board(scope=str(data_dir), team_id=tid):
                        mongo_removed += 1
    except Exception as exc:
        log.warning("orphan purge: mongo cleanup failed: %s", exc)

    return {
        "ok": True,
        "dry_run": dry_run,
        "removed_dirs": removed_dirs,
        "removed_symlinks": removed_symlinks,
        "mongo_orphans": mongo_orphans,
        "mongo_boards_removed": mongo_removed,
        "skipped": skipped,
    }


def is_deletable_team(data_dir: Path, team_id: str) -> bool:
    """Return True only for pytest-style test teams with no store registration."""
    tid = str(team_id or "").strip()
    if not tid:
        return False
    if tid in _registered_team_ids(data_dir):
        return False
    if tid in guarded_team_ids(data_dir):
        return False
    team_dir = _team_dir_for_id(data_dir, tid)
    if team_dir is None:
        return True
    return _is_test_team(tid, team_dir)

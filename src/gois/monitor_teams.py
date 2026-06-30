"""Team CRUD, kanban, members, and context handlers for GoisMonitor."""

from __future__ import annotations

import base64
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from .accounts import TeamRecord, UserRecord
from .github_team_repo import (
    create_private_team_repo,
    gh_auth_ok,
    resolve_github_org,
    team_github_auto_create_enabled,
)
from .team_presets import SCHEDULING_TEAM_ID

log = logging.getLogger(__name__)

def _chat_kanban_board_summary(
    accounts,
    actor: UserRecord,
    team: TeamRecord,
) -> dict[str, Any]:
    """Build kanban summary payload for chat team tools."""
    entry: dict = {"team_id": team.id, "team_name": team.name}
    if team.profile_slugs:
        entry["profiles"] = team.profile_slugs
    if team.local_path:
        entry["local_path"] = team.local_path
    try:
        board = accounts.read_kanban(team.id, actor.id)
        tasks = board.get("tasks", [])
        kanban_dir = Path(board.get("path", "")).parent if board.get("path") else None
        att_base = None
        if kanban_dir:
            att_base = kanban_dir / ".kanban-attachments"
            for task in tasks:
                for att in task.get("attachments") or []:
                    if not att.get("stored_path"):
                        tid = task.get("id", "")
                        safe_id = re.sub(r"[^\w\-.]", "_", tid)[:64]
                        safe_file = att.get("safe_name", "")
                        att["stored_path"] = str(att_base / safe_id / safe_file)
        entry["kanban_exists"] = True
        entry["total"] = len(tasks)
        entry["todo"] = sum(
            1 for t in tasks
            if (t.get("column") or "").lower() in ("todo", "backlog", "a fazer")
        )
        entry["doing"] = sum(
            1 for t in tasks
            if (t.get("column") or "").lower() in ("doing", "in_progress", "em andamento")
        )
        entry["done"] = sum(
            1 for t in tasks
            if (t.get("column") or "").lower() in ("done", "concluído", "concluido")
        )
        entry["blocked"] = sum(
            1 for t in tasks
            if (t.get("column") or "").lower() in ("blocked", "bloqueado")
        )
        entry["review"] = sum(
            1 for t in tasks
            if (t.get("column") or "").lower() in ("review", "em revisão", "revisao")
        )
        entry["tasks"] = tasks
        att_index: list[dict[str, Any]] = []
        for task in tasks:
            for att in task.get("attachments") or []:
                att_index.append({
                    "task_id": task.get("id", ""),
                    "task_title": task.get("title", ""),
                    "file_name": att.get("name", ""),
                    "stored_path": att.get("stored_path", ""),
                    "mime_type": att.get("mime_type", ""),
                    "size": att.get("size", 0),
                })
        if att_index:
            entry["attachments_index"] = att_index
            entry["attachments_base_dir"] = str(att_base) if att_base else ""
    except Exception:
        entry["kanban_exists"] = False
        entry["total"] = 0
        entry["tasks"] = []
    return entry


class MonitorTeamsMixin:
    _DISK_ORPHAN_SYNC_TTL = 45.0
    _disk_orphan_sync_at: float = 0.0

    def _sync_disk_orphan_teams(self, *, force: bool = False) -> None:
        """Register team folders created outside ``create_team`` (chat shell, etc.)."""
        now = time.time()
        if not force and now < self._disk_orphan_sync_at + self._DISK_ORPHAN_SYNC_TTL:
            return
        try:
            from .accounts_team_guard import register_disk_orphan_teams

            register_disk_orphan_teams(self.accounts)
            self._disk_orphan_sync_at = now
        except Exception:
            log.debug("disk orphan team sync skipped", exc_info=True)

    def _resolve_team_for_actor(self, team_id: str, actor: UserRecord) -> TeamRecord:
        """Resolve team id/name with disk sync and list-all fallback (chat picker)."""
        self._sync_disk_orphan_teams()
        try:
            return self.accounts.get_team(team_id, actor.id)
        except (ValueError, Exception):
            all_teams = self.accounts.list_all_teams()
            tid = str(team_id or "").strip()
            team = next((t for t in all_teams if t.id == tid), None)
            if team is None:
                low = tid.lower()
                team = next(
                    (
                        t
                        for t in all_teams
                        if low == (t.name or "").lower()
                        or low == t.id.lower()
                        or low in (t.name or "").lower()
                        or low in t.id.lower()
                    ),
                    None,
                )
            if team is None:
                raise ValueError(f"Time não encontrado: {team_id}")
            return team

    def handle_teams_list(self, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        # Keep scheduling lane visible in the Teams UI for the current user.
        try:
            self.accounts.ensure_scheduling_team(actor.id)
        except Exception:
            pass  # non-fatal: don't block listing if scheduling team has bad data
        self._sync_disk_orphan_teams()
        # Chat combo, kanban e tools (qclaw_list_teams) precisam ver todos os times;
        # CRUD por time continua validando owner/membro/admin em cada handler.
        teams = [t.to_public() for t in self.accounts.list_all_teams()]
        from .accounts_team_guard import _scan_team_dirs

        data_dir = self.accounts.data_dir
        _, alias_map = _scan_team_dirs(data_dir)
        for row in teams:
            tid = str(row.get("id") or "").strip()
            aliases = alias_map.get(tid) or []
            if aliases:
                row["aliases"] = sorted(aliases)
        return {"ok": True, "teams": teams}

    def handle_chat_list_teams(self, user: Optional[UserRecord]) -> dict:
        """List all teams for the chat tool — same source as kanban/dashboard."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        self.accounts.ensure_scheduling_team(actor.id)
        self._sync_disk_orphan_teams()
        all_teams = self.accounts.list_all_teams(fresh=True)
        compact = []
        for t in all_teams:
            entry: dict = {"id": t.id, "name": t.name}
            if t.description:
                entry["description"] = t.description
            if t.local_path:
                entry["local_path"] = t.local_path
            if t.github_url:
                entry["github_url"] = t.github_url
            if t.site_links:
                entry["site_links"] = t.site_links
            if t.profile_slugs:
                entry["profiles"] = t.profile_slugs
            compact.append(entry)
        return {"ok": True, "total": len(compact), "teams": compact}

    def handle_chat_team_kanban(self, team_id: str, user: Optional[UserRecord]) -> dict:
        """Read kanban board(s) for chat. If team_id is empty, returns all teams' kanbans."""
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        self.accounts.ensure_scheduling_team(actor.id)
        all_teams = self.accounts.list_all_teams()

        if team_id:
            try:
                team = self.accounts.get_team(team_id, actor.id)
            except ValueError:
                team = next((t for t in all_teams if t.id == team_id), None)
                if team is None:
                    team = next(
                        (
                            t
                            for t in all_teams
                            if team_id.lower() in (t.name or "").lower()
                            or team_id.lower() in t.id.lower()
                        ),
                        None,
                    )
                if team is None:
                    return {
                        "ok": False,
                        "error": f"Time '{team_id}' não encontrado",
                        "available_teams": [
                            {"id": t.id, "name": t.name, "board_number": i + 1}
                            for i, t in enumerate(all_teams[:10])
                        ],
                    }
            board_number = next(
                (i + 1 for i, t in enumerate(all_teams) if t.id == team.id), 0
            )
            summary = _chat_kanban_board_summary(self.accounts, actor, team)
            summary["board_number"] = board_number
            summary["team_id"] = team.id
            if str(team_id).strip() != team.id:
                summary["team_alias"] = str(team_id).strip()
            return {"ok": True, **summary}
        else:
            # All teams: parallelize kanban reads with ThreadPoolExecutor
            def fetch_team_summary(team_with_index: tuple[int, TeamRecord]) -> dict:
                i, t = team_with_index
                entry = _chat_kanban_board_summary(self.accounts, actor, t)
                entry["board_number"] = i + 1
                return entry

            results = []
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(fetch_team_summary, enumerate(all_teams)))
            return {"ok": True, "total_teams": len(results), "boards": results}

    def handle_teams_presets(self, user: Optional[UserRecord]) -> dict:
        if self._accounts_actor(user) is None:
            return {"ok": False, "error": "not authenticated"}
        from .hermes_profiles import TEAM_ROLE_PRESETS
        from .team_presets import TEAM_PRESETS

        return {
            "ok": True,
            "teams": TEAM_PRESETS,
            "roles": TEAM_ROLE_PRESETS,
        }

    def _maybe_create_team_github_repo(
        self,
        team: TeamRecord,
        *,
        actor: UserRecord,
        payload: dict,
        is_new_team: bool,
    ) -> Optional[dict[str, Any]]:
        if not is_new_team:
            return None
        if str(payload.get("github_url") or "").strip():
            return None
        if payload.get("skip_github_repo") is True:
            return None
        if team.id == SCHEDULING_TEAM_ID or team.id.startswith(f"{SCHEDULING_TEAM_ID}-"):
            return None

        auth_cfg = getattr(getattr(self, "cfg", None), "auth", None)
        enabled = bool(getattr(auth_cfg, "auto_create_team_github_repo", True))
        if not enabled or not team_github_auto_create_enabled():
            return None
        if not gh_auth_ok():
            log.warning(
                "github repo não criado para time %s: `gh auth login` necessário",
                team.id,
            )
            return None

        workspace = self.accounts.data_dir / "teams" / team.id / "workspace"
        try:
            repo_info = create_private_team_repo(
                team_id=team.id,
                team_name=team.name,
                description=team.description,
                workspace_path=workspace,
                owner=resolve_github_org(getattr(auth_cfg, "team_github_org", None)),
                branch=str(payload.get("github_branch") or team.github_branch or "main"),
            )
        except Exception as exc:
            log.warning("falha ao criar repositório GitHub para time %s: %s", team.id, exc)
            return {"ok": False, "error": str(exc)}

        team = self.accounts.update_team(
            team.id,
            actor.id,
            {
                "project_source": "github",
                "github_url": repo_info["url"],
                "github_branch": repo_info.get("branch") or "main",
                "add_github_repo": {
                    "url": repo_info["url"],
                    "name": repo_info.get("name") or team.id,
                    "branch": repo_info.get("branch") or "main",
                    "description": team.description,
                },
            },
        )
        return {"ok": True, **repo_info, "team": team.to_public()}

    def _create_team_with_mongo_recovery(self, user_id: str, **kwargs: Any) -> TeamRecord:
        """Create a team in MongoDB, restarting mongod once on transient errors."""
        from .accounts_mongo import team_exists_in_mongo
        from .mongo import _mongo_connection_errors, ping
        from .mongo_autostart import restart_mongod

        try:
            team = self.accounts.create_team(user_id, **kwargs)
        except _mongo_connection_errors() as exc:
            log.warning("create_team: MongoDB down — tentando restart: %s", exc)
            if restart_mongod() and ping():
                team = self.accounts.create_team(user_id, **kwargs)
            else:
                raise ValueError(
                    "MongoDB indisponível. O restart automático falhou — "
                    "execute `brew services restart mongodb-community@7.0` "
                    "ou `docker compose up -d mongo`."
                ) from exc
        if not team_exists_in_mongo(self.accounts, team.id):
            raise ValueError("falha ao persistir time no MongoDB")
        return team

    def handle_team_create(self, payload: dict, user: Optional[UserRecord]) -> dict:
        from .mongo import ping
        from .mongo_autostart import restart_mongod

        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        if not ping():
            if not (restart_mongod() and ping()):
                return {
                    "ok": False,
                    "error": (
                        "MongoDB indisponível — times só são criados via MongoDB. "
                        "Inicie o MongoDB e tente novamente."
                    ),
                }
        preset_id = str(payload.get("preset_id") or "").strip() or None
        raw_profiles = payload.get("profile_slugs")
        profile_slugs: Optional[list[str]] = None
        if isinstance(raw_profiles, list):
            profile_slugs = [str(s).strip() for s in raw_profiles if str(s).strip()]
        existing_ids = {t.id for t in self.accounts.list_teams(actor.id)}
        try:
            team = self._create_team_with_mongo_recovery(
                actor.id,
                name=str(payload.get("name") or "").strip(),
                team_id=str(payload.get("id") or "").strip() or None,
                description=str(payload.get("description") or "").strip(),
                project_source=str(payload.get("project_source") or "").strip() or None,
                github_url=str(payload.get("github_url") or "").strip() or None,
                local_path=str(payload.get("local_path") or "").strip() or None,
                github_branch=str(payload.get("github_branch") or "main"),
                app_url=str(payload.get("app_url") or "").strip() or None,
                site_links=payload.get("site_links") if isinstance(payload.get("site_links"), list) else None,
                profile_slugs=profile_slugs,
                preset_id=preset_id,
                seed_kanban=bool(payload.get("seed_kanban", True)),
                artifacts_dir=str(payload.get("artifacts_dir") or "").strip() or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        is_new_team = team.id not in existing_ids
        github_result = self._maybe_create_team_github_repo(
            team,
            actor=actor,
            payload=payload,
            is_new_team=is_new_team,
        )
        if github_result and github_result.get("ok") and github_result.get("team"):
            team = self.accounts.get_team(team.id, actor.id)
        kanban = self.accounts.read_kanban(team.id, actor.id)
        result: dict[str, Any] = {
            "ok": True,
            "team": team.to_public(),
            "kanban": kanban,
            "persisted_in_mongo": True,
        }
        if hasattr(self.accounts, "invalidate_caches"):
            self.accounts.invalidate_caches()
        if github_result is not None:
            result["github_repo"] = github_result
        return result

    def handle_team_update(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        team = self.accounts.update_team(team_id, actor.id, payload)
        if (
            ("swarm_name" in payload or "profile_slugs" in payload)
            and str(team.swarm_name or "").strip()
        ):
            self._sync_team_profiles_to_swarm(team)
        kanban = self.accounts.read_kanban(team.id, actor.id)
        return {"ok": True, "team": team.to_public(), "kanban": kanban}
    def handle_team_delete(self, team_id: str, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            self.accounts.delete_team(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def handle_team_kanban_get(self, team_id: str, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        team = self.accounts.get_team(team_id, actor.id)
        board = self.accounts.read_kanban(team.id, actor.id)
        kanban_path = self.accounts.team_kanban_path(team.id)
        from .hermes_kanban import enrich_board_attachments

        enrich_board_attachments(board, kanban_path)
        return {"ok": True, "team": team.to_public(), "kanban": board}

    def handle_team_kanban_save(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        action = str(payload.get("action") or "").strip().lower()
        if action:
            action_payload = dict(payload)
            action_payload["team_id"] = team_id
            return self.handle_hermes_kanban_action(action_payload, user)
        team = self.accounts.get_team(team_id, actor.id)
        board = self.accounts.write_kanban(team.id, actor.id, payload)
        return {"ok": True, "team": team.to_public(), "kanban": board}

    def handle_team_context_get(
        self,
        team_id: str,
        user: Optional[UserRecord],
        *,
        quick: bool = False,
    ) -> dict:
        """Return team context: metadata, kanban summary, WhatsApp groups, members.

        ``quick=True`` skips Chroma stats and uses a shallow file scan (picker UI).
        """
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.get_team(team_id, actor.id)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}

        info = team.to_public()

        # Parallelize I/O-bound operations: kanban, members, contacts, files, normas, articles
        results = {"kanban_summary": None, "members": [], "contacts": [], "whatsapp_groups": [],
                   "context_stats": None, "recent_files": [], "normas": [], "articles": []}

        def _load_kanban_summary() -> Optional[dict]:
            try:
                board = self.accounts.read_kanban(team.id, actor.id)
                if board and board.get("tasks"):
                    columns = board.get("columns") or []
                    tasks = board.get("tasks") or []
                    col_counts: dict[str, int] = {}
                    for col in columns:
                        col_counts[col.get("title") or col.get("id", "")] = 0
                    for t in tasks:
                        col_id = t.get("column", "todo")
                        title = col_id
                        for col in columns:
                            if col.get("id") == col_id:
                                title = col.get("title", col_id)
                                break
                        col_counts[title] = col_counts.get(title, 0) + 1
                    active_tasks = [
                        {"id": t.get("id"), "title": t.get("title"), "column": t.get("column"),
                         "priority": t.get("priority"), "assignees": t.get("assignees", [])}
                        for t in tasks
                        if (t.get("column") or "").lower() not in ("done", "concluído", "concluido")
                    ]
                    return {
                        "total_tasks": len(tasks),
                        "columns": col_counts,
                        "active_tasks": active_tasks[:30],
                    }
            except Exception:
                pass
            return None

        def _load_members() -> list[dict]:
            try:
                member_ids = self.accounts.list_team_members(team_id, actor.id)
                members = []
                for mid in member_ids:
                    u = self.accounts.get_user_by_id(mid)
                    entry: dict = {"user_id": mid, "is_owner": mid == team.owner_id}
                    if u:
                        entry["username"] = u.username
                        entry["email"] = u.email
                    members.append(entry)
                return members
            except Exception:
                return []

        def _load_contacts() -> list[dict]:
            try:
                return list(team.contacts) if team.contacts else []
            except Exception:
                return []

        def _load_whatsapp_groups() -> list[dict]:
            try:
                from .openclaw_chat import _WHATSAPP_GROUPS_CONTEXT
                team_name_lower = (info.get("name") or "").lower()
                groups = []
                for g_name, g_data in _WHATSAPP_GROUPS_CONTEXT.items():
                    if team_name_lower and team_name_lower in g_name.lower():
                        groups.append({
                            "name": g_name,
                            "jid": g_data.get("jid"),
                            "member_count": len(g_data.get("members", [])),
                            "members": [m["name"] for m in g_data.get("members", [])],
                        })
                return groups
            except Exception:
                return []

        def _load_context_stats() -> Optional[dict]:
            try:
                from .team_context_store import TeamContextStore
                store = TeamContextStore()
                return store.stats(team_id)
            except Exception:
                return None

        def _load_recent_files() -> list[dict]:
            try:
                from .team_files_download import team_file_download_href
                from .team_files_search import list_recent_team_files

                rf = list_recent_team_files(team_id=team.id, limit=12, quick=quick)
                files = []
                if rf.get("ok"):
                    for row in rf.get("files") or []:
                        rel = str(row.get("relative_path") or "").strip()
                        files.append({
                            "name": row.get("name"),
                            "relative_path": rel,
                            "path": row.get("path"),
                            "root_label": row.get("root_label"),
                            "size_bytes": row.get("size_bytes"),
                            "modified_at": row.get("modified_at"),
                            "download_url": team_file_download_href(
                                team_id=team.id,
                                relative_path=rel,
                            ),
                        })
                return files
            except Exception:
                return []

        def _load_normas() -> list[dict]:
            try:
                from .team_normas_ops import list_normas
                nr = list_normas(team.id)
                return list(nr.get("normas") or []) if nr.get("ok") else []
            except Exception:
                return []

        def _load_articles() -> list[dict]:
            try:
                from .accounts import latex_workspace_ids_from_row
                from .latex_articles import list_articles

                articles = []
                article_cap = 12 if quick else 10_000
                for ws_id in latex_workspace_ids_from_row(info):
                    ar = list_articles(ws_id, fast_mode=True)
                    if not ar.get("ok"):
                        continue
                    ws_name = ar.get("workspace_name") or ws_id
                    for row in ar.get("articles") or []:
                        articles.append({
                            "id": row.get("id"),
                            "title": row.get("title"),
                            "has_pdf": bool(row.get("has_pdf")),
                            "workspace_id": ws_id,
                            "workspace_name": ws_name,
                            "relative_tex": row.get("relative_tex") or row.get("id"),
                            "has_backup": bool(row.get("has_backup")),
                            "relative_backup": row.get("relative_backup"),
                            "tex_mtime": row.get("tex_mtime"),
                            "backup_mtime": row.get("backup_mtime"),
                        })
                        if len(articles) >= article_cap:
                            return articles
                return articles
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                "kanban": executor.submit(_load_kanban_summary),
                "members": executor.submit(_load_members),
                "contacts": executor.submit(_load_contacts),
                "whatsapp": executor.submit(_load_whatsapp_groups),
                "files": executor.submit(_load_recent_files),
                "normas": executor.submit(_load_normas),
                "articles": executor.submit(_load_articles),
            }
            if not quick:
                futures["stats"] = executor.submit(_load_context_stats)
            results["kanban_summary"] = futures["kanban"].result()
            results["members"] = futures["members"].result()
            results["contacts"] = futures["contacts"].result()
            results["whatsapp_groups"] = futures["whatsapp"].result()
            results["recent_files"] = futures["files"].result()
            results["normas"] = futures["normas"].result()
            results["articles"] = futures["articles"].result()
            if not quick:
                results["context_stats"] = futures["stats"].result()

        from .whatsapp_allowlist_contacts import (
            build_team_whatsapp_dest_index,
            enrich_whatsapp_dest_row,
            load_global_allowlist_indexes,
            whatsapp_send_allowlist_flags,
        )
        from .whatsapp_team_guard import resolve_team_group_jid

        team_index = build_team_whatsapp_dest_index(team=team)
        global_digits, global_groups = load_global_allowlist_indexes(self.allowlist_store)
        enriched_contacts: list[dict] = []
        for contact in results["contacts"]:
            if not isinstance(contact, dict):
                continue
            dest = str(
                contact.get("whatsapp") or contact.get("phone") or ""
            ).strip()
            flags = whatsapp_send_allowlist_flags(
                dest,
                team_index=team_index,
                global_digits=global_digits,
                global_group_jids=global_groups,
            )
            enriched_contacts.append({**contact, **flags})
        results["contacts"] = enriched_contacts

        whatsapp_allowlist: list[dict] = []
        seen_allowlist: set[str] = set()
        allowlist_sources: list[str] = list(team.whatsapp_numbers or [])
        group_jid, _group_source = resolve_team_group_jid(team)
        if group_jid and group_jid not in allowlist_sources:
            allowlist_sources.append(group_jid)
        for contact in team.contacts or []:
            if not isinstance(contact, dict):
                continue
            raw_contact = str(
                contact.get("whatsapp") or contact.get("phone") or contact.get("jid") or ""
            ).strip()
            if raw_contact and raw_contact not in allowlist_sources:
                allowlist_sources.append(raw_contact)
        for raw in allowlist_sources:
            key = str(raw).strip().lower()
            if not key or key in seen_allowlist:
                continue
            seen_allowlist.add(key)
            label = ""
            if key.endswith("@g.us") and group_jid and key == group_jid.lower():
                label = team.name
            whatsapp_allowlist.append(
                enrich_whatsapp_dest_row(
                    str(raw),
                    team_index=team_index,
                    global_digits=global_digits,
                    global_group_jids=global_groups,
                    label=label,
                )
            )

        return {
            "ok": True,
            "team": info,
            "kanban_summary": results["kanban_summary"],
            "members": results["members"],
            "contacts": results["contacts"],
            "whatsapp_groups": results["whatsapp_groups"],
            "whatsapp_allowlist": whatsapp_allowlist,
            "context_stats": results["context_stats"],
            "recent_files": results["recent_files"],
            "normas": results["normas"],
            "articles": results["articles"],
            "articles_count": len(results["articles"]),
            "quick": quick,
        }

    def handle_team_normas_get(self, team_id: str, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import list_normas

        return list_normas(team.id)

    def handle_team_normas_upload(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import upload_norma

        name = str(payload.get("name") or payload.get("file_name") or "").strip()
        data_b64 = str(payload.get("data_base64") or "").strip()
        mime_type = str(payload.get("mime_type") or "").strip()
        return upload_norma(team.id, name=name, data_base64=data_b64, mime_type=mime_type)

    def handle_team_normas_delete(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import delete_norma

        name = str(payload.get("name") or payload.get("file_name") or "").strip()
        return delete_norma(team.id, name=name)

    def handle_team_normas_download(
        self, team_id: str, query: dict, user: Optional[UserRecord]
    ) -> tuple[bytes, str, str] | dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import serve_norma_download

        name = str(query.get("name") or "").strip()
        return serve_norma_download(team.id, name)

    def handle_team_normas_from_file(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import copy_team_file_to_norma

        return copy_team_file_to_norma(
            team.id,
            relative_path=str(payload.get("relative_path") or payload.get("rel") or "").strip(),
            path=str(payload.get("path") or payload.get("file") or "").strip(),
            name=str(payload.get("name") or payload.get("file_name") or "").strip(),
        )

    def handle_team_normas_from_whatsapp(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import import_norma_from_whatsapp

        return import_norma_from_whatsapp(
            team.id,
            msg_id=str(payload.get("msg_id") or "").strip(),
            name=str(payload.get("name") or "").strip(),
            data_base64=str(payload.get("data_base64") or "").strip(),
            mime_type=str(payload.get("mime_type") or "").strip(),
            body=str(payload.get("body") or "").strip(),
            sender=str(payload.get("sender") or "").strip(),
            chat_name=str(payload.get("chat_name") or "").strip(),
            date=str(payload.get("date") or "").strip(),
        )

    def handle_team_articles_folder_upload(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_articles_folder_ops import upload_article_folder

        folder_name = str(payload.get("folder_name") or payload.get("name") or "").strip()
        files = payload.get("files")
        if not isinstance(files, list):
            return {"ok": False, "error": "files deve ser uma lista"}
        result = upload_article_folder(team.id, folder_name=folder_name, files=files)
        if not result.get("ok"):
            return result
        ws_id = str(result.get("workspace_id") or "").strip()
        if ws_id:
            try:
                updated = self.accounts.update_team(
                    team.id,
                    actor.id,
                    {"add_latex_workspace_id": ws_id},
                )
                result["team"] = updated.to_public()
            except Exception as exc:
                result["warning"] = f"pasta salva mas falha ao vincular ao time: {exc}"
        return result

    def handle_team_normas_preview(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .team_normas_ops import preview_norma_bytes

        return preview_norma_bytes(
            name=str(payload.get("name") or payload.get("file_name") or "").strip(),
            data_base64=str(payload.get("data_base64") or "").strip(),
            mime_type=str(payload.get("mime_type") or "").strip(),
        )

    def handle_team_normas_whatsapp_preview(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}

        msg_id = str(payload.get("msg_id") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not msg_id:
            return {"ok": False, "error": "msg_id é obrigatório"}

        from .whatsapp_busca import download_message_media, message_to_norma_markdown

        media = download_message_media(
            msg_id,
            chat=str(payload.get("chat") or payload.get("chat_jid") or "").strip(),
        )
        if media.get("ok"):
            return media

        if not body:
            return media

        md_bytes = message_to_norma_markdown(
            body=body,
            sender=str(payload.get("sender") or "").strip(),
            chat_name=str(payload.get("chat_name") or "").strip(),
            date=str(payload.get("date") or "").strip(),
            msg_id=msg_id,
        )
        excerpt = md_bytes.decode("utf-8", errors="ignore")[:2500]
        return {
            "ok": True,
            "msg_id": msg_id,
            "name": f"whatsapp-{msg_id[:8]}.md",
            "mime_type": "text/markdown",
            "size_bytes": len(md_bytes),
            "data_base64": base64.b64encode(md_bytes).decode("ascii"),
            "excerpt": excerpt.strip(),
            "has_text": bool(excerpt.strip()),
            "is_pdf": False,
            "import_kind": "text",
        }

    def handle_team_legal_evaluations_list(
        self, team_id: str, query: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .legal_evaluation_ops import list_evaluations

        try:
            limit = int(query.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        try:
            offset = int(query.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        return list_evaluations(
            team_id=team.id,
            evaluation_type=str(query.get("evaluation_type") or query.get("type") or ""),
            limit=limit,
            offset=offset,
        )

    def handle_team_legal_evaluations_search(
        self, team_id: str, query: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .legal_evaluation_ops import search_evaluations

        try:
            limit = int(query.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        return search_evaluations(
            query=str(query.get("q") or query.get("query") or ""),
            team_id=team.id,
            limit=limit,
        )

    def handle_team_legal_evaluation_get(
        self, team_id: str, evaluation_id: str, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .legal_evaluation_ops import get_evaluation

        result = get_evaluation(evaluation_id=evaluation_id)
        if not result.get("ok"):
            return result
        ev_team = str(result.get("evaluation", {}).get("team_id") or "")
        if ev_team and ev_team != team.id:
            return {"ok": False, "error": "avaliação não pertence a este time"}
        return result

    def handle_team_legal_evaluation_save(
        self, team_id: str, payload: dict, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self._resolve_team_for_actor(team_id, actor)
        except (ValueError, Exception) as e:
            return {"ok": False, "error": f"Time não encontrado: {e}"}
        from .legal_evaluation_ops import save_evaluation

        extraction = payload.get("extraction")
        if not isinstance(extraction, dict):
            extraction = None
        return save_evaluation(
            team_id=team.id,
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or payload.get("report") or ""),
            evaluation_type=str(payload.get("evaluation_type") or payload.get("type") or "geral"),
            question=str(payload.get("question") or payload.get("prompt") or ""),
            source_path=str(payload.get("source_path") or payload.get("path") or ""),
            source_name=str(payload.get("source_name") or payload.get("filename") or ""),
            norma_name=str(payload.get("norma_name") or payload.get("name") or ""),
            extraction=extraction,
            tags=payload.get("tags"),
            created_by=str(payload.get("created_by") or getattr(actor, "username", "") or "dashboard"),
            evaluation_id=str(payload.get("evaluation_id") or payload.get("id") or ""),
        )

    def handle_team_members_list(self, team_id: str, user: Optional[UserRecord]) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            member_ids = self.accounts.list_team_members(team_id, actor.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        team = self.accounts.get_team(team_id, actor.id)
        members = []
        for mid in member_ids:
            u = self.accounts.get_user_by_id(mid)
            entry: dict = {"user_id": mid, "is_owner": mid == team.owner_id}
            if u:
                entry["username"] = u.username
                entry["email"] = u.email
                entry["whatsapp"] = u.whatsapp
                entry["role"] = "Proprietário" if mid == team.owner_id else "Membro"
            else:
                entry["username"] = mid
                entry["role"] = "Membro"
            members.append(entry)
        # Include team-level contacts (WhatsApp/email contacts added in team detail)
        for c in (team.contacts or []):
            entry = {
                "user_id": c.get("id") or c.get("name", ""),
                "username": c.get("name") or c.get("email") or c.get("whatsapp") or "—",
                "email": c.get("email") or "",
                "whatsapp": c.get("whatsapp") or c.get("phone") or "",
                "role": c.get("role") or "Contato",
                "is_owner": False,
                "is_contact": True,
            }
            members.append(entry)
        return {"ok": True, "team_id": team_id, "members": members}

    def _resolve_member_user_id(self, ref: str) -> str:
        """Resolve user id or username to a canonical user id."""
        text = str(ref or "").strip()
        if not text:
            raise ValueError("user_id ou username é obrigatório")
        users = (self.accounts._load().get("users") or {})
        if text in users and isinstance(users.get(text), dict):
            return text
        found = self.accounts.find_user_by_username(text)
        if found is not None:
            return found.id
        raise ValueError(f"usuário não encontrado: {text}")

    def handle_team_members_add(
        self, team_id: str, member_user_id: str, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            resolved_id = self._resolve_member_user_id(member_user_id)
            if resolved_id == actor.id:
                raise ValueError("o proprietário já é membro do time")
            team = self.accounts.add_team_member(team_id, actor.id, resolved_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "team": team.to_public()}

    def handle_team_members_remove(
        self, team_id: str, member_user_id: str, user: Optional[UserRecord]
    ) -> dict:
        actor = self._accounts_actor(user)
        if actor is None:
            return {"ok": False, "error": "not authenticated"}
        try:
            team = self.accounts.remove_team_member(team_id, actor.id, member_user_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "team": team.to_public()}

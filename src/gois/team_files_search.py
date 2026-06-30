"""Search files across all configured team folders (local_path, artifacts, workspace)."""

from __future__ import annotations

import fnmatch
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .local_paths import project_stack_root

_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    ".stack",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "vendor",
    ".cursor",
    ".idea",
    ".vscode",
})

_SKIP_DIR_PREFIXES = (".",)


def _teams_root() -> Path:
    return project_stack_root() / "accounts" / "teams"


def _load_teams() -> tuple[list[dict[str, Any]], str]:
    """Load team metadata from accounts store, then filesystem fallback."""
    try:
        from .config import Config
        from .storage import get_account_store

        cfg = Config.load()
        store = get_account_store(Path(cfg.auth.data_dir))
        teams = [t.to_public() for t in store.list_all_teams()]
        if teams:
            return teams, "accounts"
    except Exception:
        pass

    teams: list[dict[str, Any]] = []
    root = _teams_root()
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                teams.append({"id": entry.name, "name": entry.name})
    return teams, "filesystem"


def _match_team(
    teams: list[dict[str, Any]],
    *,
    team_id: str = "",
    team_name: str = "",
) -> Optional[dict[str, Any]]:
    tid = (team_id or "").strip().lower()
    tname = (team_name or "").strip().lower()
    if tid:
        try:
            from .accounts_team_guard import resolve_team_id_alias

            canonical = resolve_team_id_alias(_teams_root().parent, team_id).lower()
            if canonical and canonical != tid:
                tid = canonical
        except Exception:
            pass
        for team in teams:
            team_key = str(team.get("id") or "").strip().lower()
            if team_key == tid or tid in team_key:
                return team
    if tname:
        for team in teams:
            name = str(team.get("name") or "").strip().lower()
            if tname in name or name in tname:
                return team
    return None


def _add_root(roots: list[dict[str, Any]], label: str, path: Path) -> None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return
    if not resolved.is_dir():
        return
    for row in roots:
        if row.get("path") == str(resolved):
            return
    roots.append({"label": label, "path": str(resolved)})


_ARTIGO_DIR_RE = re.compile(
    r'(?:ARTIGO_DIR|LOCAL_PATH|PROJECT_DIR)\s*=\s*["\']([^"\']+)["\']'
)


def _infer_project_dirs_from_team_scripts(team_dir: Path) -> list[Path]:
    """Best-effort: read ARTIGO_DIR/LOCAL_PATH from swarm shell scripts in the team folder."""
    found: list[Path] = []
    seen: set[str] = set()
    if not team_dir.is_dir():
        return found
    for script in team_dir.rglob("*.sh"):
        if _should_skip_dir(script.parent.name):
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _ARTIGO_DIR_RE.finditer(text):
            raw = match.group(1).strip()
            if not raw:
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except OSError:
                continue
            for candidate in (resolved, resolved.parent):
                try:
                    key = str(candidate.resolve())
                except OSError:
                    continue
                if key in seen or not candidate.is_dir():
                    continue
                seen.add(key)
                found.append(candidate)
    return found


def collect_team_search_roots(team: dict[str, Any], *, quick: bool = False) -> list[dict[str, Any]]:
    """Return labeled directories to search for a team.

    When ``quick`` is True, only stack folders under ``.stack/accounts/teams/<id>``
    are scanned (skips ``local_path``, artifacts, and script inference).
    """
    roots: list[dict[str, Any]] = []
    team_id = str(team.get("id") or "").strip()
    if not team_id:
        return roots

    team_dir = _teams_root() / team_id
    _add_root(roots, "stack_team", team_dir)
    _add_root(roots, "stack_workspace", team_dir / "workspace")
    _add_root(roots, "stack_docs", team_dir / "docs")
    _add_root(roots, "stack_normas", team_dir / "normas")
    _add_root(roots, "stack_attachments", team_dir / ".kanban-attachments")
    if not quick:
        _add_root(roots, "stack_repo", team_dir / "repo")

        local_path = str(team.get("local_path") or "").strip()
        if local_path:
            _add_root(roots, "local_path", Path(local_path))

        artifacts_dir = str(team.get("artifacts_dir") or "").strip()
        if artifacts_dir:
            _add_root(roots, "artifacts_dir", Path(artifacts_dir))
        else:
            _add_root(roots, "artifacts_dir", team_dir / "workspace" / "artifacts")

        project_source = str(team.get("project_source") or "").strip().lower()
        if project_source == "github" and (team_dir / "repo").is_dir():
            _add_root(roots, "github_repo", team_dir / "repo")

        for inferred in _infer_project_dirs_from_team_scripts(team_dir):
            _add_root(roots, "project_script", inferred)

    return roots


def _should_skip_dir(dirname: str) -> bool:
    if dirname in _SKIP_DIR_NAMES:
        return True
    if dirname == ".kanban-attachments":
        return False
    return dirname.startswith(_SKIP_DIR_PREFIXES)


def _matches_file(
    path: Path,
    *,
    query: str,
    pattern: str,
    extension: str,
) -> bool:
    name = path.name
    if extension:
        ext = extension.lstrip(".").lower()
        if path.suffix.lower() != f".{ext}":
            return False
    if pattern:
        return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name, pattern.lower())
    if query:
        return query.lower() in name.lower()
    return True


def _file_row(path: Path, *, team_id: str, team_name: str, root_label: str, root_path: str) -> dict[str, Any]:
    try:
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        size = stat.st_size
    except OSError:
        mtime = ""
        size = 0
    try:
        rel = str(path.resolve().relative_to(Path(root_path).resolve()))
    except ValueError:
        rel = path.name
    return {
        "team_id": team_id,
        "team_name": team_name,
        "root_label": root_label,
        "root_path": root_path,
        "path": str(path.resolve()),
        "relative_path": rel,
        "name": path.name,
        "extension": path.suffix.lower().lstrip("."),
        "size_bytes": size,
        "modified_at": mtime,
    }


def _walk_root(
    root: dict[str, Any],
    *,
    team_id: str,
    team_name: str,
    query: str,
    pattern: str,
    extension: str,
    max_depth: int,
    limit: int,
    results: list[dict[str, Any]],
) -> None:
    if len(results) >= limit:
        return
    root_path = Path(str(root.get("path") or ""))
    if not root_path.is_dir():
        return
    root_label = str(root.get("label") or "root")
    base_depth = len(root_path.parts)

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for filename in filenames:
            if len(results) >= limit:
                return
            if filename.startswith(".") and filename not in {".gitkeep"}:
                continue
            candidate = current / filename
            if not candidate.is_file():
                continue
            if not _matches_file(candidate, query=query, pattern=pattern, extension=extension):
                continue
            results.append(
                _file_row(
                    candidate,
                    team_id=team_id,
                    team_name=team_name,
                    root_label=root_label,
                    root_path=str(root_path),
                )
            )


def search_team_files(
    *,
    team_id: str = "",
    team_name: str = "",
    query: str = "",
    pattern: str = "",
    extension: str = "",
    path: str = "",
    all_teams: bool = False,
    limit: int = 50,
    max_depth: int = 8,
    quick: bool = False,
) -> dict[str, Any]:
    """Search files in team folders."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        max_depth = max(0, min(int(max_depth), 20))
    except (TypeError, ValueError):
        max_depth = 8

    query = str(query or "").strip()
    pattern = str(pattern or "").strip()
    extension = str(extension or "").strip()
    explicit_path = str(path or "").strip()

    if explicit_path:
        root = Path(explicit_path).expanduser()
        if not root.is_dir():
            return {"ok": False, "error": f"Pasta não encontrada: {explicit_path}"}
        results: list[dict[str, Any]] = []
        _walk_root(
            {"label": "path", "path": str(root.resolve())},
            team_id=team_id or "",
            team_name=team_name or "",
            query=query,
            pattern=pattern,
            extension=extension,
            max_depth=max_depth,
            limit=limit,
            results=results,
        )
        return {
            "ok": True,
            "source": "path",
            "path": str(root.resolve()),
            "query": query,
            "pattern": pattern,
            "extension": extension,
            "count": len(results),
            "truncated": len(results) >= limit,
            "files": results,
        }

    teams, source = _load_teams()
    if not teams:
        return {
            "ok": False,
            "error": "Nenhum time encontrado. Configure times no monitor ou em .stack/accounts/teams/",
        }

    selected: list[dict[str, Any]]
    if all_teams or (not team_id and not team_name):
        selected = teams
    else:
        team = _match_team(teams, team_id=team_id, team_name=team_name)
        if team is None:
            return {
                "ok": False,
                "error": f"Time não encontrado: {team_id or team_name}",
                "available_teams": [
                    {"id": t.get("id"), "name": t.get("name")}
                    for t in teams[:20]
                ],
            }
        selected = [team]

    results = []
    searched_roots: list[dict[str, Any]] = []
    for team in selected:
        tid = str(team.get("id") or "").strip()
        tname = str(team.get("name") or tid).strip()
        roots = collect_team_search_roots(team, quick=quick)
        for root in roots:
            searched_roots.append({"team_id": tid, "team_name": tname, **root})
            _walk_root(
                root,
                team_id=tid,
                team_name=tname,
                query=query,
                pattern=pattern,
                extension=extension,
                max_depth=max_depth,
                limit=limit,
                results=results,
            )
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    return {
        "ok": True,
        "source": source,
        "teams_searched": len(selected),
        "roots_searched": len(searched_roots),
        "query": query,
        "pattern": pattern,
        "extension": extension,
        "count": len(results),
        "truncated": len(results) >= limit,
        "search_roots": searched_roots,
        "files": results,
    }


def _dedupe_file_rows(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows that point at the same file (e.g. stack_team vs stack_attachments)."""
    seen_paths: set[str] = set()
    seen_fallback: set[tuple[str, int, str]] = set()
    out: list[dict[str, Any]] = []
    for row in files:
        path_key = str(row.get("path") or "").strip()
        if path_key:
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            out.append(row)
            continue
        fallback = (
            str(row.get("name") or "").lower(),
            int(row.get("size_bytes") or 0),
            str(row.get("modified_at") or ""),
        )
        if fallback in seen_fallback:
            continue
        seen_fallback.add(fallback)
        out.append(row)
    return out


def list_recent_team_files(
    *,
    team_id: str = "",
    team_name: str = "",
    limit: int = 12,
    max_depth: int = 8,
    quick: bool = False,
) -> dict[str, Any]:
    """Return the most recently modified files for a team."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 12
    if quick:
        max_depth = min(max_depth, 3)
        fetch_limit = min(max(limit * 4, 24), 80)
    else:
        fetch_limit = min(max(limit * 10, 40), 300)
    data = search_team_files(
        team_id=team_id,
        team_name=team_name,
        limit=fetch_limit,
        max_depth=max_depth,
        quick=quick,
    )
    if not data.get("ok"):
        return data
    files = list(data.get("files") or [])
    files.sort(key=lambda row: str(row.get("modified_at") or ""), reverse=True)
    files = _dedupe_file_rows(files)[:limit]
    return {
        "ok": True,
        "count": len(files),
        "files": files,
    }


def dispatch_team_files_search(args: dict[str, Any]) -> dict[str, Any]:
    """Shared handler for MCP `team_files_search` and chat `qclaw_team_files_search`."""
    return search_team_files(
        team_id=str(args.get("team_id") or ""),
        team_name=str(args.get("team_name") or ""),
        query=str(args.get("query") or args.get("q") or ""),
        pattern=str(args.get("pattern") or args.get("glob") or ""),
        extension=str(args.get("extension") or args.get("ext") or ""),
        path=str(args.get("path") or args.get("folder") or ""),
        all_teams=bool(args.get("all_teams")),
        limit=int(args.get("limit") or 50),
        max_depth=int(args.get("max_depth") or 8),
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "team_files_search",
            "description": (
                "Procura arquivos em todas as pastas de um time: local_path, artifacts_dir, "
                "workspace interno (.stack/accounts/teams/<id>/workspace), docs e anexos. "
                "Use para 'achar arquivo do time', 'buscar .md no time', 'listar PDFs'. "
                "Skill: qclaw-chat-team-files-search. Chat: qclaw_team_files_search."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "ID do time"},
                    "team_name": {"type": "string", "description": "Nome parcial do time"},
                    "query": {
                        "type": "string",
                        "description": "Substring no nome do arquivo",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob no nome (ex.: *.md, relatorio-*.pdf)",
                    },
                    "extension": {
                        "type": "string",
                        "description": "Extensão sem ponto (ex.: md, pdf, tex)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Pasta explícita (ignora resolução de time)",
                    },
                    "all_teams": {
                        "type": "boolean",
                        "description": "Buscar em todos os times",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Máximo de resultados (default 50, max 500)",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Profundidade máxima por pasta (default 8)",
                    },
                },
                "required": [],
            },
        }
    ]

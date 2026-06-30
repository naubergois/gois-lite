"""Zip a team project directory and attach the archive to a kanban card."""

from __future__ import annotations

import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .team_files_search import (
    _SKIP_DIR_NAMES,
    _load_teams,
    _match_team,
    _teams_root,
    collect_team_search_roots,
)

_DEFAULT_MAX_ZIP_BYTES = 200 * 1024 * 1024

_SECRET_FILE_NAMES = frozenset({".env", "secrets.env", "deepseek.key", "config.yaml"})
_SECRET_FILE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


def _parse_max_bytes(raw: Any) -> int:
    try:
        value = int(raw or _DEFAULT_MAX_ZIP_BYTES)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_ZIP_BYTES
    return max(1024, min(value, _DEFAULT_MAX_ZIP_BYTES))


def _should_skip_dir(name: str, *, include_hidden: bool) -> bool:
    if name in _SKIP_DIR_NAMES:
        return True
    if name == ".kanban-attachments":
        return True
    if include_hidden:
        return False
    return name.startswith(".")


def _should_skip_file(path: Path, *, include_hidden: bool) -> bool:
    name = path.name
    if name in _SECRET_FILE_NAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name.endswith(_SECRET_FILE_SUFFIXES):
        return True
    if not include_hidden and name.startswith("."):
        return name not in {".gitignore", ".env.example"}
    return False


def _match_dir_under_roots(candidate: Path, roots: list[dict[str, Any]]) -> bool:
    try:
        resolved = candidate.expanduser().resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    for root in roots:
        root_path = Path(str(root.get("path") or ""))
        try:
            root_resolved = root_path.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def resolve_project_dir(
    *,
    team_id: str = "",
    team_name: str = "",
    path: str = "",
    workdir: str = "",
    subdir: str = "",
) -> dict[str, Any]:
    """Resolve the project directory to zip."""
    explicit = str(path or "").strip()
    workdir_str = str(workdir or "").strip()
    rel = str(subdir or "").strip().replace("\\", "/").lstrip("/")
    if rel and ".." in Path(rel).parts:
        return {"ok": False, "error": "subdir inválido (.. não permitido)"}

    teams, source = _load_teams()
    team: Optional[dict[str, Any]] = None
    if team_id or team_name:
        team = _match_team(teams, team_id=team_id, team_name=team_name)
        if team is None:
            return {
                "ok": False,
                "error": f"Time não encontrado: {team_id or team_name}",
            }

    if explicit:
        candidate = Path(explicit).expanduser()
        if rel:
            candidate = candidate / rel
        if not candidate.is_dir():
            return {"ok": False, "error": f"Diretório não encontrado: {candidate}"}
        if team is not None:
            roots = collect_team_search_roots(team)
            if not _match_dir_under_roots(candidate, roots):
                return {
                    "ok": False,
                    "error": f"Diretório fora das pastas do time: {candidate}",
                }
        tid = str(team.get("id") or "").strip() if team else ""
        tname = str(team.get("name") or tid).strip() if team else ""
        resolved = candidate.resolve()
        return {
            "ok": True,
            "source": source,
            "team_id": tid,
            "team_name": tname,
            "project_path": str(resolved),
            "project_name": resolved.name,
        }

    if team is not None:
        tid = str(team.get("id") or "").strip()
        tname = str(team.get("name") or tid).strip()
        candidates: list[tuple[str, Path]] = []
        local_path = str(team.get("local_path") or "").strip()
        if local_path:
            candidates.append(("local_path", Path(local_path).expanduser()))
        project_source = str(team.get("project_source") or "").strip().lower()
        repo = _teams_root() / tid / "repo"
        if project_source == "github" and repo.is_dir():
            candidates.append(("github_repo", repo))
        candidates.append(("stack_team", _teams_root() / tid))

        for label, base in candidates:
            target = base
            if rel:
                target = base / rel
            try:
                resolved = target.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_dir():
                return {
                    "ok": True,
                    "source": source,
                    "team_id": tid,
                    "team_name": tname,
                    "project_path": str(resolved),
                    "project_name": resolved.name,
                    "root_label": label,
                }
        return {
            "ok": False,
            "error": f"Nenhum diretório de projeto encontrado para o time {tname}",
        }

    if workdir_str:
        target = Path(workdir_str).expanduser()
        if rel:
            target = target / rel
        if not target.is_dir():
            return {"ok": False, "error": f"Diretório não encontrado: {target}"}
        resolved = target.resolve()
        return {
            "ok": True,
            "source": source,
            "project_path": str(resolved),
            "project_name": resolved.name,
        }

    return {
        "ok": False,
        "error": "Informe team_id, workdir ou path do projeto a compactar.",
    }


def create_project_zip(
    source_dir: Path | str,
    *,
    output_path: Optional[Path | str] = None,
    zip_name: str = "",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """Create a zip archive for a project directory."""
    root = Path(source_dir).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"Diretório não encontrado: {root}"}

    safe_stem = re.sub(r"[^\w\-.]+", "_", root.name)[:80] or "projeto"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = str(zip_name or "").strip() or f"{safe_stem}-{stamp}.zip"
    if not fname.lower().endswith(".zip"):
        fname = f"{fname}.zip"

    if output_path is not None:
        dest = Path(output_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="qclaw-projzip-"))
        dest = tmp / fname

    file_count = 0
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for current, dirnames, filenames in __import__("os").walk(root):
                current_path = Path(current)
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not _should_skip_dir(d, include_hidden=include_hidden)
                ]
                for filename in filenames:
                    fp = current_path / filename
                    if _should_skip_file(fp, include_hidden=include_hidden):
                        continue
                    arcname = str(fp.relative_to(root))
                    zf.write(fp, arcname)
                    file_count += 1
    except OSError as exc:
        return {"ok": False, "error": f"Falha ao criar zip: {exc}"}

    if file_count == 0:
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": "Nenhum arquivo incluído no zip (diretório vazio ou tudo excluído)"}

    size = dest.stat().st_size
    return {
        "ok": True,
        "zip_path": str(dest.resolve()),
        "zip_name": dest.name,
        "file_count": file_count,
        "size_bytes": size,
        "project_path": str(root),
    }


def zip_project_and_attach(
    *,
    task_id: str,
    team_id: str = "",
    team_name: str = "",
    workdir: str = "",
    path: str = "",
    subdir: str = "",
    zip_name: str = "",
    include_hidden: bool = False,
    max_bytes: int = _DEFAULT_MAX_ZIP_BYTES,
    upload_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    host: str = "",
) -> dict[str, Any]:
    """Zip a project directory and attach the archive to a kanban card."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}

    resolved = resolve_project_dir(
        team_id=team_id,
        team_name=team_name,
        path=path,
        workdir=workdir,
        subdir=subdir,
    )
    if not resolved.get("ok"):
        return resolved

    zipped = create_project_zip(
        resolved["project_path"],
        zip_name=zip_name,
        include_hidden=include_hidden,
    )
    if not zipped.get("ok"):
        return {**zipped, "stage": "zip"}

    limit = _parse_max_bytes(max_bytes)
    size = int(zipped.get("size_bytes") or 0)
    zip_path = Path(str(zipped.get("zip_path") or ""))
    if size > limit:
        zip_path.unlink(missing_ok=True)
        return {
            "ok": False,
            "stage": "zip",
            "error": (
                f"Zip muito grande ({size} bytes; limite {limit}). "
                "Use subdir para compactar só uma pasta ou reduza o projeto."
            ),
            "size_bytes": size,
            "max_bytes": limit,
        }

    upload_payload: dict[str, Any] = {
        "task_id": task_id,
        "file_name": zipped.get("zip_name") or zip_path.name,
        "name": zipped.get("zip_name") or zip_path.name,
        "mime_type": "application/zip",
        "file_path": str(zip_path),
    }
    tid = str(resolved.get("team_id") or team_id or "").strip()
    wd = str(workdir or "").strip()
    if tid:
        upload_payload["team_id"] = tid
    if wd:
        upload_payload["workdir"] = wd

    if upload_fn is not None:
        result = upload_fn(upload_payload)
    else:
        result = _upload_via_api(upload_payload, host=host)

    cleanup = zip_path.parent.name.startswith("qclaw-projzip-")
    if cleanup:
        zip_path.unlink(missing_ok=True)
        try:
            zip_path.parent.rmdir()
        except OSError:
            pass

    if not result.get("ok"):
        return {
            "ok": False,
            "stage": "attach",
            "error": str(result.get("error") or "upload failed"),
            "zip_name": upload_payload["file_name"],
            "file_count": zipped.get("file_count"),
            "size_bytes": size,
            "project_path": resolved.get("project_path"),
            "response": result,
        }

    return {
        "ok": True,
        "task_id": task_id,
        "team_id": tid or None,
        "project_path": resolved.get("project_path"),
        "zip_name": upload_payload["file_name"],
        "file_count": zipped.get("file_count"),
        "size_bytes": size,
        "attachment": result,
    }


def _upload_via_api(payload: dict[str, Any], *, host: str = "") -> dict[str, Any]:
    import httpx

    from .team_swarm_ops import _auth_headers, monitor_base_url

    base = (host or monitor_base_url()).rstrip("/")
    try:
        with httpx.Client(
            base_url=base,
            timeout=180.0,
            headers=_auth_headers(),
        ) as client:
            resp = client.post("/hermes/kanban/attachments/upload", json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {
                    "ok": False,
                    "error": resp.text[:500] or f"HTTP {resp.status_code}",
                }
            if resp.status_code >= 400 and data.get("ok") is not False:
                data = {
                    "ok": False,
                    "error": data.get("error") or f"HTTP {resp.status_code}",
                }
            return data
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def dispatch_kanban_attach_project_zip(args: dict[str, Any]) -> dict[str, Any]:
    """Shared handler for MCP `kanban_attach_project_zip` and chat tool."""
    return zip_project_and_attach(
        task_id=str(args.get("task_id") or ""),
        team_id=str(args.get("team_id") or ""),
        team_name=str(args.get("team_name") or ""),
        workdir=str(args.get("workdir") or ""),
        path=str(args.get("path") or args.get("project_path") or ""),
        subdir=str(args.get("subdir") or args.get("relative_path") or ""),
        zip_name=str(args.get("zip_name") or args.get("name") or ""),
        include_hidden=bool(args.get("include_hidden")),
        max_bytes=_parse_max_bytes(args.get("max_bytes")),
        host=str(args.get("host") or "").strip(),
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "kanban_attach_project_zip",
            "description": (
                "Compacta um diretório de projeto (local_path, repo ou workdir) em ZIP "
                "e anexa ao card do Kanban. Use para *zip do projeto*, *anexar código*, "
                "*backup do projeto no card*. Exclui .git, node_modules, .venv e segredos. "
                "Chat: qclaw_kanban_attach_project_zip."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID do card destino (ex.: TASK-017)",
                    },
                    "team_id": {"type": "string", "description": "ID do time"},
                    "team_name": {"type": "string", "description": "Nome parcial do time"},
                    "path": {
                        "type": "string",
                        "description": "Caminho absoluto do projeto (alternativa a team_id)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Workdir do kanban (se não usar team_id)",
                    },
                    "subdir": {
                        "type": "string",
                        "description": "Subpasta relativa dentro do projeto (opcional)",
                    },
                    "zip_name": {
                        "type": "string",
                        "description": "Nome do arquivo .zip (opcional)",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "Incluir dotfiles (exceto segredos; default false)",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Limite de tamanho do zip (default 200MB)",
                    },
                },
                "required": ["task_id"],
            },
        }
    ]

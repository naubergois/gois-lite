"""
MCP Server — Kanban Cards para IDEs externas.

Expõe cards/tarefas do Kanban do gois via Model Context Protocol (MCP),
permitindo que outras IDEs (Kiro, Cursor, VS Code + Continue, etc.) leiam e
interajam com os cards que os desenvolvedores precisam implementar.

Uso:
    python -m gois.mcp_cards_server [--workdir /path/to/project]

Ou via config MCP da IDE:
    {
        "mcpServers": {
            "qclaw-cards": {
                "command": "python",
                "args": ["-m", "gois.mcp_cards_server"],
                "env": {"QCLAW_KANBAN_WORKDIRS": "/path/project1:/path/project2"}
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import hashlib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from .kanban_ide_handoff_dispatch import (
    dispatch_kanban_ide_handoff,
    mcp_tool_specs as kanban_ide_handoff_mcp_specs,
)

_DEFAULT_KANBAN_FILENAMES = ["kanban.yaml", ".hermes/kanban.yaml"]
_HASH_TEAM_PATTERN = re.compile(r"^[0-9a-f]{12,}$")
_kanban_yaml_cache: dict[tuple[str, float, int], dict[str, Any]] = {}
_boards_list_cache: tuple[float, tuple[Any, ...], dict[str, Any]] | None = None
_BOARDS_LIST_TTL_SECONDS = 15.0


def _find_kanban_file(workdir: Path) -> Path | None:
    """Locate kanban file in a workdir (YAML or Mongo-backed project board)."""
    from .kanban_mongo import list_project_board_paths

    project_paths = list_project_board_paths(workdir=workdir)
    if project_paths:
        return project_paths[0]
    for rel in _DEFAULT_KANBAN_FILENAMES:
        candidate = workdir / rel
        if candidate.is_file():
            return candidate
    return None


def _kanban_cache_key(path: Path) -> tuple[str, float, int]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime, st.st_size)
    except OSError:
        return (str(path), 0.0, 0)


def _load_kanban_yaml(path: Path) -> dict[str, Any]:
    """Load kanban board (Mongo-first for team/project boards)."""
    from .hermes_kanban import load_kanban
    from .kanban_mongo import is_managed_kanban_path, mongo_kanban_enabled

    if mongo_kanban_enabled() and is_managed_kanban_path(path):
        try:
            board = load_kanban(path)
        except ValueError as exc:
            return {"error": str(exc), "columns": [], "tasks": [], "exists": False}
        return {
            "columns": board.get("columns") or [],
            "tasks": board.get("tasks") or [],
            "exists": bool(board.get("exists", False)),
        }

    cache_key = _kanban_cache_key(path)
    cached = _kanban_yaml_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        import yaml
    except ImportError:
        data = {"error": "PyYAML not installed", "columns": [], "tasks": []}
        _kanban_yaml_cache[cache_key] = data
        return data

    if not path.is_file():
        data = {"columns": [], "tasks": [], "exists": False}
        _kanban_yaml_cache[cache_key] = data
        return data

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        data = {"error": str(exc), "columns": [], "tasks": []}
        _kanban_yaml_cache[cache_key] = data
        return data

    data = raw if isinstance(raw, dict) else {}

    # Extract columns
    columns_raw = None
    for key in ("columns", "lanes", "raias"):
        if isinstance(data.get(key), list):
            columns_raw = data[key]
            break
    columns = []
    if columns_raw:
        for row in columns_raw:
            if isinstance(row, dict):
                cid = str(row.get("id") or "").strip()
                title = str(row.get("title") or row.get("label") or cid).strip()
                if cid:
                    columns.append({"id": cid, "title": title})

    # Extract tasks
    tasks_raw = None
    for key in ("tasks", "posts"):
        if isinstance(data.get(key), list):
            tasks_raw = data[key]
            break
    tasks = []
    if tasks_raw:
        for row in tasks_raw:
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("id") or "").strip()
            title = str(row.get("title") or "").strip()
            if not task_id or not title:
                continue
            tasks.append({
                "id": task_id,
                "title": title,
                "description": str(row.get("description") or "").strip(),
                "column": str(row.get("column") or row.get("status") or "todo").strip(),
                "priority": row.get("priority"),
                "assignees": row.get("assignees") or [],
                "skills": row.get("skills") or [],
                "notes": str(row.get("notes") or "").strip(),
                "requirements": str(row.get("requirements") or "").strip(),
                "implementation_details": str(
                    row.get("implementation_details") or row.get("implementation") or ""
                ).strip(),
                "implementation_location": str(
                    row.get("implementation_location")
                    or row.get("app_location")
                    or ""
                ).strip(),
                "open_questions": row.get("open_questions") or [],
                "workdir": str(row.get("workdir") or "").strip() or None,
                "created_at": row.get("created_at"),
                "completed_at": row.get("completed_at"),
            })

    result = {"columns": columns, "tasks": tasks, "exists": True}
    if len(_kanban_yaml_cache) > 512:
        _kanban_yaml_cache.clear()
    _kanban_yaml_cache[cache_key] = result
    return result


def _get_workdirs() -> list[Path]:
    """Resolve configured workdirs from env or defaults."""
    env_val = (
        os.environ.get("GOIS_KANBAN_WORKDIRS", "").strip()
        or os.environ.get("QCLAW_KANBAN_WORKDIRS", "").strip()
    )
    if env_val:
        candidates = [
            Path(p).expanduser().resolve()
            for p in env_val.split(":")
            if p.strip()
        ]
    else:
        from .local_paths import _repo_root

        candidates = [_repo_root()]

    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        out.append(path)
    return out


def _sync_named_boards_from_hash(
    named_boards: dict[str, tuple[Path, Path]],
    hash_boards: dict[str, tuple[Path, Path]],
) -> None:
    """Copy newer hash boards into named boards when they have more tasks."""
    from .kanban_mongo import (
        mongo_kanban_enabled,
        parse_team_kanban_path,
    )
    from .hermes_kanban import save_kanban

    for name, (named_dir, named_file) in named_boards.items():
        named_data = _load_kanban_yaml(named_file)
        named_count = len(named_data.get("tasks", []))
        named_updated = 0.0
        if mongo_kanban_enabled():
            parsed_named = parse_team_kanban_path(named_file)
            if parsed_named:
                from .kanban_mongo import load_team_board

                scope, team_id = parsed_named
                mongo_named = load_team_board(scope=scope, team_id=team_id)
                if mongo_named is not None:
                    named_count = len(mongo_named.get("tasks") or [])
        else:
            try:
                named_updated = named_file.stat().st_mtime
            except OSError:
                continue

        best_hash_file: Path | None = None
        best_hash_count = 0
        best_hash_data: dict[str, Any] | None = None

        for _, (_, hfile) in hash_boards.items():
            if not mongo_kanban_enabled():
                try:
                    h_mtime = hfile.stat().st_mtime
                except OSError:
                    continue
                if h_mtime <= named_updated:
                    continue
            h_data = _load_kanban_yaml(hfile)
            h_count = len(h_data.get("tasks", []))
            if h_count > named_count and h_count > best_hash_count:
                best_hash_file = hfile
                best_hash_count = h_count
                best_hash_data = h_data

        if not best_hash_file or best_hash_count <= named_count:
            continue

        try:
            if mongo_kanban_enabled() and best_hash_data is not None:
                payload = {
                    "columns": best_hash_data.get("columns") or [],
                    "tasks": best_hash_data.get("tasks") or [],
                }
                save_kanban(named_file, payload)
            else:
                import shutil

                shutil.copy2(str(best_hash_file), str(named_file))
            _kanban_yaml_cache.clear()
            global _boards_list_cache
            _boards_list_cache = None
            log.info(
                "Synced kanban: hash (%s tasks) -> %s (%s tasks)",
                best_hash_count,
                name,
                named_count,
            )
        except OSError as exc:
            log.warning("Failed to sync kanban -> %s: %s", name, exc)


def _accounts_scope_for_workdir(wd: Path) -> str | None:
    accounts_dir = (wd / ".stack" / "accounts").resolve()
    if accounts_dir.is_dir():
        return str(accounts_dir)
    return None


def _get_team_kanban_files(
    *,
    sync: bool = False,
    include_hash: bool = True,
) -> list[tuple[Path, Path, str]]:
    """Scan team kanban boards (Mongo + legacy YAML on disk).

    Returns list of (team_dir, kanban_file, team_id) tuples.
    Hash-board sync is expensive (O(named*hash) YAML reads) and is skipped
    unless ``sync=True`` (e.g. before mutating a board).
    """
    from .kanban_mongo import kanban_board_available, list_team_ids, mongo_kanban_enabled

    results: list[tuple[Path, Path, str]] = []
    seen_team_ids: set[str] = set()

    for teams_dir in _accounts_team_roots():
        scope = str(teams_dir.parent.resolve())
        try:
            all_boards: dict[str, tuple[Path, Path]] = {}
            # Collect symlink aliases: canonical hash id → friendly alias name
            hash_to_alias: dict[str, str] = {}
            for team_dir in sorted(teams_dir.iterdir()):
                if team_dir.is_symlink():
                    # Record alias mapping so hash-only teams can use the
                    # friendly name instead of being filtered out.
                    try:
                        target = team_dir.resolve().name
                        if _HASH_TEAM_PATTERN.match(target):
                            hash_to_alias.setdefault(target, team_dir.name)
                    except OSError:
                        pass
                    continue
                if not team_dir.is_dir():
                    continue
                kanban_file = team_dir / "kanban.yaml"
                if kanban_board_available(kanban_file):
                    all_boards[team_dir.name] = (team_dir, kanban_file)

            if mongo_kanban_enabled():
                for team_id in list_team_ids(scope=scope):
                    if team_id not in all_boards:
                        team_dir = teams_dir / team_id
                        all_boards[team_id] = (team_dir, team_dir / "kanban.yaml")

            if sync and all_boards:
                named_boards = {
                    k: v for k, v in all_boards.items()
                    if not _HASH_TEAM_PATTERN.match(k)
                }
                hash_boards = {
                    k: v for k, v in all_boards.items()
                    if _HASH_TEAM_PATTERN.match(k)
                }
                if named_boards and hash_boards:
                    _sync_named_boards_from_hash(named_boards, hash_boards)

            for team_name, (team_dir, kanban_file) in all_boards.items():
                display_name = team_name
                if _HASH_TEAM_PATTERN.match(team_name):
                    alias = hash_to_alias.get(team_name)
                    if alias:
                        # Always prefer the friendly symlink alias over the hash
                        display_name = alias
                    elif not include_hash:
                        # No alias and caller doesn't want raw hashes → skip
                        continue
                if display_name in seen_team_ids:
                    continue
                results.append((team_dir, kanban_file, display_name))
                seen_team_ids.add(display_name)
        except OSError:
            continue
    return results


# ─── MCP Server Implementation (JSON-RPC over stdio) ─────────────────────────

_SERVER_NAME = "qclaw-cards"
_SERVER_VERSION = "1.0.0"

# Instrução para agentes ao fechar cards de UI — reutilizada em move_card e update_card.
_UI_IMPL_LOCATION_RULE = (
    "Ao implementar ou alterar interface gráfica (nova página, botão, menu, formulário, "
    "painel ou fluxo visível ao usuário), OBRIGATÓRIO registrar "
    "implementation_location com o caminho no menu da aplicação onde o usuário encontra "
    "a funcionalidade (ex.: 'Menu Operação & Infra > Monitor', "
    "'Equipes & Projetos > Kanban', 'Conhecimento > Memória'). "
    "Use update_card antes de mover para done, ou passe implementation_location em move_card."
)

# Tools exposed to IDEs
_TOOLS = [
    {
        "name": "list_kanban_boards",
        "description": (
            "Lista todos os boards Kanban disponíveis nos workdirs configurados. "
            "Retorna os projetos e caminhos dos kanban files encontrados."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_cards",
        "description": (
            "Retorna os cards/tarefas de um board Kanban. "
            "Pode filtrar por coluna (todo, doing, review, etc.) e/ou assignee."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Caminho do projeto (workdir). Se omitido, busca o primeiro board encontrado.",
                },
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time (ex: orientacoes-porto). Alternativa ao workdir — resolve automaticamente o board do time.",
                },
                "column": {
                    "type": "string",
                    "description": "Filtrar por coluna (backlog, todo, doing, review, done, etc.)",
                },
                "assignee": {
                    "type": "string",
                    "description": "Filtrar por responsável (assignee)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_card_detail",
        "description": (
            "Retorna detalhes completos de um card específico pelo ID (ex: TASK-001)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Caminho do projeto (workdir)",
                },
                "card_id": {
                    "type": "string",
                    "description": "ID do card (ex: TASK-001)",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "get_my_cards",
        "description": (
            "Retorna todos os cards atribuídos a um desenvolvedor específico, "
            "em todos os boards disponíveis. Ideal para saber o que precisa desenvolver."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "assignee": {
                    "type": "string",
                    "description": "Nome/slug do desenvolvedor para buscar seus cards",
                },
                "exclude_done": {
                    "type": "boolean",
                    "description": "Se true, exclui cards na coluna 'done' (padrão: true)",
                },
            },
            "required": ["assignee"],
        },
    },
    {
        "name": "get_cards_todo",
        "description": (
            "Retorna cards pendentes (backlog + todo) prontos para serem implementados. "
            "Útil para a IDE saber quais tarefas estão disponíveis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Caminho do projeto (opcional)",
                },
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time (ex: orientacoes-porto). Alternativa ao workdir.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_teams",
        "description": (
            "Lista times do Gois com swarm vinculado e perfis Hermes. "
            "Use antes de run_team_swarm para obter team_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "team_swarm_status",
        "description": (
            "Resume cards pendentes e prontidão do swarm de um time "
            "(perfis, swarm_name, contagem por coluna)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time",
                },
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "run_team_swarm",
        "description": (
            "Ativa o swarm do time para resolver cards do Kanban. "
            "Atribui cards abertos, move para doing, executa agentes (run_all) "
            "e finaliza em done. Requer Gois em execução."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time",
                },
                "objective": {
                    "type": "string",
                    "description": "Objetivo extra (opcional — usa backlog se vazio)",
                },
                "force": {
                    "type": "boolean",
                    "description": "Forçar reexecução se swarm ocupado",
                },
                "run_all": {
                    "type": "boolean",
                    "description": "Executar todos os agentes do swarm (padrão: true)",
                },
                "use_team_cards": {
                    "type": "boolean",
                    "description": "Usar cards do kanban do time (padrão: true)",
                },
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "create_card",
        "description": (
            "Cria um card no Kanban de um time (ação create_task). "
            "Requer title e team_id ou workdir."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "ID do time (preferido)",
                },
                "workdir": {
                    "type": "string",
                    "description": "Workdir do board (alternativa ao team_id)",
                },
                "title": {
                    "type": "string",
                    "description": "Título do card (obrigatório)",
                },
                "description": {
                    "type": "string",
                    "description": "Descrição do card",
                },
                "column": {
                    "type": "string",
                    "description": "Coluna inicial (todo, backlog, doing, review, done)",
                },
                "priority": {
                    "type": "integer",
                    "description": "Prioridade 1–3",
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Responsáveis (profile slugs)",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skills associadas ao card",
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace do artigo LaTeX de origem (opcional). Quando "
                        "informado com article_id, o card é vinculado ao artigo "
                        "(marcador) e o time é inferido pelo caminho do artigo."
                    ),
                },
                "article_id": {
                    "type": "string",
                    "description": "Arquivo .tex do artigo de origem (com workspace_id).",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_article_cards",
        "description": (
            "Lista os cards do Kanban vinculados a um artigo LaTeX específico "
            "(criados por create_card com workspace_id/article_id ou por "
            "evaluate_article_to_cards). Use para 'trazer de volta' as tarefas "
            "de um artigo e acompanhar coluna/estado. Varre todos os boards "
            "quando team_id/workdir não são informados."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace do artigo LaTeX",
                },
                "article_id": {
                    "type": "string",
                    "description": "Arquivo .tex do artigo (ex.: main.tex)",
                },
                "team_id": {
                    "type": "string",
                    "description": "Restringe a busca a um time (opcional)",
                },
                "workdir": {
                    "type": "string",
                    "description": "Restringe a busca a um board/workdir (opcional)",
                },
            },
            "required": ["workspace_id", "article_id"],
        },
    },
    {
        "name": "move_card",
        "description": (
            "Move um card para outra coluna do Kanban (ex: todo → doing, doing → done). "
            f"{_UI_IMPL_LOCATION_RULE}"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Caminho do projeto (workdir)",
                },
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time (ex: orientacoes-porto). Alternativa ao workdir.",
                },
                "card_id": {
                    "type": "string",
                    "description": "ID do card (ex: TASK-001)",
                },
                "column": {
                    "type": "string",
                    "description": "Coluna destino (backlog, todo, doing, testes-usabilidade, review, done)",
                },
                "result_comment": {
                    "type": "string",
                    "description": (
                        "Comentário de conclusão ao mover para done (opcional). "
                        "Registra o resultado nas notas do card."
                    ),
                },
                "implementation_location": {
                    "type": "string",
                    "description": (
                        "Caminho no menu da aplicação onde o usuário encontra a funcionalidade "
                        "(obrigatório para alterações de interface gráfica). "
                        "Ex.: 'Menu Operação & Infra > Monitor'."
                    ),
                },
            },
            "required": ["card_id", "column"],
        },
    },
    {
        "name": "update_card",
        "description": (
            "Atualiza campos de um card existente (título, descrição, notas, coluna, "
            "prioridade, assignees, skills, implementation_location, etc.). "
            "Envie apenas os campos que mudam. "
            f"{_UI_IMPL_LOCATION_RULE}"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {
                    "type": "string",
                    "description": "Caminho do projeto (workdir)",
                },
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time (ex: orientacoes-porto). Alternativa ao workdir.",
                },
                "card_id": {
                    "type": "string",
                    "description": "ID do card (ex: TASK-001)",
                },
                "title": {
                    "type": "string",
                    "description": "Novo título do card",
                },
                "description": {
                    "type": "string",
                    "description": "Nova descrição do card",
                },
                "notes": {
                    "type": "string",
                    "description": "Notas do card (substitui o valor anterior)",
                },
                "requirements": {
                    "type": "string",
                    "description": "Requisitos do card",
                },
                "implementation_details": {
                    "type": "string",
                    "description": "Detalhes técnicos da implementação",
                },
                "implementation_location": {
                    "type": "string",
                    "description": (
                        "Onde localizar a funcionalidade no menu da aplicação. "
                        "Obrigatório ao concluir alterações de interface gráfica. "
                        "Ex.: 'Menu Conhecimento > Memória'."
                    ),
                },
                "column": {
                    "type": "string",
                    "description": "Nova coluna (backlog, todo, doing, testes-usabilidade, review, done)",
                },
                "priority": {
                    "type": "integer",
                    "description": "Prioridade 1–3",
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Responsáveis (profile slugs; substitui a lista anterior)",
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skills associadas (substitui a lista anterior)",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "evaluate_article_to_cards",
        "description": (
            "Avalia um artigo LaTeX e transforma melhorias sugeridas em cards no Kanban do time. "
            "Aceita dry_run para pré-visualizar antes de criar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace de artigos LaTeX",
                },
                "article_id": {
                    "type": "string",
                    "description": "Arquivo .tex do artigo (ex: main.tex)",
                },
                "team_id": {
                    "type": "string",
                    "description": "ID do time (preferido)",
                },
                "workdir": {
                    "type": "string",
                    "description": "Workdir do board (alternativa ao team_id)",
                },
                "column": {
                    "type": "string",
                    "description": "Coluna inicial para cards (padrão: backlog)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de melhorias/cards (1-20)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Se true, só retorna preview sem criar cards",
                },
                "dedupe": {
                    "type": "boolean",
                    "description": "Evita duplicar melhorias já convertidas em card",
                },
                "assignee": {
                    "type": "string",
                    "description": "Assignee opcional para os cards gerados",
                },
            },
            "required": ["workspace_id", "article_id"],
        },
    },
    # ── Error log tools ────────────────────────────────────────────────────
    {
        "name": "get_errors",
        "description": (
            "Consulta todos os erros registrados pelo gois: linhas de erro "
            "dos logs monitorados (ERROR, CRITICAL, Traceback, etc.) e eventos de "
            "falha do monitor (health, recovery, doctor, cron, reaper). "
            "Suporta filtros por texto, categoria, fonte e janela de tempo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Máximo de erros a retornar (padrão: 100, máx: 2000)",
                },
                "query": {
                    "type": "string",
                    "description": "Filtrar erros contendo este texto (case-insensitive)",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Filtrar por categoria (ex: error_line, errors_log, "
                        "qclaw_health, hermes_health, openclaw_doctor, reaper)"
                    ),
                },
                "source": {
                    "type": "string",
                    "description": "Filtrar pela fonte/arquivo de log (substring do caminho)",
                },
                "since_minutes": {
                    "type": "number",
                    "description": "Retornar apenas erros dos últimos N minutos",
                },
            },
            "required": [],
        },
    },
    {
        "name": "errors_to_cards",
        "description": (
            "Transforma erros do gois (logs + eventos do monitor) em cards "
            "do Kanban. Deduplica por fingerprint, sugere skills/prioridade e grava "
            "no board do time. Use get_errors antes para inspecionar; dry_run=true "
            "para simular sem gravar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "ID/slug do time cujo kanban receberá os cards",
                },
                "workdir": {
                    "type": "string",
                    "description": "Workdir alternativo ao team_id",
                },
                "kanban_file": {
                    "type": "string",
                    "description": "Caminho explícito do kanban.yaml (opcional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de erros a transformar (padrão: 20, máx: 200)",
                },
                "query": {
                    "type": "string",
                    "description": "Filtrar erros contendo este texto",
                },
                "category": {
                    "type": "string",
                    "description": "Filtrar por categoria (ex: hermes_health, error_line)",
                },
                "source": {
                    "type": "string",
                    "description": "Filtrar pela fonte/arquivo de log",
                },
                "since_minutes": {
                    "type": "number",
                    "description": "Considerar apenas erros dos últimos N minutos",
                },
                "assignee": {
                    "type": "string",
                    "description": "Atribuir cards a um perfil Hermes (slug)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Simular criação sem gravar no kanban",
                },
                "dedupe": {
                    "type": "boolean",
                    "description": "Pular erros que já viraram card (padrão: true)",
                },
                "prefer_error_column": {
                    "type": "boolean",
                    "description": "Usar coluna 'error' se existir no board (padrão: true)",
                },
            },
            "required": [],
        },
    },
    # ── Article/LaTeX tools ────────────────────────────────────────────────
    {
        "name": "list_article_workspaces",
        "description": (
            "Lista os workspaces de artigos LaTeX cadastrados. "
            "Cada workspace é uma pasta com arquivos .tex."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_articles",
        "description": (
            "Lista artigos .tex de um workspace específico. "
            "Retorna título, caminho e se já tem PDF compilado."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace (obtido via list_article_workspaces)",
                },
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "read_article",
        "description": (
            "Lê o conteúdo de um artigo LaTeX (.tex). "
            "Retorna o texto do arquivo para análise, revisão ou alteração."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace",
                },
                "article_id": {
                    "type": "string",
                    "description": "ID do artigo (caminho relativo do .tex)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Máximo de caracteres a retornar (padrão: 50000)",
                },
            },
            "required": ["workspace_id", "article_id"],
        },
    },
    {
        "name": "list_bib_files",
        "description": (
            "Lista todos os arquivos .bib (referências/bibliografia) disponíveis no workspace. "
            "Retorna caminho, tamanho, e número de entradas para cada arquivo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace (obtido via list_article_workspaces). Se omitido, busca em todos.",
                },
                "include_details": {
                    "type": "boolean",
                    "description": "Se true, retorna preview e contagem de entradas de cada arquivo",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_articles",
        "description": (
            "Busca artigos por texto no conteúdo ou título. "
            "Varre todos os workspaces procurando matches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Texto a buscar nos artigos (case-insensitive)",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Filtrar por workspace específico (opcional)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "write_article",
        "description": (
            "Salva conteúdo em um artigo LaTeX (.tex). "
            "Pode ser usado para criar ou alterar artigos. "
            "Escreve o conteúdo completo no arquivo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace",
                },
                "article_id": {
                    "type": "string",
                    "description": "ID do artigo (caminho relativo do .tex)",
                },
                "content": {
                    "type": "string",
                    "description": "Conteúdo LaTeX completo para salvar no arquivo",
                },
            },
            "required": ["workspace_id", "article_id", "content"],
        },
    },
    {
        "name": "compile_article",
        "description": (
            "Compila um artigo LaTeX gerando o PDF. "
            "Usa latexmk ou pdflatex conforme disponível no sistema."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace",
                },
                "article_id": {
                    "type": "string",
                    "description": "ID do artigo (caminho relativo do .tex)",
                },
            },
            "required": ["workspace_id", "article_id"],
        },
    },
    {
        "name": "edit_article_tex",
        "description": (
            "Edita um artigo LaTeX (.tex) de forma estruturada, sem reescrever o arquivo inteiro. "
            "Ações: analyze (metadados/seções), set_metadata (título/autor/data), replace, "
            "edit_section, insert, set_lines, delete_match, page_info (contagem de páginas), "
            "get_bibliography (listar/verificar referências), set_bibliography (trocar .bib e/ou estilo)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "ID do workspace",
                },
                "article_id": {
                    "type": "string",
                    "description": "ID do artigo (caminho relativo do .tex)",
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "analyze",
                        "set_metadata",
                        "replace",
                        "edit_section",
                        "insert",
                        "set_lines",
                        "delete_match",
                        "page_info",
                        "get_bibliography",
                        "set_bibliography",
                    ],
                    "description": "Operação a executar no .tex",
                },
                "metadata": {
                    "type": "object",
                    "description": "Para set_metadata: title, author, date, titulo, autor, etc.",
                },
                "find": {
                    "type": "string",
                    "description": "Texto ou regex a localizar (replace/delete_match)",
                },
                "replace": {
                    "type": "string",
                    "description": "Substituição para replace",
                },
                "regex": {
                    "type": "boolean",
                    "description": "Tratar find/anchor como regex",
                },
                "count": {
                    "type": "integer",
                    "description": "Máximo de substituições (0 = todas)",
                },
                "section": {
                    "type": "string",
                    "description": "Título da seção para edit_section",
                },
                "content": {
                    "type": "string",
                    "description": "Novo conteúdo (edit_section, set_lines)",
                },
                "anchor": {
                    "type": "string",
                    "description": "Texto/regex de referência para insert",
                },
                "text": {
                    "type": "string",
                    "description": "Texto a inserir com insert",
                },
                "position": {
                    "type": "string",
                    "enum": ["before", "after"],
                    "description": "Posição relativa ao anchor (insert)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Linha inicial (1-based) para set_lines",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Linha final (1-based) para set_lines",
                },
                "bib_file": {
                    "type": "string",
                    "description": "Arquivo(s) .bib para usar (ex: 'references.bib' ou 'refs-1.bib,refs-2.bib'). Usado em set_bibliography.",
                },
                "bibstyle": {
                    "type": "string",
                    "description": "Estilo de citação/bibliografia (ex: 'plain', 'harvard', 'apalike', 'ieee', 'abbrv'). Usado em set_bibliography.",
                },
                "bib_system": {
                    "type": "string",
                    "enum": ["bibtex", "biblatex", "manual", "none"],
                    "description": "Sistema de referências: 'bibtex' (tradicional), 'biblatex' (moderno), 'manual' (thebibliography), 'none'. Usado em set_bibliography.",
                },
                "backup": {
                    "type": "boolean",
                    "description": "Criar .tex.bak antes de gravar (default true)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Simular sem gravar no disco",
                },
                "compile_after": {
                    "type": "boolean",
                    "description": "Recompilar PDF após editar ou forçar compile em page_info",
                },
            },
            "required": ["workspace_id", "article_id", "action"],
        },
    },
]

_TOOLS.extend(kanban_ide_handoff_mcp_specs())


def _tools_for_client() -> list[dict[str, Any]]:
    from .gois_lite import filter_lite_cards_mcp_tools, is_gois_lite

    if is_gois_lite():
        return filter_lite_cards_mcp_tools(_TOOLS)
    return _TOOLS


def _handle_list_kanban_boards(args: dict) -> dict:
    global _boards_list_cache

    quick = bool(args.get("quick"))
    include_hash = args.get("include_hash")
    if include_hash is None:
        include_hash = not quick
    sync = bool(args.get("sync"))
    cache_key = (quick, bool(include_hash), sync)
    if quick and not sync:
        cached = _boards_list_cache
        if cached and cached[0] > time.monotonic() - _BOARDS_LIST_TTL_SECONDS:
            if cached[1] == cache_key:
                return cached[2]

    boards = []

    # Standard workdir-level kanban files
    for workdir in _get_workdirs():
        kanban_file = _find_kanban_file(workdir)
        if kanban_file:
            if quick:
                total_cards = 0
                columns: list[dict[str, Any]] = []
            else:
                board = _load_kanban_yaml(kanban_file)
                total_cards = len(board.get("tasks", []))
                columns = board.get("columns", [])
            boards.append({
                "workdir": str(workdir),
                "kanban_file": str(kanban_file),
                "team_id": None,
                "columns": columns,
                "total_cards": total_cards,
            })

    # Team-level kanban files (.stack/accounts/teams/*)
    for team_dir, kanban_file, team_id in _get_team_kanban_files(
        sync=sync,
        include_hash=bool(include_hash),
    ):
        if quick:
            total_cards = 0
            columns = []
        else:
            board = _load_kanban_yaml(kanban_file)
            total_cards = len(board.get("tasks", []))
            columns = board.get("columns", [])
        boards.append({
            "workdir": str(team_dir),
            "kanban_file": str(kanban_file),
            "team_id": team_id,
            "columns": columns,
            "total_cards": total_cards,
        })

    # Assign sequential board_number to each board
    for i, b in enumerate(boards):
        b["board_number"] = i + 1

    result = {"ok": True, "boards": boards, "count": len(boards)}
    if quick and not sync:
        _boards_list_cache = (time.monotonic(), cache_key, result)
    return result


def _accounts_team_roots() -> list[Path]:
    """Team parent dirs: <workdir>/.stack/accounts/teams and config auth.data_dir/teams."""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(teams_dir: Path) -> None:
        if not teams_dir.is_dir():
            return
        key = str(teams_dir.resolve())
        if key in seen:
            return
        seen.add(key)
        roots.append(teams_dir)

    for wd in _get_workdirs():
        add(wd / ".stack" / "accounts" / "teams")

    try:
        config_path = _find_monitor_config()
        config_base = _config_base_dir()
        prev_cwd = Path.cwd()
        try:
            os.chdir(config_base)
            from .config import Config

            cfg = Config.load(config_path, auto_import=False)
        finally:
            try:
                os.chdir(prev_cwd)
            except OSError:
                pass
        auth_dir = Path(cfg.auth.data_dir).expanduser()
        if not auth_dir.is_absolute():
            auth_dir = (config_base / auth_dir).resolve()
        add(auth_dir / "teams")
    except Exception:
        pass
    return roots


def _resolve_team_workdir(team_id: str) -> Path | None:
    """Resolve a team_id to its on-disk team directory."""
    from .kanban_mongo import kanban_board_available, team_board_exists, canonical_team_id

    raw = str(team_id or "").strip()
    if not raw:
        return None
    for teams_dir in _accounts_team_roots():
        scope = str(teams_dir.parent.resolve())
        canonical = canonical_team_id(scope, raw)
        for candidate in (canonical, raw):
            team_dir = teams_dir / candidate
            if not team_dir.is_dir():
                continue
            kanban_file = team_dir / "kanban.yaml"
            if kanban_board_available(kanban_file):
                return team_dir.resolve()
            if team_board_exists(scope=scope, team_id=canonical):
                return team_dir.resolve()
    return None


def _invalidate_board_caches() -> None:
    global _boards_list_cache
    _kanban_yaml_cache.clear()
    _boards_list_cache = None


def _resolve_board_kanban_file(workdir_str: str) -> tuple[Path, Path] | None:
    """Resolve (workdir, kanban_file) when the board is in an allowed location."""
    from .kanban_mongo import board_exists_by_path, team_board_exists

    if not workdir_str:
        return None
    workdir = Path(workdir_str).expanduser().resolve()
    if not workdir.is_dir():
        return None

    for root in _get_workdirs():
        root = root.resolve()
        if workdir == root:
            kanban_file = _find_kanban_file(workdir)
            if kanban_file is not None:
                return (workdir, kanban_file)

        teams_root = root / ".stack" / "accounts" / "teams"
        if not teams_root.is_dir():
            continue
        try:
            workdir.relative_to(teams_root.resolve())
        except ValueError:
            continue
        kanban_file = workdir / "kanban.yaml"
        if kanban_file.is_file() or board_exists_by_path(kanban_file):
            return (workdir, kanban_file)
        scope = _accounts_scope_for_workdir(root)
        if scope and team_board_exists(scope=scope, team_id=workdir.name):
            return (workdir, kanban_file)

    return None


def _handle_delete_board(args: dict) -> dict:
    workdir_str = (args.get("workdir") or "").strip()
    team_id = (args.get("team_id") or "").strip()

    if team_id and not workdir_str:
        resolved = _resolve_team_workdir(team_id)
        if resolved is None:
            return {"ok": False, "error": f"Time '{team_id}' não encontrado"}
        workdir_str = str(resolved)

    if not workdir_str:
        return {"ok": False, "error": "workdir is required"}

    resolved = _resolve_board_kanban_file(workdir_str)
    if resolved is None:
        return {"ok": False, "error": "Board não encontrado ou caminho não permitido"}

    workdir, kanban_file = resolved
    from .kanban_mongo import delete_board_by_path, mongo_kanban_enabled

    if mongo_kanban_enabled():
        delete_board_by_path(kanban_file)
        try:
            if kanban_file.is_file():
                kanban_file.unlink()
        except OSError:
            pass
        _invalidate_board_caches()
        return {
            "ok": True,
            "workdir": str(workdir),
            "kanban_file": str(kanban_file),
            "deleted": True,
        }

    try:
        kanban_file.unlink()
        _invalidate_board_caches()
        return {
            "ok": True,
            "workdir": str(workdir),
            "kanban_file": str(kanban_file),
            "deleted": True,
        }
    except OSError as exc:
        return {"ok": False, "error": f"Erro ao apagar board: {exc}"}


def _handle_get_cards(args: dict) -> dict:
    workdir_str = args.get("workdir")
    team_id = (args.get("team_id") or "").strip()
    column_filter = (args.get("column") or "").strip().lower()
    assignee_filter = (args.get("assignee") or "").strip().lower()

    # Resolve workdir: team_id takes priority over workdir fallback
    if workdir_str:
        workdir = Path(workdir_str).expanduser().resolve()
    elif team_id:
        resolved = _resolve_team_workdir(team_id)
        if resolved is None:
            return {"ok": False, "error": f"Time '{team_id}' não encontrado"}
        workdir = resolved
    else:
        workdirs = _get_workdirs()
        workdir = workdirs[0] if workdirs else Path.cwd()

    kanban_file = _find_kanban_file(workdir)
    if not kanban_file:
        team_kanban = workdir / "kanban.yaml"
        if team_kanban.is_file():
            kanban_file = team_kanban
        else:
            from .kanban_mongo import canonical_team_id, team_board_exists

            scope = None
            for root in _get_workdirs():
                teams_root = root / ".stack" / "accounts" / "teams"
                try:
                    workdir.resolve().relative_to(teams_root.resolve())
                except ValueError:
                    continue
                scope = _accounts_scope_for_workdir(root)
                break
            canonical_tid = (
                canonical_team_id(scope, workdir.name)
                if scope
                else workdir.name
            )
            if scope and team_board_exists(scope=scope, team_id=canonical_tid):
                kanban_file = team_kanban
            else:
                return {"ok": False, "error": f"Nenhum kanban encontrado em {workdir}"}
    if not kanban_file:
        return {"ok": False, "error": f"Nenhum kanban encontrado em {workdir}"}

    board = _load_kanban_yaml(kanban_file)
    tasks = board.get("tasks", [])

    # Apply filters
    if column_filter:
        tasks = [t for t in tasks if t.get("column", "").lower() == column_filter]
    if assignee_filter:
        tasks = [
            t for t in tasks
            if any(assignee_filter in a.lower() for a in (t.get("assignees") or []))
        ]

    return {
        "ok": True,
        "workdir": str(workdir),
        "cards": tasks,
        "count": len(tasks),
        "columns": board.get("columns", []),
    }


def _handle_get_card_detail(args: dict) -> dict:
    card_id = (args.get("card_id") or "").strip()
    if not card_id:
        return {"ok": False, "error": "card_id is required"}

    workdir_str = args.get("workdir")
    team_id_arg = (args.get("team_id") or "").strip()

    # If team_id is provided, resolve to its specific workdir
    if team_id_arg and not workdir_str:
        resolved = _resolve_team_workdir(team_id_arg)
        if resolved is None:
            return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
        workdir_str = str(resolved)

    workdirs = [Path(workdir_str).expanduser().resolve()] if workdir_str else _get_workdirs()

    # Search in workdir-level boards
    for workdir in workdirs:
        kanban_file = _find_kanban_file(workdir)
        if not kanban_file:
            continue
        board = _load_kanban_yaml(kanban_file)
        for task in board.get("tasks", []):
            if task.get("id") == card_id:
                return {
                    "ok": True,
                    "card": task,
                    "workdir": str(workdir),
                    "kanban_file": str(kanban_file),
                }

    # Search in team-level boards
    for team_dir, kanban_file, team_id in _get_team_kanban_files():
        board = _load_kanban_yaml(kanban_file)
        for task in board.get("tasks", []):
            if task.get("id") == card_id:
                return {
                    "ok": True,
                    "card": task,
                    "workdir": str(team_dir),
                    "kanban_file": str(kanban_file),
                    "team_id": team_id,
                }

    return {"ok": False, "error": f"Card {card_id} não encontrado"}


def _handle_get_my_cards(args: dict) -> dict:
    assignee = (args.get("assignee") or "").strip().lower()
    if not assignee:
        return {"ok": False, "error": "assignee is required"}

    exclude_done = args.get("exclude_done", True)
    if isinstance(exclude_done, str):
        exclude_done = exclude_done.lower() in ("true", "1", "yes")

    all_cards = []

    # Search in workdir-level boards
    for workdir in _get_workdirs():
        kanban_file = _find_kanban_file(workdir)
        if not kanban_file:
            continue
        board = _load_kanban_yaml(kanban_file)
        for task in board.get("tasks", []):
            assignees = [a.lower() for a in (task.get("assignees") or [])]
            if assignee in assignees:
                if exclude_done and task.get("column", "").lower() == "done":
                    continue
                card = dict(task)
                card["_workdir"] = str(workdir)
                all_cards.append(card)

    # Search in team-level boards
    for team_dir, kanban_file, team_id in _get_team_kanban_files():
        board = _load_kanban_yaml(kanban_file)
        for task in board.get("tasks", []):
            assignees = [a.lower() for a in (task.get("assignees") or [])]
            if assignee in assignees:
                if exclude_done and task.get("column", "").lower() == "done":
                    continue
                card = dict(task)
                card["_workdir"] = str(team_dir)
                card["_team_id"] = team_id
                all_cards.append(card)

    return {"ok": True, "cards": all_cards, "count": len(all_cards)}


def _handle_get_cards_todo(args: dict) -> dict:
    workdir_str = args.get("workdir")
    team_id_arg = (args.get("team_id") or "").strip()

    # If team_id is provided, resolve to its specific workdir
    if team_id_arg and not workdir_str:
        resolved = _resolve_team_workdir(team_id_arg)
        if resolved is None:
            return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
        workdir_str = str(resolved)

    workdirs = [Path(workdir_str).expanduser().resolve()] if workdir_str else _get_workdirs()

    all_cards = []

    # Search in workdir-level boards
    for workdir in workdirs:
        kanban_file = _find_kanban_file(workdir)
        if not kanban_file:
            continue
        board = _load_kanban_yaml(kanban_file)
        for task in board.get("tasks", []):
            col = task.get("column", "").lower()
            if col in ("backlog", "todo", "a fazer"):
                card = dict(task)
                card["_workdir"] = str(workdir)
                all_cards.append(card)

    # Search in team-level boards (only when no specific workdir/team_id)
    if not workdir_str and not team_id_arg:
        for team_dir, kanban_file, team_id in _get_team_kanban_files():
            board = _load_kanban_yaml(kanban_file)
            for task in board.get("tasks", []):
                col = task.get("column", "").lower()
                if col in ("backlog", "todo", "a fazer"):
                    card = dict(task)
                    card["_workdir"] = str(team_dir)
                    card["_team_id"] = team_id
                    all_cards.append(card)

    return {"ok": True, "cards": all_cards, "count": len(all_cards)}


def _config_base_dir() -> Path:
    config_path = _find_monitor_config()
    if config_path is not None:
        return config_path.parent
    workdirs = _get_workdirs()
    return workdirs[0] if workdirs else Path.cwd()


def _load_hermes_create_cfg():
    from .config import Config

    config_path = _find_monitor_config()
    config_base = _config_base_dir()
    prev_cwd = Path.cwd()
    try:
        os.chdir(config_base)
        return Config.load(config_path, auto_import=False).hermes_agent_create
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass


def _board_locations_for_query(
    *,
    workdir_str: str = "",
    team_id: str = "",
) -> list[dict[str, Any]]:
    """Resolve kanban board locations for card create/move mutations."""
    locations: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(workdir: Path, team: str = "", kanban_file: Path | None = None) -> None:
        key = str(workdir.resolve())
        if key in seen:
            return
        seen.add(key)
        kf = kanban_file or (workdir / "kanban.yaml")
        locations.append(
            {
                "workdir": workdir,
                "team_id": team or workdir.name,
                "kanban_file": kf.name,
            }
        )

    if team_id and not workdir_str:
        resolved = _resolve_team_workdir(team_id)
        if resolved is None:
            return []
        from .kanban_mongo import canonical_team_id

        scope = None
        for teams_dir in _accounts_team_roots():
            scope = str(teams_dir.parent.resolve())
            break
        canonical = (
            canonical_team_id(scope, team_id) if scope else str(team_id or "").strip()
        )
        add(resolved, canonical, resolved / "kanban.yaml")
        return locations

    if workdir_str:
        resolved_board = _resolve_board_kanban_file(workdir_str)
        if resolved_board is not None:
            wd, kf = resolved_board
            add(wd, team_id or wd.name, kf)
            return locations
        wd = Path(workdir_str).expanduser().resolve()
        if wd.is_dir():
            kf = _find_kanban_file(wd)
            if kf is not None:
                add(wd, team_id, kf)
        return locations

    for wd in _get_workdirs():
        kf = _find_kanban_file(wd)
        if kf is not None:
            add(wd, "", kf)
    for team_dir, kanban_file, tid in _get_team_kanban_files():
        add(team_dir, tid, kanban_file)
    return locations


def _mutate_kanban_at_location(
    location: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    from .hermes_kanban import apply_kanban_action

    create_cfg = _load_hermes_create_cfg()
    workdir = location["workdir"]
    action_payload = {
        **payload,
        "kanban_file": location.get("kanban_file"),
        "team_id": location.get("team_id") or "",
    }
    board = apply_kanban_action(str(workdir), create_cfg, action_payload)
    _invalidate_board_caches()
    return board


def _infer_team_for_article(workspace_id: str, article_id: str) -> str:
    """Best-effort team_id for an article, derived from its on-disk path.

    Articles live under ``.../accounts/teams/<team_id>/...`` so the team owning
    an article (and therefore its Kanban board) can be recovered from the path
    even when the caller did not pass ``team_id``/``workdir``.
    """
    ws = str(workspace_id or "").strip()
    aid = str(article_id or "").strip()
    if not ws or not aid:
        return ""
    try:
        from .latex_articles import _resolve_article_tex

        _root, tex, _err = _resolve_article_tex(ws, aid)
        if tex is None:
            return ""
        parts = tex.resolve().parts
        for idx, part in enumerate(parts):
            if part == "teams" and idx + 1 < len(parts):
                return parts[idx + 1]
    except Exception:
        return ""
    return ""


def _stamp_article_marker(task: dict[str, Any], workspace_id: str, article_id: str,
                          improvement_id: str = "") -> str:
    """Append the article-eval marker to a task description so it can be
    brought back via :func:`_handle_list_article_cards`. Returns the marker."""
    fp = _article_eval_fingerprint(workspace_id, article_id)
    imp_id = str(improvement_id or "").strip()
    if not imp_id:
        import uuid

        imp_id = f"chat-{uuid.uuid4().hex[:8]}"
    marker = _article_eval_marker(fp, imp_id)
    desc = str(task.get("description") or "").strip()
    if marker not in desc:
        ref = (
            f"Workspace: {workspace_id}\n"
            f"Arquivo: {article_id}\n"
        )
        task["description"] = (f"{desc}\n\n{ref}\n{marker}").strip() if desc else f"{ref}\n{marker}"
    return marker


def _handle_create_card(args: dict) -> dict:
    title = str(args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}

    team_id_arg = str(args.get("team_id") or "").strip()
    workdir_str = str(args.get("workdir") or "").strip()
    # When a card is created from an article context (chat-LaTeX integration)
    # without an explicit board, infer the owning team from the article path so
    # the card lands on the right Kanban and stays linkable to the article.
    workspace_id = str(args.get("workspace_id") or "").strip()
    article_id = str(args.get("article_id") or "").strip()
    if not team_id_arg and not workdir_str and workspace_id and article_id:
        from .latex_kanban_integration import resolve_latex_article_team_id

        team_id_arg = resolve_latex_article_team_id(workspace_id, article_id, team_id_arg)
    locations = _board_locations_for_query(workdir_str=workdir_str, team_id=team_id_arg)
    if not locations:
        if team_id_arg:
            return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
        return {"ok": False, "error": "team_id ou workdir é necessário para criar card"}
    if not team_id_arg and not workdir_str and len(locations) > 1:
        return {
            "ok": False,
            "error": "team_id ou workdir é necessário (múltiplos boards encontrados)",
        }

    loc = locations[0]
    task: dict[str, Any] = {
        "title": title,
        "description": str(args.get("description") or "").strip(),
        "column": str(args.get("column") or "todo").strip().lower() or "todo",
    }
    if args.get("priority") is not None and str(args.get("priority")).strip() != "":
        try:
            task["priority"] = int(args.get("priority"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "priority must be an integer"}
    assignees = args.get("assignees")
    if isinstance(assignees, list):
        clean = [str(a).strip() for a in assignees if str(a).strip()]
        if clean:
            task["assignees"] = clean
    skills = args.get("skills")
    if isinstance(skills, list):
        clean_skills = [str(s).strip() for s in skills if str(s).strip()]
        if clean_skills:
            task["skills"] = clean_skills
    explicit_id = str(
        args.get("task_id") or args.get("card_id") or args.get("id") or ""
    ).strip()
    if explicit_id:
        task["id"] = explicit_id

    marker = ""
    if workspace_id and article_id:
        marker = _stamp_article_marker(
            task, workspace_id, article_id, str(args.get("improvement_id") or "")
        )

    try:
        result = _mutate_kanban_at_location(
            loc,
            {"action": "create_task", "task": task},
        )
        created_id = str(result.get("task_id") or "").strip()
        out: dict[str, Any] = {
            "ok": True,
            "task_id": created_id,
            "title": title,
            "column": task.get("column"),
            "workdir": str(loc["workdir"]),
            "team_id": team_id_arg or str(loc.get("team_id") or ""),
        }
        if marker:
            out["article_marker"] = marker
            out["workspace_id"] = workspace_id
            out["article_id"] = article_id
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _handle_list_article_cards(args: dict) -> dict:
    """Find every Kanban card linked to a given article via its eval marker."""
    workspace_id = str(args.get("workspace_id") or "").strip()
    article_id = str(args.get("article_id") or "").strip()
    if not workspace_id or not article_id:
        return {"ok": False, "error": "workspace_id and article_id are required"}

    team_id_arg = str(args.get("team_id") or "").strip()
    if not team_id_arg and workspace_id and article_id:
        from .latex_kanban_integration import resolve_latex_article_team_id

        team_id_arg = resolve_latex_article_team_id(workspace_id, article_id, team_id_arg)
    workdir_str = str(args.get("workdir") or "").strip()
    locations = _board_locations_for_query(workdir_str=workdir_str, team_id=team_id_arg)
    if not locations:
        return {"ok": False, "error": "nenhum board kanban encontrado"}

    article_fp = _article_eval_fingerprint(workspace_id, article_id)
    marker_prefix = f"[qclaw-article-eval:{article_fp}:"

    def _matches(task: dict[str, Any], current_team_id: str) -> bool:
        if team_id_arg and current_team_id == team_id_arg:
            return True
        for field in ("title", "description", "notes", "requirements"):
            if marker_prefix in str(task.get(field) or ""):
                return True
        return False

    cards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for loc in locations:
        try:
            board = _load_kanban_yaml(Path(loc["workdir"]) / str(loc["kanban_file"]))
        except Exception:
            continue
        team_id = str(loc.get("team_id") or "")
        for task in board.get("tasks") or []:
            if not isinstance(task, dict) or not _matches(task, team_id):
                continue
            cid = str(task.get("id") or "")
            dedupe_key = f"{team_id}:{cid}" if cid else f"{team_id}:{id(task)}"
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            cards.append(
                {
                    "id": cid,
                    "title": task.get("title") or "",
                    "column": task.get("column") or "",
                    "priority": task.get("priority"),
                    "assignees": task.get("assignees") or [],
                    "skills": task.get("skills") or [],
                    "team_id": team_id,
                    "workdir": str(loc.get("workdir") or ""),
                }
            )

    by_column: dict[str, int] = {}
    for c in cards:
        col = str(c.get("column") or "").lower() or "todo"
        by_column[col] = by_column.get(col, 0) + 1

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "article_id": article_id,
        "article_fingerprint": article_fp,
        "count": len(cards),
        "by_column": by_column,
        "cards": cards,
    }


def _handle_move_card(args: dict) -> dict:
    card_id = str(args.get("card_id") or args.get("task_id") or "").strip()
    column = str(args.get("column") or "").strip().lower()
    if not card_id or not column:
        return {"ok": False, "error": "card_id and column are required"}

    team_id_arg = str(args.get("team_id") or "").strip()
    workdir_str = str(args.get("workdir") or "").strip()
    locations = _board_locations_for_query(workdir_str=workdir_str, team_id=team_id_arg)
    if team_id_arg and not locations:
        return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
    if not locations:
        return {"ok": False, "error": "Nenhum board kanban encontrado"}

    impl_location = str(
        args.get("implementation_location")
        or args.get("app_location")
        or ""
    ).strip()
    result_comment = str(
        args.get("result_comment") or args.get("comment") or ""
    ).strip()

    last_error = ""
    for loc in locations:
        try:
            if impl_location:
                _mutate_kanban_at_location(
                    loc,
                    {
                        "action": "update_task",
                        "task_id": card_id,
                        "task": {"implementation_location": impl_location},
                    },
                )
            if column == "done" and result_comment:
                payload: dict[str, Any] = {
                    "action": "complete_task",
                    "task_id": card_id,
                    "result_comment": result_comment,
                }
            else:
                payload = {
                    "action": "move_task",
                    "task_id": card_id,
                    "column": column,
                }
            _mutate_kanban_at_location(loc, payload)
            out: dict[str, Any] = {
                "ok": True,
                "card_id": card_id,
                "task_id": card_id,
                "new_column": column,
                "workdir": str(loc["workdir"]),
                "team_id": str(loc.get("team_id") or team_id_arg or ""),
            }
            if impl_location:
                out["implementation_location"] = impl_location
            if result_comment:
                out["result_comment"] = result_comment
            return out
        except ValueError as exc:
            last_error = str(exc)
            if "não encontrada" in last_error:
                continue
            return {"ok": False, "error": last_error}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": False, "error": last_error or f"Card {card_id} não encontrado"}


def _build_update_card_patch(args: dict) -> dict[str, Any]:
    """Extract only fields explicitly provided for update_task."""
    patch: dict[str, Any] = {}
    text_fields = (
        "title",
        "description",
        "notes",
        "requirements",
        "implementation_details",
        "implementation_location",
    )
    for field in text_fields:
        if field not in args:
            continue
        patch[field] = str(args.get(field) or "").strip()
    if "app_location" in args and "implementation_location" not in patch:
        patch["implementation_location"] = str(args.get("app_location") or "").strip()
    if "column" in args:
        patch["column"] = str(args.get("column") or "").strip().lower()
    if args.get("priority") is not None and str(args.get("priority")).strip() != "":
        patch["priority"] = int(args.get("priority"))
    assignees = args.get("assignees")
    if isinstance(assignees, list):
        patch["assignees"] = [str(a).strip() for a in assignees if str(a).strip()]
    skills = args.get("skills")
    if isinstance(skills, list):
        patch["skills"] = [str(s).strip() for s in skills if str(s).strip()]
    return patch


def _handle_update_card(args: dict) -> dict:
    card_id = str(args.get("card_id") or args.get("task_id") or "").strip()
    if not card_id:
        return {"ok": False, "error": "card_id is required"}

    try:
        task_patch = _build_update_card_patch(args)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if not task_patch:
        return {
            "ok": False,
            "error": "at least one field to update is required",
        }

    team_id_arg = str(args.get("team_id") or "").strip()
    workdir_str = str(args.get("workdir") or "").strip()
    locations = _board_locations_for_query(workdir_str=workdir_str, team_id=team_id_arg)
    if team_id_arg and not locations:
        return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
    if not locations:
        return {"ok": False, "error": "Nenhum board kanban encontrado"}

    last_error = ""
    for loc in locations:
        try:
            _mutate_kanban_at_location(
                loc,
                {
                    "action": "update_task",
                    "task_id": card_id,
                    "task": task_patch,
                },
            )
            return {
                "ok": True,
                "card_id": card_id,
                "task_id": card_id,
                "updated_fields": sorted(task_patch.keys()),
                "workdir": str(loc["workdir"]),
                "team_id": str(loc.get("team_id") or team_id_arg or ""),
            }
        except ValueError as exc:
            last_error = str(exc)
            if "não encontrada" in last_error:
                continue
            return {"ok": False, "error": last_error}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": False, "error": last_error or f"Card {card_id} não encontrado"}


# ── Error log handlers ────────────────────────────────────────────────────────

def _find_monitor_config() -> Path | None:
    """Locate the gois config.yaml (env override or workdir scan)."""
    env_val = os.environ.get("QCLAW_MONITOR_CONFIG", "").strip()
    if env_val:
        candidate = Path(env_val).expanduser()
        if candidate.is_file():
            return candidate
    for wd in _get_workdirs():
        candidate = wd / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def _handle_get_errors(args: dict) -> dict:
    from .config import Config
    from .error_log import collect_errors
    from .state import MonitorState

    config_path = _find_monitor_config()
    # When there's no YAML file, config may still live in MongoDB. Use the
    # first workdir as the base for resolving relative paths in that case.
    config_base = config_path.parent if config_path else None
    if config_base is None:
        workdirs = _get_workdirs()
        config_base = workdirs[0] if workdirs else Path.cwd()
        try:
            from .config import Config as _Cfg

            if _Cfg.from_mongo() is None:
                return {
                    "ok": False,
                    "error": (
                        "config do gois não encontrado (nem MongoDB nem "
                        "config.yaml). Defina QCLAW_MONITOR_CONFIG/QCLAW_KANBAN_WORKDIRS "
                        "ou importe o config para o MongoDB."
                    ),
                }
        except Exception as exc:
            return {"ok": False, "error": f"Erro ao acessar config no MongoDB: {exc}"}

    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 2000))

    query = str(args.get("query") or "").strip().lower()
    category = str(args.get("category") or "").strip().lower()
    source = str(args.get("source") or "").strip().lower()
    try:
        since_minutes = float(args.get("since_minutes") or 0)
    except (TypeError, ValueError):
        since_minutes = 0.0

    # Relative paths in config.yaml (logs, state file) are resolved against
    # the config dir, mirroring how the monitor runs from the repo root.
    prev_cwd = Path.cwd()
    try:
        os.chdir(config_base)
        # Read-only: never seed Mongo from a discovered workdir config (tests use temp YAML).
        cfg = Config.load(config_path, auto_import=False)
        state_path = (
            Path(cfg.state.path).expanduser().resolve() if cfg.state.path else None
        )
        state = MonitorState.load(state_path)
        result = collect_errors(cfg, state, limit=2000)
    except Exception as exc:
        return {"ok": False, "error": f"Erro ao coletar erros: {exc}"}
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass

    if not result.get("ok"):
        return result

    errors = result.get("errors", [])
    if since_minutes > 0:
        cutoff = time.time() - since_minutes * 60
        errors = [e for e in errors if float(e.get("ts_epoch") or 0) >= cutoff]
    if category:
        errors = [e for e in errors if category in str(e.get("category") or "").lower()]
    if source:
        errors = [
            e for e in errors
            if source in str(e.get("source") or "").lower()
            or source in str(e.get("source_label") or "").lower()
        ]
    if query:
        errors = [e for e in errors if query in str(e.get("line") or "").lower()]
    errors = errors[:limit]

    summary = dict(result.get("summary") or {})
    summary["returned"] = len(errors)
    return {
        "ok": True,
        "generated_at": result.get("generated_at"),
        "config": str(config_path),
        "summary": summary,
        "sources": result.get("sources", []),
        "missing_paths": result.get("missing_paths", []),
        "errors": errors,
    }


def _load_monitor_state_for_errors(cfg) -> "MonitorState":
    from .state import MonitorState

    state_path = Path(cfg.state.path).expanduser().resolve() if cfg.state.path else None
    return MonitorState.load(state_path)


def _handle_errors_to_cards(args: dict) -> dict:
    from .config import Config
    from .error_cards import transform_errors_to_cards

    config_path = _find_monitor_config()
    config_base = config_path.parent if config_path else None
    if config_base is None:
        workdirs = _get_workdirs()
        config_base = workdirs[0] if workdirs else Path.cwd()
        try:
            if Config.from_mongo() is None:
                return {
                    "ok": False,
                    "error": (
                        "config do gois não encontrado. Defina "
                        "QCLAW_MONITOR_CONFIG ou QCLAW_KANBAN_WORKDIRS."
                    ),
                }
        except Exception as exc:
            return {"ok": False, "error": f"Erro ao acessar config: {exc}"}

    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    try:
        since_minutes = float(args.get("since_minutes") or 0)
    except (TypeError, ValueError):
        since_minutes = 0.0

    prev_cwd = Path.cwd()
    try:
        os.chdir(config_base)
        cfg = Config.load(config_path, auto_import=False)
        state = _load_monitor_state_for_errors(cfg)
        result = transform_errors_to_cards(
            cfg,
            state,
            team_id=str(args.get("team_id") or "").strip(),
            workdir=str(args.get("workdir") or "").strip(),
            kanban_file=str(args.get("kanban_file") or "").strip(),
            limit=limit,
            query=str(args.get("query") or "").strip(),
            category=str(args.get("category") or "").strip(),
            source=str(args.get("source") or "").strip(),
            since_minutes=since_minutes,
            dry_run=bool(args.get("dry_run")),
            dedupe=bool(args.get("dedupe", True)),
            assignee=str(args.get("assignee") or "").strip(),
            prefer_error_column=bool(args.get("prefer_error_column", True)),
            search_roots=_get_workdirs(),
            config_base=config_base,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Erro ao carregar config: {exc}"}
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass

    if result.get("ok"):
        _invalidate_board_caches()
    return result


def _article_eval_fingerprint(workspace_id: str, article_id: str) -> str:
    raw = f"{workspace_id.strip().lower()}::{article_id.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _article_eval_marker(article_fp: str, improvement_id: str) -> str:
    return f"[qclaw-article-eval:{article_fp}:{improvement_id}]"


def _task_has_article_marker(task: dict[str, Any], marker: str) -> bool:
    for field in ("title", "description", "notes", "requirements"):
        if marker in str(task.get(field) or ""):
            return True
    return False


def _has_section(sections: list[dict[str, Any]], *needles: str) -> bool:
    wanted = [n.strip().lower() for n in needles if n.strip()]
    if not wanted:
        return False
    for sec in sections:
        title = str(sec.get("title") or "").strip().lower()
        if not title:
            continue
        if any(n in title for n in wanted):
            return True
    return False


def _build_article_improvements(
    content: str,
    analysis: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lower = content.lower()
    sections = [s for s in (analysis.get("sections") or []) if isinstance(s, dict)]
    metadata = dict(analysis.get("metadata") or {})

    citation_count = lower.count("\\cite{") + lower.count("\\citep{") + lower.count("\\citet{")
    has_bibliography = (
        "\\bibliography{" in lower
        or "\\printbibliography" in lower
        or "\\begin{thebibliography}" in lower
    )
    figure_count = lower.count("\\begin{figure") + lower.count("\\includegraphics")
    table_count = lower.count("\\begin{table")

    improvements: list[dict[str, Any]] = []

    if not metadata.get("title"):
        improvements.append(
            {
                "id": "metadata-title",
                "title": "Definir título claro do artigo",
                "description": "Adicionar ou revisar o campo \\title para refletir contribuição e escopo do trabalho.",
                "priority": 1,
                "skills": ["paper-writing", "section-writing-agent"],
            }
        )

    if not metadata.get("author"):
        improvements.append(
            {
                "id": "metadata-author",
                "title": "Preencher autoria do manuscrito",
                "description": "Incluir \\author (e afiliações quando aplicável) para completar metadados de submissão.",
                "priority": 1,
                "skills": ["paper-writing", "journal-submission-responses"],
            }
        )

    has_abstract = "\\begin{abstract}" in lower or _has_section(sections, "resumo", "abstract")
    if not has_abstract:
        improvements.append(
            {
                "id": "missing-abstract",
                "title": "Adicionar resumo estruturado",
                "description": "Criar resumo com contexto, método, resultados quantitativos e conclusão principal.",
                "priority": 1,
                "skills": ["paper-writing", "section-writing-agent"],
            }
        )

    if not _has_section(sections, "introdu", "introduction"):
        improvements.append(
            {
                "id": "missing-introduction",
                "title": "Fortalecer seção de introdução",
                "description": "Adicionar problema, lacuna da literatura e objetivo explícito do artigo.",
                "priority": 1,
                "skills": ["outline-agent", "literature-review-agent"],
            }
        )

    if not _has_section(sections, "metod", "method"):
        improvements.append(
            {
                "id": "missing-method",
                "title": "Detalhar metodologia",
                "description": "Descrever pipeline experimental, dados, métricas e procedimentos de avaliação.",
                "priority": 1,
                "skills": ["section-writing-agent", "paper-writing"],
            }
        )

    has_results = _has_section(sections, "resultado", "results", "experim")
    if not has_results:
        improvements.append(
            {
                "id": "missing-results",
                "title": "Adicionar resultados e experimentos",
                "description": "Incluir resultados com métricas comparativas e análise crítica dos achados.",
                "priority": 1,
                "skills": ["section-writing-agent", "plotting-agent"],
            }
        )

    if not _has_section(sections, "conclus", "conclusion"):
        improvements.append(
            {
                "id": "missing-conclusion",
                "title": "Consolidar conclusão e próximos passos",
                "description": "Encerrar com contribuições, limitações e agenda de trabalho futuro.",
                "priority": 2,
                "skills": ["content-refinement-agent", "paper-writing"],
            }
        )

    if citation_count < 6:
        improvements.append(
            {
                "id": "low-citations",
                "title": "Ampliar base bibliográfica",
                "description": f"Quantidade de citações detectada ({citation_count}) está baixa. Expandir Related Work com referências verificadas.",
                "priority": 2,
                "skills": ["literature-review-agent", "paper-autoraters"],
            }
        )

    if not has_bibliography:
        improvements.append(
            {
                "id": "missing-bibliography",
                "title": "Configurar seção de referências",
                "description": "Adicionar bloco de bibliografia (BibTeX/BibLaTeX) e conferir estilo de citação exigido.",
                "priority": 1,
                "skills": ["paper-writing", "journal-submission-responses"],
            }
        )

    if figure_count + table_count == 0:
        improvements.append(
            {
                "id": "missing-figures-tables",
                "title": "Incluir figuras ou tabelas de evidência",
                "description": "Adicionar visualizações/tabelas para sustentar os principais resultados quantitativos.",
                "priority": 2,
                "skills": ["plotting-agent", "section-writing-agent"],
            }
        )

    metrics = {
        "line_count": int(analysis.get("line_count") or 0),
        "section_count": int(analysis.get("section_count") or 0),
        "citation_count": citation_count,
        "has_bibliography": has_bibliography,
        "figure_count": figure_count,
        "table_count": table_count,
    }
    return improvements, metrics


def _handle_evaluate_article_to_cards(args: dict) -> dict:
    workspace_id = str(args.get("workspace_id") or "").strip()
    article_id = str(args.get("article_id") or "").strip()
    if not workspace_id or not article_id:
        return {"ok": False, "error": "workspace_id and article_id are required"}

    team_id_arg = str(args.get("team_id") or "").strip()
    workdir_str = str(args.get("workdir") or "").strip()
    locations = _board_locations_for_query(workdir_str=workdir_str, team_id=team_id_arg)
    if team_id_arg and not locations:
        return {"ok": False, "error": f"Time '{team_id_arg}' não encontrado"}
    if not locations:
        return {"ok": False, "error": "team_id ou workdir é necessário para criar cards"}
    if not team_id_arg and not workdir_str and len(locations) > 1:
        return {
            "ok": False,
            "error": "team_id ou workdir é necessário (múltiplos boards encontrados)",
        }

    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 20))

    dry_run = bool(args.get("dry_run"))
    dedupe = bool(args.get("dedupe", True))
    assignee = str(args.get("assignee") or "").strip()
    column = str(args.get("column") or "backlog").strip().lower() or "backlog"

    try:
        from .latex_articles import _resolve_article_tex
        from .latex_tex_edit import analyze_tex

        _root, tex, err = _resolve_article_tex(workspace_id, article_id)
        if tex is None or not tex.is_file():
            return {"ok": False, "error": err or "artigo não encontrado"}
        content = tex.read_text(encoding="utf-8", errors="replace")
        analysis = analyze_tex(content)
    except Exception as exc:
        return {"ok": False, "error": f"Erro ao avaliar artigo: {exc}"}

    improvements, metrics = _build_article_improvements(content, analysis)
    improvements = improvements[:limit]
    article_fp = _article_eval_fingerprint(workspace_id, article_id)

    loc = locations[0]
    board = _load_kanban_yaml(Path(loc["kanban_file"]))
    existing_tasks = [
        t for t in (board.get("tasks") or []) if isinstance(t, dict)
    ]

    preview: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for imp in improvements:
        imp_id = str(imp.get("id") or "improvement").strip()
        marker = _article_eval_marker(article_fp, imp_id)
        if dedupe and any(_task_has_article_marker(task, marker) for task in existing_tasks):
            skipped.append(
                {
                    "id": imp_id,
                    "title": str(imp.get("title") or "").strip(),
                    "reason": "already_exists",
                }
            )
            continue

        description = (
            f"Melhoria sugerida pela avaliação automática do artigo.\n\n"
            f"Workspace: {workspace_id}\n"
            f"Arquivo: {article_id}\n\n"
            f"{str(imp.get('description') or '').strip()}\n\n"
            f"{marker}"
        )
        task: dict[str, Any] = {
            "title": f"[Artigo] {str(imp.get('title') or '').strip()}",
            "description": description,
            "column": column,
            "priority": int(imp.get("priority") or 2),
            "skills": [
                str(s).strip() for s in (imp.get("skills") or []) if str(s).strip()
            ],
        }
        if assignee:
            task["assignees"] = [assignee]

        row = {
            "improvement_id": imp_id,
            "title": task["title"],
            "column": column,
            "priority": task["priority"],
            "skills": task.get("skills") or [],
            "marker": marker,
            "dry_run": dry_run,
        }
        preview.append(row)

        if dry_run:
            continue

        try:
            out = _mutate_kanban_at_location(
                loc,
                {"action": "create_task", "task": task},
            )
            created.append({
                **row,
                "task_id": str(out.get("task_id") or "").strip(),
            })
            existing_tasks.append(task)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            skipped.append({
                "id": imp_id,
                "title": task["title"],
                "reason": row["error"],
            })

    if created:
        _invalidate_board_caches()

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "article_id": article_id,
        "article_path": str(tex),
        "team_id": team_id_arg or str(loc.get("team_id") or ""),
        "workdir": str(loc.get("workdir") or ""),
        "column": column,
        "dry_run": dry_run,
        "analysis": analysis,
        "metrics": metrics,
        "generated_count": len(improvements),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "preview": preview,
        "created": created,
        "skipped": skipped,
    }


# ── Article handlers ──────────────────────────────────────────────────────────

def _handle_list_article_workspaces(args: dict) -> dict:
    try:
        from .latex_articles import list_workspaces
        workspaces = list_workspaces()
        return {"ok": True, "workspaces": workspaces, "count": len(workspaces)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_list_articles(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "workspace_id is required"}
    try:
        from .latex_articles import list_articles
        return list_articles(workspace_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_read_article(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    article_id = (args.get("article_id") or "").strip()
    max_chars = args.get("max_chars", 50000)
    if not workspace_id or not article_id:
        return {"ok": False, "error": "workspace_id and article_id are required"}
    try:
        from .latex_articles import _resolve_article_tex
        root, tex, err = _resolve_article_tex(workspace_id, article_id)
        if tex is None:
            return {"ok": False, "error": err or "artigo não encontrado"}
        content = tex.read_text(encoding="utf-8", errors="replace")
        if isinstance(max_chars, int) and max_chars > 0:
            content = content[:max_chars]
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "path": str(tex),
            "content": content,
            "length": len(content),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_search_articles(args: dict) -> dict:
    query = (args.get("query") or "").strip().lower()
    workspace_filter = (args.get("workspace_id") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    try:
        from .latex_articles import list_workspaces, list_articles, _resolve_article_tex
        workspaces = list_workspaces()
        if workspace_filter:
            workspaces = [w for w in workspaces if w.get("id") == workspace_filter]

        results = []
        for ws in workspaces:
            wid = ws.get("id", "")
            if not ws.get("exists"):
                continue
            articles = list_articles(wid)
            if not articles.get("ok"):
                continue
            for art in articles.get("articles", []):
                aid = art.get("id", "")
                title = art.get("title", "")
                # Match on title first
                if query in title.lower():
                    results.append({
                        "workspace_id": wid,
                        "workspace_name": ws.get("name", ""),
                        "article_id": aid,
                        "title": title,
                        "match_in": "title",
                    })
                    continue
                # Search in content (first 8000 chars)
                _, tex, _ = _resolve_article_tex(wid, aid)
                if tex and tex.is_file():
                    try:
                        content = tex.read_text(encoding="utf-8", errors="replace")[:8000].lower()
                        if query in content:
                            idx = content.find(query)
                            start = max(0, idx - 60)
                            end = min(len(content), idx + len(query) + 60)
                            snippet = content[start:end].replace("\n", " ").strip()
                            results.append({
                                "workspace_id": wid,
                                "workspace_name": ws.get("name", ""),
                                "article_id": aid,
                                "title": title,
                                "match_in": "content",
                                "snippet": f"...{snippet}...",
                            })
                    except OSError:
                        pass

        return {"ok": True, "results": results, "count": len(results), "query": query}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_write_article(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    article_id = (args.get("article_id") or "").strip()
    content = args.get("content", "")
    if not workspace_id or not article_id:
        return {"ok": False, "error": "workspace_id and article_id are required"}
    if not content:
        return {"ok": False, "error": "content is required"}
    try:
        from .latex_articles import _resolve_workspace_root, _is_under
        root, err = _resolve_workspace_root(workspace_id)
        if root is None:
            return {"ok": False, "error": err or "workspace não encontrado"}

        aid = article_id.replace("\\", "/").lstrip("/")
        if ".." in Path(aid).parts:
            return {"ok": False, "error": "caminho inválido"}
        tex = (root / aid).resolve(strict=False)
        if not _is_under(tex, root):
            return {"ok": False, "error": "caminho fora do workspace"}
        if tex.suffix.lower() != ".tex":
            return {"ok": False, "error": "arquivo deve ter extensão .tex"}

        tex.parent.mkdir(parents=True, exist_ok=True)
        tex.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "path": str(tex),
            "bytes_written": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_compile_article(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    article_id = (args.get("article_id") or "").strip()
    if not workspace_id or not article_id:
        return {"ok": False, "error": "workspace_id and article_id are required"}
    try:
        from .latex_articles import compile_article
        return compile_article(workspace_id, article_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_list_bib_files(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    include_details = bool(args.get("include_details", False))
    
    try:
        from .latex_articles import list_workspaces, _resolve_workspace_root
        
        workspaces = []
        if workspace_id:
            root, err = _resolve_workspace_root(workspace_id)
            if root is None:
                return {"ok": False, "error": err or f"workspace '{workspace_id}' não encontrado"}
            workspaces = [{"id": workspace_id, "root": root}]
        else:
            all_ws = list_workspaces()
            for ws in all_ws:
                if ws.get("exists"):
                    root, _ = _resolve_workspace_root(ws.get("id", ""))
                    if root:
                        workspaces.append({"id": ws.get("id"), "root": root})
        
        bib_files: list[dict[str, Any]] = []
        
        for ws in workspaces:
            root = ws["root"]
            for bib_path in root.rglob("*.bib"):
                try:
                    size = bib_path.stat().st_size
                    
                    entry_count = 0
                    preview = ""
                    
                    if include_details:
                        try:
                            content = bib_path.read_text(encoding="utf-8", errors="replace")
                            # Count @article, @book, @inproceedings, etc.
                            entry_count = len(re.findall(r"@\w+\s*\{", content))
                            # Get first 200 chars as preview
                            preview = content[:200].replace("\n", " ").strip()
                        except OSError:
                            pass
                    
                    bib_files.append({
                        "workspace_id": ws["id"],
                        "path": str(bib_path.relative_to(root)),
                        "full_path": str(bib_path),
                        "name": bib_path.name,
                        "size_bytes": size,
                        "entry_count": entry_count if include_details else None,
                        "preview": preview if include_details else None,
                    })
                except OSError:
                    pass
        
        return {
            "ok": True,
            "workspace_id": workspace_id or "all",
            "bib_files": bib_files,
            "count": len(bib_files),
            "include_details": include_details,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _handle_edit_article_tex(args: dict) -> dict:
    workspace_id = (args.get("workspace_id") or "").strip()
    article_id = (args.get("article_id") or "").strip()
    action = (args.get("action") or "").strip()
    if not workspace_id or not article_id or not action:
        return {"ok": False, "error": "workspace_id, article_id and action are required"}
    try:
        from .latex_tex_edit import edit_article_tex

        passthrough = {
            k: v
            for k, v in args.items()
            if k
            not in {
                "workspace_id",
                "article_id",
                "action",
                "backup",
                "dry_run",
                "compile_after",
            }
        }
        return edit_article_tex(
            workspace_id,
            article_id,
            action=action,
            backup=bool(args.get("backup", True)),
            dry_run=bool(args.get("dry_run")),
            compile_after=bool(args.get("compile_after")),
            **passthrough,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Team swarm handlers ───────────────────────────────────────────────────────

def _handle_list_teams(args: dict) -> dict:
    from .team_swarm_ops import list_teams

    return list_teams()


def _handle_team_swarm_status(args: dict) -> dict:
    from .team_swarm_ops import team_swarm_status

    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return {"ok": False, "error": "team_id is required"}
    return team_swarm_status(team_id)


def _handle_run_team_swarm(args: dict) -> dict:
    from .team_swarm_ops import run_team_swarm

    team_id = (args.get("team_id") or "").strip()
    if not team_id:
        return {"ok": False, "error": "team_id is required"}
    return run_team_swarm(
        team_id,
        objective=str(args.get("objective") or "").strip(),
        force=bool(args.get("force")),
        run_all=bool(args.get("run_all", True)),
        use_team_cards=bool(args.get("use_team_cards", True)),
    )


# ─── Tool dispatcher ──────────────────────────────────────────────────────────

_HANDLERS = {
    "list_teams": _handle_list_teams,
    "team_swarm_status": _handle_team_swarm_status,
    "run_team_swarm": _handle_run_team_swarm,
    "list_kanban_boards": _handle_list_kanban_boards,
    "get_cards": _handle_get_cards,
    "get_card_detail": _handle_get_card_detail,
    "get_my_cards": _handle_get_my_cards,
    "get_cards_todo": _handle_get_cards_todo,
    "create_card": _handle_create_card,
    "list_article_cards": _handle_list_article_cards,
    "move_card": _handle_move_card,
    "update_card": _handle_update_card,
    "evaluate_article_to_cards": _handle_evaluate_article_to_cards,
    "get_errors": _handle_get_errors,
    "errors_to_cards": _handle_errors_to_cards,
    "list_article_workspaces": _handle_list_article_workspaces,
    "list_articles": _handle_list_articles,
    "read_article": _handle_read_article,
    "search_articles": _handle_search_articles,
    "write_article": _handle_write_article,
    "compile_article": _handle_compile_article,
    "list_bib_files": _handle_list_bib_files,
    "edit_article_tex": _handle_edit_article_tex,
    "kanban_ide_handoff": dispatch_kanban_ide_handoff,
}


# ─── JSON-RPC over stdio (MCP protocol) ──────────────────────────────────────

def _make_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_request(msg: dict) -> dict | None:
    """Process a single JSON-RPC request."""
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # ── Initialize
    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": _SERVER_NAME,
                "version": _SERVER_VERSION,
            },
        })

    # ── Notifications (no response needed)
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        return None

    # ── List tools
    if method == "tools/list":
        return _make_response(req_id, {"tools": _tools_for_client()})

    # ── Call tool
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}

        handler = _HANDLERS.get(tool_name)
        if not handler:
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"Tool desconhecida: {tool_name}"})}],
                "isError": True,
            })

        from .gois_lite import is_gois_lite, lite_cards_mcp_tool_allowed

        if is_gois_lite() and not lite_cards_mcp_tool_allowed(tool_name):
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"Tool não disponível no gois-lite: {tool_name}"})}],
                "isError": True,
            })

        try:
            result = handler(arguments)
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                "isError": not result.get("ok", False),
            })
        except Exception as exc:
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)})}],
                "isError": True,
            })

    # ── Ping
    if method == "ping":
        return _make_response(req_id, {})

    # ── Unknown method
    return _make_error(req_id, -32601, f"Method not found: {method}")


def run_stdio_server() -> None:
    """Run MCP server over stdio (JSON-RPC line-delimited)."""
    log.info("qclaw-cards MCP server starting (stdio)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            err = _make_error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
            continue

        response = _handle_request(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="qclaw-cards MCP Server")
    parser.add_argument(
        "--workdir",
        help="Adicionar workdir ao scan de kanban boards (pode repetir)",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    # Inject workdirs into env if provided via CLI
    if args.workdir:
        existing = os.environ.get("QCLAW_KANBAN_WORKDIRS", "")
        extra = ":".join(str(Path(w).expanduser().resolve()) for w in args.workdir)
        os.environ["QCLAW_KANBAN_WORKDIRS"] = f"{existing}:{extra}".strip(":")

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    run_stdio_server()

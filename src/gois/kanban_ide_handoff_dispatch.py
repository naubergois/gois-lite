"""MCP + chat dispatch for kanban IDE handoff."""

from __future__ import annotations

from typing import Any

from .kanban_ide_handoff_ops import kanban_ide_handoff_dispatch


def dispatch_kanban_ide_handoff(args: dict[str, Any]) -> dict[str, Any]:
    """Shared handler for MCP `kanban_ide_handoff` and chat `qclaw_kanban_ide_handoff`."""
    payload = dict(args or {})
    action = str(payload.pop("action", None) or "handoff").strip().lower()
    return kanban_ide_handoff_dispatch(action, payload)


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "kanban_ide_handoff",
            "description": (
                "[Uso diário] Prepara um card do Kanban para desenvolvimento numa IDE "
                "assistida (Kiro, Cursor, VS Code, Antigravity): move para doing, "
                "materializa contexto em arquivos da IDE e abre a ferramenta. "
                "Chat: qclaw_kanban_ide_handoff. Skill: qclaw-chat-kanban-ide-handoff."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["handoff", "list_backends", "suggest_ide"],
                        "description": "handoff (padrão) | list_backends | suggest_ide",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID do card (ex.: T-123, TASK-042)",
                    },
                    "ide": {
                        "type": "string",
                        "enum": [
                            "kiro",
                            "cursor",
                            "vscode",
                            "copilot",
                            "antigravity",
                        ],
                        "description": "IDE destino",
                    },
                    "team_id": {"type": "string", "description": "ID do time"},
                    "team_name": {
                        "type": "string",
                        "description": "Nome parcial do time",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Workdir do kanban (alternativa a team_id)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Caminho absoluto do repositório (opcional)",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Só materializar contexto, sem mover nem abrir IDE",
                    },
                    "open_ide": {
                        "type": "boolean",
                        "description": "Abrir IDE via CLI (default true)",
                    },
                    "task": {
                        "type": "object",
                        "description": "Card completo (apenas para action=suggest_ide)",
                    },
                },
                "required": [],
            },
        }
    ]

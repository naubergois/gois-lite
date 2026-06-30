"""OpenClaw chat tool JSON schemas for LLM function calling."""

from __future__ import annotations

import platform
from typing import Any, Optional

from .config import OpenclawChatConfig
from .recovery import Recovery

_tool_catalog_cache: Optional[list[dict[str, Any]]] = None
_tool_catalog_cache_key: Optional[tuple] = None

def _desktop_control_enabled_for_host(chat_cfg: OpenclawChatConfig) -> bool:
    return bool(chat_cfg.desktop_control_enabled and platform.system() == "Darwin")


def _desktop_control_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_screen_info",
                "description": (
                    "Return macOS screen size (logical pixels) and whether cliclick is installed."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_list_windows",
                "description": (
                    "List visible windows with app name, title, position and size "
                    "(logical pixels)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Optional filter by application name",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_open_app",
                "description": "Open and optionally focus a macOS application.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string", "description": "Application name"},
                        "focus": {
                            "type": "boolean",
                            "description": "Bring app to foreground (default true)",
                        },
                    },
                    "required": ["app"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_screenshot",
                "description": (
                    "Capture the macOS screen, a region, or a specific app window. "
                    "Returns image_path and image_data_url for the dashboard. "
                    "Always screenshot before clicking when you need visual context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Capture this app's front window bounds",
                        },
                        "window_title": {
                            "type": "string",
                            "description": "Optional substring to pick a window title",
                        },
                        "x": {"type": "integer", "description": "Region top-left X"},
                        "y": {"type": "integer", "description": "Region top-left Y"},
                        "width": {"type": "integer", "description": "Region width"},
                        "height": {"type": "integer", "description": "Region height"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_click",
                "description": (
                    "Click at screen coordinates (logical pixels). "
                    "Requires cliclick (brew install cliclick)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                        },
                        "double": {"type": "boolean"},
                    },
                    "required": ["x", "y"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_type",
                "description": "Type text into the focused app (or focus app first).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "app": {
                            "type": "string",
                            "description": "Optional app to focus before typing",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_key",
                "description": (
                    "Press a key or combo (e.g. return, cmd+s, ctrl+c, alt+tab)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string"},
                        "app": {
                            "type": "string",
                            "description": "Optional app to focus before key press",
                        },
                    },
                    "required": ["keys"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_desktop_scroll",
                "description": "Scroll at screen coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Scroll steps (default 3)",
                        },
                    },
                    "required": ["x", "y"],
                },
            },
        },
    ]


def _swarm_manage_tool_specs() -> list[dict[str, Any]]:
    """Tool specs for Hermes swarm CRUD (qclaw-chat-swarm-manage)."""
    agent_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string"},
            "instructions": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "handoff_to": {"type": "array", "items": {"type": "string"}},
        },
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_list",
                "description": (
                    "Lista swarms Hermes salvos (.stack/swarms). Use para 'listar swarms', "
                    "'quais enxames existem', antes de editar ou excluir."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_get",
                "description": (
                    "Detalhes completos de um swarm: agentes, handoffs, diagrama ASCII, "
                    "última execução do grafo."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome/slug do swarm"},
                        "include_graph": {
                            "type": "boolean",
                            "description": "Incluir graph_view (default true)",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_create",
                "description": (
                    "Cria definição de swarm (shell ou com agentes/perfis). "
                    "Para swarm executável com crons Hermes, prefira qclaw_create_openai_swarm."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome do swarm (slug)"},
                        "description": {"type": "string"},
                        "topology": {
                            "type": "string",
                            "enum": ["handoff", "pipeline", "broadcast", "team"],
                        },
                        "entry_agent": {"type": "string"},
                        "hermes_profiles": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "agents": {"type": "array", "items": agent_schema},
                        "team_id": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_update",
                "description": (
                    "Edita swarm existente: renomear, topologia, agentes, entry_agent, perfis."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome atual do swarm"},
                        "new_name": {"type": "string", "description": "Renomear swarm"},
                        "description": {"type": "string"},
                        "topology": {
                            "type": "string",
                            "enum": ["handoff", "pipeline", "broadcast", "team"],
                        },
                        "entry_agent": {"type": "string"},
                        "hermes_profiles": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "agents": {"type": "array", "items": agent_schema},
                        "team_id": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_delete",
                "description": (
                    "Remove definição do swarm (.json). Perfis Hermes permanecem. "
                    "Confirme com o usuário antes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome do swarm"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_health",
                "description": (
                    "Auditoria de saúde do swarm: crons, perfis, grafo, entry_agent, score."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome do swarm"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_topology",
                "description": (
                    "Diagrama ASCII + handoffs + graph_view. Use para 'topologia', "
                    "'mapa de agentes', 'diagrama do swarm'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome do swarm"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_design",
                "description": (
                    "Mostra o design do swarm no chat: diagrama ASCII, grafo de agentes "
                    "com status da última execução, handoffs e painel markdown pronto. "
                    "Use para 'design do swarm', 'mostrar swarm', 'visualizar enxame', "
                    "'estrutura do swarm', 'mapa de agentes'. Sem name: catálogo + overview."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Nome do swarm (opcional — vazio = visão geral)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_swarm_set_model",
                "description": (
                    "Troca o modelo LLM de agentes Hermes num swarm. "
                    "Sem `agent`: aplica o mesmo modelo a todos os robôs LLM do swarm. "
                    "Com `agent` (slug ou display_name): altera só esse agente. "
                    "Use quando pedir 'trocar modelo do swarm', 'usar Claude/GPT/DeepSeek "
                    "no swarm X', 'mudar LLM do agente Y'. "
                    "Prefira `qclaw_list_chat_models` antes para validar o model_id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "swarm_name": {
                            "type": "string",
                            "description": "Nome/slug do swarm",
                        },
                        "model_id": {
                            "type": "string",
                            "description": (
                                "ID do modelo (ex: deepseek-chat, gpt-4o-mini, "
                                "claude-sonnet-4-20250514)"
                            ),
                        },
                        "agent": {
                            "type": "string",
                            "description": (
                                "Opcional: slug Hermes ou display_name de um agente. "
                                "Omita para aplicar a todos."
                            ),
                        },
                    },
                    "required": ["swarm_name", "model_id"],
                },
            },
        },
    ]


def _jobs_manage_tool_specs() -> list[dict[str, Any]]:
    """Tool specs for running jobs + Hermes cron schedule (qclaw-chat-jobs-manage)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_health",
                "description": (
                    "Saúde unificada dos jobs: execuções em andamento (chat, kanban, tools, "
                    "cron) + agenda Hermes cron (ativos, pausados, erros). Preferir quando o "
                    "pedido for 'saúde dos jobs', 'status geral', 'jobs com erro' ou diagnóstico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Opcional: focar num job específico",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_token_mode",
                "description": (
                    "Gerir modos de consumo de tokens no chat QClaw "
                    "(economy/balanced/full/debug): status, trocar modo, comparar limites, "
                    "listar crons Hermes com contexto pesado."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "status",
                                "set",
                                "list",
                                "compare",
                                "cron_heavy",
                                "cron_repair",
                                "cron_stale",
                            ],
                            "description": (
                                "status=modo atual; set=trocar; compare=tabela; "
                                "cron_heavy=jobs caros; cron_repair=reagendar presos; "
                                "cron_stale=listar presos"
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["economy", "balanced", "full", "debug"],
                            "description": "Novo modo (action=set)",
                        },
                        "min_avg_tokens": {
                            "type": "integer",
                            "description": "Limiar para cron_heavy (default 50000)",
                        },
                        "persist": {
                            "type": "boolean",
                            "description": "Gravar em config ao trocar modo (default true)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_list_running",
                "description": (
                    "Lista jobs em execução agora: chat async, kanban, ferramentas e "
                    "crons Hermes. Use para 'o que está rodando', 'jobs ativos'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_get_running",
                "description": "Estado de um job em execução pelo job_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cancel",
                "description": (
                    "Para/cancela execução em andamento (chat, kanban, fila, swarm). "
                    "Não remove agendamento cron — use qclaw_jobs_cron_action remove."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "source": {
                            "type": "string",
                            "description": "chat, kanban_schedule, priority_queue, swarm_test",
                        },
                        "swarm_name": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cancel_all_batches",
                "description": (
                    "Lista ou cancela todos os batches em execução (slides image_batch, "
                    "PDF preview, book_pipeline, swarm_run). Sem confirm só mostra preview; "
                    "com confirm=true cancela todos. Skill: qclaw-chat-batch-kill-all."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "true para cancelar todos os batches ativos",
                        },
                        "host": {
                            "type": "string",
                            "description": "URL do gois (default http://127.0.0.1:9101)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cron_list",
                "description": (
                    "Lista jobs agendados no Hermes cron (ativos, pausados, em execução). "
                    "Use para 'agenda', 'crons', 'jobs agendados'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Filtrar por id"},
                        "query": {"type": "string", "description": "Busca nome/perfil/schedule"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cron_get",
                "description": "Detalhes de um job cron agendado.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cron_action",
                "description": (
                    "Ação num cron: pause (tirar da agenda ativa), resume (reativar), "
                    "run (executar agora), remove (apagar agendamento)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["pause", "resume", "run", "remove"],
                        },
                    },
                    "required": ["job_id", "action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cron_create",
                "description": (
                    "Agenda novo job Hermes cron. schedule: '0 9 * * *', 'every 2h', '30m'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule": {"type": "string"},
                        "prompt": {"type": "string"},
                        "name": {"type": "string"},
                        "profile": {"type": "string"},
                        "workdir": {"type": "string"},
                        "skills": {"type": "array", "items": {"type": "string"}},
                        "repeat": {"type": "integer"},
                    },
                    "required": ["schedule", "prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_jobs_cron_edit",
                "description": "Edita schedule, nome, prompt ou perfil de um cron.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "schedule": {"type": "string"},
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "profile": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
        },
    ]


def _monitor_update_tool_specs() -> list[dict[str, Any]]:
    """Git pull + monitor restart for gois self-update (qclaw-chat-monitor-update)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_monitor_update",
                "description": (
                    "Atualiza o repositório gois (git pull --rebase + submodules) "
                    "e/ou reinicia o monitor via scripts/restart.sh. Ações: status (git), "
                    "pull, restart, pull_and_restart. Use quando pedir 'atualizar git', "
                    "'git pull', 'reiniciar monitor', 'reiniciar gois' ou "
                    "'atualizar e reiniciar'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "status",
                                "pull",
                                "restart",
                                "pull_and_restart",
                                "update",
                            ],
                            "description": (
                                "status=git status; pull=git pull; restart=LaunchAgent/foreground; "
                                "pull_and_restart|update=atualizar código e reiniciar"
                            ),
                        },
                        "branch": {
                            "type": "string",
                            "description": "Branch opcional antes do pull",
                        },
                        "reinstall": {
                            "type": "boolean",
                            "description": "No restart: pip install -e . antes de reiniciar",
                        },
                        "submodules": {
                            "type": "boolean",
                            "description": "Atualizar submodules após pull (padrão: true)",
                        },
                        "force": {
                            "type": "boolean",
                            "description": (
                                "Com working tree suja: faz stash, pull e restaura stash"
                            ),
                        },
                        "auto_stash": {
                            "type": "boolean",
                            "description": (
                                "Stash automático antes do pull e restore depois "
                                "(padrão true em pull_and_restart)"
                            ),
                        },
                    },
                    "required": ["action"],
                },
            },
        },
    ]


def _kanban_ide_handoff_tool_specs() -> list[dict[str, Any]]:
    """Kanban card → IDE handoff (qclaw-chat-kanban-ide-handoff)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_kanban_ide_handoff",
                "description": (
                    "Prepara um card do Kanban para desenvolvimento numa IDE assistida "
                    "(Kiro, Cursor, VS Code, Antigravity): move para doing, materializa "
                    "contexto em arquivos da IDE e abre a ferramenta. Use quando pedir "
                    "'desenvolver card no Cursor/Kiro', 'abrir card na IDE', "
                    "'executar tarefa no VS Code/Antigravity'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["handoff", "list_backends", "suggest_ide"],
                            "description": "handoff (padrão) | list_backends | suggest_ide",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "ID do card (ex.: T-123)",
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
                            "description": "IDE destino (opcional — infere do card)",
                        },
                        "team_id": {"type": "string", "description": "ID do time"},
                        "workdir": {
                            "type": "string",
                            "description": "Workdir do kanban (alternativa a team_id)",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Só materializar contexto, sem mover nem abrir IDE",
                        },
                        "open_ide": {
                            "type": "boolean",
                            "description": "Abrir IDE via CLI (default true)",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        }
    ]


def _app_passwords_tool_specs() -> list[dict[str, Any]]:
    """Application passwords stored in MongoDB env_keys (qclaw-app-passwords)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_app_passwords_list",
                "description": (
                    "Lista senhas de app configuradas (Gmail, Google Calendar, SMTP, WordPress). "
                    "Valores ficam mascarados no MongoDB (env_keys), não em SQLite nem project memory. "
                    "Use quando o usuário perguntar quais senhas de app estão configuradas."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Filtrar por variável: GMAIL_APP_PASSWORD, GOOGLE_APP_PASSWORD, "
                                "TEAM_SMTP_PASSWORD, EMAIL_PASSWORD, WORDPRESS_APP_PASSWORD"
                            ),
                        },
                        "configured_only": {
                            "type": "boolean",
                            "description": "Só as já configuradas (padrão: false)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_app_passwords_store",
                "description": (
                    "Grava senha de app no MongoDB (env_keys) e aplica ao ambiente do monitor. "
                    "Use quando o usuário pedir 'armazenar/guardar/configurar senha de app', "
                    "colar os 16 caracteres do Google (com ou sem espaços), ou configurar Gmail IMAP/SMTP. "
                    "NUNCA use qclaw_save_project_note para secrets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Variável alvo (padrão: GMAIL_APP_PASSWORD). "
                                "Outras: GOOGLE_APP_PASSWORD, TEAM_SMTP_PASSWORD, "
                                "EMAIL_PASSWORD, WORDPRESS_APP_PASSWORD"
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": "Senha de app (16 letras Google; espaços são removidos)",
                        },
                    },
                    "required": ["value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_app_passwords_delete",
                "description": (
                    "Remove uma senha de app do MongoDB (env_keys). "
                    "Use quando o usuário pedir apagar/remover senha de app."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Variável a remover (ex.: GMAIL_APP_PASSWORD)",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
    ]


def _google_oauth_tool_specs() -> list[dict[str, Any]]:
    """Google OAuth tokens in MongoDB oauth_tokens (qclaw-google-oauth)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_oauth_status",
                "description": (
                    "Status da autorização OAuth Google (token + credentials) no MongoDB. "
                    "Use quando o usuário perguntar se Google Calendar/Fotos está autenticado, "
                    "se o token OAuth existe, ou antes de sincronizar calendário."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_oauth_list",
                "description": (
                    "Lista artefatos OAuth Google (token e credentials) sem expor secrets. "
                    "Use para ver o que está no MongoDB vs disco (.stack/calendar)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "Filtrar: token ou credentials",
                        },
                        "configured_only": {
                            "type": "boolean",
                            "description": "Só os já configurados",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_oauth_upload",
                "description": (
                    "Envia google_token.json ou google_credentials.json local para o MongoDB "
                    "(coleção oauth_tokens) e espelha em .stack/calendar/. "
                    "Use após baixar credentials do Google Cloud Console."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Caminho absoluto do JSON (token ou credentials)",
                        },
                        "kind": {
                            "type": "string",
                            "description": "token (padrão) ou credentials",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_oauth_download",
                "description": (
                    "Baixa OAuth Google do MongoDB para disco (.stack/calendar/). "
                    "Use para restaurar token em outra máquina ou backup."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "token (padrão) ou credentials",
                        },
                        "output": {
                            "type": "string",
                            "description": "Caminho de destino (opcional)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_oauth_migrate",
                "description": (
                    "Importa arquivos OAuth de .stack/calendar/ para o MongoDB (oauth_tokens). "
                    "Use na primeira configuração ou após upgrade do monitor."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]


def _aws_manage_tool_specs() -> list[dict[str, Any]]:
    """Tool specs for AWS cost and machines (qclaw-chat-aws-manage)."""
    common = {
        "profile": {
            "type": "string",
            "description": "Perfil AWS CLI (default: AWS_PROFILE)",
        },
        "region": {
            "type": "string",
            "description": "Região AWS (default: AWS_DEFAULT_REGION)",
        },
    }

    def _fn(name: str, description: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        props = dict(common)
        if extra:
            props.update(extra)
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [],
                },
            },
        }

    return [
        _fn(
            "qclaw_aws_overview",
            (
                "Visão unificada AWS: identidade, custo MTD, EC2/RDS e desperdícios. "
                "Preferir para 'custo AWS', 'máquinas AWS', 'ambiente AWS'. "
                "Skill: qclaw-chat-aws-manage."
            ),
        ),
        _fn(
            "qclaw_aws_cost",
            (
                "Custos AWS por serviço (Cost Explorer). Skill: qclaw-chat-aws-manage."
            ),
            {
                "days": {"type": "integer", "description": "Janela em dias (default 30)"},
                "month_to_date": {
                    "type": "boolean",
                    "description": "Só mês corrente (MTD)",
                },
                "limit": {"type": "integer", "description": "Top N serviços"},
            },
        ),
        _fn(
            "qclaw_aws_ce_get_cost_and_usage",
            (
                "Cost Explorer get-cost-and-usage com período, métrica e group-by "
                "(ex.: BlendedCost por SERVICE). Skill: qclaw-chat-aws-ce-get-cost-and-usage."
            ),
            {
                "start": {"type": "string", "description": "Início YYYY-MM-DD (inclusivo)"},
                "end": {
                    "type": "string",
                    "description": "Fim YYYY-MM-DD (exclusivo, como AWS CLI)",
                },
                "granularity": {
                    "type": "string",
                    "enum": ["DAILY", "MONTHLY", "HOURLY"],
                },
                "metric": {"type": "string", "description": "Métrica CE (default BlendedCost)"},
                "group_by_type": {"type": "string", "enum": ["DIMENSION", "TAG"]},
                "group_by_key": {
                    "type": "string",
                    "description": "Dimensão ou tag (SERVICE, REGION, …)",
                },
                "limit": {"type": "integer", "description": "Top N grupos"},
            },
        ),
        _fn(
            "qclaw_aws_machines",
            (
                "Lista EC2/RDS ou start/stop instância (confirm=true). "
                "Skill: qclaw-chat-aws-manage."
            ),
            {
                "service": {"type": "string", "enum": ["ec2", "rds"]},
                "state": {
                    "type": "string",
                    "description": "Filtrar EC2: running, stopped…",
                },
                "instance_id": {"type": "string"},
                "action": {"type": "string", "enum": ["start", "stop"]},
                "confirm": {
                    "type": "boolean",
                    "description": "Obrigatório para start/stop",
                },
            },
        ),
        _fn(
            "qclaw_aws_waste",
            (
                "Desperdícios AWS: EBS órfãos, EIPs soltos, EC2 paradas. "
                "Skill: qclaw-chat-aws-manage."
            ),
        ),
        _fn(
            "qclaw_aws_env",
            (
                "Snapshots do ambiente AWS (scan/list/get). Skill: qclaw-chat-aws-manage."
            ),
            {
                "mode": {"type": "string", "enum": ["list", "scan", "get"]},
                "label": {"type": "string", "description": "Etiqueta do snapshot (scan)"},
                "snapshot_id": {"type": "string", "description": "ID (mode=get)"},
                "limit": {"type": "integer"},
            },
        ),
    ]


def _team_files_search_tool_specs() -> list[dict[str, Any]]:
    """Search files across team folders (qclaw-chat-team-files-search)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_files_search",
                "description": (
                    "Procura arquivos em todas as pastas de um time: local_path, "
                    "artifacts_dir, workspace, docs e anexos. Use só para *listar*, "
                    "*achar* ou *onde está* — não para download (use "
                    "qclaw_team_files_download). Skill: qclaw-chat-team-files-search."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "ID do time"},
                        "team_name": {
                            "type": "string",
                            "description": "Nome parcial do time",
                        },
                        "query": {
                            "type": "string",
                            "description": "Substring no nome do arquivo",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob no nome (ex.: *.md)",
                        },
                        "extension": {
                            "type": "string",
                            "description": "Extensão sem ponto (md, pdf, tex)",
                        },
                        "path": {
                            "type": "string",
                            "description": "Pasta explícita (ignora time)",
                        },
                        "all_teams": {
                            "type": "boolean",
                            "description": "Buscar em todos os times",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default 50)",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Profundidade máxima (default 8)",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def _team_files_send_tool_specs() -> list[dict[str, Any]]:
    """Send team files by email or WhatsApp (qclaw-chat-team-files-send)."""
    file_props = {
        "path": {"type": "string", "description": "Caminho absoluto do arquivo"},
        "relative_path": {
            "type": "string",
            "description": "Caminho relativo nas pastas do time",
        },
        "query": {
            "type": "string",
            "description": "Substring no nome (deve ser único)",
        },
        "pattern": {
            "type": "string",
            "description": "Glob no nome (ex.: relatorio-*.pdf)",
        },
        "extension": {
            "type": "string",
            "description": "Extensão sem ponto (pdf, md, zip)",
        },
        "files": {
            "type": "array",
            "description": "Lista de paths ou objetos com path/relative_path/query",
            "items": {"type": "object"},
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Lista de caminhos absolutos",
        },
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_files_send",
                "description": (
                    "Envia arquivo(s) das pastas do time por **email** ou **WhatsApp** "
                    "com mensagem. Resolve path/relative_path/query como download. "
                    "Email: `to`, `subject` (opcional), `message`, múltiplos anexos. "
                    "WhatsApp: `to` (allowlist) ou `team_group: true` para grupo do time; "
                    "`message` como legenda/corpo. Não use para só baixar (use "
                    "qclaw_team_files_download). Skill: qclaw-chat-team-files-send."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {
                            "type": "string",
                            "enum": ["email", "whatsapp"],
                            "description": "Canal de envio",
                        },
                        "team_id": {"type": "string", "description": "ID do time"},
                        "team_name": {
                            "type": "string",
                            "description": "Nome parcial do time",
                        },
                        "to": {
                            "type": "string",
                            "description": "Email ou destino WhatsApp (allowlist)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Mensagem / corpo / legenda",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Assunto do email (opcional)",
                        },
                        "team_group": {
                            "type": "boolean",
                            "description": "WhatsApp: enviar ao grupo vinculado ao time",
                        },
                        "mode": {
                            "type": "string",
                            "description": "WhatsApp: auto | document | preview",
                        },
                        **file_props,
                    },
                    "required": ["channel"],
                },
            },
        },
    ]


def _team_files_download_tool_specs() -> list[dict[str, Any]]:
    """Download team files (qclaw-chat-team-files-download)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_files_download",
                "description": (
                    "Prepara **download** de um arquivo das pastas do time (local_path, "
                    "workspace, docs, artifacts, anexos). Use quando pedirem *baixar*, "
                    "*download*, *enviar ficheiro*, *trazer PDF* — chame direto, sem listar "
                    "opções. Retorna link 📎; não renderiza páginas (preview = "
                    "qclaw_show_slides_pdf só se pedirem ver/mostrar). "
                    "Skill: qclaw-chat-team-files-download."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "ID do time"},
                        "team_name": {
                            "type": "string",
                            "description": "Nome parcial do time",
                        },
                        "path": {
                            "type": "string",
                            "description": "Caminho absoluto do arquivo",
                        },
                        "relative_path": {
                            "type": "string",
                            "description": "Caminho relativo nas pastas do time",
                        },
                        "query": {
                            "type": "string",
                            "description": "Substring no nome (deve ser único)",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob no nome (ex.: relatorio-*.pdf)",
                        },
                        "extension": {
                            "type": "string",
                            "description": "Extensão sem ponto (pdf, md, zip)",
                        },
                        "max_bytes": {
                            "type": "integer",
                            "description": "Limite de tamanho em bytes (default 200MB)",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def _email_team_pdf_tool_specs() -> list[dict[str, Any]]:
    """Gmail PDF attachments → team normas (qclaw-chat-email-team-pdf)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
    }
    filter_props = {
        "mailbox": {"type": "string", "description": "Pasta IMAP (default INBOX)"},
        "subject": {"type": "string", "description": "Filtrar por assunto"},
        "from": {"type": "string", "description": "Filtrar por remetente"},
        "ext": {
            "type": "string",
            "description": "Filtrar extensão (ex: pdf, docx). Vazio = tipos suportados em normas/",
        },
        "days": {
            "type": "integer",
            "description": "Últimos N dias (default 30 quando sem uid/assunto/remetente)",
        },
        "limit": {"type": "integer", "description": "Máximo de emails a processar"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_team_pdf_list",
                "description": (
                    "Lista emails do Gmail com anexos para normas/ do time (sem baixar). "
                    "Use para 'listar PDFs no email', 'ver anexos do gmail para o time'. "
                    "Skill: qclaw-chat-email-team-pdf."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(filter_props),
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_team_pdf_save",
                "description": (
                    "Baixa anexos do Gmail e salva em normas/ do time "
                    "(PDF, DOCX, MD, etc.; indexação ChromaDB automática). "
                    "Use para 'guardar anexo do email no time', 'salvar PDF do "
                    "email nos documentos do time'. Requer team_id/team_name "
                    "(ou time selecionado na sessão). Skill: qclaw-chat-email-team-pdf."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        **filter_props,
                        "uid": {
                            "type": "string",
                            "description": "UID de um email específico (opcional)",
                        },
                        "name_prefix": {
                            "type": "string",
                            "description": "Prefixo opcional no nome do arquivo salvo",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Apenas simular sem gravar no time",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def _gmail_attachments_tool_specs() -> list[dict[str, Any]]:
    """Gmail attachments list/download (qclaw-chat-gmail-attachments)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time (salva em workspace/subdir)"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
        "subdir": {
            "type": "string",
            "description": "Subpasta no workspace do time (default: entregas)",
        },
    }
    filter_props = {
        "mailbox": {"type": "string", "description": "Pasta IMAP (default INBOX)"},
        "uid": {"type": "string", "description": "UID de um email específico (opcional)"},
        "subject": {"type": "string", "description": "Filtrar por assunto"},
        "from": {"type": "string", "description": "Filtrar por remetente"},
        "ext": {
            "type": "string",
            "description": "Filtrar por extensão (ex: pdf, xlsx, zip, ipynb)",
        },
        "days": {
            "type": "integer",
            "description": "Últimos N dias (opcional)",
        },
        "limit": {"type": "integer", "description": "Máximo de emails a processar"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_gmail_attachments_list",
                "description": (
                    "Lista emails do Gmail com anexos (sem baixar). Use para "
                    "'procurar anexos no email', 'listar anexos do gmail', "
                    "'ver emails com anexo'. Skill: qclaw-chat-gmail-attachments."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(filter_props),
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_gmail_attachments_download",
                "description": (
                    "Baixa anexos do Gmail e salva em disco local (qualquer extensão). "
                    "Use para 'baixar anexos', 'salvar arquivos do email', "
                    "'descarregar anexos'. Com team_id/team_name (ou time da sessão) "
                    "grava em workspace/entregas/ e retorna link 📎. Para PDFs em "
                    "normas/ use qclaw_email_team_pdf_save. Skill: qclaw-chat-gmail-attachments."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **filter_props,
                        **team_props,
                        "output_dir": {
                            "type": "string",
                            "description": (
                                "Diretório destino explícito (default: .stack/downloads/email)"
                            ),
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Apenas simular sem gravar arquivos",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


def _trello_tool_specs() -> list[dict[str, Any]]:
    """Trello REST API (qclaw-chat-trello / qclaw-chat-trello-sync)."""
    board_props = {
        "board_id": {"type": "string", "description": "ID do board Trello"},
        "board_name": {"type": "string", "description": "Nome do board (alternativa ao ID)"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_connect",
                "description": (
                    "Testa conexão Trello e lista boards abertos. "
                    "Use para 'conectar trello', 'testar trello'. Skill: qclaw-chat-trello."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_boards",
                "description": (
                    "Lista boards Trello do usuário. Skill: qclaw-chat-trello."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_closed": {
                            "type": "boolean",
                            "description": "Incluir boards arquivados",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_board_detail",
                "description": (
                    "Detalha listas e cards de um board Trello. Skill: qclaw-chat-trello."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(board_props),
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_card_create",
                "description": (
                    "Cria card no Trello. Skill: qclaw-chat-trello."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "list_id": {"type": "string", "description": "ID da lista Trello"},
                        "name": {"type": "string", "description": "Título do card"},
                        "desc": {"type": "string", "description": "Descrição (opcional)"},
                    },
                    "required": ["list_id", "name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_card_move",
                "description": (
                    "Move card Trello para outra lista. Skill: qclaw-chat-trello."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "ID do card"},
                        "list_id": {"type": "string", "description": "ID da lista destino"},
                    },
                    "required": ["card_id", "list_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_kanban_sync",
                "description": (
                    "Sincroniza cards Trello → Kanban QClaw (dedupe por título). "
                    "Use dry_run=true primeiro. Skill: qclaw-chat-trello-sync."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **board_props,
                        "team_id": {
                            "type": "string",
                            "description": "ID do time/projeto QClaw",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Simular sem criar cards (default true)",
                        },
                        "dedupe_by_title": {
                            "type": "boolean",
                            "description": "Ignorar títulos já existentes no Kanban",
                        },
                    },
                    "required": ["team_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_trello_json_import",
                "description": (
                    "Importa cards de JSON exportado do Trello (Export as JSON) para o "
                    "Kanban QClaw. Não requer API Trello. Use dry_run=true primeiro. "
                    "Skill: qclaw-chat-trello-json-import."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trello_json": {
                            "type": "object",
                            "description": "Objeto JSON exportado do Trello",
                        },
                        "json_path": {
                            "type": "string",
                            "description": "Caminho absoluto do arquivo .json exportado",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID do time/projeto QClaw",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Simular sem criar cards (default true)",
                        },
                        "dedupe_by_title": {
                            "type": "boolean",
                            "description": "Ignorar títulos já existentes no Kanban",
                        },
                    },
                    "required": ["team_id"],
                },
            },
        },
    ]


def _team_rules_tool_specs() -> list[dict[str, Any]]:
    """Team rules in MongoDB (qclaw-chat-team-rules)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_rule_store",
                "description": (
                    "Cria ou atualiza regra memorizada do time no MongoDB. "
                    "Use para 'definir regra', 'memorizar política', 'guardar convenção'. "
                    "Skill: qclaw-chat-team-rules."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "title": {"type": "string", "description": "Título curto da regra"},
                        "content": {"type": "string", "description": "Texto completo da regra"},
                        "category": {
                            "type": "string",
                            "description": "codigo|processo|comunicacao|deploy|review|geral",
                        },
                        "priority": {"type": "string", "description": "alta|media|baixa"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "active": {"type": "boolean"},
                        "rule_id": {
                            "type": "integer",
                            "description": "ID para atualizar regra existente",
                        },
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_rule_list",
                "description": (
                    "Lista regras memorizadas de um time. Skill: qclaw-chat-team-rules."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "category": {"type": "string"},
                        "priority": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_rule_search",
                "description": (
                    "Busca regras memorizadas de um time por texto. "
                    "Skill: qclaw-chat-team-rules."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {"type": "string", "description": "Texto a buscar"},
                        "category": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_rule_delete",
                "description": (
                    "Desativa ou remove regra memorizada do time. "
                    "Skill: qclaw-chat-team-rules."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "rule_id": {"type": "integer"},
                        "hard": {
                            "type": "boolean",
                            "description": "true = apagar permanentemente",
                        },
                    },
                    "required": ["rule_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_rules_summary",
                "description": (
                    "Resumo das regras memorizadas de um time. Skill: qclaw-chat-team-rules."
                ),
                "parameters": {
                    "type": "object",
                    "properties": team_props,
                    "required": [],
                },
            },
        },
    ]


def _team_facts_tool_specs() -> list[dict[str, Any]]:
    """Team research facts in MongoDB (qclaw-chat-team-facts)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_fact_store",
                "description": (
                    "Cria ou atualiza fato memorizado do time no MongoDB. "
                    "Use para guardar fatos de pesquisa úteis a roteiros. "
                    "Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "title": {"type": "string"},
                        "content": {"type": "string", "description": "Fato em 1-3 frases"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "relevance": {"type": "string"},
                        "topic": {"type": "string", "description": "Tema/pesquisa de origem"},
                        "category": {
                            "type": "string",
                            "description": "ciencia|historia|dados|estatistica|contexto|geral",
                        },
                        "source_type": {
                            "type": "string",
                            "description": "deep_research|manual|import|llm",
                        },
                        "confidence": {
                            "type": "string",
                            "description": "alta|media|baixa|estimativa",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "fact_id": {"type": "integer"},
                        "research_batch_id": {"type": "string"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_fact_import",
                "description": (
                    "Importa lote de fatos (ex.: deep research LLM) para o time. "
                    "Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "topic": {"type": "string"},
                        "query": {"type": "string", "description": "Alias de topic"},
                        "facts": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "source_type": {"type": "string"},
                        "category": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "research_batch_id": {"type": "string"},
                    },
                    "required": ["facts"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_fact_list",
                "description": (
                    "Lista fatos memorizados de um time. Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "topic": {"type": "string"},
                        "category": {"type": "string"},
                        "source_type": {"type": "string"},
                        "research_batch_id": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_fact_search",
                "description": (
                    "Busca fatos memorizados de um time por texto. "
                    "Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {"type": "string"},
                        "topic": {"type": "string"},
                        "category": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_fact_delete",
                "description": (
                    "Desativa ou remove fato memorizado do time. "
                    "Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "fact_id": {"type": "integer"},
                        "hard": {"type": "boolean"},
                    },
                    "required": ["fact_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_facts_summary",
                "description": (
                    "Resumo dos fatos memorizados de um time. Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": team_props,
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_facts_for_roteiro",
                "description": (
                    "Exporta fatos do time formatados para roteiro (markdown). "
                    "Skill: qclaw-chat-team-facts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "topic": {"type": "string"},
                        "query": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
    ]


def _legal_evaluation_tool_specs() -> list[dict[str, Any]]:
    """Legal PDF/normas evaluation (qclaw-chat-avaliacao-juridica)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_pdf_extract",
                "description": (
                    "Extrai texto e metadados de PDF ou documento jurídico (lei, decreto, "
                    "portaria). Use para 'avaliar norma', 'analisar PDF jurídico', 'extrair "
                    "texto da lei'. Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Caminho do PDF ou arquivo (.md, .txt)",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Limite de caracteres (default 50000)",
                        },
                        "include_full_text": {
                            "type": "boolean",
                            "description": "Incluir texto completo na resposta",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_normas_list",
                "description": (
                    "Lista documentos normativos do time (pasta normas/). Use para 'normas "
                    "do time', 'legislação interna', 'políticas jurídicas'. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {
                            "type": "string",
                            "description": "Filtrar por nome ou trecho",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_norma_extract",
                "description": (
                    "Extrai texto de norma específica do time pelo nome do arquivo. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "name": {
                            "type": "string",
                            "description": "Nome do arquivo em normas/ (ex.: politica.pdf)",
                        },
                        "max_chars": {"type": "integer"},
                        "include_full_text": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_evaluation_save",
                "description": (
                    "Grava parecer ou avaliação jurídica de PDF no MongoDB. Use após analisar "
                    "norma para 'salvar parecer', 'guardar avaliação'. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "title": {"type": "string", "description": "Título do parecer"},
                        "content": {"type": "string", "description": "Markdown do parecer"},
                        "evaluation_type": {
                            "type": "string",
                            "description": "resumo|conformidade|interpretacao|comparativo|geral",
                        },
                        "question": {"type": "string", "description": "Pergunta do usuário"},
                        "source_path": {"type": "string", "description": "Caminho do PDF"},
                        "source_name": {"type": "string"},
                        "norma_name": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "evaluation_id": {"type": "string", "description": "Atualizar por ID"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_evaluation_get",
                "description": (
                    "Recupera avaliação jurídica salva no MongoDB pelo ID. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "evaluation_id": {"type": "string", "description": "ID da avaliação"},
                    },
                    "required": ["evaluation_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_evaluation_list",
                "description": (
                    "Lista avaliações jurídicas salvas no MongoDB. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "evaluation_type": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_legal_evaluation_search",
                "description": (
                    "Busca avaliações jurídicas salvas por texto. "
                    "Skill: qclaw-chat-avaliacao-juridica."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {"type": "string", "description": "Texto a buscar"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def _budget_tool_specs() -> list[dict[str, Any]]:
    """Domestic budget / personal finance (qclaw-chat-orcamento-domestico)."""
    user_props = {
        "user_id": {"type": "string", "description": "ID do usuário (default: sessão/dono)"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_budget_get",
                "description": (
                    "Lê perfil financeiro guardado (receitas, despesas, reserva, dívidas, "
                    "investimentos). Use para 'meu orçamento', 'finanças salvas'. "
                    "Skill: qclaw-chat-orcamento-domestico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(user_props),
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_budget_save",
                "description": (
                    "Guarda ou atualiza perfil financeiro do usuário (merge parcial). "
                    "Skill: qclaw-chat-orcamento-domestico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **user_props,
                        "monthly_income": {"type": "number"},
                        "monthly_fixed_expenses": {"type": "number"},
                        "monthly_variable_expenses": {"type": "number"},
                        "monthly_essential_expenses": {"type": "number"},
                        "emergency_fund": {"type": "number"},
                        "risk_profile": {
                            "type": "string",
                            "description": "conservador | moderado | arrojado",
                        },
                        "investment_horizon_years": {"type": "integer"},
                        "debts": {"type": "array"},
                        "investments": {"type": "array"},
                        "categories": {"type": "object"},
                        "notes": {"type": "string"},
                        "custom_fields": {"type": "object"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_budget_summary",
                "description": (
                    "Calcula indicadores do perfil financeiro (fluxo, poupança, reserva em meses). "
                    "Skill: qclaw-chat-orcamento-domestico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(user_props),
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_budget_analyze_csv",
                "description": (
                    "Analisa extrato bancário/cartão em CSV (totais, categorias, fluxo). "
                    "Skill: qclaw-chat-orcamento-domestico."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Caminho do CSV"},
                        "delimiter": {"type": "string", "description": "Separador (; para bancos BR)"},
                        "encoding": {"type": "string", "description": "utf-8 ou latin-1"},
                        "month": {"type": "string", "description": "Filtrar YYYY-MM"},
                    },
                    "required": ["path"],
                },
            },
        },
    ]


def _team_payments_tool_specs() -> list[dict[str, Any]]:
    """Team-scoped payments (qclaw-chat-team-pagamentos)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time"},
        "team_name": {"type": "string", "description": "Nome parcial do time"},
    }
    payment_props = {
        "title": {"type": "string", "description": "Título ou descrição curta"},
        "amount": {"type": "number", "description": "Valor (ex.: 1500.00)"},
        "description": {"type": "string"},
        "currency": {"type": "string"},
        "category": {
            "type": "string",
            "description": "fornecedor|servico|reembolso|salario|infra|software|marketing|imposto|outro",
        },
        "status": {"type": "string", "description": "pendente|pago|cancelado|atrasado"},
        "payer": {"type": "string"},
        "payee": {"type": "string"},
        "payment_date": {"type": "string", "description": "YYYY-MM-DD"},
        "due_date": {"type": "string", "description": "YYYY-MM-DD"},
        "reference": {"type": "string"},
        "payment_method": {
            "type": "string",
            "description": "pix|boleto|transferencia|cartao|dinheiro|outro",
        },
        "notes": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "payment_id": {"type": "string"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payment_save",
                "description": (
                    "Grava ou atualiza pagamento do time no MongoDB. Use para 'registrar pagamento', "
                    "'lançar despesa do time'. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {**team_props, **payment_props},
                    "required": ["title", "amount"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payment_get",
                "description": (
                    "Recupera pagamento do time por ID. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {**team_props, "payment_id": {"type": "string"}},
                    "required": ["payment_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payment_list",
                "description": (
                    "Lista pagamentos do time. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "category": {"type": "string"},
                        "status": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payment_search",
                "description": (
                    "Busca pagamentos do time por texto. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "status": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payment_delete",
                "description": (
                    "Cancela ou apaga pagamento do time. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "payment_id": {"type": "string"},
                        "hard": {"type": "boolean"},
                    },
                    "required": ["payment_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_payments_summary",
                "description": (
                    "Resumo financeiro dos pagamentos do time. Skill: qclaw-chat-team-pagamentos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": dict(team_props),
                    "required": [],
                },
            },
        },
    ]


def _journal_rules_tool_specs() -> list[dict[str, Any]]:
    """Journal submission rules store (qclaw-chat-revista-regras)."""
    team_props = {
        "team_id": {"type": "string", "description": "ID do time (opcional)"},
        "team_name": {"type": "string", "description": "Nome parcial do time (opcional)"},
    }
    profile_props = {
        "journal_name": {"type": "string", "description": "Nome da revista"},
        "publisher": {"type": "string"},
        "issn": {"type": "string"},
        "citation_style": {"type": "string"},
        "language": {"type": "string"},
        "area": {"type": "string"},
        "website": {"type": "string"},
        "description": {"type": "string"},
        "rules": {"type": "array", "items": {"type": "object"}},
        "format_options": {"type": "object"},
        "submission": {"type": "object"},
        "source_path": {"type": "string"},
        "source_name": {"type": "string"},
        "source_text_excerpt": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "slug": {"type": "string"},
        "profile_id": {"type": "string"},
        "merge_rules": {"type": "boolean"},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_save",
                "description": (
                    "Grava ou atualiza todas as regras de uma revista científica no MongoDB. "
                    "Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {**team_props, **profile_props, "active": {"type": "boolean"}},
                    "required": ["journal_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_get",
                "description": (
                    "Recupera perfil de regras de revista por id, slug ou nome. "
                    "Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "slug": {"type": "string"},
                        "journal_name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_list",
                "description": (
                    "Lista revistas com regras cadastradas. Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "area": {"type": "string"},
                        "citation_style": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_search",
                "description": (
                    "Busca revistas/regras por texto. Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "query": {"type": "string"},
                        "active_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_extract",
                "description": (
                    "Extrai regras de revista de PDF/DOCX/MD e opcionalmente grava. "
                    "Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **team_props,
                        "path": {"type": "string"},
                        "journal_name": {"type": "string"},
                        "save": {"type": "boolean"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_journal_rules_delete",
                "description": (
                    "Desativa ou apaga perfil de regras de revista. Skill: qclaw-chat-revista-regras."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "slug": {"type": "string"},
                        "hard": {"type": "boolean"},
                    },
                    "required": [],
                },
            },
        },
    ]


def _qclaw_chat_tool_catalogue(
    recovery: Recovery,
    *,
    cli_available: bool,
    shell_enabled: bool = True,
    desktop_control_enabled: bool = False,
    hermes_agent_create_enabled: bool = False,
    kanban_create_card_enabled: bool = False,
    whatsapp_send_enabled: bool = False,
    heygen_mcp_enabled: bool = False,
    suno_mcp_enabled: bool = False,
    seedance_mcp_enabled: bool = False,
    runway_mcp_enabled: bool = False,
    virtual_band_enabled: bool = False,
    roteiro_thumbnails_enabled: bool = False,
    roteiro_mongo_enabled: bool = False,
    roteiro_api_enabled: bool = False,
    allowlist_enabled: bool = False,
    external_mcp_enabled: bool = True,
) -> list[dict[str, Any]]:
    global _tool_catalog_cache, _tool_catalog_cache_key
    _cache_key = (
        tuple(recovery.cfg.log_paths or []),
        cli_available, shell_enabled, desktop_control_enabled,
        hermes_agent_create_enabled, kanban_create_card_enabled,
        whatsapp_send_enabled, heygen_mcp_enabled, suno_mcp_enabled, seedance_mcp_enabled,
        runway_mcp_enabled,
        virtual_band_enabled, roteiro_thumbnails_enabled, roteiro_mongo_enabled, roteiro_api_enabled, allowlist_enabled,
        external_mcp_enabled,
    )
    if _tool_catalog_cache is not None and _tool_catalog_cache_key == _cache_key:
        return list(_tool_catalog_cache)

    allowed_logs = recovery.cfg.log_paths
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "qclaw_health_check",
                "description": (
                    "Probe QClaw process + gateway HTTP health. Returns per-check status."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_process_status",
                "description": (
                    "List QClaw-related processes on the host (main, helpers, gateway)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_read_log_tail",
                "description": (
                    "Read the last lines of an allowed QClaw log file. "
                    f"path must be one of: {allowed_logs or '(none configured)'}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "lines": {"type": "integer", "default": 120},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_monitor_snapshot",
                "description": (
                    "Current gois view: failures, recovery, Hermes, processes."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_list_teams",
                "description": (
                    "Lista todos os times/squads cadastrados no gois com seus "
                    "IDs, nomes, descrições e projetos vinculados. Use quando o usuário "
                    "perguntar quais times ativos, listar times, squads ou equipes."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_kanban",
                "description": (
                    "Lê o board Kanban de um time específico retornando colunas e tarefas "
                    "com status, assignees, prioridade, skills e ANEXOS de cada card "
                    "(campo attachments com file_name, safe_name, mime_type, stored_path). "
                    "Cada board retorna um campo board_number (número sequencial do board). "
                    "Use quando o usuário perguntar qual o kanban de um time, board do time, "
                    "tarefas do time, quais anexos/documentos estão em um card, "
                    "ou pedir para identificar o kanban associado a cada time. "
                    "Se team_id for vazio, retorna o mapa de TODOS os times com seus kanbans. "
                    "SEMPRE use esta ferramenta antes de tentar mover, copiar ou referenciar "
                    "um anexo de card para obter o safe_name correto."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {
                            "type": "string",
                            "description": (
                                "ID do time (slug). Se vazio retorna kanban de todos os times."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_kanban_requirements",
                "description": (
                    "Agente de Requisitos: analisa um cartão do Kanban e levanta TUDO o "
                    "que falta para implementá-lo — requisitos, critérios de aceite, "
                    "detalhes de implementação e PERGUNTAS objetivas endereçadas ao "
                    "membro certo do time (campo open_questions com ask_to). Persiste o "
                    "resultado no cartão (aparece ao clicar nele). Use quando o usuário "
                    "pedir 'levantar requisitos', 'o que falta para implementar este "
                    "card', 'perguntar ao time', 'refinar tarefa' ou 'critérios de "
                    "aceite' de um cartão. Use qclaw_team_kanban antes para obter o "
                    "team_id e o task_id corretos."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {
                            "type": "string",
                            "description": (
                                "ID do time (slug). Se vazio, usa o time da sessão atual."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "ID do cartão (ex. TASK-012). Obrigatório.",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_save_project_note",
                "description": (
                    "Save a durable fact to project memory (shared across all dashboard "
                    "chats). Use for paths, conventions, decisions, and long-lived context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Fact text to remember for the whole project",
                        },
                        "kind": {
                            "type": "string",
                            "description": "Category label (default: note)",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_hermes_cron_health",
                "description": (
                    "Saúde dos cron jobs Hermes (agendamentos): totais, ativos, pausados, "
                    "última execução com erro, jobs em execução agora e prévia do último "
                    "resultado. Use quando o usuário perguntar sobre cron, chron, agendamentos "
                    "ou tarefas programadas dos agentes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": (
                                "Opcional: id do job para incluir o último resultado salvo "
                                "(markdown em ~/.hermes/cron/output/)"
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_list_chat_models",
                "description": (
                    "Lista modelos LLM disponíveis no chat (DeepSeek, OpenAI, Gemini, "
                    "Claude, Perplexity) e se a chave API está configurada. Use quando "
                    "o usuário pedir para trocar de modelo ou antes de sugerir Codex, "
                    "Claude ou Perplexity."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_list_image_models",
                "description": (
                    "Lista modelos de GERAÇÃO DE IMAGEM (Imagen, Gemini Image, Grok, "
                    "OpenAI, Flux, etc.) com qualidade, preço estimado USD, resolução, "
                    "texto na imagem e skill QClaw recomendada. Catálogo em MongoDB. "
                    "Use quando o usuário pedir modelos de imagem, comparar Imagen vs "
                    "Grok, preço de imagem, ou escolher provider para slides/thumbnails."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "description": (
                                "Filtrar provedor: google, grok, openai, imagen, nano, fal, …"
                            ),
                        },
                        "quality": {
                            "type": "string",
                            "description": "Tier de qualidade: A, B, C ou D",
                        },
                        "use_case": {
                            "type": "string",
                            "description": (
                                "Cenário: slides_didaticos, batch_100_slides, "
                                "thumbnail_texto, editar_foto_4k, capa_livro"
                            ),
                        },
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "json"],
                            "description": "markdown=tabela no chat; json=estruturado",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_model_quotas",
                "description": (
                    "Consulta as cotas diárias dos modelos LLM (tokens/dia e USD/dia), "
                    "uso atual de cada modelo e se algum está bloqueado por excesso de cota. "
                    "Use quando o usuário perguntar sobre limites, cotas, bloqueio de modelos "
                    "ou quanto foi consumido hoje."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_nano_banana_generate",
                "description": (
                    "Gera ou edita imagem com Nano Banana Pro (Gemini 3 Pro Image). "
                    "Use para capas, thumbnails, arte ou editar foto existente. "
                    "Requer GEMINI_API_KEY."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Descrição da imagem ou instruções de edição",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro PNG de saída (ex. 2026-06-01-cap.png)",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "description": "Resolução (1K=rascunho, 4K=final)",
                        },
                        "input_image_path": {
                            "type": "string",
                            "description": "Caminho absoluto da imagem a editar (opcional)",
                        },
                        "card_id": {
                            "type": "string",
                            "description": (
                                "Opcional: ID do card kanban (ex. TASK-038 ou 038) para "
                                "anexar a imagem gerada ao card. Aceita alias task_id."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Alias de card_id para anexar a imagem ao card",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "Opcional: time alvo (default = time da sessão)",
                        },
                    },
                    "required": ["prompt", "filename"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_grok_imagine_generate",
                "description": (
                    "Gera ou edita imagem com Grok Imagine (xAI). "
                    "Use para thumbnails, banners, posts com texto legível na imagem. "
                    "Requer XAI_API_KEY."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Descrição da imagem ou instruções de edição",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro de saída (ex. 2026-06-14-thumb.jpeg)",
                        },
                        "model": {
                            "type": "string",
                            "enum": [
                                "grok-imagine-image-quality",
                                "grok-imagine-image",
                            ],
                            "description": "Modelo xAI (quality=2K/texto nítido, image=rápido)",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": ["1k", "2k"],
                            "description": "Resolução (1k=rascunho, 2k=final)",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "description": "Proporção: auto, 1:1, 16:9, 9:16, 4:3, 3:4, etc.",
                        },
                        "input_image_path": {
                            "type": "string",
                            "description": "Caminho absoluto da imagem a editar (opcional)",
                        },
                        "slide_index": {
                            "type": "integer",
                            "description": (
                                "Índice do slide atual (1-based) — atualiza o slider de blocos no chat"
                            ),
                        },
                        "slide_total": {
                            "type": "integer",
                            "description": "Total de slides do lote/deck (ex.: 100)",
                        },
                        "block_index": {
                            "type": "integer",
                            "description": (
                                "Bloco atual (ex.: 2 de 20 lotes de 5 slides) — preferir com block_total"
                            ),
                        },
                        "block_total": {
                            "type": "integer",
                            "description": "Total de blocos/lotes (ex.: 20 para 100 slides em lotes de 5)",
                        },
                        "no_fallback": {
                            "type": "boolean",
                            "description": (
                                "Se true, usa APENAS o modelo pedido sem tentar fallback. "
                                "Também é ativado automaticamente quando se pede um modelo específico."
                            ),
                        },
                        "card_id": {
                            "type": "string",
                            "description": (
                                "Opcional: ID do card kanban (ex. TASK-038 ou 038) para "
                                "anexar a imagem gerada ao card. Aceita alias task_id."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Alias de card_id para anexar a imagem ao card",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "Opcional: time alvo (default = time da sessão)",
                        },
                    },
                    "required": ["prompt", "filename"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_imagen_generate",
                "description": (
                    "Gera imagem com Imagen 4 (Google) localmente — sem API Roteiro Viral. "
                    "Use para slides, ilustrações e capas com estilos IMAGE_STYLES. "
                    "Requer GEMINI_API_KEY. Preferível quando Nano Banana/Grok falham."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Descrição da imagem",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro PNG de saída",
                        },
                        "model": {
                            "type": "string",
                            "description": "Modelo Imagen (default imagen-4.0-ultra-generate-001)",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "description": "16:9, 1:1, 9:16, 4:3 ou 3:4",
                        },
                        "style": {
                            "type": "string",
                            "description": "Nome exacto em IMAGE_STYLES (opcional)",
                        },
                        "card_id": {
                            "type": "string",
                            "description": (
                                "Opcional: ID do card kanban (ex. TASK-038 ou 038) para "
                                "anexar a imagem gerada ao card. Aceita alias task_id."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Alias de card_id para anexar a imagem ao card",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "Opcional: time alvo (default = time da sessão)",
                        },
                    },
                    "required": ["prompt", "filename"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_openrouter_image_generate",
                "description": (
                    "Gera/edita imagem via OpenRouter com QUALQUER modelo de imagem "
                    "(google/gemini-3-pro-image, openai/gpt-5-image, "
                    "black-forest-labs/flux.2-pro, x-ai/grok-imagine-image-quality, "
                    "bytedance-seed/seedream-4.5, microsoft/mai-image-2.5, …). "
                    "Use quando o usuário pedir OpenRouter ou um modelo owner/model "
                    "específico. Fallback automático para outros providers. "
                    "Requer OPENROUTER_API_KEY. Veja modelos em qclaw_list_image_models."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Descrição da imagem ou instruções de edição",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro de saída (ex. 2026-06-01-cap.png)",
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Slug OpenRouter owner/model "
                                "(default google/gemini-3-pro-image)"
                            ),
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "description": "16:9, 1:1, 9:16, 4:3 ou 3:4",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "description": "Resolução (1K=rascunho, 4K=final)",
                        },
                        "input_image_path": {
                            "type": "string",
                            "description": "Caminho absoluto da imagem a editar (opcional)",
                        },
                        "allow_fallback": {
                            "type": "boolean",
                            "description": (
                                "Tentar outros modelos/providers se falhar "
                                "(default false quando um modelo específico é dado)"
                            ),
                        },
                        "card_id": {
                            "type": "string",
                            "description": (
                                "Opcional: ID do card kanban (ex. TASK-038 ou 038) para "
                                "anexar a imagem gerada ao card. Aceita alias task_id."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Alias de card_id para anexar a imagem ao card",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "Opcional: time alvo (default = time da sessão)",
                        },
                    },
                    "required": ["prompt", "filename"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_replicate_generate",
                "description": (
                    "Gera imagem ou vídeo via Replicate (FLUX, Ideogram, Luma, Kling, Seedance, …). "
                    "Use quando o usuário pedir Replicate ou modelos owner/name da plataforma. "
                    "Requer REPLICATE_API_TOKEN em Chaves & Secrets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Descrição da imagem ou cena de vídeo",
                        },
                        "model": {
                            "type": "string",
                            "description": "Slug Replicate owner/name (ex. black-forest-labs/flux-schnell)",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro de saída (opcional)",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "description": "16:9, 1:1, 9:16, 4:3 ou 3:4",
                        },
                        "duration_seconds": {
                            "type": "integer",
                            "description": "Duração do vídeo em segundos (só modelos video)",
                        },
                    },
                    "required": ["prompt", "model"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_wacli_auth_qr",
                "description": (
                    "Gera o QR code do wacli para reautenticar o WhatsApp (pareamento). "
                    "Use quando o usuário pedir QR, código de barras, reautenticar WhatsApp, "
                    "wacli auth ou sessão WhatsApp expirada. Retorna imagem para exibir no chat."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_status",
                "description": (
                    "Estado instantâneo do WhatsApp outbound: fila (enqueued/sent/failed), "
                    "lock do store wacli, sync webhook ativo e processos wacli. "
                    "Use para 'estado do envio', fila pendente, lock ou diagnóstico — "
                    "nunca `wacli`/`qclaw_run_shell` para isso (trava minutos no lock)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_wacli_unlock",
                "description": (
                    "Destrava o store wacli quando bloqueado (store locked): encerra wacli sync/auth "
                    "que seguram o lock. **Obrigatório antes de reenviar** quando "
                    "qclaw_send_whatsapp falha, expira ou o destinatário não recebeu."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kill_sync": {
                            "type": "boolean",
                            "description": (
                                "Se true (padrão), encerra processos wacli sync em execução."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_groups_sync",
                "description": (
                    "Sincroniza todos os grupos WhatsApp do wacli para o MongoDB "
                    "(coleção wa_groups). Use quando pedir atualizar/sincronizar grupos, "
                    "salvar grupos no banco, ou antes de indexar mensagens com all_known."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "refresh": {
                            "type": "boolean",
                            "description": (
                                "Se true, executa wacli groups refresh antes de listar "
                                "(mais lento; use para grupo novo que não aparece)."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_wacli_group_numbers",
                "description": (
                    "Busca os números de telefone (participantes) de um grupo WhatsApp "
                    "via wacli groups info. Informe o nome parcial do grupo ou JID direto. "
                    "Use quando o usuário pedir números, membros, participantes ou telefones de um grupo."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group": {
                            "type": "string",
                            "description": (
                                "Nome parcial do grupo (ex: T12, devops, EDD) ou JID completo "
                                "(ex: 120363403284081453@g.us)."
                            ),
                        },
                    },
                    "required": ["group"],
                },
            },
        },
    ]
    if whatsapp_send_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_send_whatsapp",
                    "description": (
                        "Envia mensagem de texto para WhatsApp. Sem `to`, usa "
                        "whatsapp_digest.recipient (DM padrão). Com `to`, envia para "
                        "número ou grupo allowlisted (JID, dígitos ou nome parcial, "
                        "ex.: FUNCEME/UNIFOR ou 120363...@g.us). Enfileirado — preferir "
                        "em vez de `wacli send` no shell. Use qclaw_allowlist_list "
                        "para confirmar grupos permitidos. Se falhar (timeout, lock, "
                        "usuário não recebeu): qclaw_wacli_unlock (kill_sync: true) e "
                        "repita o envio."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Texto da mensagem",
                            },
                            "to": {
                                "type": "string",
                                "description": (
                                    "Destino opcional: JID (@g.us ou @s.whatsapp.net), "
                                    "número ou nome parcial na allowlist"
                                ),
                            },
                            "wait": {
                                "type": "boolean",
                                "description": (
                                    "Aguardar confirmação wacli antes de responder. "
                                    "Padrão: false (enfileira e retorna job_id imediatamente). "
                                    "Use true só quando o usuário pedir confirmação explícita."
                                ),
                            },
                        },
                        "required": ["message"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_send_whatsapp_file",
                    "description": (
                        "Envia arquivo para WhatsApp (PDF, imagem, ZIP, etc.). "
                        "PDFs vão como documento anexado por padrão (mode=document). "
                        "Use mode=preview para renderizar páginas de PDF/slides em PNG "
                        "antes do envio. Com `to`, envia para número ou grupo allowlisted; "
                        "sem `to`, usa whatsapp_digest.recipient."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Caminho absoluto do arquivo local",
                            },
                            "to": {
                                "type": "string",
                                "description": (
                                    "Destino opcional: JID, número ou nome parcial "
                                    "na allowlist"
                                ),
                            },
                            "caption": {
                                "type": "string",
                                "description": "Legenda opcional do arquivo",
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["auto", "document", "preview"],
                                "description": (
                                    "auto: PDF/imagem como documento; slides como preview. "
                                    "document: envia o arquivo bruto. "
                                    "preview: renderiza páginas em PNG e envia."
                                ),
                            },
                            "pages": {
                                "type": "string",
                                "description": (
                                    "Intervalo de páginas no mode=preview (ex.: 1-5)"
                                ),
                            },
                            "max_pages": {
                                "type": "integer",
                                "description": (
                                    "Máximo de páginas no mode=preview (default 10, máx. 12)"
                                ),
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            }
        )
    if hermes_agent_create_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_create_hermes_agent",
                    "description": (
                        "Cria um perfil/agente Hermes a partir de linguagem natural. "
                        "Pausa temporariamente todos os cron jobs ativos, aguarda o "
                        "dashboard Hermes ficar pronto, cria o agente e reativa os crons. "
                        "Use quando o usuário pedir para criar agente, perfil ou papel Hermes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": (
                                    "Descrição do agente (nome, função, skills desejadas)"
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["dev", "role", "project"],
                                "description": "Tipo de criação (padrão: dev)",
                            },
                            "schedule": {
                                "type": "string",
                                "description": "Agendamento do cron Hermes (ex. every 24h)",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Diretório de trabalho do agente",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (obrigatório em mode=project)",
                            },
                        },
                        "required": ["text"],
                    },
                },
            }
        )
    if hermes_agent_create_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_create_openai_swarm",
                    "description": (
                        "Cria um swarm de agentes executáveis no Hermes (não stubs). "
                        "Cada agente recebe SOUL, skills, cron com prompt concreto e "
                        "registro na topologia. Retorna execution_ready=false se algum "
                        "agente ficar sem cron. Use quando o usuário pedir swarm, "
                        "enxame de agentes ou multi-agente com handoff/pipeline."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": (
                                    "Descrição do swarm (agentes, papéis, relação entre eles)"
                                ),
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Diretório de trabalho dos agentes (opcional)",
                            },
                            "schedule": {
                                "type": "string",
                                "description": "Agendamento cron dos agentes (ex. every 24h)",
                            },
                        },
                        "required": ["text"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_run_swarm",
                    "description": (
                        "Executa um swarm Hermes pelo nome (como Executar 1 / Executar todos "
                        "em /swarm). Roda todos os agentes em sequência. Agentes com "
                        "execution_backend vscode/cursor/kiro/antigravity recebem handoff "
                        "na IDE (contexto + abrir app); demais agentes usam LLM. "
                        "Use quando pedir 'rodar swarm no VS Code/Cursor', 'executar swarm "
                        "na IDE' ou 'swarm do curso no vscode'. Com time vinculado: "
                        "max_cards=1 (padrão) pega o próximo card; all_cards=true ou "
                        "max_cards=0 processa todos os cards abertos de uma vez."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "swarm_name": {
                                "type": "string",
                                "description": "Nome/slug do swarm (ex. fluxo-dev)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": (
                                    "ID do time com kanban. Inferido da sessão ou vínculo swarm."
                                ),
                            },
                            "objective": {
                                "type": "string",
                                "description": (
                                    "Objetivo extra (opcional — usa card(s) do kanban se vazio)"
                                ),
                            },
                            "max_cards": {
                                "type": "integer",
                                "description": (
                                    "Quantos cards processar: 1 = próximo (padrão); 0 = todos abertos"
                                ),
                            },
                            "all_cards": {
                                "type": "boolean",
                                "description": (
                                    "true = executar todos os cards abertos (equivalente a max_cards=0)"
                                ),
                            },
                            "force": {
                                "type": "boolean",
                                "description": "Forçar se swarm já estiver ocupado",
                            },
                            "open_ide": {
                                "type": "boolean",
                                "description": (
                                    "Abrir VS Code/Cursor/Kiro via CLI no handoff IDE "
                                    "(default true; false = só materializar contexto)"
                                ),
                            },
                        },
                        "required": ["swarm_name"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_run_team_swarm",
                    "description": (
                        "Executa o swarm vinculado a um time do Kanban (fluxo multi-agente). "
                        "Use quando pedir executar o time, rodar swarm do time ou "
                        "'swarm na IDE/VS Code/Cursor'. Agentes IDE recebem handoff; "
                        "demais usam LLM. max_cards=1 (padrão) = 1 card; "
                        "all_cards=true = todos os abertos."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "team_id": {
                                "type": "string",
                                "description": (
                                    "ID do time. Use o team_id do time selecionado na conversa."
                                ),
                            },
                            "objective": {
                                "type": "string",
                                "description": (
                                    "Objetivo do fluxo (opcional — usa backlog do kanban se vazio)"
                                ),
                            },
                            "max_cards": {
                                "type": "integer",
                                "description": (
                                    "1 = próximo card (padrão); 0 = todos os cards abertos"
                                ),
                            },
                            "all_cards": {
                                "type": "boolean",
                                "description": "true = todos os cards abertos de uma vez",
                            },
                            "force": {
                                "type": "boolean",
                                "description": (
                                    "Forçar reexecução se o swarm do time já estiver ocupado"
                                ),
                            },
                            "open_ide": {
                                "type": "boolean",
                                "description": (
                                    "Abrir IDE no handoff (default true; false = só contexto)"
                                ),
                            },
                        },
                        "required": [],
                    },
                },
            }
        )
        tools.extend(_swarm_manage_tool_specs())
    if kanban_create_card_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_create_kanban_card",
                    "description": (
                        "Cria um cartão no Kanban do gois e associa a um time. "
                        "Use quando o usuário pedir criar card/cartão/tarefa no kanban. "
                        "IMPORTANTE: SEMPRE passe o team_id do time selecionado na conversa "
                        "(informado no bloco '## Time selecionado nesta conversa' do system prompt). "
                        "Se nenhum time estiver selecionado, pergunte ao usuário qual time usar."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Título do cartão (obrigatório)",
                            },
                            "description": {
                                "type": "string",
                                "description": "Descrição detalhada da tarefa",
                            },
                            "team_id": {
                                "type": "string",
                                "description": (
                                    "ID do time no gois. "
                                    "OBRIGATÓRIO — use o team_id do time selecionado na conversa."
                                ),
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do projeto para inferir o time (opcional)",
                            },
                            "column": {
                                "type": "string",
                                "description": "Coluna do Kanban (todo/backlog/doing/review/done)",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "Prioridade numérica (opcional)",
                            },
                            "assignees": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Lista de responsáveis (opcional)",
                            },
                            "skills": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Lista de skills relacionadas (opcional)",
                            },
                            "implementation_location": {
                                "type": "string",
                                "description": (
                                    "Como localizar a implementação na aplicação (opcional): "
                                    "menu, rota/página, módulo ou arquivo principal"
                                ),
                            },
                        },
                        "required": ["title"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_create_card_with_article",
                    "description": (
                        "Cria um cartão no Kanban do time E anexa a versão do artigo (PDF/TEX/DOCX/MD) "
                        "ao card recém-criado, num único passo. "
                        "Use quando o usuário pedir para 'criar card e anexar o artigo', "
                        "'abrir tarefa com a versão do paper' ou 'registrar a entrega do artigo no board'. "
                        "IMPORTANTE: SEMPRE passe o team_id do time selecionado na conversa "
                        "(bloco '## Time selecionado nesta conversa' do system prompt). "
                        "Passe os caminhos absolutos dos ficheiros do artigo em article_paths."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Título do cartão (obrigatório)",
                            },
                            "article_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Caminhos absolutos dos ficheiros da versão do artigo a anexar "
                                    "(ex: ['/abs/paper-v3.pdf', '/abs/paper-v3.tex']). Obrigatório."
                                ),
                            },
                            "description": {
                                "type": "string",
                                "description": "Descrição detalhada da tarefa",
                            },
                            "team_id": {
                                "type": "string",
                                "description": (
                                    "ID do time no gois. "
                                    "OBRIGATÓRIO — use o team_id do time selecionado na conversa."
                                ),
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do projeto para inferir o time (opcional)",
                            },
                            "column": {
                                "type": "string",
                                "description": "Coluna do Kanban (todo/backlog/doing/review/done)",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "Prioridade numérica (opcional)",
                            },
                            "assignees": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Lista de responsáveis (opcional)",
                            },
                            "skills": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Lista de skills relacionadas (opcional)",
                            },
                        },
                        "required": ["title", "article_paths"],
                    },
                },
            }
        )
        # Kanban attachment tools — upload, move, copy via chat
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_attach_upload",
                    "description": (
                        "Anexa um documento/arquivo a um cartão do Kanban. "
                        "Use quando o usuário pedir para anexar arquivo a um card específico pelo chat. "
                        "Passe file_path para arquivos locais (preferível) ou data_base64 para conteúdo em base64."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "ID do card/tarefa destino (ex: TASK-001)",
                            },
                            "file_name": {
                                "type": "string",
                                "description": "Nome do arquivo com extensão",
                            },
                            "mime_type": {
                                "type": "string",
                                "description": "MIME type (ex: application/pdf, image/png)",
                            },
                            "data_base64": {
                                "type": "string",
                                "description": "Conteúdo do arquivo codificado em base64 (alternativa a file_path)",
                            },
                            "file_path": {
                                "type": "string",
                                "description": "Caminho absoluto do arquivo no sistema local (preferível a data_base64 para arquivos grandes)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (opcional)",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do projeto (opcional)",
                            },
                        },
                        "required": ["task_id", "file_name"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_attach_move",
                    "description": (
                        "Move um anexo de um card para outro no Kanban. "
                        "O anexo é removido do card de origem e adicionado ao destino. "
                        "Use quando o usuário pedir para mover/transferir anexo entre cards."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_task_id": {
                                "type": "string",
                                "description": "ID do card de origem (ex: TASK-001)",
                            },
                            "dest_task_id": {
                                "type": "string",
                                "description": "ID do card de destino (ex: TASK-002)",
                            },
                            "safe_name": {
                                "type": "string",
                                "description": "Nome seguro do arquivo (safe_name do attachment metadata)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (opcional)",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do projeto (opcional)",
                            },
                        },
                        "required": ["source_task_id", "dest_task_id", "safe_name"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_list_attachments",
                    "description": (
                        "Lista os anexos de um card do Kanban. "
                        "Use quando o usuário perguntar quais arquivos estão anexados a um card, "
                        "pedir para encontrar/localizar um documento em um card, "
                        "ou antes de mover/copiar um anexo para obter o safe_name correto. "
                        "Retorna file_name, safe_name, mime_type, size e stored_path de cada anexo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "ID do card (ex: TASK-016). Se vazio, lista anexos de todos os cards.",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (slug). Opcional — usa o time da conversa se omitido.",
                            },
                        },
                        "required": [],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_attach_project_zip",
                    "description": (
                        "Compacta um diretório de projeto em ZIP e anexa ao card do Kanban. "
                        "Use quando pedir zip do projeto, backup do código, anexar projeto ao card. "
                        "Exclui .git, node_modules, .venv e ficheiros de segredo (.env). "
                        "Resolve o projeto via team_id (local_path) ou path/workdir explícito."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "ID do card destino (ex: TASK-017)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (usa local_path do projeto)",
                            },
                            "team_name": {
                                "type": "string",
                                "description": "Nome parcial do time",
                            },
                            "path": {
                                "type": "string",
                                "description": "Caminho absoluto do projeto (alternativa a team_id)",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do kanban/projeto",
                            },
                            "subdir": {
                                "type": "string",
                                "description": "Subpasta relativa dentro do projeto",
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
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_attach_copy",
                    "description": (
                        "Copia um anexo de um card para outro no Kanban. "
                        "O anexo original permanece no card de origem. "
                        "Use quando o usuário pedir para copiar/duplicar anexo entre cards."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_task_id": {
                                "type": "string",
                                "description": "ID do card de origem (ex: TASK-001)",
                            },
                            "dest_task_id": {
                                "type": "string",
                                "description": "ID do card de destino (ex: TASK-002)",
                            },
                            "safe_name": {
                                "type": "string",
                                "description": "Nome seguro do arquivo (safe_name do attachment metadata)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (opcional)",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do projeto (opcional)",
                            },
                        },
                        "required": ["source_task_id", "dest_task_id", "safe_name"],
                    },
                },
            }
        )
    if hermes_agent_create_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_kanban_attach_latex_zip",
                    "description": (
                        "Coleta .tex, figuras, .bib, estilos e PDF de um artigo LaTeX, "
                        "compacta em ZIP e anexa ao card. Use para anexar latex ao card, "
                        "pacote overleaf, manuscrito compilável — não o repo inteiro."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "ID do card destino (ex: TASK-017)",
                            },
                            "team_id": {
                                "type": "string",
                                "description": "ID do time (usa local_path do projeto)",
                            },
                            "team_name": {
                                "type": "string",
                                "description": "Nome parcial do time",
                            },
                            "path": {
                                "type": "string",
                                "description": "Caminho absoluto do projeto/artigo",
                            },
                            "workdir": {
                                "type": "string",
                                "description": "Workdir do kanban/projeto",
                            },
                            "subdir": {
                                "type": "string",
                                "description": "Subpasta relativa do artigo",
                            },
                            "article": {
                                "type": "string",
                                "description": "Caminho relativo do .tex principal",
                            },
                            "zip_name": {
                                "type": "string",
                                "description": "Nome do arquivo .zip (opcional)",
                            },
                            "include_pdf": {
                                "type": "boolean",
                                "description": "Incluir PDF compilado (default true)",
                            },
                            "include_styles": {
                                "type": "boolean",
                                "description": "Incluir .cls/.sty/.bst locais (default true)",
                            },
                            "max_bytes": {
                                "type": "integer",
                                "description": "Limite de tamanho do zip (default 200MB)",
                            },
                        },
                        "required": ["task_id"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_create_team",
                    "description": (
                        "Cria um time/projeto no gois e persiste em MongoDB (coleção "
                        "accounts), com Kanban, workspace e artefatos em disco. Use "
                        "quando o usuário pedir criar time, equipe, squad ou projeto "
                        "no Kanban. Nunca edite store.json, accounts.db nem pastas "
                        "em teams/ manualmente — esta ferramenta provisiona tudo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Nome do time (obrigatório)",
                            },
                            "id": {
                                "type": "string",
                                "description": "Slug/ID do time (opcional)",
                            },
                            "description": {
                                "type": "string",
                                "description": "Descrição curta do objetivo do time",
                            },
                            "project_source": {
                                "type": "string",
                                "description": "Fonte do projeto (ex.: github, local, preset)",
                            },
                            "github_url": {
                                "type": "string",
                                "description": "URL do repositório GitHub (opcional)",
                            },
                            "github_branch": {
                                "type": "string",
                                "description": "Branch principal (padrão: main)",
                            },
                            "local_path": {
                                "type": "string",
                                "description": "Caminho local do projeto (opcional)",
                            },
                            "app_url": {
                                "type": "string",
                                "description": "URL da aplicação em produção/staging (opcional)",
                            },
                            "site_links": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "url": {"type": "string"},
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                },
                                "description": "Links de sites do time (docs, dashboards, Notion, etc.)",
                            },
                            "profile_slugs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Perfis Hermes para o time",
                            },
                            "preset_id": {
                                "type": "string",
                                "description": "Preset de time para pré-configurar papéis",
                            },
                            "seed_kanban": {
                                "type": "boolean",
                                "description": (
                                    "Criar board Kanban inicial com colunas padrão "
                                    "(padrão: true; use preset_id para cards iniciais)"
                                ),
                            },
                        },
                        "required": ["name"],
                    },
                },
            }
        )
    if shell_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_run_shell",
                    "description": (
                        "Run a bash command on the Mac host and return stdout/stderr. "
                        "Use for diagnostics, git, curl, scripts, file inspection, etc. "
                        "Do NOT tell the user to run commands manually — call this tool."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Shell command to execute",
                            },
                            "cwd": {
                                "type": "string",
                                "description": (
                                    "Optional working directory (absolute path)"
                                ),
                            },
                        },
                        "required": ["command"],
                    },
                },
            }
        )
    if desktop_control_enabled:
        tools.extend(_desktop_control_tool_specs())
    if cli_available:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "ask_qclaw_agent",
                    "description": (
                        "Send a question to the live QClaw OpenClaw agent via CLI and "
                        "return its reply. Use for tasks that need the desktop agent's "
                        "session, tools, or workspace — not for simple monitor stats."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "Question for the QClaw agent",
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "OpenClaw agent id (default: main)",
                            },
                        },
                        "required": ["question"],
                    },
                },
            }
        )
    if heygen_mcp_enabled and cli_available:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_heygen_via_openclaw",
                    "description": (
                        "Cria ou gere vídeos HeyGen via MCP oficial (OAuth) no agente "
                        "OpenClaw. Use para avatar, Video Agent, tradução, lip-sync, "
                        "listar vídeos ou créditos — não para batch de curso Roteiro Viral."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "Pedido em linguagem natural (script, avatar, idioma, etc.)"
                                ),
                            },
                            "session_id": {
                                "type": "string",
                                "description": (
                                    "Opcional: session_id do Video Agent para follow-up"
                                ),
                            },
                            "video_id": {
                                "type": "string",
                                "description": (
                                    "Opcional: video_id HeyGen para consultar estado/URL"
                                ),
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "OpenClaw agent id (default: main)",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
    if suno_mcp_enabled and cli_available:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_suno_via_openclaw",
                    "description": (
                        "Gera ou gere música com Suno via MCP AceDataCloud no agente "
                        "OpenClaw. Use para criar músicas, letras, covers, extensões, "
                        "stems ou consultar tarefas — não para banda virtual em batch RV."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "Pedido em linguagem natural (género, letras, estilo, etc.)"
                                ),
                            },
                            "task_id": {
                                "type": "string",
                                "description": (
                                    "Opcional: task_id Suno para consultar estado/URL"
                                ),
                            },
                            "audio_id": {
                                "type": "string",
                                "description": (
                                    "Opcional: audio_id Suno para extensão/cover/stems"
                                ),
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "OpenClaw agent id (default: main)",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
    if seedance_mcp_enabled and cli_available:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_seedance_via_openclaw",
                    "description": (
                        "Gera ou gere vídeo com Seedance (ByteDance, incl. Seedance 2.0) "
                        "via MCP AceDataCloud no agente OpenClaw. Use para text-to-video, "
                        "image-to-video, consultar tarefas ou listar modelos — não para "
                        "batch RV ou avatar HeyGen."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "Pedido em linguagem natural (cena, estilo, resolução, etc.)"
                                ),
                            },
                            "task_id": {
                                "type": "string",
                                "description": (
                                    "Opcional: task_id Seedance para consultar estado/URL"
                                ),
                            },
                            "model": {
                                "type": "string",
                                "description": (
                                    "Opcional: modelo Seedance "
                                    "(ex. doubao-seedance-2-0-260128, "
                                    "doubao-seedance-2-0-fast-260128)"
                                ),
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "OpenClaw agent id (default: main)",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
    if runway_mcp_enabled and cli_available:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_runway_via_openclaw",
                    "description": (
                        "Gera vídeo cinematográfico a partir de foto via Runway MCP no agente "
                        "OpenClaw (OAuth). Use para image-to-video, movimento de câmera e "
                        "cenas criativas — não para avatar falando (use HeyGen) nem batch RV."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "Pedido com foto anexa + prompt de movimento/câmera"
                                ),
                            },
                            "asset_id": {
                                "type": "string",
                                "description": "Opcional: asset_id Runway para follow-up",
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "OpenClaw agent id (default: main)",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
    if virtual_band_enabled:
        from .virtual_band import chat_tool_specs as _vb_chat_tools

        tools.extend(_vb_chat_tools())
    if roteiro_thumbnails_enabled:
        from .roteiro_thumbnails import chat_tool_specs as _thumb_chat_tools

        tools.extend(_thumb_chat_tools())
    if roteiro_mongo_enabled:
        from .roteiro_mongo import chat_tool_specs as _rv_mongo_chat_tools
        from .roteiro_scripts_mongo import chat_tool_specs as _rv_scripts_chat_tools
        from .roteiro_scripts_write import chat_tool_specs as _rv_scripts_write_tools

        tools.extend(_rv_mongo_chat_tools())
        tools.extend(_rv_scripts_chat_tools())
        tools.extend(_rv_scripts_write_tools())
    if roteiro_api_enabled:
        from .roteiro_api_ops import chat_tool_specs as _rv_api_chat_tools
        from .book_cover_studio import chat_tool_specs as _book_cover_studio_tools
        from .roteiro_book_cover import chat_tool_specs as _book_cover_chat_tools

        tools.extend(_rv_api_chat_tools())
        tools.extend(_book_cover_chat_tools())
        tools.extend(_book_cover_studio_tools())
    from .chat_creative_widgets import chat_tool_specs as _creative_widget_tools

    tools.extend(_creative_widget_tools())
    # Team WhatsApp association tool — always available when teams are visible
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_whatsapp",
                "description": (
                    "Gerencia números/grupos WhatsApp associados a um time. "
                    "Ações: list (listar todos), get (ver de um time), add (adicionar), "
                    "remove (remover), set (substituir todos), broadcast (enviar mensagem "
                    "para números individuais), send_to_group (enviar mensagem para o grupo "
                    "WhatsApp do time), get_emails (ver emails de notificação do time), "
                    "set_emails (definir emails de notificação do time). "
                    "Use send_to_group quando quiser enviar mensagem para o grupo do time. "
                    "Use get_emails/set_emails quando quiser associar, vincular, listar ou "
                    "remover emails de notificação de um time. "
                    "Use quando o usuário pedir para associar, vincular, listar, remover "
                    "whatsapp ou emails de um time ou enviar mensagem para o time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "get", "add", "remove", "set", "broadcast", "send_to_group", "get_emails", "set_emails"],
                            "description": (
                                "Ação: list=todos os times com WhatsApp, get=números de um time, "
                                "add=adicionar número, remove=remover número, "
                                "set=substituir todos, broadcast=enviar para números individuais, "
                                "send_to_group=enviar para o grupo WhatsApp do time, "
                                "get_emails=ver emails de notificação, "
                                "set_emails=definir emails de notificação"
                            ),
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID/slug do time (obrigatório exceto para action=list)",
                        },
                        "number": {
                            "type": "string",
                            "description": "Número ou JID WhatsApp (para add/remove)",
                        },
                        "numbers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de números (para action=set)",
                        },
                        "emails": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Lista de emails de notificação (para action=set_emails)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Mensagem para broadcast ou send_to_group",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Caminho do arquivo (imagem/vídeo/documento) para enviar junto com a mensagem via send_to_group",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Legenda para o arquivo (imagem/vídeo) enviado via send_to_group",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_whatsapp_individual",
                "description": (
                    "Vincula ou desvincula um número WhatsApp individual (ou grupo @g.us) "
                    "a um time. Ações: add (vincular), remove (desvincular), list (listar). "
                    "Use quando o usuário pedir para vincular, associar, adicionar, desvincular, "
                    "remover ou listar números WhatsApp individuais de um time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "remove", "list"],
                            "description": "add=vincular número, remove=desvincular, list=listar",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID/slug do time",
                        },
                        "number": {
                            "type": "string",
                            "description": "Número ou JID WhatsApp (ex: 5585999990000 ou 120363...@g.us)",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_git",
                "description": (
                    "Operações Git (pull, push, status, clone) nos repositórios GitHub "
                    "associados a um time. Ações: list (todos os times com repos), "
                    "repos (listar repos do time), status (git status em lote), "
                    "pull (atualizar todos), push (add+commit+push com mudanças), "
                    "clone (clonar repos em falta), dry_run (simular push). "
                    "Use quando o usuário pedir pull, push, baseline, sincronizar ou "
                    "atualizar repos do time no GitHub."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "repos", "status", "pull", "push", "clone", "dry_run"],
                            "description": (
                                "list=todos os times, repos=repos do time, status=git status, "
                                "pull=git pull --rebase, push=add+commit+push, clone=gh repo clone, "
                                "dry_run=simular push"
                            ),
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID ou nome do time (obrigatório exceto action=list)",
                        },
                        "base_dir": {
                            "type": "string",
                            "description": "Diretório base local dos clones (opcional)",
                        },
                        "commit_message": {
                            "type": "string",
                            "description": "Mensagem de commit para action=push",
                        },
                        "repo_name": {
                            "type": "string",
                            "description": "Operar só neste repo (nome curto, opcional)",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Simular push sem executar (alternativa a action=dry_run)",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_contacts",
                "description": (
                    "Gerencia a lista de integrantes/membros de um time (contatos). "
                    "Cada contato pode ter: name, email, role, phone, whatsapp, notes, department. "
                    "Ações: get (listar todos), upsert (adicionar ou atualizar por email/id), "
                    "remove (remover por email ou id). "
                    "IMPORTANTE: chame automaticamente com action=upsert SEMPRE que o usuário "
                    "mencionar dados de uma pessoa (nome, email, telefone, cargo, whatsapp) "
                    "em qualquer mensagem — mesmo que não peça explicitamente para cadastrar. "
                    "Extraia e persista o contato sem aguardar confirmação. "
                    "Use o team_id do time selecionado na conversa."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["get", "upsert", "remove"],
                            "description": "get=listar contatos, upsert=adicionar/atualizar, remove=remover",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID/slug do time",
                        },
                        "contact": {
                            "type": "object",
                            "description": (
                                "Dados do contato para upsert. Campos: "
                                "name (nome completo), email, role (cargo/papel), "
                                "phone (telefone), whatsapp (número WhatsApp), "
                                "department (departamento/setor), notes (observações). "
                                "Se o contato já existir (mesmo email ou id), é atualizado."
                            ),
                            "properties": {
                                "name": {"type": "string"},
                                "email": {"type": "string"},
                                "role": {"type": "string"},
                                "phone": {"type": "string"},
                                "whatsapp": {"type": "string"},
                                "department": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                        },
                        "id": {
                            "type": "string",
                            "description": "ID ou email do contato a remover (para action=remove)",
                        },
                    },
                    "required": ["action", "team_id"],
                },
            },
        }
    )
    if allowlist_enabled:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_allowlist_list",
                    "description": (
                        "Lista destinatários da allowlist WhatsApp (DMs e grupos). "
                        "Use antes de afirmar que um grupo não está permitido — ver "
                        "enabled_group_jids e entries com jid @g.us."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_allowlist_add",
                    "description": (
                        "BLOQUEADO no chat — cadastro só via dashboard /allowlist. "
                        "Não tente adicionar; use qclaw_allowlist_list para ver o que "
                        "já está permitido."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Nome do destinatário",
                            },
                            "phone": {
                                "type": "string",
                                "description": (
                                    "Número completo com código do país "
                                    "(ex: +5585991736779 ou 558591736779@s.whatsapp.net)"
                                ),
                            },
                            "label": {
                                "type": "string",
                                "description": "Label opcional (ex: suporte, dev)",
                            },
                        },
                        "required": ["name", "phone"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_allowlist_remove",
                    "description": (
                        "Remove um destinatário da allowlist WhatsApp pelo ID."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "ID do registro na allowlist",
                            },
                        },
                        "required": ["id"],
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "qclaw_allowlist_toggle",
                    "description": (
                        "Ativa ou desativa um destinatário na allowlist WhatsApp."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "ID do registro na allowlist",
                            },
                            "enabled": {
                                "type": "boolean",
                                "description": "true para ativar, false para desativar",
                            },
                        },
                        "required": ["id", "enabled"],
                    },
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_show_media",
                "description": (
                    "Exibe uma imagem ou vídeo inline na bubble do chat. "
                    "Aceita caminho local (PNG/JPG/WebP/GIF/MP4/WebM/MOV) ou URL https/http. "
                    "Imagens locais são codificadas em data URL; vídeos locais só são "
                    "codificados se ≤ 4 MB (acima disso, forneça URL externa). "
                    "Use para mostrar artefatos gerados, frames, gráficos, screenshots ou clipes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path_or_url": {
                            "type": "string",
                            "description": (
                                "Caminho absoluto local OU URL https/http. "
                                "Ex.: '/tmp/grafico.png' ou 'https://cdn/clip.mp4'."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["auto", "image", "video"],
                            "description": (
                                "Tipo do conteúdo. 'auto' (default) infere pela extensão/MIME."
                            ),
                        },
                        "caption": {
                            "type": "string",
                            "description": "Legenda curta mostrada abaixo da mídia.",
                        },
                        "poster_path_or_url": {
                            "type": "string",
                            "description": (
                                "Opcional para vídeo: imagem de capa (poster). "
                                "Caminho local ou URL https/http."
                            ),
                        },
                    },
                    "required": ["path_or_url"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_show_slides_pdf",
                "description": (
                    "Renderiza páginas de PDF ou apresentação (PPTX/PPT/ODP/KEY) "
                    "como imagens PNG e exibe inline no chat. "
                    "Use quando o usuário pedir preview de slides/PDF, "
                    "ver páginas do deck, ou revisar layout de apresentação. "
                    "Requer Poppler (PDF) e LibreOffice (slides)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Caminho absoluto do PDF ou deck "
                                "(.pdf, .pptx, .ppt, .odp, .key)."
                            ),
                        },
                        "pages": {
                            "type": "string",
                            "description": (
                                "Intervalo opcional de páginas: '3', '1-5'. "
                                "Omitir para as primeiras max_pages."
                            ),
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": (
                                "Máximo de páginas a exibir por turno (default 12, máx. 12)."
                            ),
                        },
                        "dpi": {
                            "type": "integer",
                            "description": (
                                "Resolução de renderização (default 150). "
                                "Use 120 se as imagens ficarem grandes."
                            ),
                        },
                        "whatsapp_to": {
                            "type": "string",
                            "description": (
                                "Destinatário WhatsApp (nome, telefone ou JID). "
                                "Ao concluir o preview, envia as páginas renderizadas automaticamente."
                            ),
                        },
                        "whatsapp_caption": {
                            "type": "string",
                            "description": (
                                "Legenda opcional nas imagens enviadas no WhatsApp."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_slides_corner_decor",
                "description": (
                    "Analisa ou decora cantos inferiores de slides (PPTX/PPT/HTML) com "
                    "ilustrações geradas por Nano Banana ou Grok, apenas onde houver "
                    "espaço livre. Use quando o usuário pedir arte nos cantos do deck, "
                    "decorar apresentação existente, ilustração no rodapé do slide, "
                    "ou enriquecer slides com mascote/ícone. Com analyze=true só "
                    "reporta cantos livres (sem gerar imagens)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Caminho absoluto do deck (.pptx, .ppt, .html, .htm)."
                            ),
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": (
                                "Se true, apenas analisa cantos livres (sem API de imagem)."
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Prompt base da ilustração de canto "
                                "(obrigatório quando analyze=false)."
                            ),
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Caminho de saída do deck decorado (opcional).",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["imagen", "nano", "grok"],
                            "description": "Gerador: nano (Gemini) ou grok (xAI).",
                        },
                        "corner": {
                            "type": "string",
                            "enum": ["auto", "bottom-left", "bottom-right"],
                            "description": (
                                "Canto alvo. auto escolhe o mais livre em PPTX."
                            ),
                        },
                        "slides": {
                            "type": "string",
                            "description": "Slides: all, 2, 1-5, 2,4,7",
                        },
                        "size_ratio": {
                            "type": "number",
                            "description": (
                                "Tamanho da ilustração vs menor dimensão (default 0.18)."
                            ),
                        },
                        "max_overlap": {
                            "type": "number",
                            "description": (
                                "Máx. fração ocupada do canto antes de ignorar (default 0.12)."
                            ),
                        },
                        "assets_dir": {
                            "type": "string",
                            "description": "Pasta para PNGs geradas (opcional).",
                        },
                    },
                    "required": ["path"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_slides_replace_didactic",
                "description": (
                    "Substitui um ou mais slides de PPTX/PPT/HTML por imagem didática "
                    "16:9 gerada por Nano Banana ou Grok, usando o texto do slide no "
                    "prompt. Use quando o usuário pedir substituir slide por ilustração "
                    "explicativa, converter slide em infográfico visual, ou trocar "
                    "página da apresentação por imagem didática. Com analyze=true só "
                    "extrai conteúdo dos slides (sem gerar imagens)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Caminho absoluto do deck (.pptx, .ppt, .html, .htm)."
                            ),
                        },
                        "slides": {
                            "type": "string",
                            "description": "Slides a substituir: 3, 2-5, 2,4,7",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": (
                                "Se true, apenas extrai conteúdo dos slides "
                                "(sem API de imagem)."
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Tema opcional; se vazio, usa texto extraído do slide."
                            ),
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Caminho de saída do deck (opcional).",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["imagen", "nano", "grok"],
                            "description": "Gerador: nano (Gemini) ou grok (xAI).",
                        },
                        "style": {
                            "type": "string",
                            "description": "Modifier de estilo visual didático (opcional).",
                        },
                        "keep_title": {
                            "type": "boolean",
                            "description": (
                                "Manter título PPTX sobre a imagem (default false)."
                            ),
                        },
                        "assets_dir": {
                            "type": "string",
                            "description": "Pasta para PNGs geradas (opcional).",
                        },
                        "allow_fallback": {
                            "type": "boolean",
                            "description": (
                                "Se false (ou no_fallback=true), mantém o provider/modelo "
                                "escolhido em todo o batch — sem trocar para nano/imagen."
                            ),
                        },
                        "no_fallback": {
                            "type": "boolean",
                            "description": "Alias de allow_fallback=false",
                        },
                        "model": {
                            "type": "string",
                            "description": "Modelo preferido (ex.: grok-imagine-image-quality)",
                        },
                    },
                    "required": ["path", "slides"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_slides_batch_images",
                "description": (
                    "Gera 100–200 slides como imagens PNG em batch com um único modelo "
                    "(provider + model + no_fallback) e empacota num ZIP anexado para "
                    "download no chat. Aceita deck PPTX/HTML (extrai prompt por slide) "
                    "ou ficheiro JSON/JSONL de prompts. Skill: qclaw-slides-batch-images. "
                    "MCP equivalente: slides_batch_images. Com analyze=true só lista prompts. "
                    "Corrige automaticamente empty_prompt (Excel truncado) quando card_id/task_id informado."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Deck PPTX/HTML para extrair slides",
                        },
                        "prompts_file": {
                            "type": "string",
                            "description": "JSON/JSONL com prompts por slide",
                        },
                        "slides": {
                            "type": "string",
                            "description": "Filtro: all, 3, 2-5 (só com path)",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": "Só listar prompts, sem gerar imagens",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Override de tema (modo deck)",
                        },
                        "style": {
                            "type": "string",
                            "description": "Modifier visual no prompt didático",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["nano", "grok", "imagen"],
                            "description": "Gerador: nano (Gemini), grok, imagen",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "description": "Resolução Nano Banana (default 2K)",
                        },
                        "max_slides": {
                            "type": "integer",
                            "description": "Limite de segurança (default 200)",
                        },
                        "preview_count": {
                            "type": "integer",
                            "description": "Imagens inline no chat (default 3)",
                        },
                        "resume": {
                            "type": "boolean",
                            "description": "Retomar slides cujo PNG já existe",
                        },
                        "delay_seconds": {
                            "type": "number",
                            "description": "Pausa entre chamadas API (default 2s)",
                        },
                        "workers": {
                            "type": "integer",
                            "description": "Workers paralelos (default 1, max 8)",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta de saída das PNGs (opcional)",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho do ZIP (opcional)",
                        },
                        "allow_fallback": {
                            "type": "boolean",
                            "description": (
                                "Se false (ou no_fallback=true), mantém o provider escolhido "
                                "(ex.: grok) em todos os slides do batch."
                            ),
                        },
                        "no_fallback": {
                            "type": "boolean",
                            "description": "Alias de allow_fallback=false",
                        },
                        "model": {
                            "type": "string",
                            "description": "Modelo preferido (ex.: grok-imagine-image-quality)",
                        },
                        "card_id": {
                            "type": "string",
                            "description": "Card Kanban (ex. TASK-038) para auto-correção de prompts/xlsx",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Alias de card_id (ex. TASK-038 ou 038)",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Raiz do time (opcional; auto-detecta pelo card)",
                        },
                        "no_auto_fix_prompts": {
                            "type": "boolean",
                            "description": "Desliga correção automática de empty_prompt",
                        },
                    },
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_slides_narration",
                "description": (
                    "Gera locução/narração falada para cada slide de uma lição "
                    "(até 200). Aceita deck PPTX/HTML (extrai texto) ou JSON/JSONL "
                    "de slides. Saída: narration.jsonl, Markdown teleprompter, "
                    "heygen_scripts.json e ZIP anexado. Use quando pedir fala por "
                    "slide, narração de apresentação, script para narrador/HeyGen/TTS. "
                    "Com analyze=true só lista slides."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Deck PPTX/HTML para extrair slides",
                        },
                        "slides_file": {
                            "type": "string",
                            "description": "JSON/JSONL com {slide,title,content}",
                        },
                        "slides": {
                            "type": "string",
                            "description": "Filtro: all, 3, 2-5 (só com path)",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": "Só listar slides, sem LLM",
                        },
                        "lesson_title": {
                            "type": "string",
                            "description": "Título da lição (contexto)",
                        },
                        "lesson_context": {
                            "type": "string",
                            "description": "Objetivo ou contexto da lição",
                        },
                        "tone": {
                            "type": "string",
                            "enum": ["didático", "dinâmico", "formal"],
                        },
                        "language": {
                            "type": "string",
                            "description": "Idioma da locução (default pt-BR)",
                        },
                        "target_seconds": {
                            "type": "number",
                            "description": "Meta de duração por slide (default 25)",
                        },
                        "wpm": {
                            "type": "number",
                            "description": "Palavras/min para estimar tempo (default 140)",
                        },
                        "model": {
                            "type": "string",
                            "description": "Modelo texto (default gemini-3.5-flash)",
                        },
                        "max_slides": {
                            "type": "integer",
                            "description": "Limite de segurança (default 200)",
                        },
                        "workers": {
                            "type": "integer",
                            "description": "Paralelismo LLM (default 4)",
                        },
                        "preview_count": {
                            "type": "integer",
                            "description": "Amostras de fala no resultado (default 3)",
                        },
                        "resume": {
                            "type": "boolean",
                            "description": "Retomar slides cujo .txt já existe",
                        },
                        "delay_seconds": {
                            "type": "number",
                            "description": "Pausa entre chamadas (default 0.5s)",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta de saída (opcional)",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho do ZIP (opcional)",
                        },
                    },
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_elevenlabs_narrate",
                "description": (
                    "Gera áudio MP3 de narração com ElevenLabs TTS. Modo único: "
                    "`text` ou `text_file`. Modo batch: `narration_file` (JSON/JSONL "
                    "de qclaw_slides_narration). Com analyze=true lista vozes. "
                    "Requer ELEVENLABS_API_KEY em /chaves."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Texto para uma narração única",
                        },
                        "text_file": {
                            "type": "string",
                            "description": "Ficheiro .txt com roteiro",
                        },
                        "narration_file": {
                            "type": "string",
                            "description": "JSON/JSONL com {slide,title,narration}",
                        },
                        "voice_id": {
                            "type": "string",
                            "description": "Voice ID ElevenLabs",
                        },
                        "model_id": {
                            "type": "string",
                            "description": "Modelo TTS (default eleven_multilingual_v2)",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do MP3 (modo único)",
                        },
                        "stability": {"type": "number"},
                        "similarity_boost": {"type": "number"},
                        "max_items": {
                            "type": "integer",
                            "description": "Limite batch (default 200)",
                        },
                        "delay_seconds": {
                            "type": "number",
                            "description": "Pausa entre slides no batch",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": "Listar vozes ElevenLabs",
                        },
                        "output_dir": {"type": "string"},
                        "zip_path": {"type": "string"},
                    },
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_gemini_music_generate",
                "description": (
                    "Gera música com Google Gemini Lyria (text-to-music). Suporta "
                    "instrumental, letras opcionais e até 10 imagens de referência. "
                    "Modelos: lyria-3-clip-preview (mp3) ou lyria-3-pro-preview "
                    "(mp3/wav). Requer GEMINI_API_KEY em /chaves. Skill: "
                    "qclaw-chat-gemini-music."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Estilo, género, mood e instrumentação",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do ficheiro (ex. 2026-06-17-lofi.mp3)",
                        },
                        "model": {
                            "type": "string",
                            "enum": [
                                "lyria-3-clip-preview",
                                "lyria-3-pro-preview",
                            ],
                        },
                        "lyrics": {
                            "type": "string",
                            "description": "Letra exacta a cantar",
                        },
                        "instrumental": {
                            "type": "boolean",
                            "description": "Sem vocais",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["mp3", "wav"],
                        },
                        "input_image_path": {
                            "type": "string",
                            "description": "Imagem de referência",
                        },
                        "input_image_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "output_dir": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["prompt"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_gemini_computer_use",
                "description": (
                    "Agente visual Gemini Computer Use + Playwright: vê screenshots "
                    "e executa click/type/scroll no browser até concluir a tarefa. "
                    "Requer GEMINI_API_KEY em /chaves. Skill: "
                    "qclaw-chat-gemini-computer-use. Para desktop nativo use Touchpoint."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Tarefa em linguagem natural",
                        },
                        "start_url": {
                            "type": "string",
                            "description": "URL inicial (default google.com)",
                        },
                        "model": {
                            "type": "string",
                            "enum": [
                                "gemini-3.5-flash",
                                "gemini-3-flash-preview",
                                "gemini-2.5-computer-use-preview-10-2025",
                            ],
                        },
                        "max_turns": {
                            "type": "integer",
                            "description": "Máximo de passos (default 8)",
                        },
                        "headless": {
                            "type": "boolean",
                            "description": "Browser sem UI (default true)",
                        },
                        "screen_width": {"type": "integer"},
                        "screen_height": {"type": "integer"},
                        "enable_prompt_injection_detection": {
                            "type": "boolean",
                        },
                    },
                    "required": ["task"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_curso_notebooks_docker",
                "description": (
                    "Gera notebooks Jupyter (.ipynb) com código executável e "
                    "docker-compose.yml para lições de curso em batch (até 200). "
                    "Aceita JSON/JSONL de lições ou PLANO.md do curso-builder. "
                    "Use quando pedir laboratórios práticos, notebooks de aula, "
                    "ambiente Docker para curso, ou gerar 50/100/200 aulas com código. "
                    "Com analyze=true só valida lições sem gerar ficheiros."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lessons_file": {
                            "type": "string",
                            "description": "JSON/JSONL com lições do curso",
                        },
                        "plano": {
                            "type": "string",
                            "description": "PLANO.md gerado pelo curso-builder",
                        },
                        "course_slug": {
                            "type": "string",
                            "description": "Identificador do curso (slug)",
                        },
                        "course_title": {
                            "type": "string",
                            "description": "Título legível do curso",
                        },
                        "stack": {
                            "type": "string",
                            "enum": [
                                "python",
                                "python-data",
                                "python-ml",
                                "node",
                                "fullstack",
                                "sql",
                                "devops",
                            ],
                            "description": "Stack Docker (default python-data)",
                        },
                        "level": {
                            "type": "string",
                            "description": "Nível: iniciante, intermediario, avancado",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": "Só validar lições, sem gerar",
                        },
                        "max_lessons": {
                            "type": "integer",
                            "description": "Limite de segurança (default 200)",
                        },
                        "workers": {
                            "type": "integer",
                            "description": "Paralelismo na geração (default 4)",
                        },
                        "hide_solutions": {
                            "type": "boolean",
                            "description": "Omitir células de solução",
                        },
                        "resume": {
                            "type": "boolean",
                            "description": "Ignorar notebooks já existentes",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta de saída (opcional)",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho do ZIP (opcional)",
                        },
                    },
                },
            },
        }
    )
    from .roteiro_books import chat_tool_specs as _book_chat_tools

    tools.extend(_book_chat_tools())
    from .roteiro_book_sync import chat_tool_specs as _book_sync_chat_tools

    tools.extend(_book_sync_chat_tools())
    from .roteiro_course_sync import chat_tool_specs as _course_sync_chat_tools

    tools.extend(_course_sync_chat_tools())
    from .roteiro_book_latex import chat_tool_specs as _book_latex_chat_tools

    tools.extend(_book_latex_chat_tools())
    from .roteiro_courses import chat_tool_specs as _course_chat_tools

    tools.extend(_course_chat_tools())
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_modulo_portal",
                "description": (
                    "Monta módulo de curso com slides, imagens, áudio, PDF e vídeo "
                    "num pacote ZIP + HTML estático pronto para portal LMS/Hotmart. "
                    "Aceita manifest JSON/JSONL ou auto-scan de pasta de assets. "
                    "Use quando pedir módulo para portal, empacotar aula com mídia, "
                    "HTML de entrega ou juntar slides + áudio + vídeo + PDF. "
                    "Com analyze=true só lista secções."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assets_dir": {
                            "type": "string",
                            "description": "Pasta com ficheiros de mídia (obrigatório)",
                        },
                        "manifest": {
                            "type": "string",
                            "description": "JSON/JSONL com secções do módulo",
                        },
                        "module_id": {
                            "type": "string",
                            "description": "Slug do módulo",
                        },
                        "module_title": {
                            "type": "string",
                            "description": "Título se não estiver no manifest",
                        },
                        "auto_scan": {
                            "type": "boolean",
                            "description": "Detectar mídia por extensão na pasta",
                        },
                        "analyze": {
                            "type": "boolean",
                            "description": "Só listar secções, sem gerar ficheiros",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta de saída (opcional)",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho do ZIP (opcional)",
                        },
                    },
                    "required": ["assets_dir"],
                },
            },
        }
    )
    tools += [
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_memoria_index",
                "description": (
                    "Busca threads do Gmail para cada membro do time e salva no "
                    "MongoDB (coleções email_team_members, email_threads) com "
                    "metadados de squad, papel e direção (from/to/both). Use quando "
                    "o usuário pedir 'guardar emails em memória', 'indexar emails "
                    "do time', 'salvar emails no banco', 'persistir emails'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team": {
                            "type": "array",
                            "description": (
                                "Lista de membros do time. Cada item: "
                                "{name, email, squad?, role?, slack_handle?}."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "email": {"type": "string"},
                                    "squad": {"type": "string"},
                                    "role": {"type": "string"},
                                    "slack_handle": {"type": "string"},
                                },
                                "required": ["name", "email"],
                            },
                        },
                        "threads": {
                            "type": "object",
                            "description": (
                                "Dict {email: [thread,...]} com threads já buscadas "
                                "via search_threads do Gmail MCP."
                            ),
                        },
                        "full": {
                            "type": "boolean",
                            "description": "Salvar raw_json completo de cada thread (default false).",
                        },
                    },
                    "required": ["team"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_memoria_search",
                "description": (
                    "Busca nos emails indexados no MongoDB (assunto, snippet, "
                    "membro, squad). Use quando o usuário pedir 'buscar email "
                    "salvo', 'pesquisar email sobre [assunto]', 'email do fulano "
                    "sobre X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Texto a buscar (assunto, snippet, nome do membro, squad).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_memoria_list",
                "description": (
                    "Lista threads de email indexadas no MongoDB, opcionalmente "
                    "filtradas por email do membro. Use para 'listar emails do "
                    "fulano', 'ver emails do banco', 'mostrar emails persistidos'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "member_email": {
                            "type": "string",
                            "description": "Filtrar por email do membro (opcional).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_email_memoria_summary",
                "description": (
                    "Retorna estatísticas de emails indexados no MongoDB agrupadas "
                    "por squad/membro: total de threads, não lidas, etc. Use para "
                    "'resumo de emails do time', 'quantos emails temos por squad', "
                    "'estatísticas de email'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- AI Engineer knowledge base ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_ai_kb_store",
                "description": (
                    "Guarda técnica, paper, padrão arquitetural ou nota de IA no banco "
                    "local (ai_engineer_kb.sqlite3). Use após pesquisa web ou quando o "
                    "usuário pedir 'guardar no banco de conhecimento', 'aprender e "
                    "persistir técnica', 'salvar paper/arquitetura'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Título curto da entrada."},
                        "category": {
                            "type": "string",
                            "description": (
                                "architecture|technique|paper|tool|pattern|framework|"
                                "benchmark|safety|mlops|rag|agent"
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "Conteúdo completo: resumo técnico, trade-offs, quando usar.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Resumo em 1-2 frases (opcional).",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags para busca (ex: rag, agents, vllm).",
                        },
                        "source_url": {
                            "type": "string",
                            "description": "URL da fonte (paper, docs, blog).",
                        },
                        "source_type": {
                            "type": "string",
                            "description": "web|paper|arxiv|github|conversation|internal",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0-1 confiança na informação (default 0.8).",
                        },
                    },
                    "required": ["title", "category", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ai_kb_search",
                "description": (
                    "Busca full-text no banco de conhecimento de IA. Use antes de "
                    "pesquisar na web: 'o que já sabemos sobre X', 'buscar no banco "
                    "de IA', 'técnica salva sobre RAG'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Termos de busca FTS."},
                        "category": {
                            "type": "string",
                            "description": "Filtrar por categoria (opcional).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default 20).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ai_kb_list",
                "description": (
                    "Lista entradas do banco de conhecimento de IA por categoria ou tag. "
                    "Use para 'listar papers salvos', 'ver arquiteturas no banco'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Filtrar categoria."},
                        "tag": {"type": "string", "description": "Filtrar por tag parcial."},
                        "limit": {"type": "integer", "description": "Máximo (default 30)."},
                        "offset": {"type": "integer", "description": "Paginação."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ai_kb_summary",
                "description": (
                    "Estatísticas do banco de conhecimento de IA: total, por categoria, "
                    "entradas recentes. Use para 'resumo do banco de IA', 'quantas "
                    "técnicas guardadas'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- Ruflo swarm scripts + architecture ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_ruflo_swarm_architecture",
                "description": (
                    "Mostra a arquitetura Ruflo neste repo: diagrama ASCII, componentes "
                    "(MCP Cursor, /chat/ruflo, scripts, Hermes swarm), inventário de "
                    "scripts e resumo do ruflo doctor. Use para 'arquitetura ruflo', "
                    "'como funciona o enxame', 'mapa ruflo'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_doctor": {
                            "type": "boolean",
                            "description": "Executar npx ruflo doctor (default true).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ruflo_swarm_audit",
                "description": (
                    "Audita todos os scripts *ruflo* em scripts/ (setup, troubleshoot, "
                    "orchestration). Use para 'auditar scripts ruflo', 'revisar bash ruflo'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root": {
                            "type": "string",
                            "description": "Raiz do repo (opcional, default projeto).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ruflo_swarm_validate",
                "description": (
                    "Valida convenções de um script bash Ruflo (set -euo, REPO_ROOT, "
                    "CLI ruflo@latest). Use para 'corrigir script ruflo', 'lint script swarm'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Caminho do script (ex. scripts/setup-ruflo-dev.sh).",
                        },
                        "content": {
                            "type": "string",
                            "description": "Conteúdo inline do script (alternativa a path).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ruflo_swarm_plan",
                "description": (
                    "Gera plano de enxame Ruflo: topologia, agentes, comandos CLI/MCP e "
                    "diagrama. Use antes de criar script ou iniciar swarm para 'planejar "
                    "enxame', 'ruflo dev/research/test'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Objetivo do enxame (obrigatório).",
                        },
                        "task_type": {
                            "type": "string",
                            "description": (
                                "development|research|testing|security|refactor "
                                "(auto se omitido)."
                            ),
                        },
                    },
                    "required": ["task"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_ruflo_swarm_scaffold",
                "description": (
                    "Gera scripts/ruflo-swarm-<nome>.sh com init/start/spawn Ruflo. "
                    "Use para 'criar script de enxame ruflo', 'gerar bash swarm'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Slug do script (ex. auth-feature).",
                        },
                        "task": {
                            "type": "string",
                            "description": "Objetivo passado ao swarm start -o.",
                        },
                        "task_type": {
                            "type": "string",
                            "description": "development|research|testing|security|refactor",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Diretório de saída (default scripts/).",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Substituir se o ficheiro já existir.",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        # --- Memória de aulas, notas e skills de estudo ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_aula_store",
                "description": (
                    "Cria ou atualiza uma aula na memória acadêmica (MongoDB). "
                    "Use para 'guardar aula', 'registrar disciplina', 'anotar aula de hoje'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Título da aula."},
                        "discipline": {"type": "string", "description": "Disciplina/matéria."},
                        "professor": {"type": "string", "description": "Nome do professor."},
                        "course": {"type": "string", "description": "Curso ou turma."},
                        "institution": {"type": "string", "description": "Instituição."},
                        "description": {"type": "string", "description": "Resumo ou pauta."},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags para busca.",
                        },
                        "aula_id": {
                            "type": "integer",
                            "description": "ID para atualizar aula existente (opcional).",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_aula_search",
                "description": (
                    "Busca full-text em aulas salvas. Use para 'achar aula sobre X', "
                    "'buscar disciplina Y'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Termos de busca."},
                        "discipline": {"type": "string", "description": "Filtrar disciplina."},
                        "limit": {"type": "integer", "description": "Máximo de resultados."},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_aula_list",
                "description": "Lista aulas salvas, opcionalmente por disciplina.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "discipline": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_nota_store",
                "description": (
                    "Guarda anotação, prova ou trabalho no banco. Use para 'anotar aula', "
                    "'registrar nota da prova', 'salvar resumo'. "
                    "Com nota_id: atualiza registo existente; se já houver grade, omitir grade "
                    "preserva a nota (não apagar para sem nota)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string", "description": "Texto da anotação."},
                        "aula_id": {"type": "integer", "description": "Vincular à aula."},
                        "note_type": {
                            "type": "string",
                            "description": "anotacao|prova|trabalho|resumo|exercicio|outro",
                        },
                        "grade": {"type": "number", "description": "Nota numérica (opcional)."},
                        "max_grade": {"type": "number", "description": "Nota máxima (ex.: 10)."},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "nota_id": {"type": "integer", "description": "Atualizar nota existente."},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_nota_search",
                "description": "Busca full-text em notas e anotações salvas.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "note_type": {"type": "string"},
                        "aula_id": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_nota_list",
                "description": "Lista notas salvas por aula ou tipo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "aula_id": {"type": "integer"},
                        "note_type": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_study_skill_store",
                "description": (
                    "Registra competência ou habilidade aprendida. Use para 'skill aprendida', "
                    "'competência dominada', 'habilidade da aula'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome da skill/competência."},
                        "aula_id": {"type": "integer"},
                        "category": {"type": "string"},
                        "level": {
                            "type": "string",
                            "description": "iniciante|intermediario|avancado|dominado",
                        },
                        "description": {"type": "string"},
                        "proficiency": {
                            "type": "number",
                            "description": "0-100 domínio estimado.",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "skill_id": {"type": "integer", "description": "Atualizar skill existente."},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_study_skill_search",
                "description": "Busca competências/skills de estudo no banco.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "level": {"type": "string"},
                        "aula_id": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_study_skill_list",
                "description": "Lista skills de estudo por categoria, nível ou aula.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "aula_id": {"type": "integer"},
                        "category": {"type": "string"},
                        "level": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_aulas_memoria_show",
                "description": (
                    "Painel completo da memória acadêmica (MongoDB): todas as aulas, notas "
                    "e competências formatadas em markdown. Use para 'mostrar memória do "
                    "projeto', 'ver tudo que está salvo', 'painel de estudos'. Retorna "
                    "campo markdown pronto para exibir. NÃO verificar ficheiro SQLite."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de itens por coleção (default 50).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_aulas_memoria_summary",
                "description": (
                    "Resumo da memória acadêmica: totais de aulas/notas/skills, média de "
                    "notas, itens recentes. Use para 'resumo do semestre', 'visão geral dos estudos'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_memclaw_memoria_show",
                "description": (
                    "Painel completo da memória MemClaw (longo prazo): stats, keystones "
                    "e memórias recentes ou busca semântica. Use para 'ver memória MemClaw', "
                    "'mostrar o que está salvo na memória persistente', 'listar decisões "
                    "anteriores'. Retorna campo markdown pronto. Distinto de memória "
                    "acadêmica (qclaw_aulas_memoria_show) e email/WhatsApp."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de memórias (default 30).",
                        },
                        "scope": {
                            "type": "string",
                            "description": "Escopo de leitura: agent (default), fleet, all.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Busca semântica opcional (memclaw_recall).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_memclaw_memoria_summary",
                "description": (
                    "Resumo leve do MemClaw: contagens e saúde do store. Use para "
                    "'quantas memórias no MemClaw', 'status da memória persistente'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "description": "Escopo: agent (default), fleet, all.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_memclaw_memoria_search",
                "description": (
                    "Busca semântica no MemClaw (memclaw_recall). Use para 'o que "
                    "decidimos sobre X', 'memórias sobre deploy', 'última vez que falamos de Y'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Tema ou pergunta para recall semântico.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default 20).",
                        },
                        "scope": {
                            "type": "string",
                            "description": "Escopo: agent (default), fleet, all.",
                        },
                        "include_brief": {
                            "type": "boolean",
                            "description": "Incluir síntese em um parágrafo (default true).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_evaluate_agents",
                "description": (
                    "Avalia agentes Hermes e robôs do swarm criados: SOUL, modelo, cron, "
                    "skills, kanban e saúde operacional. Retorna nota 0–100 por agente. "
                    "Use para 'avaliar agente', 'auditar robô', 'score dos agentes', "
                    "checklist pós-criação de swarm, ou verificar se agentes são stubs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Slug de um agente específico (opcional)",
                        },
                        "host": {
                            "type": "string",
                            "description": (
                                "URL do gois para enriquecer com cron/swarm "
                                "(ex. http://127.0.0.1:9101). Opcional."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_fix_agents",
                "description": (
                    "Conserta agentes Hermes com problemas comuns: SOUL incompleto, skills "
                    "faltando, cron stub ausente, provider/model desalinhado. Use após "
                    "avaliação com nota baixa ou quando o usuário pedir consertar/reparar "
                    "agente ou robô. Sempre re-avaliar depois com qclaw_evaluate_agents."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": (
                                "Slug do agente a consertar. Se omitido, use swarm_name "
                                "para limitar o lote (evita varrer centenas de perfis)."
                            ),
                        },
                        "swarm_name": {
                            "type": "string",
                            "description": (
                                "Nome do swarm (ex. gois-dev-swarm). Conserta só os perfis "
                                "vinculados a esse swarm com nota < 70."
                            ),
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Se true, simula sem gravar alterações.",
                        },
                        "schedule": {
                            "type": "string",
                            "description": "Schedule do cron se precisar criar (ex. every 24h).",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Workdir para prompt de execução/kanban.",
                        },
                        "host": {
                            "type": "string",
                            "description": "URL do gois para avaliação antes/depois.",
                        },
                    },
                    "required": [],
                },
            },
        },
        # --- WhatsApp Memória (indexação de conversas de grupo) ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_memoria_index",
                "description": (
                    "Busca mensagens dos grupos WhatsApp via wacli e persiste no MongoDB "
                    "(coleções wa_groups, wa_messages) com metadados de grupo, remetente e timestamp. "
                    "Use quando o usuário pedir 'indexar conversas do WhatsApp', "
                    "'salvar mensagens dos grupos em memória', 'guardar histórico WhatsApp', "
                    "'persistir mensagens dos grupos', 'indexar WhatsApp'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "groups": {
                            "type": "array",
                            "description": (
                                "Lista de grupos a indexar [{jid, name?}]. "
                                "Se omitido com all_known=true, indexa todos os grupos conhecidos."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "jid": {"type": "string"},
                                    "name": {"type": "string"},
                                },
                                "required": ["jid"],
                            },
                        },
                        "all_known": {
                            "type": "boolean",
                            "description": "Se true, indexa todos os grupos conhecidos (T10-T12, EDD, Devops, etc).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de mensagens por grupo (default: 200).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_memoria_search",
                "description": (
                    "Busca por texto nas mensagens de grupos WhatsApp já indexadas no MongoDB "
                    "(coleção wa_messages). Para busca ao vivo em qualquer chat (incluindo DMs), "
                    "prefira qclaw_whatsapp_messages_search. "
                    "Use para 'buscar mensagem salva sobre X', 'quem falou de Z no grupo indexado'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Texto a buscar nas mensagens.",
                        },
                        "group": {
                            "type": "string",
                            "description": "Filtrar por JID ou nome parcial do grupo (opcional).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_memoria_list",
                "description": (
                    "Lista mensagens WhatsApp indexadas, filtráveis por grupo ou remetente. "
                    "Use para 'listar mensagens do grupo T12', 'ver histórico WhatsApp do fulano'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group": {
                            "type": "string",
                            "description": "Filtrar por JID ou nome parcial do grupo.",
                        },
                        "sender": {
                            "type": "string",
                            "description": "Filtrar por JID ou nome parcial do remetente.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_memoria_summary",
                "description": (
                    "Estatísticas das mensagens WhatsApp indexadas por grupo: "
                    "total, remetentes únicos, última mensagem. "
                    "Use para 'resumo WhatsApp', 'quantas mensagens indexadas'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- Autoaprimoramento de ferramentas (erros → lições) ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_tool_learning_search",
                "description": (
                    "Busca correções aprendidas de falhas anteriores de ferramentas do chat. "
                    "Use ANTES de retentar uma ferramenta que falhou, ou quando o usuário pedir "
                    "'como corrigir esse erro', 'já vimos esse erro', 'autoaprimorar'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Trecho do erro ou sintoma (ex.: 'store LOCK', 'query vazio').",
                        },
                        "tool": {
                            "type": "string",
                            "description": "Nome da ferramenta (ex.: qclaw_whatsapp_memoria_search).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default 10).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_tool_learning_record",
                "description": (
                    "Regista erro de ferramenta e correção que funcionou (ou tentativa) no banco "
                    "tool_learnings.sqlite3. Use APÓS diagnosticar e aplicar fix, para aprendizado "
                    "persistente entre sessões."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "Nome da ferramenta que falhou.",
                        },
                        "error": {
                            "type": "string",
                            "description": "Mensagem de erro ou sintoma.",
                        },
                        "fix": {
                            "type": "string",
                            "description": "Descrição da correção aplicada ou recomendada.",
                        },
                        "skill": {
                            "type": "string",
                            "description": "Skill relacionada (opcional).",
                        },
                        "error_kind": {
                            "type": "string",
                            "enum": ["param", "auth", "timeout", "not_found", "rate_limit", "lock", "other"],
                            "description": "Classificação do erro.",
                        },
                        "fix_kind": {
                            "type": "string",
                            "enum": ["retry", "param_change", "alternate_tool", "skill_update", "user_action", "other"],
                            "description": "Tipo de correção.",
                        },
                        "attempted_args": {
                            "type": "object",
                            "description": "Args usados na tentativa que falhou (sem secrets).",
                        },
                        "fix_args": {
                            "type": "object",
                            "description": "Args corrigidos para retry bem-sucedido.",
                        },
                        "success": {
                            "type": "boolean",
                            "description": "True se o retry após o fix funcionou.",
                        },
                    },
                    "required": ["tool", "error", "fix"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_tool_learning_list",
                "description": (
                    "Lista lições recentes de autoaprimoramento de ferramentas. "
                    "Use para 'mostrar erros aprendidos', 'histórico de correções'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de registos (default 20).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_tool_learning_stats",
                "description": (
                    "Estatísticas de erros e correções por ferramenta — identifica padrões "
                    "recorrentes que merecem atualização de skill."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_generation_status",
                "description": (
                    "Consulta gerações de imagens/slides **em curso** em qualquer conversa "
                    "do chat (jobs assíncronos). Use quando perguntarem progresso de lote, "
                    "TASK-037, quantos slides já saíram, ou se há geração a correr noutra sessão."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Filtrar por texto no preview/progresso (ex.: TASK-037, nome do deck)"
                            ),
                        },
                        "session_key": {
                            "type": "string",
                            "description": "Restringir a uma sessão específica (opcional)",
                        },
                        "job_id": {
                            "type": "string",
                            "description": "Detalhe de um job específico (opcional)",
                        },
                        "include_non_image": {
                            "type": "boolean",
                            "description": (
                                "Se true, inclui jobs de chat sem geração de imagem (default false)"
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_slides_batch_artifacts",
                "description": (
                    "Localiza slides PNG/ZIP já gerados em disco — mesmo quando o job "
                    "falhou ou o chat disse 'nenhum gerado'. Use após erro de batch, "
                    "'onde estão os slides', ou antes de regenerar tudo. Procura por "
                    "output_dir, job_id ou keyword (ex. d3, prompts-d3-100, TASK-039). "
                    "Skill: qclaw-chat-slides-batch-artifacts. MCP: slides_batch_artifacts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta com slide-NNN.png",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho do ZIP (opcional)",
                        },
                        "prompts_file": {
                            "type": "string",
                            "description": "JSONL para calcular slides em falta",
                        },
                        "job_id": {
                            "type": "string",
                            "description": "Job image_batch (chat_send_jobs)",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "Pesquisa em .stack/chat/artifacts (ex. d3)",
                        },
                        "session_key": {
                            "type": "string",
                            "description": "Staging do ZIP para link de download",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_failures",
                "description": (
                    "Lista falhas recentes do chat QClaw: erros de ferramentas (tool_learnings), "
                    "mensagens de erro no histórico (LLM/backend) e incidentes do monitor. "
                    "Use quando pedir 'mostrar falhas', 'o que falhou', 'diagnóstico do chat' "
                    "ou após ok=false em qualquer ferramenta. Siga a skill qclaw-chat-falhas."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "since_minutes": {
                            "type": "number",
                            "description": "Janela em minutos (default 120).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de falhas (default 40).",
                        },
                        "query": {
                            "type": "string",
                            "description": "Filtrar por texto na mensagem/erro.",
                        },
                        "session_key": {
                            "type": "string",
                            "description": "Limitar falhas de chat a uma sessão.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["monitor", "tool", "chat"],
                            },
                            "description": "Origens a incluir (omitir = todas).",
                        },
                    },
                    "required": [],
                },
            },
        },
        # --- Chat user personality ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_personality_get",
                "description": (
                    "Lê o perfil de personalidade/estilo de resposta do usuário autenticado. "
                    "Use antes de gerar ou aplicar personalização: 'meu perfil de chat', "
                    "'como você se adapta a mim', 'ver personalidade salva'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "ID do usuário autenticado (obrigatório).",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_personality_samples",
                "description": (
                    "Coleta mensagens recentes do usuário no histórico do chat para inferir "
                    "tom, verbosidade e estilo. Use ao gerar perfil: 'analisar como eu escrevo', "
                    "'criar personalidade baseada nas minhas mensagens'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "ID do usuário autenticado (obrigatório).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de mensagens (default 60).",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_personality_save",
                "description": (
                    "Persiste ou atualiza o perfil de personalidade do usuário "
                    "(tone, verbosity, style_guide, system_prompt_addon, etc.). "
                    "Use após inferir estilo: 'salvar meu perfil', 'personalizar chat para mim'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "ID do usuário."},
                        "display_name": {"type": "string"},
                        "tone": {
                            "type": "string",
                            "description": (
                                "formal|casual|friendly|direct|technical|empathetic|coaching"
                            ),
                        },
                        "verbosity": {
                            "type": "string",
                            "description": "concise|balanced|detailed",
                        },
                        "language": {"type": "string", "description": "ex.: pt-BR"},
                        "expertise": {
                            "type": "string",
                            "description": "beginner|intermediate|expert|mixed",
                        },
                        "format_preference": {
                            "type": "string",
                            "description": "bullets|prose|code-first|step-by-step|mixed",
                        },
                        "traits": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "style_guide": {
                            "type": "string",
                            "description": "Markdown com bullets de como responder ao usuário.",
                        },
                        "system_prompt_addon": {
                            "type": "string",
                            "description": "Frases imperativas curtas para adaptar respostas.",
                        },
                        "sample_phrases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "topics": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "avoid": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Comportamentos a evitar (emojis, floreios, etc.).",
                        },
                        "confidence": {"type": "number"},
                        "active": {"type": "boolean"},
                        "message_samples_count": {"type": "integer"},
                        "source": {
                            "type": "string",
                            "description": "inferred|explicit|manual",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Motivo da atualização (para histórico).",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_personality_deactivate",
                "description": (
                    "Desativa a personalização do chat para o usuário (volta ao estilo padrão). "
                    "Use quando pedir 'voltar ao padrão', 'desativar personalidade'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_chat_personality_summary",
                "description": (
                    "Estatísticas dos perfis de personalidade guardados (total, ativos, recentes)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- Visual memory (users & characters for thumbnails) ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_list",
                "description": (
                    "Lista perfis visuais (usuários, personagens, mascotes) com fotos de referência "
                    "para thumbnails e imagens. Use para 'quem tenho na memória visual', "
                    "'listar personagens', 'meus perfis de foto'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "user|character|mascot",
                        },
                        "user_id": {"type": "string"},
                        "query": {"type": "string", "description": "Busca por nome ou tag"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_get",
                "description": (
                    "Lê perfil visual completo (fotos, persona, traços). "
                    "Use por profile_id, name ou user_id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string", "description": "Nome ou slug do personagem"},
                        "user_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_save",
                "description": (
                    "Cria ou atualiza perfil visual (usuário/personagem/mascote) com "
                    "persona_prompt, visual_traits e style_notes para gerar thumbnails consistentes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "user_id": {"type": "string"},
                        "name": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "description": "user|character|mascot",
                        },
                        "display_name": {"type": "string"},
                        "persona_prompt": {
                            "type": "string",
                            "description": "Descrição visual/persona para Imagen",
                        },
                        "visual_traits": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Traços: cabelo, roupa, expressão, etc.",
                        },
                        "style_notes": {
                            "type": "string",
                            "description": "Notas de estilo para thumbnails",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "active": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_add_photo",
                "description": (
                    "Registra foto de referência no perfil visual (copia para .stack/visual-memory). "
                    "Use após qclaw_local_photos_search ou caminho absoluto fornecido pelo usuário."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string", "description": "Alternativa ao profile_id"},
                        "source_path": {
                            "type": "string",
                            "description": "Caminho absoluto da foto",
                        },
                        "caption": {"type": "string"},
                        "role": {
                            "type": "string",
                            "description": "headshot|full|action|reference|thumbnail",
                        },
                        "set_primary": {"type": "boolean"},
                        "analyze": {
                            "type": "boolean",
                            "description": "Analisar foto com Gemini e fundir traços no perfil (default true)",
                        },
                    },
                    "required": ["source_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_merge_photos",
                "description": (
                    "Re-analisa todas as fotos do perfil visual e funde características físicas "
                    "numa ficha consolidada. Use após adicionar várias fotos de referência."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_lora_train",
                "description": (
                    "Treina LoRA Flux no Replicate a partir das fotos do perfil (mín. 4). "
                    "Opcionalmente avalia qualidade (nota A-F). Requer REPLICATE_API_TOKEN."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                        "trigger_word": {
                            "type": "string",
                            "description": "Token único no prompt (ex. QCPERSONNAUBER)",
                        },
                        "training_steps": {"type": "integer", "description": "500-3000, default 1000"},
                        "wait": {"type": "boolean", "description": "Aguardar fim do treino"},
                        "evaluate": {"type": "boolean", "description": "Gerar amostras e pontuar"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_lora_status",
                "description": (
                    "Estado do LoRA do perfil: treino, trigger_word, nota de qualidade (A-F), "
                    "scores. Use para 'como está o meu LoRA'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_lora_evaluate",
                "description": (
                    "Gera imagens de teste com o LoRA treinado e calcula nota de fidelidade "
                    "(0-100, grade A-F) vs foto de referência."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_remove_photo",
                "description": "Remove foto de referência de um perfil visual.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "photo_id": {"type": "string"},
                    },
                    "required": ["profile_id", "photo_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_set_primary",
                "description": "Define foto principal do perfil para thumbnails.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "photo_id": {"type": "string"},
                    },
                    "required": ["profile_id", "photo_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_resolve",
                "description": (
                    "Resolve user_photo_path e character_persona_override a partir da memória visual. "
                    "Use antes de qclaw_thumbnail_generate."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                        "user_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_deactivate",
                "description": "Desativa perfil visual (não aparece em listagens/resolução).",
                "parameters": {
                    "type": "object",
                    "properties": {"profile_id": {"type": "string"}},
                    "required": ["profile_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_profile_from_photo",
                "description": (
                    "[Conhecimento] Analisa foto de retrato (Gemini), guarda TODAS as descrições "
                    "físicas no MongoDB (memória visual + dados pessoais + times) para reconstruir "
                    "a aparência em qualquer conversa ou time. Use quando pedir cadastrar minha "
                    "foto, aprender meu rosto, guardar aparência para o chat lembrar, ou sincronizar "
                    "descrição visual em todos os times."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "photo_path": {
                            "type": "string",
                            "description": "Caminho absoluto da foto no disco",
                        },
                        "name": {"type": "string", "description": "Nome do perfil (ex.: Nauber)"},
                        "display_name": {"type": "string"},
                        "user_id": {"type": "string", "description": "Default: dono do sistema"},
                        "owner": {
                            "type": "boolean",
                            "description": "Usar DEFAULT_OWNER_USER_ID (padrão true)",
                        },
                        "sync_conversations": {
                            "type": "boolean",
                            "description": "Sincronizar para conversas e times (padrão true)",
                        },
                        "data_base64": {
                            "type": "string",
                            "description": "Alternativa ao photo_path: imagem em base64",
                        },
                        "mime_type": {"type": "string", "description": "image/jpeg, image/png, …"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_visual_memory_summary",
                "description": "Estatísticas da memória visual (totais por tipo, recentes).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- Character preview & psychology ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_character_search",
                "description": (
                    "[Conteúdo] Busca personagens na memória visual com miniatura e traços resumidos. "
                    "Use para 'buscar personagem', 'listar heróis', 'quem tenho cadastrado'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Nome, tag ou arquétipo"},
                        "kind": {"type": "string", "description": "user|character|mascot"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_character_preview",
                "description": (
                    "[Conteúdo] Ficha completa do personagem: foto, traços visuais, ficha física "
                    "e perfil psicológico. Use para 'ver personagem X', 'características de [nome]'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string", "description": "Nome ou slug do personagem"},
                        "character_name": {"type": "string"},
                        "user_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_character_psych_save",
                "description": (
                    "[Conteúdo] Atribui ou atualiza perfil psicológico de um personagem "
                    "(arquétipo, traços, motivações, medos, estilo de fala, backstory). "
                    "Use após inferir personalidade a partir de roteiro ou descrição."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile_id": {"type": "string"},
                        "name": {"type": "string"},
                        "character_name": {"type": "string"},
                        "merge": {
                            "type": "boolean",
                            "description": "Mesclar com perfil psicológico existente (padrão true)",
                        },
                        "psych_profile": {
                            "type": "object",
                            "description": "Objeto com archetype, traits, motivations, fears, values, speech_style, backstory_summary, emotional_range, relationships, psych_prompt",
                        },
                        "archetype": {"type": "string"},
                        "mbti": {"type": "string"},
                        "traits": {"type": "array", "items": {"type": "string"}},
                        "motivations": {"type": "array", "items": {"type": "string"}},
                        "fears": {"type": "array", "items": {"type": "string"}},
                        "values": {"type": "array", "items": {"type": "string"}},
                        "speech_style": {"type": "string"},
                        "backstory_summary": {"type": "string"},
                        "emotional_range": {"type": "string"},
                        "psych_prompt": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        # --- Thumbnail style presets (reusable visual look) ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_list",
                "description": (
                    "Lista presets de estilo de thumbnail guardados para reuso. "
                    "Use para 'estilos de thumbnail salvos', 'presets de capa', 'meus looks de canal'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Busca por nome, tag ou estilo"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_get",
                "description": (
                    "Lê preset de estilo de thumbnail (styles, neuro_boost, layout, personagem). "
                    "Use por style_id ou name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "style_id": {"type": "string"},
                        "name": {"type": "string", "description": "Nome ou slug do preset"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_save",
                "description": (
                    "Cria ou atualiza preset de estilo de thumbnail para reuso em gerações futuras. "
                    "Guarde styles, text_overlay_hints, thumbnail_format, character_name e tags."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "style_id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "styles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Nomes de estilos visuais (IMAGE_STYLES ou custom)",
                        },
                        "image_style_name": {"type": "string"},
                        "neuro_boost": {"type": "boolean"},
                        "include_mascot": {"type": "boolean"},
                        "maintain_persona": {"type": "boolean"},
                        "auto_optimize_viral": {"type": "boolean"},
                        "text_overlay_hints": {
                            "type": "string",
                            "description": "Regras de texto na thumb (MAIÚSCULAS, 3 palavras, etc.)",
                        },
                        "thumbnail_format": {"type": "object"},
                        "reference_image_path": {"type": "string"},
                        "character_name": {"type": "string"},
                        "visual_memory_id": {"type": "string"},
                        "notes": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_capture",
                "description": (
                    "Guarda preset de estilo a partir de args de uma geração de thumbnail (JSON). "
                    "Use após uma thumb bem-sucedida para capturar o look."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "args": {
                            "type": "object",
                            "description": "Args de qclaw_thumbnail_generate a persistir",
                        },
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "args"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_resolve",
                "description": (
                    "Resolve preset de estilo para args de qclaw_thumbnail_generate. "
                    "Use antes de gerar para ver o que será aplicado."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "style_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_deactivate",
                "description": "Desativa preset de estilo de thumbnail (soft delete).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "style_id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_thumbnail_style_summary",
                "description": "Estatísticas dos presets de estilo de thumbnail guardados.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        # --- User personal data ---
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_get",
                "description": (
                    "Lê todos os dados pessoais persistidos do usuário (nome, contato, "
                    "endereço, profissão, relacionamentos). Use para 'meus dados pessoais', "
                    "'o que você sabe sobre mim', 'ver perfil pessoal salvo'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_save",
                "description": (
                    "Guarda ou atualiza dados pessoais do usuário (merge parcial). "
                    "Use quando o usuário informar nome, email, telefone, endereço, "
                    "empresa, bio ou outros dados pessoais para lembrar."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "full_name": {"type": "string"},
                        "display_name": {"type": "string"},
                        "nickname": {"type": "string"},
                        "pronouns": {"type": "string"},
                        "date_of_birth": {"type": "string"},
                        "nationality": {"type": "string"},
                        "email": {"type": "string"},
                        "phone": {"type": "string"},
                        "whatsapp": {"type": "string"},
                        "telegram": {"type": "string"},
                        "address": {"type": "string"},
                        "city": {"type": "string"},
                        "state": {"type": "string"},
                        "country": {"type": "string"},
                        "postal_code": {"type": "string"},
                        "timezone": {"type": "string"},
                        "job_title": {"type": "string"},
                        "company": {"type": "string"},
                        "department": {"type": "string"},
                        "linkedin": {"type": "string"},
                        "language": {"type": "string"},
                        "preferred_contact": {"type": "string"},
                        "communication_notes": {"type": "string"},
                        "emergency_contact_name": {"type": "string"},
                        "emergency_contact_phone": {"type": "string"},
                        "notes": {"type": "string"},
                        "bio": {"type": "string"},
                        "document_id": {
                            "type": "string",
                            "description": "CPF/RG legado — preferir cpf/rg",
                        },
                        "cpf": {"type": "string"},
                        "rg": {"type": "string"},
                        "titulo_eleitor": {"type": "string"},
                        "nis": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "relationships": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Familiares/contatos: {name, relation, phone}",
                        },
                        "custom_fields": {
                            "type": "object",
                            "description": "Campos extras arbitrários (sem segredos)",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_delete_field",
                "description": (
                    "Remove um campo dos dados pessoais do usuário. "
                    "Use quando pedir apagar/remover dado salvo (LGPD)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "field": {"type": "string", "description": "Nome do campo"},
                        "custom_key": {
                            "type": "string",
                            "description": "Chave em custom_fields (se field=custom_fields)",
                        },
                    },
                    "required": ["user_id", "field"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_search",
                "description": (
                    "Busca perfis de dados pessoais por nome, email ou telefone. "
                    "Use para 'procurar usuário Ana', 'quem tem email @empresa'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_export",
                "description": (
                    "Exporta todos os dados pessoais do usuário em JSON. "
                    "Use para 'exportar meus dados', portabilidade LGPD."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_preview",
                "description": (
                    "Mostra preview visual dos dados pessoais do usuário no chat, "
                    "com foto (memória visual ou photo_path) e resumo em markdown. "
                    "Use para 'mostrar meu perfil', 'preview dos meus dados', "
                    "'ver meus dados com foto'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "Opcional — default: dono (naubergois)",
                        },
                        "photo_path": {
                            "type": "string",
                            "description": "Caminho absoluto da foto (opcional; senão memória visual)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_user_data_summary",
                "description": "Estatísticas dos perfis de dados pessoais guardados.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_identidade_get",
                "description": (
                    "Lê dados de identidade civil do usuário: nome, CPF, RG, "
                    "título de eleitor, NIS e endereço. Use para 'meu CPF', "
                    "'meus documentos', 'meu endereço salvo', 'identidade civil'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "Opcional — default: dono (naubergois)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_identidade_save",
                "description": (
                    "Guarda ou atualiza identidade civil (merge parcial): CPF, RG, "
                    "título de eleitor, NIS, endereço, nome, data de nascimento. "
                    "Use quando o usuário informar documentos ou endereço para lembrar."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "full_name": {"type": "string"},
                        "display_name": {"type": "string"},
                        "nickname": {"type": "string"},
                        "date_of_birth": {"type": "string"},
                        "nationality": {"type": "string"},
                        "cpf": {"type": "string", "description": "CPF (validado)"},
                        "rg": {"type": "string"},
                        "titulo_eleitor": {"type": "string"},
                        "nis": {"type": "string", "description": "NIS / PIS / PASEP"},
                        "address": {"type": "string"},
                        "city": {"type": "string"},
                        "state": {"type": "string"},
                        "country": {"type": "string"},
                        "postal_code": {"type": "string", "description": "CEP"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_identidade_delete",
                "description": (
                    "Remove um campo de identidade civil (CPF, RG, endereço, etc.). "
                    "Use quando pedir apagar documento salvo (LGPD)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "field": {
                            "type": "string",
                            "description": "cpf, rg, titulo_eleitor, nis, address, …",
                        },
                    },
                    "required": ["field"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_curriculum_list",
                "description": (
                    "[Conhecimento] Lista documentos pessoais no cofre (PDF, DOCX, MD): "
                    "currículos, diplomas, certificados. Use para 'meus documentos', "
                    "'listar PDFs salvos', 'cofre de documentos'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "description": "cv, diploma, certificate, other",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_curriculum_register",
                "description": (
                    "[Conhecimento] Copia um arquivo para o cofre pessoal de documentos. "
                    "Use quando o usuário anexar ou indicar caminho de currículo/diploma."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_path": {
                            "type": "string",
                            "description": "Caminho absoluto do arquivo",
                        },
                        "path": {"type": "string"},
                        "file_path": {"type": "string"},
                        "user_id": {"type": "string"},
                        "label": {"type": "string", "description": "Nome amigável"},
                        "kind": {
                            "type": "string",
                            "description": "cv, diploma, certificate, other",
                        },
                        "copy": {
                            "type": "boolean",
                            "description": "Copiar para cofre (default true)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_curriculum_extract",
                "description": (
                    "[Conhecimento] Extrai texto de documento no cofre ou caminho local "
                    "(PDF, DOCX, MD, TXT). Use para 'extrair meu currículo', 'ler diploma PDF'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string"},
                        "id": {"type": "string"},
                        "name": {"type": "string", "description": "Nome ou label parcial"},
                        "file_path": {"type": "string"},
                        "path": {"type": "string"},
                        "user_id": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_curriculum_generate",
                "description": (
                    "[Conteúdo] Gera currículo em Markdown a partir do perfil pessoal "
                    "(MongoDB) e custom_fields. Salva em memory/user/documents/generated/. "
                    "Use para 'gerar meu currículo', 'montar CV', 'atualizar resume'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "save": {
                            "type": "boolean",
                            "description": "Salvar arquivo .md (default true)",
                        },
                        "filename": {"type": "string"},
                        "include_contact": {
                            "type": "boolean",
                            "description": "Incluir email/telefone (default true)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_curriculum_send",
                "description": (
                    "[Comunicação] Envia documento do cofre pessoal via WhatsApp "
                    "(allowlist). Use para 'mandar meu CV no zap para fulano', "
                    "'enviar diploma por whatsapp'. Confirme destinatário antes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string"},
                        "id": {"type": "string"},
                        "name": {"type": "string", "description": "Nome/label do documento"},
                        "file_path": {"type": "string"},
                        "path": {"type": "string"},
                        "to": {
                            "type": "string",
                            "description": "Telefone, JID ou nome na agenda",
                        },
                        "recipient": {"type": "string"},
                        "caption": {"type": "string"},
                        "message": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "description": "auto, document ou preview",
                        },
                        "pages": {"type": "string", "description": "Ex.: 1-5 (mode preview)"},
                        "max_pages": {"type": "integer"},
                    },
                    "required": ["to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_minicurriculum_generate",
                "description": (
                    "[Conteúdo] Gera minicurrículo profissional compacto (HTML + PNG) "
                    "a partir do perfil pessoal e mostra preview no chat. Use para "
                    "'gerar meu minicurrículo', 'cartão profissional', 'CV resumido'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "include_contact": {
                            "type": "boolean",
                            "description": "Incluir email/telefone (default true)",
                        },
                        "include_photo": {
                            "type": "boolean",
                            "description": "Incluir foto de perfil (default true)",
                        },
                        "photo_path": {"type": "string"},
                        "save": {
                            "type": "boolean",
                            "description": "Salvar HTML/PNG em previews (default true)",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_minicurriculum_preview",
                "description": (
                    "[Conteúdo] Preview visual do minicurrículo no chat (cartão + imagem). "
                    "Use para 'ver meu minicurrículo', 'mostrar preview do CV'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "include_contact": {"type": "boolean"},
                        "include_photo": {"type": "boolean"},
                        "photo_path": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_minicurriculum_send",
                "description": (
                    "[Comunicação] Gera minicurrículo e envia por WhatsApp (allowlist). "
                    "Use para 'mandar minicurrículo no zap', 'enviar CV curto para fulano'. "
                    "Confirme destinatário antes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "to": {
                            "type": "string",
                            "description": "Telefone, JID ou nome na agenda",
                        },
                        "recipient": {"type": "string"},
                        "caption": {"type": "string"},
                        "message": {"type": "string"},
                        "include_contact": {"type": "boolean"},
                        "include_photo": {"type": "boolean"},
                        "mode": {
                            "type": "string",
                            "description": "auto, document ou preview",
                        },
                    },
                    "required": ["to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_contacts_search",
                "description": (
                    "Busca RÁPIDA de contatos WhatsApp por nome ou telefone via wacli "
                    "(~1s, ao vivo). **Use primeiro** para 'procurar contato no zap', "
                    "'achar o Marcelo no whatsapp', 'telefone do fulano'. "
                    "NÃO chame agenda_sync antes — esta ferramenta não precisa de sincronização."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Nome, sobrenome ou telefone (ex.: 'marcelo', '5585').",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default: 50).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_agenda_sync",
                "description": (
                    "Sincroniza contatos e chats WhatsApp do wacli para a agenda local "
                    "(MongoDB wa_contacts). Use **somente** quando o usuário pedir "
                    "'sincronizar agenda', 'atualizar contatos do zap' ou 'importar agenda'. "
                    "NÃO use antes de procurar contato — prefira qclaw_whatsapp_contacts_search. "
                    "Por padrão é rápido (chats list). refresh=true atualiza wacli; "
                    "full=true varredura completa (lenta, minutos)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "refresh": {
                            "type": "boolean",
                            "description": "Rodar wacli contacts refresh antes da sync.",
                        },
                        "full": {
                            "type": "boolean",
                            "description": (
                                "Varredura alfabética completa de contatos (lenta; minutos)."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_agenda_list",
                "description": (
                    "Lista contatos/chats da agenda WhatsApp já sincronizada (MongoDB). "
                    "Com query, busca ao vivo no wacli (mesmo efeito que contacts_search). "
                    "Sem query, lista a agenda local. Use para 'listar todos contatos', "
                    "'quem está na agenda', tela /agenda. Para procurar por nome, "
                    "prefira qclaw_whatsapp_contacts_search."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Filtro por nome, telefone ou JID (opcional).",
                        },
                        "chat_type": {
                            "type": "string",
                            "description": "Filtrar: individual, dm, group, newsletter (opcional).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default: 50).",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_agenda_stats",
                "description": (
                    "Estatísticas da agenda WhatsApp: total de contatos, por tipo, "
                    "última sincronização. Use para 'quantos contatos no whatsapp'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_messages_search",
                "description": (
                    "Busca mensagens WhatsApp em qualquer chat/grupo via wacli (ao vivo). "
                    "Cobre DMs e grupos sem precisar indexar antes. "
                    "Use para 'buscar mensagem no whatsapp', 'procurar no zap sobre X', "
                    "'mensagens do grupo Y sobre Z', 'quem falou sobre deploy'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Texto a buscar nas mensagens.",
                        },
                        "chat": {
                            "type": "string",
                            "description": "JID ou nome parcial do chat/grupo (opcional).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Máximo de resultados (default: 30).",
                        },
                        "after": {
                            "type": "string",
                            "description": "Data inicial YYYY-MM-DD (opcional).",
                        },
                        "before": {
                            "type": "string",
                            "description": "Data final YYYY-MM-DD (opcional).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_whatsapp_media_download",
                "description": (
                    "Baixa anexo/documento/imagem de uma mensagem WhatsApp via wacli "
                    "(planilhas, PDFs, etc.). Requer msg_id da mensagem; chat (JID do grupo) "
                    "é recomendado — se omitido, tenta resolver pelo wacli.db local. "
                    "Use após qclaw_whatsapp_messages_search para obter msg_id e chat_jid. "
                    "Nunca use `wacli media download` no shell."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "msg_id": {
                            "type": "string",
                            "description": "ID da mensagem WhatsApp (campo msg_id da busca).",
                        },
                        "chat": {
                            "type": "string",
                            "description": "JID do chat/grupo (ex.: 120363...@g.us) ou nome parcial.",
                        },
                    },
                    "required": ["msg_id"],
                },
            },
        },
    ]
    # --- Team Context Search (interrogação) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_context_search",
                "description": (
                    "Busca informações no contexto do time: membros de grupos WhatsApp, "
                    "nomes de grupos, pessoas em um ou mais grupos, quem participa de qual "
                    "turma/projeto. Use quando o usuário perguntar 'quem é X?', "
                    "'em quais grupos está o fulano', 'listar membros do grupo Y', "
                    "'buscar pessoa', 'interrogar contexto do time', ou usar '?' seguido "
                    "de uma busca. Procura na memória de grupos WhatsApp carregada."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Termo de busca: nome de pessoa, nome de grupo, "
                                "ou pergunta sobre quem participa de quê."
                            ),
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["all", "people", "groups"],
                            "description": (
                                "Escopo da busca: all=tudo, people=apenas pessoas, "
                                "groups=apenas nomes de grupos. Padrão: all."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }
    )
    # --- Team Articles (LaTeX / ChromaDB) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_articles",
                "description": (
                    "Lista ou busca artigos científicos (LaTeX) indexados no contexto de um time. "
                    "Sem query retorna todos os artigos indexados; com query faz busca semântica. "
                    "Use quando o usuário perguntar 'quais artigos do time', 'artigos do squad', "
                    "'buscar artigo sobre X', 'papers do time', 'documentos acadêmicos', "
                    "'o que o time publicou', 'artigos LaTeX indexados'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {
                            "type": "string",
                            "description": "ID do time. Se vazio usa o time selecionado na conversa.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Texto de busca semântica. Se vazio lista todos os artigos.",
                        },
                        "n_results": {
                            "type": "integer",
                            "description": "Número máximo de resultados (padrão 10).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    # --- Article figures (LaTeX includegraphics) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_article_images",
                "description": (
                    "Lista, exibe e altera figuras de artigos LaTeX (\\includegraphics). "
                    "Use quando o usuário pedir figuras do artigo, imagens do paper, "
                    "preview de gráfico LaTeX, trocar figura, regenerar ilustração "
                    "científica ou recompilar após alterar figuras. "
                    "Skill: qclaw-chat-article-images."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "show", "replace", "generate", "suggest_prompt", "models", "compile"],
                            "description": (
                                "list=inventário; show=preview inline; replace=substituir ficheiro; "
                                "generate=IA (Grok/Nano), use_context=true gera prompt do contexto se vazio; "
                                "suggest_prompt=sugere prompt usando contexto do artigo (título, resumo, "
                                "legenda, texto ao redor); models=lista modelos de imagem disponíveis; "
                                "compile=PDF."
                            ),
                        },
                        "workspace_id": {
                            "type": "string",
                            "description": "ID do workspace LaTeX (list_article_workspaces).",
                        },
                        "article_id": {
                            "type": "string",
                            "description": "Caminho relativo do .tex (list_articles).",
                        },
                        "figure": {
                            "type": "integer",
                            "description": "Índice da figura (1-based) para show/replace/generate.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Caminho da imagem origem (action=replace).",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Prompt para geração IA (action=generate).",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["grok", "nano", "imagen"],
                            "description": "Provider de imagem (default grok).",
                        },
                        "model": {
                            "type": "string",
                            "description": "Modelo opcional (ex. grok-imagine-image-quality).",
                        },
                        "input_image_path": {
                            "type": "string",
                            "description": "Imagem base para edição (generate).",
                        },
                        "update_tex": {
                            "type": "boolean",
                            "description": "Atualizar \\includegraphics após generate.",
                        },
                        "use_context": {
                            "type": "boolean",
                            "description": (
                                "Em generate: se o prompt estiver vazio, cria o prompt a partir do "
                                "contexto do artigo (legenda + texto ao redor) antes de gerar."
                            ),
                        },
                        "style": {
                            "type": "string",
                            "description": "Estilo visual preferido para suggest_prompt/generate (opcional).",
                        },
                        "model_id": {
                            "type": "string",
                            "description": "Modelo LLM para redigir o prompt em suggest_prompt (opcional).",
                        },
                        "max_preview": {
                            "type": "integer",
                            "description": "Máximo de previews inline (show, default 6).",
                        },
                    },
                    "required": ["action", "workspace_id", "article_id"],
                },
            },
        }
    )
    # --- Team article PDF (compile + save to team folder) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_team_article_pdf",
                "description": (
                    "Compila um artigo LaTeX em PDF e salva o ficheiro na pasta do time "
                    "(local_path ou workspace/artifacts). Use quando pedir 'gerar pdf do artigo', "
                    "'compilar paper e salvar no time', 'exportar pdf para pasta do squad', "
                    "'pdf na pasta do projeto', ou entregar PDF final do artigo ao time. "
                    "Skill: qclaw-chat-team-article-pdf."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "description": "ID do workspace LaTeX (list_article_workspaces).",
                        },
                        "article_id": {
                            "type": "string",
                            "description": "Caminho relativo do .tex (list_articles).",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID do time. Se vazio usa o time selecionado no chat.",
                        },
                        "team_name": {
                            "type": "string",
                            "description": "Nome parcial do time (alternativa a team_id).",
                        },
                        "subdir": {
                            "type": "string",
                            "description": "Subpasta no time (default: artifacts).",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Nome do PDF de saída (default: stem do .tex).",
                        },
                        "dest": {
                            "type": "string",
                            "enum": ["auto", "workspace", "local_path"],
                            "description": (
                                "Destino: auto (local_path se existir), workspace ou local_path."
                            ),
                        },
                        "skip_compile": {
                            "type": "boolean",
                            "description": "Se true, copia PDF já compilado sem recompilar.",
                        },
                    },
                    "required": ["workspace_id", "article_id"],
                },
            },
        }
    )
    # --- Index team articles (LaTeX → ChromaDB) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_index_team_articles",
                "description": (
                    "Indexa (vincula) artigos LaTeX de um workspace ao contexto ChromaDB de um time. "
                    "Varre todos os arquivos .tex do workspace e salva o conteúdo no time. "
                    "Use quando o usuário pedir 'vincular artigos ao time', 'indexar artigos LaTeX', "
                    "'associar workspace de papers ao squad', 'sincronizar artigos do time', "
                    "'salvar papers no contexto do time'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "description": "ID do workspace LaTeX (obtido em list_workspaces).",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID do time destino. Se vazio usa o time selecionado na conversa.",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
        }
    )
    # --- Remove team articles (ChromaDB) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_remove_team_articles",
                "description": (
                    "Remove (desvincula) do contexto ChromaDB de um time todos os artigos LaTeX "
                    "de um workspace específico. "
                    "Use quando o usuário pedir 'desvincular artigos do time', 'remover artigos do squad', "
                    "'excluir papers do contexto do time', 'desindexar workspace do time'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "description": "ID do workspace LaTeX a desvincular.",
                        },
                        "team_id": {
                            "type": "string",
                            "description": "ID do time. Se vazio usa o time selecionado na conversa.",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
        }
    )
    # --- Gmail IMAP tools (send / read / list) ---
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_gmail_send",
                "description": (
                    "Envia um email via Gmail SMTP com senha de app. "
                    "Suporta anexos (arquivos locais). "
                    "Use quando o usuário pedir 'enviar email', 'mandar email', "
                    "'escrever email para', 'send email to'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Endereço de email do destinatário.",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Assunto do email.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Corpo do email (texto plano).",
                        },
                        "attachments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Lista de caminhos absolutos de arquivos para anexar ao email. "
                                "Pode incluir arquivos do chat (salvos em attachments_temp_dir) "
                                "ou qualquer caminho local acessível."
                            ),
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_gmail_list",
                "description": (
                    "Lista emails da inbox do Gmail via IMAP. "
                    "Use quando o usuário pedir 'ler emails', 'ver inbox', "
                    "'listar emails', 'emails não lidos', 'checar caixa de entrada', "
                    "'mostrar emails', 'read emails', 'check inbox'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Número máximo de emails a retornar (padrão: 10).",
                        },
                        "unread": {
                            "type": "boolean",
                            "description": "Se true, lista apenas emails não lidos.",
                        },
                        "from_filter": {
                            "type": "string",
                            "description": "Filtrar por remetente (opcional).",
                        },
                        "subject_filter": {
                            "type": "string",
                            "description": "Filtrar por assunto (opcional).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_gmail_read",
                "description": (
                    "Lê o conteúdo completo de um email por UID. "
                    "Use quando o usuário pedir 'ler email X', 'abrir email', "
                    "'ver conteúdo do email', 'mostrar email completo'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uid": {
                            "type": "string",
                            "description": "UID do email (obtido via qclaw_gmail_list).",
                        },
                    },
                    "required": ["uid"],
                },
            },
        }
    )
    # ─── Google Calendar CRUD tools ───────────────────────────────────
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_sync",
                "description": (
                    "Sincroniza Google Calendar com banco local SQLite (bidirecional). "
                    "Use quando o usuário pedir 'sincronizar calendar', 'sync calendar', "
                    "'atualizar agenda', 'puxar eventos do google'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Número de dias a sincronizar (padrão: 30).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_today",
                "description": (
                    "Retorna eventos de hoje do banco local (offline, sem API). "
                    "Use quando o usuário pedir 'agenda hoje', 'eventos de hoje', "
                    "'o que tenho hoje', 'compromissos de hoje'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_week",
                "description": (
                    "Retorna eventos dos próximos 7 dias do banco local (offline). "
                    "Use quando o usuário pedir 'agenda semana', 'eventos da semana', "
                    "'próximos compromissos', 'agenda próximos dias'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_search",
                "description": (
                    "Busca eventos por título ou descrição no banco local. "
                    "Use quando o usuário pedir 'buscar evento X', 'quando é a reunião X', "
                    "'encontrar compromisso', 'procurar no calendário'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "Termo de busca (título ou descrição).",
                        },
                    },
                    "required": ["term"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_create",
                "description": (
                    "Cria um evento no Google Calendar e salva no banco local. "
                    "Use quando o usuário pedir 'criar evento', 'agendar reunião', "
                    "'marcar compromisso', 'adicionar ao calendário'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Título do evento.",
                        },
                        "start": {
                            "type": "string",
                            "description": "Início em ISO 8601 com timezone (ex: 2026-06-10T14:00:00-03:00).",
                        },
                        "end": {
                            "type": "string",
                            "description": "Fim em ISO 8601 com timezone (ex: 2026-06-10T15:00:00-03:00).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Descrição do evento (opcional).",
                        },
                        "location": {
                            "type": "string",
                            "description": "Local do evento (opcional).",
                        },
                    },
                    "required": ["summary", "start", "end"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_meet_create",
                "description": (
                    "Agenda reunião no Google Calendar com link Google Meet automático. "
                    "Use quando o usuário pedir 'agendar reunião com meet', 'marcar call', "
                    "'criar meeting com link', 'google meet', 'convidar para videoconferência'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Título da reunião.",
                        },
                        "start": {
                            "type": "string",
                            "description": "Início em ISO 8601 com timezone (ex: 2026-06-17T14:00:00-03:00).",
                        },
                        "end": {
                            "type": "string",
                            "description": "Fim em ISO 8601 (opcional; usa duration_minutes se omitido).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Descrição ou pauta (opcional).",
                        },
                        "attendees": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Emails dos participantes (opcional).",
                        },
                        "send_invites": {
                            "type": "boolean",
                            "description": "Enviar convites por email (padrão true com attendees).",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Duração em minutos se end omitido (padrão 60).",
                        },
                    },
                    "required": ["summary", "start"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_roteiro_scene_setup",
                "description": (
                    "Monta cenas de produção (locução, B-roll, enquadramento, transição) "
                    "a partir de roteiro de vídeo ou atos do Script Lab. "
                    "Use quando pedir montar cena, plano de cenas, breakdown de produção "
                    "ou dividir atos em cenas."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job de roteiro viral com atos.",
                        },
                        "act": {
                            "type": "integer",
                            "description": "Número do ato (omitir para todos).",
                        },
                        "text": {
                            "type": "string",
                            "description": "Roteiro em texto bruto.",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Caminho do arquivo .txt/.md.",
                        },
                        "scenes": {
                            "type": "integer",
                            "description": "Número alvo de cenas (padrão 12).",
                        },
                        "video_format": {
                            "type": "string",
                            "description": "youtube | aula | shorts | ads",
                        },
                        "visual_style": {
                            "type": "string",
                            "description": "Estilo visual de referência.",
                        },
                        "wpm": {
                            "type": "number",
                            "description": "Palavras/min para estimar duração.",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Pasta para scenes.json/md/csv.",
                        },
                        "no_llm": {
                            "type": "boolean",
                            "description": "Só divisão heurística.",
                        },
                        "model_id": {
                            "type": "string",
                            "description": "Override de modelo (padrão: modelo selecionado no chat).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_flights_search",
                "description": (
                    "Prepara busca de voos no Google Flights (origem, destino, datas). "
                    "Retorna URL para abrir no browser e extrair preços/horários. "
                    "Use quando o usuário pedir voos, passagens, tarifas, ida e volta "
                    "ou Google Flights."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {
                            "type": "string",
                            "description": "Origem (cidade ou IATA, ex: GRU).",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Destino (cidade ou IATA).",
                        },
                        "departure": {
                            "type": "string",
                            "description": "Data ida YYYY-MM-DD.",
                        },
                        "return_date": {
                            "type": "string",
                            "description": "Data volta YYYY-MM-DD (omitir para só ida).",
                        },
                        "one_way": {
                            "type": "boolean",
                            "description": "Somente ida.",
                        },
                        "travelers": {
                            "type": "integer",
                            "description": "Passageiros (padrão 1).",
                        },
                        "cabin": {
                            "type": "string",
                            "enum": ["economy", "business", "first"],
                            "description": "Classe (padrão economy).",
                        },
                        "query": {
                            "type": "string",
                            "description": "Consulta em linguagem natural (alternativa aos campos).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_youtube_metadata_generate",
                "description": (
                    "Gera metadados YouTube a partir de roteiro ou job RV: títulos virais, "
                    "descrição SEO, tags, hashtags e ideias de texto para thumbnail. "
                    "Use quando o usuário pedir metadados YouTube, título do vídeo, "
                    "descrição, tags, SEO YouTube ou preparar publicação."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Roteiro ou transcrição do vídeo.",
                        },
                        "script": {
                            "type": "string",
                            "description": "Alias de content.",
                        },
                        "topic": {
                            "type": "string",
                            "description": "Tema ou nicho do vídeo.",
                        },
                        "job_id": {
                            "type": "string",
                            "description": "Job do Roteiro Viral (usa roteiro combinado).",
                        },
                        "parent_job_id": {
                            "type": "string",
                            "description": "Alias de job_id.",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_flights_search",
                "description": (
                    "Busca voos em múltiplas APIs (Amadeus, Kiwi, Skyscanner, Duffel, "
                    "Aviationstack) e inclui link Google Flights. Use para passagens, "
                    "tarifas e comparação de voos. Requer chaves em /chaves."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string", "description": "Origem (IATA ou GRU)."},
                        "destination": {"type": "string", "description": "Destino (IATA)."},
                        "departure": {"type": "string", "description": "Ida YYYY-MM-DD."},
                        "return_date": {"type": "string", "description": "Volta YYYY-MM-DD."},
                        "one_way": {"type": "boolean"},
                        "travelers": {"type": "integer"},
                        "cabin": {"type": "string", "enum": ["economy", "business", "first"]},
                        "limit": {"type": "integer", "description": "Máx. ofertas (default 5)."},
                        "providers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "amadeus, kiwi, skyscanner, duffel, aviationstack",
                        },
                    },
                    "required": ["origin", "destination", "departure"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_flights_providers_status",
                "description": (
                    "Verifica quais APIs de voo estão configuradas (chaves presentes). "
                    "Use antes de buscar voos se não souber quais provedores estão ativos."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_delete",
                "description": (
                    "Remove um evento do Google Calendar e do banco local. "
                    "Use quando o usuário pedir 'remover evento', 'cancelar reunião', "
                    "'deletar compromisso', 'apagar do calendário'. "
                    "SEMPRE confirme com o usuário antes de executar."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "ID do evento Google Calendar.",
                        },
                    },
                    "required": ["event_id"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_update",
                "description": (
                    "Atualiza um evento existente no Google Calendar e banco local. "
                    "Use quando o usuário pedir 'mover evento', 'alterar horário', "
                    "'renomear evento', 'editar compromisso', 'reagendar'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "ID do evento Google Calendar.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Novo título (opcional).",
                        },
                        "start": {
                            "type": "string",
                            "description": "Novo início ISO 8601 (opcional).",
                        },
                        "end": {
                            "type": "string",
                            "description": "Novo fim ISO 8601 (opcional).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Nova descrição (opcional).",
                        },
                        "location": {
                            "type": "string",
                            "description": "Novo local (opcional).",
                        },
                    },
                    "required": ["event_id"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_status",
                "description": (
                    "Status do banco local de calendário: total de eventos, última sync, "
                    "pendentes. Use quando o usuário pedir 'status calendar', "
                    "'quando foi sincronizado', 'estado do calendário'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_notion_sync",
                "description": (
                    "Sincronização completa Google Calendar ↔ Notion: atualiza Google, "
                    "envia eventos para database Notion e importa eventos do Notion. "
                    "Use para 'sync notion', 'sincronizar calendário com notion'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Dias à frente (padrão: 30).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_notion_status",
                "description": (
                    "Status da integração Notion Calendar: token, database ID, "
                    "mapeamentos Google↔Notion."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_calendar_notion_configure",
                "description": (
                    "Configura o database Notion para sync de calendário. "
                    "Use quando o usuário informar o ID do database Notion."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "database_id": {
                            "type": "string",
                            "description": "ID do database Notion (32 caracteres).",
                        },
                    },
                    "required": ["database_id"],
                },
            },
        }
    )
    # ─── Microsoft Teams / Outlook Calendar tools ─────────────────────
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_teams_calendar_sync",
                "description": (
                    "Sincroniza calendário Microsoft Teams/Outlook (Graph API) para SQLite local. "
                    "Use quando o usuário pedir 'sincronizar teams', 'sync outlook', "
                    "'atualizar agenda microsoft', 'puxar reuniões do teams'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "Número de dias a sincronizar (padrão: 30).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_teams_calendar_today",
                "description": (
                    "Retorna eventos de hoje do calendário Teams/Outlook (banco local). "
                    "Use quando o usuário pedir 'agenda teams hoje', 'reuniões outlook hoje'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_teams_calendar_week",
                "description": (
                    "Retorna eventos dos próximos 7 dias do calendário Teams/Outlook (banco local). "
                    "Use quando o usuário pedir 'agenda teams semana', 'reuniões da semana outlook'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_teams_calendar_search",
                "description": (
                    "Busca eventos Teams/Outlook por título ou descrição no banco local. "
                    "Use quando o usuário pedir 'buscar reunião no teams', 'quando é X no outlook'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "Termo de busca (título ou descrição).",
                        },
                    },
                    "required": ["term"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_teams_calendar_status",
                "description": (
                    "Status do calendário Teams/Outlook: token, total de eventos, última sync. "
                    "Use quando o usuário pedir 'status teams calendar', 'estado outlook'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    tools += [
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_status",
                "description": (
                    "Status da integração Notion: token, bot, databases padrão. "
                    "Use para 'status notion', 'notion conectado?'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_configure",
                "description": (
                    "Configura API key Notion e IDs de databases padrão (tasks, calendário). "
                    "Use quando o usuário informar token ou database ID."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string", "description": "Token da integração Notion (ntn_...)."},
                        "tasks_database_id": {"type": "string", "description": "Database de tarefas padrão."},
                        "calendar_database_id": {"type": "string", "description": "Database de calendário padrão."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_search",
                "description": (
                    "Busca full-text no workspace Notion (páginas e databases). "
                    "Use para 'buscar no notion', 'achar página sobre X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Texto de busca."},
                        "filter_type": {
                            "type": "string",
                            "enum": ["", "page", "database"],
                            "description": "Filtrar por tipo (opcional).",
                        },
                        "limit": {"type": "integer", "description": "Máximo de resultados (padrão 20)."},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_query",
                "description": (
                    "Consulta linhas de um database Notion. "
                    "Use para 'listar tarefas', 'tarefas em progresso no notion'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "ID do database (usa padrão se omitido)."},
                        "filter": {"type": "string", "description": "Filtro JSON da API Notion (opcional)."},
                        "sorts": {"type": "string", "description": "Sorts JSON da API Notion (opcional)."},
                        "limit": {"type": "integer", "description": "Máximo de linhas (padrão 50)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_get_page",
                "description": "Lê propriedades de uma página Notion por ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "ID da página Notion."},
                    },
                    "required": ["page_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_get_database",
                "description": "Lê schema de um database Notion (nomes e tipos de colunas).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "ID do database Notion."},
                    },
                    "required": ["database_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_create_row",
                "description": (
                    "Cria linha/tarefa em database Notion. "
                    "Propriedades: dict {nome_coluna: valor}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "ID do database (usa padrão se omitido)."},
                        "properties": {
                            "type": "object",
                            "description": "Propriedades ex: {Name: 'Tarefa', Status: 'To Do', Due: '2026-06-15'}.",
                        },
                    },
                    "required": ["properties"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_create_page",
                "description": "Cria página Notion sob outra página ou em database.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "parent_page_id": {"type": "string", "description": "ID da página pai."},
                        "database_id": {"type": "string", "description": "ID do database (alternativa ao parent)."},
                        "title": {"type": "string", "description": "Título da página."},
                        "content": {"type": "string", "description": "Parágrafo inicial (só com parent_page_id)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_notion_update_page",
                "description": "Atualiza propriedades de uma página Notion existente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "ID da página."},
                        "properties": {
                            "type": "object",
                            "description": "Propriedades a atualizar {nome: valor}.",
                        },
                    },
                    "required": ["page_id", "properties"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_list",
                "description": (
                    "Lista/busca templates LaTeX salvos (bundled + extraídos do Overleaf). "
                    "Skill: qclaw-chat-latex-templates-search. "
                    "Use para 'procurar template salva', 'listar templates', 'template serpro/beamer'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "description": "Filtrar por tag (ex: article, beamer)."},
                        "query": {"type": "string", "description": "Busca por nome ou descrição."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_get",
                "description": (
                    "Detalhes de um template LaTeX — preâmbulo, pacotes, documentclass, tags. "
                    "Skill: qclaw-chat-latex-templates-search. Use antes de aplicar um template."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "template_id": {
                            "type": "string",
                            "description": "ID do template (ex: article-a4-12pt, ieee-conference).",
                        },
                    },
                    "required": ["template_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_extract",
                "description": (
                    "Extrai template LaTeX (preâmbulo, regras, assets, prévia) de pasta Overleaf "
                    "clonada ou arquivo .zip e registra na base de templates."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder": {
                            "type": "string",
                            "description": "Pasta local do projeto fonte (alternativa ao zip_path).",
                        },
                        "zip_path": {
                            "type": "string",
                            "description": "Caminho absoluto para .zip do projeto Overleaf/LaTeX.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Nome/slug do template (ex: ieee-conference). Opcional com ZIP.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_apply",
                "description": (
                    "Aplica template LaTeX a artigo destino, preservando conteúdo (seções). "
                    "Sem template_id abre catálogo inline no chat (prévia de cada template); "
                    "após escolher, mostra antes/depois e pede confirmação. "
                    "Use confirm=true só após aceite explícito."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "template_id": {
                            "type": "string",
                            "description": "ID do template no catálogo (preferido).",
                        },
                        "manifest": {
                            "type": "string",
                            "description": "Caminho alternativo ao .manifest.json.",
                        },
                        "target": {
                            "type": "string",
                            "description": "Pasta do artigo destino (alternativa a workspace_id).",
                        },
                        "workspace_id": {
                            "type": "string",
                            "description": "ID da pasta LaTeX cadastrada (preferido no chat).",
                        },
                        "article_id": {
                            "type": "string",
                            "description": "ID do artigo .tex (ex.: main.tex).",
                        },
                        "target_tex": {
                            "type": "string",
                            "description": "Caminho relativo ao .tex dentro de target.",
                        },
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Se true, aplica de imediato sem prévia (ou após aceite explícito)."
                            ),
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Alias legado de prévia (ignorado — prévia é o padrão).",
                        },
                        "no_backup": {
                            "type": "boolean",
                            "description": "Se true, não cria main.tex.bak antes de sobrescrever.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_sync",
                "description": (
                    "Sincroniza catalog.json com manifests *.manifest.json em bundled/ e user/. "
                    "Skill: qclaw-chat-latex-templates-search."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_preview",
                "description": (
                    "Gera ou recupera prévia PNG de um template (documentclass, capa, assets). "
                    "Skill: qclaw-chat-overleaf-template."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "template_id": {
                            "type": "string",
                            "description": "ID do template no catálogo.",
                        },
                    },
                    "required": ["template_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_discover",
                "description": (
                    "Pipeline MCP — busca templates LaTeX em catálogo local, fontes oficiais "
                    "(Elsevier/Springer/IEEE/ACM/MDPI/PLOS), MongoDB e opcionalmente GitHub. "
                    "Skill: qclaw-chat-latex-template-pipeline. "
                    "Use para 'template oficial da revista X', 'Scientific Reports', 'IEEE conference'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Termo livre (revista, classe, editora)."},
                        "journal": {"type": "string", "description": "Nome do periódico."},
                        "publisher": {"type": "string", "description": "Editora (Elsevier, IEEE, …)."},
                        "tag": {"type": "string", "description": "Filtrar catálogo local por tag."},
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "local, official, catalog, github.",
                        },
                        "limit": {"type": "integer", "description": "Máximo de resultados."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_fetch",
                "description": (
                    "Baixa pacote .zip de template LaTeX (CTAN/editora), extrai manifest e registra "
                    "no catálogo. Skill: qclaw-chat-latex-template-pipeline."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL do .zip do template."},
                        "name": {"type": "string", "description": "Slug/nome do template (opcional)."},
                        "no_register": {
                            "type": "boolean",
                            "description": "Só baixar ZIP, sem extrair/registrar.",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_catalog_search",
                "description": (
                    "Busca metadados estruturados de templates no MongoDB (publisher, journal, engine). "
                    "Skill: qclaw-chat-latex-template-pipeline."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "publisher": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_catalog_get",
                "description": (
                    "Metadados YAML-like de um template pelo id (documentclass, bibliografia, engine). "
                    "Skill: qclaw-chat-latex-template-pipeline."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "ID (ex: springer-sn-article)."},
                        "template_id": {
                            "type": "string",
                            "description": "Alias de id.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_catalog_sync",
                "description": (
                    "Popula MongoDB com registro curado de fontes oficiais (Elsevier, Springer, IEEE…). "
                    "Skill: qclaw-chat-latex-template-pipeline."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "qclaw_overleaf_template_pipeline",
                "description": (
                    "Orquestra discover → fetch → catalog → apply para templates científicos. "
                    "Skill: qclaw-chat-latex-template-pipeline. "
                    "Ex.: journal=Scientific Reports, target=pasta do artigo, dry_run=true."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "journal": {"type": "string"},
                        "publisher": {"type": "string"},
                        "template_id": {
                            "type": "string",
                            "description": "Pular discover se já conhecido.",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "Escolher resultado específico do discover.",
                        },
                        "target": {
                            "type": "string",
                            "description": "Pasta do artigo para aplicar template.",
                        },
                        "workspace_id": {
                            "type": "string",
                            "description": "Pasta LaTeX cadastrada (alternativa a target).",
                        },
                        "article_id": {
                            "type": "string",
                            "description": "Arquivo .tex dentro do workspace.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Simular apply sem escrever.",
                        },
                        "no_fetch": {
                            "type": "boolean",
                            "description": "Não baixar ZIP externo.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [],
                },
            },
        },
    ]
    # ─── Google Photos (Picker API — mar/2025+) ───────────────────────
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_picker_start",
                "description": (
                    "Inicia Google Photos Picker — devolve picker_uri_web. "
                    "O USUÁRIO deve abrir o link e TOCAR nas fotos desejadas antes de Concluído. "
                    "O agente NÃO seleciona sozinho e NÃO vê a biblioteca inteira. "
                    "NUNCA instrua 'Concluído sem selecionar'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_items": {
                            "type": "integer",
                            "description": "Máximo de fotos que o usuário pode escolher (padrão: 50).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_picker_poll",
                "description": (
                    "Aguarda o usuário concluir a seleção no Google Fotos (picker). "
                    "Chame depois que o usuário abrir o link. Retorna ready=true quando pronto para listar."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "ID da sessão (picker_start)."},
                        "timeout": {
                            "type": "integer",
                            "description": "Segundos aguardando (padrão: 120).",
                        },
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_picker_list",
                "description": (
                    "Lista fotos/vídeos que o usuário escolheu no Picker. "
                    "Use após picker_poll com ready=true."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "ID da sessão picker."},
                        "limit": {"type": "integer", "description": "Máximo de itens."},
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_recent",
                "description": (
                    "Lista fotos já escolhidas em sessões anteriores do Picker (cache local). "
                    "Se vazio, use picker_start primeiro."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Quantidade (padrão: 20)."},
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_get",
                "description": (
                    "Obtém uma foto escolhida no Picker por media_id; devolve image_data_url para preview. "
                    "Requer session_id da sessão picker (ou usa a última)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "media_id": {"type": "string", "description": "ID do mediaItem."},
                        "session_id": {"type": "string", "description": "ID da sessão picker."},
                        "with_image": {
                            "type": "boolean",
                            "description": "Preview inline (padrão: true).",
                        },
                        "max_width": {"type": "integer", "description": "Largura máx. (padrão: 1200)."},
                    },
                    "required": ["media_id"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_google_photos_status",
                "description": (
                    "Status da integração Google Fotos: auth, total no banco, última sync. "
                    "Use para 'status google fotos', 'está conectado ao google fotos?'."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    # ─── Local photos (Mac folders) ───────────────────────────────────
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_local_photos_recent",
                "description": (
                    "Lista fotos/vídeos mais recentes em pastas locais (Pictures, Downloads, Desktop). "
                    "Busca automática SEM Google Picker. Use para 'últimas fotos do Mac', "
                    "'fotos locais recentes', 'achar foto no computador'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "Pasta (ex.: ~/Pictures). Vazio = padrão."},
                        "limit": {"type": "integer", "description": "Máximo (padrão: 20)."},
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_local_photos_search",
                "description": (
                    "Busca fotos/vídeos locais por nome, data ou pasta. Automático, sem Picker. "
                    "Use para 'foto casal Pictures', 'fotos de junho na pasta X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "Pasta base."},
                        "query": {"type": "string", "description": "Trecho do nome do arquivo."},
                        "after": {"type": "string", "description": "Data mínima YYYY-MM-DD (mtime)."},
                        "before": {"type": "string", "description": "Data máxima YYYY-MM-DD."},
                        "media_type": {"type": "string", "description": "PHOTO ou VIDEO."},
                        "limit": {"type": "integer", "description": "Máximo de resultados."},
                    },
                    "required": [],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_local_photos_get",
                "description": (
                    "Carrega foto local por caminho absoluto; devolve image_data_url para preview no chat."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Caminho do arquivo (campo path da busca)."},
                        "with_image": {"type": "boolean", "description": "Preview inline (padrão: true)."},
                        "max_width": {"type": "integer", "description": "Largura máx. thumbnail."},
                    },
                    "required": ["path"],
                },
            },
        }
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "qclaw_local_photos_roots",
                "description": "Lista pastas onde fotos locais são buscadas e config extra_roots.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )
    from .skills_mcp import chat_tool_specs as skills_chat_tool_specs

    tools.extend(skills_chat_tool_specs())
    from .cards_mcp import chat_tool_specs as cards_chat_tool_specs

    tools.extend(cards_chat_tool_specs())
    tools.extend(_jobs_manage_tool_specs())
    tools.extend(_monitor_update_tool_specs())
    tools.extend(_kanban_ide_handoff_tool_specs())
    tools.extend(_app_passwords_tool_specs())
    tools.extend(_google_oauth_tool_specs())
    tools.extend(_aws_manage_tool_specs())
    tools.extend(_team_files_search_tool_specs())
    tools.extend(_team_files_download_tool_specs())
    tools.extend(_team_files_send_tool_specs())
    tools.extend(_team_rules_tool_specs())
    tools.extend(_team_facts_tool_specs())
    tools.extend(_email_team_pdf_tool_specs())
    tools.extend(_gmail_attachments_tool_specs())
    tools.extend(_trello_tool_specs())
    tools.extend(_legal_evaluation_tool_specs())
    tools.extend(_budget_tool_specs())
    tools.extend(_team_payments_tool_specs())
    tools.extend(_journal_rules_tool_specs())
    from .mcp_chat_aliases import extra_chat_tool_specs

    existing = {
        str(t.get("function", {}).get("name") or "")
        for t in tools
        if isinstance(t, dict) and t.get("type") == "function"
    }
    # Suppress MCP aliases for feature-gated tools that are disabled, so a
    # turned-off feature is not re-exposed through the alias path.
    suppressed: set[str] = set()
    if not virtual_band_enabled:
        from .virtual_band import chat_tool_specs as _vb_specs

        suppressed |= {t["function"]["name"] for t in _vb_specs()}
    if not roteiro_thumbnails_enabled:
        from .roteiro_thumbnails import chat_tool_specs as _thumb_specs

        suppressed |= {t["function"]["name"] for t in _thumb_specs()}
    if not roteiro_mongo_enabled:
        from .roteiro_mongo import chat_tool_specs as _rv_mongo_specs
        from .roteiro_scripts_mongo import chat_tool_specs as _rv_scripts_specs
        from .roteiro_scripts_write import chat_tool_specs as _rv_scripts_write_specs

        suppressed |= {t["function"]["name"] for t in _rv_mongo_specs()}
        suppressed |= {t["function"]["name"] for t in _rv_scripts_specs()}
        suppressed |= {t["function"]["name"] for t in _rv_scripts_write_specs()}
    if not roteiro_api_enabled:
        from .roteiro_api_ops import chat_tool_specs as _rv_api_specs
        from .roteiro_book_cover import chat_tool_specs as _book_cover_specs

        suppressed |= {t["function"]["name"] for t in _rv_api_specs()}
        suppressed |= {t["function"]["name"] for t in _book_cover_specs()}
    tools.extend(extra_chat_tool_specs(existing | suppressed))
    if external_mcp_enabled:
        from .mcp_external_chat import external_mcp_chat_tool_specs

        tools.extend(external_mcp_chat_tool_specs())
    _tool_catalog_cache = tools
    _tool_catalog_cache_key = _cache_key
    return list(tools)

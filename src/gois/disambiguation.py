"""Module for identifying vague user requests and generating interactive disambiguation questions."""

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Any, Optional

from .latex_articles import (
    article_picker_label,
    list_workspaces_for_team,
    list_articles,
    compile_article,
)

log = logging.getLogger(__name__)

# Pattern matchers — structured wizard steps (button follow-ups)
_LATEX_LIST_RE = re.compile(
    r"^listar artigos da pasta\s+(\S+)", re.IGNORECASE
)
_LATEX_EXEC_RE = re.compile(
    r"^compilar artigo\s+(\S+)\s+na pasta\s+(\S+)", re.IGNORECASE
)

_HELP_RE = re.compile(
    r"^(ajuda|help|menu|opcoes|opções|bom dia|olá|ola|\?)$", re.IGNORECASE
)

# Exact chip / menu commands only — avoid intercepting contextual messages
# (e.g. "o swarm cadastrado") that the LLM + tools should handle.
_VAGUE_LATEX_COMMANDS = frozenset({
    "compilar", "compile", "build", "gerar pdf", "gerar artigo",
})
_VAGUE_SWARM_COMMANDS = frozenset({
    "iniciar swarm",
    "rodar swarm",
    "swarm",
    "iniciar agente",
    "rodar agente",
    "iniciar swarm bug fix",
    "iniciar swarm feature",
    "iniciar swarm refactor",
    "listar robos",
    "listar robôs",
})
_VAGUE_KANBAN_COMMANDS = frozenset({
    "status",
    "status kanban",
    "listar fila",
    "como enfileirar tarefa",
})
_ACADEMIC_TOOLS_RE = re.compile(
    r"^(?:abrir|mostrar|exibir|ver|usar|ativar)\s+(?:as?\s+)?ferramentas?\s+acad[eê]micas?\s*$|"
    r"^ferramentas?\s+acad[eê]micas?\s*$|"
    r"^(?:abrir|mostrar)\s+(?:o\s+)?(?:painel\s+)?(?:latex|artigos?)\s*$",
    re.IGNORECASE,
)
_LATEX_EDITOR_EXEC_RE = re.compile(
    r"^editar (?:artigo|latex) (\S+) na pasta (\S+)", re.IGNORECASE
)
_LATEX_IMAGES_EXEC_RE = re.compile(
    r"^abrir imagens do artigo (\S+) na pasta (\S+)", re.IGNORECASE
)
_LATEX_EDITOR_CMD_RE = re.compile(
    r"^(?:abrir|abre|mostrar|exibir|ver|editar|iniciar|open)\s+"
    r"(?:o\s+|a\s+)?editor(?:\s+(?:de|do|da|para|no|em))?\s+"
    r"(?:latex|tex|\.tex|overleaf)\s*$|"
    r"^editor(?:\s+(?:de|do|da))?\s+(?:latex|tex|\.tex|overleaf)\s*$",
    re.IGNORECASE,
)
_LATEX_COMPILE_NATURAL_RE = re.compile(
    r"(?:"
    r"\b(?:re?compil(?:a|ar|e)|compil(?:a|ar|e)|compile|build|export(?:a|ar|e)|"
    r"ger(?:a|ar|e)|cri(?:a|ar|e))\b"
    r"(?:\s+(?!na\s+pasta\b)\w+){0,8}\s+"
    r"([\w.\-/\\]+\.tex)\b"
    r"|"
    r"\b(?:re?compil(?:a|ar|e)|compil(?:a|ar|e)|compile|ger(?:a|ar|e))\s+"
    r"([\w.\-/\\]+\.tex)\b"
    r")",
    re.IGNORECASE,
)


def _latex_no_folder_reply(team_info: Optional[dict[str, Any]]) -> str:
    if team_info and str(team_info.get("id") or "").strip():
        return (
            "⚠️ **Nenhuma pasta LaTeX encontrada para este time.**\n\n"
            "Vincule a pasta do projeto na aba **Artigos** do seletor de time "
            "(canto superior direito) ou confirme que `local_path` do time contém arquivos `.tex`."
        )
    return (
        "⚠️ **Nenhuma pasta LaTeX configurada.**\n\n"
        "Cadastre uma pasta com arquivos `.tex` no painel **Artigos** "
        "(canto superior direito) e tente novamente."
    )


def _article_ref_matches_id(article_ref: str, article_id: str) -> bool:
    ref = str(article_ref or "").strip().replace("\\", "/")
    aid = str(article_id or "").strip().replace("\\", "/")
    if not ref or not aid:
        return False
    ref_lower = ref.lower()
    aid_lower = aid.lower()
    if ref_lower == aid_lower:
        return True
    if aid_lower.endswith("/" + ref_lower):
        return True
    return Path(aid).name.lower() == Path(ref).name.lower()


def _article_ref_wants_backup(article_ref: str) -> bool:
    ref = str(article_ref or "").strip().lower().replace("\\", "/")
    return ref.endswith(".bak") or ".tex.bak" in ref


def _article_ref_for_match(article_ref: str) -> str:
    """Normaliza referência do utilizador para id de artigo (.tex principal)."""
    ref = str(article_ref or "").strip().replace("\\", "/")
    lower = ref.lower()
    if lower.endswith(".tex.bak"):
        return ref[: -len(".bak")]
    if lower.endswith(".bak") and not lower.endswith(".tex.bak"):
        return ref[: -len(".bak")]
    return ref


def _article_interactive_option(
    art: dict[str, Any],
    *,
    label: str,
    value: str,
    article_ref: str = "",
) -> dict[str, str]:
    """Option chip for perguntas interativas (label + value only)."""
    _ = art, article_ref
    return {"label": label, "value": value}


def _article_disambiguation_label(
    art: dict[str, Any],
    *,
    prefix: str = "📄 ",
    ws_name: str = "",
    article_ref: str = "",
) -> str:
    include_backup = _article_ref_wants_backup(article_ref)
    return article_picker_label(
        art,
        prefix=prefix,
        ws_name=ws_name,
        include_backup=include_backup,
    )


def _find_team_article_matches(
    article_ref: str,
    team_info: Optional[dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    ref = str(article_ref or "").strip()
    if not ref:
        return []
    match_ref = _article_ref_for_match(ref)
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for ws in list_workspaces_for_team(team_info):
        ws_id = str(ws.get("id") or "").strip()
        if not ws_id:
            continue
        res = list_articles(ws_id)
        if not res.get("ok"):
            continue
        for art in res.get("articles") or []:
            aid = str(art.get("id") or "").strip()
            if aid and _article_ref_matches_id(match_ref, aid):
                matches.append((ws_id, aid, art))
    return matches


def _compile_article_response(ws_id: str, art_id: str) -> dict[str, Any]:
    try:
        res = compile_article(ws_id, art_id)
        if res.get("ok"):
            reply = (
                f"✅ **Compilação bem-sucedida!**\n\n"
                f"O PDF do artigo `{art_id}` foi gerado com sucesso. "
                f"Você pode visualizá-lo ou baixá-lo no painel de Artigos LaTeX."
            )
        else:
            err = res.get("error") or "erro na compilação"
            log_tail = res.get("log_tail") or ""
            reply = (
                f"⚠️ **Erro ao compilar artigo `{art_id}`:**\n\n"
                f"`{err}`\n\n"
            )
            if log_tail:
                reply += f"**Log do LaTeX:**\n```\n{log_tail[:500]}...\n```"
        return {
            "ok": True,
            "reply": reply,
            "interactiveQuestion": None,
        }
    except Exception as e:
        log.exception("Error compiling article via disambiguation: %s", e)
        return {
            "ok": True,
            "reply": f"⚠️ **Erro inesperado ao compilar:** {e}",
            "interactiveQuestion": None,
        }


def _compile_natural_language_response(
    text: str,
    team_info: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    match = _LATEX_COMPILE_NATURAL_RE.search(text.strip())
    if not match:
        return None
    article_ref = str(match.group(1) or match.group(2) or "").strip()
    if not article_ref:
        return None

    active_workspaces = list_workspaces_for_team(team_info)
    if not active_workspaces:
        return {
            "ok": True,
            "reply": _latex_no_folder_reply(team_info),
            "interactiveQuestion": None,
        }

    matches = _find_team_article_matches(article_ref, team_info)
    if not matches:
        return {
            "ok": True,
            "reply": (
                f"⚠️ **Artigo `{article_ref}` não encontrado.**\n\n"
                "Confirme a pasta LaTeX do time ou use "
                f"`compilar artigo <caminho> na pasta <workspace>`."
            ),
            "interactiveQuestion": None,
        }
    if len(matches) == 1:
        ws_id, art_id, _art = matches[0]
        return _compile_article_response(ws_id, art_id)

    options = []
    for ws_id, art_id, art in matches:
        ws_name = next(
            (str(ws.get("name") or ws_id) for ws in active_workspaces if str(ws.get("id") or "") == ws_id),
            ws_id,
        )
        title = str(art.get("title") or art_id)
        compile_art = {**art, "title": title}
        options.append(_article_interactive_option(
            compile_art,
            label=_article_disambiguation_label(
                compile_art,
                prefix=f"📄 Compilar ({ws_name}) ",
                article_ref=article_ref,
            ),
            value=f"compilar artigo {art_id} na pasta {ws_id}",
            article_ref=article_ref,
        ))
    return {
        "ok": True,
        "reply": f"Encontrei `{article_ref}` em mais de uma pasta. Qual deseja compilar?",
        "interactiveQuestion": {
            "question": "Selecione o artigo para compilação:",
            "options": options,
        },
    }


def build_latex_editor_open_response(
    team_info: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Resolve workspace/article pairs and return a LaTeX editor open payload."""
    active_workspaces = list_workspaces_for_team(team_info)
    if not active_workspaces:
        return {
            "ok": True,
            "reply": _latex_no_folder_reply(team_info),
            "interactiveQuestion": None,
        }

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ws in active_workspaces:
        ws_id = str(ws.get("id") or "").strip()
        if not ws_id:
            continue
        res = list_articles(ws_id)
        if not res.get("ok"):
            continue
        for art in res.get("articles") or []:
            pairs.append((ws, art))

    if not pairs:
        return {
            "ok": True,
            "reply": (
                "⚠️ **Nenhum artigo `.tex` encontrado.**\n\n"
                "Verifique se a pasta do time contém arquivos LaTeX compiláveis."
            ),
            "interactiveQuestion": None,
        }

    if len(pairs) == 1:
        ws, art = pairs[0]
        ws_id = str(ws.get("id") or "")
        art_id = str(art.get("id") or "")
        title = str(art.get("title") or art_id)
        return {
            "ok": True,
            "reply": f"📐 **Editor LaTeX aberto** — {title}.",
            "interactiveQuestion": None,
            "latexEditorOpen": {
                "workspace_id": ws_id,
                "article_id": art_id,
                "title": title,
            },
        }

    options = []
    for ws, art in pairs[:40]:
        ws_id = str(ws.get("id") or "")
        ws_name = str(ws.get("name") or ws_id)
        art_id = str(art.get("id") or "")
        options.append(_article_interactive_option(
            art,
            label=_article_disambiguation_label(art, ws_name=ws_name),
            value=f"editar artigo {art_id} na pasta {ws_id}",
        ))

    return {
        "ok": True,
        "reply": "Selecione o artigo para abrir no editor LaTeX:",
        "interactiveQuestion": {
            "question": "Qual artigo editar?",
            "options": options,
        },
    }


def build_latex_images_open_response(
    team_info: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Resolve workspace/article pairs and return a LaTeX image editor open payload."""
    active_workspaces = list_workspaces_for_team(team_info)
    if not active_workspaces:
        return {
            "ok": True,
            "reply": _latex_no_folder_reply(team_info),
            "interactiveQuestion": None,
        }

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ws in active_workspaces:
        ws_id = str(ws.get("id") or "").strip()
        if not ws_id:
            continue
        res = list_articles(ws_id)
        if not res.get("ok"):
            continue
        for art in res.get("articles") or []:
            pairs.append((ws, art))

    if not pairs:
        return {
            "ok": True,
            "reply": (
                "⚠️ **Nenhum artigo `.tex` encontrado.**\n\n"
                "Verifique se a pasta do time contém arquivos LaTeX compiláveis."
            ),
            "interactiveQuestion": None,
        }

    if len(pairs) == 1:
        ws, art = pairs[0]
        ws_id = str(ws.get("id") or "")
        art_id = str(art.get("id") or "")
        title = str(art.get("title") or art_id)
        return {
            "ok": True,
            "reply": f"🖼️ **Editor de imagens aberto** — {title}.",
            "interactiveQuestion": None,
            "latexFiguresOpen": {
                "workspace_id": ws_id,
                "article_id": art_id,
                "title": title,
            },
        }

    options = []
    for ws, art in pairs[:40]:
        ws_id = str(ws.get("id") or "")
        ws_name = str(ws.get("name") or ws_id)
        art_id = str(art.get("id") or "")
        options.append(_article_interactive_option(
            art,
            label=_article_disambiguation_label(art, ws_name=ws_name),
            value=f"abrir imagens do artigo {art_id} na pasta {ws_id}",
        ))

    return {
        "ok": True,
        "reply": "Selecione o artigo para abrir o editor de imagens LaTeX:",
        "interactiveQuestion": {
            "question": "Qual artigo editar?",
            "options": options,
        },
    }


class DisambiguationManager:
    """Manages programmatic disambiguation of user chat messages.

    Intercepts vague user messages and resolves them into structured,
    interactive multiple-choice questions (options) to avoid calling the
    LLM and save tokens/time.
    """

    def check_disambiguation(
        self,
        message: str,
        *,
        team_info: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Check if a message should be programmatically disambiguated.

        Returns:
            A dictionary containing the bot reply and interactive options/question if matched,
            or None if the message should fall through to the LLM agent.
        """
        text = message.strip()
        text_lc = text.lower()

        if not text:
            return None

        # 1. LaTeX Compile execution commands (clicked from interactive buttons)
        m_exec = _LATEX_EXEC_RE.match(text)
        if m_exec:
            art_id = m_exec.group(1)
            ws_id = m_exec.group(2)
            return _compile_article_response(ws_id, art_id)

        # 1a. Natural-language compile (e.g. "gere o pdf de paper.tex")
        compile_natural = _compile_natural_language_response(text, team_info)
        if compile_natural is not None:
            return compile_natural

        # 1b. LaTeX editor open (clicked from interactive buttons)
        m_editor = _LATEX_EDITOR_EXEC_RE.match(text)
        if m_editor:
            art_id = m_editor.group(1)
            ws_id = m_editor.group(2)
            return {
                "ok": True,
                "reply": f"📐 **Editor LaTeX aberto** — `{art_id}` em `{ws_id}`.",
                "interactiveQuestion": None,
                "latexEditorOpen": {
                    "workspace_id": ws_id,
                    "article_id": art_id,
                    "title": art_id,
                },
            }

        # 1c. LaTeX images open (clicked from interactive buttons)
        m_images = _LATEX_IMAGES_EXEC_RE.match(text)
        if m_images:
            art_id = m_images.group(1)
            ws_id = m_images.group(2)
            return {
                "ok": True,
                "reply": f"🖼️ **Editor de imagens aberto** — `{art_id}` em `{ws_id}`.",
                "interactiveQuestion": None,
                "latexFiguresOpen": {
                    "workspace_id": ws_id,
                    "article_id": art_id,
                    "title": art_id,
                },
            }

        # 2. LaTeX List articles in a workspace (clicked from interactive buttons)
        m_list = _LATEX_LIST_RE.match(text)
        if m_list:
            ws_id = m_list.group(1)
            try:
                res = list_articles(ws_id)
                if not res.get("ok"):
                    raise ValueError(res.get("error") or "falha ao listar")
                articles = res.get("articles") or []
                if not articles:
                    return {
                        "ok": True,
                        "reply": f"📂 A pasta selecionada não contém arquivos `.tex` compiláveis.",
                        "interactiveQuestion": None,
                    }
                
                options = []
                for art in articles:
                    art_name = art.get("id") or "artigo.tex"
                    title = str(art.get("title") or art_name)
                    compile_art = {**art, "title": title}
                    options.append(_article_interactive_option(
                        compile_art,
                        label=_article_disambiguation_label(
                            compile_art,
                            prefix="📄 Compilar ",
                        ),
                        value=f"compilar artigo {art_name} na pasta {ws_id}",
                    ))
                
                return {
                    "ok": True,
                    "reply": f"Escolha qual artigo da pasta deseja compilar:",
                    "interactiveQuestion": {
                        "question": "Selecione o artigo para compilação:",
                        "options": options
                    }
                }
            except Exception as e:
                log.exception("Error listing articles for disambiguation: %s", e)
                return {
                    "ok": True,
                    "reply": f"⚠️ Não foi possível listar os artigos: {e}",
                    "interactiveQuestion": None,
                }

        # 3. Help/Greeting trigger
        if _HELP_RE.match(text_lc):
            return {
                "ok": True,
                "reply": (
                    "Olá! Eu sou o assistente do QClaw.\n\n"
                    "Posso ajudar você a compilar artigos LaTeX, gerenciar crons, "
                    "ver o status do Kanban, ou iniciar swarms de agentes. "
                    "Selecione uma das opções rápidas abaixo ou me diga o que deseja:"
                ),
                "interactiveQuestion": {
                    "question": "Menu Principal",
                    "options": [
                        {"label": "📋 Ver status do Kanban / Fila", "value": "status"},
                        {"label": "📄 Compilar artigos LaTeX", "value": "compilar"},
                        {"label": "📚 Ferramentas acadêmicas (template + PDF)", "value": "abrir ferramentas acadêmicas"},
                        {"label": "🐞 Iniciar Swarm multi-agente", "value": "iniciar swarm"},
                        {"label": "⚙️ Ver status do sistema", "value": "status sistema"}
                    ]
                }
            }

        # 3b. Academic tools bar (LaTeX template + PDF)
        if _ACADEMIC_TOOLS_RE.match(text_lc):
            return {
                "ok": True,
                "reply": (
                    "📚 **Ferramentas acadêmicas abertas.**\n\n"
                    "Use a barra acima do campo de mensagem para escolher pasta, artigo e template, "
                    "aplicar o template (🎨) ou regenerar o PDF (📄)."
                ),
                "interactiveQuestion": None,
                "academicToolsBar": True,
            }

        # 3c. LaTeX Monaco editor (embedded .tex editor)
        if _LATEX_EDITOR_CMD_RE.match(text_lc):
            try:
                return build_latex_editor_open_response(team_info)
            except Exception as e:
                log.exception("Error opening LaTeX editor via disambiguation: %s", e)
                return None

        # 4. LaTeX Compile general trigger (exact chip command only)
        if text_lc in _VAGUE_LATEX_COMMANDS:
            try:
                active_workspaces = list_workspaces_for_team(team_info)
                if not active_workspaces:
                    return {
                        "ok": True,
                        "reply": _latex_no_folder_reply(team_info),
                        "interactiveQuestion": None,
                    }
                
                options = []
                for ws in active_workspaces:
                    ws_id = ws.get("id")
                    ws_name = ws.get("name") or ws_id
                    options.append({
                        "label": f"📂 Pasta: {ws_name}",
                        "value": f"listar artigos da pasta {ws_id}"
                    })
                
                return {
                    "ok": True,
                    "reply": "Selecione em qual pasta LaTeX está o artigo que deseja compilar:",
                    "interactiveQuestion": {
                        "question": "Selecione a pasta do artigo:",
                        "options": options
                    }
                }
            except Exception as e:
                log.exception("Error retrieving workspaces for disambiguation: %s", e)
                return None

        # 5. Swarm trigger (exact chip command only)
        if text_lc in _VAGUE_SWARM_COMMANDS:
            return {
                "ok": True,
                "reply": (
                    "Qual tipo de Swarm de agentes você gostaria de iniciar? "
                    "Os swarms coordenam múltiplos agentes especialistas para resolver a tarefa."
                ),
                "interactiveQuestion": {
                    "question": "Escolha o tipo de Swarm:",
                    "options": [
                        {"label": "🐞 Swarm de Correção de Bug (Bug Fix)", "value": "iniciar swarm bug fix"},
                        {"label": "✨ Swarm de Nova Funcionalidade (Feature)", "value": "iniciar swarm feature"},
                        {"label": "🧹 Swarm de Refatoração (Refactor)", "value": "iniciar swarm refactor"},
                        {"label": "🤖 Listar Robôs / Swarms ativos", "value": "listar robos"}
                    ]
                }
            }

        # 6. Kanban / Tasks trigger (exact chip command only)
        if text_lc in _VAGUE_KANBAN_COMMANDS and text_lc != "como enfileirar tarefa":
            return {
                "ok": True,
                "reply": "Você gostaria de gerenciar as tarefas do Kanban ou verificar a fila de prioridades?",
                "interactiveQuestion": {
                    "question": "Operações do Kanban:",
                    "options": [
                        {"label": "📋 Ver status geral do Kanban", "value": "status kanban"},
                        {"label": "🔍 Listar fila de prioridade", "value": "listar fila"},
                        {"label": "➕ Enfileirar uma nova tarefa", "value": "como enfileirar tarefa"}
                    ]
                }
            }

        # 7. Helper for enqueuing task
        if text_lc == "como enfileirar tarefa":
            return {
                "ok": True,
                "reply": (
                    "Para enfileirar uma nova tarefa, use o comando no seguinte formato:\n\n"
                    "`enfileirar TASK-005 prioridade 2`\n\n"
                    "Onde:\n"
                    "- `TASK-005` é o ID do cartão no Kanban.\n"
                    "- `prioridade 2` (opcional) define a prioridade de 1 (mais alta) a 10 (mais baixa)."
                ),
                "interactiveQuestion": {
                    "question": "O que deseja fazer a seguir?",
                    "options": [
                        {"label": "🔍 Listar fila de prioridade", "value": "listar fila"},
                        {"label": "📋 Ver status geral do Kanban", "value": "status kanban"}
                    ]
                }
            }

        return None


# Re-exported for backward compatibility; the canonical implementation now lives
# in ``interactive_questions`` and also understands free-text input prompts.
from .interactive_questions import extract_interactive_question  # noqa: E402,F401

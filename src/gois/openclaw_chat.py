"""OpenClaw / QClaw chat bridge for the gois dashboard.

Lists sessions and reads message history from the on-disk session store.
Sends via DeepSeek + OpenClaw SKILL.md (default) or the bundled openclaw CLI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import subprocess
import sys
import logging
import os
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from openai import OpenAI, RateLimitError

from .chat_history import ChatPersistence
from .disambiguation import DisambiguationManager
from .interactive_questions import extract_interactive_question
from .chat_jobs import is_job_cancelled, list_running_jobs, set_tool_progress, update_job_media
from .chat_progress import ProgressHeartbeat
from .config import AgentConfig, OpenclawChatConfig, OpenclawDoctorConfig, QclawConfig
from .chat_models import (
    anthropic_messages_create,
    attachments_include_images,
    build_openai_client,
    build_user_content,
    coding_system_suffix,
    completion_extra_kwargs,
    effective_chat_default_model_id,
    extract_pdf_text,
    is_audio_attachment,
    is_pdf_attachment,
    list_chat_models,
    model_supports_tools,
    model_supports_vision,
    parse_attachments,
    persist_attachments,
    resolve_attachments_dir,
    resolve_chat_model,
    resolve_vision_model_for_attachments,
    strip_image_blocks_from_messages,
    transcribe_audio_bytes,
    ParsedAttachment,
)
from .llm_tool_limits import (
    cap_llm_tools,
    resolve_model_tool_limit,
    tools_cap_system_note,
    tools_limit_failover,
)
from .chat_prompt_policy import (
    build_skill_notes_prefix,
    effective_max_context_chars,
    effective_system_prompt,
    maybe_log_prompt_sizes,
    MAX_TOOL_ACTION_NUDGES,
    reply_promises_deferred_action,
    requires_immediate_tool_call,
    resolve_token_limits,
    select_chat_tools,
    strip_stale_screenshots_from_messages,
    team_contacts_rule_block,
    TOOL_ACTION_NUDGE,
)
from .long_task_hints import (
    format_long_task_warning,
    long_task_hint_for_tool,
    long_task_hint_for_user_message,
    long_task_rule_block,
)
from .tool_progress import (
    ToolRun,
    begin_tool_run,
    end_tool_run,
    format_availability_message,
    format_status_with_peers,
    format_tool_limit_reply,
    set_tool_turn,
    with_model_prefix,
)
from .local_paths import openclaw_state_dir, project_stack_root
from .recovery import Recovery, _resolve_openclaw_paths
from .secrets_fallback import resolve_llm_api_key
from .openclaw_chat_runtime import QclawRuntime, resolve_qclaw_runtime
from .openclaw_chat_whatsapp_groups import (
    _WHATSAPP_GROUPS_CONTEXT,
    _load_group_memory,
    _load_groups_context,
    _team_context_search,
    _wacli_group_numbers,
)
from .openclaw_chat_skill_map import (
    _TOOL_TO_SKILL_MAP,
    _infer_skills_from_tools,
    extract_mcp_skill_from_tool_call,
)
from .openclaw_chat_sessions import (
    HERMES_SESSION_PREFIX,
    _SESSION_LIST_CACHE,
    _agent_from_session_key,
    _append_transcript_message,
    _ensure_session_entry,
    _load_session_store,
    _read_sessions_json,
    _save_session_store,
    _session_kind,
    _session_title,
    _sessions_store_path,
    _summarize_store_entries,
    clear_session_list_cache,
    create_openclaw_session,
    default_session_key,
    _new_session_title,
    delete_openclaw_session,
    delete_openclaw_session_entry,
    get_session_list_cache,
    hermes_session_key,
    list_openclaw_agents,
    new_session_key,
    openclaw_key_for_hermes_profile,
    parse_hermes_session_key,
    purge_openclaw_sessions,
    set_session_list_cache,
)
from .openclaw_chat_tool_catalog import (
    _desktop_control_enabled_for_host,
    _desktop_control_tool_specs,
    _qclaw_chat_tool_catalogue,
)
from .openclaw_chat_tool_runners import (
    _resolve_group_jid_from_memory,
    _run_agent_evaluate_tool,
    _run_agent_fix_tool,
    _run_ai_kb_tool,
    _run_aulas_memoria_tool,
    _run_calendar_tool,
    _run_chat_personality_tool,
    _run_desktop_control_tool,
    _run_email_memoria_tool,
    _run_gmail_tool,
    _run_google_photos_tool,
    _run_article_images_tool,
    _run_index_team_articles_tool,
    _run_team_article_pdf_tool,
    _run_local_photos_tool,
    _run_notion_tool,
    _run_remove_team_articles_tool,
    _run_ruflo_swarm_tool,
    _run_swarm_manage_tool,
    _run_team_articles_tool,
    _run_team_whatsapp_tool,
    _run_teams_calendar_tool,
    _run_tool_learning_tool,
    _run_whatsapp_busca_tool,
    _run_whatsapp_memoria_tool,
    _send_to_group_jid_direct,
    _wacli_resolve_group_jid,
)
from .openclaw_chat_media_tools import (
    _collect_attachments_from_tool_result,
    _collect_media_from_tool_result,
    _media_total,
    _push_image,
    _push_video,
    _run_show_media,
    _safe_media_url,
    attachments_meta_for_persistence,
    build_chat_media_out,
    has_persistable_chat_media,
    media_payload_from_job_id,
)
from .openclaw_chat_tool_dispatch import (
    QclawChatToolContext,
    _SHELL_TOOL_NAMES,
    _check_whatsapp_guard,
    _compact_monitor_snapshot,
    _extract_whatsapp_message_from_command,
    _extract_whatsapp_recipients,
    _normalize_whatsapp_recipient,
    _run_nano_banana_generate,
    _run_qclaw_chat_tool,
    _run_shell_command,
    _strip_data_urls_for_llm,
    _tool_result_string,
)




_QCLAW_TOOL_SESSION = "agent:main:gois-tool-bridge"
_MEDIA_ONLY_FALLBACK_REPLY = "Imagem gerada. (O modelo não enviou texto adicional.)"

# --- Skill usage tag detection ------------------------------------------------
# The LLM is instructed to emit <!--skills:name1,name2--> on the first line.
_SKILLS_TAG_RE = re.compile(r"^\s*<!--\s*skills:\s*([^>]+?)\s*-->\s*\n?", re.IGNORECASE)


def _extract_skills_used(reply: str) -> tuple[str, list[str]]:
    """Strip the hidden skills tag from the reply and return (clean_reply, skill_names)."""
    m = _SKILLS_TAG_RE.match(reply)
    if not m:
        return reply, []
    raw = m.group(1).strip()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    clean = reply[m.end():]
    return clean, names


# --- Skill inference from tools used ------------------------------------------
# When the LLM does not emit the <!--skills:--> tag, we can infer likely skills
# from the tools that were actually called during the conversation turn.

# --- wacli group numbers tool implementation ----------------------------------





# Prepended to SKILL.md block when shell tools are enabled (skills often say "run in terminal").
_CHAT_SHELL_SKILLS_NOTE = (
    "## Execução bash neste chat\n"
    "Use a ferramenta `qclaw_run_shell` para correr comandos no Mac do utilizador. "
    "Nunca peça ao utilizador para abrir o Terminal ou copiar comandos — execute você "
    "e resuma stdout/stderr na resposta.\n"
)

_CHAT_MONITOR_UPDATE_NOTE = (
    "## Atualizar git e reiniciar o monitor\n"
    "Para **git pull** e **restart** do próprio gois (não repos de times), "
    "use `qclaw_monitor_update` — skill `qclaw-chat-monitor-update`. "
    "Ações: `status`, `pull`, `restart`, `update` (pull + submódulos + pip + restart). "
    "O restart corre em background; avise que o chat pode cair ~30s.\n"
)

_CHAT_DESKTOP_SKILLS_NOTE = (
    "## Controlo do desktop macOS neste chat\n"
    "Use as ferramentas `qclaw_desktop_*` para ver e operar o ecrã real do utilizador. "
    "Fluxo recomendado: `qclaw_desktop_screenshot` → analisar coordenadas → "
    "`qclaw_desktop_click` / `qclaw_desktop_type` / `qclaw_desktop_key`. "
    "Coordenadas são pixels lógicos (origem canto superior esquerdo). "
    "Para janelas específicas, use `app`/`window_title` no screenshot ou "
    "`qclaw_desktop_open_app` antes de clicar.\n"
)

_CHAT_MEDIA_SKILLS_NOTE = (
    "## Mídia inline no chat\n"
    "Use `qclaw_show_media` para exibir imagens (PNG/JPG/WebP/GIF) ou vídeos (MP4/WebM/MOV) "
    "diretamente na bubble do chat. Aceita caminho local absoluto OU URL https/http. "
    "Sempre que gerar/encontrar mídia que ajude o utilizador (gráfico, screenshot, clipe), "
    "chame esta ferramenta com `caption` curto. Para vídeos > 4 MB, forneça URL externa. "
    "Não cole markdown `![](file:///…)` — sempre passe pela ferramenta.\n"
)

_CHAT_ARTICLE_IMAGES_SKILLS_NOTE = (
    "## Figuras de artigos LaTeX no chat\n"
    "Para **listar, exibir e alterar figuras** de artigos `.tex`, use "
    "`qclaw_article_images` — skill `qclaw-chat-article-images`. "
    "Ações: `list` (inventário), `show` (preview inline com `images[]`), "
    "`replace` (substituir ficheiro), `generate` (Grok/Nano/Imagen), `compile` (PDF). "
    "Parâmetros obrigatórios: `workspace_id`, `article_id`. "
    "Obtenha IDs via MCP `list_article_workspaces` + `list_articles` (qclaw-cards). "
    "`generate` corre em background com barra de progresso.\n"
)

_CHAT_SLIDES_PDF_SKILLS_NOTE = (
    "## Slides e PDF no chat\n"
    "Para **preview paginado** de PDF ou apresentação (PPTX/PPT/ODP/KEY), use "
    "`qclaw_show_slides_pdf` — skill `qclaw-chat-slides-pdf`. "
    "**Só quando o utilizador pedir explicitamente** ver/preview/mostrar páginas/renderizar. "
    "Se pedirem *baixar* ou *download*, use `qclaw_team_files_download` (link 📎), não preview. "
    "Renderiza cada página como PNG e exibe inline (até 12 páginas por turno). "
    "Parâmetros: `path` (obrigatório), `pages` opcional (`1-5`, `3`), `dpi`, `max_pages`. "
    "**Background:** corre em thread separada com barra de progresso por página; "
    "o chat fica livre e recebe avisos de progresso automaticamente. "
    "No Cursor IDE, use o script `skills/qclaw-chat-slides-pdf/scripts/render_pages.py` "
    "e a ferramenta Read nas PNGs geradas.\n"
)

_CHAT_SLIDES_CORNER_DECOR_SKILLS_NOTE = (
    "## Decoração de cantos em slides\n"
    "Para **ilustrações nos cantos inferiores** de decks já criados (PPTX/HTML), "
    "use `qclaw_slides_corner_decor` — skill `qclaw-slides-corner-decor`. "
    "Com `analyze=true` lista slides com canto livre; sem analyze gera imagens "
    "via `provider` nano (Gemini) ou grok (xAI) e aplica só onde couber. "
    "Depois use `qclaw_show_slides_pdf` para preview do resultado.\n"
)

_CHAT_SLIDES_REPLACE_DIDACTIC_SKILLS_NOTE = (
    "## Substituir slide por imagem didática\n"
    "Para **trocar slides inteiros** por ilustrações didáticas 16:9 geradas por IA, "
    "use `qclaw_slides_replace_didactic` — skill `qclaw-slides-replace-didactic`. "
    "Informe `slides` (ex.: `\"3\"` ou `\"2-5\"`); o texto do slide alimenta o prompt. "
    "Com `analyze=true` só extrai conteúdo. Providers: nano ou grok. "
    "Preview: `qclaw_show_slides_pdf`.\n"
)

_CHAT_SLIDES_BATCH_IMAGES_SKILLS_NOTE = (
    "## Batch de slides como imagens (100–200, modelo único)\n"
    "Para gerar **muitos slides (100–200) como PNG** com **um único modelo** e empacotar "
    "em ZIP anexado, use `qclaw_slides_batch_images` — skill `qclaw-slides-batch-images`. "
    "Aceita deck PPTX/HTML (`path`) ou ficheiro JSON/JSONL de prompts (`prompts_file`). "
    "Defina `provider` + `model` + `no_fallback=true` para consistência visual. "
    "Fluxo: `analyze=true` (contar slides) → piloto 3–5 em `1K` → lote em `2K` com "
    "`max_slides` 100–200. MCP equivalente: `slides_batch_images`. "
    "Preview: primeiras imagens inline + ZIP em `attachments[]`.\n"
    "**Progresso noutro chat:** qualquer conversa pode consultar lotes em curso com "
    "`qclaw_chat_generation_status` (filtro `keyword`, ex. TASK-037).\n"
    "**Background:** `qclaw_slides_batch_images` corre sempre em thread separada — "
    "o chat não bloqueia e recebe avisos de progresso no histórico.\n"
    "**Artefatos em disco (falha parcial):** se o lote falhar ou disser "
    "`no images generated`, **sempre** chame `qclaw_slides_batch_artifacts` "
    "(skill `qclaw-chat-slides-batch-artifacts`) com `keyword` (ex. d3) ou "
    "`output_dir` antes de regenerar — PNGs/ZIP podem existir mesmo com job em erro.\n"
    "**Auto-correção `empty_prompt`:** se o JSONL tiver prompts vazios (Excel Kanban "
    "truncado), o batch **corrige automaticamente** — substitui por fonte válida "
    "(ex. `ConsultoriaGED/slides-d{n}/prompts-d{n}-100.jsonl`), repara o `.xlsx` do card "
    "quando possível, e **retoma** com `resume`. `card_id`/`task_id` (ex. TASK-038) "
    "ajuda a reparar o Excel do Kanban; também é inferido do caminho do JSONL. "
    "`no_auto_fix_prompts=true` desliga.\n"
    "Ferramentas `qclaw_grok_imagine_generate`, `qclaw_imagen_generate` e "
    "`qclaw_nano_banana_generate` também correm em background no chat com avisos "
    "de progresso.\n"
)

_CHAT_GROK_IMAGINE_SKILLS_NOTE = (
    "## Grok Imagine (imagens xAI) neste chat\n"
    "Para **painel interativo inline** (prompt, 900+ estilos, proporção, botão Gerar), "
    "use `qclaw_chat_widget_image_generate` — **obrigatório** chamar a ferramenta; "
    "nunca diga que o editor está aberto sem `tool_calls`. "
    "Após gerar, `qclaw_chat_widget_image_editor` para regenerar/upscale.\n"
    "Para gerar direto (sem painel), use `qclaw_grok_imagine_generate` — skill "
    "`qclaw-grok-imagine`. Requer `XAI_API_KEY` em /chaves (importável do Roteiro Viral). "
    "Para edição de foto local / 4K Gemini, use `qclaw_nano_banana_generate`. "
    "Para Imagen 4 local (sem API Roteiro Viral), use `qclaw_imagen_generate` — skill "
    "`qclaw-roteiro-gerar-imagem`.\n"
    "Para QUALQUER modelo de imagem (OpenAI gpt-5-image, Flux, Seedream, Grok, Gemini, …) "
    "via OpenRouter, use `qclaw_openrouter_image_generate` com `model=owner/model` "
    "(ex. `openai/gpt-5-image`, `black-forest-labs/flux.2-pro`). Requer `OPENROUTER_API_KEY`. "
    "Veja a lista com `qclaw_list_image_models`.\n"
    "**Lotes de slides (50–200):** prefira `qclaw_slides_batch_images` (slider automático). "
    "Se gerar slide a slide com Grok, passe `slide_index` + `slide_total` (ou `block_index` + "
    "`block_total`) em **cada** chamada e continue o lote **sem pedir confirmação** até "
    "terminar — o painel mostra o slider de blocos em tempo real.\n"
)

_CHAT_HEYGEN_MCP_SKILLS_NOTE = (
    "## HeyGen MCP oficial neste chat\n"
    "Para vídeos HeyGen (avatar, Video Agent, tradução, lip-sync), use "
    "`qclaw_heygen_via_openclaw` — delega ao agente OpenClaw com MCP remoto HeyGen "
    "(OAuth, sem API key). Siga a skill `qclaw-heygen-mcp`. "
    "Para batch de curso na API Roteiro Viral, use a skill `qclaw-roteiro-heygen`.\n"
)

_CHAT_SUNO_MCP_SKILLS_NOTE = (
    "## Suno MCP neste chat\n"
    "Para gerar música com IA (Suno), use `qclaw_suno_via_openclaw` — delega ao "
    "agente OpenClaw com MCP Suno (AceDataCloud). Siga a skill `qclaw-suno-mcp`. "
    "Requer `ACEDATACLOUD_API_TOKEN` em /env-keys. "
    "Para música directa com Google Gemini Lyria (instrumental, trilha, referência "
    "visual), use `qclaw_gemini_music_generate` — skill `qclaw-chat-gemini-music` "
    "(requer `GEMINI_API_KEY`). "
    "Para banda virtual completa (letras, clipe, imagens, batch RV), use "
    "`qclaw_virtual_band_create` — skill `qclaw-chat-roteiro-musica`.\n"
)

_CHAT_SEEDANCE_MCP_SKILLS_NOTE = (
    "## Seedance MCP neste chat\n"
    "Para gerar vídeo com IA (Seedance / ByteDance, incl. Seedance 2.0), use "
    "`qclaw_seedance_via_openclaw` — delega ao agente OpenClaw com MCP Seedance "
    "(AceDataCloud). Siga a skill `qclaw-seedance-video`. "
    "Requer `ACEDATACLOUD_API_TOKEN` em /env-keys. "
    "Modelo por defeito: `doubao-seedance-2-0-260128`. "
    "Para avatar com fala (HeyGen), use `qclaw_heygen_via_openclaw`.\n"
)

_CHAT_PHOTO_TO_VIDEO_SKILLS_NOTE = (
    "## Foto → vídeo (MCP) neste chat\n"
    "Para transformar foto em vídeo, use `qclaw_photo_to_video_plan` para obter "
    "provider + prompt, depois:\n"
    "- Cinematográfico: `qclaw_runway_via_openclaw` (Runway OAuth)\n"
    "- Pessoa falando: `qclaw_heygen_via_openclaw` (HeyGen OAuth)\n"
    "- Motion/i2v rápido: `qclaw_seedance_via_openclaw` (AceDataCloud)\n"
    "- Testar modelos: MCP `replicate` ou `qclaw_replicate_generate`\n"
    "Siga `qclaw-chat-photo-to-video-mcp`. Setup: `./scripts/setup_photo_to_video_mcps.sh`.\n"
)

_CHAT_RUNWAY_MCP_SKILLS_NOTE = (
    "## Runway MCP neste chat\n"
    "Para vídeo cinematográfico image-to-video (foto + movimento de câmera), use "
    "`qclaw_runway_via_openclaw` — delega ao agente OpenClaw com MCP Runway (OAuth). "
    "Para lip-sync/avatar falando, prefira `qclaw_heygen_via_openclaw`.\n"
)

_CHAT_VIRTUAL_BAND_SKILLS_NOTE = (
    "## Banda virtual (Roteiro Viral) neste chat\n"
    "Para criar banda/música com pipeline RV (letras por trecho, prompts de clipe, "
    "imagens, Suno no worker), use:\n"
    "1. `qclaw_virtual_band_create` com `concept` (e opcionalmente `wait=true`)\n"
    "2. `qclaw_virtual_band_job_status` com `job_id`\n"
    "3. `qclaw_virtual_band_suggest_members` / `qclaw_virtual_band_portrait` para integrantes\n"
    "4. `qclaw_virtual_band_list_bands` para bandas existentes\n"
    "Modo local por defeito (Gemini + Imagen); `QCLAW_RV_USE_API=1` só se quiser API RV externa. "
    "Requer `GEMINI_API_KEY`. "
    "Siga `qclaw-chat-roteiro-musica`. Para música avulsa Suno, use `qclaw_suno_via_openclaw`.\n"
)

_CHAT_ROTEIRO_THUMBNAILS_SKILLS_NOTE = (
    "## Thumbnails (Roteiro Viral) neste chat\n"
    "Para thumbnails YouTube/redes com Imagen + neurodesign na API RV:\n"
    "1. `qclaw_thumbnail_prompt` — prompt final (revisar antes de gerar)\n"
    "2. `qclaw_thumbnail_generate` — enfileirar Imagen (`wait=true` opcional)\n"
    "3. `qclaw_thumbnail_job_status` — poll com `job_id`\n"
    "4. `qclaw_thumbnail_concepts` — 3 ideias virais a partir de roteiro\n"
    "Requer API RV + `GEMINI_API_KEY`. Siga `qclaw-chat-roteiro-thumbnails`. "
    "Para texto via Grok/xAI, use `qclaw_grok_imagine_generate`.\n"
)

_CHAT_ROTEIRO_MONGO_SKILLS_NOTE = (
    "## MongoDB Roteiro Viral neste chat\n"
    "Para consultar **todas** as coleções MongoDB do domínio RV (read-only):\n"
    "1. `qclaw_rv_mongo_collections` — listar tabelas (`include_counts=true` para totais)\n"
    "2. `qclaw_rv_mongo_find` — buscar documentos (`collection`, `filter`, `limit`)\n"
    "3. `qclaw_rv_mongo_count` — contar registos\n"
    "Para **roteiros gerados e atos** (alto nível, sem montar filtros Mongo):\n"
    "1. `qclaw_rv_scripts_list` — listar jobs de roteiro\n"
    "2. `qclaw_rv_scripts_get` — texto completo do roteiro\n"
    "3. `qclaw_rv_scripts_acts` / `qclaw_rv_scripts_act` — atos por job_id\n"
    "4. `qclaw_rv_scripts_insert` / `update` / `insert_acts` — gravar texto do utilizador\n"
    "Banco: `QCLAW_RV_MONGO_DB` (default `viralscript`), não confundir com `MONGODB_DB` do QClaw. "
    "Siga `qclaw-chat-roteiro-mongo` ou `qclaw-chat-roteiro-scripts`.\n"
)

_CHAT_ROTEIRO_API_SKILLS_NOTE = (
    "## API Roteiro Viral local neste chat\n"
    "Código em `src/gois/roteiro_viral/` — modo local por defeito (sem servidor externo):\n"
    "1. `qclaw_rv_api_health` — verificar API embedded\n"
    "2. `qclaw_rv_api_post` — criar jobs (`/book/generate`, `/acts/plan`, `/course/generate`, …); `wait=true` aguarda\n"
    "3. `qclaw_rv_api_get` — consultar (`/jobs`, `/books/ID`, `/acts/JOB/bundle`)\n"
    "4. `qclaw_rv_job_status` / `qclaw_rv_jobs_list` — acompanhar jobs\n"
    "Índice de domínios: `qclaw-chat-roteiro-index`. Livros: execução inline via chat (`qclaw_roteiro_book_sync`).\n"
)

_CHAT_WHATSAPP_BUSCA_SKILLS_NOTE = (
    "## WhatsApp — busca, envio e agenda neste chat\n"
    "**Cadastro** na allowlist (add/remove/toggle): só via dashboard `/allowlist` — "
    "bloqueado no chat por segurança.\n"
    "**Envio** para destinos já cadastrados: `qclaw_send_whatsapp` com `message` e "
    "opcional `to` (JID, número ou nome parcial, ex.: FUNCEME/UNIFOR ou "
    "120363139397648190@g.us). Sem `to`, vai para whatsapp_digest.recipient. "
    "Padrão enfileirado (`wait=false`) — retorna rápido com `job_id`.\n"
    "**Se envio falhar, travar ou o usuário disser que não recebeu:** "
    "`qclaw_wacli_unlock` (kill_sync: true) → repetir `qclaw_send_whatsapp` "
    "(com `wait: true` só se precisar confirmar entrega). "
    "Nunca `wacli send` no shell.\n"
    "**Estado da fila / lock / sync:** `qclaw_whatsapp_status` — instantâneo; "
    "nunca `wacli doctor` ou `wacli sync` no shell (trava o chat).\n"
    "Antes de dizer que um grupo **não** está na allowlist, chame "
    "`qclaw_allowlist_list` (campo `enabled_group_jids`).\n"
    "PDF/imagem no zap: `qclaw_send_whatsapp_file` (PDF como documento; "
    "`mode=preview` para páginas renderizadas) ou `qclaw_show_slides_pdf` "
    "com `whatsapp_to`.\n"
    "Para procurar mensagens em **qualquer** chat/grupo WhatsApp, use "
    "`qclaw_whatsapp_messages_search` (wacli ao vivo). "
    "Para **baixar anexos** (planilha, PDF, imagem), use "
    "`qclaw_whatsapp_media_download` com `msg_id` (+ `chat` JID) — "
    "nunca `wacli media download` no shell. "
    "Para procurar contato por nome/telefone no WhatsApp, use "
    "`qclaw_whatsapp_contacts_search` (wacli ao vivo, ~1s) — **não** chame "
    "`qclaw_whatsapp_agenda_sync` antes. "
    "Para listar a agenda já sincronizada (sem filtro) ou estatísticas, use "
    "`qclaw_whatsapp_agenda_list` / `qclaw_whatsapp_agenda_stats`. "
    "Sincronizar agenda (`qclaw_whatsapp_agenda_sync`) só quando o usuário pedir "
    "explicitamente para atualizar/importar contatos. "
    "Para histórico de **grupos já indexados** no banco, use "
    "`qclaw_whatsapp_memoria_search` / `qclaw_whatsapp_memoria_index`. "
    "Tela web da agenda: `/agenda`. Siga a skill `qclaw-chat-whatsapp-busca`.\n"
)

_CHAT_NOTION_SKILLS_NOTE = (
    "## Notion — workspace completo\n"
    "Use `qclaw_notion_*` para buscar, consultar databases, criar tarefas/páginas e "
    "atualizar propriedades. Calendário Google↔Notion: `qclaw_calendar_notion_sync` "
    "(skill `notion-calendar-sync`). Siga `qclaw-chat-notion`.\n"
)

_CHAT_OVERLEAF_TEMPLATE_SKILLS_NOTE = (
    "## Templates Overleaf / LaTeX\n"
    "Base em `data/overleaf-templates/` (article-a4-12pt, article-twocol, report-chapters, "
    "beamer-basic + templates extraídos). **Procurar templates salvos:** "
    "`qclaw_overleaf_template_list` / `_get` / `_sync` — skill `qclaw-chat-latex-templates-search`. "
    "**Pipeline científico (fontes oficiais):** `_discover` / `_fetch` / `_catalog_*` / `_pipeline` — "
    "skill `qclaw-chat-latex-template-pipeline`. "
    "Extrair/aplicar: `_extract` / `_apply` / `_preview` (catálogo inline + prévia antes/depois; "
    "só `confirm=true` após aceite — skill `qclaw-chat-overleaf-template`). "
    "Clone/push: `qclaw-overleaf`.\n"
)

_CHAT_GOOGLE_FOTOS_SKILLS_NOTE = (
    "## Google Fotos — Picker (regra obrigatória)\n"
    "O agente **NÃO pode** abrir, navegar nem selecionar fotos sozinho na nuvem Google. "
    "Só recebe o que o **usuário marcar** no link `picker_uri_web`.\n"
    "Para busca **automática**, use `qclaw_local_photos_*` (skill `qclaw-chat-fotos-locais`) "
    "em pastas do Mac — sem Picker.\n"
    "**PROIBIDO** dizer: 'clique Concluído sem selecionar', 'vejo todas as fotos', "
    "'escolho a melhor para você' ou prometer busca automática na nuvem Google.\n"
    "Instrua o usuário: abrir o link → **tocar nas fotos** (ficam marcadas) → Concluído → "
    "avisar no chat → então `picker_poll` + `picker_list` + `get`.\n"
    "Se lista vazia: pedir para selecionar de novo (mín. 1 foto). "
    "Alternativas: fotos locais, anexo no chat, ou gerar imagem com IA.\n"
)

_CHAT_LOCAL_FOTOS_SKILLS_NOTE = (
    "## Fotos locais (Mac) — busca automática\n"
    "Use `qclaw_local_photos_recent` / `qclaw_local_photos_search` / `qclaw_local_photos_get` "
    "para achar e mostrar fotos em `~/Pictures`, `~/Downloads`, `~/Desktop` ou pasta customizada — "
    "**sem Google Picker**. Preferir quando o usuário quer que o agente escolha/liste sozinho.\n"
)

_CHAT_AULAS_MEMORIA_SKILLS_NOTE = (
    "## Memória de aulas, notas e skills de estudo\n"
    "Use `qclaw_aula_store` / `qclaw_aula_search` / `qclaw_aula_list` para disciplinas e aulas; "
    "`qclaw_nota_store` / `qclaw_nota_search` / `qclaw_nota_list` para anotações e notas de prova; "
    "`qclaw_study_skill_store` / `qclaw_study_skill_search` / `qclaw_study_skill_list` para "
    "competências aprendidas; `qclaw_aulas_memoria_summary` para totais; "
    "`qclaw_aulas_memoria_show` para painel completo formatado. "
    "Backend: **MongoDB** (coleções `aulas`, `aulas_notas`, `study_skills`) — **não** há banco "
    "por time; a memória é global. **Sempre** chame `qclaw_aulas_memoria_show` ou "
    "`qclaw_nota_list` antes de dizer que não há notas; **nunca** verifique existência "
    "do ficheiro `.stack/chat/aulas_memoria.sqlite3`. "
    "Ao atualizar com `nota_id`, se a nota já tiver `grade` atribuída, **não** a apague "
    "(omitir `grade` preserva a existente; só envie `grade` para alterar ou atribuir). "
    "Para exibir bonito: skill `qclaw-chat-projeto-memoria`.\n"
)

_CHAT_MEMCLAW_MEMORIA_SKILLS_NOTE = (
    "## MemClaw — memória de longo prazo (persistente entre sessões)\n"
    "Use `qclaw_memclaw_memoria_show` para painel completo (stats + keystones + "
    "memórias); `qclaw_memclaw_memoria_summary` para totais; "
    "`qclaw_memclaw_memoria_search` para busca semântica por tema. "
    "**Sempre** chame uma dessas tools antes de dizer que não há memória MemClaw. "
    "Distinto de memória acadêmica (`qclaw_aulas_memoria_*`) e email/WhatsApp. "
    "Backend: **MongoDB embutido** (sem Docker). "
    "Para **gravar**: `qclaw_mcp__memclaw__memclaw_write` ou skill `memclaw`. "
    "Para exibir bonito: skill `qclaw-chat-memclaw-memoria`.\n"
)

_CHAT_TEAM_SWARM_SKILLS_NOTE = (
    "## Swarm de times — resolver cards do Kanban\n"
    "Para executar o swarm vinculado a um time e processar cards pendentes:\n"
    "1. `qclaw_list_teams` → `team_id`\n"
    "2. `qclaw_team_kanban` ou `qclaw_cards_get_cards` → backlog do time\n"
    "3. `qclaw_run_team_swarm` ou `qclaw_cards_run_team_swarm` — multi-agente\n"
    "**IDE (VS Code / Cursor / Kiro / Antigravity):** quando pedirem "
    "'rodar swarm na IDE', 'executar swarm no VS Code/Cursor' ou 'swarm do curso "
    "no vscode', use `qclaw_run_team_swarm` ou `qclaw_run_swarm` — agentes com "
    "`execution_backend` IDE recebem handoff (contexto + abrir app); demais usam LLM. "
    "Card único sem swarm → `qclaw_kanban_ide_handoff`. "
    "`open_ide: false` só materializa contexto sem abrir a IDE.\n"
    "**Background:** `qclaw_run_team_swarm` corre em thread separada com barra de "
    "progresso por agente; o chat fica livre e recebe avisos de progresso.\n"
    "Se `busy: true`, só reexecute com `force` após confirmação do utilizador.\n"
    "Preferir ferramentas `qclaw_cards_*` (MCP qclaw-cards in-process).\n"
    "Siga a skill `qclaw-chat-team-swarm`.\n"
)

_CHAT_CARDS_MCP_NOTE = (
    "## Kanban e erros via MCP qclaw-cards\n"
    "O chat invoca o mesmo backend do MCP `qclaw-cards` via ferramentas `qclaw_cards_*`:\n"
    "1. `qclaw_cards_get_errors` — erros agregados (logs + monitor)\n"
    "2. `qclaw_cards_errors_to_cards` — transformar erros em cards (`dry_run` primeiro)\n"
    "3. `qclaw_cards_list_teams` / `qclaw_cards_get_cards` / `qclaw_cards_move_card`\n"
    "4. `qclaw_cards_team_swarm_status` / `qclaw_cards_run_team_swarm`\n"
    "Aliases legados: `qclaw_get_errors`, `qclaw_errors_to_cards`.\n"
    "Siga a skill `qclaw-chat-errors`.\n"
)

_CHAT_AWS_SKILLS_NOTE = (
    "## AWS — custos e máquinas (FinOps)\n"
    "Para **custo AWS**, **EC2/RDS**, **desperdícios** (EBS órfão, EIP solto) ou "
    "**snapshots do ambiente**, use `qclaw_aws_overview` — skill `qclaw-chat-aws-manage`. "
    "Credenciais: [`/chaves`](/chaves) → aba AWS (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, "
    "`AWS_DEFAULT_REGION` ou `AWS_PROFILE`). "
    "Detalhe: `qclaw_aws_cost` (UnblendedCost), `qclaw_aws_ce_get_cost_and_usage` "
    "(get-cost-and-usage com BlendedCost e período explícito — skill "
    "`qclaw-chat-aws-ce-get-cost-and-usage`), `qclaw_aws_machines` (listar/start/stop "
    "com `confirm=true`), `qclaw_aws_waste`, `qclaw_aws_env` (scan/list snapshots). "
    "MCP `qclaw-skills`: `aws_overview`, `aws_cost`, `aws_ce_get_cost_and_usage`, "
    "`aws_machines`, `aws_waste`, `aws_env`. "
    "Não confundir com custo de **modelos LLM** (`qclaw-chat-custo-modelo`). "
    "Job cron ec2-monitor com erro → skill `qclaw-ec2-monitor-diag`.\n"
)

_CHAT_TEAM_FILES_SEARCH_NOTE = (
    "## Arquivos do time — busca, download, envio e preview\n"
    "**Envio (email / WhatsApp):** quando pedirem *enviar por email*, *mandar no zap*, "
    "*compartilhar no whatsapp*, *anexar e enviar* → **`qclaw_team_files_send`** "
    "(skill `qclaw-chat-team-files-send`). Informe `channel`, destino (`to` ou "
    "`team_group: true`) e `message`.\n"
    "**Download (link no chat):** quando pedirem *baixar*, *download*, *trazer PDF* "
    "ou *quero o arquivo* (sem enviar a terceiros) → **`qclaw_team_files_download`** "
    "(skill `qclaw-chat-team-files-download`). Responda só com link 📎 — sem renderizar.\n"
    "**Busca (só listar):** `qclaw_team_files_search` — skill `qclaw-chat-team-files-search` — "
    "apenas quando pedirem *listar*, *achar*, *onde está* ou houver ambiguidade real.\n"
    "**Preview (só quando pedirem):** `qclaw_show_slides_pdf` — skill `qclaw-chat-slides-pdf` — "
    "somente se pedirem *ver*, *preview*, *mostrar páginas*, *renderizar* no chat. "
    "Nunca use preview como substituto de download.\n"
    "Parâmetros busca: `team_name` ou `team_id`, `query`, `pattern`, `extension`, `all_teams`, `path`.\n"
    "MCP `qclaw-skills`: `team_files_search`, `team_files_download`, `team_files_send`. "
    "Read-only na origem.\n"
    "Artigos LaTeX indexados no ChromaDB → `qclaw_team_articles` (outra ferramenta).\n"
)

_CHAT_FALHAS_SKILLS_NOTE = (
    "## Falhas do chat — visão unificada\n"
    "Para listar **qualquer falha recente** (ferramentas, backend LLM, histórico, monitor):\n"
    "1. `qclaw_chat_failures` — agrega monitor + tool_learnings + mensagens + errorlog (MongoDB)\n"
    "2. Parâmetros úteis: `since_minutes`, `query`, `session_key`, `sources` (incl. `errorlog`)\n"
    "3. Se `kind=errorlog` → ler `detail` (stderr); skill `qclaw-chat-error-log`\n"
    "4. Se `kind=tool` → `qclaw_tool_learning_search` (skill `qclaw-chat-autoaprimorar`)\n"
    "5. Se `kind=monitor` e precisa de cards → `qclaw-chat-errors`\n"
    "Siga a skill `qclaw-chat-falhas` (transparência: `qclaw-chat-error-log`).\n"
)

_CHAT_SKILLS_MCP_NOTE = (
    "## Skills via MCP (catálogo universal)\n"
    "Para descobrir e usar **qualquer** skill do repositório sem depender só do prompt:\n"
    "1. `qclaw_skills_search` ou `qclaw_skills_list` — achar a skill\n"
    "2. `qclaw_skills_get` — ler SKILL.md\n"
    "3. `qclaw_skills_tools_for_skill` — ferramentas nativas `qclaw_*` da skill\n"
    "4. `qclaw_skills_run` — executar script CLI quando existir\n"
    "MCP externo equivalente: servidor `qclaw-skills` (tools skills_*).\n"
)

_CHAT_MCP_REGISTER_NOTE = (
    "## MCP externos (cadastro + chat + Cursor)\n"
    "Para **cadastrar** um MCP externo: `qclaw_mcp_register` (action=register) ou UI `/mcp`.\n"
    "Após cadastro: tools ficam no chat como `qclaw_mcp__{servidor}__{tool}` e o config "
    "sincroniza em `.mcp.json` + `.cursor/mcp.json` para o Cursor.\n"
    "Listar: action=list · sync tools: sync_tools · sync configs: sync_config · skill "
    "`qclaw-mcp-register`.\n"
)

_CHAT_TEXT_HUMANIZER_MCP_NOTE = (
    "## Humanização de texto (MCP) neste chat\n"
    "Routing (skill `qclaw-text-humanizer-mcp`):\n"
    "1. **Humanizar / melhorar fluidez** → `qclaw_mcp__humantext__humanize_text` "
    "(skill `qclaw-chat-humantext-mcp`; setup `./scripts/setup_humantext_mcp.sh` + "
    "`HUMANTEXT_API_KEY`). Académico: `tone: academic`, `level: light`.\n"
    "2. **Detetar texto IA** → `qclaw_mcp__ai-humanizer__detect` (Text2Go, grátis — "
    "`setup_ai_humanizer_mcp.sh`) ou `qclaw_mcp__humantext__detect_ai`.\n"
    "3. **Alternativa local (MIT, sem MCP pago)** → `qclaw_skills_get` + edição com skill "
    "`humanizer` (remove padrões de escrita IA).\n"
    "4. **Humanizer PRO** (`qclaw_mcp__humanizer-pro__*`) — **só** se pedido explicitamente; "
    "não usar em TCC/dissertação/artigo científico (foco em burlar detectores). "
    "Setup: `setup_humanizer_pro_mcp.sh` + OAuth.\n"
    "Execute a tool MCP **neste turno** — não prometa humanizar depois.\n"
)

_CHAT_ROTEIRO_LAB_SKILLS_NOTE = (
    "## Laboratórios Roteiro Viral (Script Lab)\n"
    "Menu 🧪 **Laboratório** da API RV — acesse via `qclaw_skills_search` / `qclaw_skills_get` / "
    "`qclaw_skills_run` (MCP `skills_*`). Índice: **`qclaw-roteiro-lab-index`**.\n"
    "**Roteiro por atos:** `qclaw-roteiro-lab-atos` → `lab-refino` → `lab-limpa-fala`; "
    "com texto pronto: `qclaw_chat_widget_script_acts_editor` com `text` + `num_acts` "
    "(divide atos com LLM por defeito; `no_llm` só se pedir divisão local); "
    "estilos: `lab-estilos-escritor`, `lab-viral-humor`; análise: `lab-analise`.\n"
    "**Texto longo:** `qclaw-roteiro-lab-texto-aula`, `qclaw-roteiro-lab-texto-secao`.\n"
    "**Referências reais:** quando o material tiver bibliografia ou citações, use `qclaw-article-references-verify` para confirmar se as referências existem de fato.\n"
    "**Outros labs:** metadata, ideias, youtube-paraphrase, software-spec, video-test "
    "(prefixo `qclaw-roteiro-lab-*`).\n"
    "Hooks/análise global: **`qclaw-roteiro-script-lab`**. Catálogo RV: **`qclaw-roteiro-index`**. "
    "Thumbnails/imagem/analyze-face correm localmente; labs de livros/curso ainda podem usar `QCLAW_RV_USE_API=1`.\n"
)

_CHAT_RV_SCREENS_NOTE = (
    "## Telas Roteiro Viral no chat (editores em diálogo)\n"
    "Para abrir **qualquer tela** da UI Roteiro Viral como editor em diálogo no chat, use "
    "`qclaw_chat_widget_rv_screen` — **obrigatório** chamar a ferramenta; nunca diga que o "
    "editor está aberto sem `tool_calls`. Parâmetros: `path` (/storyboard), `screen` (slug) "
    "ou `name` (nome da tela). Listar catálogo: `qclaw_rv_screens_list`.\n"
    "Widgets nativos (melhor UX): Lab de Roteiros → `qclaw_chat_widget_script_acts_editor`; "
    "com texto pronto no chat passe `text`/`roteiro` + `num_acts` — **não chame modelo** "
    "para gerar/dividir os atos (divisão local); depois aplique operações do lab (ex. stand-up) "
    "se o utilizador pedir tom/estilo; "
    "gerador/editor de imagem → `qclaw_chat_widget_image_generate` / `_image_editor`; "
    "thumbnails → `qclaw_chat_widget_thumbnail_editor`; storyboard → "
    "`qclaw_chat_widget_storyboard_editor`; HQ/saga (Saga→Histórias→Páginas→Painéis) → "
    "`qclaw_chat_widget_comic_editor` (skill `qclaw-chat-roteiro-hq-comics`, card nativo + RV /comic); "
    "personagens de HQ → `qclaw_chat_widget_character_editor` (skill `qclaw-chat-roteiro-hq-comics`, "
    "/comic/characters); campanha visual combinada (HQ + thumbnails + código) → "
    "`qclaw_chat_widget_visual_advanced` (skill `qclaw-chat-roteiro-visual-avancado`, "
    "abas comic/characters/thumbnail/code); editor só de thumbnail inline → "
    "`qclaw_chat_widget_thumbnail_editor`; cursos → `qclaw_roteiro_course_generate` (headless, sem UI) ou "
    "`qclaw_chat_widget_course_editor` (UI iframe, skill `qclaw-chat-roteiro-cursos`, abas code/slides/Gamma) ou "
    "`qclaw_chat_widget_course_manager` (biblioteca / gerenciador em /courses); projetos de pesquisa → "
    "`qclaw_chat_widget_research_project_editor` (skill `qclaw-chat-roteiro-pesquisa`); "
    "ideias de pesquisa → `qclaw_chat_widget_research_ideas_editor` "
    "(skill `qclaw-chat-roteiro-ideias-pesquisa`); livros → "
    "`qclaw_roteiro_book_sync` (inline/Hermes, sem worker) ou `qclaw_roteiro_book_generate` "
    "(CLI headless) ou `qclaw_chat_widget_book_editor` (UI iframe, skill "
    "`qclaw-chat-roteiro-livros`, abas code/design/EPUB); "
    "Word → `qclaw_chat_widget_word_editor`; LaTeX → `qclaw_chat_widget_latex_editor`; "
    "templates → `qclaw_chat_widget_template_editor` "
    "(não use `qclaw_overleaf_template_list` para abrir editores). "
    "Atalho no chat: «abrir editor latex». "
    "**Por defeito** `qclaw_chat_widget_rv_screen` abre widget nativo inline quando existe "
    "(ex.: /script-lab → editor de atos); `force_iframe=true` abre iframe RV (`?embed=1&qclaw=1`). "
    "App completa: path `/` ou «abrir roteiro viral»; botão «Roteiro Viral» na barra lateral do chat.\n"
)

log = logging.getLogger(__name__)



class ChatProgressReporter(Protocol):
    def __call__(self, message: str) -> None: ...


def _format_tool_progress(tool_name: str, args: dict[str, Any]) -> str:
    name = (tool_name or "").strip()
    if name in _SHELL_TOOL_NAMES:
        cmd = str(
            args.get("command") or args.get("cmd") or args.get("script") or ""
        ).strip()
        if len(cmd) > 120:
            cmd = cmd[:117] + "…"
        return f"A executar comando: {cmd or '(shell)'}"
    if name == "ask_qclaw_agent":
        q = str(args.get("question") or "").strip()
        if len(q) > 100:
            q = q[:97] + "…"
        return f"A consultar o agente QClaw: {q or '…'}"
    if name == "qclaw_heygen_via_openclaw":
        task = str(args.get("task") or "").strip()
        if len(task) > 100:
            task = task[:97] + "…"
        return f"A gerar vídeo HeyGen (MCP): {task or '…'}"
    if name == "qclaw_suno_via_openclaw":
        task = str(args.get("task") or "").strip()
        if len(task) > 100:
            task = task[:97] + "…"
        return f"A gerar música Suno (MCP): {task or '…'}"
    if name == "qclaw_seedance_via_openclaw":
        task = str(args.get("task") or "").strip()
        if len(task) > 100:
            task = task[:97] + "…"
        return f"A gerar vídeo Seedance (MCP): {task or '…'}"
    if name == "qclaw_runway_via_openclaw":
        task = str(args.get("task") or "").strip()
        if len(task) > 100:
            task = task[:97] + "…"
        return f"A gerar vídeo Runway (MCP): {task or '…'}"

    # --- Search tools: show *what* is being searched, not just the tool name.
    # Covers built-in OpenClaw/DeepSeek tools and the RuFlo MCP search family.
    name_lc = name.lower()
    if name_lc in {
        "web_search",
        "x_search",
        "memory_search",
        "memory_search_unified",
        "memory_get",
        "embeddings_search",
        "embeddings_rabitq_search",
        "hooks_intelligence_pattern-search",
        "agentdb_pattern-search",
        "agentdb_graph-query",
        "agentdb_hierarchical-recall",
        "agentdb_semantic-route",
    }:
        q = str(
            args.get("query")
            or args.get("q")
            or args.get("text")
            or args.get("prompt")
            or args.get("pattern")
            or ""
        ).strip()
        ns = str(args.get("namespace") or args.get("scope") or "").strip()
        limit = args.get("limit") or args.get("topK") or args.get("k")
        scope = {
            "web_search": "na web",
            "x_search": "no X (Twitter)",
            "memory_search": "na memória vetorial",
            "memory_search_unified": "em todas as memórias (unified)",
            "memory_get": "na memória vetorial",
            "embeddings_search": "nos embeddings",
            "embeddings_rabitq_search": "nos embeddings (RaBitQ)",
            "hooks_intelligence_pattern-search": "nos padrões aprendidos",
            "agentdb_pattern-search": "nos padrões do AgentDB",
            "agentdb_graph-query": "no grafo do AgentDB",
            "agentdb_hierarchical-recall": "na memória hierárquica",
            "agentdb_semantic-route": "no roteamento semântico",
        }.get(name_lc, "no sistema")
        if len(q) > 120:
            q = q[:117] + "…"
        bits = [f"A pesquisar {scope}"]
        if q:
            bits.append(f"“{q}”")
        if ns:
            bits.append(f"(namespace: {ns})")
        if limit:
            bits.append(f"[top {limit}]")
        return " ".join(bits) + "…"
    if name_lc in {"grep", "qclaw_grep", "grep_search", "file_grep", "ripgrep"}:
        pat = str(args.get("pattern") or args.get("query") or args.get("regex") or "").strip()
        path = str(args.get("path") or args.get("dir") or args.get("cwd") or "").strip()
        if len(pat) > 100:
            pat = pat[:97] + "…"
        tail = f" em {path}" if path else " no projeto"
        return f"A varrer ficheiros (grep) por “{pat}”{tail}…" if pat else f"A varrer ficheiros (grep){tail}…"
    if name_lc in {"glob", "file_search", "qclaw_glob", "find", "qclaw_find"}:
        pat = str(args.get("pattern") or args.get("glob") or args.get("query") or "").strip()
        path = str(args.get("path") or args.get("dir") or args.get("cwd") or "").strip()
        if len(pat) > 100:
            pat = pat[:97] + "…"
        tail = f" em {path}" if path else " no projeto"
        return f"A listar ficheiros (glob) “{pat}”{tail}…" if pat else f"A listar ficheiros (glob){tail}…"
    if name_lc in {"read_file", "qclaw_read_file"}:
        path = str(args.get("path") or args.get("file") or args.get("filepath") or "").strip()
        if path and len(path) > 120:
            path = "…" + path[-117:]
        return f"A ler ficheiro: {path or '?'}…"
    if name_lc in {"web_fetch", "fetch", "http_get", "browser_open", "browser_snapshot"}:
        url = str(args.get("url") or args.get("href") or "").strip()
        if len(url) > 120:
            url = url[:117] + "…"
        return f"A obter URL: {url or '…'}"
    if name_lc.startswith("hooks_route") or name_lc == "hooks_intelligence":
        task = str(args.get("task") or args.get("input") or args.get("prompt") or "").strip()
        if len(task) > 100:
            task = task[:97] + "…"
        return f"A rotear tarefa (hooks): {task or '…'}"

    if name.startswith("qclaw_cards_"):
        mcp_tool = name[len("qclaw_cards_") :]
        pretty = mcp_tool.replace("_", " ")
        team = str(args.get("team_id") or "").strip()
        if team:
            return f"A executar MCP qclaw-cards ({pretty}) — time {team}…"
        return f"A executar MCP qclaw-cards ({pretty})…"

    labels = {
        "qclaw_health_check": "A verificar saúde do QClaw…",
        "qclaw_list_teams": "A listar times…",
        "qclaw_process_status": "A verificar processos…",
        "qclaw_read_log_tail": "A ler logs…",
        "qclaw_monitor_snapshot": "A recolher estado do monitor…",
        "qclaw_monitor_update": "A atualizar git / reiniciar monitor…",
        "qclaw_kanban_ide_handoff": "A preparar handoff do card para a IDE…",
        "qclaw_team_files_search": "A procurar arquivos nas pastas do time…",
        "qclaw_team_files_download": "A preparar download do arquivo do time…",
        "qclaw_team_files_send": "A enviar arquivo(s) do time…",
        "qclaw_legal_pdf_extract": "A extrair texto do documento jurídico…",
        "qclaw_legal_normas_list": "A listar normas do time…",
        "qclaw_legal_norma_extract": "A extrair norma do time…",
        "qclaw_budget_get": "A ler perfil financeiro…",
        "qclaw_budget_save": "A guardar dados do orçamento…",
        "qclaw_budget_summary": "A calcular indicadores financeiros…",
        "qclaw_budget_analyze_csv": "A analisar extrato CSV…",
        "qclaw_team_payment_save": "A guardar pagamento do time…",
        "qclaw_team_payment_get": "A consultar pagamento…",
        "qclaw_team_payment_list": "A listar pagamentos do time…",
        "qclaw_team_payment_search": "A buscar pagamentos…",
        "qclaw_team_payment_delete": "A cancelar pagamento…",
        "qclaw_team_payments_summary": "A calcular resumo financeiro do time…",
        "qclaw_errors_to_cards": "A transformar erros em cards…",
        "qclaw_chat_generation_status": "A consultar gerações de imagens em curso…",
        "qclaw_slides_batch_artifacts": "A procurar slides/ZIP gerados em disco…",
        "qclaw_chat_failures": "A listar falhas recentes do chat…",
        "qclaw_team_kanban": "A consultar Kanban do time…",
        "qclaw_kanban_requirements": "A levantar requisitos e perguntas ao time…",
        "qclaw_hermes_cron_health": "A consultar crons Hermes…",
        "qclaw_list_chat_models": "A listar modelos do chat…",
        "qclaw_model_quotas": "A consultar cotas dos modelos…",
        "qclaw_nano_banana_generate": "A gerar imagem (Nano Banana)…",
        "qclaw_grok_imagine_generate": "A gerar imagem (Grok Imagine)…",
        "qclaw_imagen_generate": "A gerar imagem (Imagen 4)…",
        "qclaw_openrouter_image_generate": "A gerar imagem (OpenRouter)…",
        "qclaw_replicate_generate": "A gerar mídia (Replicate)…",
        "qclaw_show_media": "A exibir mídia no chat…",
        "qclaw_show_slides_pdf": "A renderizar páginas do PDF/slides…",
        "qclaw_slides_corner_decor": "A decorar cantos dos slides…",
        "qclaw_slides_replace_didactic": "A substituir slide por imagem didática…",
        "qclaw_slides_batch_images": "A gerar slides em batch e empacotar ZIP…",
        "qclaw_slides_narration": "A gerar narração por slide e empacotar ZIP…",
        "qclaw_elevenlabs_narrate": "A sintetizar narração com ElevenLabs…",
        "qclaw_gemini_music_generate": "A gerar música com Gemini Lyria…",
        "qclaw_gemini_computer_use": "A executar agente Gemini Computer Use…",
        "qclaw_curso_notebooks_docker": "A gerar notebooks Jupyter e Docker Compose…",
        "qclaw_roteiro_book_generate": "A gerar livro (capítulos, seções, capa, imagens)…",
        "qclaw_roteiro_book_latex": "A exportar livro para LaTeX…",
        "qclaw_roteiro_course_generate": "A criar curso (headless, sem UI)…",
        "qclaw_modulo_portal": "A montar módulo com mídia e HTML para portal…",
        "qclaw_wacli_auth_qr": "A gerar QR do WhatsApp…",
        "qclaw_wacli_unlock": "A desbloquear wacli…",
        "qclaw_whatsapp_status": "A consultar fila e lock WhatsApp…",
        "qclaw_wacli_group_numbers": "A buscar participantes do grupo…",
        "qclaw_send_whatsapp": "A enfileirar mensagem WhatsApp…",
        "qclaw_send_whatsapp_file": "A enviar arquivo no WhatsApp…",
        "qclaw_team_whatsapp": "A gerir WhatsApp do time…",
        "qclaw_team_whatsapp_individual": "A vincular/desvincular WhatsApp individual…",
        "qclaw_team_git": "A sincronizar repos Git do time…",
        "qclaw_create_hermes_agent": "A criar agente Hermes…",
        "qclaw_create_openai_swarm": "A criar swarm de agentes…",
        "qclaw_run_team_swarm": "A executar swarm do time…",
        "qclaw_run_swarm": "A executar swarm…",
        "qclaw_swarm_list": "A listar swarms Hermes…",
        "qclaw_swarm_get": "A carregar definição do swarm…",
        "qclaw_swarm_create": "A criar definição de swarm…",
        "qclaw_swarm_update": "A editar swarm…",
        "qclaw_swarm_delete": "A excluir swarm…",
        "qclaw_swarm_health": "A auditar saúde do swarm…",
        "qclaw_swarm_topology": "A gerar diagrama do swarm…",
        "qclaw_swarm_design": "A montar design do swarm…",
        "qclaw_swarm_set_model": "A atualizar modelo LLM do swarm…",
        "qclaw_jobs_health": "A consultar saúde dos jobs…",
        "qclaw_token_mode": "A consultar modo de consumo de tokens…",
        "qclaw_jobs_list_running": "A listar jobs em execução…",
        "qclaw_monitor_update": "A atualizar git / reiniciar monitor…",
        "qclaw_kanban_ide_handoff": "A preparar handoff do card para a IDE…",
        "qclaw_jobs_get_running": "A consultar job em execução…",
        "qclaw_jobs_cancel": "A cancelar job em execução…",
        "qclaw_jobs_cancel_all_batches": "A cancelar batches em execução…",
        "qclaw_jobs_cron_list": "A listar agenda cron…",
        "qclaw_jobs_cron_get": "A carregar cron agendado…",
        "qclaw_jobs_cron_action": "A aplicar ação no cron…",
        "qclaw_jobs_cron_create": "A agendar novo cron…",
        "qclaw_jobs_cron_edit": "A editar cron agendado…",
        "qclaw_aws_overview": "A consultar visão geral AWS…",
        "qclaw_aws_cost": "A consultar custos AWS…",
        "qclaw_aws_ce_get_cost_and_usage": "A consultar Cost Explorer (get-cost-and-usage)…",
        "qclaw_aws_machines": "A listar máquinas AWS…",
        "qclaw_aws_waste": "A procurar desperdícios AWS…",
        "qclaw_aws_env": "A consultar snapshots AWS…",
        "qclaw_team_files_search": "A procurar arquivos nas pastas do time…",
        "qclaw_team_files_download": "A preparar download do arquivo do time…",
        "qclaw_team_files_send": "A enviar arquivo(s) do time…",
        "qclaw_evaluate_agents": "A avaliar agentes criados…",
        "qclaw_fix_agents": "A consertar agentes criados…",
        "qclaw_create_team": "A criar time…",
        "qclaw_create_kanban_card": "A criar cartão no Kanban…",
        "qclaw_create_card_with_article": "A criar card e anexar a versão do artigo…",
        "qclaw_team_contacts": "A gerir contatos do time…",
        "qclaw_kanban_list_attachments": "A listar anexos do card…",
        "qclaw_kanban_attach_upload": "A anexar arquivo ao card…",
        "qclaw_kanban_attach_move": "A mover anexo entre cards…",
        "qclaw_kanban_attach_copy": "A copiar anexo entre cards…",
        "qclaw_kanban_attach_project_zip": "A compactar projeto e anexar ao card…",
        "qclaw_kanban_attach_latex_zip": "A reunir fontes LaTeX e anexar ao card…",
        "qclaw_desktop_screenshot": "A capturar ecrã…",
        "qclaw_desktop_click": "A clicar no ecrã…",
        "qclaw_desktop_type": "A escrever texto…",
        "qclaw_desktop_key": "A premir teclas…",
        "qclaw_desktop_scroll": "A fazer scroll…",
        "qclaw_desktop_list_windows": "A listar janelas…",
        "qclaw_desktop_open_app": "A abrir aplicação…",
        "qclaw_desktop_screen_info": "A ler informação do ecrã…",
        "qclaw_heygen_via_openclaw": "A usar HeyGen via OpenClaw…",
        "qclaw_suno_via_openclaw": "A usar Suno via OpenClaw…",
        "qclaw_seedance_via_openclaw": "A usar Seedance via OpenClaw…",
        "qclaw_runway_via_openclaw": "A usar Runway via OpenClaw…",
        "qclaw_photo_to_video_plan": "A planear foto→vídeo…",
        "qclaw_virtual_band_create": "A criar banda virtual (Roteiro Viral)…",
        "qclaw_virtual_band_portrait": "A gerar retrato de integrante…",
        "qclaw_virtual_band_job_status": "A consultar job de banda virtual…",
        "qclaw_virtual_band_list_bands": "A listar bandas virtuais…",
        "qclaw_virtual_band_suggest_members": "A sugerir integrantes da banda…",
        "qclaw_rv_mongo_collections": "A listar coleções MongoDB do Roteiro Viral…",
        "qclaw_rv_mongo_find": "A consultar MongoDB do Roteiro Viral…",
        "qclaw_rv_mongo_count": "A contar documentos no MongoDB RV…",
        "qclaw_rv_scripts_list": "A listar roteiros gerados no MongoDB RV…",
        "qclaw_rv_scripts_get": "A obter roteiro do MongoDB RV…",
        "qclaw_rv_scripts_acts": "A obter atos do roteiro no MongoDB RV…",
        "qclaw_rv_scripts_act": "A obter ato do roteiro no MongoDB RV…",
        "qclaw_rv_scripts_insert": "A inserir roteiro no MongoDB RV…",
        "qclaw_rv_scripts_update": "A atualizar roteiro no MongoDB RV…",
        "qclaw_rv_scripts_update_act": "A atualizar ato do roteiro no MongoDB RV…",
        "qclaw_rv_scripts_insert_acts": "A inserir atos do roteiro no MongoDB RV…",
        "qclaw_rv_api_health": "A verificar API Roteiro Viral local…",
        "qclaw_rv_api_get": "A consultar API Roteiro Viral local…",
        "qclaw_rv_api_post": "A enviar pedido à API Roteiro Viral local…",
        "qclaw_rv_api_patch": "A atualizar recurso na API Roteiro Viral…",
        "qclaw_rv_api_delete": "A remover recurso na API Roteiro Viral…",
        "qclaw_rv_job_status": "A consultar status do job RV…",
        "qclaw_rv_jobs_list": "A listar jobs do Roteiro Viral…",
        "qclaw_roteiro_book_cover_portrait": "A gerar capa de livro em retrato (2:3)…",
        "qclaw_book_cover_studio_brief": "A preparar briefing editorial da capa…",
        "qclaw_book_cover_studio_concepts": "A gerar variações visuais da capa…",
        "qclaw_book_cover_studio_review": "A rever capa (legibilidade e género)…",
        "qclaw_book_cover_studio_export": "A calcular specs de export da capa…",
        "qclaw_book_cover_studio_pipeline": "A correr estúdio de capas (pipeline completo)…",
        "qclaw_chapter_image_epub": "A inserir imagem no capítulo e gerar EPUB…",
        "ask_qclaw_agent": "A consultar agente QClaw…",
        "qclaw_save_project_note": "A guardar nota do projeto…",
        "qclaw_run_shell": "A executar comando shell…",
        "qclaw_allowlist_list": "A listar allowlist WhatsApp…",
        "qclaw_allowlist_add": "A adicionar à allowlist…",
        "qclaw_allowlist_remove": "A remover da allowlist…",
        "qclaw_allowlist_toggle": "A alterar estado na allowlist…",
        "qclaw_email_memoria_index": "A indexar emails do time no MongoDB…",
        "qclaw_email_memoria_search": "A buscar emails no MongoDB…",
        "qclaw_email_memoria_list": "A listar emails do membro…",
        "qclaw_email_memoria_summary": "A resumir emails por squad…",
        "qclaw_email_team_pdf_list": "A listar PDFs nos emails…",
        "qclaw_email_team_pdf_save": "A guardar PDFs do email no time…",
        "qclaw_gmail_attachments_list": "A procurar anexos nos emails…",
        "qclaw_gmail_attachments_download": "A baixar anexos do Gmail…",
        "qclaw_trello_connect": "A ligar ao Trello…",
        "qclaw_trello_boards": "A listar boards Trello…",
        "qclaw_trello_board_detail": "A carregar board Trello…",
        "qclaw_trello_card_create": "A criar card no Trello…",
        "qclaw_trello_card_move": "A mover card no Trello…",
        "qclaw_trello_kanban_sync": "A sincronizar Trello com Kanban…",
        "qclaw_ai_kb_store": "A guardar conhecimento de IA no banco…",
        "qclaw_ai_kb_search": "A buscar no banco de conhecimento de IA…",
        "qclaw_ai_kb_list": "A listar entradas do banco de IA…",
        "qclaw_ai_kb_summary": "A resumir banco de conhecimento de IA…",
        "qclaw_ruflo_swarm_architecture": "A mapear arquitetura Ruflo (MCP, chat, scripts)…",
        "qclaw_ruflo_swarm_audit": "A auditar scripts de enxame Ruflo…",
        "qclaw_ruflo_swarm_validate": "A validar script bash Ruflo…",
        "qclaw_ruflo_swarm_plan": "A planear enxame Ruflo (topologia e agentes)…",
        "qclaw_ruflo_swarm_scaffold": "A gerar script bash de enxame Ruflo…",
        "qclaw_aula_store": "A guardar aula no banco de memória…",
        "qclaw_aula_search": "A buscar aulas no banco…",
        "qclaw_aula_list": "A listar aulas…",
        "qclaw_nota_store": "A guardar nota/anotação no banco…",
        "qclaw_nota_search": "A buscar notas no banco…",
        "qclaw_nota_list": "A listar notas…",
        "qclaw_study_skill_store": "A guardar competência de estudo…",
        "qclaw_study_skill_search": "A buscar skills no banco…",
        "qclaw_study_skill_list": "A listar competências…",
        "qclaw_aulas_memoria_summary": "A resumir memória de aulas e notas…",
        "qclaw_aulas_memoria_show": "A montar painel completo da memória acadêmica…",
        "qclaw_memclaw_memoria_summary": "A consultar stats do MemClaw…",
        "qclaw_memclaw_memoria_show": "A montar painel da memória MemClaw…",
        "qclaw_memclaw_memoria_search": "A buscar memórias no MemClaw…",
        "qclaw_whatsapp_memoria_index": "A indexar mensagens WhatsApp no banco…",
        "qclaw_whatsapp_memoria_search": "A buscar mensagens WhatsApp no banco…",
        "qclaw_whatsapp_memoria_list": "A listar mensagens WhatsApp do banco…",
        "qclaw_whatsapp_memoria_summary": "A resumir mensagens por grupo…",
        "qclaw_tool_learning_record": "A registar lição aprendida da ferramenta…",
        "qclaw_tool_learning_search": "A buscar correções aprendidas…",
        "qclaw_tool_learning_list": "A listar lições de ferramentas…",
        "qclaw_tool_learning_stats": "A consultar estatísticas de autoaprimoramento…",
        "qclaw_chat_personality_get": "A ler perfil de personalidade do usuário…",
        "qclaw_chat_personality_samples": "A coletar mensagens do usuário para análise…",
        "qclaw_chat_personality_save": "A guardar perfil de personalidade…",
        "qclaw_chat_personality_deactivate": "A desativar personalização do chat…",
        "qclaw_chat_personality_summary": "A resumir perfis de personalidade…",
        "qclaw_visual_memory_list": "A listar perfis visuais…",
        "qclaw_visual_memory_get": "A ler memória visual…",
        "qclaw_visual_memory_save": "A guardar perfil visual…",
        "qclaw_visual_memory_add_photo": "A registar foto de referência…",
        "qclaw_visual_memory_merge_photos": "A fundir características de todas as fotos…",
        "qclaw_visual_memory_lora_train": "A treinar LoRA Flux no Replicate…",
        "qclaw_visual_memory_lora_status": "A consultar estado do LoRA…",
        "qclaw_visual_memory_lora_evaluate": "A avaliar qualidade do LoRA…",
        "qclaw_visual_memory_remove_photo": "A remover foto da memória visual…",
        "qclaw_visual_memory_set_primary": "A definir foto principal…",
        "qclaw_visual_memory_resolve": "A resolver foto/persona para thumbnail…",
        "qclaw_visual_memory_deactivate": "A desativar perfil visual…",
        "qclaw_visual_memory_summary": "A resumir memória visual…",
        "qclaw_profile_from_photo": "A analisar foto e guardar aparência…",
        "qclaw_character_search": "A buscar personagens…",
        "qclaw_character_preview": "A carregar ficha do personagem…",
        "qclaw_character_psych_save": "A guardar perfil psicológico…",
        "qclaw_thumbnail_style_list": "A listar estilos de thumbnail guardados…",
        "qclaw_thumbnail_style_get": "A ler preset de estilo de thumbnail…",
        "qclaw_thumbnail_style_save": "A guardar estilo de thumbnail…",
        "qclaw_thumbnail_style_capture": "A capturar estilo de thumbnail…",
        "qclaw_thumbnail_style_resolve": "A resolver preset de estilo…",
        "qclaw_thumbnail_style_deactivate": "A desativar preset de estilo…",
        "qclaw_thumbnail_style_summary": "A resumir presets de estilo…",
        "qclaw_user_data_get": "A ler dados pessoais do usuário…",
        "qclaw_user_data_save": "A guardar dados pessoais…",
        "qclaw_user_data_delete_field": "A remover campo de dados pessoais…",
        "qclaw_user_data_search": "A buscar perfis de dados pessoais…",
        "qclaw_user_data_export": "A exportar dados pessoais…",
        "qclaw_user_data_summary": "A resumir dados pessoais guardados…",
        "qclaw_user_data_preview": "A montar preview dos dados pessoais…",
        "qclaw_identidade_get": "A ler identidade civil…",
        "qclaw_identidade_save": "A guardar identidade civil…",
        "qclaw_identidade_delete": "A remover campo de identidade civil…",
        "qclaw_curriculum_list": "A listar documentos do cofre…",
        "qclaw_curriculum_register": "A registrar documento no cofre…",
        "qclaw_curriculum_extract": "A extrair texto do documento…",
        "qclaw_curriculum_generate": "A gerar currículo…",
        "qclaw_curriculum_send": "A enviar documento no WhatsApp…",
        "qclaw_minicurriculum_generate": "A gerar minicurrículo…",
        "qclaw_minicurriculum_preview": "A montar preview do minicurrículo…",
        "qclaw_minicurriculum_send": "A enviar minicurrículo no WhatsApp…",
        "qclaw_whatsapp_contacts_search": "A procurar contato WhatsApp (wacli)…",
        "qclaw_whatsapp_agenda_sync": "A sincronizar agenda WhatsApp…",
        "qclaw_whatsapp_agenda_list": "A listar contatos WhatsApp…",
        "qclaw_whatsapp_agenda_stats": "A consultar estatísticas da agenda…",
        "qclaw_whatsapp_messages_search": "A buscar mensagens WhatsApp (wacli)…",
        "qclaw_whatsapp_media_download": "A baixar anexo WhatsApp (wacli)…",
        "qclaw_team_articles": "A buscar artigos do time…",
        "qclaw_article_images": "A processar figuras do artigo…",
        "qclaw_team_article_pdf": "A compilar PDF e salvar na pasta do time…",
        "qclaw_index_team_articles": "A indexar artigos do workspace no time…",
        "qclaw_remove_team_articles": "A desvincular artigos do time…",
        "qclaw_gmail_send": "A enviar email via Gmail…",
        "qclaw_gmail_list": "A listar emails da inbox…",
        "qclaw_gmail_read": "A ler email completo…",
        "qclaw_app_passwords_list": "A listar senhas de app…",
        "qclaw_app_passwords_store": "A gravar senha de app no MongoDB…",
        "qclaw_app_passwords_delete": "A remover senha de app…",
        "qclaw_google_oauth_status": "A verificar OAuth Google…",
        "qclaw_google_oauth_list": "A listar tokens OAuth Google…",
        "qclaw_google_oauth_upload": "A enviar OAuth Google para o MongoDB…",
        "qclaw_google_oauth_download": "A baixar OAuth Google do MongoDB…",
        "qclaw_google_oauth_migrate": "A importar OAuth Google para o MongoDB…",
        "qclaw_calendar_sync": "A sincronizar Google Calendar…",
        "qclaw_calendar_today": "A consultar agenda de hoje…",
        "qclaw_calendar_week": "A consultar agenda da semana…",
        "qclaw_calendar_search": "A buscar eventos no calendário…",
        "qclaw_calendar_create": "A criar evento no calendário…",
        "qclaw_calendar_meet_create": "A agendar reunião com Google Meet…",
        "qclaw_google_flights_search": "A preparar busca no Google Flights…",
        "qclaw_flights_search": "A buscar voos em múltiplas APIs…",
        "qclaw_flights_providers_status": "A verificar APIs de voo…",
        "qclaw_roteiro_scene_setup": "A montar plano de cenas do roteiro…",
        "qclaw_youtube_metadata_generate": "A gerar metadados YouTube…",
        "qclaw_calendar_delete": "A remover evento do calendário…",
        "qclaw_calendar_update": "A atualizar evento no calendário…",
        "qclaw_calendar_status": "A verificar status do calendário…",
        "qclaw_calendar_notion_sync": "A sincronizar calendário com Notion…",
        "qclaw_calendar_notion_status": "A verificar status Notion…",
        "qclaw_calendar_notion_configure": "A configurar database Notion…",
        "qclaw_teams_calendar_sync": "A sincronizar calendário Teams/Outlook…",
        "qclaw_teams_calendar_today": "A consultar agenda Teams de hoje…",
        "qclaw_teams_calendar_week": "A consultar agenda Teams da semana…",
        "qclaw_teams_calendar_search": "A buscar eventos no calendário Teams…",
        "qclaw_teams_calendar_status": "A verificar status do calendário Teams…",
        "qclaw_notion_status": "A verificar integração Notion…",
        "qclaw_notion_configure": "A configurar Notion…",
        "qclaw_notion_search": "A buscar no Notion…",
        "qclaw_notion_query": "A consultar database Notion…",
        "qclaw_notion_get_page": "A ler página Notion…",
        "qclaw_notion_get_database": "A ler schema do database Notion…",
        "qclaw_notion_create_row": "A criar linha no Notion…",
        "qclaw_notion_create_page": "A criar página no Notion…",
        "qclaw_notion_update_page": "A atualizar página Notion…",
        "qclaw_overleaf_template_list": "A listar templates LaTeX…",
        "qclaw_overleaf_template_get": "A carregar template LaTeX…",
        "qclaw_overleaf_template_sync": "A sincronizar catálogo de templates LaTeX…",
        "qclaw_overleaf_template_extract": "A extrair template do Overleaf…",
        "qclaw_overleaf_template_apply": "A abrir catálogo de templates LaTeX…",
        "qclaw_overleaf_template_preview": "A gerar prévia do template LaTeX…",
        "qclaw_overleaf_template_discover": "A procurar templates oficiais LaTeX…",
        "qclaw_overleaf_template_fetch": "A baixar template LaTeX…",
        "qclaw_overleaf_template_catalog_search": "A buscar metadados de templates LaTeX…",
        "qclaw_overleaf_template_catalog_get": "A carregar metadados do template…",
        "qclaw_overleaf_template_catalog_sync": "A sincronizar catálogo MongoDB de templates…",
        "qclaw_overleaf_template_pipeline": "A executar pipeline de template LaTeX…",
        "qclaw_google_photos_picker_start": "A abrir seletor Google Fotos…",
        "qclaw_google_photos_picker_poll": "A aguardar seleção no Google Fotos…",
        "qclaw_google_photos_picker_list": "A listar fotos escolhidas…",
        "qclaw_google_photos_sync": "A iniciar seletor Google Fotos…",
        "qclaw_google_photos_recent": "A listar fotos escolhidas…",
        "qclaw_google_photos_get": "A carregar foto do Google Fotos…",
        "qclaw_google_photos_status": "A verificar status do Google Fotos…",
        "qclaw_local_photos_search": "A buscar fotos locais…",
        "qclaw_local_photos_recent": "A listar fotos recentes no Mac…",
        "qclaw_local_photos_get": "A carregar foto local…",
        "qclaw_local_photos_roots": "A listar pastas de fotos…",
        "qclaw_skills_list": "A listar skills disponíveis…",
        "qclaw_skills_get": "A carregar SKILL.md…",
        "qclaw_skills_search": "A buscar skills…",
        "qclaw_skills_run": "A executar script da skill…",
        "qclaw_skills_tools_for_skill": "A mapear ferramentas da skill…",
    }
    return labels.get(name, f"A executar {name or 'ferramenta'}…")


def _notify_tool_availability(
    remaining: list[ToolRun],
    *,
    finished_session: str,
    finished_label: str,
    persistence: Optional[ChatPersistence],
    on_progress: Optional[ChatProgressReporter],
) -> None:
    msg = format_availability_message(remaining, finished_label=finished_label)
    targets: set[str] = set()
    if finished_session:
        targets.add(finished_session)
    for job in list_running_jobs():
        sk = (job.session_key or "").strip()
        if sk and sk != finished_session:
            targets.add(sk)
    for sk in targets:
        publish_chat_status(
            msg,
            persistence=persistence,
            session_key=sk,
            on_progress=on_progress if sk == finished_session else None,
        )


def _public_chat_messages(messages: list[dict]) -> list[dict]:
    """Drop internal progress rows; reasoning is shown in the chat bubble."""
    return [m for m in messages if m.get("role") != "status"]


def publish_chat_status(
    message: str,
    *,
    persistence: Optional[ChatPersistence] = None,
    session_key: str = "",
    on_progress: Optional[ChatProgressReporter] = None,
    persist: bool = False,
) -> None:
    text = (message or "").strip()
    if not text:
        return
    if on_progress is not None:
        try:
            on_progress(text)
        except Exception:
            log.debug("publish_chat_status: on_progress callback failed", exc_info=True)
    if persist and persistence is not None and session_key:
        try:
            persistence.history.append_message(session_key, role="status", text=text)
        except Exception as e:
            log.warning("could not append chat status for %s: %s", session_key, e)


def _record_llm_call_attempt(
    progress_job_id: Optional[str],
    *,
    model_id: str,
    model_label: str = "",
    provider: str = "",
    ok: bool = True,
    error: str = "",
) -> None:
    """Record one LLM API call on the running chat job (live model stats)."""
    if not progress_job_id:
        return
    mid = str(model_id or "").strip()
    if not mid or mid == "none":
        return
    from .chat_jobs import record_model_attempt

    record_model_attempt(
        progress_job_id,
        model_id=mid,
        model_label=str(model_label or mid).strip(),
        provider=str(provider or "").strip(),
        ok=bool(ok),
        error=str(error or "")[:300],
        kind="llm",
    )


def _track_chat_model_attempt(
    progress_job_id: Optional[str],
    out: dict[str, Any],
    *,
    model_id: str = "",
    model_label: str = "",
    provider: str = "",
) -> None:
    if not progress_job_id:
        return
    mid = str(out.get("modelId") or model_id or "").strip()
    if not mid or mid == "none":
        return
    _record_llm_call_attempt(
        progress_job_id,
        model_id=mid,
        model_label=str(out.get("modelLabel") or model_label or mid),
        provider=str(out.get("provider") or provider or ""),
        ok=bool(out.get("ok")),
        error=str(out.get("error") or "")[:300],
    )


def _progress_heartbeat(
    label: str,
    *,
    model_label: str = "",
    persistence: Optional[ChatPersistence] = None,
    session_key: str = "",
    on_progress: Optional[ChatProgressReporter] = None,
    interval_seconds: float = 8.0,
) -> ProgressHeartbeat:
    tick = {"n": 0}

    def on_tick(msg: str) -> None:
        tick["n"] += 1
        # Job bar updates every tick; chat log status lines throttled.
        publish_chat_status(
            msg,
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
            persist=tick["n"] == 1 or tick["n"] % 3 == 0,
        )

    return ProgressHeartbeat(
        interval_seconds=interval_seconds,
        label=label,
        model_label=model_label,
        on_update=on_tick,
    )






def _audio_transcription_model() -> str:
    model = (
        os.environ.get("QCLAW_AUDIO_TRANSCRIPTION_MODEL")
        or os.environ.get("OPENAI_AUDIO_TRANSCRIPTION_MODEL")
        or "whisper-1"
    ).strip()
    return model or "whisper-1"


def _audio_transcription_timeout(default_timeout: float) -> float:
    raw = os.environ.get("QCLAW_AUDIO_TRANSCRIPTION_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(30.0, float(raw))
        except (TypeError, ValueError):
            pass
    return max(30.0, min(float(default_timeout), 300.0))


def _transcript_attachment_from_audio(
    att: ParsedAttachment,
    transcript: str,
) -> ParsedAttachment:
    stem = Path(att.name).stem or "audio"
    text = transcript.strip()
    data = text.encode("utf-8")
    data_url = "data:text/plain;base64," + base64.b64encode(data).decode("ascii")
    return ParsedAttachment(
        name=f"{stem}.transcricao.txt",
        mime_type="text/plain",
        data=data,
        data_url=data_url,
    )


def _pdf_text_attachment_from(att: ParsedAttachment, text: str) -> ParsedAttachment:
    stem = Path(att.name).stem or "documento"
    data = text.encode("utf-8")
    data_url = "data:text/plain;base64," + base64.b64encode(data).decode("ascii")
    return ParsedAttachment(
        name=f"{stem}.texto.txt",
        mime_type="text/plain",
        data=data,
        data_url=data_url,
        path=att.path,
    )


def _normalize_chat_attachments(
    attachments: list[ParsedAttachment],
    *,
    timeout: float,
) -> tuple[list[ParsedAttachment], Optional[str], bool]:
    if not attachments:
        return [], None, False

    has_audio = any(is_audio_attachment(a) for a in attachments)
    api_key: Optional[str] = None
    if has_audio:
        api_key = resolve_llm_api_key("OPENAI_API_KEY")
        if not api_key:
            return [], "OPENAI_API_KEY é obrigatório para transcrever áudio", False

    resolved: list[ParsedAttachment] = []
    transcribed = False
    model = _audio_transcription_model()
    effective_timeout = _audio_transcription_timeout(timeout)
    for att in attachments:
        if is_pdf_attachment(att):
            try:
                text = extract_pdf_text(att.data)
            except Exception as exc:
                log.warning("PDF text extraction failed for %r: %s", att.name, exc)
                resolved.append(att)
                continue
            if text:
                resolved.append(_pdf_text_attachment_from(att, text))
            else:
                resolved.append(att)
            continue
        if not is_audio_attachment(att):
            resolved.append(att)
            continue
        transcribed = True
        try:
            transcript = transcribe_audio_bytes(
                data=att.data,
                name=att.name,
                mime_type=att.mime_type,
                api_key=api_key,  # type: ignore[arg-type]
                model=model,
                timeout=effective_timeout,
            )
        except Exception as exc:
            return [], f"não foi possível transcrever {att.name!r}: {exc}", False
        resolved.append(_transcript_attachment_from_audio(att, transcript))
    return resolved, None, transcribed


def _brief_target_instruction_block(brief_target: Optional[str]) -> str:
    mode = (brief_target or "").strip().lower()
    if not mode or mode == "auto":
        return ""
    common = (
        "Responda em português, de forma objetiva e estruturada. "
        "Não explique o raciocínio passo a passo. "
        "Transforme a transcrição do áudio em um briefing pronto para uso."
    )
    if mode == "agent":
        return (
            "## Saída desejada: briefing para criar um agente\n"
            f"{common}\n"
            "Use os campos:\n"
            "- nome sugerido\n"
            "- papel / especialidade\n"
            "- objetivo\n"
            "- responsabilidades\n"
            "- skills ou capacidades\n"
            "- tom de resposta\n"
            "- critérios de sucesso\n"
            "- prompt-base para colar na criação do agente\n"
        )
    if mode == "kanban":
        return (
            "## Saída desejada: briefing para criar um kanban\n"
            f"{common}\n"
            "Use os campos:\n"
            "- nome do time ou projeto\n"
            "- objetivo do quadro\n"
            "- colunas sugeridas\n"
            "- tarefas iniciais\n"
            "- responsáveis / perfis\n"
            "- skills relevantes\n"
            "- pasta ou repositório alvo\n"
            "- notas operacionais\n"
        )
    if mode == "task":
        return (
            "## Saída desejada: briefing para criar uma task\n"
            f"{common}\n"
            "Use os campos:\n"
            "- título\n"
            "- descrição resumida\n"
            "- prioridade\n"
            "- responsável\n"
            "- lista/coluna\n"
            "- skills\n"
            "- workdir\n"
            "- critérios de aceite\n"
        )
    return ""


def _image_model_instruction_block(image_model: Optional[str]) -> str:
    raw = (image_model or "").strip()
    if not raw or raw.lower() == "auto":
        return ""
    model_id = raw.split(":", 1)[1].strip() if ":" in raw else raw
    if not model_id:
        return ""
    return (
        "## Modelo de imagem preferido (controles do chat)\n\n"
        "O utilizador fixou o modelo de geração de imagem nesta conversa.\n"
        f"- **Modelo obrigatório:** `{model_id}`\n"
        "- Ao gerar imagens (`qclaw_grok_imagine_generate`, `qclaw_imagen_generate`, "
        "`qclaw_openrouter_image_generate`, widgets `image_generate`, figuras LaTeX, "
        "slides em batch), passe `model=\""
        f"{model_id}\"` salvo pedido explícito em contrário.\n"
    )


def build_chat_persistence(chat_cfg: OpenclawChatConfig) -> Optional[ChatPersistence]:
    if not chat_cfg.history_enabled:
        return None
    from .chat_memory import ChatMemoryIndex
    from .storage import get_chat_history_store

    db_path = Path(chat_cfg.history_db_path).expanduser()
    if not db_path.is_absolute():
        db_path = (project_stack_root().parent / db_path).resolve()
    memory = None
    if chat_cfg.chroma_enabled:
        chroma_path = Path(chat_cfg.chroma_path).expanduser()
        if not chroma_path.is_absolute():
            chroma_path = (project_stack_root().parent / chroma_path).resolve()
        memory = ChatMemoryIndex(chroma_path)
    return ChatPersistence(history=get_chat_history_store(db_path), memory=memory)


def build_project_memory_store(
    chat_cfg: OpenclawChatConfig,
) -> Optional[Any]:
    if not chat_cfg.project_memory_enabled:
        return None
    from .project_memory import ProjectMemoryStore, resolve_project_memory_path

    path = resolve_project_memory_path(chat_cfg.project_memory_path)
    store = ProjectMemoryStore(path)
    return store


def _pick_session_title(title_a: object, title_b: object) -> str:
    """Prefer a real title over placeholders like 'Nova conversa 12:34'."""
    a = title_a.strip() if isinstance(title_a, str) else ""
    b = title_b.strip() if isinstance(title_b, str) else ""
    a_ph = _is_placeholder_title(a)
    b_ph = _is_placeholder_title(b)
    if a_ph and not b_ph:
        return b
    if b_ph and not a_ph:
        return a
    return b or a


def _merge_session_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge two session list rows for the same session key."""
    out = dict(existing)
    for field in (
        "sessionId",
        "agentId",
        "kind",
        "lastTo",
        "model",
        "source",
        "team_id",
        "swarm_mode",
    ):
        if field == "swarm_mode":
            if incoming.get(field):
                out[field] = incoming[field]
            continue
        if not out.get(field) and incoming.get(field):
            out[field] = incoming[field]
    try:
        existing_ts = int(out.get("updatedAt") or 0)
    except (TypeError, ValueError):
        existing_ts = 0
    try:
        incoming_ts = int(incoming.get("updatedAt") or 0)
    except (TypeError, ValueError):
        incoming_ts = 0
    if incoming_ts >= existing_ts:
        out["updatedAt"] = incoming_ts
        out["title"] = _pick_session_title(out.get("title"), incoming.get("title"))
    else:
        out["title"] = _pick_session_title(incoming.get("title"), out.get("title"))
    return out


def _merge_session_rows(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in primary + secondary:
        key = str(row.get("key") or "")
        if not key:
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = dict(row)
        else:
            by_key[key] = _merge_session_row(prev, row)
    merged = list(by_key.values())
    merged.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    return merged


def list_sessions(
    runtime: QclawRuntime,
    *,
    limit: int = 80,
    agent_id: Optional[str] = None,
    cache_seconds: float = 15.0,
    persistence: Optional[ChatPersistence] = None,
    user_id: Optional[str] = None,
    skip_cache: bool = False,
) -> dict:
    """List recent OpenClaw sessions across configured agents."""
    agents_root = runtime.state_dir / "agents"
    if not agents_root.is_dir():
        return {
            "ok": False,
            "error": f"agents directory not found: {agents_root}",
        }

    cache_key = f"{agents_root}:{agent_id or '*'}:{limit}:{user_id or ''}"
    now = time.time()
    cached = get_session_list_cache(cache_key)
    if not skip_cache and cached and (now - cached[0]) < cache_seconds:
        sessions = cached[1]
        return {
            "ok": True,
            "sessions": sessions[:limit],
            "count": len(sessions[:limit]),
            "cached": True,
            "control_url": runtime.control_url,
        }

    all_rows: list[dict] = []
    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        aid = agent_dir.name
        if agent_id and aid != agent_id:
            continue
        store_path = agent_dir / "sessions" / "sessions.json"
        if not store_path.is_file():
            continue
        store = _read_sessions_json(store_path)
        if store is None:
            continue
        if not isinstance(store, dict):
            continue
        for row in _summarize_store_entries(store):
            row["agentId"] = aid
            all_rows.append(row)

    all_rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
    if persistence is not None:
        db_rows = persistence.history.list_sessions(
            agent_id=agent_id, user_id=user_id, limit=limit
        )
        all_rows = _merge_session_rows(db_rows, all_rows)
    set_session_list_cache(cache_key, (now, all_rows))

    return {
        "ok": True,
        "sessions": all_rows[:limit],
        "count": len(all_rows[:limit]),
        "total": len(all_rows),
        "control_url": runtime.control_url,
        "history": persistence is not None,
    }


def _thinking_block_text(block: dict) -> str:
    for key in ("thinking", "text", "content"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


_THINKING_TAG_RE = re.compile(
    r"<\s*(?:redacted_)?think(?:ing)?\s*>(.*?)<\s*/\s*(?:redacted_)?think(?:ing)?\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_thinking_tags(text: str) -> str:
    return _THINKING_TAG_RE.sub("", text or "").strip()


def _extract_reasoning_from_completion_message(msg: Any) -> tuple[str, str]:
    """Split model reasoning from visible assistant text in a chat completion message."""
    reasoning_parts: list[str] = []
    content = getattr(msg, "content", None)

    reasoning_content = getattr(msg, "reasoning_content", None)
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        reasoning_parts.append(reasoning_content.strip())

    clean = ""
    if isinstance(content, str):
        clean = content.strip()
    elif isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("thinking", "redacted_thinking"):
                part = _thinking_block_text(block)
                if part:
                    reasoning_parts.append(part)
            elif kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        clean = "\n".join(text_parts)

    if clean:
        for match in _THINKING_TAG_RE.finditer(clean):
            inner = (match.group(1) or "").strip()
            if inner:
                reasoning_parts.append(inner)
        clean = _THINKING_TAG_RE.sub("", clean).strip()

    reasoning = "\n\n".join(p for p in reasoning_parts if p).strip()
    return reasoning, clean


def _publish_model_reasoning_live(
    reasoning: str,
    *,
    progress_job_id: Optional[str],
    persistence: Optional[ChatPersistence],
    session_key: str,
) -> None:
    text = (reasoning or "").strip()
    if not text:
        return
    labeled = f"Pensamento do modelo:\n{text}"
    if progress_job_id:
        from .chat_jobs import append_partial_reasoning

        append_partial_reasoning(progress_job_id, labeled)
    if persistence is not None and session_key:
        try:
            persistence.history.append_message(
                session_key,
                role="reasoning",
                text=labeled,
            )
        except Exception as e:
            log.debug("could not persist live model reasoning for %s: %s", session_key, e)


def _extract_thinking_blocks(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("thinking", "redacted_thinking"):
            part = _thinking_block_text(block)
            if part:
                parts.append(part)
    return "\n\n".join(parts)


def _extract_text_blocks(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        elif kind == "thinking" and block.get("thinking"):
            continue
    return "\n".join(parts)


def _jsonl_line_to_messages(line: str) -> list[dict]:
    line = line.strip()
    if not line:
        return []
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return []
    if row.get("type") != "message":
        return []
    inner = row.get("message")
    if not isinstance(inner, dict):
        return []
    role = inner.get("role")
    ts = row.get("timestamp")
    msg_id = row.get("id")
    out: list[dict] = []
    if role == "user":
        text = _extract_text_blocks(inner)
        if text:
            out.append(
                {
                    "role": "user",
                    "text": text,
                    "timestamp": ts,
                    "id": msg_id,
                }
            )
    elif role == "assistant":
        thinking = _extract_thinking_blocks(inner)
        if thinking:
            out.append(
                {
                    "role": "reasoning",
                    "text": thinking,
                    "source": "model",
                    "timestamp": ts,
                    "id": f"{msg_id}-thinking" if msg_id else None,
                }
            )
        text = _extract_text_blocks(inner)
        if text:
            out.append(
                {
                    "role": "assistant",
                    "text": text,
                    "timestamp": ts,
                    "id": msg_id,
                }
            )
    return out


def _jsonl_line_to_message(line: str) -> Optional[dict]:
    msgs = _jsonl_line_to_messages(line)
    if len(msgs) == 1:
        return msgs[0]
    if len(msgs) == 2 and msgs[0].get("role") == "reasoning":
        return msgs[1]
    return msgs[-1] if msgs else None


_JSONL_TAIL_CHUNK_BYTES = 65536


def _jsonl_transcript_count(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") in ("user", "assistant"))


def _companion_reasoning_before_assistant(messages: list[dict]) -> list[dict]:
    last_ai = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_ai = i
            break
    if last_ai <= 0:
        return []
    out: list[dict] = []
    j = last_ai - 1
    while j >= 0 and messages[j].get("role") == "reasoning":
        out.insert(0, messages[j])
        j -= 1
    return out


def _messages_from_jsonl_forward(jsonl_path: Path) -> list[dict]:
    messages: list[dict] = []
    with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            messages.extend(_jsonl_line_to_messages(line))
    return messages


def _messages_from_jsonl_tail(jsonl_path: Path, *, limit: int) -> list[dict]:
    """Read only the last *limit* transcript rows without scanning the whole file."""
    collected: list[dict] = []
    with open(jsonl_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size == 0:
            return []
        pos = size
        partial = b""
        while pos > 0 and _jsonl_transcript_count(collected) < limit:
            read_len = min(_JSONL_TAIL_CHUNK_BYTES, pos)
            pos -= read_len
            fh.seek(pos)
            chunk = fh.read(read_len)
            block = chunk + partial
            parts = block.split(b"\n")
            partial = parts[0]
            for raw in reversed(parts[1:]):
                if _jsonl_transcript_count(collected) >= limit:
                    break
                if not raw.strip():
                    continue
                new_msgs = _jsonl_line_to_messages(
                    raw.decode("utf-8", errors="replace")
                )
                if not new_msgs:
                    continue
                collected = new_msgs + collected
        if _jsonl_transcript_count(collected) < limit and partial.strip():
            new_msgs = _jsonl_line_to_messages(
                partial.decode("utf-8", errors="replace")
            )
            if new_msgs:
                collected = new_msgs + collected
    return collected


def _messages_from_jsonl_file(jsonl_path: Path, *, limit: int) -> list[dict]:
    if not jsonl_path.is_file():
        return []
    try:
        if limit > 0:
            return _messages_from_jsonl_tail(jsonl_path, limit=limit)
        return _messages_from_jsonl_forward(jsonl_path)
    except OSError as e:
        log.warning("could not read transcript %s: %s", jsonl_path, e)
        return []


def _openclaw_jsonl_messages(
    runtime: QclawRuntime,
    session_key: str,
    agent_id: str,
    *,
    limit: int,
) -> tuple[list[dict], Optional[str], Optional[dict]]:
    """Return (messages, session_id, sessions.json entry) from OpenClaw disk store."""
    store_path = _sessions_store_path(runtime, agent_id)
    if not store_path.is_file():
        return [], None, None
    store = _read_sessions_json(store_path)
    if store is None:
        return [], None, None
    entry = store.get(session_key)
    if not isinstance(entry, dict):
        return [], None, None
    session_id = entry.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return [], None, entry
    jsonl_path = store_path.parent / f"{session_id}.jsonl"
    return _messages_from_jsonl_file(jsonl_path, limit=limit), session_id, entry


def _transcript_message_count(messages: list[dict]) -> int:
    return sum(
        1 for m in messages if m.get("role") in ("user", "assistant")
    )


def _transcript_rows(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") in ("user", "assistant")]


def _last_transcript_row(messages: list[dict]) -> Optional[dict]:
    rows = _transcript_rows(messages)
    return rows[-1] if rows else None


def _stitch_split_transcript_turn(
    db_msgs: list[dict],
    jsonl_msgs: list[dict],
) -> Optional[list[dict]]:
    """Merge db user tail + jsonl assistant when the same turn is split across stores."""
    db_rows = _transcript_rows(db_msgs)
    jl_rows = _transcript_rows(jsonl_msgs)
    if not db_rows or not jl_rows or len(db_rows) != len(jl_rows):
        return None
    if db_rows[-1].get("role") != "user" or jl_rows[-1].get("role") != "assistant":
        return None
    for db_row, jl_row in zip(db_rows[:-1], jl_rows[:-1]):
        if db_row.get("role") != jl_row.get("role"):
            return None
        if str(db_row.get("text") or "").strip() != str(jl_row.get("text") or "").strip():
            return None
    db_user = str(db_rows[-1].get("text") or "").strip()
    if not db_user:
        return None
    for row in reversed(jl_rows):
        if row.get("role") == "user":
            if str(row.get("text") or "").strip() == db_user:
                return None
            break
    return list(db_msgs) + _companion_reasoning_before_assistant(jsonl_msgs) + [jl_rows[-1]]


def _pick_transcript_messages(
    db_msgs: list[dict],
    jsonl_msgs: list[dict],
) -> tuple[list[dict], bool]:
    """Choose the richer transcript; second value means jsonl should be backfilled."""
    if not jsonl_msgs:
        return db_msgs, False
    if not db_msgs:
        return jsonl_msgs, True
    db_n = _transcript_message_count(db_msgs)
    jl_n = _transcript_message_count(jsonl_msgs)
    if jl_n > db_n:
        return jsonl_msgs, True
    if db_n > jl_n:
        return db_msgs, False

    stitched = _stitch_split_transcript_turn(db_msgs, jsonl_msgs)
    if stitched is not None:
        return stitched, True

    def _last_content(msgs: list[dict]) -> str:
        row = _last_transcript_row(msgs)
        return str(row.get("text") or "") if row else ""

    if len(_last_content(jsonl_msgs)) > len(_last_content(db_msgs)):
        return jsonl_msgs, True
    return db_msgs, False


def _merge_db_media_messages(
    picked: list[dict],
    db_msgs: list[dict],
) -> list[dict]:
    """Keep background PDF/slide completions stored only in Mongo/SQLite history."""
    media_rows = [
        m
        for m in db_msgs
        if m.get("role") == "assistant"
        and (m.get("media") or m.get("attachments"))
    ]
    if not media_rows:
        return picked
    seen = {
        (m.get("text") or "").strip()
        for m in picked
        if m.get("role") == "assistant"
    }
    extras = [m for m in media_rows if (m.get("text") or "").strip() not in seen]
    if not extras:
        return picked
    merged = list(picked) + extras
    merged.sort(key=lambda m: str(m.get("timestamp") or ""))
    return merged


def _backfill_history_from_jsonl(
    persistence: ChatPersistence,
    session_key: str,
    db_messages: list[dict],
    jsonl_messages: list[dict],
) -> None:
    """Copy transcript tail into SQLite when jsonl has more than the DB."""
    if len(jsonl_messages) <= len(db_messages):
        return
    for row in jsonl_messages[len(db_messages) :]:
        role = row.get("role")
        text = str(row.get("text") or "").strip()
        if role not in ("user", "assistant", "reasoning") or not text:
            continue
        if role == "reasoning" and row.get("source") == "model":
            text = f"Pensamento do modelo:\n{text}"
        mid = persistence.history.append_message(
            session_key, role=role, text=text
        )
        if mid is None:
            log.warning(
                "could not backfill %s message for session %s",
                role,
                session_key,
            )
            break


def read_messages(
    runtime: QclawRuntime,
    *,
    session_key: str,
    agent_id: Optional[str] = None,
    limit: int = 120,
    light: bool = False,
    persistence: Optional[ChatPersistence] = None,
) -> dict:
    """Load messages from SQLite history when present, merged with OpenClaw jsonl."""
    if not session_key:
        return {"ok": False, "error": "session_key is required"}

    parts = session_key.split(":")
    resolved_agent = agent_id or (parts[1] if len(parts) >= 2 else "main")

    if persistence is not None:
        sess = persistence.history.get_session_by_key(session_key)
        if sess is not None:
            db_msgs = persistence.history.list_messages(
                session_key,
                limit=limit,
                light=light,
            )
            jsonl_agent = agent_id or sess.agent_id or resolved_agent
            jsonl_msgs, oc_session_id, _entry = _openclaw_jsonl_messages(
                runtime,
                session_key,
                jsonl_agent,
                limit=limit,
            )
            source = "history"
            messages, jsonl_newer = _pick_transcript_messages(db_msgs, jsonl_msgs)
            messages = _merge_db_media_messages(messages, db_msgs)
            if jsonl_newer:
                # Polls use light=1 — skip Mongo writes so /openclaw/messages stays fast
                # while the agent is still streaming into the jsonl transcript.
                if not light:
                    _backfill_history_from_jsonl(
                        persistence, session_key, db_msgs, jsonl_msgs
                    )
                source = "history+jsonl" if db_msgs else "openclaw+jsonl"
            elif not db_msgs and jsonl_msgs:
                source = "openclaw+jsonl"
            return {
                "ok": True,
                "sessionKey": session_key,
                "sessionId": oc_session_id or sess.id,
                "agentId": sess.agent_id,
                "title": sess.title,
                "messages": _public_chat_messages(messages),
                "exists": True,
                "source": source,
                "control_url": runtime.control_url,
            }

    store_path = _sessions_store_path(runtime, resolved_agent)
    if not store_path.is_file():
        return {"ok": False, "error": f"session store not found for agent {resolved_agent!r}"}

    store = _read_sessions_json(store_path)
    if store is None:
        return {"ok": False, "error": f"could not read session store for agent {resolved_agent!r}"}

    entry = store.get(session_key) if isinstance(store, dict) else None
    if not isinstance(entry, dict):
        return {
            "ok": True,
            "sessionKey": session_key,
            "sessionId": None,
            "agentId": resolved_agent,
            "title": _session_title(session_key, {}),
            "messages": [],
            "exists": False,
            "control_url": runtime.control_url,
        }

    session_id = entry.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return {"ok": False, "error": "session has no sessionId"}

    jsonl_path = store_path.parent / f"{session_id}.jsonl"
    messages = _messages_from_jsonl_file(jsonl_path, limit=limit)

    return {
        "ok": True,
        "sessionKey": session_key,
        "sessionId": session_id,
        "agentId": resolved_agent,
        "title": _session_title(session_key, entry),
        "messages": _public_chat_messages(messages),
        "control_url": runtime.control_url,
    }


def _openclaw_cli_env(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
) -> tuple[Optional[Path], dict[str, str]]:
    bin_path, node_path, mjs_path = _resolve_openclaw_paths(doctor_cfg)
    if not bin_path or not node_path or not mjs_path:
        # Fallback: try to find openclaw on the system PATH
        import shutil as _shutil

        found = _shutil.which("openclaw")
        if found:
            bin_path = Path(found)
            env = {
                **os.environ,
                "OPENCLAW_STATE_DIR": str(runtime.state_dir),
                "OPENCLAW_CONFIG_PATH": str(runtime.config_path),
            }
            return bin_path, env

        # Fallback 2: try npx openclaw (requires npx on PATH)
        npx = _shutil.which("npx")
        if npx:
            bin_path = Path(npx)
            env = {
                **os.environ,
                "OPENCLAW_STATE_DIR": str(runtime.state_dir),
                "OPENCLAW_CONFIG_PATH": str(runtime.config_path),
                "_OPENCLAW_VIA_NPX": "1",
            }
            return bin_path, env

        return None, {}
    env = {
        **os.environ,
        "QCLAW_CLI_NODE_BINARY": str(node_path),
        "QCLAW_CLI_OPENCLAW_MJS": str(mjs_path),
        "OPENCLAW_STATE_DIR": str(runtime.state_dir),
        "OPENCLAW_CONFIG_PATH": str(runtime.config_path),
    }
    return bin_path, env


def send_message(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    chat_cfg: OpenclawChatConfig,
    *,
    session_key: str,
    message: str,
    agent_id: Optional[str] = None,
    persistence: Optional[ChatPersistence] = None,
    agent_cfg: Optional[AgentConfig] = None,
    on_progress: Optional[ChatProgressReporter] = None,
) -> dict:
    """Send a user message to OpenClaw via `openclaw agent`."""
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "message is required"}

    key = (session_key or "").strip() or chat_cfg.default_session_key
    if not key:
        return {"ok": False, "error": "session_key is required"}

    bin_path, env = _openclaw_cli_env(runtime, doctor_cfg)
    if not bin_path:
        return {
            "ok": False,
            "error": "openclaw CLI not found (configure openclaw_doctor paths)",
        }

    _via_npx = env.get("_OPENCLAW_VIA_NPX") == "1"

    resolved_agent = agent_id
    if not resolved_agent and ":" in key:
        parts = key.split(":")
        if len(parts) >= 2 and parts[1]:
            resolved_agent = parts[1]
    if not resolved_agent:
        resolved_agent = (chat_cfg.default_agent or "main").strip() or "main"

    session_id: Optional[str] = None
    store_path = runtime.state_dir / "agents" / resolved_agent / "sessions" / "sessions.json"
    if store_path.is_file():
        store = _read_sessions_json(store_path)
        if store is not None:
            entry = store.get(key)
            sid = entry.get("sessionId") if isinstance(entry, dict) else None
            if isinstance(sid, str) and sid:
                session_id = sid
    if not session_id:
        # Brand-new conversation: mint a stable session id we control and pass it
        # to the CLI via --session-id, so the agent writes into THIS conversation.
        # Never guess the "newest" session afterwards — that can persist the reply
        # into a different conversation (cross-conversation leak).
        session_id, _ = _ensure_session_entry(
            runtime, session_key=key, agent_id=resolved_agent
        )

    cmd = [str(bin_path), "openclaw", "agent"] if _via_npx else [str(bin_path), "agent"]
    if resolved_agent:
        cmd.extend(["--agent", resolved_agent])
    if session_id:
        # OpenClaw CLI >= 2026.4 uses --session-id (not --session-key).
        cmd.extend(["--session-id", session_id])
    cmd.extend(
        [
            "-m",
            text,
            "--json",
            "--timeout",
            str(int(chat_cfg.send_timeout_seconds)),
        ]
    )

    log.info("openclaw chat send: key=%s len=%d", key, len(text))

    publish_chat_status(
        "OpenClaw CLI a processar o pedido…",
        persistence=persistence,
        session_key=key,
        on_progress=on_progress,
    )

    try:
        with _progress_heartbeat(
            "OpenClaw CLI a processar",
            persistence=persistence,
            session_key=key,
            on_progress=on_progress,
            interval_seconds=10.0,
        ):
            completed = subprocess.run(
                cmd,
                capture_output=True,
                env=env,
                timeout=chat_cfg.send_timeout_seconds + 30.0,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": (
                f"openclaw agent timed out after "
                f"{int(chat_cfg.send_timeout_seconds)}s — "
                "a resposta pode ainda aparecer no histórico"
            ),
            "sessionKey": key,
            "pending": True,
        }

    stdout = completed.stdout.decode(errors="replace").strip()
    stderr = completed.stderr.decode(errors="replace").strip()
    reply = ""
    payload: Optional[dict[str, Any]] = None
    if stdout:
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                reply = (
                    payload.get("text")
                    or payload.get("message")
                    or payload.get("reply")
                    or ""
                )
                if not reply and "result" in payload:
                    reply = str(payload["result"])[:4000]
        except json.JSONDecodeError:
            reply = stdout[:4000]

    if completed.returncode != 0 and not reply:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        return {"ok": False, "error": detail[:500], "sessionKey": key}

    if not reply.strip():
        detail = stderr or stdout or "openclaw CLI returned no content"
        return {
            "ok": False,
            "error": f"openclaw CLI respondeu sem conteúdo: {detail[:300]}",
            "sessionKey": key,
        }

    # Invalidate session list cache after a send.
    clear_session_list_cache()

    # The conversation the user sent from (``key``) is the authoritative target.
    # We always pass a session id we control, so the reply belongs to THIS
    # conversation; only adopt a CLI-provided sessionKey when it matches ``key``.
    # Guessing the newest session here is forbidden — it leaks replies across
    # conversations.
    resolved_key = key
    if isinstance(payload, dict):
        pkey = payload.get("sessionKey")
        if isinstance(pkey, str) and pkey == key:
            resolved_key = pkey

    if persistence is not None and reply:
        aid = (resolved_agent or chat_cfg.default_agent or "main").strip() or "main"
        try:
            _ensure_session_entry(
                runtime,
                session_key=resolved_key,
                agent_id=aid,
            )
            persistence.remember_exchange(
                resolved_key,
                user_text=text,
                assistant_text=reply,
                agent_id=aid,
            )
        except Exception as e:
            log.warning("could not persist openclaw CLI exchange: %s", e)

    resolved_title: Optional[str] = None
    if reply and agent_cfg is not None:
        aid = (resolved_agent or chat_cfg.default_agent or "main").strip() or "main"
        api_key = resolve_llm_api_key(agent_cfg.api_key_env)
        if api_key:
            try:
                resolved_title = _maybe_auto_rename_session(
                    runtime,
                    chat_cfg,
                    agent_cfg,
                    session_key=resolved_key,
                    agent_id=aid,
                    user_text=text,
                    assistant_text=reply,
                    api_key=api_key,
                    timeout=float(agent_cfg.timeout_seconds),
                    persistence=persistence,
                )
            except Exception as e:
                log.warning("auto-title after openclaw CLI send failed: %s", e)

    out: dict[str, Any] = {
        "ok": True,
        "sessionKey": resolved_key,
        "reply": reply,
        "stderr_tail": stderr[-500:] if stderr else None,
    }
    if resolved_title:
        out["title"] = resolved_title
    return out




# Placeholder labels auto-renamed once the conversation has enough messages:
#   - "Nova conversa" and "Nova conversa HH:MM" (set by create_openclaw_session)
#   - "session-<ms>-<hex>" / "conv-<hex>"         (derived from new_session_key)
#   - "qcm-<ms>-<hex>"                           (legacy MongoDB session keys)
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:Nova conversa(?:\s+\d{2}:\d{2})?|session-\d+-[0-9a-f]+|conv-[0-9a-f]+|qcm-\d+-[0-9a-f]+)$"
)

# Sentinel inside the system prompt so tests (and logs) can recognize a title call.
_AUTO_TITLE_SYSTEM_PROMPT = (
    "Você gera títulos curtos (4 a 7 palavras) para conversas de chat. "
    "Responda APENAS com o título, na mesma língua da conversa. "
    "Sem aspas, sem ponto final, sem prefixos como 'Título:'."
)


def _is_placeholder_title(label: object) -> bool:
    if not isinstance(label, str):
        return True
    text = label.strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_TITLE_RE.match(text))


def _format_messages_for_auto_title(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 2400,
) -> str:
    """Build a compact transcript snippet for title generation."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        label = "Usuário" if role == "user" else "Assistente"
        lines.append(f"{label}: {text[:500]}")
    body = "\n\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
    return body


def _conversation_message_count(
    runtime: QclawRuntime,
    *,
    session_key: str,
    agent_id: str,
    persistence: Optional[ChatPersistence],
) -> int:
    if persistence is not None:
        n = persistence.history.count_conversation_messages(session_key)
        if n > 0:
            return n
    hist = read_messages(
        runtime,
        session_key=session_key,
        agent_id=agent_id,
        limit=500,
        persistence=persistence,
    )
    return sum(
        1
        for m in (hist.get("messages") or [])
        if m.get("role") in ("user", "assistant")
    )


def _clean_auto_title(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    # Models sometimes wrap in quotes/backticks or add a trailing period.
    text = text.strip("`\"' \t\n\r")
    if text.endswith("."):
        text = text[:-1].rstrip()
    # Some models prefix with "Título:" despite the instruction.
    lowered = text.lower()
    for prefix in ("título:", "titulo:", "title:"):
        if lowered.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    if len(text) > 80:
        head = text[:80]
        cut = head.rsplit(" ", 1)[0] if " " in head else head
        text = (cut or head) + "…"
    return text or None


def _persistence_session_title(
    persistence: Optional[ChatPersistence],
    session_key: str,
) -> Optional[str]:
    if persistence is None:
        return None
    sess = persistence.history.get_session_by_key(session_key)
    if sess is None:
        return None
    title = (sess.title or "").strip()
    return title or None


def _session_needs_auto_title(
    openclaw_label: object,
    persistence_title: Optional[str],
) -> bool:
    if _is_placeholder_title(openclaw_label):
        return True
    if persistence_title is not None and _is_placeholder_title(persistence_title):
        return True
    return False


def _generate_session_title(
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    user_text: str,
    assistant_text: str,
    conversation_snippet: Optional[str] = None,
) -> Optional[str]:
    """Ask the chat model for a short, descriptive title for the conversation."""
    snippet = (conversation_snippet or "").strip()
    if snippet:
        body = snippet
    else:
        snippet_user = (user_text or "").strip()[:600]
        if not snippet_user:
            return None
        snippet_assistant = (assistant_text or "").strip()[:600]
        body = f"Usuário: {snippet_user}"
        if snippet_assistant:
            body += f"\n\nAssistente: {snippet_assistant}"
    try:
        from .llm_gateway import make_client, trace_context

        client = make_client(api_key=api_key, base_url=base_url, timeout=timeout)
        with trace_context(name="chat.auto_title", model_label=model):
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _AUTO_TITLE_SYSTEM_PROMPT},
                    {"role": "user", "content": body},
                ],
            )
    except Exception as e:
        log.warning("auto-title generation failed: %s", e)
        return None
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return None
    content = getattr(getattr(choices[0], "message", None), "content", "") or ""
    return _clean_auto_title(content)


def _maybe_auto_rename_session(
    runtime: QclawRuntime,
    chat_cfg: OpenclawChatConfig,
    agent_cfg: AgentConfig,
    *,
    session_key: str,
    agent_id: str,
    user_text: str,
    assistant_text: str,
    api_key: str,
    timeout: float,
    persistence: Optional[ChatPersistence],
    title_model: Optional[str] = None,
    title_base_url: Optional[str] = None,
) -> Optional[str]:
    """Rename placeholder session labels after enough user+assistant messages."""
    min_msgs = max(1, int(chat_cfg.auto_title_after_messages))
    msg_count = _conversation_message_count(
        runtime,
        session_key=session_key,
        agent_id=agent_id,
        persistence=persistence,
    )
    if msg_count < min_msgs:
        return None

    store_path = _sessions_store_path(runtime, agent_id)
    store = _load_session_store(store_path)
    entry = store.get(session_key)
    persistence_title = _persistence_session_title(persistence, session_key)
    openclaw_label = (
        entry.get("label") if isinstance(entry, dict) else None
    )

    if not _session_needs_auto_title(openclaw_label, persistence_title):
        resolved = _pick_session_title(persistence_title, openclaw_label)
        if resolved and persistence is not None and persistence_title != resolved:
            persistence.history.touch_session(session_key, title=resolved)
        if (
            resolved
            and isinstance(entry, dict)
            and isinstance(entry.get("label"), str)
            and entry.get("label").strip() != resolved
        ):
            synced = dict(entry)
            synced["updatedAt"] = int(time.time() * 1000)
            synced["label"] = resolved
            store[session_key] = synced
            _save_session_store(store_path, store)
            clear_session_list_cache()
        return resolved or None

    if not isinstance(entry, dict):
        bootstrap = (
            persistence_title
            if not _is_placeholder_title(persistence_title)
            else _new_session_title()
        )
        _ensure_session_entry(
            runtime,
            session_key=session_key,
            agent_id=agent_id,
            title=bootstrap,
        )
        store = _load_session_store(store_path)
        entry = store.get(session_key)
        if not isinstance(entry, dict):
            return persistence_title or None

    title_snippet: Optional[str] = None
    if persistence is not None:
        title_snippet = _format_messages_for_auto_title(
            persistence.history.list_messages(session_key, limit=40),
        )
    else:
        fresh_hist = read_messages(
            runtime,
            session_key=session_key,
            agent_id=agent_id,
            limit=40,
            persistence=persistence,
        )
        title_snippet = _format_messages_for_auto_title(
            fresh_hist.get("messages") or [],
        )

    model = (title_model or agent_cfg.model or "").strip() or agent_cfg.model
    base_url = (title_base_url or agent_cfg.base_url or "").strip() or agent_cfg.base_url
    auto_title = _generate_session_title(
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        user_text=user_text,
        assistant_text=assistant_text,
        conversation_snippet=title_snippet or None,
    )
    if not auto_title:
        fallback = _pick_session_title(persistence_title, openclaw_label)
        if isinstance(entry, dict) and isinstance(entry.get("label"), str):
            fallback = _pick_session_title(fallback, entry.get("label"))
        return fallback or None

    entry = dict(entry)
    entry["updatedAt"] = int(time.time() * 1000)
    entry["label"] = auto_title
    store[session_key] = entry
    _save_session_store(store_path, store)
    clear_session_list_cache()
    if persistence is not None:
        persistence.history.touch_session(session_key, title=auto_title)
    return auto_title


def _stream_completion(
    client: OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    progress_job_id: Optional[str] = None,
    model_entry: Optional[Any] = None,
    model_id: str = "",
    model_label: str = "",
    provider: str = "",
) -> str:
    """Call LLM with stream=True and push partial tokens to the job tracker."""
    from .chat_jobs import update_partial_reply

    mid = str(model_id or getattr(model_entry, "id", None) or model or "").strip()
    label = str(model_label or getattr(model_entry, "label", None) or mid).strip()
    prov = str(provider or getattr(model_entry, "provider", None) or "").strip()
    kwargs = completion_extra_kwargs(model_entry) if model_entry else {}
    # Some providers don't support streaming; fall back gracefully
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            **kwargs,
        )
    except Exception as exc:
        # Fallback to non-streaming
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                **kwargs,
            )
            _record_llm_call_attempt(
                progress_job_id,
                model_id=mid,
                model_label=label,
                provider=prov,
                ok=True,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as retry_exc:
            _record_llm_call_attempt(
                progress_job_id,
                model_id=mid,
                model_label=label,
                provider=prov,
                ok=False,
                error=f"{type(retry_exc).__name__}: {retry_exc}",
            )
            raise retry_exc from exc

    chunks: list[str] = []
    for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        token = delta.content or ""
        if token:
            chunks.append(token)
            if progress_job_id and len(chunks) % 3 == 0:  # throttle updates
                update_partial_reply(
                    progress_job_id,
                    _strip_thinking_tags("".join(chunks)),
                )

    full = _strip_thinking_tags("".join(chunks))
    if progress_job_id:
        update_partial_reply(progress_job_id, full)
    _record_llm_call_attempt(
        progress_job_id,
        model_id=mid,
        model_label=label,
        provider=prov,
        ok=True,
    )
    return full


def _is_context_overflow_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "maximum context length" in msg or "context length" in msg and "reduce" in msg


def _gemini_prepared_tools(
    tools: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    aggressive: bool = False,
) -> list[dict[str, Any]]:
    from .gemini_tool_schema import is_gemini_compat_endpoint, prepare_llm_tools_for_gemini

    if not tools or not is_gemini_compat_endpoint(base_url, model):
        return tools
    return prepare_llm_tools_for_gemini(tools, aggressive=aggressive)


def _completion_create_with_tool_failover(
    client: OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tools_full: list[dict[str, Any]],
    model_id: str,
    base_url: str,
    model_entry: Optional[Any] = None,
    tool_choice: str = "auto",
    max_context_chars: int = 0,
) -> tuple[Any, list[dict[str, Any]]]:
    """Call chat completions; on tools-limit 400, persist cap and retry once."""
    from openai import BadRequestError

    from .gemini_tool_schema import (
        gemini_schema_failover,
        is_gemini_compat_endpoint,
        is_gemini_invalid_argument_error,
    )

    tools = _gemini_prepared_tools(tools, base_url=base_url, model=model)

    active_tool_choice = tool_choice

    def _create(active_tools: list[dict[str, Any]]):
        return client.chat.completions.create(
            model=model,
            messages=messages,
            tools=active_tools,
            tool_choice=active_tool_choice,
            **completion_extra_kwargs(model_entry),
        )

    active_tools = tools
    schema_attempt = 0
    while True:
        try:
            return _create(active_tools), active_tools
        except BadRequestError as exc:
            if (
                active_tool_choice == "required"
                and is_gemini_compat_endpoint(base_url, model)
                and is_gemini_invalid_argument_error(exc)
            ):
                log.warning(
                    "openclaw chat: gemini tool_choice=required rejected, retrying with auto "
                    "(model=%s, tools=%d)",
                    model_id,
                    len(active_tools),
                )
                active_tool_choice = "auto"
                continue
            failover = tools_limit_failover(
                exc,
                model_id=model_id,
                base_url=base_url,
                tools_full=tools_full,
                tools_current=active_tools,
            )
            if failover is not None:
                active_tools, dropped, limit = failover
                active_tools = _gemini_prepared_tools(
                    active_tools, base_url=base_url, model=model
                )
                log.warning(
                    "openclaw chat: tools-limit failover model=%s %d -> %d (dropped %d, limit=%d)",
                    model_id,
                    len(tools_full),
                    len(active_tools),
                    len(dropped),
                    limit,
                )
                continue
            schema_failover = gemini_schema_failover(
                exc,
                tools_full=tools_full,
                tools_current=active_tools,
                base_url=base_url,
                model=model,
                attempt=schema_attempt,
            )
            if schema_failover is not None:
                active_tools, reason = schema_failover
                schema_attempt += 1
                log.warning(
                    "openclaw chat: gemini schema failover model=%s -> %d tools (%s)",
                    model_id,
                    len(active_tools),
                    reason,
                )
                continue
            if _is_context_overflow_error(exc) and max_context_chars > 0:
                before = sum(_msg_char_size(m) for m in messages)
                budget = max(int(max_context_chars * 0.65), 100_000)
                trimmed = _trim_messages_to_budget(messages, budget)
                after = sum(_msg_char_size(m) for m in trimmed)
                if after < before:
                    log.warning(
                        "openclaw chat: context overflow retry model=%s %d -> %d chars (budget=%d)",
                        model_id,
                        before,
                        after,
                        budget,
                    )
                    messages[:] = trimmed
                    continue
            raise


def _collect_team_created_from_tool(
    tool_name: str,
    result: Any,
    teams_created: list[dict[str, Any]],
) -> None:
    """Record teams created via qclaw_create_team for the chat UI refresh."""
    if (tool_name or "").strip() != "qclaw_create_team":
        return
    if not isinstance(result, dict) or not result.get("ok"):
        return
    team = result.get("team")
    if not isinstance(team, dict):
        return
    tid = str(team.get("id") or "").strip()
    if not tid:
        return
    if any(str(t.get("id") or "").strip() == tid for t in teams_created):
        return
    teams_created.append(dict(team))


def _deepseek_completion_with_qclaw_tools(
    client: OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tools_full: list[dict[str, Any]],
    model_id: str,
    base_url: str,
    tool_ctx: QclawChatToolContext,
    runtime: QclawRuntime,
    chat_cfg: OpenclawChatConfig,
    max_iterations: int,
    on_progress: Optional[ChatProgressReporter] = None,
    persistence: Optional[ChatPersistence] = None,
    project_store: Optional[Any] = None,
    session_key: str = "",
    progress_job_id: Optional[str] = None,
    model_label: str = "",
    model_entry: Optional[Any] = None,
) -> tuple[
    str,
    list[str],
    list[str],
    Optional[dict[str, Any]],
    Optional[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    Optional[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Run tool loop; return assistant text, tools, mcp skills, extras, media, attachments, background, teams."""
    used_tools: list[str] = []
    teams_created: list[dict[str, Any]] = []
    mcp_skills_used: list[str] = []
    wacli_qr_extra: Optional[dict[str, Any]] = None
    desktop_shot_extra: Optional[dict[str, Any]] = None
    media_extra: dict[str, list[dict[str, Any]]] = {"images": [], "videos": []}
    attachments_extra: list[dict[str, Any]] = []
    working = list(messages)
    reply = ""
    max_iters = max(1, int(max_iterations))
    initial_user_text = ""
    for msg in reversed(working):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            initial_user_text = content.strip()
            break
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                initial_user_text = joined
                break
    force_tools_first_turn = bool(
        initial_user_text and requires_immediate_tool_call(initial_user_text) and tools
    )
    tool_run = begin_tool_run(
        session_key,
        max_iters,
        job_id=progress_job_id,
        kind="chat",
        model_label=model_label,
    )
    if progress_job_id:
        set_tool_progress(
            progress_job_id,
            0,
            max_iters,
            message=with_model_prefix("A preparar ferramentas…", model_label),
        )

    try:
        reply, used_tools, mcp_skills_used, wacli_qr_extra, desktop_shot_extra, media_extra, attachments_extra, background_meta, teams_created = (
            _deepseek_completion_with_qclaw_tools_loop(
                client,
                model=model,
                tools=tools,
                tools_full=tools_full,
                model_id=model_id,
                base_url=base_url,
                tool_ctx=tool_ctx,
                runtime=runtime,
                chat_cfg=chat_cfg,
                max_iterations=max_iters,
                on_progress=on_progress,
                persistence=persistence,
                project_store=project_store,
                session_key=session_key,
                progress_job_id=progress_job_id,
                tool_run=tool_run,
                working=working,
                used_tools=used_tools,
                teams_created=teams_created,
                mcp_skills_used=mcp_skills_used,
                wacli_qr_extra=wacli_qr_extra,
                desktop_shot_extra=desktop_shot_extra,
                media_extra=media_extra,
                attachments_extra=attachments_extra,
                reply=reply,
                model_entry=model_entry,
                force_tools_first_turn=force_tools_first_turn,
            )
        )
        return (
            reply,
            used_tools,
            mcp_skills_used,
            wacli_qr_extra,
            desktop_shot_extra,
            media_extra,
            attachments_extra,
            background_meta,
            teams_created,
        )
    finally:
        remaining = end_tool_run(tool_run.job_id)
        label = tool_run.label or session_key
        _notify_tool_availability(
            remaining,
            finished_session=session_key,
            finished_label=label,
            persistence=persistence,
            on_progress=on_progress,
        )


def _deepseek_completion_with_qclaw_tools_loop(
    client: OpenAI,
    *,
    model: str,
    tools: list[dict[str, Any]],
    tools_full: list[dict[str, Any]],
    model_id: str,
    base_url: str,
    tool_ctx: QclawChatToolContext,
    runtime: QclawRuntime,
    chat_cfg: OpenclawChatConfig,
    max_iterations: int,
    on_progress: Optional[ChatProgressReporter],
    persistence: Optional[ChatPersistence],
    project_store: Optional[Any] = None,
    session_key: str,
    progress_job_id: Optional[str],
    tool_run: ToolRun,
    working: list[dict[str, Any]],
    used_tools: list[str],
    teams_created: list[dict[str, Any]],
    mcp_skills_used: list[str],
    wacli_qr_extra: Optional[dict[str, Any]],
    desktop_shot_extra: Optional[dict[str, Any]],
    media_extra: dict[str, list[dict[str, Any]]],
    attachments_extra: list[dict[str, Any]],
    reply: str,
    model_entry: Optional[Any] = None,
    force_tools_first_turn: bool = False,
) -> tuple[
    str,
    list[str],
    list[str],
    Optional[dict[str, Any]],
    Optional[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    Optional[dict[str, Any]],
    list[dict[str, Any]],
]:
    from .chat_tool_background import (
        background_tool_meta,
        background_tool_reply,
        is_background_tool_result,
    )

    background_meta: Optional[dict[str, Any]] = None
    token_limits = resolve_token_limits(chat_cfg)
    action_nudges = 0
    for turn in range(max_iterations):
        model_ctx = getattr(model_entry, "max_context_chars", 0) or 0 if model_entry else 0
        max_ctx = effective_max_context_chars(chat_cfg, model_max_context_chars=model_ctx)
        if max_ctx and max_ctx > 0 and len(working) > 2:
            trimmed = _trim_messages_to_budget(working, max_ctx)
            working[:] = trimmed
        if token_limits.trim_tool_loop:
            strip_stale_screenshots_from_messages(working)
        # --- Cancellation checkpoint ---
        if progress_job_id and is_job_cancelled(progress_job_id):
            reply = "⏹ Execução cancelada pelo usuário."
            break
        step = turn + 1
        updated = set_tool_turn(tool_run.job_id, step)
        status_line = format_status_with_peers(updated or tool_run)
        if progress_job_id:
            set_tool_progress(
                progress_job_id,
                step,
                max_iterations,
                message=status_line,
            )
        status_run = updated or tool_run
        status_run.turn = step
        publish_chat_status(
            status_line,
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
        )
        hb_label = f"Passo {step}/{max_iterations} — a consultar o modelo"
        from .gemini_tool_schema import resolve_chat_tool_choice

        tool_choice = resolve_chat_tool_choice(
            turn=turn,
            force_tools_first_turn=force_tools_first_turn,
            used_tools=bool(used_tools),
            base_url=base_url,
            model=model,
            tool_count=len(tools),
        )
        with _progress_heartbeat(
            hb_label,
            model_label=status_run.model_label,
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
        ):
            resp, tools = _completion_create_with_tool_failover(
                client,
                model=model,
                messages=working,
                tools=tools,
                tools_full=tools_full,
                model_id=model_id,
                base_url=base_url,
                model_entry=model_entry,
                tool_choice=tool_choice,
                max_context_chars=max_ctx,
            )
            _record_llm_call_attempt(
                progress_job_id,
                model_id=model_id,
                model_label=tool_run.model_label or (
                    getattr(model_entry, "label", None) if model_entry else ""
                ) or model_id,
                provider=getattr(model_entry, "provider", "") if model_entry else "",
                ok=True,
            )
        msg = resp.choices[0].message
        reasoning, visible_content = _extract_reasoning_from_completion_message(msg)
        if reasoning:
            _publish_model_reasoning_live(
                reasoning,
                progress_job_id=progress_job_id,
                persistence=persistence,
                session_key=session_key,
            )
        assistant_turn: dict[str, Any] = {
            "role": "assistant",
            "content": visible_content if visible_content else (msg.content or ""),
        }
        if msg.tool_calls:
            from .gemini_tool_schema import serialize_tool_call_for_replay

            assistant_turn["tool_calls"] = [
                serialize_tool_call_for_replay(
                    tc,
                    base_url=base_url,
                    model=model,
                )
                for tc in msg.tool_calls
            ]
        working.append(assistant_turn)

        if not msg.tool_calls:
            reply = (visible_content or (msg.content or "")).strip()
            if (
                action_nudges < MAX_TOOL_ACTION_NUDGES
                and turn + 1 < max_iterations
                and reply_promises_deferred_action(reply)
            ):
                action_nudges += 1
                log.info(
                    "openclaw chat: deferred-action nudge %d/%d model=%s",
                    action_nudges,
                    MAX_TOOL_ACTION_NUDGES,
                    model_id,
                )
                working.append({"role": "user", "content": TOOL_ACTION_NUDGE})
                continue
            break

        freed_status = ""
        background_spawn_count = 0
        for tc in msg.tool_calls:
            tname = tc.function.name
            used_tools.append(tname)
            try:
                targs = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                targs = {}
            if not isinstance(targs, dict):
                targs = {}
            mcp_skill = extract_mcp_skill_from_tool_call(tname, targs)
            if mcp_skill and mcp_skill not in mcp_skills_used:
                mcp_skills_used.append(mcp_skill)
            tool_detail = _format_tool_progress(tname, targs)
            duration_hint = long_task_hint_for_tool(tname, targs)
            if duration_hint:
                publish_chat_status(
                    format_long_task_warning(duration_hint),
                    persistence=persistence,
                    session_key=session_key,
                    on_progress=on_progress,
                    persist=True,
                )
            status_run = set_tool_turn(tool_run.job_id, step) or tool_run
            status_run.turn = step
            publish_chat_status(
                format_status_with_peers(status_run, tool_name=tool_detail),
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            try:
                with _progress_heartbeat(
                    tool_detail,
                    model_label=status_run.model_label,
                    persistence=persistence,
                    session_key=session_key,
                    on_progress=on_progress,
                    interval_seconds=6.0,
                ):
                    result = _run_qclaw_chat_tool(
                        tname,
                        targs,
                        ctx=tool_ctx,
                        runtime=runtime,
                        chat_cfg=chat_cfg,
                        persistence=persistence,
                        project_store=project_store,
                        session_key=session_key,
                        progress_job_id=progress_job_id,
                        model_id=model_id,
                    )
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            _collect_team_created_from_tool(tname, result, teams_created)
            if tname == "qclaw_wacli_auth_qr" and isinstance(result, dict):
                if result.get("qr_image_data_url"):
                    wacli_qr_extra = {
                        "qr_image_data_url": result.get("qr_image_data_url"),
                        "instructions": result.get("instructions"),
                        "note": result.get("note"),
                        "already_authenticated": result.get("already_authenticated"),
                        "poll_auth": bool(result.get("poll_auth", True)),
                    }
                elif result.get("already_authenticated"):
                    wacli_qr_extra = {
                        "already_authenticated": True,
                        "whatsapp_ready": True,
                        "message": result.get("message"),
                    }
            if tname == "qclaw_desktop_screenshot" and isinstance(result, dict):
                if result.get("image_data_url"):
                    desktop_shot_extra = {
                        "image_data_url": result.get("image_data_url"),
                        "image_path": result.get("image_path"),
                        "caption": "Captura de ecrã do desktop",
                    }
                if (
                    model_entry is not None
                    and model_supports_vision(model_entry)
                    and result.get("image_data_url")
                ):
                    working.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Screenshot capturado pela ferramenta "
                                        "qclaw_desktop_screenshot (use para decidir cliques):"
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": str(result.get("image_data_url")),
                                    },
                                },
                            ],
                        }
                    )
            if isinstance(result, dict) and tname not in {
                "qclaw_desktop_screenshot",
                "qclaw_wacli_auth_qr",
            }:
                _collect_media_from_tool_result(result, media_extra)
                _collect_attachments_from_tool_result(result, attachments_extra)
                if progress_job_id and (
                    media_extra.get("images")
                    or media_extra.get("videos")
                    or media_extra.get("preview_fallback")
                    or media_extra.get("creativeWidget")
                ):
                    update_job_media(progress_job_id, media_extra)
            log.info("chat tool %s (turn %d)", tname, turn + 1)
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _tool_result_string(result),
                }
            )
            if isinstance(result, dict) and is_background_tool_result(result):
                background_spawn_count += 1
                if background_spawn_count == 1:
                    reply = background_tool_reply(result)
                    background_meta = background_tool_meta(result)
                else:
                    background_meta = background_tool_meta(result)
                freed_status = "Tarefa em background — chat libertado."
                if background_spawn_count > 1:
                    reply = (
                        f"{background_spawn_count} gerações de imagem em background — "
                        "chat libertado."
                    )
                continue
            # --- Cancellation checkpoint after each tool execution ---
            if progress_job_id and is_job_cancelled(progress_job_id):
                break
        if background_meta is not None:
            if progress_job_id:
                set_tool_progress(
                    progress_job_id,
                    step,
                    max_iterations,
                    message=freed_status or "Tarefa em background — chat libertado.",
                )
            publish_chat_status(
                freed_status or "Tarefa em background — chat libertado.",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            break
        # Break outer loop if cancelled
        if progress_job_id and is_job_cancelled(progress_job_id):
            reply = "⏹ Execução cancelada pelo usuário."
            break
        if background_meta is not None:
            break
    else:
        if progress_job_id:
            set_tool_progress(progress_job_id, max_iterations, max_iterations)
        set_tool_turn(tool_run.job_id, max_iterations)
        tool_run.turn = max_iterations
        publish_chat_status(
            format_status_with_peers(tool_run, hit_limit=True),
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
        )
        reply = format_tool_limit_reply(max_iterations)

    return (
        reply,
        used_tools,
        mcp_skills_used,
        wacli_qr_extra,
        desktop_shot_extra,
        media_extra,
        attachments_extra,
        background_meta,
        teams_created,
    )


def _history_to_llm_messages(
    history: list[dict],
    *,
    limit: int,
) -> list[dict[str, str]]:
    """Map stored chat rows to DeepSeek/OpenAI message roles (no ``status``)."""
    rows = history[-limit:] if limit > 0 else history
    out: list[dict[str, str]] = []
    for row in rows:
        role = row.get("role")
        text = (row.get("text") or "").strip()
        if not text:
            continue
        # ``status`` is for the dashboard only (tool progress, limits, availability).
        if role == "status":
            continue
        if role in ("user", "assistant"):
            out.append({"role": role, "content": text})
    return out


def _msg_char_size(msg: dict[str, Any]) -> int:
    """Estimate char size of a single LLM message (content may be str or list)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                total += len(part.get("text", ""))
                # image_url blocks are typically base64 — estimate conservatively
                url = (part.get("image_url") or {}).get("url", "")
                total += len(url)
            elif isinstance(part, str):
                total += len(part)
        return total
    return 0


def _trim_messages_to_budget(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    tail_keep: int = 10,
) -> list[dict[str, Any]]:
    """Trim oldest history messages (keeping system + recent turns) to fit in budget.

    Strategy: always keep the first (system) message and the last ``tail_keep``
    non-system messages (tool loop continuity). Trim older history from the front.
    If a single history message is over 50% of budget, truncate its content.
    """
    if not messages:
        return messages

    total = sum(_msg_char_size(m) for m in messages)
    if total <= max_chars:
        return messages

    system_msg = messages[0] if messages[0].get("role") == "system" else None
    start = 1 if system_msg else 0
    body = messages[start:]
    user_msg = messages[-1] if messages[-1].get("role") == "user" else None

    if user_msg is not None and len(body) > 1:
        protected_tail = [user_msg]
        middle = body[:-1]
    elif len(body) > tail_keep:
        protected_tail = body[-tail_keep:]
        middle = body[:-tail_keep]
    else:
        protected_tail = body
        middle = []

    fixed_cost = sum(_msg_char_size(m) for m in ([system_msg] if system_msg else []) + protected_tail)
    history_budget = max(max_chars - fixed_cost, max_chars // 4)

    per_msg_cap = max(history_budget // 3, 20_000)
    trimmed_middle: list[dict[str, Any]] = []
    for msg in middle:
        size = _msg_char_size(msg)
        if size > per_msg_cap:
            content = msg.get("content", "")
            if isinstance(content, str):
                msg = {**msg, "content": content[:per_msg_cap] + "\n…(truncated)…"}
            elif isinstance(content, list):
                continue
        trimmed_middle.append(msg)

    hist_total = sum(_msg_char_size(m) for m in trimmed_middle)
    while trimmed_middle and hist_total > history_budget:
        dropped = trimmed_middle.pop(0)
        hist_total -= _msg_char_size(dropped)

    trimmed_tail: list[dict[str, Any]] = []
    for msg in protected_tail:
        size = _msg_char_size(msg)
        if size > per_msg_cap:
            content = msg.get("content", "")
            if isinstance(content, str):
                msg = {**msg, "content": content[:per_msg_cap] + "\n…(truncated)…"}
            elif isinstance(content, list):
                continue
        trimmed_tail.append(msg)

    result: list[dict[str, Any]] = []
    if system_msg:
        result.append(system_msg)
    result.extend(trimmed_middle)
    result.extend(trimmed_tail)

    trimmed_total = sum(_msg_char_size(m) for m in result)
    if trimmed_total > max_chars and result and result[0].get("role") == "system":
        sys_content = result[0].get("content", "")
        if isinstance(sys_content, str):
            over = trimmed_total - max_chars
            new_len = max(len(sys_content) - over - 100, max_chars // 4)
            result[0] = {**result[0], "content": sys_content[:new_len] + "\n…(system truncated)…"}

    log.info(
        "context trim: %d chars → %d chars (%d history msgs kept)",
        total,
        sum(_msg_char_size(m) for m in result),
        len(result) - (1 if system_msg else 0),
    )
    return result


def send_message_deepseek(
    runtime: QclawRuntime,
    agent_cfg: AgentConfig,
    chat_cfg: OpenclawChatConfig,
    *,
    session_key: str,
    message: str,
    agent_id: Optional[str] = None,
    model_id: Optional[str] = None,
    brief_target: Optional[str] = None,
    image_model: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    qclaw_tools: Optional[QclawChatToolContext] = None,
    persistence: Optional[ChatPersistence] = None,
    project_store: Optional[Any] = None,
    user_id: Optional[str] = None,
    team_info: Optional[dict[str, Any]] = None,
    on_progress: Optional[ChatProgressReporter] = None,
    user_already_saved: bool = False,
    progress_job_id: Optional[str] = None,
    preloaded_skills_catalog: Optional[dict] = None,
) -> dict:
    """Answer via configured LLM using OpenClaw SKILL.md bodies in the system prompt."""
    from .chat_latency import ChatLatencyTracker
    from .openclaw_skills import format_openclaw_skills_for_chat, list_openclaw_skills

    latency = ChatLatencyTracker()

    text = (message or "").strip()
    if not text and not attachments:
        return {"ok": False, "error": "message or attachments required"}

    key = (session_key or "").strip() or chat_cfg.default_session_key
    if not key:
        return {"ok": False, "error": "session_key is required"}

    resolved_agent = (agent_id or _agent_from_session_key(key, chat_cfg.default_agent)).strip()
    resolved_agent = resolved_agent or chat_cfg.default_agent

    from .model_router import resolve_effective_model_id

    effective_model_id, route = resolve_effective_model_id(
        chat_cfg,
        model_id,
        message=text,
        attachments=attachments,
    )
    if route and on_progress:
        on_progress(f"Auto → {route.label} ({route.reason})")

    resolved, model_err = resolve_chat_model(chat_cfg, effective_model_id)
    if model_err or resolved is None:
        out = {
            "ok": False,
            "error": model_err or "model unavailable",
            "sessionKey": key,
        }
        _track_chat_model_attempt(
            progress_job_id,
            out,
            model_id=str(effective_model_id or model_id or ""),
        )
        return out

    def _return(out: dict[str, Any]) -> dict[str, Any]:
        # Per-call tracking happens at each LLM request; keep _return for failures only.
        if not out.get("ok"):
            _track_chat_model_attempt(
                progress_job_id,
                out,
                model_id=resolved.entry.id,
                model_label=resolved.entry.label,
                provider=resolved.entry.provider,
            )
        return out

    attachments_dir = resolve_attachments_dir(chat_cfg)
    parsed_attachments, att_err = parse_attachments(
        attachments,
        max_count=chat_cfg.max_attachments,
        max_bytes=chat_cfg.max_attachment_bytes,
        attachments_dir=attachments_dir,
    )
    if att_err:
        return _return({"ok": False, "error": att_err, "sessionKey": key})

    parsed_attachments, transcribe_err, _ = _normalize_chat_attachments(
        parsed_attachments,
        timeout=chat_cfg.send_timeout_seconds,
    )
    if transcribe_err:
        return _return({"ok": False, "error": transcribe_err, "sessionKey": key})

    parsed_attachments = persist_attachments(
        parsed_attachments,
        session_key=key,
        attachments_dir=attachments_dir,
    )

    # --- Short-circuit: image-only messages skip LLM entirely ---
    # When the user sends image(s) with no meaningful text, just save and display.
    _IMAGE_ONLY_TRIGGERS = {"", "imagem", "img", "foto", "image", "photo", "picture"}
    _is_image_only = (
        parsed_attachments
        and all(att.mime_type.startswith("image/") for att in parsed_attachments)
        and text.lower().strip().rstrip(".!?") in _IMAGE_ONLY_TRIGGERS
    )
    if _is_image_only:
        # Build media response showing the saved images
        images_out: list[dict[str, Any]] = []
        for att in parsed_attachments:
            entry: dict[str, Any] = {"ok": True}
            if att.path:
                entry["image_path"] = str(att.path)
            if att.data_url:
                entry["image_data_url"] = att.data_url
            entry["name"] = att.name
            entry["mime_type"] = att.mime_type
            entry["size"] = len(att.data)
            images_out.append(entry)

        img_names = ", ".join(att.name for att in parsed_attachments)
        reply_text = f"📷 Imagem recebida e salva: {img_names}"

        # Persist to history
        if persistence is not None:
            user_extras = None
            meta = attachments_meta_for_persistence(
                [
                    {
                        "name": att.name,
                        "mime_type": att.mime_type,
                        "path": str(att.path) if att.path else None,
                        "size": len(att.data),
                    }
                    for att in parsed_attachments
                ]
            )
            if meta:
                user_extras = {"attachments": meta}
            if not user_already_saved:
                persistence.history.append_message(
                    key, role="user", text=text or "(imagem)", extras=user_extras
                )
            persistence.remember_exchange(
                key,
                user_text=text or "(imagem)",
                assistant_text=reply_text,
                tools_used=None,
                extras={"media": {"images": images_out, "videos": []}},
                agent_id=resolved_agent,
                user_already_saved=True,
            )

        return {
            "ok": True,
            "sessionKey": key,
            "reply": reply_text,
            "backend": "image-passthrough",
            "model": "none",
            "modelId": "none",
            "provider": "local",
            "agentId": resolved_agent,
            "media": {"images": images_out, "videos": []},
            "skillsUsed": ["image-passthrough"],
        }
    # --- End short-circuit ---

    publish_chat_status(
        "A carregar contexto…",
        persistence=persistence,
        session_key=key,
        on_progress=on_progress,
    )

    from .gois_lite import is_gois_lite

    if preloaded_skills_catalog is not None:
        catalog = preloaded_skills_catalog
    elif is_gois_lite():
        catalog = {"ok": True, "skills": []}
    else:
        with latency.phase("skills"):
            with _progress_heartbeat(
                "A carregar skills e contexto",
                model_label=resolved.entry.label,
                persistence=persistence,
                session_key=key,
                on_progress=on_progress,
                interval_seconds=6.0,
            ):
                catalog = list_openclaw_skills(
                    runtime,
                    agent_id=resolved_agent,
                    gois_extra_dirs=list(getattr(chat_cfg, "extra_skill_dirs", None) or []),
                )
    if not catalog.get("ok", True):
        return {
            "ok": False,
            "error": catalog.get("error") or "could not load OpenClaw skills",
            "sessionKey": key,
        }

    from .gois_lite import is_gois_lite

    token_limits = resolve_token_limits(chat_cfg)

    skills_block = format_openclaw_skills_for_chat(
        catalog.get("skills") or [],
        max_items=token_limits.max_skills_in_prompt,
        max_body_chars=token_limits.max_skill_body_chars,
        user_text=text,
    )
    notes_prefix = ""
    if not is_gois_lite():
        notes_prefix = build_skill_notes_prefix(
            chat_cfg,
            runtime=runtime,
            qclaw_tools=qclaw_tools,
        )
    if notes_prefix:
        skills_block = notes_prefix + "\n\n" + skills_block

    if is_gois_lite():
        system_content = (
            f"{effective_system_prompt(chat_cfg)}"
            f"{coding_system_suffix(resolved.entry)}"
        )
    else:
        system_content = (
            f"{effective_system_prompt(chat_cfg)}"
            f"{long_task_rule_block()}"
            f"{coding_system_suffix(resolved.entry)}\n\n"
            f"## OpenClaw skills (follow when relevant)\n\n{skills_block}"
        )
    brief_block = _brief_target_instruction_block(brief_target)
    if brief_block:
        system_content += f"\n\n{brief_block}"
    image_model_block = _image_model_instruction_block(image_model)
    if image_model_block:
        system_content += f"\n\n{image_model_block}"

    if user_id:
        try:
            from .chat_personality import format_system_block, get_profile

            prof_result = get_profile(user_id)
            profile = prof_result.get("profile") if prof_result.get("ok") else None
            if profile and profile.get("active"):
                personality_block = format_system_block(profile)
                if personality_block:
                    system_content += f"\n\n{personality_block}"
        except Exception:
            log.debug("chat personality inject skipped for user_id=%s", user_id, exc_info=True)

    try:
        from .user_personal_data import format_system_block as format_personal_block
        from .user_personal_data import get_profile_for_chat

        personal_profile = get_profile_for_chat(user_id)
        if personal_profile:
            personal_block = format_personal_block(personal_profile)
            if personal_block:
                system_content += f"\n\n{personal_block}"
    except Exception:
        log.debug("user personal data inject skipped for user_id=%s", user_id, exc_info=True)

    # Inject selected team + kanban context so the LLM knows which team is active.
    if team_info is None and persistence is not None:
        # Auto-resolve from session if caller did not provide explicit team_info.
        sess_row = persistence.history.get_session_by_key(key)
        if sess_row is not None and sess_row.team_id:
            team_info = {"id": sess_row.team_id}
    if team_info:
        from .chat_context_models import build_team_chat_context

        kanban_data = team_info.pop("__kanban", None)
        normas_data = team_info.pop("__normas", None)
        try:
            chat_ctx = build_team_chat_context(team_info, kanban_data, normas_data)
            system_content += "\n\n" + chat_ctx.format_system_block()
            try:
                from .user_appearance_team import format_team_appearance_block

                appearance_block = format_team_appearance_block(
                    str(team_info.get("id") or ""),
                    current_user_id=str(user_id or ""),
                )
                if appearance_block:
                    system_content += "\n\n" + appearance_block
            except Exception:
                log.debug("team appearance inject skipped", exc_info=True)
        except Exception:
            # Fallback: minimal team id if Pydantic validation fails
            system_content += (
                f"\n\n## Time selecionado nesta conversa\n\n"
                f"- ID: {team_info.get('id', '?')}\n\n"
                "Todas as respostas e ações devem levar em conta o contexto "
                "deste time."
            )
        if qclaw_tools is not None and getattr(qclaw_tools, "team_contacts_upsert", None) is not None:
            contacts_rule = team_contacts_rule_block(chat_cfg)
            if contacts_rule:
                system_content += contacts_rule

    if persistence is not None:
        from .chat_context_documents import session_context_documents_block

        sess_for_docs = persistence.history.get_session_by_key(key)
        docs_team_id = (
            (team_info or {}).get("id")
            if team_info
            else (sess_for_docs.team_id if sess_for_docs else None)
        )
        docs_block = session_context_documents_block(
            persistence.history,
            key,
            team_id=docs_team_id,
        )
        if docs_block:
            system_content += (
                "\n\n## Documentos da conversa (fixos nesta sessão)\n\n"
                f"{docs_block}\n\n"
                "Estes documentos foram adicionados pelo utilizador ao contexto "
                "desta conversa — trate-os como referência principal."
            )

    if persistence is not None:
        # Skip expensive ChromaDB context retrieval for trivial messages
        _TRIVIAL = {"oi", "olá", "alo", "ola", "hey", "hi", "opa", "e aí", "e ai"}
        is_trivial = len(text) < 10 and text.lower().strip().rstrip("?.!") in _TRIVIAL

        if is_trivial:
            log.info("openclaw chat: skipping context retrieval for trivial message")
            # Skip expensive context loading for trivial greetings
        elif chat_cfg.project_memory_enabled and project_store is not None:
            proj_block, sess_block = persistence.project_and_session_context_block(
                key,
                text,
                agent_id=resolved_agent,
                project_store=project_store,
                project_max_chars=token_limits.project_memory_max_chars,
                session_limit=token_limits.context_retrieval_limit,
                project_search_limit=token_limits.context_retrieval_limit,
            )
            if proj_block:
                system_content += (
                    f"\n\n## Project memory (all chats)\n\n{proj_block}"
                )
            if sess_block:
                system_content += (
                    f"\n\n## Stored context (this session only)\n\n{sess_block}"
                )
            if proj_block or sess_block:
                system_content += (
                    "\n\n**IMPORTANT**: Always prioritise the conversation history "
                    "and session context above. Project memory is background reference only. "
                    "Answer strictly about the topic the user is asking in THIS session. "
                    "Do NOT confuse topics from other sessions stored in project memory."
                )
        else:
            ctx_block = persistence.relevant_context_block(
                key,
                text,
                agent_id=resolved_agent,
                limit=token_limits.context_retrieval_limit,
            )
            if ctx_block:
                system_content += (
                    f"\n\n## Stored context (SQLite + ChromaDB)\n\n{ctx_block}"
                )

    if persistence is not None and persistence.history.get_session_by_key(key) is None:
        persistence.history.create_session(
            agent_id=resolved_agent,
            title=_new_session_title(),
            session_key=key,
            source="dashboard",
            user_id=user_id,
        )

    if persistence is not None and not user_already_saved:
        user_extras = None
        if parsed_attachments:
            meta = attachments_meta_for_persistence(
                [
                    {
                        "name": att.name,
                        "mime_type": att.mime_type,
                        "path": str(att.path) if att.path else None,
                        "size": len(att.data),
                    }
                    for att in parsed_attachments
                ]
            )
            if meta:
                user_extras = {"attachments": meta}
        persistence.history.append_message(
            key, role="user", text=text, extras=user_extras
        )
        user_already_saved = True

    tools_might_be_used = bool(chat_cfg.qclaw_tools_enabled and qclaw_tools is not None)
    resolved, vision_label = resolve_vision_model_for_attachments(
        chat_cfg,
        resolved,
        parsed_attachments,
        require_tools=tools_might_be_used,
    )
    if vision_label and on_progress:
        on_progress(f"Imagem anexada → {vision_label}")
    if attachments_include_images(parsed_attachments) and not model_supports_vision(
        resolved.entry
    ):
        return {
            "ok": False,
            "error": (
                f"O modelo {resolved.entry.label} não suporta imagens. "
                "Escolha Auto, Gemini 2.5 Flash, GPT-4o ou Claude Sonnet."
            ),
            "sessionKey": key,
        }

    hist = read_messages(
        runtime,
        session_key=key,
        agent_id=resolved_agent,
        limit=token_limits.history_limit,
        persistence=persistence,
    )
    prior = _history_to_llm_messages(
        hist.get("messages") or [],
        limit=token_limits.history_limit,
    )

    user_content = build_user_content(
        text,
        parsed_attachments,
        supports_attachments=model_supports_vision(resolved.entry),
    )

    llm_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        *prior,
        {"role": "user", "content": user_content},
    ]
    if not model_supports_vision(resolved.entry):
        llm_messages = strip_image_blocks_from_messages(llm_messages)

    # --- Context size guard: trim history from the oldest end if total exceeds budget.
    model_ctx = getattr(resolved.entry, "max_context_chars", 0) or 0
    max_ctx = effective_max_context_chars(chat_cfg, model_max_context_chars=model_ctx)
    if max_ctx and max_ctx > 0:
        llm_messages = _trim_messages_to_budget(llm_messages, max_ctx)

    timeout = max(agent_cfg.timeout_seconds, 30.0)
    timeout = min(timeout, chat_cfg.send_timeout_seconds)

    use_tools = bool(
        chat_cfg.qclaw_tools_enabled
        and qclaw_tools is not None
        and model_supports_tools(resolved)
    )
    bin_path, _env = (
        _openclaw_cli_env(runtime, qclaw_tools.doctor_cfg)
        if qclaw_tools is not None
        else (None, None)
    )
    with latency.phase("tools"):
        tools_full = (
            _qclaw_chat_tool_catalogue(
                qclaw_tools.recovery,
                cli_available=bool(bin_path)
                and bool(getattr(chat_cfg, "openclaw_connection_enabled", True)),
                shell_enabled=chat_cfg.shell_enabled,
                desktop_control_enabled=_desktop_control_enabled_for_host(chat_cfg),
                hermes_agent_create_enabled=qclaw_tools.hermes_agent_create is not None,
                kanban_create_card_enabled=qclaw_tools.kanban_create_card is not None,
                whatsapp_send_enabled=qclaw_tools.whatsapp_send is not None,
                heygen_mcp_enabled=bool(chat_cfg.heygen_mcp_enabled),
                suno_mcp_enabled=bool(chat_cfg.suno_mcp_enabled),
                seedance_mcp_enabled=bool(chat_cfg.seedance_mcp_enabled),
                runway_mcp_enabled=bool(chat_cfg.runway_mcp_enabled),
                virtual_band_enabled=bool(chat_cfg.virtual_band_enabled),
                roteiro_thumbnails_enabled=bool(chat_cfg.roteiro_thumbnails_enabled),
                roteiro_mongo_enabled=bool(chat_cfg.roteiro_mongo_enabled),
                roteiro_api_enabled=bool(chat_cfg.roteiro_api_enabled),
                allowlist_enabled=qclaw_tools.allowlist_list is not None,
                external_mcp_enabled=bool(chat_cfg.external_mcp_enabled),
            )
            if use_tools and qclaw_tools is not None
            else []
        )
        from .gois_lite import filter_lite_chat_tools, is_gois_lite

        if is_gois_lite() and tools_full:
            tools_full = filter_lite_chat_tools(tools_full)
    history_snippet = "\n".join(
        str(m.get("content") or "")[:400]
        for m in prior[-6:]
        if m.get("role") in ("user", "assistant")
    )
    tools = select_chat_tools(
        tools_full,
        chat_cfg=chat_cfg,
        user_text=text,
        history_text=history_snippet,
    )
    tools_dropped: list[str] = []
    mode_tool_cap = int(token_limits.max_tools_cap or 0)
    if tools and mode_tool_cap > 0 and len(tools) > mode_tool_cap:
        tools, mode_dropped = cap_llm_tools(tools, limit=mode_tool_cap)
        tools_dropped.extend(mode_dropped)
    tool_limit = resolve_model_tool_limit(resolved)
    if tools and tool_limit is not None and len(tools) > tool_limit:
        tools, tools_dropped = cap_llm_tools(tools, limit=tool_limit)
        if tools_dropped:
            note = tools_cap_system_note(tools_dropped, limit=tool_limit)
            if note and llm_messages and llm_messages[0].get("role") == "system":
                llm_messages[0] = {
                    **llm_messages[0],
                    "content": str(llm_messages[0].get("content") or "") + note,
                }
            log.warning(
                "openclaw chat: capped tools %d -> %d for model %s (dropped %d)",
                len(tools) + len(tools_dropped),
                len(tools),
                resolved.entry.id,
                len(tools_dropped),
            )

    maybe_log_prompt_sizes(
        chat_cfg,
        session_key=key,
        system_chars=len(str(llm_messages[0].get("content") or "")) if llm_messages else 0,
        tools_count=len(tools),
        tools_chars=len(json.dumps(tools, ensure_ascii=False)) if tools else 0,
        history_count=len(prior),
    )

    log.info(
        "openclaw chat llm: key=%s agent=%s model=%s provider=%s token_mode=%s history=%d skills=%d tools=%d/%d attachments=%d",
        key,
        resolved_agent,
        resolved.entry.model,
        resolved.entry.provider,
        token_limits.mode,
        len(prior),
        len(catalog.get("skills") or []),
        len(tools),
        len(tools_full),
        len(parsed_attachments),
    )

    tools_used: list[str] = []
    teams_created: list[dict[str, Any]] = []
    mcp_skills_used: list[str] = []
    wacli_qr_extra: Optional[dict[str, Any]] = None
    desktop_shot_extra: Optional[dict[str, Any]] = None
    media_extra: dict[str, list[dict[str, Any]]] = {"images": [], "videos": []}
    attachments_extra: list[dict[str, Any]] = []
    background_meta: Optional[dict[str, Any]] = None
    publish_chat_status(
        with_model_prefix("A preparar resposta…", resolved.entry.label),
        persistence=persistence,
        session_key=key,
        on_progress=on_progress,
    )
    _llm_t0 = __import__('time').perf_counter()
    try:
        if resolved.entry.provider == "codex_cli":
            if parsed_attachments:
                return _return({
                    "ok": False,
                    "error": (
                        "Codex CLI (ChatGPT) não suporta anexos — "
                        "use outro modelo ou envie só texto"
                    ),
                    "sessionKey": key,
                })
            from .codex_cli import build_codex_prompt, run_codex_exec
            from .local_paths import project_stack_root

            codex_prompt = build_codex_prompt(
                system_content,
                [m for m in llm_messages if m.get("role") != "system"],
            )
            codex_cwd = project_stack_root().parent
            with _progress_heartbeat(
                "A consultar Codex CLI",
                model_label=resolved.entry.label,
                persistence=persistence,
                session_key=key,
                on_progress=on_progress,
            ):
                reply = run_codex_exec(
                    codex_prompt,
                    cwd=codex_cwd,
                    timeout=timeout,
                )
            _record_llm_call_attempt(
                progress_job_id,
                model_id=resolved.entry.id,
                model_label=resolved.entry.label,
                provider=resolved.entry.provider,
                ok=bool(str(reply or "").strip()),
                error="" if str(reply or "").strip() else "empty response",
            )
        elif resolved.entry.provider == "anthropic":
            if use_tools and tools:
                return _return({
                    "ok": False,
                    "error": (
                        "ferramentas qclaw não estão disponíveis com Claude nesta versão — "
                        "escolha DeepSeek ou OpenAI"
                    ),
                    "sessionKey": key,
                })
            with _progress_heartbeat(
                "A consultar Claude",
                model_label=resolved.entry.label,
                persistence=persistence,
                session_key=key,
                on_progress=on_progress,
            ):
                reply = anthropic_messages_create(
                    resolved,
                    system=system_content,
                    messages=[m for m in llm_messages if m.get("role") != "system"],
                    timeout=timeout,
                )
            _record_llm_call_attempt(
                progress_job_id,
                model_id=resolved.entry.id,
                model_label=resolved.entry.label,
                provider=resolved.entry.provider,
                ok=bool(str(reply or "").strip()),
                error="" if str(reply or "").strip() else "empty response",
            )
        else:
            from .llm_gateway import trace_context

            client = build_openai_client(resolved, timeout=timeout)
            with trace_context(
                name="chat.completion",
                session_id=key,
                profile=resolved_agent or "",
                model_label=resolved.entry.label,
                tags=["chat"],
            ):
                if use_tools and qclaw_tools is not None and tools:
                    max_iters = token_limits.max_tool_iterations or agent_cfg.max_tool_iterations
                    reply, tools_used, mcp_skills_used, wacli_qr_extra, desktop_shot_extra, media_extra, attachments_extra, background_meta, teams_created = (
                        _deepseek_completion_with_qclaw_tools(
                        client,
                        model=resolved.entry.model,
                        messages=llm_messages,
                        tools=tools,
                        tools_full=tools_full,
                        model_id=resolved.entry.id,
                        base_url=resolved.entry.base_url,
                        tool_ctx=qclaw_tools,
                        runtime=runtime,
                        chat_cfg=chat_cfg,
                        max_iterations=max_iters,
                        on_progress=on_progress,
                        persistence=persistence,
                        project_store=project_store,
                        session_key=key,
                        progress_job_id=progress_job_id,
                        model_label=resolved.entry.label,
                        model_entry=resolved.entry,
                    )
                    )
                else:
                    if use_tools and not tools:
                        log.warning(
                            "openclaw chat: tools enabled but none selected for model %s "
                            "(full=%d, token_mode=%s)",
                            resolved.entry.id,
                            len(tools_full),
                            token_limits.mode,
                        )
                    with _progress_heartbeat(
                        "A consultar o modelo",
                        model_label=resolved.entry.label,
                        persistence=persistence,
                        session_key=key,
                        on_progress=on_progress,
                    ):
                        reply = _stream_completion(
                            client,
                            model=resolved.entry.model,
                            messages=llm_messages,
                            progress_job_id=progress_job_id,
                            model_entry=resolved.entry,
                            model_id=resolved.entry.id,
                            model_label=resolved.entry.label,
                            provider=resolved.entry.provider,
                        )
    except RateLimitError as e:
        err_msg = str(e)
        # If error is due to request size (tokens), try again with halved context
        if "Requested" in err_msg and "tokens" in err_msg.lower():
            log.warning(
                "chat llm rate limit (request too large): %s — retrying with reduced context",
                err_msg[:200],
            )
            reduced_budget = max_ctx // 2 if max_ctx else 200_000
            llm_messages_reduced = _trim_messages_to_budget(llm_messages, reduced_budget)
            try:
                if resolved.entry.provider == "anthropic":
                    reply = anthropic_messages_create(
                        resolved,
                        system=system_content,
                        messages=[m for m in llm_messages_reduced if m.get("role") != "system"],
                        timeout=timeout,
                    )
                    _record_llm_call_attempt(
                        progress_job_id,
                        model_id=resolved.entry.id,
                        model_label=resolved.entry.label,
                        provider=resolved.entry.provider,
                        ok=bool(str(reply or "").strip()),
                        error="" if str(reply or "").strip() else "empty response",
                    )
                else:
                    client = build_openai_client(resolved, timeout=timeout)
                    if use_tools and qclaw_tools is not None and tools:
                        max_iters = token_limits.max_tool_iterations or agent_cfg.max_tool_iterations
                        reply, tools_used, mcp_skills_used, wacli_qr_extra, desktop_shot_extra, media_extra, attachments_extra, background_meta, teams_created = (
                            _deepseek_completion_with_qclaw_tools(
                            client,
                            model=resolved.entry.model,
                            messages=llm_messages_reduced,
                            tools=tools,
                            tools_full=tools_full,
                            model_id=resolved.entry.id,
                            base_url=resolved.entry.base_url,
                            tool_ctx=qclaw_tools,
                            runtime=runtime,
                            chat_cfg=chat_cfg,
                            max_iterations=max_iters,
                            on_progress=on_progress,
                            persistence=persistence,
                            project_store=project_store,
                            session_key=key,
                            progress_job_id=progress_job_id,
                            model_label=resolved.entry.label,
                            model_entry=resolved.entry,
                        )
                        )
                    else:
                        reply = _stream_completion(
                            client,
                            model=resolved.entry.model,
                            messages=llm_messages_reduced,
                            progress_job_id=progress_job_id,
                            model_entry=resolved.entry,
                            model_id=resolved.entry.id,
                            model_label=resolved.entry.label,
                            provider=resolved.entry.provider,
                        )
            except Exception as retry_err:
                log.exception("chat llm retry also failed: %s", retry_err)
                return _return({
                    "ok": False,
                    "error": f"{resolved.entry.label}: {type(retry_err).__name__}: {retry_err}",
                    "sessionKey": key,
                })
        else:
            # Rate limit not due to size (e.g. RPM/TPM throttling) — surface error
            log.exception("chat llm rate limit: %s", e)
            return _return({
                "ok": False,
                "error": f"{resolved.entry.label}: {type(e).__name__}: {e}",
                "sessionKey": key,
            })
    except Exception as e:
        log.exception("chat llm failed: %s", e)
        return _return({
            "ok": False,
            "error": f"{resolved.entry.label}: {type(e).__name__}: {e}",
            "sessionKey": key,
        })

    latency._timings["llm"] = round((__import__('time').perf_counter() - _llm_t0) * 1000, 1)
    reply = reply.strip()
    if not reply:
        if has_persistable_chat_media(media_extra):
            reply = _MEDIA_ONLY_FALLBACK_REPLY
        else:
            return _return({
                "ok": False,
                "error": "model returned empty response",
                "sessionKey": key,
            })

    # Extract skill usage tag (<!--skills:name1,name2-->) from reply
    reply, skills_used = _extract_skills_used(reply)
    # Extract an interactive question marker (option chips / text input prompt).
    reply, interactive_question = extract_interactive_question(reply)
    reply = reply.strip()
    if not reply:
        if has_persistable_chat_media(media_extra):
            reply = _MEDIA_ONLY_FALLBACK_REPLY
        else:
            return _return({
                "ok": False,
                "error": "model returned empty response",
                "sessionKey": key,
            })

    # Fallback: infer skills from tools used when the LLM did not emit the tag
    if not skills_used and tools_used:
        skills_used = _infer_skills_from_tools(
            tools_used, catalog.get("skills") or []
        )

    session_id, _entry = _ensure_session_entry(
        runtime,
        session_key=key,
        agent_id=resolved_agent,
    )
    _append_transcript_message(
        runtime, agent_id=resolved_agent, session_id=session_id, role="user", text=text
    )
    _append_transcript_message(
        runtime,
        agent_id=resolved_agent,
        session_id=session_id,
        role="assistant",
        text=reply,
    )

    publish_chat_status(
        with_model_prefix("A guardar resposta…", resolved.entry.label),
        persistence=persistence,
        session_key=key,
        on_progress=on_progress,
    )

    if persistence is not None:
        try:
            # Combine tools and skills into a single list for storage.
            # Skills are prefixed with "skill:" to distinguish from tools.
            combined_tools = list(tools_used) if tools_used else []
            if skills_used:
                combined_tools.extend(f"skill:{s}" for s in skills_used)
            # Build extras dict for persistent media/QR/screenshot rendering.
            persist_extras: Optional[dict[str, Any]] = None
            _pe: dict[str, Any] = {}
            if media_extra and (
                media_extra.get("images")
                or media_extra.get("videos")
                or media_extra.get("templatePreview")
                or media_extra.get("miniCurriculumPreview")
                or media_extra.get("creativeWidget")
            ):
                _pe["media"] = {
                    "images": media_extra.get("images") or [],
                    "videos": media_extra.get("videos") or [],
                }
                if media_extra.get("templatePreview"):
                    _pe["templatePreview"] = media_extra["templatePreview"]
                    _pe["media"]["templatePreview"] = media_extra["templatePreview"]
                if media_extra.get("miniCurriculumPreview"):
                    _pe["miniCurriculumPreview"] = media_extra["miniCurriculumPreview"]
                    _pe["media"]["miniCurriculumPreview"] = media_extra["miniCurriculumPreview"]
                if media_extra.get("creativeWidget"):
                    _pe["creativeWidget"] = media_extra["creativeWidget"]
                    _pe["media"]["creativeWidget"] = media_extra["creativeWidget"]
                if media_extra.get("latexFiguresOpen"):
                    _pe["latexFiguresOpen"] = media_extra["latexFiguresOpen"]
            if wacli_qr_extra:
                _pe["wacliQr"] = wacli_qr_extra
            if desktop_shot_extra:
                _pe["desktopScreenshot"] = desktop_shot_extra
            if attachments_extra:
                _pe["attachments"] = attachments_extra
            if mcp_skills_used:
                _pe["mcpSkillsUsed"] = mcp_skills_used
            if interactive_question:
                _pe["interactiveQuestion"] = interactive_question
            _pe["modelLabel"] = resolved.entry.label
            _pe["modelId"] = resolved.entry.id
            persist_extras = _pe
            persistence.remember_exchange(
                key,
                user_text=text,
                assistant_text=reply,
                tools_used=combined_tools or None,
                extras=persist_extras,
                agent_id=resolved_agent,
                user_already_saved=user_already_saved,
            )
        except Exception as e:
            log.exception("could not persist chat exchange to SQLite: %s", e)

    import threading
    resolved_title = None  # Title will arrive asynchronously
    def _bg_rename():
        try:
            _maybe_auto_rename_session(
                runtime,
                chat_cfg,
                agent_cfg,
                session_key=key,
                agent_id=resolved_agent,
                user_text=text,
                assistant_text=reply,
                api_key=resolved.api_key,
                timeout=timeout,
                persistence=persistence,
                title_model=resolved.entry.model,
                title_base_url=resolved.entry.base_url,
            )
            clear_session_list_cache()
        except Exception:
            log.debug("async auto-rename failed", exc_info=True)
    threading.Thread(target=_bg_rename, daemon=True, name="chat-auto-rename").start()


    out: dict[str, Any] = {
        "ok": True,
        "sessionKey": key,
        "reply": reply,
        "backend": "deepseek",
        "model": resolved.entry.model,
        "modelId": resolved.entry.id,
        "modelLabel": resolved.entry.label,
        "provider": resolved.entry.provider,
        "agentId": resolved_agent,
        "title": resolved_title,
    }
    if tools_used:
        out["qclawToolsUsed"] = tools_used
    if teams_created:
        out["teamsCreated"] = teams_created
    if skills_used:
        out["skillsUsed"] = skills_used
    if mcp_skills_used:
        out["mcpSkillsUsed"] = mcp_skills_used
    if interactive_question:
        out["interactiveQuestion"] = interactive_question
    if wacli_qr_extra:
        out["wacliQr"] = wacli_qr_extra
        out["text"] = reply
    if desktop_shot_extra:
        out["desktopScreenshot"] = desktop_shot_extra
    if media_extra and has_persistable_chat_media(media_extra):
        media_out = build_chat_media_out(media_extra)
        out["media"] = media_out
        if media_extra.get("templatePreview"):
            out["templatePreview"] = media_extra["templatePreview"]
            out["media"]["templatePreview"] = media_extra["templatePreview"]
        if media_extra.get("miniCurriculumPreview"):
            out["miniCurriculumPreview"] = media_extra["miniCurriculumPreview"]
            out["media"]["miniCurriculumPreview"] = media_extra["miniCurriculumPreview"]
        if media_extra.get("creativeWidget"):
            out["creativeWidget"] = media_extra["creativeWidget"]
            out["media"]["creativeWidget"] = media_extra["creativeWidget"]
        if media_extra.get("latexFiguresOpen"):
            out["latexFiguresOpen"] = media_extra["latexFiguresOpen"]
        if media_extra.get("roteiroLabOpen"):
            out["roteiroLabOpen"] = media_extra["roteiroLabOpen"]
        if media_extra.get("bookEditorOpen"):
            out["bookEditorOpen"] = media_extra["bookEditorOpen"]
        if media_extra.get("bookCoverEditorOpen"):
            out["bookCoverEditorOpen"] = media_extra["bookCoverEditorOpen"]
        if media_extra.get("socialPostEditorOpen"):
            out["socialPostEditorOpen"] = media_extra["socialPostEditorOpen"]
    if attachments_extra:
        out["attachments"] = attachments_extra
    if background_meta:
        out.update(background_meta)
    latency.log_summary()
    out["latency"] = latency.report()
    return _return(out)


def _team_info_for_chat_shortcuts(
    team_info: Optional[dict[str, Any]],
    *,
    persistence: Optional[ChatPersistence],
    session_key: str,
) -> Optional[dict[str, Any]]:
    """Ensure team context is available for LaTeX/editor fast paths."""
    if team_info and str(team_info.get("id") or "").strip():
        return team_info
    if persistence is None:
        return team_info
    try:
        sess_row = persistence.history.get_session_by_key(session_key)
    except Exception:
        return team_info
    if sess_row is None or not sess_row.team_id:
        return team_info
    tid = str(sess_row.team_id).strip()
    if not tid:
        return team_info
    if team_info:
        merged = dict(team_info)
        merged.setdefault("id", tid)
        return merged
    return {"id": tid}


def send_chat_message(
    runtime: QclawRuntime,
    doctor_cfg: OpenclawDoctorConfig,
    chat_cfg: OpenclawChatConfig,
    agent_cfg: AgentConfig,
    *,
    session_key: str,
    message: str,
    agent_id: Optional[str] = None,
    model_id: Optional[str] = None,
    brief_target: Optional[str] = None,
    image_model: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    qclaw_tools: Optional[QclawChatToolContext] = None,
    persistence: Optional[ChatPersistence] = None,
    project_store: Optional[Any] = None,
    user_id: Optional[str] = None,
    team_info: Optional[dict[str, Any]] = None,
    on_progress: Optional[ChatProgressReporter] = None,
    user_already_saved: bool = False,
    progress_job_id: Optional[str] = None,
    preloaded_skills_catalog: Optional[dict] = None,
) -> dict:
    """Route chat send to DeepSeek+skills or openclaw CLI per openclaw_chat.backend."""

    shortcut_team = _team_info_for_chat_shortcuts(
        team_info,
        persistence=persistence,
        session_key=session_key,
    )

    # 1. Check for programmatic disambiguation
    disambig = DisambiguationManager().check_disambiguation(
        message,
        team_info=shortcut_team,
    )
    if disambig is not None:
        publish_chat_status(
            "Resposta gerada (disambiguação).",
            persistence=persistence,
            session_key=session_key,
            on_progress=on_progress,
        )
        if persistence is not None:
            extras = None
            if (
                disambig.get("interactiveQuestion")
                or disambig.get("academicToolsBar")
                or disambig.get("latexEditorOpen")
            ):
                extras = {}
                if disambig.get("interactiveQuestion"):
                    extras["interactiveQuestion"] = disambig["interactiveQuestion"]
                if disambig.get("academicToolsBar"):
                    extras["academicToolsBar"] = True
                if disambig.get("latexEditorOpen"):
                    extras["latexEditorOpen"] = disambig["latexEditorOpen"]
            try:
                persistence.remember_exchange(
                    session_key,
                    user_text=message,
                    assistant_text=disambig["reply"],
                    agent_id=agent_id or "main",
                    user_already_saved=user_already_saved,
                    extras=extras,
                )
            except Exception as e:
                log.exception("could not persist disambiguation exchange to SQLite: %s", e)
        return {
            "ok": True,
            "sessionKey": session_key,
            "text": disambig["reply"],
            "interactiveQuestion": disambig.get("interactiveQuestion"),
            "academicToolsBar": disambig.get("academicToolsBar"),
            "latexEditorOpen": disambig.get("latexEditorOpen"),
        }

    fastpath_enabled = bool(getattr(chat_cfg, "fastpath_enabled", True))
    if fastpath_enabled:
        from .chat_creative_widget_fastpath import try_creative_widget_fastpath
        from .profile_from_photo_fastpath import try_profile_from_photo_fastpath
        from .trello_json_import_fastpath import try_trello_json_import_fastpath
        from .whatsapp_contact_fastpath import try_whatsapp_contact_fastpath

        profile_photo_fast = try_profile_from_photo_fastpath(
            message,
            attachments,
            chat_cfg=chat_cfg,
            user_id=str(user_id or "").strip(),
        )
        if profile_photo_fast is not None:
            publish_chat_status(
                "A analisar foto e guardar aparência…",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            extras_pf: dict[str, Any] = {}
            if profile_photo_fast.get("media"):
                extras_pf["media"] = profile_photo_fast["media"]
            if profile_photo_fast.get("skillsUsed"):
                extras_pf["skillsUsed"] = profile_photo_fast["skillsUsed"]
            if persistence is not None:
                try:
                    persistence.remember_exchange(
                        session_key,
                        user_text=message,
                        assistant_text=profile_photo_fast["reply"],
                        extras=extras_pf or None,
                        agent_id=agent_id or "main",
                        user_already_saved=user_already_saved,
                        tools_used=["qclaw_profile_from_photo"],
                    )
                except Exception as e:
                    log.exception("could not persist profile-from-photo fastpath: %s", e)
            out_pf: dict[str, Any] = {
                "ok": True,
                "sessionKey": session_key,
                "text": profile_photo_fast["reply"],
                "reply": profile_photo_fast["reply"],
                "fastpath": profile_photo_fast.get("fastpath"),
                "skillsUsed": profile_photo_fast.get("skillsUsed"),
            }
            if profile_photo_fast.get("media"):
                out_pf["media"] = profile_photo_fast["media"]
            return out_pf

        trello_fast = try_trello_json_import_fastpath(
            message,
            attachments,
            chat_cfg=chat_cfg,
            team_info=shortcut_team,
        )
        if trello_fast is not None:
            publish_chat_status(
                "Import Trello JSON (anexo).",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            if persistence is not None:
                try:
                    persistence.remember_exchange(
                        session_key,
                        user_text=message,
                        assistant_text=trello_fast["reply"],
                        agent_id=agent_id or "main",
                        user_already_saved=user_already_saved,
                    )
                except Exception as e:
                    log.exception("could not persist trello json fastpath: %s", e)
            return {
                "ok": True,
                "sessionKey": session_key,
                "text": trello_fast["reply"],
                "reply": trello_fast["reply"],
                "fastpath": trello_fast.get("fastpath"),
            }

        widget_fast = try_creative_widget_fastpath(message, team_info=shortcut_team)
        if widget_fast is not None:
            publish_chat_status(
                "Editor nativo aberto no chat.",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            widget = widget_fast.get("creativeWidget")
            extras: dict[str, Any] = {}
            if isinstance(widget, dict):
                extras["creativeWidget"] = widget
                extras["media"] = {"creativeWidget": widget}
            if widget_fast.get("interactiveQuestion"):
                extras["interactiveQuestion"] = widget_fast["interactiveQuestion"]
            if widget_fast.get("latexEditorOpen"):
                extras["latexEditorOpen"] = widget_fast["latexEditorOpen"]
            if widget_fast.get("latexFiguresOpen"):
                extras["latexFiguresOpen"] = widget_fast["latexFiguresOpen"]
            if widget_fast.get("roteiroLabOpen"):
                extras["roteiroLabOpen"] = widget_fast["roteiroLabOpen"]
            if widget_fast.get("bookEditorOpen"):
                extras["bookEditorOpen"] = widget_fast["bookEditorOpen"]
            if widget_fast.get("bookCoverEditorOpen"):
                extras["bookCoverEditorOpen"] = widget_fast["bookCoverEditorOpen"]
            if widget_fast.get("socialPostEditorOpen"):
                extras["socialPostEditorOpen"] = widget_fast["socialPostEditorOpen"]
            if persistence is not None:
                try:
                    persistence.remember_exchange(
                        session_key,
                        user_text=message,
                        assistant_text=widget_fast["reply"],
                        extras=extras or None,
                        agent_id=agent_id or "main",
                        user_already_saved=user_already_saved,
                    )
                except Exception as e:
                    log.exception("could not persist creative widget fastpath: %s", e)
            out_fast: dict[str, Any] = {
                "ok": True,
                "sessionKey": session_key,
                "text": widget_fast["reply"],
                "reply": widget_fast["reply"],
                "fastpath": widget_fast.get("fastpath"),
            }
            if widget_fast.get("interactiveQuestion"):
                out_fast["interactiveQuestion"] = widget_fast["interactiveQuestion"]
            if widget_fast.get("latexEditorOpen"):
                out_fast["latexEditorOpen"] = widget_fast["latexEditorOpen"]
            if widget_fast.get("latexFiguresOpen"):
                out_fast["latexFiguresOpen"] = widget_fast["latexFiguresOpen"]
            if widget_fast.get("roteiroLabOpen"):
                out_fast["roteiroLabOpen"] = widget_fast["roteiroLabOpen"]
            if widget_fast.get("bookEditorOpen"):
                out_fast["bookEditorOpen"] = widget_fast["bookEditorOpen"]
            if widget_fast.get("bookCoverEditorOpen"):
                out_fast["bookCoverEditorOpen"] = widget_fast["bookCoverEditorOpen"]
            if widget_fast.get("socialPostEditorOpen"):
                out_fast["socialPostEditorOpen"] = widget_fast["socialPostEditorOpen"]
            if isinstance(widget, dict):
                out_fast["creativeWidget"] = widget
                out_fast["media"] = {"creativeWidget": widget}
            return out_fast

        wa_fast = try_whatsapp_contact_fastpath(message)
        if wa_fast is not None:
            publish_chat_status(
                "Contatos WhatsApp (busca rápida).",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            if persistence is not None:
                try:
                    persistence.remember_exchange(
                        session_key,
                        user_text=message,
                        assistant_text=wa_fast["reply"],
                        agent_id=agent_id or "main",
                        user_already_saved=user_already_saved,
                    )
                except Exception as e:
                    log.exception("could not persist whatsapp fastpath exchange: %s", e)
            return {
                "ok": True,
                "sessionKey": session_key,
                "text": wa_fast["reply"],
                "fastpath": wa_fast.get("fastpath"),
            }

    from .whatsapp_send_policy import CONTEXT_CHAT, whatsapp_outbound_scope

    with whatsapp_outbound_scope(CONTEXT_CHAT):
        backend = (chat_cfg.backend or "deepseek").strip().lower()
        connection_enabled = bool(
            getattr(chat_cfg, "openclaw_connection_enabled", True)
        )
        # When the OpenClaw connection is turned off, the chat runs web-only:
        # force the LLM backend even if config asks for the OpenClaw CLI.
        if not connection_enabled and backend in ("openclaw_cli", "openclaw", "cli"):
            log.info("openclaw connection disabled; forcing web (deepseek) backend")
            backend = "deepseek"
        if backend in ("deepseek", "llm"):
            primary = send_message_deepseek(
                runtime,
                agent_cfg,
                chat_cfg,
                session_key=session_key,
                message=message,
                agent_id=agent_id,
                model_id=model_id,
                brief_target=brief_target,
                image_model=image_model,
                attachments=attachments,
                qclaw_tools=qclaw_tools,
                persistence=persistence,
                project_store=project_store,
                user_id=user_id,
                team_info=team_info,
                on_progress=on_progress,
                user_already_saved=user_already_saved,
                progress_job_id=progress_job_id,
                preloaded_skills_catalog=preloaded_skills_catalog,
            )
            if primary.get("ok"):
                return primary
            primary_error = str(primary.get("error") or "")
            from .chat_models import (
                attachments_payload_include_images,
                is_llm_retriable_error_text,
                iter_chat_model_fallback_ids,
                iter_vision_model_fallback_ids,
            )

            has_image_attachments = attachments_payload_include_images(attachments)
            non_fallback = (
                "message or attachments required",
                "session_key is required",
            )
            hard_fail = any(token in primary_error.lower() for token in non_fallback)
            should_fallback = bool(primary_error) and not hard_fail and (
                is_llm_retriable_error_text(primary_error) or has_image_attachments
            )
            if should_fallback:
                chain = (
                    iter_vision_model_fallback_ids(chat_cfg, model_id)
                    if has_image_attachments
                    else iter_chat_model_fallback_ids(chat_cfg, model_id)
                )
                tried = {
                    str(x).strip()
                    for x in (
                        model_id,
                        primary.get("modelId"),
                    )
                    if x and str(x).strip()
                }
                last_alt_error = primary_error
                for alt_id in chain:
                    if alt_id in tried:
                        continue
                    tried.add(alt_id)
                    publish_chat_status(
                        (
                            f"A tentar fallback visão ({alt_id})…"
                            if has_image_attachments
                            else f"A tentar fallback LLM ({alt_id})…"
                        ),
                        persistence=persistence,
                        session_key=session_key,
                        on_progress=on_progress,
                    )
                    alt = send_message_deepseek(
                        runtime,
                        agent_cfg,
                        chat_cfg,
                        session_key=session_key,
                        message=message,
                        agent_id=agent_id,
                        model_id=alt_id,
                        brief_target=brief_target,
                        image_model=image_model,
                        attachments=attachments,
                        qclaw_tools=qclaw_tools,
                        persistence=persistence,
                        project_store=project_store,
                        user_id=user_id,
                        team_info=team_info,
                        on_progress=on_progress,
                        user_already_saved=user_already_saved,
                        progress_job_id=progress_job_id,
                        preloaded_skills_catalog=preloaded_skills_catalog,
                    )
                    if alt.get("ok"):
                        alt["llm_fallback_from"] = model_id or (chain[0] if chain else None)
                        alt["llm_fallback_reason"] = primary_error
                        return alt
                    last_alt_error = str(alt.get("error") or last_alt_error)
                primary_error = last_alt_error

            if (
                "insufficient_quota" in primary_error
                or "insufficient balance" in primary_error.lower()
                or "quota" in primary_error.lower()
            ):
                model_label = primary_error.split(":")[0] if ":" in primary_error else "Modelo"
                quota_msg = (
                    f"⚠️ **Cota do modelo esgotada** ({model_label}).\n\n"
                    "Nenhum modelo alternativo disponível respondeu. "
                    "Configure outra chave de API ou escolha um modelo diferente no seletor."
                )
                publish_chat_status(
                    "Cota esgotada em todos os modelos tentados.",
                    persistence=persistence,
                    session_key=session_key,
                    on_progress=on_progress,
                )
                if persistence is not None:
                    try:
                        persistence.remember_exchange(
                            session_key,
                            user_text=message,
                            assistant_text=quota_msg,
                            agent_id=agent_id or "main",
                            user_already_saved=user_already_saved,
                        )
                    except Exception as e:
                        log.exception("could not persist chat exchange quota error to SQLite: %s", e)
                return {
                    "ok": True,
                    "text": quota_msg,
                    "sessionKey": session_key,
                    "quota_exhausted": True,
                }
            # Resiliency fallback: if LLM path fails, try native OpenClaw CLI.
            # This keeps chat usable during transient API/provider issues, but
            # is skipped when the OpenClaw connection is disabled (web-only mode).
            fallback: Optional[dict] = None
            if connection_enabled:
                log.warning(
                    "openclaw chat deepseek failed; falling back to CLI: %s",
                    primary_error or "unknown error",
                )
                publish_chat_status(
                    "A tentar fallback via OpenClaw CLI…",
                    persistence=persistence,
                    session_key=session_key,
                    on_progress=on_progress,
                )
                fallback = send_message(
                    runtime,
                    doctor_cfg,
                    chat_cfg,
                    session_key=session_key,
                    message=message,
                    agent_id=agent_id,
                    persistence=persistence,
                    agent_cfg=agent_cfg,
                    on_progress=on_progress,
                )
                if fallback.get("ok"):
                    fallback["fallback_from"] = "deepseek"
                    fallback["fallback_reason"] = primary_error
                    return fallback
            else:
                log.warning(
                    "openclaw chat deepseek failed and connection is disabled; "
                    "skipping CLI fallback: %s",
                    primary_error or "unknown error",
                )
            # Both paths failed (or fallback disabled) — show error in preview
            # instead of a hard failure.
            if fallback is not None:
                combined_error = (
                    f"{primary.get('error') or 'erro desconhecido'} | "
                    f"{fallback.get('error') or 'erro desconhecido'}"
                )
            else:
                combined_error = str(primary.get("error") or "erro desconhecido")
            log.warning("openclaw chat: both deepseek and CLI fallback failed: %s", combined_error)
            publish_chat_status(
                "Falha no DeepSeek e no fallback — erro exibido no chat.",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            error_text = (
                "⚠️ **Erro temporário no backend LLM**\n\n"
                f"{combined_error}\n\n"
                "Tente novamente em alguns instantes ou troque o modelo no seletor."
            )
            job_media = media_payload_from_job_id(progress_job_id)
            persist_extras: Optional[dict[str, Any]] = None
            if job_media:
                persist_extras = {"media": job_media}
            if persistence is not None:
                try:
                    persistence.remember_exchange(
                        session_key,
                        user_text=message,
                        assistant_text=error_text,
                        agent_id=agent_id or "main",
                        user_already_saved=user_already_saved,
                        extras=persist_extras,
                    )
                except Exception as e:
                    log.exception("could not persist chat exchange error to SQLite: %s", e)
            out: dict[str, Any] = {
                "ok": True,
                "text": error_text,
                "sessionKey": session_key,
                "fallback_failed": True,
            }
            if job_media:
                out["media"] = job_media
            return out
        if backend in ("openclaw_cli", "openclaw", "cli"):
            publish_chat_status(
                "A contactar o agente OpenClaw…",
                persistence=persistence,
                session_key=session_key,
                on_progress=on_progress,
            )
            return send_message(
                runtime,
                doctor_cfg,
                chat_cfg,
                session_key=session_key,
                message=message,
                agent_id=agent_id,
                persistence=persistence,
                agent_cfg=agent_cfg,
                on_progress=on_progress,
            )
        return {
            "ok": False,
            "error": f"unknown openclaw_chat.backend: {chat_cfg.backend!r}",
        }


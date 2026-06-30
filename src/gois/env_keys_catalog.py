"""Metadata for env keys shown in the admin configuration UI."""

from __future__ import annotations

from typing import Any

CATEGORY_LABELS: dict[str, str] = {
    "llm": "Modelos LLM",
    "aws": "AWS (custos e máquinas)",
    "observability": "Observabilidade",
    "memory": "Memória de swarm",
    "messaging": "Mensagens",
    "email": "E-mail",
    "auth": "Autenticação",
    "env": "Variáveis de ambiente",
    "other": "Outras",
}

# Catálogo estático de providers LLM (label, hint, base URL de referência).
LLM_KEY_CATALOG: dict[str, dict[str, str]] = {
    "DEEPSEEK_API_KEY": {
        "label": "DeepSeek",
        "hint": "Chat, recovery, agentes e modelos deepseek-*.",
        "base_url": "https://api.deepseek.com",
    },
    "DEEPSEEK_API_BASE": {
        "label": "DeepSeek base URL",
        "hint": "Override do endpoint OpenAI-compatível.",
        "base_url": "https://api.deepseek.com",
    },
    "DEEPSEEK_MODEL": {
        "label": "DeepSeek model default",
        "hint": "Modelo padrão quando não especificado (ex.: deepseek-chat).",
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI",
        "hint": "GPT-4o, GPT-5.x, Codex, transcrição de áudio.",
        "base_url": "https://api.openai.com/v1",
    },
    "GEMINI_API_KEY": {
        "label": "Google Gemini",
        "hint": "Gemini 2.x via endpoint OpenAI-compatível; Lyria (`qclaw_gemini_music_generate`).",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "GOOGLE_API_KEY": {
        "label": "Google API (alias Gemini)",
        "hint": "Fallback se GEMINI_API_KEY ausente. Não use chave de outro projeto — geração de imagem usa GEMINI_API_KEY.",
    },
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic Claude",
        "hint": "Claude Opus/Sonnet/Haiku via endpoint OpenAI-compatível.",
        "base_url": "https://api.anthropic.com/v1/",
    },
    "PERPLEXITY_API_KEY": {
        "label": "Perplexity Sonar",
        "hint": "Pesquisa com modelo sonar.",
        "base_url": "https://api.perplexity.ai",
    },
    "XAI_API_KEY": {
        "label": "xAI Grok (Imagine + chat)",
        "hint": "Grok Imagine no chat (`qclaw_grok_imagine_generate`). Importe de /chaves → Roteiro Viral ou edite aqui; fallback: MongoDB do RV (`xai_api_key`).",
        "base_url": "https://api.x.ai/v1",
    },
    "XAI_MANAGEMENT_KEY": {
        "label": "xAI Management key (billing)",
        "hint": "Saldo prepaid Grok — skill `qclaw-api-credits`. console.x.ai → Settings → Management Keys (não gasta crédito).",
        "base_url": "https://management-api.x.ai",
    },
    "XAI_TEAM_ID": {
        "label": "xAI Team ID",
        "hint": "UUID do team xAI para consulta de saldo (`qclaw-api-credits`). Visível na URL do console ou em Billing.",
    },
    "OPENAI_ADMIN_API_KEY": {
        "label": "OpenAI Admin API key",
        "hint": "Gasto agregado via /v1/organization/costs (`qclaw-api-credits`). platform.openai.com → Admin keys.",
        "base_url": "https://api.openai.com/v1",
    },
    "DASHSCOPE_API_KEY": {
        "label": "Alibaba DashScope (Qwen)",
        "hint": "Modelos Qwen via DashScope.",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "YI_API_KEY": {
        "label": "Yi (01.AI)",
        "hint": "Modelos Yi-Large.",
        "base_url": "https://api.lingyiwanwu.com/v1",
    },
    "ZHIPU_API_KEY": {
        "label": "Zhipu GLM",
        "hint": "GLM-4 via BigModel.",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "MOONSHOT_API_KEY": {
        "label": "Moonshot Kimi",
        "hint": "Kimi chat models.",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "BAICHUAN_API_KEY": {
        "label": "Baichuan",
        "hint": "Baichuan4 e similares.",
        "base_url": "https://api.baichuan-ai.com/v1",
    },
    "MISTRAL_API_KEY": {
        "label": "Mistral AI",
        "hint": "Mistral Large / Codestral (IDE).",
        "base_url": "https://api.mistral.ai/v1",
    },
    "GROQ_API_KEY": {
        "label": "Groq",
        "hint": "Inferência rápida Llama/Mixtral (IDE).",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "hint": "Gateway multi-modelo — chat (/chat) e IDE. Grupos openrouter-* e openrouter-barato (grátis/ultra-baratos).",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "QCLAW_LLM_API_KEY": {
        "label": "QClaw LLM gateway",
        "hint": "Chave customizada para proxy/gateway interno.",
    },
    "QCLAW_LLM_BASE_URL": {
        "label": "QClaw LLM base URL",
        "hint": "URL do gateway LLM interno.",
    },
    "COHERE_API_KEY": {
        "label": "Cohere",
        "hint": "Command / Embed (opcional).",
        "base_url": "https://api.cohere.com/v1",
    },
    "TOGETHER_API_KEY": {
        "label": "Together AI",
        "hint": "Modelos open-source hospedados.",
        "base_url": "https://api.together.xyz/v1",
    },
    "FIREWORKS_API_KEY": {
        "label": "Fireworks AI",
        "hint": "Llama, Mixtral e similares.",
        "base_url": "https://api.fireworks.ai/inference/v1",
    },
    "CEREBRAS_API_KEY": {
        "label": "Cerebras",
        "hint": "Inferência ultra-rápida.",
        "base_url": "https://api.cerebras.ai/v1",
    },
    "NVIDIA_API_KEY": {
        "label": "NVIDIA NIM",
        "hint": "NIM API / build.nvidia.com.",
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "LLM_API_KEY": {
        "label": "LLM API (genérico)",
        "hint": "Chave genérica usada em integrações legadas.",
    },
}

AWS_KEY_CATALOG: dict[str, dict[str, str]] = {
    "AWS_ACCESS_KEY_ID": {
        "label": "AWS Access Key ID",
        "hint": "IAM access key — Cost Explorer, EC2, RDS (`qclaw_aws_*`). Configure aqui ou use AWS_PROFILE.",
    },
    "AWS_SECRET_ACCESS_KEY": {
        "label": "AWS Secret Access Key",
        "hint": "Par da access key. Obrigatório com AWS_ACCESS_KEY_ID (salvo se usar perfil CLI).",
    },
    "AWS_SESSION_TOKEN": {
        "label": "AWS Session Token",
        "hint": "Opcional — credenciais temporárias STS / assumed role.",
    },
    "AWS_DEFAULT_REGION": {
        "label": "AWS região padrão",
        "hint": "Ex.: sa-east-1, us-east-1. Recursos EC2/RDS; Cost Explorer usa us-east-1 na API.",
    },
    "AWS_PROFILE": {
        "label": "AWS CLI profile",
        "hint": "Alternativa às chaves estáticas — perfil em ~/.aws/credentials (ex.: default).",
    },
    "QCLAW_AWS_FX_BRL": {
        "label": "FX USD→BRL (custos AWS)",
        "hint": "Taxa para converter custos AWS em reais nos relatórios (default 5,20).",
    },
}

KEY_CATALOG: dict[str, dict[str, str]] = {
    **{k: {**v, "category": "llm"} for k, v in LLM_KEY_CATALOG.items()},
    **{k: {**v, "category": "aws"} for k, v in AWS_KEY_CATALOG.items()},
    "LITELLM_MASTER_KEY": {
        "label": "LiteLLM master key",
        "category": "observability",
        "hint": "Proxy LiteLLM (llm_gateway no config).",
    },
    "LANGFUSE_PUBLIC_KEY": {
        "label": "Langfuse public key",
        "category": "observability",
    },
    "LANGFUSE_SECRET_KEY": {
        "label": "Langfuse secret key",
        "category": "observability",
    },
    "MEM0_API_KEY": {"label": "Mem0", "category": "memory"},
    "LETTA_API_KEY": {"label": "Letta", "category": "memory"},
    "TELEGRAM_BOT_TOKEN": {"label": "Telegram bot", "category": "messaging"},
    "QCLAW_TELEGRAM_BOT_TOKEN": {
        "label": "QClaw Telegram bot",
        "category": "messaging",
    },
    "HERMES_GATEWAY_TOKEN": {"label": "Hermes gateway token", "category": "messaging"},
    "GMAIL_APP_PASSWORD": {"label": "Gmail app password", "category": "email"},
    "GOOGLE_APP_PASSWORD": {"label": "Google app password", "category": "email"},
    "TEAM_SMTP_PASSWORD": {"label": "SMTP do time", "category": "email"},
    "GAMMA_API_KEY": {"label": "Gamma", "category": "other"},
    "ACEDATACLOUD_API_TOKEN": {
        "label": "AceDataCloud (Suno / Seedance)",
        "category": "other",
        "hint": "Token para MCP Suno (música) e Seedance (vídeo). Obtido em platform.acedata.cloud.",
    },
    "HEYGEN_API_KEY": {"label": "HeyGen", "category": "other"},
    "ELEVENLABS_API_KEY": {
        "label": "ElevenLabs",
        "category": "other",
        "hint": "Narração TTS no chat (`qclaw_elevenlabs_narrate`). Obtenha em elevenlabs.io/app/settings/api-keys.",
    },
    "FAL_KEY": {"label": "FAL.ai (FLUX)", "category": "other", "hint": "Geração de imagem FLUX via fal.ai. Importável do Roteiro Viral."},
    "STABILITY_API_KEY": {"label": "Stability AI", "category": "other", "hint": "Stable Diffusion 3.x — fallback de imagem RV."},
    "WAVESPEED_API_KEY": {"label": "WaveSpeed", "category": "other", "hint": "Flux via wavespeed.ai — fallback de imagem RV."},
    "BYTEPLUS_API_KEY": {"label": "BytePlus ModelArk", "category": "other", "hint": "Seedream/Seededit — fallback de imagem RV."},
    "RUNWAY_API_KEY": {"label": "Runway", "category": "other", "hint": "Vídeo/imagem Runway (Roteiro Viral)."},
    "MESHY_API_KEY": {
        "label": "Meshy AI",
        "category": "other",
        "hint": "Modelos 3D Meshy MCP — personagens, textura, rig. Obtenha em meshy.ai.",
    },
    "POSTIZ_API_KEY": {
        "label": "Postiz API",
        "category": "other",
        "hint": "Agendar/publicar social media via Postiz MCP. Settings → Developers → Public API.",
    },
    "POSTIZ_URL": {
        "label": "Postiz URL (self-hosted)",
        "category": "other",
        "hint": "Base URL da instância Postiz própria (ex: http://localhost:5000). Opcional se usar cloud.",
    },
    "REPLICATE_API_TOKEN": {
        "label": "Replicate API token",
        "category": "other",
        "hint": "Replicate MCP — concept art, Flux/SDXL, upscalers. replicate.com/account/api-tokens",
    },
    "STABILITY_AI_API_KEY": {
        "label": "Stability AI (MCP)",
        "category": "other",
        "hint": "Stability MCP — alias aceite: STABILITY_API_KEY.",
    },
    "KLING_ACCESS_KEY": {"label": "Kling access key", "category": "other", "hint": "Kling AI — par com KLING_SECRET_KEY."},
    "KLING_SECRET_KEY": {"label": "Kling secret key", "category": "other", "hint": "Kling AI — par com KLING_ACCESS_KEY."},
    "YOUTUBE_API_KEY": {"label": "YouTube Data API", "category": "other", "hint": "Thumbnails e metadados YouTube (RV)."},
    "COMIC_VINE_API_KEY": {
        "label": "Comic Vine API",
        "category": "other",
        "hint": "HQ ocidental — comicvine_search, dc-comics MCP. Obtenha em comicvine.gamespot.com/api/",
    },
    "ANILIST_TOKEN": {
        "label": "AniList API token",
        "category": "other",
        "hint": "Opcional — listas/login AniList MCP. Busca pública funciona sem token.",
    },
    "MAL_CLIENT_ID": {
        "label": "MyAnimeList client ID",
        "category": "other",
        "hint": "MCP mal — myanimelist.net/apiconfig (App Type: other).",
    },
    "APIFY_API_TOKEN": {
        "label": "Apify API token",
        "category": "other",
        "hint": "Opcional — MCP comicvine-apify (scraper Apify).",
    },
    "NOTION_API_KEY": {"label": "Notion", "category": "other"},
    "TAVILY_API_KEY": {"label": "Tavily", "category": "other"},
    "BRIGHTDATA_API_KEY": {"label": "Bright Data", "category": "other"},
    "OVERLEAF_PROJECT_ID": {
        "label": "Overleaf Project ID",
        "category": "other",
        "hint": "ID do projeto default (legado). Prefira .stack/overleaf/projects.json para multi-projeto.",
    },
    "OVERLEAF_GIT_TOKEN": {
        "label": "Overleaf Git Token",
        "category": "other",
        "hint": "Token git (olp_...) — partilhado por todos os projetos sem token próprio.",
    },
    "OVERLEAF_PROJECTS_CONFIG": {
        "label": "Overleaf projects config",
        "category": "other",
        "hint": "Caminho para .stack/overleaf/projects.json (multi-projeto com tokens).",
    },
    "SECRET_KEY": {"label": "Secret key (app)", "category": "auth"},
    "ADMIN_PASSWORD": {"label": "Admin password", "category": "auth"},
}


def _chat_model_key_envs() -> dict[str, list[str]]:
    """Map api_key_env → model ids from the built-in chat catalog."""
    from .chat_models import static_chat_model_entries
    from .openrouter_catalog import cached_openrouter_model_ids

    out: dict[str, list[str]] = {}
    for entry in static_chat_model_entries():
        env = str(entry.api_key_env or "").strip().upper()
        if not env:
            continue
        out.setdefault(env, []).append(str(entry.id))
    for mid in cached_openrouter_model_ids():
        bucket = out.setdefault("OPENROUTER_API_KEY", [])
        if mid not in bucket:
            bucket.append(mid)
    return out


def _ide_model_key_envs() -> dict[str, list[str]]:
    """Map api_key_env → provider ids from IDE runtime."""
    try:
        from .ide_runtime import _PROVIDERS
    except ImportError:
        return {}
    out: dict[str, list[str]] = {}
    for pid, spec in _PROVIDERS.items():
        if not isinstance(spec, dict) or spec.get("kind") != "openai_compat":
            continue
        env = str(spec.get("api_key_env") or "").strip().upper()
        if not env:
            continue
        out.setdefault(env, []).append(str(pid))
    return out


def llm_models_by_key_env() -> dict[str, list[str]]:
    """Union of chat + IDE models per api_key_env."""
    merged: dict[str, list[str]] = {}
    for source in (_chat_model_key_envs(), _ide_model_key_envs()):
        for env, models in source.items():
            bucket = merged.setdefault(env, [])
            for mid in models:
                if mid not in bucket:
                    bucket.append(mid)
    return merged


def llm_key_env_vars() -> tuple[str, ...]:
    """All LLM api_key_env names referenced in chat + IDE + static catalog."""
    names: list[str] = []
    seen: set[str] = set()
    for env in (
        *LLM_KEY_CATALOG.keys(),
        *llm_models_by_key_env().keys(),
    ):
        key = str(env or "").strip().upper()
        if key and key not in seen:
            seen.add(key)
            names.append(key)
    return tuple(names)


def aws_key_env_vars() -> tuple[str, ...]:
    """AWS credential and config vars for /chaves and qclaw_aws_* tools."""
    return tuple(AWS_KEY_CATALOG.keys())


def all_catalog_key_names() -> tuple[str, ...]:
    """Every key shown in the admin UI (LLM, AWS, then the rest)."""
    llm = list(llm_key_env_vars())
    aws = list(aws_key_env_vars())
    seen = set(llm) | set(aws)
    rest = sorted(k for k in KEY_CATALOG if k not in seen)
    return tuple([*llm, *aws, *rest])


def catalog_entry(name: str) -> dict[str, Any]:
    meta = KEY_CATALOG.get(name, {})
    llm_meta = LLM_KEY_CATALOG.get(name, {})
    aws_meta = AWS_KEY_CATALOG.get(name, {})
    in_catalog = name in KEY_CATALOG or name in LLM_KEY_CATALOG or name in AWS_KEY_CATALOG
    if in_catalog:
        category = str(
            meta.get("category")
            or ("llm" if name in LLM_KEY_CATALOG else "aws" if name in AWS_KEY_CATALOG else "other")
        )
    else:
        category = "env"
    models = llm_models_by_key_env().get(name, [])
    return {
        "name": name,
        "label": str(
            meta.get("label")
            or llm_meta.get("label")
            or aws_meta.get("label")
            or name.replace("_", " ").title()
        ),
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "hint": str(meta.get("hint") or llm_meta.get("hint") or aws_meta.get("hint") or ""),
        "base_url": str(meta.get("base_url") or llm_meta.get("base_url") or ""),
        "models": models,
        "model_count": len(models),
        "is_llm": category == "llm" or name in LLM_KEY_CATALOG,
        "is_aws": category == "aws" or name in AWS_KEY_CATALOG,
        "is_env": category == "env",
        "in_catalog": in_catalog,
    }

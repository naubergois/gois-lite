"""Resolve API keys: os.environ → MongoDB → .env local → projetos irmãos."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Mesma conta DeepSeek que AIManager / CGEClaw; depois Hermes e demais.
SIBLING_ENV_FILES: tuple[Path, ...] = (
    Path.home() / ".openclaw/.env",
    Path.home() / "gois/.stack/hermes/.env",
    Path.home() / "AIManager/.env",
    Path.home() / "CGEClaw/.env",
    Path.home() / ".hermes/.env",
    Path.home() / "QueimandasGemeosDigitais/ceara-queimadas/backend/.env",
    Path.home() / "MapeamentoCurso/.env",
    Path("/Volumes/NAUBER/GerenciaTreinamentos/.env"),
    Path.home() / "GerenciaTreinamentos/.env",
    Path("/Volumes/NAUBER/Eleicoes/.env"),
    Path.home() / "Eleicoes/.env",
    Path.home() / "curso_orquestracao_agentes_langchain/.env",
)

DEEPSEEK_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE",
    "DEEPSEEK_MODEL",
)

LLM_API_ENV_VARS: tuple[str, ...] = (
    *DEEPSEEK_ENV_VARS,
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY",
    "QCLAW_LLM_API_KEY",
    "QCLAW_LLM_BASE_URL",
)

# Provider keys referenced across chat_models, ide_runtime, llm_gateway, swarm_memory.
EXTRA_MANAGED_ENV_VARS: tuple[str, ...] = (
    "XAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "YI_API_KEY",
    "ZHIPU_API_KEY",
    "MOONSHOT_API_KEY",
    "BAICHUAN_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "CEREBRAS_API_KEY",
    "NVIDIA_API_KEY",
    "LLM_API_KEY",
    "GAMMA_API_KEY",
    "ACEDATACLOUD_API_TOKEN",
    "HEYGEN_API_KEY",
    "ELEVENLABS_API_KEY",
    "FAL_KEY",
    "STABILITY_API_KEY",
    "WAVESPEED_API_KEY",
    "BYTEPLUS_API_KEY",
    "RUNWAY_API_KEY",
    "REPLICATE_API_TOKEN",
    "KLING_ACCESS_KEY",
    "KLING_SECRET_KEY",
    "YOUTUBE_API_KEY",
    "NOTION_API_KEY",
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "OVERLEAF_PROJECT_ID",
    "OVERLEAF_GIT_TOKEN",
    "LITELLM_MASTER_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "MEM0_API_KEY",
    "LETTA_API_KEY",
    "TEAM_SMTP_PASSWORD",
    "GMAIL_APP_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "QCLAW_AWS_FX_BRL",
)

MANAGED_ENV_VARS: tuple[str, ...] = tuple(
    dict.fromkeys((*LLM_API_ENV_VARS, *EXTRA_MANAGED_ENV_VARS))
)


def is_managed_env_key(name: str) -> bool:
    """True for known provider keys and common secret env var suffixes."""
    key = (name or "").strip().upper()
    if not key:
        return False
    if key in MANAGED_ENV_VARS:
        return True
    if key.endswith(("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")):
        return True
    if key.endswith("_KEY"):
        return True
    return False


def is_storable_env_key(name: str) -> bool:
    """True for any valid env var name importable from .env into MongoDB."""
    key = (name or "").strip()
    if not key:
        return False
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key))


# Per-instance deployment flags — never share via MongoDB env_keys (gois vs gois-lite).
INSTANCE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "GOIS_LITE",
        "MONGODB_DB",
        "GOIS_HTTP_PORT",
        "QCLAW_MONITOR_PORT",
        "GOIS_STACK_ROOT",
        "QCLAW_BUNDLED_SKILLS_DIR",
        "HERMES_HOME",
        "GOIS_CACHE_ROOT",
    }
)


def is_instance_env_key(name: str) -> bool:
    """True for env vars that pin a deployment (full gois vs gois-lite)."""
    return (name or "").strip().upper() in INSTANCE_ENV_KEYS


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def is_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    if v.startswith("your_") or v.startswith("sk-..."):
        return True
    return v in {
        "changeme",
        "xxx",
        "placeholder",
        "sua_chave_aqui",
        "sua-chave-aqui",
    }


def deepseek_from_siblings() -> dict[str, str]:
    """Lê DEEPSEEK_* do primeiro .env irmão com chave válida."""
    merged = llm_keys_from_siblings()
    result: dict[str, str] = {}
    for var in DEEPSEEK_ENV_VARS:
        if merged.get(var) and not is_placeholder(merged[var]):
            result[var] = merged[var]
    return result


def sibling_env_file_paths() -> tuple[Path, ...]:
    """Static sibling .env paths plus Roteiro Viral project .env when present."""
    try:
        from .roteiro_viral_keys import roteiro_viral_env_path

        rv_env = roteiro_viral_env_path()
    except Exception:
        rv_env = None
    paths: list[Path] = list(SIBLING_ENV_FILES)
    if rv_env is not None and rv_env not in paths:
        paths.append(rv_env)
    return tuple(paths)


def llm_keys_from_siblings() -> dict[str, str]:
    """Merge managed keys from sibling .env files (later files do not override)."""
    merged: dict[str, str] = {}
    for path in sibling_env_file_paths():
        if not path.is_file():
            continue
        data = parse_env_file(path)
        for var, val in data.items():
            if var in merged:
                continue
            if is_managed_env_key(var) and val and not is_placeholder(val):
                merged[var] = val
    try:
        from .roteiro_viral_keys import fetch_roteiro_viral_managed_keys

        for var, val in fetch_roteiro_viral_managed_keys().items():
            if var in merged:
                continue
            if is_managed_env_key(var) and val and not is_placeholder(val):
                merged[var] = val
    except Exception:
        pass
    return merged


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_local_env_path() -> Optional[Path]:
    """Resolve project .env without relying on a live process cwd.

    Long-running monitors can outlive the directory they were started from
    (e.g. external volume unmount); ``Path.cwd()`` then raises FileNotFoundError.
    """
    for key in ("GOIS_ENV_FILE", "QCLAW_MONITOR_ENV_FILE"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return Path(raw).expanduser()
    config_raw = os.environ.get("QCLAW_MONITOR_CONFIG", "").strip()
    if config_raw:
        return Path(config_raw).expanduser().parent / ".env"
    candidates = (
        _repo_root() / ".env",
        Path.home() / "qclawmonitor" / ".env",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    try:
        return Path.cwd() / ".env"
    except FileNotFoundError:
        return None


def _keys_from_mongo() -> dict[str, str]:
    try:
        from .env_keys_mongo import get_cached_env_keys
        from .mongo import ping

        if not ping():
            return {}
        return get_cached_env_keys()
    except Exception:
        return {}


def resolve_llm_api_key(env_var: str, *, local_env: Optional[Path] = None) -> Optional[str]:
    """Resolve one API key: os.environ > MongoDB > local .env > sibling projects."""
    if env_var == "XAI_API_KEY":
        from .roteiro_viral_keys import resolve_xai_api_key

        return resolve_xai_api_key(local_env=local_env)
    explicit = os.getenv(env_var, "").strip()
    if explicit and not is_placeholder(explicit):
        return explicit
    mongo_val = _keys_from_mongo().get(env_var, "")
    if mongo_val and not is_placeholder(mongo_val):
        return mongo_val
    if local_env is None:
        local_env = _default_local_env_path()
    if local_env is not None and local_env.is_file():
        val = parse_env_file(local_env).get(env_var, "")
        if val and not is_placeholder(val):
            return val
    return llm_keys_from_siblings().get(env_var)


def resolve_google_gemini_api_key(
    explicit: Optional[str] = None,
    *,
    local_env: Optional[Path] = None,
) -> Optional[str]:
    """Chave Google Gemini/Imagen: explicit > GEMINI_API_KEY > GOOGLE_API_KEY."""
    if explicit and explicit.strip() and not is_placeholder(explicit):
        return explicit.strip()
    gemini = resolve_llm_api_key("GEMINI_API_KEY", local_env=local_env)
    if gemini:
        return gemini
    return resolve_llm_api_key("GOOGLE_API_KEY", local_env=local_env)


def build_gemini_subprocess_env(
    base_env: Optional[dict[str, str]] = None,
    *,
    explicit_key: Optional[str] = None,
    local_env: Optional[Path] = None,
) -> tuple[dict[str, str], Optional[str]]:
    """Subprocess env for Google image APIs — inject GEMINI, strip conflicting GOOGLE."""
    env = dict(base_env or os.environ)
    key = resolve_google_gemini_api_key(explicit_key, local_env=local_env)
    if not key:
        return env, None
    env["GEMINI_API_KEY"] = key
    google = (env.get("GOOGLE_API_KEY") or "").strip()
    if google and google != key:
        env.pop("GOOGLE_API_KEY", None)
    return env, key


def image_generation_timeout_seconds(
    resolution: str,
    base_timeout: float,
) -> float:
    """Floor timeouts for Nano Banana / Imagen (2K/4K often exceed 180s)."""
    res = (resolution or "").strip().upper()
    floor = 180.0
    if res == "2K":
        floor = 300.0
    elif res == "4K":
        floor = 480.0
    return max(float(base_timeout), floor)


def resolve_deepseek_api_key(*, local_env: Optional[Path] = None) -> Optional[str]:
    """Chave DeepSeek: env explícita > MongoDB > .env local > projetos irmãos."""
    return resolve_llm_api_key("DEEPSEEK_API_KEY", local_env=local_env)


def apply_deepseek_to_environ(*, local_env: Optional[Path] = None) -> Optional[str]:
    """Define DEEPSEEK_* em os.environ se ausente ou placeholder."""
    return apply_llm_keys_to_environ(local_env=local_env)


def apply_llm_keys_to_environ(*, local_env: Optional[Path] = None) -> Optional[str]:
    """Load managed keys from MongoDB, local .env, and siblings into os.environ."""
    if local_env is None:
        local_env = Path.cwd() / ".env"

    def _apply(
        source: dict[str, str],
        *,
        all_keys: bool = False,
        skip_instance_keys: bool = False,
    ) -> None:
        for key, val in source.items():
            if not val or is_placeholder(val):
                continue
            if skip_instance_keys and is_instance_env_key(key):
                continue
            if all_keys:
                if not is_storable_env_key(key):
                    continue
            elif not is_managed_env_key(key):
                continue
            if not os.getenv(key) or is_placeholder(os.getenv(key, "")):
                os.environ[key] = val

    _apply(_keys_from_mongo(), all_keys=True, skip_instance_keys=True)
    if local_env.is_file():
        _apply(parse_env_file(local_env))
    _apply(llm_keys_from_siblings())
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return key if key and not is_placeholder(key) else None


def sync_deepseek_to_env_file(
    target: Path,
    *,
    source: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """Copia DEEPSEEK_* para target .env; retorna (ok, mensagem)."""
    if source is None:
        source = deepseek_from_siblings()
    if not source.get("DEEPSEEK_API_KEY"):
        return False, "nenhuma chave DeepSeek encontrada nos projetos irmãos"

    lines: list[str] = []
    if target.is_file():
        lines = target.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in DEEPSEEK_ENV_VARS and key in source:
                out.append(f"{key}={source[key]}")
                seen.add(key)
                continue
        out.append(line)

    for var in DEEPSEEK_ENV_VARS:
        if var not in seen and var in source:
            out.append(f"{var}={source[var]}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return True, f"DeepSeek sincronizado → {target}"


def upsert_env_var(env_path: Path, key_line: str) -> None:
    """Substitui ou acrescenta uma linha KEY=VALUE no .env."""
    key = key_line.split("=", 1)[0].strip()
    content = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    if re.search(rf"^{re.escape(key)}=", content, re.M):
        content = re.sub(
            rf"^{re.escape(key)}=.*$",
            key_line,
            content,
            flags=re.M,
        )
    else:
        content = (content.rstrip() + "\n" if content else "") + key_line + "\n"
    env_path.write_text(content, encoding="utf-8")

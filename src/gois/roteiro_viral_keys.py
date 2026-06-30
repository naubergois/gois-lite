"""Resolve API keys stored in the Roteiro Viral project (MongoDB via HTTP API or .env)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Roteiro Viral /config/api-keys field → QClaw env var
_RV_TO_QCLAW: dict[str, str] = {
    "xai_api_key": "XAI_API_KEY",
    "google_api_key": "GEMINI_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "fal_api_key": "FAL_KEY",
    "gamma_api_key": "GAMMA_API_KEY",
    "heygen_api_key": "HEYGEN_API_KEY",
    "stability_api_key": "STABILITY_API_KEY",
    "wavespeed_api_key": "WAVESPEED_API_KEY",
    "byteplus_api_key": "BYTEPLUS_API_KEY",
    "runway_api_key": "RUNWAY_API_KEY",
    "youtube_api_key": "YOUTUBE_API_KEY",
    "kling_access_key": "KLING_ACCESS_KEY",
    "kling_secret_key": "KLING_SECRET_KEY",
}

# Direct .env variable names in Roteiro Viral (same QClaw name when possible)
_RV_ENV_DIRECT: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY",
    "FAL_KEY",
    "STABILITY_API_KEY",
    "WAVESPEED_API_KEY",
    "BYTEPLUS_API_KEY",
    "RUNWAY_API_KEY",
    "GAMMA_API_KEY",
    "HEYGEN_API_KEY",
    "YOUTUBE_API_KEY",
    "KLING_ACCESS_KEY",
    "KLING_SECRET_KEY",
    "OPENROUTER_API_KEY",
)

IMAGE_PROVIDER_KEY_ENV: dict[str, str] = {
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "fal": "FAL_KEY",
    "stability": "STABILITY_API_KEY",
    "wavespeed": "WAVESPEED_API_KEY",
    "byteplus": "BYTEPLUS_API_KEY",
    "modelark": "BYTEPLUS_API_KEY",
    "klingai": "KLING_ACCESS_KEY",
    "runway": "RUNWAY_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
}


def roteiro_viral_api_base() -> str:
    return (os.environ.get("ROTEIRO_VIRAL_API") or "http://127.0.0.1:8000").rstrip("/")


def roteiro_viral_env_path() -> Path:
    raw = os.environ.get("ROTEIRO_VIRAL_PATH", "/Volumes/NAUBER/roteiroviral")
    return Path(raw).expanduser().resolve() / ".env"


def fetch_roteiro_viral_api_keys(
    *,
    api_base: Optional[str] = None,
    include_secrets: bool = True,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """GET /config/api-keys from the Roteiro Viral API."""
    if not include_secrets:
        return {}
    base = (api_base or roteiro_viral_api_base()).rstrip("/")
    url = f"{base}/config/api-keys"
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params={"include_secrets": "true"})
            if resp.status_code >= 400:
                log.debug("roteiro viral api-keys HTTP %s", resp.status_code)
                return {}
            data = resp.json()
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("roteiro viral api-keys fetch failed: %s", exc)
        return {}


def fetch_roteiro_viral_env_file_keys() -> dict[str, str]:
    """Read managed keys from Roteiro Viral project .env (ROTEIRO_VIRAL_PATH)."""
    from .secrets_fallback import is_placeholder, parse_env_file

    path = roteiro_viral_env_path()
    if not path.is_file():
        return {}
    parsed = parse_env_file(path)
    out: dict[str, str] = {}
    for name in _RV_ENV_DIRECT:
        val = str(parsed.get(name) or "").strip()
        if val and not is_placeholder(val):
            out[name] = val
    gemini = out.get("GEMINI_API_KEY", "")
    google = out.get("GOOGLE_API_KEY", "")
    if not gemini and google:
        out["GEMINI_API_KEY"] = google
    return out


def map_roteiro_viral_keys(raw: dict[str, Any]) -> dict[str, str]:
    """Map RV config/api-keys payload to QClaw env var names."""
    from .secrets_fallback import is_placeholder

    out: dict[str, str] = {}
    for rv_field, env_name in _RV_TO_QCLAW.items():
        val = str(raw.get(rv_field) or "").strip()
        if val and not is_placeholder(val):
            out[env_name] = val
    return out


def fetch_roteiro_viral_managed_keys(
    *,
    api_base: Optional[str] = None,
    timeout: float = 12.0,
) -> dict[str, str]:
    """Managed keys from Roteiro Viral API + local RV .env file."""
    merged: dict[str, str] = {}
    merged.update(fetch_roteiro_viral_env_file_keys())
    for key, val in map_roteiro_viral_keys(
        fetch_roteiro_viral_api_keys(
            api_base=api_base,
            include_secrets=True,
            timeout=timeout,
        )
    ).items():
        if key not in merged:
            merged[key] = val
    return merged


def provider_has_image_credentials(provider: str) -> bool:
    from .secrets_fallback import resolve_llm_api_key

    prov = (provider or "").strip().lower()
    if prov == "roteiro_viral_api":
        return True
    env_var = IMAGE_PROVIDER_KEY_ENV.get(prov)
    if not env_var:
        return False
    if prov == "klingai":
        access = resolve_llm_api_key("KLING_ACCESS_KEY")
        secret = resolve_llm_api_key("KLING_SECRET_KEY")
        return bool(access and secret)
    if prov == "google":
        from .secrets_fallback import resolve_google_gemini_api_key

        return bool(resolve_google_gemini_api_key())
    return bool(resolve_llm_api_key(env_var))


def resolve_xai_api_key(*, local_env: Optional[Path] = None) -> Optional[str]:
    """XAI/Grok key: env → MongoDB QClaw → local .env → siblings (incl. Roteiro Viral)."""
    from .secrets_fallback import (
        _keys_from_mongo,
        is_placeholder,
        llm_keys_from_siblings,
        parse_env_file,
    )

    explicit = os.environ.get("XAI_API_KEY", "").strip()
    if explicit and not is_placeholder(explicit):
        return explicit
    mongo_val = _keys_from_mongo().get("XAI_API_KEY", "")
    if mongo_val and not is_placeholder(mongo_val):
        return mongo_val
    if local_env is None:
        local_env = Path.cwd() / ".env"
    if local_env.is_file():
        val = parse_env_file(local_env).get("XAI_API_KEY", "")
        if val and not is_placeholder(val):
            return val
    sibling = llm_keys_from_siblings().get("XAI_API_KEY", "")
    if sibling and not is_placeholder(sibling):
        return sibling
    rv = fetch_roteiro_viral_managed_keys().get("XAI_API_KEY", "")
    if rv and not is_placeholder(rv):
        return rv
    return None


def sync_roteiro_viral_keys_to_store(*, api_base: Optional[str] = None) -> dict[str, Any]:
    """Import all RV keys into MongoDB env_keys (merge missing/overwritable)."""
    from .env_keys_mongo import EnvKeysStore

    rv_keys = fetch_roteiro_viral_managed_keys(api_base=api_base)
    if not rv_keys:
        return {"ok": False, "error": "no keys found in Roteiro Viral", "imported": 0}
    store = EnvKeysStore()
    written = store.upsert(
        rv_keys,
        source_path=f"roteiro_viral:{api_base or roteiro_viral_api_base()}",
        merge=True,
        allow_all=False,
    )
    from .env_keys_mongo import apply_env_keys_cache_to_environ

    apply_env_keys_cache_to_environ(store.doc_id)
    return {"ok": True, "imported": written, "imported_keys": sorted(rv_keys.keys())}

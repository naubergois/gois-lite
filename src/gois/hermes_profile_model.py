"""Resolve LLM model labels from Hermes profile config.yaml (model.default)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from .chat_models import resolve_chat_model
from .model_router import AUTO_MODEL_ID, AUTO_MODEL_LABEL, is_auto_model_id
from .config import AgentConfig, OpenclawChatConfig
from .hermes_profiles import hermes_profiles_root

_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# Chat entry api_key_env → Hermes provider slug. Sem isto, um robô criado a
# partir do template (provider deepseek) com modelo Claude manda o pedido para
# a API errada e a execução trava.
_HERMES_PROVIDER_BY_KEY_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "anthropic",
    "DEEPSEEK_API_KEY": "deepseek",
    "OPENAI_API_KEY": "openai-api",
    "GEMINI_API_KEY": "gemini",
    "XAI_API_KEY": "xai",
}


def hermes_provider_for_entry(entry: Any) -> Optional[str]:
    """Best-effort Hermes provider slug for a dashboard ChatModelEntry."""
    key_env = str(getattr(entry, "api_key_env", "") or "").strip().upper()
    return _HERMES_PROVIDER_BY_KEY_ENV.get(key_env)


def normalize_hermes_model_ref(raw: str) -> str:
    """``deepseek/deepseek-chat`` → ``deepseek-chat``."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "/" in s:
        return s.split("/", 1)[-1].strip()
    return s


def _load_profile_config(config_path: Path) -> dict[str, Any]:
    key = str(config_path)
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return {}
    cached = _CONFIG_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    _CONFIG_CACHE[key] = (mtime, loaded)
    if len(_CONFIG_CACHE) > 2000:
        for old_key in list(_CONFIG_CACHE.keys())[:200]:
            _CONFIG_CACHE.pop(old_key, None)
    return loaded


def read_profile_model_default(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> Optional[str]:
    """Return normalized model id from ``profiles/<name>/config.yaml``."""
    name = (profile or "").strip()
    if not name:
        return None
    root = (profiles_root or hermes_profiles_root()).expanduser()
    cfg_path = root / name / "config.yaml"
    if not cfg_path.is_file():
        return None
    loaded = _load_profile_config(cfg_path)
    model_block = loaded.get("model")
    raw: Optional[str] = None
    if isinstance(model_block, dict):
        for key in ("default", "model", "id"):
            val = model_block.get(key)
            if isinstance(val, str) and val.strip():
                raw = val.strip()
                break
    elif isinstance(model_block, str) and model_block.strip():
        raw = model_block.strip()
    if not raw:
        return None
    normalized = normalize_hermes_model_ref(raw)
    return normalized or None


def read_profile_display_name(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> Optional[str]:
    """Return ``display_name`` from ``profiles/<name>/profile.yaml`` when present."""
    name = (profile or "").strip()
    if not name:
        return None
    root = (profiles_root or hermes_profiles_root()).expanduser()
    meta_path = root / name / "profile.yaml"
    if not meta_path.is_file():
        return None
    loaded = _load_profile_config(meta_path)
    value = loaded.get("display_name") if isinstance(loaded, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def model_fields_for_profile(
    profile: Optional[str],
    *,
    chat_cfg: Optional[OpenclawChatConfig] = None,
    agent_cfg: Optional[AgentConfig] = None,
    profiles_root: Optional[Path] = None,
) -> dict[str, str]:
    """Build ``modelId`` / ``modelLabel`` for dashboard rows tied to a Hermes profile."""
    fallback = (agent_cfg.model if agent_cfg else None) or "deepseek-chat"
    model_id = read_profile_model_default(profile or "", profiles_root=profiles_root)
    if not model_id:
        model_id = normalize_hermes_model_ref(fallback) or fallback
    if is_auto_model_id(model_id):
        return {"modelId": AUTO_MODEL_ID, "modelLabel": AUTO_MODEL_LABEL}
    label = model_id.replace("-", " ").replace("_", " ")
    if chat_cfg is not None:
        resolved, _ = resolve_chat_model(chat_cfg, model_id)
        if resolved is not None:
            return {
                "modelId": resolved.entry.id,
                "modelLabel": resolved.entry.label,
            }
    return {"modelId": model_id, "modelLabel": label}


def read_profile_meta_dict(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Return parsed ``profile.yaml`` for a Hermes profile."""
    name = (profile or "").strip()
    if not name:
        return {}
    root = (profiles_root or hermes_profiles_root()).expanduser()
    meta_path = root / name / "profile.yaml"
    if not meta_path.is_file():
        return {}
    loaded = _load_profile_config(meta_path)
    return loaded if isinstance(loaded, dict) else {}


def read_profile_execution_backend(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> str:
    """Return how this profile executes code: ``llm`` or an IDE id."""
    from .kanban_ide_handoff import normalize_execution_backend

    meta = read_profile_meta_dict(profile, profiles_root=profiles_root)
    raw = meta.get("execution_backend") or meta.get("code_runner") or "llm"
    return normalize_execution_backend(raw)


def read_profile_config_dict(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Return parsed ``config.yaml`` for a Hermes profile."""
    name = (profile or "").strip()
    if not name:
        return {}
    root = (profiles_root or hermes_profiles_root()).expanduser()
    cfg_path = root / name / "config.yaml"
    if not cfg_path.is_file():
        return {}
    loaded = _load_profile_config(cfg_path)
    return loaded if isinstance(loaded, dict) else {}


def read_profile_skills(
    profile: str,
    *,
    profiles_root: Optional[Path] = None,
) -> list[str]:
    """Skills slugs stored in profile.yaml or config.yaml."""
    meta = read_profile_meta_dict(profile, profiles_root=profiles_root)
    config = read_profile_config_dict(profile, profiles_root=profiles_root)
    for source in (meta, config):
        raw = source.get("skills")
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
    return []


def write_profile_skills(
    profile: str,
    skills: list[str],
    *,
    profiles_root: Optional[Path] = None,
) -> bool:
    """Persist skill slugs on profile.yaml (primary) and config.yaml when present."""
    cleaned = [str(s).strip() for s in skills if str(s).strip()]
    if not write_profile_meta(profile, {"skills": cleaned}, profiles_root=profiles_root):
        return False
    name = (profile or "").strip()
    if not name:
        return False
    root = (profiles_root or hermes_profiles_root()).expanduser()
    cfg_path = root / name / "config.yaml"
    if cfg_path.is_file():
        loaded = read_profile_config_dict(name, profiles_root=profiles_root)
        loaded["skills"] = cleaned
        try:
            cfg_path.write_text(
                yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except OSError:
            return False
        _CONFIG_CACHE.pop(str(cfg_path), None)
    return True


def write_profile_meta(
    profile: str,
    updates: dict[str, Any],
    *,
    profiles_root: Optional[Path] = None,
) -> bool:
    """Merge keys into ``profiles/<name>/profile.yaml``."""
    name = (profile or "").strip()
    if not name or not updates:
        return False
    root = (profiles_root or hermes_profiles_root()).expanduser()
    meta_path = root / name / "profile.yaml"
    if not meta_path.parent.is_dir():
        return False
    loaded: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                loaded = raw
        except (OSError, yaml.YAMLError):
            loaded = {}
    for key, value in updates.items():
        if value is None:
            continue
        loaded[key] = value
    try:
        meta_path.write_text(
            yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError:
        return False
    _CONFIG_CACHE.pop(str(meta_path), None)
    return True


def write_profile_model_default(
    profile: str,
    model_id: str,
    *,
    chat_cfg: Optional[OpenclawChatConfig] = None,
    profiles_root: Optional[Path] = None,
) -> bool:
    """Persist ``model.default`` in ``profiles/<name>/config.yaml``."""
    name = (profile or "").strip()
    mid = (model_id or "").strip()
    if not name or not mid:
        return False
    if is_auto_model_id(mid):
        hermes_ref = AUTO_MODEL_ID
        hermes_provider = None
    else:
        hermes_ref = mid
        hermes_provider = None
    if chat_cfg is not None and not is_auto_model_id(mid):
        resolved, _err = resolve_chat_model(chat_cfg, mid)
        if resolved is not None and resolved.entry.model.strip():
            hermes_ref = resolved.entry.model.strip()
            hermes_provider = hermes_provider_for_entry(resolved.entry)
    root = (profiles_root or hermes_profiles_root()).expanduser()
    cfg_path = root / name / "config.yaml"
    if not cfg_path.parent.is_dir():
        return False
    loaded: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                loaded = raw
        except (OSError, yaml.YAMLError):
            loaded = {}
    model_block = loaded.get("model")
    if not isinstance(model_block, dict):
        model_block = {}
    model_block["default"] = hermes_ref
    if hermes_provider:
        model_block["provider"] = hermes_provider
    loaded["model"] = model_block
    try:
        cfg_path.write_text(
            yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError:
        return False
    _CONFIG_CACHE.pop(str(cfg_path), None)
    return True


def enrich_row_with_profile_model(
    row: dict[str, Any],
    *,
    profile_key: str = "profile",
    chat_cfg: Optional[OpenclawChatConfig] = None,
    agent_cfg: Optional[AgentConfig] = None,
    profiles_root: Optional[Path] = None,
) -> None:
    """Mutate *row* in place with model fields when a profile name is present."""
    if not isinstance(row, dict):
        return
    profile = str(row.get(profile_key) or "").strip()
    if not profile:
        return
    if not row.get("profileLabel"):
        display_name = read_profile_display_name(profile, profiles_root=profiles_root)
        if display_name:
            row["profileLabel"] = display_name
    row.update(
        model_fields_for_profile(
            profile,
            chat_cfg=chat_cfg,
            agent_cfg=agent_cfg,
            profiles_root=profiles_root,
        )
    )

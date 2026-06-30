"""Roteiro Viral image model catalog and fallback attempt planning."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Same order as src/gois/roteiroviral/services/image_provider_registry.py
RV_IMAGE_PROVIDER_ORDER: tuple[str, ...] = (
    "google",
    "openrouter",
    "openai",
    "grok",
    "fal",
    "stability",
    "wavespeed",
    "byteplus",
    "klingai",
)

# Mirrors src/gois/roteiroviral/utils/model_fallback.py DEFAULT_IMAGE_MODELS
RV_DEFAULT_IMAGE_MODELS: tuple[str, ...] = (
    "imagen-4.0-ultra-generate-001",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
    "gemini-3-pro-image",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
    "gpt-image-1",
    "gpt-image-1-mini",
    "grok-imagine-image-quality",
    "grok-imagine-image",
    "grok-imagine-image-pro",
    "fal-ai/flux/dev",
    "wavespeedai/flux-2-dev-text-to-image",
    "seedream-4.0",
)

SKILL_TO_RV_PROVIDER: dict[str, str] = {
    "imagen": "google",
    "nano": "google",
    "grok": "grok",
    "openrouter": "openrouter",
}

# Tier order: largest / highest quota cost first → smaller / cheaper on fallback.
GOOGLE_GEMINI_NATIVE_MODELS: tuple[str, ...] = (
    "gemini-3-pro-image",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
)

GOOGLE_IMAGEN_MODELS: tuple[str, ...] = (
    "imagen-4.0-ultra-generate-001",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
)

GROK_IMAGE_MODELS_TIERED: tuple[str, ...] = (
    "grok-imagine-image-quality",
    "grok-imagine-image-pro",
    "grok-imagine-image",
    "grok-2-image-1212",
)

OPENAI_IMAGE_MODELS_TIERED: tuple[str, ...] = (
    "gpt-image-1",
    "gpt-image-1.5",
    "gpt-image-1-mini",
    "dall-e-3",
)

OPENROUTER_IMAGE_MODELS: tuple[str, ...] = (
    "google/gemini-3-pro-image",
    "google/gemini-3.1-flash-image",
    "google/gemini-2.5-flash-image",
    "openai/gpt-5-image",
    "openai/gpt-5-image-mini",
    "openai/gpt-5.4-image-2",
    "black-forest-labs/flux.2-pro",
    "x-ai/grok-imagine-image-quality",
    "sourceful/riverflow-v2.5-pro",
    "microsoft/mai-image-2.5",
    "bytedance-seed/seedream-4.5",
)

PROVIDER_DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "google": (*GOOGLE_IMAGEN_MODELS, *GOOGLE_GEMINI_NATIVE_MODELS),
    "openrouter": OPENROUTER_IMAGE_MODELS,
    "openai": OPENAI_IMAGE_MODELS_TIERED,
    "grok": GROK_IMAGE_MODELS_TIERED,
    "fal": ("fal-ai/flux/dev", "fal-ai/flux/schnell"),
    "stability": ("stable-diffusion-3.5-large", "stable-diffusion-3-medium"),
    "wavespeed": ("wavespeedai/flux-2-dev-text-to-image",),
    "byteplus": ("seedream-4.0", "seedream-3.0"),
    "klingai": ("kling-v1",),
}


@dataclass(frozen=True)
class ImageAttempt:
    provider: str
    model: str


def roteiro_viral_root() -> Path:
    import os

    raw = os.environ.get("ROTEIRO_VIRAL_PATH", "/Volumes/NAUBER/roteiroviral")
    return Path(raw).expanduser().resolve()


def manifest_path() -> Path:
    return roteiro_viral_root() / "configs" / "models" / "manifest.json"


def resolve_image_provider(model_name: str) -> str:
    name = (model_name or "").strip().lower().replace("models/", "")
    if not name:
        return "google"
    from .openrouter_image_catalog import is_openrouter_image_model_id

    if is_openrouter_image_model_id(name):
        return "openrouter"
    if name.startswith("gpt-image") or "dalle" in name or "dall-e" in name:
        return "openai"
    if "grok" in name or "aurora" in name or name.startswith("xai-"):
        return "grok"
    if name.startswith("fal") or name.startswith("fal-ai/") or name == "flux":
        return "fal"
    if "seedream" in name or "seededit" in name or "byteplus" in name:
        return "byteplus"
    if "stability" in name or name.startswith("stable-"):
        return "stability"
    if "wavespeed" in name or "/" in name and "flux" in name:
        return "wavespeed"
    if "kling" in name or "kolors" in name:
        return "klingai"
    if "imagen" in name or "gemini" in name or "nano-banana" in name:
        return "google"
    return "google"


def _is_image_model(entry: dict[str, Any]) -> bool:
    name = str(entry.get("name") or "").lower()
    if "veo" in name:
        return False
    model_type = str(entry.get("model_type") or "").lower()
    modality = str(entry.get("modality") or "").lower()
    methods = entry.get("supported_methods") or []
    if model_type == "image" or modality == "image":
        return True
    if "imagen" in name or "image" in name:
        return True
    if isinstance(methods, list) and (
        "generateImages" in methods or "predict" in methods
    ):
        return True
    return False


def load_manifest_image_models() -> list[tuple[str, str]]:
    path = manifest_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("RV manifest read failed: %s", exc)
        return []
    models: list[tuple[str, str]] = []
    for entry in data.get("models") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("active") is False:
            continue
        if not _is_image_model(entry):
            continue
        name = str(entry.get("name") or "").strip().replace("models/", "")
        if not name:
            continue
        provider = str(entry.get("provider") or "").strip().lower()
        if provider in ("xai (grok)", "xai", "grok"):
            prov = "grok"
        elif provider in ("openai",):
            prov = "openai"
        elif provider in ("google", "gemini"):
            prov = "google"
        elif provider in ("fal",):
            prov = "fal"
        else:
            prov = resolve_image_provider(name)
        models.append((name, prov))
    return models


def all_rv_image_models() -> list[tuple[str, str]]:
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for name, prov in load_manifest_image_models():
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((name, prov))
    for name in RV_DEFAULT_IMAGE_MODELS:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((name, resolve_image_provider(name)))
    for prov, names in PROVIDER_DEFAULT_MODELS.items():
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append((name, prov))
    return ordered


def all_openrouter_image_model_ids() -> tuple[str, ...]:
    """All OpenRouter image models (curated defaults + API/cache catalog)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for mid in OPENROUTER_IMAGE_MODELS:
        key = mid.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(mid)
    try:
        from .openrouter_image_catalog import load_openrouter_image_raw_models

        for raw in load_openrouter_image_raw_models():
            mid = str(raw.get("id") or "").strip()
            if not mid or mid == "openrouter/auto":
                continue
            key = mid.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(mid)
    except Exception as exc:
        log.debug("openrouter catalog for fallback: %s", exc)
    return tuple(ordered)


def build_fallback_attempts(
    primary_skill: str,
    *,
    allow_fallback: bool = True,
    preferred_model: Optional[str] = None,
    input_image_path: Optional[str] = None,
) -> list[ImageAttempt]:
    """Build ordered model attempts mirroring RV provider/model fallback."""
    skill = (primary_skill or "imagen").strip().lower()
    primary_provider = SKILL_TO_RV_PROVIDER.get(skill, resolve_image_provider(skill))

    attempts: list[ImageAttempt] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str) -> None:
        prov = (provider or "google").strip().lower()
        mod = (model or "").strip()
        if not mod:
            return
        if input_image_path and str(input_image_path).strip():
            if prov == "google" and mod.startswith("imagen-"):
                return
        key = (prov, mod.lower())
        if key in seen:
            return
        seen.add(key)
        attempts.append(ImageAttempt(provider=prov, model=mod))

    pref = (preferred_model or "").strip()
    if pref:
        add(resolve_image_provider(pref), pref)

    if skill == "imagen":
        for model in GOOGLE_IMAGEN_MODELS:
            add("google", model)
        if allow_fallback:
            for model in GOOGLE_GEMINI_NATIVE_MODELS:
                add("google", model)
    elif skill == "nano":
        for model in GOOGLE_GEMINI_NATIVE_MODELS:
            add("google", model)
        if allow_fallback:
            for model in GOOGLE_IMAGEN_MODELS:
                add("google", model)
    elif skill == "grok":
        for model in GROK_IMAGE_MODELS_TIERED:
            add("grok", model)
    elif skill == "openrouter":
        for model in all_openrouter_image_model_ids():
            add("openrouter", model)

    if not allow_fallback:
        if pref:
            return [ImageAttempt(resolve_image_provider(pref), pref)]
        default = GOOGLE_IMAGEN_MODELS[0]
        if skill == "nano":
            default = GOOGLE_GEMINI_NATIVE_MODELS[0]
        elif skill == "grok":
            default = GROK_IMAGE_MODELS_TIERED[0]
        elif skill == "openrouter":
            default = OPENROUTER_IMAGE_MODELS[0]
        if attempts:
            primary_attempts = [
                a for a in attempts if a.provider == primary_provider
            ]
            if primary_attempts:
                return primary_attempts
        return [ImageAttempt(provider=primary_provider, model=pref or default)]

    if allow_fallback:
        for prov in RV_IMAGE_PROVIDER_ORDER:
            if skill == "nano" and prov == "google":
                continue
            if skill == "imagen" and prov == "google":
                continue
            if skill == "grok" and prov == "grok":
                continue
            if skill == "openrouter" and prov == "openrouter":
                continue
            for model in (
                all_openrouter_image_model_ids()
                if prov == "openrouter"
                else PROVIDER_DEFAULT_MODELS.get(prov, ())
            ):
                add(prov, model)

    for model, prov in all_rv_image_models():
        add(prov, model)

    if allow_fallback:
        for model in all_openrouter_image_model_ids():
            add("openrouter", model)

    return attempts

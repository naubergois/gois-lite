"""Local style/face analysis endpoints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .bootstrap import ensure_runtime_on_path
from .gemini_text import DEFAULT_TEXT_MODEL, generate_text

_IMAGE_STYLES_CACHE: dict[str, str] | None = None


def load_image_styles() -> dict[str, str]:
    """Return the full RV IMAGE_STYLES catalog (900+ entries) plus custom DB styles."""
    global _IMAGE_STYLES_CACHE
    if _IMAGE_STYLES_CACHE is not None:
        return dict(_IMAGE_STYLES_CACHE)
    ensure_runtime_on_path()
    from utils.styles_library import IMAGE_STYLES  # type: ignore[import-untyped]

    catalog: dict[str, str] = dict(IMAGE_STYLES)
    try:
        import database  # type: ignore[import-untyped]

        for doc in database.get_all_image_styles():
            name = str(doc.get("name") or "").strip()
            modifier = str(doc.get("prompt_modifier") or doc.get("modifier") or "").strip()
            if name and modifier:
                catalog[name] = modifier
    except Exception:
        pass
    _IMAGE_STYLES_CACHE = catalog
    return dict(_IMAGE_STYLES_CACHE)


def invalidate_image_styles_cache() -> None:
    """Clear in-process IMAGE_STYLES cache after create/update/delete."""
    global _IMAGE_STYLES_CACHE
    _IMAGE_STYLES_CACHE = None

STYLE_ANALYSIS_PROMPT = """Analyze this image and create a detailed visual style prompt that could be used to generate similar images.

Please provide:
1. **Style Name**: A catchy name for this visual style (e.g., "Neon Cyberpunk", "Soft Watercolor")
2. **Base Prompt**: A detailed description of the visual style that can be appended to any image generation prompt. Include:
   - Art style (digital, traditional, photographic)
   - Color palette and mood
   - Lighting characteristics
   - Texture and detail level
   - Any unique visual elements
3. **Negative Prompt**: Elements to avoid to maintain this style

Format your response as JSON:
{
  "name": "Style Name",
  "prompt": "Detailed base prompt describing the style...",
  "negative_prompt": "Elements to avoid..."
}
"""

STYLE_TEXT_ANALYSIS_PROMPT = """Create a detailed visual style prompt from this text description.

User description:
{description}

Provide:
1. **Style Name**: A catchy name for this visual style
2. **Base Prompt**: A detailed description that can be appended to any image generation prompt (art style, palette, lighting, texture, mood)
3. **Negative Prompt**: Elements to avoid

Format your response as JSON:
{{
  "name": "Style Name",
  "prompt": "Detailed base prompt describing the style...",
  "negative_prompt": "Elements to avoid...",
  "category": "photo|painting|anime|concept|style"
}}
"""


def normalize_style_proposal(style: dict[str, Any]) -> dict[str, str]:
    """Map Gemini style JSON to chat form fields (STYLE: modifier)."""
    name = str(style.get("name") or "Custom Style").strip()
    prompt = str(style.get("prompt") or style.get("base_prompt") or "").strip()
    negative = str(style.get("negative_prompt") or "").strip()
    if not prompt:
        raise ValueError("Resposta da IA sem prompt de estilo")
    modifier = prompt if prompt.upper().startswith("STYLE:") else f"STYLE: {prompt}"
    if negative:
        modifier = f"{modifier.rstrip('.')} Avoid: {negative}."
    description = str(style.get("description") or prompt[:400]).strip()
    category = str(style.get("category") or "custom").strip() or "custom"
    characteristics = str(style.get("characteristics") or negative or "").strip()
    return {
        "name": name,
        "modifier": modifier,
        "description": description,
        "category": category,
        "characteristics": characteristics,
    }


def analyze_style_text(description: str, *, api_key: Optional[str] = None) -> dict[str, Any]:
    text = str(description or "").strip()
    if not text:
        raise ValueError("description is required")
    prompt = STYLE_TEXT_ANALYSIS_PROMPT.format(description=text)
    response_text = generate_text(prompt, api_key=api_key, model=DEFAULT_TEXT_MODEL)
    style_data = _parse_style_json(response_text)
    return {"success": True, "style": style_data}


def create_custom_image_style(
    name: str,
    modifier: str,
    *,
    description: str = "",
    category: str = "custom",
    characteristics: str = "",
) -> bool:
    ensure_runtime_on_path()
    import database  # type: ignore[import-untyped]

    ok = database.create_image_style(
        name=name,
        modifier=modifier,
        description=description,
        category=category or "custom",
        characteristics=characteristics,
    )
    if ok:
        invalidate_image_styles_cache()
        try:
            from api_v2.routers.system.utils import invalidate_styles_cache  # type: ignore[import-untyped]

            invalidate_styles_cache()
        except Exception:
            pass
    return bool(ok)


def _parse_style_json(text: str) -> dict[str, Any]:
    try:
        json_match = re.search(r'\{[^{}]*"name"[^{}]*"prompt"[^{}]*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)
    except json.JSONDecodeError:
        name_match = re.search(r'"name":\s*"([^"]+)"', text)
        prompt_match = re.search(r'"prompt":\s*"([^"]+)"', text, re.DOTALL)
        neg_match = re.search(r'"negative_prompt":\s*"([^"]+)"', text)
        return {
            "name": name_match.group(1) if name_match else "Custom Style",
            "prompt": prompt_match.group(1) if prompt_match else text[:500],
            "negative_prompt": neg_match.group(1) if neg_match else "blurry, low quality",
        }


def analyze_image_bytes(data: bytes, *, api_key: Optional[str] = None) -> dict[str, Any]:
    from ..face_analysis import _open_image_bytes

    pil_image = _open_image_bytes(data)
    response_text = generate_text(
        STYLE_ANALYSIS_PROMPT,
        api_key=api_key,
        model=DEFAULT_TEXT_MODEL,
        images=[pil_image],
    )
    style_data = _parse_style_json(response_text)
    return {"success": True, "style": style_data}


def analyze_image_path(path: Path, *, api_key: Optional[str] = None) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"foto não encontrada: {path}")
    return analyze_image_bytes(path.read_bytes(), api_key=api_key)


def analyze_face_path(path: Path, *, api_key: Optional[str] = None) -> dict[str, Any]:
    from ..face_analysis import analyze_face_path as _analyze

    result = _analyze(path, api_key)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "analyze-face falhou")
    return {
        "success": True,
        "face_data": result.get("face_data") or {},
        "raw_text": result.get("raw_text") or "",
    }


def analyze_face_bytes(data: bytes, *, api_key: Optional[str] = None) -> dict[str, Any]:
    from ..face_analysis import analyze_face_bytes as _analyze

    result = _analyze(data, api_key=api_key)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "analyze-face falhou")
    return {
        "success": True,
        "face_data": result.get("face_data") or {},
        "raw_text": result.get("raw_text") or "",
    }

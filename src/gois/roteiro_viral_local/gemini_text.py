"""Gemini text/vision helpers for local RV endpoints."""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_TEXT_MODEL = "gemini-3.5-flash"


def generate_text(
    prompt: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    images: Optional[list[Any]] = None,
    timeout_seconds: float = 180.0,
) -> str:
    from google import genai

    from ..llm_http_pool import get_gemini_client
    from ..secrets_fallback import build_gemini_subprocess_env

    _env, key = build_gemini_subprocess_env(explicit_key=api_key)
    if not key:
        raise ValueError("GEMINI_API_KEY / GOOGLE_API_KEY necessária")

    model_id = (model or DEFAULT_TEXT_MODEL).strip()
    client = get_gemini_client(api_key=key, timeout_seconds=timeout_seconds)
    parts: list[Any] = [prompt]
    if images:
        parts.extend(images)
    response = client.models.generate_content(model=model_id, contents=parts)
    text = getattr(response, "text", None)
    if text and str(text).strip():
        return str(text).strip()
    chunks: list[str] = []
    for part in getattr(response, "parts", []) or []:
        pt = getattr(part, "text", None)
        if pt and str(pt).strip():
            chunks.append(str(pt).strip())
    if chunks:
        return "\n".join(chunks)
    return str(response)

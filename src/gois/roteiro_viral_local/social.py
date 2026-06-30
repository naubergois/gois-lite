"""Local /thumbnail social concepts endpoint."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .gemini_text import DEFAULT_TEXT_MODEL, generate_text


def handle_social_concepts(payload: dict[str, Any]) -> dict[str, Any]:
    script = str(payload.get("script") or "").strip()
    if len(script) < 50:
        raise ValueError("Script deve ter pelo menos 50 caracteres")

    prompt = f"""
    You are a YouTube Thumbnail Expert.
    Analyze the following content (Script) and create 3 DISTINCT highly clickbaity thumbnail concepts.

    CONTENT START:
    {script[:3000]}
    CONTENT END

    GUIDELINES:
    1. Variant A: "Emotional/Shocking" - Focus on facial expressions or dramatic contrast.
    2. Variant B: "Text-Heavy/Educational" - Clear value proposition, bold text.
    3. Variant C: "Abstract/Intriguing" - Uses metaphor or curiosity gap.

    OUTPUT JSON (in Portuguese):
    {{
        "thumbnails": [
            {{
                "type": "Emocional",
                "visual_description": "Detailed prompt for AI image generator describing the scene, lighting, style, facial expression...",
                "overlay_text": "Short text to place on image (optional)"
            }},
            {{
                "type": "Educacional",
                "visual_description": "...",
                "overlay_text": "..."
            }},
            {{
                "type": "Abstrato",
                "visual_description": "...",
                "overlay_text": "..."
            }}
        ]
    }}
    """
    api_key = payload.get("api_key")
    response_text = generate_text(prompt, api_key=api_key, model=DEFAULT_TEXT_MODEL)
    text = response_text.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError("Falha ao gerar thumbnails") from None
        data = json.loads(match.group())

    thumbnails_data = data.get("thumbnails") or []
    if not thumbnails_data:
        raise ValueError("Falha ao gerar thumbnails")

    thumbnails = [
        {
            "type": t.get("type", "Conceito"),
            "visual_description": t.get("visual_description", ""),
            "overlay_text": t.get("overlay_text"),
        }
        for t in thumbnails_data
        if isinstance(t, dict)
    ]
    return {"thumbnails": thumbnails}

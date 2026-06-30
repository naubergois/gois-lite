"""Local image generation and file serving."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from .config import local_output_root


def handle_generate_image(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Prompt é obrigatório.")

    from ..roteiro_imagen import DEFAULT_IMAGEN_MODEL, generate_imagen_to_path

    api_key = payload.get("api_key")
    model = str(payload.get("model_name") or DEFAULT_IMAGEN_MODEL).strip()
    aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()

    out_dir = local_output_root() / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"img_{uuid.uuid4().hex[:12]}.png"

    result = generate_imagen_to_path(
        prompt=prompt,
        output_path=out_path,
        api_key=api_key,
        model_name=model,
        aspect_ratio=aspect_ratio,
        style=payload.get("style"),
    )
    file_path = str(result.get("output_path") or result.get("image_path") or "")
    if not file_path:
        raise RuntimeError("Falha ao gerar imagem.")

    return {
        "status": "success",
        "file_path": file_path,
        "model_used": result.get("model_used") or model,
        "cost_usd": 0.0,
    }


def read_local_file(path: str) -> tuple[Path, bytes]:
    """Read a generated file from disk (local RV output)."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        root = local_output_root().resolve()
        alt = (root / path.lstrip("/")).resolve()
        if alt.is_file() and str(alt).startswith(str(root)):
            candidate = alt
        else:
            raise FileNotFoundError(path)
    return candidate, candidate.read_bytes()

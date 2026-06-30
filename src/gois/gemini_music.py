"""Google Gemini Lyria music generation — chat and MCP helpers."""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional

from .chat_artifacts import resolve_artifacts_dir, stage_artifact
from .roteiro_imagen import resolve_gemini_api_key

DEFAULT_MODEL = "lyria-3-clip-preview"
PRO_MODEL = "lyria-3-pro-preview"
SUPPORTED_MODELS = frozenset({DEFAULT_MODEL, PRO_MODEL})
MAX_INPUT_IMAGES = 10
DEFAULT_TIMEOUT = 180.0


def _supported_formats(model: str) -> frozenset[str]:
    if model == PRO_MODEL:
        return frozenset({"mp3", "wav"})
    return frozenset({"mp3"})


def _mime_for_format(fmt: str) -> str:
    if fmt == "wav":
        return "audio/wav"
    return "audio/mpeg"


def _ext_for_mime(mime: str, fmt: str) -> str:
    if "wav" in (mime or "").lower() or fmt == "wav":
        return "wav"
    return "mp3"


def build_music_prompt(
    prompt: str,
    *,
    lyrics: Optional[str] = None,
    instrumental: bool = False,
) -> str:
    parts = [(prompt or "").strip()]
    if instrumental:
        parts.append(
            "Instrumental only. No vocals, no sung lyrics, no spoken word."
        )
    lyric_text = (lyrics or "").strip()
    if lyric_text:
        parts.append(f"Lyrics:\n{lyric_text}")
    return "\n\n".join(part for part in parts if part)


def _load_reference_images(paths: list[str]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for raw in paths[:MAX_INPUT_IMAGES]:
        path = Path(str(raw).strip()).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"reference image not found: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        images.append(
            {
                "mime_type": mime,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return images


def generate_gemini_music(
    *,
    prompt: str,
    output_path: Path,
    model: str = DEFAULT_MODEL,
    lyrics: Optional[str] = None,
    instrumental: bool = False,
    output_format: str = "mp3",
    input_image_paths: Optional[list[str]] = None,
    api_key: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Generate one music track with Google Lyria via google-genai."""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required")

    key = resolve_gemini_api_key(api_key)
    if not key:
        raise ValueError(
            "GEMINI_API_KEY / GOOGLE_API_KEY necessária (Chaves & Secrets ou .env)"
        )

    model_name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(
            f"model must be one of: {', '.join(sorted(SUPPORTED_MODELS))}"
        )

    fmt = (output_format or "mp3").strip().lower()
    if fmt not in _supported_formats(model_name):
        raise ValueError(
            f"model {model_name} supports {', '.join(sorted(_supported_formats(model_name)))}"
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai não instalado. Use: "
            "uv run skills/qclaw-gemini-music/scripts/generate_music.py"
        ) from exc

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    contents: list[Any] = [build_music_prompt(text, lyrics=lyrics, instrumental=instrumental)]
    for image in _load_reference_images(list(input_image_paths or [])):
        contents.append(
            types.Part.from_bytes(
                data=base64.b64decode(image["data"]),
                mime_type=image["mime_type"],
            )
        )

    env_backup = {
        "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY"),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    }
    try:
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = key
        client = genai.Client(
            api_key=key,
            http_options={"timeout": int(max(30.0, timeout_seconds) * 1000)},
        )
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO", "TEXT"],
            ),
        )
    finally:
        if env_backup["GOOGLE_API_KEY"] is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = env_backup["GOOGLE_API_KEY"]
        if env_backup["GEMINI_API_KEY"] is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = env_backup["GEMINI_API_KEY"]

    audio_bytes: bytes | None = None
    audio_mime = _mime_for_format(fmt)
    returned_lyrics: list[str] = []

    for part in response.parts or []:
        if getattr(part, "text", None):
            returned_lyrics.append(str(part.text).strip())
            continue
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        if not data:
            continue
        mime = (
            getattr(inline, "mime_type", None)
            or getattr(inline, "mimeType", None)
            or audio_mime
        )
        if isinstance(data, str):
            audio_bytes = base64.b64decode(data)
        else:
            audio_bytes = bytes(data)
        audio_mime = str(mime or audio_mime)
        break

    if not audio_bytes:
        raise RuntimeError("Lyria não retornou áudio (filtro de conteúdo ou erro da API)")

    ext = _ext_for_mime(audio_mime, fmt)
    if output_path.suffix.lower() not in {".mp3", ".wav"}:
        output_path = output_path.with_suffix(f".{ext}")
    output_path.write_bytes(audio_bytes)

    return {
        "ok": True,
        "output_path": str(output_path),
        "audio_path": str(output_path),
        "model_used": model_name,
        "format": ext,
        "mime_type": audio_mime,
        "bytes": len(audio_bytes),
        "lyrics": "\n".join(line for line in returned_lyrics if line) or None,
        "instrumental": instrumental,
        "input_image_count": len(input_image_paths or []),
    }


def _default_output_path(filename: Optional[str], output_dir: Optional[str]) -> Path:
    if output_dir:
        base = Path(output_dir).expanduser()
    else:
        base = resolve_artifacts_dir() / "music"
    base.mkdir(parents=True, exist_ok=True)
    name = (filename or "gemini-music-track.mp3").strip()
    path = Path(name)
    if not path.suffix:
        path = path.with_suffix(".mp3")
    if path.is_absolute():
        return path
    return (base / path.name).resolve()


def _append_duration_hint(prompt: str, duration_seconds: Any) -> str:
    try:
        seconds = int(duration_seconds)
    except (TypeError, ValueError):
        return prompt
    if seconds <= 0:
        return prompt
    mins = seconds // 60
    secs = seconds % 60
    if mins and secs:
        hint = f"Target duration: approximately {mins}m{secs:02d}s."
    elif mins:
        hint = f"Target duration: approximately {mins} minute(s)."
    else:
        hint = f"Target duration: approximately {seconds} seconds."
    base = (prompt or "").strip()
    return f"{base}\n\n{hint}".strip() if base else hint


def dispatch_gemini_music_generate(args: dict[str, Any]) -> dict[str, Any]:
    """Generate music with Gemini Lyria (shared by chat and MCP)."""
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}
    duration_raw = args.get("duration_seconds")
    if duration_raw is None:
        duration_raw = args.get("duration")
    prompt = _append_duration_hint(prompt, duration_raw)

    try:
        timeout = float(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    model = str(args.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    lyrics = str(args.get("lyrics") or "").strip() or None
    instrumental = bool(args.get("instrumental"))
    output_format = str(args.get("format") or args.get("output_format") or "mp3").strip()

    image_paths: list[str] = []
    single = str(args.get("input_image_path") or args.get("image") or "").strip()
    if single:
        image_paths.append(single)
    for item in args.get("input_image_paths") or args.get("images") or []:
        text = str(item or "").strip()
        if text:
            image_paths.append(text)

    out_path = _default_output_path(
        str(args.get("filename") or "").strip() or None,
        str(args.get("output_dir") or args.get("cwd") or "").strip() or None,
    )
    session_key = str(args.get("session_key") or "").strip()

    try:
        result = generate_gemini_music(
            prompt=prompt,
            output_path=out_path,
            model=model,
            lyrics=lyrics,
            instrumental=instrumental,
            output_format=output_format,
            input_image_paths=image_paths or None,
            api_key=str(args.get("api_key") or "").strip() or None,
            timeout_seconds=max(30.0, timeout),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:800]}

    att = stage_artifact(
        Path(result["output_path"]),
        label=Path(result["output_path"]).name,
        session_key=session_key,
    )
    return {
        **result,
        "attachments": [att],
        "download_url": att.get("download_url"),
    }


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "gemini_music_generate",
            "description": (
                "Gera música com Google Gemini Lyria (text-to-music). Suporta instrumental, "
                "letras opcionais e até 10 imagens de referência. Requer GEMINI_API_KEY em "
                "/chaves. Skill: qclaw-gemini-music. Equivalente a "
                "qclaw_gemini_music_generate no chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Descrição do estilo, género, mood e tema da música",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Nome do ficheiro de saída (ex. 2026-06-17-lofi.mp3)",
                    },
                    "model": {
                        "type": "string",
                        "enum": [DEFAULT_MODEL, PRO_MODEL],
                        "description": (
                            "lyria-3-clip-preview (rápido, mp3) ou "
                            "lyria-3-pro-preview (mp3/wav)"
                        ),
                    },
                    "lyrics": {
                        "type": "string",
                        "description": "Letra exacta a cantar (opcional)",
                    },
                    "instrumental": {
                        "type": "boolean",
                        "description": "Só instrumental, sem vocais",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["mp3", "wav"],
                        "description": "wav só em lyria-3-pro-preview",
                    },
                    "input_image_path": {
                        "type": "string",
                        "description": "Imagem de referência (opcional)",
                    },
                    "input_image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Até 10 imagens de referência",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Pasta de saída (opcional)",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout em segundos (default 180)",
                    },
                },
                "required": ["prompt"],
            },
        }
    ]

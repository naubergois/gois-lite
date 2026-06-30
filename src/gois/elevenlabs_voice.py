"""ElevenLabs text-to-speech narration — chat and MCP helpers."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx

from .chat_artifacts import resolve_artifacts_dir, stage_artifact
from .secrets_fallback import resolve_llm_api_key

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY = 0.75


def resolve_elevenlabs_api_key() -> Optional[str]:
    return resolve_llm_api_key("ELEVENLABS_API_KEY")


def _api_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "xi-api-key": api_key,
    }


def list_voices(*, api_key: Optional[str] = None, timeout: float = 30.0) -> dict[str, Any]:
    key = (api_key or resolve_elevenlabs_api_key() or "").strip()
    if not key:
        return {"ok": False, "error": "ELEVENLABS_API_KEY not configured — set it in /chaves"}

    try:
        with httpx.Client(timeout=max(5.0, timeout)) as client:
            resp = client.get(
                f"{ELEVENLABS_API_BASE}/voices",
                headers=_api_headers(key),
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code == 401:
        return {"ok": False, "error": "ElevenLabs 401 — invalid ELEVENLABS_API_KEY"}
    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        return {"ok": False, "error": f"ElevenLabs {resp.status_code}: {detail}"}

    payload = resp.json()
    voices = []
    for voice in payload.get("voices") or []:
        if not isinstance(voice, dict):
            continue
        voice_id = str(voice.get("voice_id") or "").strip()
        if not voice_id:
            continue
        name = str(voice.get("name") or voice_id)
        category = str(voice.get("category") or "").strip()
        label = f"{name} ({category})" if category else name
        voices.append({"voice_id": voice_id, "name": name, "label": label})
    voices.sort(key=lambda item: str(item.get("label") or "").lower())
    return {"ok": True, "voices": voices, "count": len(voices)}


def synthesize_speech(
    text: str,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL_ID,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    stability: float = DEFAULT_STABILITY,
    similarity_boost: float = DEFAULT_SIMILARITY,
    api_key: Optional[str] = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {"ok": False, "error": "text is required"}

    key = (api_key or resolve_elevenlabs_api_key() or "").strip()
    if not key:
        return {"ok": False, "error": "ELEVENLABS_API_KEY not configured — set it in /chaves"}

    body = {
        "text": content,
        "model_id": str(model_id or DEFAULT_MODEL_ID).strip(),
        "voice_settings": {
            "stability": max(0.0, min(float(stability), 1.0)),
            "similarity_boost": max(0.0, min(float(similarity_boost), 1.0)),
        },
    }
    url = (
        f"{ELEVENLABS_API_BASE}/text-to-speech/"
        f"{str(voice_id or DEFAULT_VOICE_ID).strip()}"
    )
    headers = {
        **_api_headers(key),
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    try:
        with httpx.Client(timeout=max(10.0, timeout)) as client:
            resp = client.post(
                url,
                params={"output_format": str(output_format or DEFAULT_OUTPUT_FORMAT).strip()},
                headers=headers,
                json=body,
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if resp.status_code == 401:
        return {"ok": False, "error": "ElevenLabs 401 — invalid ELEVENLABS_API_KEY"}
    if resp.status_code >= 400:
        detail = (resp.text or "")[:400]
        return {"ok": False, "error": f"ElevenLabs {resp.status_code}: {detail}"}

    audio = resp.content
    if not audio:
        return {"ok": False, "error": "ElevenLabs returned empty audio"}
    return {"ok": True, "audio_bytes": audio, "bytes": len(audio)}


def _safe_label(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:48]


def _default_output_paths(
    *,
    output_dir: Optional[str],
    zip_path: Optional[str],
    label: str,
) -> tuple[Path, Optional[Path]]:
    if output_dir:
        out_dir = Path(output_dir).expanduser()
        zip_out = Path(zip_path).expanduser() if zip_path else out_dir.parent / f"{out_dir.name}.zip"
        return out_dir, zip_out
    safe = _safe_label(label or "elevenlabs-voice")
    base = resolve_artifacts_dir() / safe
    return base / "audio", base / f"{safe}-audio.zip"


def _load_narration_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows

    parsed = json.loads(text)
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        slides = parsed.get("slides")
        if isinstance(slides, list):
            return [row for row in slides if isinstance(row, dict)]
    return []


def _narration_text(row: dict[str, Any]) -> str:
    for key in ("narration", "script", "text", "content"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    title = str(row.get("title") or "").strip()
    return title


def _write_zip(source_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.glob("*.mp3")):
            zf.write(file_path, arcname=file_path.name)


def _run_elevenlabs_narrate(
    *,
    text: Optional[str] = None,
    narration_file: Optional[str] = None,
    text_file: Optional[str] = None,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL_ID,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    stability: float = DEFAULT_STABILITY,
    similarity_boost: float = DEFAULT_SIMILARITY,
    filename: Optional[str] = None,
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    max_items: int = 200,
    delay: float = 0.3,
    session_key: str = "",
    timeout: float = 120.0,
    analyze: bool = False,
) -> dict[str, Any]:
    if analyze:
        return list_voices(timeout=timeout)

    narration_file = str(narration_file or "").strip() or None
    text_file = str(text_file or "").strip() or None
    text = str(text or "").strip() or None

    if sum(bool(x) for x in (text, narration_file, text_file)) != 1:
        return {
            "ok": False,
            "error": "provide exactly one of: text, narration_file, or text_file",
        }

    api_key = resolve_elevenlabs_api_key()
    if not api_key:
        return {"ok": False, "error": "ELEVENLABS_API_KEY not configured — set it in /chaves"}

    label = filename or "narration"
    out_dir, zip_out = _default_output_paths(
        output_dir=output_dir,
        zip_path=zip_path,
        label=Path(narration_file or text_file or label).stem,
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if text or text_file:
        content = text
        if text_file:
            src = Path(text_file).expanduser()
            if not src.is_file():
                return {"ok": False, "error": f"text_file not found: {src}"}
            content = src.read_text(encoding="utf-8").strip()
        assert content
        out_name = _safe_label(filename or "narration") + ".mp3"
        out_path = out_dir / out_name
        result = synthesize_speech(
            content,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
            stability=stability,
            similarity_boost=similarity_boost,
            api_key=api_key,
            timeout=timeout,
        )
        if not result.get("ok"):
            return result
        out_path.write_bytes(result["audio_bytes"])
        att = stage_artifact(out_path, label=out_path.name, session_key=session_key)
        return {
            "ok": True,
            "mode": "single",
            "output_path": str(out_path),
            "bytes": result.get("bytes"),
            "voice_id": voice_id,
            "model_id": model_id,
            "attachments": [att],
            "download_url": att.get("download_url"),
        }

    src = Path(narration_file or "").expanduser()
    if not src.is_file():
        return {"ok": False, "error": f"narration_file not found: {src}"}

    rows = _load_narration_rows(src)
    if not rows:
        return {"ok": False, "error": f"no narration rows in {src}"}

    cap = max(1, min(int(max_items), 500))
    rows = rows[:cap]
    generated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for idx, row in enumerate(rows, start=1):
        slide_no = row.get("slide") or idx
        content = _narration_text(row)
        if not content:
            failed.append({"slide": slide_no, "error": "empty narration text"})
            continue
        out_path = out_dir / f"slide-{int(slide_no):03d}.mp3"
        result = synthesize_speech(
            content,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
            stability=stability,
            similarity_boost=similarity_boost,
            api_key=api_key,
            timeout=timeout,
        )
        if not result.get("ok"):
            failed.append({"slide": slide_no, "error": result.get("error")})
            continue
        out_path.write_bytes(result["audio_bytes"])
        generated.append(
            {
                "slide": slide_no,
                "title": row.get("title"),
                "path": str(out_path),
                "bytes": result.get("bytes"),
            }
        )
        if delay > 0 and idx < len(rows):
            time.sleep(min(float(delay), 5.0))

    if not generated:
        return {
            "ok": False,
            "error": "no audio generated",
            "failed": failed[:20],
        }

    manifest_path = out_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as fh:
        for item in generated:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    attachments: list[dict[str, Any]] = []
    zip_file: Optional[Path] = None
    if zip_out is not None:
        _write_zip(out_dir, zip_out)
        zip_file = zip_out
        att = stage_artifact(zip_file, label=zip_file.name, session_key=session_key)
        attachments.append(att)

    return {
        "ok": True,
        "mode": "batch",
        "output_dir": str(out_dir),
        "zip_path": str(zip_file) if zip_file else None,
        "generated_count": len(generated),
        "failed_count": len(failed),
        "voice_id": voice_id,
        "model_id": model_id,
        "generated": generated[:20],
        "failed": failed[:20],
        "attachments": attachments,
        "download_url": attachments[0].get("download_url") if attachments else None,
    }


def dispatch_elevenlabs_narrate(args: dict[str, Any]) -> dict[str, Any]:
    """Synthesize narration audio via ElevenLabs (shared by chat and MCP)."""
    try:
        timeout = float(args.get("timeout_seconds") or 120.0)
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        max_items = int(args.get("max_items") or args.get("max_slides") or 200)
    except (TypeError, ValueError):
        max_items = 200
    try:
        delay = float(args.get("delay_seconds") or args.get("delay") or 0.3)
    except (TypeError, ValueError):
        delay = 0.3
    try:
        stability = float(args.get("stability") or DEFAULT_STABILITY)
    except (TypeError, ValueError):
        stability = DEFAULT_STABILITY
    try:
        similarity = float(args.get("similarity_boost") or DEFAULT_SIMILARITY)
    except (TypeError, ValueError):
        similarity = DEFAULT_SIMILARITY

    return _run_elevenlabs_narrate(
        text=str(args.get("text") or "").strip() or None,
        narration_file=str(
            args.get("narration_file")
            or args.get("jsonl_path")
            or args.get("narration_jsonl")
            or ""
        ).strip()
        or None,
        text_file=str(args.get("text_file") or args.get("path") or "").strip() or None,
        voice_id=str(args.get("voice_id") or DEFAULT_VOICE_ID).strip(),
        model_id=str(args.get("model_id") or args.get("model") or DEFAULT_MODEL_ID).strip(),
        output_format=str(args.get("output_format") or DEFAULT_OUTPUT_FORMAT).strip(),
        stability=stability,
        similarity_boost=similarity,
        filename=str(args.get("filename") or "").strip() or None,
        output_dir=str(args.get("output_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
        max_items=max_items,
        delay=delay,
        session_key=str(args.get("session_key") or "").strip(),
        timeout=max(10.0, timeout),
        analyze=bool(args.get("analyze") or args.get("list_voices")),
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "elevenlabs_narrate",
            "description": (
                "Gera áudio de narração com ElevenLabs TTS (MP3). Modo único: `text` "
                "ou ficheiro `.txt`. Modo batch: JSON/JSONL de `qclaw_slides_narration` "
                "(campo narration). Requer ELEVENLABS_API_KEY em /chaves. Skill: "
                "qclaw-elevenlabs-voice. Equivalente a qclaw_elevenlabs_narrate no chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Texto para narração única"},
                    "text_file": {
                        "type": "string",
                        "description": "Ficheiro .txt com o roteiro",
                    },
                    "narration_file": {
                        "type": "string",
                        "description": "JSON/JSONL com {slide,title,narration}",
                    },
                    "voice_id": {
                        "type": "string",
                        "description": f"Voice ID ElevenLabs (default {DEFAULT_VOICE_ID})",
                    },
                    "model_id": {
                        "type": "string",
                        "description": f"Modelo TTS (default {DEFAULT_MODEL_ID})",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Nome do MP3 (modo único)",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Limite batch (default 200)",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Listar vozes disponíveis (sem gerar áudio)",
                    },
                },
            },
        }
    ]

"""HTTP clients for Roteiro Viral image providers (local fallback chain)."""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_grok_throttle_lock = threading.Lock()
_grok_last_request_at = 0.0

_GROK_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "429",
    "resource-exhausted",
    "resource_exhausted",
    "too many requests",
    "requests per second",
    "rate limit",
    "rate_limit",
)


def is_grok_rate_limit_error(error: str) -> bool:
    """True for xAI Grok 429 rate-limit (not billing/credit exhaustion or deprecated model)."""
    text = (error or "").strip().lower()
    if not text:
        return False
    if "deprecated" in text or "not-found" in text or "404" in text:
        return False
    if "permission-denied" in text or "credit" in text and (
        "depleted" in text or "exhausted" in text or "balance" in text
    ):
        return False
    return any(marker in text for marker in _GROK_RATE_LIMIT_MARKERS)


def _grok_min_interval_seconds() -> float:
    raw = os.environ.get("GROK_IMAGE_MIN_INTERVAL", "0.25")
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return 0.25


def _grok_rate_limit_max_retries() -> int:
    raw = os.environ.get("GROK_IMAGE_RATE_LIMIT_RETRIES", "5")
    try:
        return max(1, min(int(raw), 10))
    except (TypeError, ValueError):
        return 5


def _wait_grok_rate_slot() -> None:
    """Serialize Grok calls to stay under xAI's per-second request cap."""
    global _grok_last_request_at
    interval = _grok_min_interval_seconds()
    with _grok_throttle_lock:
        now = time.monotonic()
        wait = interval - (now - _grok_last_request_at)
        if wait > 0:
            time.sleep(wait)
        _grok_last_request_at = time.monotonic()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def nano_script_path() -> Path:
    return _repo_root() / "skills/qclaw-nano-banana-pro/scripts/generate_image.py"


def grok_script_path() -> Path:
    return _repo_root() / "skills/qclaw-grok-imagine/scripts/generate_image.py"


def _provider_api_key(env_var: str) -> Optional[str]:
    from .secrets_fallback import resolve_llm_api_key

    return resolve_llm_api_key(env_var)


def _save_bytes(output_path: Path, data: bytes) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path.resolve()


def _download_url(url: str, output_path: Path, timeout: float) -> Path:
    with httpx.Client(timeout=max(30.0, timeout)) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return _save_bytes(output_path, resp.content)


def generate_google_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    style: Optional[str],
    timeout: float,
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
) -> dict[str, Any]:
    name = (model or "").lower()
    if "imagen" in name:
        from .roteiro_imagen import generate_imagen_to_path
        from .secrets_fallback import build_gemini_subprocess_env, image_generation_timeout_seconds

        _, key = build_gemini_subprocess_env()
        if not key:
            return {"ok": False, "error": "GEMINI_API_KEY ausente", "provider": "google", "model": model}
        timeout_eff = image_generation_timeout_seconds("2K", timeout)
        full_prompt = prompt
        if style and style.strip():
            from .roteiro_imagen import build_prompt_with_style

            full_prompt = build_prompt_with_style(prompt, style)
        try:
            result = generate_imagen_to_path(
                prompt=full_prompt,
                output_path=output_path,
                api_key=key,
                model_name=model,
                aspect_ratio=aspect_ratio,
                style=None,
                timeout_seconds=timeout_eff,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:800], "provider": "google", "model": model}
        return {
            "ok": True,
            "provider": "google",
            "model": result.get("model_used", model),
            "output_path": result["output_path"],
            "image_path": result["image_path"],
        }

    return generate_nano_image(
        prompt=prompt,
        output_path=output_path,
        resolution=resolution,
        timeout=timeout,
        model=model or "gemini-3-pro-image",
        input_image_path=input_image_path,
    )


def generate_openai_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    timeout: float,
) -> dict[str, Any]:
    key = _provider_api_key("OPENAI_API_KEY")
    if not key or not key.startswith("sk-"):
        return {"ok": False, "error": "OPENAI_API_KEY ausente ou inválida", "provider": "openai", "model": model}

    aliases = {
        "openai-dalle-3": "dall-e-3",
        "dalle-3": "dall-e-3",
        "dalle-2": "dall-e-2",
    }
    model_id = aliases.get((model or "").lower(), model or "gpt-image-1")
    is_gpt = str(model_id).startswith("gpt-image")
    body: dict[str, Any] = {"model": model_id, "prompt": prompt, "n": 1}
    size_map = {"1:1": "1024x1024", "16:9": "1536x1024", "9:16": "1024x1536", "4:3": "1536x1024", "3:4": "1024x1536"}
    if is_gpt:
        body["size"] = size_map.get(aspect_ratio, "auto")
        body["quality"] = "auto"
    else:
        body["size"] = size_map.get(aspect_ratio, "1024x1024")
        body["response_format"] = "b64_json"

    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "openai", "model": model_id}
            payload = resp.json()
            items = payload.get("data") or []
            if not items:
                return {"ok": False, "error": "OpenAI sem imagem", "provider": "openai", "model": model_id}
            first = items[0]
            if first.get("b64_json"):
                raw = base64.b64decode(first["b64_json"])
                saved = _save_bytes(output_path, raw)
            elif first.get("url"):
                saved = _download_url(first["url"], output_path, timeout)
            else:
                return {"ok": False, "error": "OpenAI resposta vazia", "provider": "openai", "model": model_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "openai", "model": model_id}

    return {
        "ok": True,
        "provider": "openai",
        "model": model_id,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def _run_uv_image_script(
    script_parts: tuple[str, ...],
    *,
    output_path: Path,
    timeout: float,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    import os
    import subprocess

    resolved_out = output_path.expanduser().resolve()
    cmd = ["uv", "run", *script_parts]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(resolved_out.parent),
            timeout=max(30.0, float(timeout)),
            check=False,
            env=env or os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {int(timeout)}s"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if completed.returncode != 0 or not resolved_out.is_file():
        detail = (completed.stderr or completed.stdout or "generation failed").strip()
        return {"ok": False, "error": detail[:800]}
    return {"ok": True, "output_path": str(resolved_out), "image_path": str(resolved_out)}


def generate_grok_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    timeout: float,
    input_image_path: Optional[str] = None,
    resolution: str = "2k",
) -> dict[str, Any]:
    """Generate image via xAI Grok Imagine API (direct HTTP, no subprocess)."""
    from .secrets_fallback import resolve_llm_api_key

    key = resolve_llm_api_key("XAI_API_KEY")
    if not key:
        return {"ok": False, "error": "XAI_API_KEY ausente", "provider": "grok", "model": model}

    model_id = model or "grok-imagine-image-quality"
    res = _grok_resolution_for_model(model_id, resolution or "2k")
    out_abs = output_path.expanduser().resolve()
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    body: dict[str, Any] = {"model": model_id, "prompt": prompt}
    if aspect_ratio and aspect_ratio.strip() != "auto":
        body["aspect_ratio"] = aspect_ratio.strip()
    if res:
        body["resolution"] = res

    if input_image_path:
        inp = Path(input_image_path).expanduser().resolve()
        if not inp.is_file():
            return {"ok": False, "error": f"input image not found: {inp}", "provider": "grok", "model": model_id}
        import mimetypes

        mime = mimetypes.guess_type(inp.name)[0] or "image/png"
        b64 = base64.b64encode(inp.read_bytes()).decode("ascii")
        body["image"] = {"url": f"data:{mime};base64,{b64}", "type": "image_url"}
        endpoint = "https://api.x.ai/v1/images/edits"
    else:
        endpoint = "https://api.x.ai/v1/images/generations"

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    effective_timeout = max(30.0, min(float(timeout), 180.0))
    max_retries = _grok_rate_limit_max_retries()
    last_error = ""

    try:
        with httpx.Client(timeout=effective_timeout) as client:
            for attempt in range(max_retries):
                _wait_grok_rate_slot()
                resp = client.post(endpoint, headers=headers, json=body)
                if resp.status_code >= 400:
                    detail = resp.text[:500]
                    last_error = (
                        f"xAI API {resp.status_code}: {detail}"
                    )
                    if (
                        is_grok_rate_limit_error(last_error)
                        and attempt < max_retries - 1
                    ):
                        wait = min(8.0, 1.0 * (1.5 ** attempt))
                        log.warning(
                            "grok rate limit %s/%s — aguardando %.1fs",
                            attempt + 1,
                            max_retries,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    return {
                        "ok": False,
                        "error": last_error,
                        "provider": "grok",
                        "model": model_id,
                    }
                data = resp.json()
                items = data.get("data") or []
                if not items or not isinstance(items[0], dict):
                    return {
                        "ok": False,
                        "error": "xAI sem imagem na resposta",
                        "provider": "grok",
                        "model": model_id,
                    }
                url = str(items[0].get("url") or "").strip()
                if not url:
                    return {
                        "ok": False,
                        "error": "xAI resposta sem URL",
                        "provider": "grok",
                        "model": model_id,
                    }
                img_resp = client.get(url, timeout=120.0)
                img_resp.raise_for_status()
                out_abs.write_bytes(img_resp.content)
                from .image_file_utils import normalize_saved_image

                out_abs = normalize_saved_image(out_abs)
                break
            else:
                return {
                    "ok": False,
                    "error": last_error or "xAI rate limit",
                    "provider": "grok",
                    "model": model_id,
                }
    except httpx.TimeoutException:
        return {"ok": False, "error": f"xAI timeout após {int(effective_timeout)}s", "provider": "grok", "model": model_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "grok", "model": model_id}

    return {
        "ok": True,
        "provider": "grok",
        "model": model_id,
        "resolution": res,
        "output_path": str(out_abs),
        "image_path": str(out_abs),
    }


def _gemini_image_resolution_for_model(model: str, requested: str) -> str:
    """Flash/smaller Gemini image models cap at 1K (cheaper quota tier)."""
    name = (model or "").lower()
    res = (requested or "1K").strip().upper()
    if res not in ("1K", "2K", "4K"):
        res = "1K"
    if "flash" in name and res != "1K":
        return "1K"
    return res


def _grok_resolution_for_model(model: str, requested: str) -> str:
    name = (model or "").lower()
    res = (requested or "2k").strip().lower()
    if res not in ("1k", "2k"):
        res = "2k"
    if name in ("grok-imagine-image", "grok-2-image-1212"):
        return "1k"
    return res


def generate_nano_image(
    *,
    prompt: str,
    output_path: Path,
    resolution: str,
    timeout: float,
    model: str = "gemini-3-pro-image",
    input_image_path: Optional[str] = None,
) -> dict[str, Any]:
    from io import BytesIO

    from .secrets_fallback import build_gemini_subprocess_env, image_generation_timeout_seconds

    model_id = (model or "gemini-3-pro-image").strip()
    run_env, key = build_gemini_subprocess_env()
    if not key:
        return {"ok": False, "error": "GEMINI_API_KEY ausente", "provider": "google", "model": model_id}

    res = _gemini_image_resolution_for_model(model_id, resolution)
    timeout_eff = image_generation_timeout_seconds(res, timeout)

    try:
        from google import genai
        from google.genai import types
        from PIL import Image as PILImage
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"google-genai/Pillow: {exc}",
            "provider": "google",
            "model": model_id,
        }

    import os

    env_backup = {
        "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY"),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    }
    try:
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = key
        client = genai.Client(
            api_key=key,
            http_options={"timeout": int(max(30.0, timeout_eff) * 1000)},
        )
        contents: Any = prompt
        if input_image_path:
            inp = Path(input_image_path).expanduser()
            if inp.is_file():
                contents = [PILImage.open(inp), prompt]

        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(image_size=res),
            ),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:800], "provider": "google", "model": model_id}
    finally:
        if env_backup["GOOGLE_API_KEY"] is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = env_backup["GOOGLE_API_KEY"]
        if env_backup["GEMINI_API_KEY"] is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = env_backup["GEMINI_API_KEY"]

    image_bytes: Optional[bytes] = None
    for part in response.parts:
        if part.inline_data is not None and part.inline_data.data:
            raw = part.inline_data.data
            image_bytes = base64.b64decode(raw) if isinstance(raw, str) else raw
            break

    if not image_bytes:
        return {
            "ok": False,
            "error": "Gemini não retornou imagem",
            "provider": "google",
            "model": model_id,
        }

    try:
        image = PILImage.open(BytesIO(image_bytes))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if image.mode == "RGBA":
            rgb_image = PILImage.new("RGB", image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[3])
            rgb_image.save(str(output_path), "PNG")
        elif image.mode == "RGB":
            image.save(str(output_path), "PNG")
        else:
            image.convert("RGB").save(str(output_path), "PNG")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "google", "model": model_id}

    saved = output_path.resolve()
    return {
        "ok": True,
        "provider": "google",
        "model": model_id,
        "resolution": res,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def _openrouter_aspect_ratio(aspect_ratio: str) -> str:
    ar = (aspect_ratio or "16:9").strip()
    allowed = {
        "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
        "1:4", "4:1", "1:8", "8:1",
    }
    return ar if ar in allowed else "16:9"


def _openrouter_image_size(resolution: str) -> str:
    res = (resolution or "1K").strip().upper()
    if res in ("1K", "2K", "4K", "0.5K"):
        return res
    return "1K"


def _extract_openrouter_image_bytes(message: dict[str, Any]) -> Optional[bytes]:
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            block = item.get("image_url") if isinstance(item.get("image_url"), dict) else item
            url = str((block or {}).get("url") or "").strip()
            if not url:
                continue
            if url.startswith("data:"):
                try:
                    _, b64 = url.split(",", 1)
                    return base64.b64decode(b64)
                except (ValueError, TypeError):
                    continue
            if url.startswith("http"):
                return None  # caller downloads via URL
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                block = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                url = str(block.get("url") or "").strip()
                if url.startswith("data:"):
                    try:
                        _, b64 = url.split(",", 1)
                        return base64.b64decode(b64)
                    except (ValueError, TypeError):
                        pass
    return None


def _extract_openrouter_image_url(message: dict[str, Any]) -> Optional[str]:
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            block = item.get("image_url") if isinstance(item.get("image_url"), dict) else item
            url = str((block or {}).get("url") or "").strip()
            if url.startswith("http"):
                return url
    return None


def generate_openrouter_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    timeout: float,
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
) -> dict[str, Any]:
    """Generate image via OpenRouter chat/completions (modalities image)."""
    from .openrouter_image_catalog import openrouter_modalities_for_model

    key = _provider_api_key("OPENROUTER_API_KEY")
    model_id = (model or "google/gemini-2.5-flash-image").strip()
    if not key:
        return {
            "ok": False,
            "error": "OPENROUTER_API_KEY ausente",
            "provider": "openrouter",
            "model": model_id,
        }

    user_content: Any = prompt
    if input_image_path:
        inp = Path(input_image_path).expanduser()
        if inp.is_file():
            mime = "image/png"
            suffix = inp.suffix.lower()
            if suffix in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif suffix == ".webp":
                mime = "image/webp"
            b64 = base64.b64encode(inp.read_bytes()).decode("ascii")
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]

    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": user_content}],
        "modalities": openrouter_modalities_for_model(model_id),
        "image_config": {
            "aspect_ratio": _openrouter_aspect_ratio(aspect_ratio),
            "image_size": _openrouter_image_size(resolution),
        },
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://qclaw.local",
        "X-Title": "QClaw Image Generation",
    }
    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": resp.text[:800],
                    "provider": "openrouter",
                    "model": model_id,
                }
            payload = resp.json()
            choices = payload.get("choices") or []
            if not choices:
                return {
                    "ok": False,
                    "error": "OpenRouter sem choices",
                    "provider": "openrouter",
                    "model": model_id,
                }
            message = choices[0].get("message") or {}
            if not isinstance(message, dict):
                return {
                    "ok": False,
                    "error": "OpenRouter resposta inválida",
                    "provider": "openrouter",
                    "model": model_id,
                }
            raw_bytes = _extract_openrouter_image_bytes(message)
            if raw_bytes:
                saved = _save_bytes(output_path, raw_bytes)
            else:
                url = _extract_openrouter_image_url(message)
                if not url:
                    return {
                        "ok": False,
                        "error": "OpenRouter não retornou imagem",
                        "provider": "openrouter",
                        "model": model_id,
                    }
                saved = _download_url(url, output_path, timeout)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:800],
            "provider": "openrouter",
            "model": model_id,
        }

    return {
        "ok": True,
        "provider": "openrouter",
        "model": model_id,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def generate_fal_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    timeout: float,
) -> dict[str, Any]:
    key = _provider_api_key("FAL_KEY")
    if not key:
        return {"ok": False, "error": "FAL_KEY ausente", "provider": "fal", "model": model}

    model_id = model or "fal-ai/flux/dev"
    if model_id == "flux":
        model_id = "fal-ai/flux/dev"
    endpoint = f"https://queue.fal.run/{model_id.lstrip('/')}"
    body = {"prompt": prompt, "image_size": "landscape_16_9" if aspect_ratio == "16:9" else "square_hd"}
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(endpoint, headers=headers, json=body)
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "fal", "model": model_id}
            data = resp.json()
            url = None
            for key_name in ("images", "image"):
                block = data.get(key_name)
                if isinstance(block, list) and block:
                    item = block[0]
                    url = item.get("url") if isinstance(item, dict) else item
                    break
            if not url and isinstance(data.get("response"), dict):
                imgs = data["response"].get("images") or []
                if imgs:
                    url = imgs[0].get("url") if isinstance(imgs[0], dict) else imgs[0]
            if not url:
                return {"ok": False, "error": "FAL sem URL de imagem", "provider": "fal", "model": model_id}
            saved = _download_url(str(url), output_path, timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "fal", "model": model_id}

    return {"ok": True, "provider": "fal", "model": model_id, "output_path": str(saved), "image_path": str(saved)}


def generate_wavespeed_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    key = _provider_api_key("WAVESPEED_API_KEY")
    if not key:
        return {"ok": False, "error": "WAVESPEED_API_KEY ausente", "provider": "wavespeed", "model": model}

    model_id = model or "wavespeedai/flux-2-dev-text-to-image"
    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(
                "https://api.wavespeed.ai/api/v3/predictions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_id, "input": {"prompt": prompt}},
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "wavespeed", "model": model_id}
            data = resp.json()
            output = data.get("output") or {}
            url = output.get("url") if isinstance(output, dict) else None
            if not url and isinstance(output, str) and output.startswith("http"):
                url = output
            if not url:
                return {"ok": False, "error": "WaveSpeed sem imagem", "provider": "wavespeed", "model": model_id}
            saved = _download_url(str(url), output_path, timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "wavespeed", "model": model_id}

    return {
        "ok": True,
        "provider": "wavespeed",
        "model": model_id,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def generate_byteplus_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    key = _provider_api_key("BYTEPLUS_API_KEY")
    if not key:
        return {"ok": False, "error": "BYTEPLUS_API_KEY ausente", "provider": "byteplus", "model": model}

    model_id = model or "seedream-4.0"
    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(
                "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_id, "prompt": prompt, "size": "1024x1024", "n": 1},
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "byteplus", "model": model_id}
            data = resp.json()
            items = data.get("data") or []
            if not items:
                return {"ok": False, "error": "BytePlus sem imagem", "provider": "byteplus", "model": model_id}
            item = items[0]
            if item.get("b64_json"):
                saved = _save_bytes(output_path, base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                saved = _download_url(item["url"], output_path, timeout)
            else:
                return {"ok": False, "error": "BytePlus resposta inválida", "provider": "byteplus", "model": model_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "byteplus", "model": model_id}

    return {
        "ok": True,
        "provider": "byteplus",
        "model": model_id,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def generate_stability_image(
    *,
    prompt: str,
    output_path: Path,
    model: str,
    aspect_ratio: str,
    timeout: float,
) -> dict[str, Any]:
    key = _provider_api_key("STABILITY_API_KEY")
    if not key:
        return {"ok": False, "error": "STABILITY_API_KEY ausente", "provider": "stability", "model": model}

    model_id = model or "stable-diffusion-3.5-large"
    ar = aspect_ratio if aspect_ratio in ("1:1", "16:9", "9:16", "4:3", "3:4") else "16:9"
    try:
        with httpx.Client(timeout=max(60.0, timeout)) as client:
            resp = client.post(
                "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                data={"prompt": prompt, "model": model_id, "aspect_ratio": ar, "output_format": "png"},
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "stability", "model": model_id}
            payload = resp.json()
            b64 = payload.get("image") or payload.get("artifacts", [{}])[0].get("base64")
            if not b64:
                return {"ok": False, "error": "Stability sem imagem", "provider": "stability", "model": model_id}
            saved = _save_bytes(output_path, base64.b64decode(b64))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "stability", "model": model_id}

    return {
        "ok": True,
        "provider": "stability",
        "model": model_id,
        "output_path": str(saved),
        "image_path": str(saved),
    }


def generate_via_roteiro_viral_api(
    *,
    prompt: str,
    output_path: Path,
    aspect_ratio: str,
    model: Optional[str],
    timeout: float,
) -> dict[str, Any]:
    try:
        from .roteiro_viral_local.config import use_local_rv
        from .roteiro_viral_local.images import handle_generate_image
        from .roteiro_imagen import resolve_gemini_api_key

        if use_local_rv():
            body: dict[str, Any] = {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio or "16:9",
                "api_key": resolve_gemini_api_key(),
            }
            if model:
                body["model_name"] = model
            data = handle_generate_image(body)
            file_path = str(data.get("file_path") or "").strip()
            if not file_path:
                return {"ok": False, "error": "Local RV sem file_path", "provider": "roteiro_viral_local"}
            src = Path(file_path)
            if not src.is_file():
                return {"ok": False, "error": f"Arquivo não encontrado: {file_path}", "provider": "roteiro_viral_local"}
            saved = _save_bytes(output_path, src.read_bytes())
            return {
                "ok": True,
                "provider": "roteiro_viral_local",
                "model": data.get("model_used") or model or "imagen-4",
                "output_path": str(saved),
                "image_path": str(saved),
            }
    except Exception as exc:
        from .roteiro_viral_local.config import use_local_rv

        if use_local_rv():
            return {"ok": False, "error": str(exc)[:500], "provider": "roteiro_viral_local"}

    from .roteiro_viral_keys import roteiro_viral_api_base

    base = roteiro_viral_api_base()
    body: dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio or "16:9"}
    if model:
        body["model_name"] = model
    try:
        with httpx.Client(timeout=max(90.0, timeout)) as client:
            resp = client.post(f"{base}/tools/generate-image", json=body)
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:500], "provider": "roteiro_viral_api"}
            data = resp.json()
            file_path = str(data.get("file_path") or "").strip()
            if not file_path:
                return {"ok": False, "error": "RV API sem file_path", "provider": "roteiro_viral_api"}
            file_resp = client.get(f"{base}/files", params={"path": file_path})
            if file_resp.status_code >= 400:
                return {"ok": False, "error": f"RV file download {file_resp.status_code}", "provider": "roteiro_viral_api"}
            saved = _save_bytes(output_path, file_resp.content)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "provider": "roteiro_viral_api"}

    return {
        "ok": True,
        "provider": "roteiro_viral_api",
        "model": data.get("model_used") or model or "rv-default",
        "output_path": str(saved),
        "image_path": str(saved),
    }


def generate_with_rv_provider(
    *,
    provider: str,
    model: str,
    prompt: str,
    output_path: Path,
    aspect_ratio: str,
    style: Optional[str],
    timeout: float,
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
) -> dict[str, Any]:
    prov = (provider or "google").strip().lower()
    common = {
        "prompt": prompt,
        "output_path": output_path,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "timeout": timeout,
    }
    if prov == "google":
        return generate_google_image(style=style, resolution=resolution, **common)
    if prov == "openai":
        return generate_openai_image(**common)
    if prov == "grok":
        grok_res = resolution.strip().lower() if resolution else "2k"
        if grok_res in ("1k", "2k", "4k"):
            grok_res = "2k" if grok_res == "4k" else grok_res
        else:
            grok_res = "2k"
        return generate_grok_image(
            input_image_path=input_image_path,
            resolution=grok_res,
            **common,
        )
    if prov == "fal":
        return generate_fal_image(**common)
    if prov == "wavespeed":
        return generate_wavespeed_image(**common)
    if prov in ("byteplus", "modelark"):
        return generate_byteplus_image(**common)
    if prov == "stability":
        return generate_stability_image(**common)
    if prov == "openrouter":
        return generate_openrouter_image(
            resolution=resolution,
            input_image_path=input_image_path,
            **common,
        )
    return {"ok": False, "error": f"provider não suportado: {provider}", "provider": prov, "model": model}

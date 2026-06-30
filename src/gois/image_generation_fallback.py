"""Local image generation with Roteiro Viral provider/model fallback."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

from .image_provider_clients import (
    generate_via_roteiro_viral_api,
    generate_with_rv_provider,
    grok_script_path,
    is_grok_rate_limit_error,
    nano_script_path,
)
from .roteiro_viral_image_catalog import build_fallback_attempts
from .roteiro_viral_keys import IMAGE_PROVIDER_KEY_ENV

DEFAULT_IMAGEN_MODEL = "imagen-4.0-ultra-generate-001"
FAST_IMAGEN_MODEL = "imagen-4.0-fast-generate-001"
DEFAULT_GROK_IMAGINE_MODEL = "grok-imagine-image-quality"

IMAGEN_MODEL_CHAIN: tuple[str, ...] = (DEFAULT_IMAGEN_MODEL, FAST_IMAGEN_MODEL)

PROVIDER_CHAINS: dict[str, tuple[str, ...]] = {
    "imagen": ("imagen", "openrouter", "nano", "grok"),
    "nano": ("nano", "openrouter", "imagen", "grok"),
    "grok": ("grok", "openrouter", "nano", "imagen"),
    "openrouter": ("openrouter", "imagen", "nano", "grok"),
}

_QUOTA_MARKERS: tuple[str, ...] = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "exceeded your current quota",
)

_GOOGLE_BILLING_EXHAUSTED_MARKERS: tuple[str, ...] = (
    "prepayment credits are depleted",
    "prepayment credits depleted",
    "billing account",
    "enable billing",
    "payment required",
    "insufficient funds",
    "credit balance",
)


def parse_allow_fallback_flag(payload: dict[str, Any], *, default: bool = True) -> bool:
    """Resolve allow_fallback from tool/CLI args (supports no_fallback aliases)."""
    if payload.get("no_fallback") or payload.get("disable_fallback"):
        return False
    if "allow_fallback" in payload:
        val = payload.get("allow_fallback")
        if isinstance(val, str):
            return val.strip().lower() not in ("0", "false", "no", "off")
        return bool(val)
    return default


def is_retriable_quota_error(error: str) -> bool:
    """True when failure is likely quota/rate-limit (try smaller model next)."""
    text = (error or "").strip().lower()
    if not text:
        return False
    if is_google_billing_exhausted(text):
        return False
    return any(marker in text for marker in _QUOTA_MARKERS)


def is_google_billing_exhausted(error: str) -> bool:
    """True when Google/Gemini billing credits are gone (skip remaining google attempts)."""
    text = (error or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _GOOGLE_BILLING_EXHAUSTED_MARKERS):
        return True
    if "resource_exhausted" in text and (
        "credit" in text or "billing" in text or "prepay" in text
    ):
        return True
    return False


def summarize_image_generation_error(
    error: str,
    result: Optional[dict[str, Any]] = None,
) -> str:
    """Turn provider errors into a short user-facing diagnosis."""
    err = (error or "").strip()
    if not err and isinstance(result, dict):
        err = str(result.get("error") or "").strip()
    lower = err.lower()
    hints: list[str] = []
    if any(
        marker in lower
        for marker in (
            "resource_exhausted",
            "prepayment credits are depleted",
            "quota exceeded",
            "exceeded your current quota",
        )
    ):
        hints.append(
            "Créditos Gemini/Imagen esgotados — recarregue em "
            "https://ai.studio/projects"
        )
    if "permission-denied" in lower or (
        "403" in lower and ("credit" in lower or "spending limit" in lower)
    ):
        hints.append(
            "Créditos xAI/Grok esgotados ou limite mensal atingido"
        )
    if "gemini_api_key" in lower or "api_key ausente" in lower:
        hints.append("Chave GEMINI_API_KEY ausente ou inválida")
    if "openrouter_api_key" in lower:
        hints.append("Chave OPENROUTER_API_KEY ausente ou inválida (openrouter.ai/keys)")
    if "timeout" in lower or "timed out" in lower or (
        isinstance(result, dict) and result.get("timeout")
    ):
        hints.append(
            "A API não respondeu a tempo — tente outro modelo ou reduza a resolução"
        )
    if is_grok_rate_limit_error(err):
        hints.append(
            "Rate-limit Grok/xAI (5 req/s) — o sistema já aguarda e reenvia; "
            "se persistir, reduza paralelismo ou espere alguns segundos"
        )
    elif is_retriable_quota_error(err) and not hints:
        hints.append("Quota/rate-limit do provider — aguarde ou troque de modelo")
    if not hints:
        return err
    return err + "\n\n" + "\n".join(f"→ {hint}" for hint in hints)


def _grok_script_path() -> Path:
    return grok_script_path()


def _nano_script_path() -> Path:
    return nano_script_path()


def attach_image_data_url(result: dict[str, Any], saved: Path, prompt: str) -> None:
    try:
        file_size = saved.stat().st_size
        if file_size > 8 * 1024 * 1024:
            return
        raw = saved.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        from .image_file_utils import mime_type_for_path

        mime = mime_type_for_path(saved, data=raw[:16])
        result["image_data_url"] = f"data:{mime};base64,{b64}"
        result["caption"] = prompt[:120]
    except OSError:
        pass


def provider_has_credentials(provider: str) -> bool:
    from .roteiro_viral_keys import provider_has_image_credentials

    return provider_has_image_credentials(provider)


def build_provider_chain(
    primary: str,
    *,
    allow_fallback: bool = True,
    input_image_path: Optional[str] = None,
) -> list[str]:
    """Legacy skill-level chain (imagen/nano/grok)."""
    prov = (primary or "imagen").strip().lower()
    if prov not in PROVIDER_CHAINS:
        prov = "imagen"
    chain = list(PROVIDER_CHAINS[prov])
    if input_image_path and str(input_image_path).strip():
        chain = [p for p in chain if p != "imagen"]
    if not allow_fallback:
        return [prov] if prov in chain else [chain[0]]
    return chain


def _resolve_work_dir(cwd: Optional[str]) -> Path:
    work_dir = Path(cwd).expanduser() if cwd else Path.cwd()
    if not work_dir.is_dir():
        work_dir = Path.home()
    return work_dir.resolve()


def _normalize_image_target(
    filename: str,
    work_dir: Path,
) -> tuple[Path, str]:
    """Return absolute work_dir and basename — never a nested relative path."""
    name = (filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    path = Path(name).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
        return resolved.parent, resolved.name
    # Relative paths like ".stack/.../slide-001.png" must not nest under cwd.
    return work_dir.resolve(), path.name


def resolve_existing_image(work_dir: Path, filename: str) -> Optional[Path]:
    """Return an on-disk image for this slide/filename stem if already generated."""
    name = Path((filename or "").strip()).name
    if not name:
        return None
    direct = work_dir / name
    if direct.is_file() and direct.stat().st_size > 0:
        return direct
    stem = Path(name).stem
    if not stem:
        return None
    for candidate in sorted(work_dir.glob(f"{stem}*")):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _output_path_for_attempt(work_dir: Path, filename: str, provider: str, model: str) -> Path:
    out_name = Path(filename).name
    stem = Path(out_name).stem or "image"
    ext = Path(out_name).suffix.lower()
    if not ext:
        ext = ".jpeg" if provider == "grok" else ".png"
    safe_model = model.replace("/", "-").replace(":", "-")[:40]
    return work_dir / f"{stem}-{provider}-{safe_model}{ext}"


def _ensure_canonical_output(
    result: dict[str, Any],
    work_dir: Path,
    out_name: str,
) -> dict[str, Any]:
    """Copy provider-suffixed output to the requested filename when they differ."""
    if not result.get("ok"):
        return result
    canonical = (work_dir / out_name).resolve()
    saved_raw = result.get("output_path") or result.get("image_path")
    if not saved_raw:
        return result
    saved = Path(str(saved_raw)).expanduser().resolve()
    if not saved.is_file():
        return result
    if saved != canonical:
        import shutil

        canonical.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, canonical)
        saved = canonical
    from .image_file_utils import normalize_saved_image

    normalized = normalize_saved_image(saved)
    result["output_path"] = str(normalized.resolve())
    result["image_path"] = str(normalized.resolve())
    return result


def _finalize_result(
    result: dict[str, Any],
    prompt: str,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    path_raw = result.get("output_path") or result.get("image_path")
    if path_raw:
        saved = Path(str(path_raw))
        if saved.is_file():
            attach_image_data_url(result, saved, prompt)
            try:
                from .generated_images_store import register_from_generation_result

                register_from_generation_result(result, prompt=prompt, **(register_meta or {}))
            except Exception:
                pass
    return result


def generate_image_with_fallback(
    *,
    prompt: str,
    filename: str,
    primary_provider: str = "imagen",
    allow_fallback: bool = True,
    cwd: Optional[str] = None,
    timeout: float = 180.0,
    model: Optional[str] = None,
    aspect_ratio: str = "16:9",
    style: Optional[str] = None,
    resolution: str = "1K",
    input_image_path: Optional[str] = None,
    grok_model: Optional[str] = None,
    grok_resolution: Optional[str] = None,
    grok_aspect_ratio: Optional[str] = None,
    skip_existing: bool = False,
    register_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Try every RV catalog model/provider, then Roteiro Viral API as last resort."""
    import logging as _log

    _logger = _log.getLogger(__name__)

    work_dir_raw = _resolve_work_dir(cwd)
    work_dir, out_name = _normalize_image_target(filename, work_dir_raw)
    if skip_existing:
        existing = resolve_existing_image(work_dir, out_name)
        if existing is not None:
            result = {
                "ok": True,
                "output_path": str(existing.resolve()),
                "image_path": str(existing.resolve()),
                "resumed": True,
                "primary_provider": primary_provider,
                "provider_used": primary_provider,
            }
            result = _ensure_canonical_output(result, work_dir, out_name)
            return _finalize_result(result, prompt, register_meta)
    preferred = (model or grok_model or "").strip() or None
    attempts = build_fallback_attempts(
        primary_provider,
        allow_fallback=allow_fallback,
        preferred_model=preferred,
        input_image_path=input_image_path,
    )

    import time as _time

    log_attempts: list[dict[str, Any]] = []
    primary = (primary_provider or "imagen").strip().lower()
    attempt_timeout = timeout
    google_billing_exhausted = False
    grok_rate_limited = False

    for attempt in attempts:
        if grok_rate_limited and attempt.provider == "grok":
            _time.sleep(1.2)
            grok_rate_limited = False
        if google_billing_exhausted and attempt.provider == "google":
            log_attempts.append(
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "ok": False,
                    "error": "google billing exhausted — skipped",
                    "skipped": True,
                }
            )
            continue
        if not provider_has_credentials(attempt.provider):
            env_hint = IMAGE_PROVIDER_KEY_ENV.get(attempt.provider, "")
            err = f"sem credencial ({env_hint})" if env_hint else "sem credencial"
            log_attempts.append(
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "ok": False,
                    "error": err,
                }
            )
            continue

        out_path = _output_path_for_attempt(work_dir, out_name, attempt.provider, attempt.model)
        aspect = grok_aspect_ratio or aspect_ratio
        if attempt.provider == "grok" and grok_aspect_ratio:
            aspect = grok_aspect_ratio

        result = generate_with_rv_provider(
            provider=attempt.provider,
            model=attempt.model,
            prompt=prompt,
            output_path=out_path,
            aspect_ratio=aspect,
            style=style if attempt.provider == "google" else None,
            timeout=attempt_timeout,
            resolution=resolution,
            input_image_path=input_image_path,
        )
        result = _finalize_result(result, prompt, register_meta)
        attempt_entry = {
            "provider": attempt.provider,
            "model": attempt.model,
            "ok": bool(result.get("ok")),
            "error": (result.get("error") or "")[:300],
        }
        log_attempts.append(attempt_entry)
        if not result.get("ok"):
            err_text = attempt_entry["error"]
            if attempt.provider == "grok" and is_grok_rate_limit_error(err_text):
                grok_rate_limited = True
                if primary == "grok":
                    _logger.warning(
                        "grok rate limit on %s — pausa antes do próximo modelo",
                        attempt.model,
                    )
            if attempt.provider == "google" and is_google_billing_exhausted(err_text):
                google_billing_exhausted = True
                _logger.warning(
                    "google billing exhausted — skipping remaining google models"
                )
            _logger.warning(
                "image provider failed: %s/%s — %s",
                attempt.provider,
                attempt.model,
                err_text[:200],
            )
        if result.get("ok"):
            skill_provider = primary
            if attempt.provider != primary and attempt.model != preferred:
                result["fallback_used"] = True
            result["primary_provider"] = primary_provider
            result["provider_used"] = attempt.provider
            result["model_used"] = result.get("model") or attempt.model
            result["fallback_attempts"] = log_attempts
            result = _ensure_canonical_output(result, work_dir, out_name)
            return _finalize_result(result, prompt, register_meta)

    if allow_fallback:
        rv_out = generate_via_roteiro_viral_api(
            prompt=prompt,
            output_path=work_dir / out_name,
            aspect_ratio=aspect_ratio,
            model=preferred,
            timeout=attempt_timeout,
        )
        rv_out = _finalize_result(rv_out, prompt, register_meta)
        log_attempts.append(
            {
                "provider": "roteiro_viral_api",
                "model": rv_out.get("model") or preferred or "",
                "ok": bool(rv_out.get("ok")),
                "error": (rv_out.get("error") or "")[:300],
            }
        )
        if rv_out.get("ok"):
            rv_out["fallback_used"] = True
            rv_out["primary_provider"] = primary_provider
            rv_out["provider_used"] = "roteiro_viral_api"
            rv_out["fallback_attempts"] = log_attempts
            rv_out = _ensure_canonical_output(rv_out, work_dir, out_name)
            return _finalize_result(rv_out, prompt)

    errors = "; ".join(
        f"{a.get('provider')}:{a.get('model')}={a.get('error') or 'failed'}"
        for a in log_attempts
        if not a.get("ok")
    )
    return {
        "ok": False,
        "error": errors[:1200] or "All image providers failed",
        "primary_provider": primary_provider,
        "fallback_attempts": log_attempts,
    }


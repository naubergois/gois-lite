"""Local Imagen 4 image generation (Roteiro Viral logic, no RV API)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

DEFAULT_IMAGEN_MODEL = "imagen-4.0-ultra-generate-001"
FAST_IMAGEN_MODEL = "imagen-4.0-fast-generate-001"
SUPPORTED_ASPECT_RATIOS = frozenset({"1:1", "9:16", "16:9", "4:3", "3:4"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_vendor_styles() -> dict[str, str]:
    from .roteiro_viral_local.styles import load_image_styles

    return load_image_styles()


def normalize_aspect_ratio(aspect_ratio: str) -> str:
    ratio = (aspect_ratio or "16:9").strip()
    if ratio == "2:3":
        return "3:4"
    if ratio in SUPPORTED_ASPECT_RATIOS:
        return ratio
    return "16:9"


def build_prompt_with_style(prompt: str, style: Optional[str] = None) -> str:
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required")
    style_name = (style or "").strip()
    if not style_name:
        return text
    styles = _ensure_vendor_styles()
    modifier = styles.get(style_name, "")
    if modifier:
        return f"{text}\n\n{modifier}"
    return f"{text}\n\nEstilo: {style_name}"


def build_prompt_with_styles(prompt: str, style_names: list[str]) -> str:
    """Fuse multiple IMAGE_STYLES modifiers into one Imagen prompt (Roteiro Viral combined mode)."""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required")
    names = [str(s).strip() for s in (style_names or []) if str(s).strip()]
    if not names:
        return text
    if len(names) == 1:
        return build_prompt_with_style(text, names[0])
    catalog = _ensure_vendor_styles()
    modifiers: list[str] = []
    for name in names:
        mod = (catalog.get(name) or "").strip()
        modifiers.append(mod if mod else f"Estilo: {name}")
    label = " + ".join(names)
    combined = ", ".join(modifiers)
    return (
        f"{text}\n\n"
        f"Combine harmoniosamente os seguintes estilos visuais ({label}):\n"
        f"{combined}"
    )


def resolve_gemini_api_key(explicit: Optional[str] = None) -> Optional[str]:
    from .secrets_fallback import resolve_google_gemini_api_key

    return resolve_google_gemini_api_key(explicit)


def _imagen_script_path() -> Path:
    return _repo_root() / "skills/qclaw-roteiro-gerar-imagem/scripts/generate_image.py"


def _generate_imagen_via_uv_subprocess(
    *,
    prompt: str,
    full_prompt: str,
    output_path: Path,
    api_key: str,
    model_name: str,
    aspect_ratio: str,
    picked_styles: list[str],
    style: Optional[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run Imagen via PEP 723 script when google-genai is missing in-process."""
    import json
    import subprocess

    from .secrets_fallback import build_gemini_subprocess_env

    script = _imagen_script_path()
    if not script.is_file():
        raise RuntimeError(
            f"google-genai não instalado e script ausente: {script}. Execute: uv sync"
        )

    resolved_out = output_path.expanduser().resolve()
    cmd: list[str] = [
        "uv",
        "run",
        str(script),
        "--no-fallback",
        "--filename",
        str(resolved_out),
        "--aspect",
        aspect_ratio,
        "--model",
        model_name,
    ]
    if len(picked_styles) > 1:
        cmd.extend(["--prompt", full_prompt])
    else:
        cmd.extend(["--prompt", prompt])
        single = picked_styles[0] if picked_styles else (style or "")
        if single:
            cmd.extend(["--style", single])
    if api_key:
        cmd.extend(["--api-key", api_key])

    run_env, _ = build_gemini_subprocess_env(explicit_key=api_key)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_repo_root()),
            timeout=max(30.0, float(timeout_seconds)),
            check=False,
            env=run_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Imagen timeout após {int(timeout_seconds)}s") from exc
    except OSError as exc:
        raise RuntimeError(f"uv run imagem falhou: {exc}") from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        detail = (completed.stderr or "sem saída do subprocess").strip()
        raise RuntimeError(detail[:800])

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(stdout[:800]) from exc

    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or payload)[:800])

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not resolved_out.is_file():
        raise RuntimeError("Imagen subprocess não criou ficheiro de saída")

    style_label = result.get("style") or (
        " + ".join(picked_styles)
        if len(picked_styles) > 1
        else (picked_styles[0] if picked_styles else (style or ""))
    )
    return {
        "ok": True,
        "output_path": str(resolved_out),
        "image_path": str(resolved_out),
        "model_used": result.get("model_used", model_name),
        "aspect_ratio": result.get("aspect_ratio", aspect_ratio),
        "style": style_label,
        "styles": picked_styles,
        "prompt_chars": len(full_prompt),
        "via_subprocess": True,
    }


def generate_imagen_to_path(
    *,
    prompt: str,
    output_path: Path,
    api_key: Optional[str] = None,
    model_name: str = DEFAULT_IMAGEN_MODEL,
    aspect_ratio: str = "16:9",
    style: Optional[str] = None,
    styles: Optional[list[str]] = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Generate one PNG with Imagen 4 via google-genai (local, no RV API)."""
    key = resolve_gemini_api_key(api_key)
    if not key:
        raise ValueError(
            "GEMINI_API_KEY / GOOGLE_API_KEY necessária (Chaves & Secrets ou .env)"
        )

    picked = [str(s).strip() for s in (styles or []) if str(s).strip()][:6]
    if not picked and style and str(style).strip():
        raw = str(style).strip()
        picked = [p.strip() for p in raw.split(" + ") if p.strip()] if " + " in raw else [raw]
    if len(picked) > 1:
        full_prompt = build_prompt_with_styles(prompt, picked)
    else:
        full_prompt = build_prompt_with_style(prompt, picked[0] if picked else style)
    final_ratio = normalize_aspect_ratio(aspect_ratio)
    model = (model_name or DEFAULT_IMAGEN_MODEL).strip() or DEFAULT_IMAGEN_MODEL

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return _generate_imagen_via_uv_subprocess(
            prompt=prompt,
            full_prompt=full_prompt,
            output_path=output_path,
            api_key=key,
            model_name=model,
            aspect_ratio=final_ratio,
            picked_styles=picked,
            style=style,
            timeout_seconds=timeout_seconds,
        )

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
            http_options={"timeout": int(max(30.0, timeout_seconds) * 1000)},
        )
        config = types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=final_ratio,
            include_rai_reason=True,
            output_mime_type="image/png",
            person_generation=types.PersonGeneration.ALLOW_ADULT,
        )
        response = client.models.generate_images(
            model=model,
            prompt=full_prompt,
            config=config,
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
    if not response.generated_images:
        raise RuntimeError("Imagen não retornou imagem (filtro de conteúdo ou erro da API)")

    image_bytes = response.generated_images[0].image.image_bytes
    if not image_bytes:
        raise RuntimeError("Imagen retornou resposta vazia")

    output_path.write_bytes(image_bytes)
    return {
        "ok": True,
        "output_path": str(output_path),
        "image_path": str(output_path),
        "model_used": model,
        "aspect_ratio": final_ratio,
        "style": " + ".join(picked) if len(picked) > 1 else (picked[0] if picked else (style or "")),
        "styles": picked,
        "prompt_chars": len(full_prompt),
    }


def dispatch_imagen_generate(args: dict[str, Any]) -> dict[str, Any]:
    """Shared handler for chat tool and MCP."""
    from .image_generation_fallback import generate_image_with_fallback

    prompt = str(args.get("prompt") or "").strip()
    filename = str(args.get("filename") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}
    if not filename:
        return {"ok": False, "error": "filename is required"}

    model = str(args.get("model") or DEFAULT_IMAGEN_MODEL).strip()
    aspect_ratio = str(args.get("aspect_ratio") or "16:9").strip()
    style = str(args.get("style") or "").strip() or None

    cwd_raw = args.get("cwd") or args.get("output_dir")
    cwd: Optional[str] = None
    if cwd_raw:
        p = Path(str(cwd_raw).strip()).expanduser()
        if p.is_dir():
            cwd = str(p.resolve())

    try:
        timeout = float(args.get("timeout_seconds") or 180.0)
    except (TypeError, ValueError):
        timeout = 180.0

    return generate_image_with_fallback(
        prompt=prompt,
        filename=filename,
        primary_provider="imagen",
        cwd=cwd,
        timeout=max(30.0, timeout),
        model=model,
        aspect_ratio=aspect_ratio,
        style=style,
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "imagen_generate",
            "description": (
                "Gera imagem com Imagen 4 (Google Gemini API) localmente — sem API Roteiro Viral. "
                "Suporta estilos IMAGE_STYLES do roteiro viral. Skill: qclaw-roteiro-gerar-imagem."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Descrição da imagem"},
                    "filename": {
                        "type": "string",
                        "description": "Nome do ficheiro PNG de saída",
                    },
                    "model": {
                        "type": "string",
                        "description": "Modelo Imagen (default imagen-4.0-ultra-generate-001)",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "16:9, 1:1, 9:16, 4:3 ou 3:4",
                    },
                    "style": {
                        "type": "string",
                        "description": "Nome exacto em IMAGE_STYLES (opcional)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Directório de saída (opcional)",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout em segundos (default 180)",
                    },
                },
                "required": ["prompt", "filename"],
            },
        }
    ]

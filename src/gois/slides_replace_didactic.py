"""Replace slides with didactic AI images — chat and MCP helpers."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .image_generation_fallback import parse_allow_fallback_flag


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-slides-replace-didactic"
        / "scripts"
        / "replace_slide_with_image.py"
    )


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def _stream_block_progress_line(line: str, progress_job_id: str) -> None:
    from .chat_jobs import parse_block_progress, set_block_progress

    text = (line or "").strip()
    if not text:
        return
    parsed = parse_block_progress(text)
    if parsed is None:
        return
    turn, total = parsed
    detail = ""
    if "QCLAW_BLOCK" in text.upper():
        parts = text.split(None, 3)
        if len(parts) >= 4:
            detail = parts[3].strip()
    set_block_progress(progress_job_id, turn, total, message=detail or None)


def _run_command_with_optional_block_progress(
    cmd: list[str],
    *,
    cwd: str,
    timeout: float,
    progress_job_id: Optional[str],
) -> subprocess.CompletedProcess[str]:
    if not progress_job_id:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=max(60.0, float(timeout)),
            check=False,
        )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    stderr_lines: list[str] = []

    def _collect_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            stderr_lines.append(raw)
            _stream_block_progress_line(raw, progress_job_id)

    reader = threading.Thread(target=_collect_stderr, daemon=True)
    reader.start()
    try:
        stdout, _ = proc.communicate(timeout=max(60.0, float(timeout)))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    reader.join(timeout=1.0)
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode or 0,
        stdout=stdout or "",
        stderr="".join(stderr_lines),
    )


def _run_slides_replace_didactic(
    *,
    path: str,
    slides: str,
    analyze: bool = False,
    prompt: str = "",
    output_path: Optional[str] = None,
    provider: str = "imagen",
    style: str = "",
    keep_title: bool = False,
    assets_dir: Optional[str] = None,
    timeout: float = 900.0,
    progress_job_id: Optional[str] = None,
    allow_fallback: bool = True,
    model: str = "",
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"slides-replace-didactic script not found: {script}"}

    deck = Path(str(path).strip()).expanduser()
    if not deck.is_file():
        return {"ok": False, "error": f"deck not found: {deck}"}

    slide_spec = str(slides or "").strip()
    if not slide_spec:
        return {"ok": False, "error": "slides is required (e.g. 3, 2-5, 2,4,7)"}

    prov = str(provider or "imagen").strip().lower()
    if prov not in ("imagen", "nano", "grok"):
        prov = "imagen"

    cmd = ["uv", "run", str(script), str(deck.resolve()), "--slides", slide_spec]
    if analyze:
        cmd.append("--analyze")
    else:
        if prompt:
            cmd.extend(["--prompt", str(prompt).strip()])
        if output_path:
            cmd.extend(["--output", str(Path(output_path).expanduser())])
        cmd.extend(["--provider", prov])
        if style:
            cmd.extend(["--style", str(style).strip()])
        if keep_title:
            cmd.append("--keep-title")
        if assets_dir:
            cmd.extend(["--assets-dir", str(Path(assets_dir).expanduser())])
        if not allow_fallback:
            cmd.append("--no-fallback")
        if str(model or "").strip():
            cmd.extend(["--model", str(model).strip()])
    cmd.append("--json")

    work_dir = deck.parent if deck.parent.is_dir() else Path.cwd()
    try:
        completed = _run_command_with_optional_block_progress(
            cmd,
            cwd=str(work_dir),
            timeout=max(60.0, float(timeout)),
            progress_job_id=progress_job_id if not analyze else None,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"slides_replace_didactic timed out after {int(timeout)}s",
            "path": str(deck),
        }
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        return {
            "ok": False,
            "error": detail[:1200],
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
            "path": str(deck),
        }

    if analyze:
        return {
            "ok": True,
            "analyze": True,
            "input": str(deck),
            **payload,
        }

    applied = payload.get("applied") or []
    skipped = payload.get("skipped") or []
    out_file = str(payload.get("output") or output_path or "").strip()
    result: dict[str, Any] = {
        "ok": True,
        "input": str(deck),
        "output": out_file or None,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "provider": prov,
        "slides": slide_spec,
        "allow_fallback": allow_fallback,
    }
    if out_file:
        result["output_path"] = out_file
    return result


def dispatch_slides_replace_didactic(args: dict[str, Any]) -> dict[str, Any]:
    """Analyze or replace slides with didactic images (shared by chat and MCP)."""
    path = str(args.get("path") or args.get("input") or "").strip()
    if not path:
        return {"ok": False, "error": "path is required"}

    slides = str(args.get("slides") or "").strip()
    if not slides:
        return {"ok": False, "error": "slides is required (e.g. 3, 2-5)"}

    analyze = bool(args.get("analyze"))
    try:
        timeout = float(args.get("timeout_seconds") or 900.0)
    except (TypeError, ValueError):
        timeout = 900.0

    return _run_slides_replace_didactic(
        path=path,
        slides=slides,
        analyze=analyze,
        prompt=str(args.get("prompt") or "").strip(),
        output_path=str(args.get("output_path") or args.get("output") or "").strip() or None,
        provider=str(args.get("provider") or "imagen").strip(),
        style=str(args.get("style") or "").strip(),
        keep_title=bool(args.get("keep_title")),
        assets_dir=str(args.get("assets_dir") or "").strip() or None,
        timeout=max(60.0, timeout),
        allow_fallback=parse_allow_fallback_flag(args),
        model=str(args.get("model") or args.get("grok_model") or "").strip(),
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    """MCP tool descriptor for qclaw-skills server."""
    return [
        {
            "name": "slides_replace_didactic",
            "description": (
                "Substitui slides de PPTX/HTML por imagem didática 16:9 gerada por Nano Banana "
                "ou Grok, usando o texto do slide no prompt. "
                "Skill: qclaw-slides-replace-didactic. Equivalente a "
                "qclaw_slides_replace_didactic no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho absoluto do deck (.pptx, .ppt, .html)",
                    },
                    "slides": {
                        "type": "string",
                        "description": "Slides a substituir: 3, 2-5, 2,4,7",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Se true, só extrai conteúdo dos slides (sem gerar imagens)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Tema opcional; se vazio, usa texto do slide",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Caminho de saída do deck (opcional)",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["imagen", "nano", "grok"],
                        "description": "Gerador de imagem (imagen=Imagen 4 local, nano=Gemini, grok=xAI)",
                    },
                    "style": {
                        "type": "string",
                        "description": "Modifier de estilo visual didático (opcional)",
                    },
                    "keep_title": {
                        "type": "boolean",
                        "description": "Manter título PPTX sobre a imagem (default false)",
                    },
                    "assets_dir": {
                        "type": "string",
                        "description": "Pasta para PNGs geradas (opcional)",
                    },
                    "allow_fallback": {
                        "type": "boolean",
                        "description": (
                            "Se false (ou no_fallback=true), mantém o provider escolhido "
                            "em todos os slides — sem trocar para outro gerador."
                        ),
                    },
                    "no_fallback": {
                        "type": "boolean",
                        "description": "Alias de allow_fallback=false",
                    },
                    "model": {
                        "type": "string",
                        "description": "Modelo preferido (ex.: grok-imagine-image-quality)",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout em segundos (default 900)",
                    },
                },
                "required": ["path", "slides"],
            },
        }
    ]

"""Slide corner decoration helpers for dashboard chat and MCP."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-slides-corner-decor"
        / "scripts"
        / "add_corner_images.py"
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


def _run_slides_corner_decor(
    *,
    path: str,
    prompt: str = "",
    analyze: bool = False,
    output_path: Optional[str] = None,
    provider: str = "imagen",
    corner: str = "auto",
    slides: str = "all",
    size_ratio: float = 0.18,
    margin_ratio: float = 0.025,
    max_overlap: float = 0.12,
    assets_dir: Optional[str] = None,
    timeout: float = 900.0,
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"slides-corner-decor script not found: {script}"}

    deck = Path(str(path).strip()).expanduser()
    if not deck.is_file():
        return {"ok": False, "error": f"deck not found: {deck}"}

    prompt = str(prompt or "").strip()
    if not analyze and not prompt:
        return {"ok": False, "error": "prompt is required unless analyze=true"}

    prov = str(provider or "imagen").strip().lower()
    if prov not in ("imagen", "nano", "grok"):
        prov = "imagen"

    corner_val = str(corner or "auto").strip().lower()
    if corner_val not in ("auto", "bottom-left", "bottom-right"):
        corner_val = "auto"

    cmd = ["uv", "run", str(script), str(deck.resolve())]
    if analyze:
        cmd.append("--analyze")
    else:
        cmd.extend(["--prompt", prompt])
        if output_path:
            cmd.extend(["--output", str(Path(output_path).expanduser())])
        cmd.extend(["--provider", prov, "--corner", corner_val, "--slides", str(slides or "all")])
        cmd.extend(
            [
                "--size-ratio",
                str(size_ratio),
                "--margin-ratio",
                str(margin_ratio),
                "--max-overlap",
                str(max_overlap),
            ]
        )
        if assets_dir:
            cmd.extend(["--assets-dir", str(Path(assets_dir).expanduser())])
    cmd.append("--json")

    work_dir = deck.parent if deck.parent.is_dir() else Path.cwd()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=max(60.0, float(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"slides_corner_decor timed out after {int(timeout)}s",
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
        "corner": corner_val,
    }
    if out_file:
        result["output_path"] = out_file
    return result


def dispatch_slides_corner_decor(args: dict[str, Any]) -> dict[str, Any]:
    """Analyze or decorate slide corners (shared by chat and MCP)."""
    path = str(args.get("path") or args.get("input") or "").strip()
    if not path:
        return {"ok": False, "error": "path is required"}

    analyze = bool(args.get("analyze"))
    try:
        timeout = float(args.get("timeout_seconds") or 900.0)
    except (TypeError, ValueError):
        timeout = 900.0

    try:
        size_ratio = float(args.get("size_ratio") or 0.18)
    except (TypeError, ValueError):
        size_ratio = 0.18
    try:
        margin_ratio = float(args.get("margin_ratio") or 0.025)
    except (TypeError, ValueError):
        margin_ratio = 0.025
    try:
        max_overlap = float(args.get("max_overlap") or 0.12)
    except (TypeError, ValueError):
        max_overlap = 0.12

    return _run_slides_corner_decor(
        path=path,
        prompt=str(args.get("prompt") or "").strip(),
        analyze=analyze,
        output_path=str(args.get("output_path") or args.get("output") or "").strip() or None,
        provider=str(args.get("provider") or "imagen").strip(),
        corner=str(args.get("corner") or "auto").strip(),
        slides=str(args.get("slides") or "all").strip(),
        size_ratio=size_ratio,
        margin_ratio=margin_ratio,
        max_overlap=max_overlap,
        assets_dir=str(args.get("assets_dir") or "").strip() or None,
        timeout=max(60.0, timeout),
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    """MCP tool descriptor for qclaw-skills server."""
    return [
        {
            "name": "slides_corner_decor",
            "description": (
                "Analisa ou decora cantos inferiores de slides (PPTX/HTML) com ilustrações "
                "geradas por Nano Banana ou Grok, apenas onde houver espaço livre. "
                "Skill: qclaw-slides-corner-decor. Equivalente a qclaw_slides_corner_decor "
                "no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Caminho absoluto do deck (.pptx, .ppt, .html)",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Se true, só reporta cantos livres (sem gerar imagens)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Prompt base da ilustração (obrigatório se analyze=false)",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Caminho de saída do deck decorado (opcional)",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["imagen", "nano", "grok"],
                        "description": "Gerador de imagem (imagen=Imagen 4 local, nano=Gemini, grok=xAI)",
                    },
                    "corner": {
                        "type": "string",
                        "enum": ["auto", "bottom-left", "bottom-right"],
                        "description": "Canto alvo (auto escolhe o mais livre em PPTX)",
                    },
                    "slides": {
                        "type": "string",
                        "description": "Slides a processar: all, 2, 1-5, 2,4,7",
                    },
                    "size_ratio": {
                        "type": "number",
                        "description": "Tamanho da ilustração vs menor dimensão do slide (default 0.18)",
                    },
                    "max_overlap": {
                        "type": "number",
                        "description": "Máx. fração ocupada do canto antes de ignorar (default 0.12)",
                    },
                    "assets_dir": {
                        "type": "string",
                        "description": "Pasta para PNGs geradas (opcional)",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout em segundos (default 900)",
                    },
                },
                "required": ["path"],
            },
        }
    ]

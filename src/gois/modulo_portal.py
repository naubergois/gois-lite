"""Build course module with slides, images, audio, PDF, video + portal HTML — chat and MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .chat_artifacts import stage_artifact
from .slides_batch_images import (
    _parse_json_stdout,
    _run_command_with_optional_block_progress,
)


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-modulo-portal"
        / "scripts"
        / "build_module_portal.py"
    )


def _default_output_paths(
    *,
    output_dir: Optional[str],
    zip_path: Optional[str],
    label: str,
) -> tuple[Path, Path]:
    if output_dir and zip_path:
        return Path(output_dir).expanduser(), Path(zip_path).expanduser()
    from .chat_artifacts import resolve_artifacts_dir

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "modulo-portal"))[:48]
    base = resolve_artifacts_dir() / safe
    return base, base / f"{safe}.zip"


def _run_modulo_portal(
    *,
    assets_dir: str,
    manifest: Optional[str] = None,
    module_id: str = "",
    module_title: str = "",
    auto_scan: bool = False,
    analyze: bool = False,
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    session_key: str = "",
    timeout: float = 600.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"modulo-portal script not found: {script}"}

    assets = Path(assets_dir).expanduser()
    if not assets.is_dir():
        return {"ok": False, "error": f"assets_dir not found: {assets}"}

    label = module_id or module_title or assets.name
    out_dir, zip_out = _default_output_paths(
        output_dir=output_dir,
        zip_path=zip_path,
        label=label,
    )

    cmd = [
        "uv",
        "run",
        str(script),
        "--assets-dir",
        str(assets),
        "--output-dir",
        str(out_dir),
        "--zip-path",
        str(zip_out),
        "--json",
    ]
    manifest_path = str(manifest or "").strip() or None
    if manifest_path:
        cmd.extend(["--manifest", str(Path(manifest_path).expanduser())])
    if module_id:
        cmd.extend(["--module-id", module_id])
    if module_title:
        cmd.extend(["--module-title", module_title])
    if auto_scan:
        cmd.append("--auto-scan")
    if analyze:
        cmd.append("--analyze")

    work_dir = Path(__file__).resolve().parents[2]
    import subprocess

    try:
        completed = _run_command_with_optional_block_progress(
            cmd,
            cwd=str(work_dir),
            timeout=max(60.0, float(timeout)),
            progress_job_id=progress_job_id if not analyze else None,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"modulo_portal timed out after {int(timeout)}s"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        return {"ok": False, "error": detail[:1200], "stdout": stdout[:2000], "stderr": stderr[:2000]}

    if analyze:
        return {"ok": True, "analyze": True, **payload}

    if not payload.get("ok"):
        return {
            "ok": False,
            "error": str(payload.get("error") or "module build failed"),
            **payload,
        }

    zip_file = Path(str(payload.get("zip_path") or zip_out)).expanduser()
    if not zip_file.is_file():
        return {"ok": False, "error": f"zip not found: {zip_file}"}

    try:
        att = stage_artifact(zip_file, label=zip_file.name, session_key=session_key)
    except OSError as exc:
        return {"ok": False, "error": f"could not stage zip: {exc}"}

    return {
        "ok": True,
        "module_id": payload.get("module_id"),
        "title": payload.get("title"),
        "section_count": int(payload.get("section_count") or 0),
        "index_html": payload.get("index_html"),
        "manifest_path": payload.get("manifest_path"),
        "output_dir": str(payload.get("output_dir") or out_dir),
        "zip_path": att["path"],
        "attachments": [att],
        "download_url": att.get("download_url"),
        "sections": (payload.get("sections") or [])[:20],
    }


def dispatch_modulo_portal(args: dict[str, Any]) -> dict[str, Any]:
    """Build portal module package (shared by chat and MCP)."""
    assets_dir = str(args.get("assets_dir") or args.get("assets") or "").strip()
    if not assets_dir:
        return {"ok": False, "error": "assets_dir is required"}

    try:
        timeout = float(args.get("timeout_seconds") or 600.0)
    except (TypeError, ValueError):
        timeout = 600.0

    return _run_modulo_portal(
        assets_dir=assets_dir,
        manifest=str(args.get("manifest") or args.get("manifest_file") or "").strip() or None,
        module_id=str(args.get("module_id") or "").strip(),
        module_title=str(args.get("module_title") or args.get("title") or "").strip(),
        auto_scan=bool(args.get("auto_scan")),
        analyze=bool(args.get("analyze")),
        output_dir=str(args.get("output_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
        session_key=str(args.get("session_key") or "").strip(),
        timeout=max(60.0, timeout),
        progress_job_id=str(args.get("progress_job_id") or "").strip() or None,
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "modulo_portal",
            "description": (
                "Monta módulo de curso com slides, imagens, áudio, PDF e vídeo "
                "num pacote ZIP + HTML estático para portal LMS. Aceita manifest JSON "
                "ou auto-scan de pasta. Skill: qclaw-modulo-portal. Equivalente a "
                "qclaw_modulo_portal no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "assets_dir": {
                        "type": "string",
                        "description": "Pasta com ficheiros de mídia",
                    },
                    "manifest": {
                        "type": "string",
                        "description": "JSON/JSONL com secções do módulo",
                    },
                    "module_id": {"type": "string"},
                    "module_title": {"type": "string"},
                    "auto_scan": {
                        "type": "boolean",
                        "description": "Detectar mídia por extensão na pasta",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Só listar secções, sem gerar ficheiros",
                    },
                },
                "required": ["assets_dir"],
            },
        }
    ]

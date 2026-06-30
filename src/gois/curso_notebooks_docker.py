"""Batch Jupyter notebooks + Docker Compose for course lessons — chat and MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .chat_artifacts import stage_artifact
from .slides_batch_images import (
    _parse_json_stdout,
    _run_command_with_optional_block_progress,
)

_STACKS = (
    "python",
    "python-data",
    "python-ml",
    "node",
    "fullstack",
    "sql",
    "devops",
)


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-curso-notebooks-docker"
        / "scripts"
        / "generate_course_notebooks.py"
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

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "course-labs"))[:48]
    base = resolve_artifacts_dir() / safe
    return base, base / f"{safe}.zip"


def _run_curso_notebooks_docker(
    *,
    lessons_file: Optional[str] = None,
    plano_path: Optional[str] = None,
    course_slug: str = "",
    course_title: str = "",
    stack: str = "python-data",
    level: str = "iniciante",
    analyze: bool = False,
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    max_lessons: int = 200,
    workers: int = 4,
    jupyter_port: int = 8888,
    token: str = "",
    hide_solutions: bool = False,
    resume: bool = False,
    session_key: str = "",
    timeout: float = 3600.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"curso-notebooks script not found: {script}"}

    lessons_file = str(lessons_file or "").strip() or None
    plano_path = str(plano_path or "").strip() or None
    if not lessons_file and not plano_path:
        return {"ok": False, "error": "lessons_file or plano is required"}
    if lessons_file and plano_path:
        return {"ok": False, "error": "use lessons_file OR plano, not both"}

    stk = str(stack or "python-data").strip().lower()
    if stk not in _STACKS:
        stk = "python-data"

    label = course_slug or Path(lessons_file or plano_path or "curso").stem
    out_dir, zip_out = _default_output_paths(
        output_dir=output_dir,
        zip_path=zip_path,
        label=label,
    )

    cmd = ["uv", "run", str(script)]
    if lessons_file:
        cmd.extend(["--lessons", str(Path(lessons_file).expanduser())])
    if plano_path:
        cmd.extend(["--plano", str(Path(plano_path).expanduser())])
    cmd.extend(
        [
            "--course-slug",
            str(course_slug or label),
            "--course-title",
            str(course_title or course_slug or label),
            "--stack",
            stk,
            "--level",
            str(level or "iniciante"),
            "--output-dir",
            str(out_dir),
            "--zip-path",
            str(zip_out),
            "--max-lessons",
            str(max(1, min(int(max_lessons), 500))),
            "--workers",
            str(max(1, min(int(workers), 32))),
            "--jupyter-port",
            str(max(1024, min(int(jupyter_port), 65535))),
            "--json",
        ]
    )
    if token:
        cmd.extend(["--token", token])
    if analyze:
        cmd.append("--analyze")
    if hide_solutions:
        cmd.append("--hide-solutions")
    if resume:
        cmd.append("--resume")

    work_dir = ROOT if (ROOT := Path(__file__).resolve().parents[2]).is_dir() else Path.cwd()
    try:
        completed = _run_command_with_optional_block_progress(
            cmd,
            cwd=str(work_dir),
            timeout=max(120.0, float(timeout)),
            progress_job_id=progress_job_id if not analyze else None,
        )
    except Exception as exc:  # noqa: BLE001 — subprocess.TimeoutExpired + OSError
        if type(exc).__name__ == "TimeoutExpired":
            return {
                "ok": False,
                "error": f"curso_notebooks_docker timed out after {int(timeout)}s",
            }
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        from .slides_batch_images import subprocess_failure_detail

        detail = subprocess_failure_detail(
            stderr=stderr,
            stdout=stdout,
            returncode=completed.returncode,
            payload=payload,
        )
        return {
            "ok": False,
            "error": detail,
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
        }

    if analyze:
        return {"ok": True, "analyze": True, **payload}

    if not payload.get("ok"):
        return {
            "ok": False,
            "error": str(payload.get("error") or "notebook generation failed"),
            **payload,
        }

    zip_file = Path(str(payload.get("zip_path") or zip_out)).expanduser()
    if not zip_file.is_file():
        return {"ok": False, "error": f"zip not found: {zip_file}"}

    try:
        att = stage_artifact(
            zip_file,
            label=zip_file.name,
            session_key=session_key,
        )
    except OSError as exc:
        return {"ok": False, "error": f"could not stage zip: {exc}"}

    manifest_path = Path(str(payload.get("manifest_path") or out_dir / "manifest.json"))
    preview_lessons: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            preview_lessons = (manifest.get("lessons") or [])[:5]
        except (OSError, json.JSONDecodeError):
            preview_lessons = []

    return {
        "ok": True,
        "course_slug": payload.get("course_slug"),
        "course_title": payload.get("course_title"),
        "stack": stk,
        "output_dir": str(payload.get("output_dir") or out_dir),
        "manifest_path": str(manifest_path),
        "compose_path": payload.get("compose_path"),
        "generated_count": int(payload.get("generated_count") or 0),
        "skipped_count": int(payload.get("skipped_count") or 0),
        "failed_count": int(payload.get("failed_count") or 0),
        "zip_path": att["path"],
        "attachments": [att],
        "download_url": att.get("download_url"),
        "preview_lessons": preview_lessons,
        "failed": (payload.get("failed") or [])[:20],
    }


def dispatch_curso_notebooks_docker(args: dict[str, Any]) -> dict[str, Any]:
    """Generate course notebooks + Docker Compose (shared by chat and MCP)."""
    lessons_file = str(args.get("lessons_file") or args.get("lessons") or "").strip() or None
    plano_path = str(args.get("plano") or args.get("plano_path") or "").strip() or None
    if not lessons_file and not plano_path:
        return {"ok": False, "error": "lessons_file or plano is required"}

    try:
        timeout = float(args.get("timeout_seconds") or 3600.0)
    except (TypeError, ValueError):
        timeout = 3600.0
    try:
        max_lessons = int(args.get("max_lessons") or 200)
    except (TypeError, ValueError):
        max_lessons = 200
    try:
        workers = int(args.get("workers") or 4)
    except (TypeError, ValueError):
        workers = 4
    try:
        jupyter_port = int(args.get("jupyter_port") or 8888)
    except (TypeError, ValueError):
        jupyter_port = 8888

    return _run_curso_notebooks_docker(
        lessons_file=lessons_file,
        plano_path=plano_path,
        course_slug=str(args.get("course_slug") or "").strip(),
        course_title=str(args.get("course_title") or "").strip(),
        stack=str(args.get("stack") or "python-data").strip(),
        level=str(args.get("level") or "iniciante").strip(),
        analyze=bool(args.get("analyze")),
        output_dir=str(args.get("output_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
        max_lessons=max_lessons,
        workers=workers,
        jupyter_port=jupyter_port,
        token=str(args.get("token") or "").strip(),
        hide_solutions=bool(args.get("hide_solutions")),
        resume=bool(args.get("resume")),
        session_key=str(args.get("session_key") or "").strip(),
        timeout=max(120.0, timeout),
        progress_job_id=str(args.get("progress_job_id") or "").strip() or None,
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "curso_notebooks_docker",
            "description": (
                "Gera notebooks Jupyter (.ipynb) com código e docker-compose.yml "
                "para lições de curso em batch (até 200). Aceita JSON/JSONL de lições "
                "ou PLANO.md do curso-builder. Skill: qclaw-curso-notebooks-docker. "
                "Equivalente a qclaw_curso_notebooks_docker no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "lessons_file": {
                        "type": "string",
                        "description": "JSON/JSONL com lições do curso",
                    },
                    "plano": {
                        "type": "string",
                        "description": "PLANO.md gerado pelo curso-builder",
                    },
                    "course_slug": {"type": "string"},
                    "course_title": {"type": "string"},
                    "stack": {
                        "type": "string",
                        "enum": list(_STACKS),
                    },
                    "level": {"type": "string"},
                    "analyze": {
                        "type": "boolean",
                        "description": "Só validar lições, sem gerar ficheiros",
                    },
                    "max_lessons": {
                        "type": "integer",
                        "description": "Limite de segurança (default 200)",
                    },
                    "workers": {
                        "type": "integer",
                        "description": "Paralelismo na geração (default 4)",
                    },
                    "hide_solutions": {
                        "type": "boolean",
                        "description": "Omitir células de solução nos notebooks",
                    },
                    "resume": {
                        "type": "boolean",
                        "description": "Ignorar notebooks já existentes",
                    },
                },
            },
        }
    ]

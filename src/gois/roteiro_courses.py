"""Course generation pipeline — chat and MCP helpers (roteiro viral API)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .slides_batch_images import (
    _parse_json_stdout,
    _run_command_with_optional_block_progress,
)

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _script_path(name: str) -> Path:
    return _REPO_ROOT / "skills" / "qclaw-roteiro-curso-completo" / "scripts" / name


def _run_course_script(
    script: str,
    cmd_args: list[str],
    *,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    path = _script_path(script)
    if not path.is_file():
        return {"ok": False, "error": f"script not found: {path}"}

    command = ["uv", "run", str(path), *cmd_args]
    work_dir = str(_REPO_ROOT)
    try:
        completed = _run_command_with_optional_block_progress(
            command,
            cwd=work_dir,
            timeout=max(120.0, float(timeout)),
            progress_job_id=progress_job_id,
        )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "TimeoutExpired":
            return {"ok": False, "error": f"course pipeline timed out after {int(timeout)}s"}
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        return {"ok": False, "error": detail[:1200], "stdout": stdout[:2000], "stderr": stderr[:2000]}

    if payload.get("ok") is False:
        return {"ok": False, "error": str(payload.get("error") or "course script failed"), **payload}

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    course_id = (
        str(result.get("course_id") or payload.get("course_id") or "").strip()
        if isinstance(result, dict)
        else ""
    )
    job_id = (
        str(result.get("job_id") or payload.get("job_id") or "").strip()
        if isinstance(result, dict)
        else str(payload.get("job_id") or "").strip()
    )
    out: dict[str, Any] = {"ok": True, **payload}
    if course_id:
        out["course_id"] = course_id
    if job_id:
        out["job_id"] = job_id
    return out


def run_course_generate(
    *,
    command: str = "full",
    course_id: str = "",
    topic: str = "",
    title: str = "",
    text: str = "",
    from_text: str = "",
    audience: str = "Público geral",
    modules_count: int = 5,
    difficulty: str = "Iniciante",
    lessons_per_module: int = 3,
    slides_per_lesson: int = 3,
    model: str = "gemini-3.5-flash",
    language: str = "Português (Brasil)",
    execution_mode: str = "economic",
    plan_first: bool = False,
    avatar_id: str = "",
    voice_id: str = "",
    visual_styles: str = "",
    wait: bool = True,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run course pipeline or create/plan scripts with optional chat progress."""
    if command == "plan":
        if not topic and not text and not from_text:
            return {"ok": False, "error": "topic, text or from_text is required for plan"}
        cmd = [
            "--topic", topic or text or "curso",
            "--audience", audience,
            "--modules", str(max(1, modules_count)),
            "--difficulty", difficulty,
            "--language", language,
            "--execution-mode", execution_mode,
            "--model", model,
        ]
        if title:
            cmd.extend(["--title", title])
        return _run_course_script("course_plan.py", cmd, timeout=min(timeout, 600.0), progress_job_id=progress_job_id)

    if command == "create":
        if not topic and not text and not from_text:
            return {"ok": False, "error": "topic, text or from_text is required for create"}
        cmd = [
            "--audience", audience,
            "--modules", str(max(1, modules_count)),
            "--difficulty", difficulty,
            "--language", language,
            "--execution-mode", execution_mode,
            "--model", model,
        ]
        if text:
            cmd.extend(["--text", text])
        elif from_text:
            cmd.extend(["--from-text", str(Path(from_text).expanduser())])
        else:
            cmd.extend(["--topic", topic])
        if title:
            cmd.extend(["--title", title])
        if plan_first:
            cmd.append("--plan-first")
        if wait:
            cmd.append("--wait")
        cmd.extend(["--timeout", str(max(300.0, float(timeout)))])
        return _run_course_script("course_create.py", cmd, timeout=timeout, progress_job_id=progress_job_id)

    if not course_id:
        return {"ok": False, "error": "course_id is required"}

    cmd = [command, "--course-id", course_id, "--model", model]
    
    if command in ("expand", "full"):
        cmd.extend(["--lessons-per-module", str(max(1, lessons_per_module))])
    
    if command in ("slides", "code-slides", "full"):
        cmd.extend(["--slides-per-lesson", str(max(1, slides_per_lesson))])
        
    if command in ("slides", "full") and visual_styles:
        cmd.extend(["--visual-styles", visual_styles])
        
    if command in ("gamma", "gamma-code", "full"):
        cmd.extend(["--language", language])
        
    if command in ("heygen", "full"):
        if avatar_id:
            cmd.extend(["--avatar-id", avatar_id])
        if voice_id:
            cmd.extend(["--voice-id", voice_id])

    if wait:
        cmd.append("--wait")
    
    cmd.extend(["--timeout", str(max(300.0, float(timeout)))])

    return _run_course_script("course_pipeline.py", cmd, timeout=timeout, progress_job_id=progress_job_id)


def _format_course_completion(result: dict[str, Any]) -> str:
    course_id = str(result.get("course_id") or "").strip()
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    steps = inner.get("steps") if isinstance(inner, dict) else None
    lines = ["Curso gerado com sucesso."]
    if course_id:
        lines.append(f"**course_id:** `{course_id}`")
    if isinstance(steps, list):
        lines.append(f"Etapas: **{len(steps)}**")
    return "\n".join(lines)


def spawn_course_generate_background(
    *,
    persistence: Any = None,
    session_key: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Start course pipeline in background thread."""
    from .chat_tool_background import spawn_chat_tool_background

    label = kwargs.get("course_id") or kwargs.get("topic") or kwargs.get("text") or "curso"
    return spawn_chat_tool_background(
        kind="course_pipeline",
        session_key=session_key,
        message_text=f"[course_pipeline] {label}",
        label=f"Gerando curso — {label}",
        run_fn=lambda job_id: run_course_generate(progress_job_id=job_id, **kwargs),
        persistence=persistence,
        format_success=_format_course_completion,
    )


def dispatch_roteiro_course_tool(
    args: dict[str, Any],
    *,
    progress_job_id: Optional[str] = None,
    persistence: Any = None,
) -> dict[str, Any]:
    """Chat/MCP dispatcher for qclaw_roteiro_course_generate."""
    command = str(args.get("command") or "create").strip().lower()
    valid = {
        "create",
        "plan",
        "full",
        "expand",
        "write",
        "slides",
        "code-slides",
        "code_slides",
        "gamma",
        "heygen",
    }
    if command not in valid:
        command = "create"

    try:
        modules_count = int(args.get("modules_count") or args.get("modules") or 5)
    except (TypeError, ValueError):
        modules_count = 5
    try:
        lessons = int(args.get("lessons_per_module") or 3)
    except (TypeError, ValueError):
        lessons = 3
    try:
        slides = int(args.get("slides_per_lesson") or 3)
    except (TypeError, ValueError):
        slides = 3

    if command == "code_slides":
        command = "code-slides"

    run_kwargs = dict(
        command=command,
        course_id=str(args.get("course_id") or "").strip(),
        topic=str(args.get("topic") or args.get("tema") or "").strip(),
        title=str(args.get("title") or "").strip(),
        text=str(args.get("text") or args.get("prompt") or "").strip(),
        from_text=str(args.get("from_text") or args.get("text_file") or "").strip(),
        audience=str(args.get("audience") or args.get("target_audience") or "Público geral").strip(),
        modules_count=modules_count,
        difficulty=str(args.get("difficulty") or "Iniciante").strip(),
        lessons_per_module=lessons,
        slides_per_lesson=slides,
        model=str(args.get("model") or args.get("model_name") or "gemini-3.5-flash").strip(),
        language=str(args.get("language") or "Português (Brasil)").strip(),
        execution_mode=str(args.get("execution_mode") or args.get("mode") or "economic").strip(),
        plan_first=bool(args.get("plan_first")),
        avatar_id=str(args.get("avatar_id") or "").strip(),
        voice_id=str(args.get("voice_id") or "").strip(),
        visual_styles=str(args.get("visual_styles") or "").strip(),
        wait=bool(args.get("wait", True)),
        timeout=float(args.get("timeout") or 7200.0),
    )

    session_key = str(args.get("session_key") or "").strip()
    if session_key or progress_job_id:
        return spawn_course_generate_background(
            persistence=persistence,
            session_key=session_key,
            **run_kwargs,
        )

    return run_course_generate(progress_job_id=progress_job_id, **run_kwargs)


def chat_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_roteiro_course_generate",
                "description": (
                    "Cria ou completa um curso via API Roteiro Viral SEM abrir a UI: "
                    "planejar módulos (plan), criar curso (create), expandir aulas, "
                    "escrever conteúdo, slides, Gamma, HeyGen (full/expand/write/…). "
                    "Equivalente headless ao wizard /course. "
                    "Skill: qclaw-roteiro-curso-completo. Requer API :8000 + course_worker."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": [
                                "create",
                                "plan",
                                "full",
                                "expand",
                                "write",
                                "slides",
                                "code-slides",
                                "gamma",
                                "heygen",
                            ],
                            "description": (
                                "create=novo curso; plan=só módulos (Gerar Estrutura); "
                                "full=pipeline completo; demais=etapa isolada"
                            ),
                        },
                        "topic": {"type": "string", "description": "Tema do curso (create/plan)"},
                        "title": {"type": "string", "description": "Título do curso"},
                        "text": {
                            "type": "string",
                            "description": "Texto livre — criação rápida como na UI",
                        },
                        "from_text": {
                            "type": "string",
                            "description": "Ficheiro .md/.txt com outline",
                        },
                        "course_id": {"type": "string", "description": "ID do curso existente"},
                        "modules_count": {"type": "integer", "description": "Nº módulos (default 5)"},
                        "lessons_per_module": {"type": "integer"},
                        "slides_per_lesson": {"type": "integer"},
                        "audience": {"type": "string"},
                        "difficulty": {"type": "string"},
                        "language": {"type": "string"},
                        "execution_mode": {
                            "type": "string",
                            "enum": ["mock", "economic", "full"],
                            "description": "mock=estrutura estática; economic/full=IA",
                        },
                        "plan_first": {
                            "type": "boolean",
                            "description": "Gerar módulos via /courses/plan antes de criar",
                        },
                        "visual_styles": {"type": "string"},
                        "avatar_id": {"type": "string"},
                        "voice_id": {"type": "string"},
                        "wait": {
                            "type": "boolean",
                            "description": "Aguardar conclusão (default true)",
                        },
                    },
                    "required": [],
                },
            },
        }
    ]


def mcp_tool_specs() -> list[dict[str, Any]]:
    spec = chat_tool_specs()[0]["function"]
    return [
        {
            "name": "roteiro_course_generate",
            "description": spec["description"],
            "inputSchema": spec["parameters"],
        }
    ]

"""Book generation pipeline — chat and MCP helpers (roteiro viral API)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .slides_batch_images import (
    _parse_json_stdout,
    _run_command_with_optional_block_progress,
    subprocess_failure_detail,
)

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_BASIC_COMMANDS = frozenset({"create", "pipeline", "write-chapters"})
_FULL_COMMANDS = frozenset(
    {
        "full",
        "structure",
        "subsections",
        "subsections-plan",
        "subsections-write",
        "cover",
        "images",
        "place-images",
        "metadata",
    }
)
_ALL_COMMANDS = _BASIC_COMMANDS | _FULL_COMMANDS


def _script_path(skill: str, name: str) -> Path:
    return _REPO_ROOT / "skills" / skill / "scripts" / name


def _run_script(
    path: Path,
    cmd_args: list[str],
    *,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
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
            return {"ok": False, "error": f"book pipeline timed out after {int(timeout)}s"}
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        detail = subprocess_failure_detail(
            stderr=stderr,
            stdout=stdout,
            returncode=completed.returncode,
            payload=payload,
        )
        return {"ok": False, "error": detail, "stdout": stdout[:2000], "stderr": stderr[:2000]}

    if payload.get("ok") is False:
        return {"ok": False, "error": str(payload.get("error") or "book script failed"), **payload}

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    book_id = (
        str(result.get("book_id") or payload.get("book_id") or "").strip()
        if isinstance(result, dict)
        else ""
    )
    job_id = (
        str(result.get("job_id") or payload.get("job_id") or "").strip()
        if isinstance(result, dict)
        else str(payload.get("job_id") or "").strip()
    )
    out: dict[str, Any] = {"ok": True, **payload}
    if book_id:
        out["book_id"] = book_id
    if job_id:
        out["job_id"] = job_id
    return out


def _run_book_script(
    script: str,
    cmd_args: list[str],
    *,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    return _run_script(
        _script_path("qclaw-roteiro-livros", script),
        cmd_args,
        timeout=timeout,
        progress_job_id=progress_job_id,
    )


def _run_full_pipeline_script(
    cmd_args: list[str],
    *,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    return _run_script(
        _script_path("qclaw-roteiro-livro-completo", "book_pipeline.py"),
        cmd_args,
        timeout=timeout,
        progress_job_id=progress_job_id,
    )


def run_book_generate(
    *,
    command: str = "pipeline",
    topic: str = "",
    from_text: Optional[str] = None,
    book_id: str = "",
    chapters: int = 8,
    sections_per_chapter: int = 4,
    audience: str = "Público geral",
    tone: str = "Didático e acessível",
    language: str = "Português (Brasil)",
    model: str = "gemini-3.5-flash",
    with_metadata: bool = False,
    with_subsections: bool = True,
    with_cover: bool = True,
    with_cover_back: bool = False,
    with_images: bool = True,
    with_place_images: bool = True,
    skip_structure: bool = False,
    cover_styles: str = "Cinematic",
    author: str = "",
    wait: bool = True,
    workers: int = 4,
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run book pipeline inline via chat (no background worker)."""
    command = (command or "pipeline").strip().lower()

    _SYNC_COMMAND_MAP = {
        "cover": "cover",
        "images": "images",
        "place-images": "place_images",
        "metadata": "metadata",
        "subsections": "subsections",
        "subsections-plan": "subsections",
        "subsections-write": "subsections",
    }

    if command in _SYNC_COMMAND_MAP and book_id:
        from .roteiro_book_sync import run_assemble_step_sync

        step = _SYNC_COMMAND_MAP[command]
        return run_assemble_step_sync(
            book_id=book_id,
            step=step,
            num_chapters=max(1, chapters),
            sections_per_chapter=max(1, sections_per_chapter),
            cover_styles=cover_styles,
            author=author,
            max_workers=max(1, min(int(workers), 8)) if workers else None,
        )

    if command == "structure" and book_id:
        from .roteiro_book_sync import assemble_book_sync

        steps = ["chapters", "plan_sections", "write_sections"]
        if with_metadata:
            steps.append("metadata")
        return assemble_book_sync(
            book_id=book_id,
            steps=steps,
            num_chapters=max(1, chapters),
            sections_per_chapter=max(1, sections_per_chapter),
            with_metadata=False,
            with_subsections=False,
            max_workers=max(1, min(int(workers), 8)) if workers else None,
        )

    if command == "full":
        if not book_id:
            if not topic and not from_text:
                return {"ok": False, "error": "topic, from_text or book_id is required for full"}
            create_result = run_book_generate(
                command="create",
                topic=topic,
                from_text=from_text,
                chapters=chapters,
                audience=audience,
                tone=tone,
                language=language,
                model=model,
                wait=wait,
                timeout=timeout,
                progress_job_id=progress_job_id,
            )
            if not create_result.get("ok"):
                return create_result
            book_id = str(create_result.get("book_id") or "").strip()
            if not book_id:
                return {"ok": False, "error": "create did not return book_id"}
        from .roteiro_book_sync import assemble_book_sync

        return assemble_book_sync(
            book_id=book_id,
            num_chapters=max(1, chapters),
            sections_per_chapter=max(1, sections_per_chapter),
            with_metadata=with_metadata,
            with_subsections=with_subsections,
            with_cover=with_cover,
            with_images=with_images,
            with_place_images=with_place_images,
            skip_structure=skip_structure,
            max_workers=max(1, min(int(workers), 8)) if workers else None,
        )

    if command in _FULL_COMMANDS:
        if not book_id:
            return {"ok": False, "error": "book_id is required"}
        cmd = [command, "--book-id", book_id]
        if command in ("full", "structure"):
            cmd.extend(
                [
                    "--num-chapters",
                    str(max(1, chapters)),
                    "--sections-per-chapter",
                    str(max(1, sections_per_chapter)),
                    "--workers",
                    str(max(1, min(int(workers), 8))),
                ]
            )
        if command == "full":
            if with_metadata:
                cmd.append("--with-metadata")
            if not with_subsections:
                cmd.append("--no-subsections")
            if not with_cover:
                cmd.append("--no-cover")
            if with_cover_back:
                cmd.append("--with-cover-back")
            if not with_images:
                cmd.append("--no-images")
            if not with_place_images:
                cmd.append("--no-place-images")
            if skip_structure:
                cmd.append("--skip-structure")
            if cover_styles:
                cmd.extend(["--cover-styles", cover_styles])
            if author:
                cmd.extend(["--author", author])
        elif command == "structure" and with_metadata:
            cmd.append("--with-metadata")
        elif command == "cover" and cover_styles:
            cmd.extend(["--cover-styles", cover_styles, "--from-plan"])
            if author:
                cmd.extend(["--author", author])
        if wait:
            cmd.append("--wait")
        if progress_job_id:
            cmd.append("--progress")
        cmd.extend(["--timeout", str(max(300.0, float(timeout)))])
        return _run_full_pipeline_script(cmd, timeout=timeout, progress_job_id=progress_job_id)

    if command == "create":
        cmd = ["create"]
        if from_text:
            cmd.extend(["--from-text", str(Path(from_text).expanduser())])
        elif topic:
            cmd.extend(["--topic", topic, "--chapters", str(max(1, chapters))])
        else:
            return {"ok": False, "error": "topic or from_text is required for create"}
        cmd.extend(
            [
                "--audience",
                audience,
                "--tone",
                tone,
                "--language",
                language,
                "--model",
                model,
            ]
        )
    elif command in ("pipeline", "write-chapters"):
        if not book_id:
            return {"ok": False, "error": "book_id is required"}
        cmd = [command, "--book-id", book_id]
        if command == "pipeline":
            cmd.extend(
                [
                    "--num-chapters",
                    str(max(1, chapters)),
                    "--sections-per-chapter",
                    str(max(1, sections_per_chapter)),
                    "--model",
                    model,
                ]
            )
            if with_metadata:
                cmd.append("--with-metadata")
    else:
        return {"ok": False, "error": f"unknown command: {command}"}

    if wait:
        cmd.append("--wait")
    if workers > 1 and command != "create":
        cmd.extend(["--workers", str(max(1, min(int(workers), 8)))])
    if progress_job_id:
        cmd.append("--progress")
    cmd.extend(["--timeout", str(max(300.0, float(timeout)))])

    return _run_book_script("book_generate.py", cmd, timeout=timeout, progress_job_id=progress_job_id)


def _format_book_completion(result: dict[str, Any]) -> str:
    book_id = str(result.get("book_id") or "").strip()
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    steps = inner.get("steps") if isinstance(inner, dict) else None
    lines = ["Livro gerado com sucesso."]
    if book_id:
        lines.append(f"**book_id:** `{book_id}`")
    if isinstance(steps, list):
        lines.append(f"Etapas: **{len(steps)}**")
    return "\n".join(lines)


def spawn_book_generate_background(
    *,
    persistence: Any = None,
    session_key: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Start book pipeline in background thread."""
    from .chat_tool_background import spawn_chat_tool_background

    label = kwargs.get("topic") or kwargs.get("book_id") or "livro"
    return spawn_chat_tool_background(
        kind="book_pipeline",
        session_key=session_key,
        message_text=f"[book_pipeline] {label}",
        label=f"Gerando livro — {label}",
        run_fn=lambda job_id: run_book_generate(progress_job_id=job_id, **kwargs),
        persistence=persistence,
        format_success=_format_book_completion,
    )


def dispatch_roteiro_book_tool(
    args: dict[str, Any],
    *,
    progress_job_id: Optional[str] = None,
    persistence: Any = None,
) -> dict[str, Any]:
    """Chat tool dispatcher for qclaw_roteiro_book_generate."""
    command = str(args.get("command") or "pipeline").strip().lower()
    if command not in _ALL_COMMANDS:
        command = "pipeline"

    try:
        chapters = int(args.get("chapters") or args.get("num_chapters") or 8)
    except (TypeError, ValueError):
        chapters = 8
    try:
        sections = int(args.get("sections_per_chapter") or 4)
    except (TypeError, ValueError):
        sections = 4
    try:
        workers = int(args.get("workers") or 4)
    except (TypeError, ValueError):
        workers = 4

    run_kwargs = dict(
        command=command,
        topic=str(args.get("topic") or args.get("tema") or "").strip(),
        from_text=str(args.get("from_text") or args.get("text_file") or "").strip() or None,
        book_id=str(args.get("book_id") or "").strip(),
        chapters=chapters,
        sections_per_chapter=sections,
        audience=str(args.get("audience") or "Público geral").strip(),
        tone=str(args.get("tone") or "Didático e acessível").strip(),
        language=str(args.get("language") or "Português (Brasil)").strip(),
        model=str(args.get("model") or "gemini-3.5-flash").strip(),
        with_metadata=bool(args.get("with_metadata", command == "full")),
        with_subsections=bool(args.get("with_subsections", True)),
        with_cover=bool(args.get("with_cover", True)),
        with_cover_back=bool(args.get("with_cover_back")),
        with_images=bool(args.get("with_images", True)),
        with_place_images=bool(args.get("with_place_images", True)),
        skip_structure=bool(args.get("skip_structure")),
        cover_styles=str(args.get("cover_styles") or "Cinematic").strip(),
        author=str(args.get("author") or "").strip(),
        wait=bool(args.get("wait", True)),
        workers=workers,
        timeout=float(args.get("timeout") or 7200.0),
    )

    session_key = str(args.get("session_key") or "").strip()
    if session_key or progress_job_id:
        return spawn_book_generate_background(
            persistence=persistence,
            session_key=session_key,
            **run_kwargs,
        )

    return run_book_generate(progress_job_id=progress_job_id, **run_kwargs)


def chat_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_roteiro_book_generate",
                "description": (
                    "Cria ou completa um livro inline via chat (sem worker): "
                    "capítulos, seções, subseções, capa, imagens e EPUB. "
                    "Preferir `qclaw_roteiro_book_sync` para pipeline síncrono explícito. "
                    "Skill: qclaw-roteiro-livro-sync."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": sorted(_ALL_COMMANDS),
                            "description": (
                                "full=pipeline completo (subseções+capa+imagens); "
                                "create=novo livro; pipeline=capítulos+seções+textos; "
                                "structure/subsections/cover/images=etapas isoladas"
                            ),
                        },
                        "topic": {"type": "string", "description": "Tema do livro (create/full sem book_id)"},
                        "from_text": {
                            "type": "string",
                            "description": "Ficheiro .md com texto longo (create/full)",
                        },
                        "book_id": {"type": "string", "description": "ID do livro existente"},
                        "chapters": {"type": "integer", "description": "Nº capítulos (default 8)"},
                        "sections_per_chapter": {
                            "type": "integer",
                            "description": "Secções por capítulo (default 4)",
                        },
                        "audience": {"type": "string"},
                        "tone": {"type": "string"},
                        "language": {"type": "string"},
                        "author": {"type": "string", "description": "Autor (capa/metadados)"},
                        "with_metadata": {"type": "boolean"},
                        "with_subsections": {
                            "type": "boolean",
                            "description": "Incluir subseções no full (default true)",
                        },
                        "with_cover": {
                            "type": "boolean",
                            "description": "Gerar capa frontal no full (default true)",
                        },
                        "with_cover_back": {"type": "boolean", "description": "Capa de verso"},
                        "with_images": {
                            "type": "boolean",
                            "description": "Imagens ilustrativas por secção (default true)",
                        },
                        "with_place_images": {
                            "type": "boolean",
                            "description": "Colocar imagens no EPUB (default true)",
                        },
                        "skip_structure": {
                            "type": "boolean",
                            "description": "full sem regenerar capítulos/seções",
                        },
                        "cover_styles": {
                            "type": "string",
                            "description": "Estilos de capa separados por vírgula",
                        },
                        "workers": {
                            "type": "integer",
                            "description": "Threads paralelas por capítulo (default 4, max 8)",
                        },
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
            "name": "roteiro_book_generate",
            "description": spec["description"],
            "inputSchema": spec["parameters"],
        }
    ]

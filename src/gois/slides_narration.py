"""Batch slide narration generation + ZIP download — chat and MCP helpers."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .chat_artifacts import stage_artifact


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-slides-narration"
        / "scripts"
        / "generate_slides_narration.py"
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
    stderr_chunks: list[str] = []

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)
            _stream_block_progress_line(line, progress_job_id)

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    try:
        stdout, _ = proc.communicate(timeout=max(60.0, float(timeout)))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    reader.join(timeout=2.0)
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr="".join(stderr_chunks),
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

    stamp = label or "slides-narration"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stamp)[:48]
    base = resolve_artifacts_dir() / safe
    return base / "narration", base / f"{safe}-narration.zip"


def _run_slides_narration(
    *,
    slides_file: Optional[str] = None,
    deck_path: Optional[str] = None,
    slides: str = "all",
    analyze: bool = False,
    lesson_title: str = "",
    lesson_context: str = "",
    tone: str = "didático",
    language: str = "pt-BR",
    target_seconds: float = 25.0,
    wpm: float = 140.0,
    model: str = "gemini-3.5-flash",
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    max_slides: int = 200,
    workers: int = 4,
    resume: bool = False,
    delay: float = 0.5,
    preview_count: int = 3,
    session_key: str = "",
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"slides-narration script not found: {script}"}

    slides_file = str(slides_file or "").strip() or None
    deck_path = str(deck_path or "").strip() or None
    if not slides_file and not deck_path:
        return {"ok": False, "error": "slides_file or deck path is required"}
    if slides_file and deck_path:
        return {"ok": False, "error": "use slides_file OR deck, not both"}

    label = Path(deck_path or slides_file or "narration").stem
    out_dir, zip_out = _default_output_paths(
        output_dir=output_dir,
        zip_path=zip_path,
        label=label,
    )

    cmd = ["uv", "run", str(script)]
    if slides_file:
        cmd.extend(["--slides-file", str(Path(slides_file).expanduser())])
    if deck_path:
        cmd.extend(["--deck", str(Path(deck_path).expanduser())])
        cmd.extend(["--slides", str(slides or "all")])
    if lesson_title:
        cmd.extend(["--lesson-title", lesson_title])
    if lesson_context:
        cmd.extend(["--lesson-context", lesson_context])
    cmd.extend(
        [
            "--tone",
            str(tone or "didático").strip(),
            "--language",
            str(language or "pt-BR").strip(),
            "--target-seconds",
            str(max(5.0, float(target_seconds or 25.0))),
            "--wpm",
            str(max(80.0, float(wpm or 140.0))),
            "--model",
            str(model or "gemini-3.5-flash").strip(),
            "--output-dir",
            str(out_dir),
            "--zip-path",
            str(zip_out),
            "--max-slides",
            str(max(1, min(int(max_slides), 500))),
            "--workers",
            str(max(1, min(int(workers), 16))),
            "--delay",
            str(max(0.0, float(delay))),
            "--json",
        ]
    )
    if analyze:
        cmd.append("--analyze")
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
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"slides_narration timed out after {int(timeout)}s",
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
        }

    if analyze:
        return {"ok": True, "analyze": True, **payload}

    if not payload.get("ok"):
        return {
            "ok": False,
            "error": str(payload.get("error") or "narration generation failed"),
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

    generated = payload.get("generated") or []
    preview_n = max(0, int(preview_count))
    samples = [
        {
            "slide": r.get("slide"),
            "title": r.get("title"),
            "narration": str(r.get("narration") or "")[:500],
            "words": r.get("words"),
            "duration_sec": r.get("duration_sec"),
        }
        for r in generated[:preview_n]
    ]

    return {
        "ok": True,
        "zip_path": att["path"],
        "output_dir": str(payload.get("output_dir") or out_dir),
        "jsonl_path": payload.get("jsonl_path"),
        "markdown_path": payload.get("markdown_path"),
        "heygen_path": payload.get("heygen_path"),
        "generated_count": int(payload.get("generated_count") or len(generated)),
        "failed_count": int(payload.get("failed_count") or 0),
        "total_words": int(payload.get("total_words") or 0),
        "estimated_minutes": payload.get("estimated_minutes"),
        "model": str(payload.get("model") or model),
        "tone": str(payload.get("tone") or tone),
        "attachments": [att],
        "download_url": att.get("download_url"),
        "samples": samples,
        "generated": generated[:20],
        "failed": (payload.get("failed") or [])[:20],
    }


def dispatch_slides_narration(args: dict[str, Any]) -> dict[str, Any]:
    """Generate batch slide narration and ZIP (shared by chat and MCP)."""
    slides_file = str(args.get("slides_file") or "").strip() or None
    deck_path = str(args.get("path") or args.get("deck") or "").strip() or None
    if not slides_file and not deck_path:
        return {"ok": False, "error": "slides_file or path (deck) is required"}
    if slides_file and deck_path:
        return {"ok": False, "error": "use slides_file OR path (deck), not both"}

    try:
        timeout = float(args.get("timeout_seconds") or 7200.0)
    except (TypeError, ValueError):
        timeout = 7200.0
    try:
        max_slides = int(args.get("max_slides") or 200)
    except (TypeError, ValueError):
        max_slides = 200
    try:
        preview_count = int(args.get("preview_count") or 3)
    except (TypeError, ValueError):
        preview_count = 3
    try:
        workers = int(args.get("workers") or 4)
    except (TypeError, ValueError):
        workers = 4
    try:
        delay = float(args.get("delay_seconds") or args.get("delay") or 0.5)
    except (TypeError, ValueError):
        delay = 0.5
    try:
        target_seconds = float(args.get("target_seconds") or 25.0)
    except (TypeError, ValueError):
        target_seconds = 25.0
    try:
        wpm = float(args.get("wpm") or 140.0)
    except (TypeError, ValueError):
        wpm = 140.0

    return _run_slides_narration(
        slides_file=slides_file,
        deck_path=deck_path,
        slides=str(
            args.get("slides_filter")
            or args.get("slide_filter")
            or (args.get("slides") if deck_path else "all")
            or "all"
        ).strip(),
        analyze=bool(args.get("analyze")),
        lesson_title=str(args.get("lesson_title") or "").strip(),
        lesson_context=str(args.get("lesson_context") or "").strip(),
        tone=str(args.get("tone") or "didático").strip(),
        language=str(args.get("language") or "pt-BR").strip(),
        target_seconds=target_seconds,
        wpm=wpm,
        model=str(args.get("model") or "gemini-3.5-flash").strip(),
        output_dir=str(args.get("output_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
        max_slides=max_slides,
        workers=workers,
        resume=bool(args.get("resume")),
        delay=delay,
        preview_count=preview_count,
        session_key=str(args.get("session_key") or "").strip(),
        timeout=max(120.0, timeout),
        progress_job_id=str(args.get("progress_job_id") or "").strip() or None,
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "slides_narration",
            "description": (
                "Gera locução/narração falada para cada slide de uma lição (até 200). "
                "Aceita deck PPTX/HTML ou JSON/JSONL de slides. Saída: JSONL, Markdown, "
                "HeyGen scripts e ZIP. Skill: qclaw-slides-narration. Equivalente a "
                "qclaw_slides_narration no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Deck PPTX/HTML para extrair texto por slide",
                    },
                    "slides_file": {
                        "type": "string",
                        "description": "JSON/JSONL com slides {slide,title,content}",
                    },
                    "slides_filter": {
                        "type": "string",
                        "description": "Filtro de slides: all, 3, 2-5 (só com path)",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Só listar slides, sem gerar narração",
                    },
                    "lesson_title": {"type": "string"},
                    "lesson_context": {"type": "string"},
                    "tone": {
                        "type": "string",
                        "enum": ["didático", "dinâmico", "formal"],
                    },
                    "target_seconds": {
                        "type": "number",
                        "description": "Meta de duração por slide (default 25)",
                    },
                    "max_slides": {
                        "type": "integer",
                        "description": "Limite de segurança (default 200)",
                    },
                    "workers": {
                        "type": "integer",
                        "description": "Paralelismo LLM (default 4)",
                    },
                    "preview_count": {
                        "type": "integer",
                        "description": "Amostras de fala no resultado (default 3)",
                    },
                },
            },
        }
    ]

"""Batch slide image generation + ZIP download — chat and MCP helpers."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .chat_artifacts import stage_artifact
from .image_generation_fallback import attach_image_data_url, parse_allow_fallback_flag
from .slides_batch_artifacts import (
    enrich_batch_result_with_disk,
    format_artifacts_summary,
    list_slide_pngs,
)
log = logging.getLogger(__name__)


def _script_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "qclaw-slides-batch-images"
        / "scripts"
        / "generate_slides_batch.py"
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


_QCLAW_BLOCK_LINE_RE = re.compile(r"^\s*QCLAW_BLOCK\s+", re.IGNORECASE)


def _strip_progress_stderr(text: str) -> str:
    """Remove QCLAW_BLOCK progress lines from captured stderr."""
    lines = [
        line.rstrip()
        for line in (text or "").splitlines()
        if line.strip() and not _QCLAW_BLOCK_LINE_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


def subprocess_failure_detail(
    *,
    stderr: str = "",
    stdout: str = "",
    returncode: int = 1,
    payload: dict[str, Any] | None = None,
) -> str:
    """Best-effort error text when a progress-emitting subprocess fails."""
    if isinstance(payload, dict):
        err = str(payload.get("error") or "").strip()
        if err:
            return err[:1200]

    cleaned_err = _strip_progress_stderr(stderr)
    if cleaned_err:
        return cleaned_err[:1200]

    cleaned_out = _strip_progress_stderr(stdout)
    if cleaned_out:
        return cleaned_out[:1200]

    return f"exit code {returncode}"


def _batch_worker_should_abort(
    progress_job_id: Optional[str],
    persistence: Any = None,
) -> bool:
    """True when a batch subprocess should stop (cancelled or conversation gone)."""
    from .chat_jobs import (
        _conversation_history_key,
        cancel_job_if_session_missing,
        get_job,
        should_abort_background_job,
    )

    job_id = (progress_job_id or "").strip()
    if not job_id:
        return False
    if should_abort_background_job(job_id):
        return True
    if persistence is None:
        return False
    job = get_job(job_id)
    if job is None:
        return False
    client_key = (job.display_key or "").strip()
    send_key = (job.session_key or "").strip()
    persist_key = _conversation_history_key(client_key, send_key)
    return cancel_job_if_session_missing(
        job_id,
        persistence,
        persist_key,
        reason="Conversa removida — lote interrompido",
    )


def _stream_block_progress_line(
    line: str,
    progress_job_id: str,
    *,
    streaming_images: Optional[list[dict[str, Any]]] = None,
) -> None:
    from .chat_jobs import (
        get_job,
        parse_block_progress,
        record_image_batch_outcome,
        set_block_progress,
        set_block_progress_detail,
        update_job_media,
    )

    text = (line or "").strip()
    if not text:
        return
    if text.startswith("QCLAW_ERROR "):
        if not progress_job_id:
            return
        try:
            payload = json.loads(text[len("QCLAW_ERROR ") :].strip())
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        slide_no = payload.get("slide")
        title = str(payload.get("title") or "").strip()
        err = str(payload.get("error") or "").strip()
        head = f"⚠️ Slide {slide_no} falhou" if slide_no is not None else "⚠️ Slide falhou"
        if title:
            head += f" ({title})"
        detail = f"{head}: {err}" if err else head
        set_block_progress_detail(progress_job_id, detail)
        log.warning("slides batch %s: %s", progress_job_id, detail)
        return
    if text.startswith("QCLAW_IMAGE "):
        if not progress_job_id or streaming_images is None:
            return
        try:
            payload = json.loads(text[len("QCLAW_IMAGE ") :].strip())
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        img_path = str(payload.get("image") or "").strip()
        if not img_path:
            return
        path = Path(img_path)
        if not path.is_file():
            return
        slide_no = payload.get("slide")
        title = str(payload.get("title") or "").strip()
        caption = f"Slide {slide_no}" if slide_no is not None else "Slide"
        if title:
            caption = f"{caption} — {title}"
        entry: dict[str, Any] = {
            "caption": caption,
            "image_path": str(path.resolve()),
        }
        if slide_no is not None:
            entry["slide"] = int(slide_no)
        # Batch: persist paths only — inline base64 in partial_media blows MongoDB BSON.
        replaced = False
        if slide_no is not None:
            for idx, existing in enumerate(streaming_images):
                if existing.get("slide") == slide_no:
                    streaming_images[idx] = entry
                    replaced = True
                    break
        if not replaced:
            streaming_images.append(entry)
        update_job_media(
            progress_job_id,
            {
                "images": list(streaming_images),
                "videos": [],
                "output_dir": str(path.parent.resolve()),
            },
        )
        job = get_job(progress_job_id)
        if job is not None:
            session_key = (job.display_key or job.session_key or "").strip()
            if session_key:
                record_image_batch_outcome(session_key, had_success=True)
        if slide_no is not None:
            from .chat_jobs import get_job, set_block_progress

            job = get_job(progress_job_id)
            total = int(job.block_max) if job is not None and job.block_max > 0 else 0
            if total > 0:
                set_block_progress(
                    progress_job_id,
                    int(slide_no),
                    total,
                    message=caption,
                )
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
    persistence: Any = None,
) -> subprocess.CompletedProcess[str]:
    streaming_images: list[dict[str, Any]] = []

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
            if _batch_worker_should_abort(progress_job_id, persistence):
                proc.kill()
                break
            stderr_lines.append(raw)
            _stream_block_progress_line(
                raw,
                progress_job_id,
                streaming_images=streaming_images,
            )

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


def _default_output_paths(
    *,
    output_dir: Optional[str],
    zip_path: Optional[str],
    label: str,
) -> tuple[Path, Path]:
    if output_dir and zip_path:
        return Path(output_dir).expanduser(), Path(zip_path).expanduser()
    from .chat_artifacts import resolve_artifacts_dir

    stamp = label or "slides-batch"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stamp)[:48]
    base = resolve_artifacts_dir() / safe
    return base / "images", base / f"{safe}.zip"


def _attach_preview_images(
    result: dict[str, Any],
    generated: list[dict[str, Any]],
    *,
    preview_count: int,
) -> None:
    previews: list[dict[str, Any]] = []
    for item in generated[: max(0, preview_count)]:
        img_path = str(item.get("image") or "").strip()
        if not img_path:
            continue
        path = Path(img_path)
        if not path.is_file():
            continue
        entry: dict[str, Any] = {
            "caption": f"Slide {item.get('slide', '?')}",
            "image_path": str(path.resolve()),
        }
        from gois.openclaw_chat_media_tools import _image_download_url_from_path

        dl = _image_download_url_from_path(entry["image_path"])
        if dl:
            entry["download_url"] = dl
        previews.append(entry)
    if previews:
        result["images"] = previews


def _run_slides_batch_images(
    *,
    prompts_file: Optional[str] = None,
    deck_path: Optional[str] = None,
    slides: str = "all",
    analyze: bool = False,
    prompt: str = "",
    style: str = "",
    provider: str = "nano",
    resolution: str = "2K",
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    max_slides: int = 200,
    resume: bool = False,
    delay: float = 2.0,
    preview_count: int = 3,
    workers: int = 1,
    session_key: str = "",
    timeout: float = 7200.0,
    progress_job_id: Optional[str] = None,
    allow_fallback: bool = True,
    model: str = "",
    card_id: Optional[str] = None,
    task_id: Optional[str] = None,
    workdir: Optional[str] = None,
    auto_fix_prompts: bool = True,
    persistence: Any = None,
    _retry_after_fix: bool = False,
) -> dict[str, Any]:
    script = _script_path()
    if not script.is_file():
        return {"ok": False, "error": f"slides-batch script not found: {script}"}

    prompts_file = str(prompts_file or "").strip() or None
    deck_path = str(deck_path or "").strip() or None
    if not prompts_file and not deck_path:
        return {"ok": False, "error": "prompts_file or deck path is required"}
    if prompts_file and deck_path:
        return {"ok": False, "error": "use prompts_file OR deck, not both"}

    auto_fix_meta: dict[str, Any] | None = None
    if prompts_file and auto_fix_prompts and not analyze and not _retry_after_fix:
        from .slides_batch_prompts_fix import auto_repair_prompts_file

        prompts_file, auto_fix_meta = auto_repair_prompts_file(
            prompts_file,
            card_id=card_id,
            task_id=task_id,
            workdir=workdir,
        )
        if auto_fix_meta and auto_fix_meta.get("auto_fixed") and progress_job_id:
            from .chat_jobs import append_progress

            src = auto_fix_meta.get("prompts_source", "")
            append_progress(
                progress_job_id,
                f"Prompts corrigidos automaticamente (empty_prompt) — fonte: {Path(str(src)).name if src else '?'}",
            )

    prov = str(provider or "nano").strip().lower()
    if prov not in ("nano", "grok", "imagen"):
        prov = "nano"

    res = str(resolution or "2K").strip().upper()
    if res not in ("1K", "2K", "4K"):
        res = "2K"

    label = Path(deck_path or prompts_file or "batch").stem
    out_dir, zip_out = _default_output_paths(
        output_dir=output_dir,
        zip_path=zip_path,
        label=label,
    )
    out_dir = out_dir.expanduser().resolve()
    zip_out = zip_out.expanduser().resolve()

    cmd = ["uv", "run", str(script)]
    if prompts_file:
        cmd.extend(["--prompts-file", str(Path(prompts_file).expanduser())])
    if deck_path:
        cmd.extend(["--deck", str(Path(deck_path).expanduser())])
        cmd.extend(["--slides", str(slides or "all")])
        if prompt:
            cmd.extend(["--prompt", prompt])
        if style:
            cmd.extend(["--style", style])
    cmd.extend(
        [
            "--provider",
            prov,
            "--resolution",
            res,
            "--output-dir",
            str(out_dir),
            "--zip-path",
            str(zip_out),
            "--max-slides",
            str(max(1, min(int(max_slides), 500))),
            "--delay",
            str(max(0.0, float(delay))),
            "--workers",
            str(max(1, min(int(workers), 8))),
            "--json",
        ]
    )
    if analyze:
        cmd.append("--analyze")
    # Auto-enable resume when the output directory already has images,
    # so re-running only generates the missing slides (e.g. 4 of 100).
    if not resume and not analyze and out_dir.is_dir():
        existing = list_slide_pngs(out_dir)
        if existing:
            resume = True
            log.info(
                "slides batch: auto-enabling --resume (%d images already in %s)",
                len(existing),
                out_dir,
            )
    if resume:
        cmd.append("--resume")
    if not allow_fallback:
        cmd.append("--no-fallback")
    if str(model or "").strip():
        cmd.extend(["--model", str(model).strip()])

    work_dir = ROOT if (ROOT := Path(__file__).resolve().parents[2]).is_dir() else Path.cwd()
    try:
        completed = _run_command_with_optional_block_progress(
            cmd,
            cwd=str(work_dir),
            timeout=max(120.0, float(timeout)),
            progress_job_id=progress_job_id if not analyze else None,
            persistence=persistence,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"slides_batch_images timed out after {int(timeout)}s",
        }
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload = _parse_json_stdout(stdout)

    if completed.returncode != 0:
        # Prefer the structured error the script emits, then its per-slide
        # reasons, then raw stderr — never collapse to a bare exit code.
        structured = str(payload.get("error") or "").strip()
        detail = structured or stderr or stdout or f"exit code {completed.returncode}"
        err_result: dict[str, Any] = {
            "ok": False,
            "error": detail[:1200],
            "failed": (payload.get("failed") or [])[:20],
            "failed_count": int(payload.get("failed_count") or len(payload.get("failed") or [])),
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
        }
        missing = payload.get("missing_credentials")
        if missing:
            err_result["missing_credentials"] = missing
        if auto_fix_meta:
            err_result["auto_fix"] = auto_fix_meta
        err_result = enrich_batch_result_with_disk(
            err_result,
            output_dir=out_dir,
            zip_path=zip_out,
            prompts_file=prompts_file,
            session_key=session_key,
        )
        if err_result.get("recovered_from_disk") and err_result.get("ok"):
            generated = [
                {"slide": n, "image": str(p)}
                for n, p in list_slide_pngs(out_dir)
            ]
            _attach_preview_images(
                err_result,
                generated,
                preview_count=max(0, int(preview_count)),
            )
            if progress_job_id:
                from .slides_batch_artifacts import sync_batch_job_media_from_disk

                sync_batch_job_media_from_disk(progress_job_id, out_dir)
            return err_result
        from .slides_batch_prompts_fix import auto_repair_prompts_file, batch_has_empty_prompt_failures

        if (
            prompts_file
            and auto_fix_prompts
            and not _retry_after_fix
            and batch_has_empty_prompt_failures(err_result)
        ):
            repaired_path, repair_meta = auto_repair_prompts_file(
                prompts_file,
                card_id=card_id,
                task_id=task_id,
                workdir=workdir,
            )
            if repair_meta and repair_meta.get("auto_fixed"):
                log.warning(
                    "slides batch: empty_prompt detected — auto-retry with repaired prompts"
                )
                retry = _run_slides_batch_images(
                    prompts_file=repaired_path,
                    deck_path=None,
                    slides=slides,
                    analyze=False,
                    prompt=prompt,
                    style=style,
                    provider=provider,
                    resolution=resolution,
                    output_dir=str(out_dir),
                    zip_path=str(zip_out),
                    max_slides=max_slides,
                    resume=True,
                    delay=delay,
                    preview_count=preview_count,
                    workers=workers,
                    session_key=session_key,
                    timeout=timeout,
                    progress_job_id=progress_job_id,
                    allow_fallback=allow_fallback,
                    model=model,
                    card_id=card_id,
                    task_id=task_id,
                    workdir=workdir,
                    auto_fix_prompts=False,
                    persistence=persistence,
                    _retry_after_fix=True,
                )
                if isinstance(retry, dict):
                    retry["auto_fix"] = repair_meta
                    retry["retried_after_empty_prompt"] = True
                return retry
        return err_result

    if analyze:
        return {"ok": True, "analyze": True, **payload}

    if not payload.get("ok"):
        return {
            "ok": False,
            "error": str(payload.get("error") or "batch generation failed"),
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
    result: dict[str, Any] = {
        "ok": True,
        "zip_path": att["path"],
        "output_dir": str(payload.get("output_dir") or out_dir),
        "generated_count": int(payload.get("generated_count") or len(generated)),
        "failed_count": int(payload.get("failed_count") or 0),
        "provider": prov,
        "resolution": res,
        "allow_fallback": allow_fallback,
        "model": str(model or "").strip() or None,
        "attachments": [att],
        "download_url": att.get("download_url"),
        "generated": generated[:20],
        "failed": (payload.get("failed") or [])[:20],
    }
    if auto_fix_meta:
        result["auto_fix"] = auto_fix_meta
    _attach_preview_images(result, generated, preview_count=max(0, int(preview_count)))
    result = enrich_batch_result_with_disk(
        result,
        output_dir=out_dir,
        zip_path=zip_out,
        prompts_file=prompts_file,
        session_key=session_key,
    )
    if progress_job_id:
        from .slides_batch_artifacts import sync_batch_job_media_from_disk

        sync_batch_job_media_from_disk(progress_job_id, out_dir)
    return result


def _format_batch_completion_reply(result: dict[str, Any]) -> str:
    count = int(result.get("generated_count") or 0)
    failed = int(result.get("failed_count") or 0)
    missing = int(result.get("missing_count") or 0)
    zip_path = str(result.get("zip_path") or "").strip()
    download = str(result.get("download_url") or "").strip()
    if result.get("recovered_from_disk"):
        lines = [
            f"Lote com falha parcial recuperado do disco — **{count}** imagem(ns) encontrada(s).",
        ]
        batch_err = str(result.get("batch_error") or "").strip()
        if batch_err:
            lines.append(f"Aviso do script: {batch_err[:400]}")
    else:
        lines = [
            f"Lote de slides concluído — **{count}** imagem(ns) gerada(s).",
        ]
    if failed:
        lines.append(f"Falhas na API: **{failed}** slide(s).")
    if missing:
        lines.append(f"Em falta no disco: **{missing}** slide(s).")
    fix = result.get("auto_fix") or {}
    if fix.get("auto_fixed"):
        lines.append(
            "Prompts corrigidos automaticamente (Excel/JSONL truncado → fonte válida)."
        )
    if result.get("retried_after_empty_prompt"):
        lines.append("Lote retomado após correção de `empty_prompt`.")
        failed_items = result.get("failed") or []
        for item in failed_items[:5]:
            if not isinstance(item, dict):
                continue
            slide_no = item.get("slide", "?")
            reason = str(item.get("error") or "erro desconhecido").strip()
            lines.append(f"  - Slide {slide_no}: {reason[:300]}")
        if len(failed_items) > 5:
            lines.append(f"  - … +{len(failed_items) - 5} outra(s) falha(s)")
    if download:
        lines.append(f"Download: [{Path(zip_path).name or 'slides.zip'}]({download})")
    elif zip_path:
        lines.append(f"ZIP: `{zip_path}`")
    return "\n".join(lines)


def _format_batch_failure_reply(result: dict[str, Any]) -> str:
    """Render a batch failure for chat with maximum transparency.

    Shows the full error, missing credentials with guidance, every per-slide
    reason we have room for, and the stderr tail when the failure is
    script-level (no per-slide breakdown). Nothing is silently dropped.
    """
    err = str(result.get("error") or "batch generation failed").strip()
    on_disk = result.get("on_disk") if isinstance(result.get("on_disk"), dict) else {}
    on_disk_count = int(on_disk.get("generated_count") or result.get("generated_count") or 0)
    if on_disk_count > 0 and not result.get("ok"):
        lines = [
            f"⚠️ **Lote falhou na API, mas há {on_disk_count} slide(s) em disco.**",
        ]
        if err and "no images generated" in err.lower():
            lines.append(
                "O erro `no images generated` refere-se só à última tentativa — "
                "verifique a pasta abaixo antes de regenerar tudo."
            )
        elif err:
            lines.append(f"Erro reportado: {err[:800]}")
        lines.append(format_artifacts_summary(on_disk))
    else:
        lines = [f"❌ **Erro no lote de slides (geração falhou):** {err[:1500]}"]

    missing = result.get("missing_credentials") or []
    if missing:
        keys = ", ".join(str(k) for k in missing)
        lines.append(
            f"🔑 Credencial ausente: **{keys}** — configure a chave ou ative o fallback "
            "(remova `no_fallback`)."
        )

    failed_items = [f for f in (result.get("failed") or []) if isinstance(f, dict)]
    if failed_items:
        shown = failed_items[:15]
        lines.append(f"Slides com falha ({len(failed_items)}):")
        for item in shown:
            slide_no = item.get("slide", "?")
            reason = str(item.get("error") or "erro desconhecido").strip()
            lines.append(f"- Slide {slide_no}: {reason[:400]}")
        if len(failed_items) > len(shown):
            lines.append(f"- … +{len(failed_items) - len(shown)} outra(s) falha(s)")
    else:
        # Script-level failure (preflight, no prompts, crash): surface the raw tail.
        stderr = str(result.get("stderr") or "").strip()
        if stderr:
            tail = "\n".join(stderr.splitlines()[-12:])[:1200]
            lines.append("Saída de erro (stderr):\n```\n" + tail + "\n```")
    return "\n".join(lines)


def _log_sync_batch_failure(result: dict[str, Any], *, session_key: str) -> None:
    """Persist a synchronous (non-background) batch failure to the DB error log."""
    try:
        from .chat_error_log import log_error

        extra: dict[str, Any] = {}
        for key in ("missing_credentials", "failed", "failed_count", "provider"):
            if result.get(key):
                extra[key] = result[key]
        detail = (
            str(result.get("stderr") or "").strip()
            or str(result.get("stdout") or "").strip()
            or None
        )
        log_error(
            source="slides_batch_sync",
            kind="image_batch",
            error=str(result.get("error") or "batch generation failed"),
            session_key=session_key,
            detail=detail,
            extra=extra or None,
        )
    except Exception:  # noqa: BLE001 — logging must not break the caller
        log.debug("sync batch failure log skipped", exc_info=True)


def _resolve_batch_job_session_keys(
    session_key: str,
    *,
    parent_job_id: Optional[str] = None,
    display_key: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return (send_key, display_key, persist_key) for batch job binding.

    ``display_key`` is the UI conversation id (job matching / tool-jobs poll).
    ``persist_key`` is where SQLite history lives (may differ for Hermes).
    """
    from .chat_jobs import _conversation_history_key, get_job

    send_key = (session_key or "").strip()
    client_key = (display_key or "").strip() or send_key
    parent_id = (parent_job_id or "").strip()
    if parent_id:
        parent = get_job(parent_id)
        if parent is not None:
            send_key = (parent.session_key or send_key).strip() or send_key
            client_key = (parent.display_key or client_key).strip() or client_key
    persist_key = _conversation_history_key(client_key, send_key)
    return send_key, client_key, persist_key


def spawn_slides_batch_background(
    *,
    persistence: Any = None,
    session_key: str = "",
    parent_job_id: Optional[str] = None,
    display_key: Optional[str] = None,
    **batch_kwargs: Any,
) -> dict[str, Any]:
    """Start batch generation in a daemon thread; returns immediately."""
    from .chat_jobs import (
        append_progress,
        cancel_job_if_session_missing,
        complete_job,
        create_job,
        fail_job,
        is_job_cancelled,
        prime_slide_job_from_message,
    )
    from .chat_tool_background import chat_session_exists, start_chat_progress_mirror

    if batch_kwargs.get("analyze"):
        return _run_slides_batch_images(session_key=session_key, **batch_kwargs)

    send_key, client_key, persist_key = _resolve_batch_job_session_keys(
        session_key,
        parent_job_id=parent_job_id,
        display_key=display_key,
    )
    if persistence is not None and persist_key and not chat_session_exists(
        persistence, persist_key
    ):
        return {
            "ok": False,
            "error": f"conversa {client_key!r} não existe — lote não iniciado",
        }

    label = Path(
        str(batch_kwargs.get("deck_path") or batch_kwargs.get("prompts_file") or "batch")
    ).stem
    max_slides = int(batch_kwargs.get("max_slides") or 200)
    job = create_job(
        send_key,
        kind="image_batch",
        display_key=client_key,
        message_text=f"[image_batch] {label} ({max_slides} slides max)",
    )
    prime_slide_job_from_message(job.id, f"gerar {max_slides} slides")

    mirror_stop = start_chat_progress_mirror(
        job.id,
        persistence=persistence,
        session_key=persist_key,
        mirror_to_chat=False,
    )

    def _worker() -> None:
        try:
            result = _run_slides_batch_images(
                progress_job_id=job.id,
                session_key=send_key,
                persistence=persistence,
                **batch_kwargs,
            )
            if is_job_cancelled(job.id):
                return
            if cancel_job_if_session_missing(
                job.id, persistence, persist_key
            ):
                return
            if result.get("ok"):
                try:
                    complete_job(job.id, result)
                except Exception as persist_exc:
                    log.exception(
                        "batch images generated but job persistence failed job=%s",
                        job.id,
                    )
                    from .openclaw_chat_media_tools import (
                        sanitize_job_result_for_persistence,
                    )

                    try:
                        complete_job(
                            job.id,
                            sanitize_job_result_for_persistence(result) or result,
                        )
                    except Exception:
                        log.warning(
                            "could not persist slim batch result for job %s",
                            job.id,
                        )
                from .chat_image_background import clear_session_image_job

                clear_session_image_job(client_key)
                if (
                    persistence is not None
                    and persist_key
                    and chat_session_exists(persistence, persist_key)
                ):
                    extras: dict[str, Any] = {}
                    media: dict[str, Any] = {}
                    images = result.get("images") or []
                    if images:
                        from .openclaw_chat_media_tools import (
                            sanitize_media_payload_for_persistence,
                        )

                        media = (
                            sanitize_media_payload_for_persistence(
                                {"images": images, "videos": []}
                            )
                            or {}
                        )
                    attachments = result.get("attachments") or []
                    if attachments:
                        extras["attachments"] = attachments
                    if media:
                        extras["media"] = media
                    try:
                        persistence.history.append_message(
                            persist_key,
                            role="assistant",
                            text=_format_batch_completion_reply(result),
                            extras=extras or None,
                        )
                    except Exception as exc:
                        log.warning(
                            "could not persist batch completion for %s: %s",
                            persist_key,
                            exc,
                        )
            else:
                err = str(result.get("error") or "batch generation failed")
                if result.get("recovered_from_disk") and result.get("ok"):
                    try:
                        complete_job(job.id, result)
                    except Exception as persist_exc:
                        log.warning(
                            "could not persist recovered batch job %s: %s",
                            job.id,
                            persist_exc,
                        )
                    if (
                        persistence is not None
                        and persist_key
                        and chat_session_exists(persistence, persist_key)
                    ):
                        try:
                            persistence.history.append_message(
                                persist_key,
                                role="assistant",
                                text=_format_batch_completion_reply(result),
                            )
                        except Exception as exc:
                            log.warning(
                                "could not persist recovered batch for %s: %s",
                                persist_key,
                                exc,
                            )
                else:
                    fail_job(job.id, err, result=result)
                    if (
                        persistence is not None
                        and persist_key
                        and chat_session_exists(persistence, persist_key)
                    ):
                        try:
                            persistence.history.append_message(
                                persist_key,
                                role="assistant",
                                text=_format_batch_failure_reply(result),
                            )
                        except Exception as exc:
                            log.warning(
                                "could not persist batch failure for %s: %s",
                                persist_key,
                                exc,
                            )
        except Exception as exc:
            log.exception("background slides batch crashed job=%s", job.id)
            if not is_job_cancelled(job.id):
                err = f"{type(exc).__name__}: {exc}"
                fail_job(job.id, err)
                if (
                    persistence is not None
                    and persist_key
                    and chat_session_exists(persistence, persist_key)
                ):
                    try:
                        persistence.history.append_message(
                            persist_key,
                            role="assistant",
                            text=(
                                "❌ **Erro inesperado no lote de slides:** "
                                f"{err[:1500]}"
                            ),
                        )
                    except Exception as persist_exc:
                        log.warning(
                            "could not persist batch crash for %s: %s",
                            persist_key,
                            persist_exc,
                        )
        finally:
            mirror_stop.set()

    thread = threading.Thread(
        target=_worker,
        name=f"slides-batch-{job.id}",
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "background": True,
        "batchJobId": job.id,
        "jobId": job.id,
        "sessionKey": client_key,
        "maxSlides": max_slides,
        "message": (
            f"Lote de slides iniciado em background (job `{job.id}`). "
            "O chat continua livre — acompanhe com `qclaw_chat_generation_status` "
            "ou aguarde a mensagem automática com o ZIP."
        ),
    }


def dispatch_slides_batch_images(args: dict[str, Any]) -> dict[str, Any]:
    """Generate batch slide images and ZIP (shared by chat and MCP)."""
    prompts_file = str(args.get("prompts_file") or args.get("prompts") or "").strip() or None
    deck_path = str(args.get("path") or args.get("deck") or "").strip() or None
    if not prompts_file and not deck_path:
        return {"ok": False, "error": "prompts_file or path (deck) is required"}

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
        delay = float(args.get("delay_seconds") or args.get("delay") or 2.0)
    except (TypeError, ValueError):
        delay = 2.0
    try:
        workers = int(args.get("workers") or 1)
    except (TypeError, ValueError):
        workers = 1

    from .slides_batch_prompts_fix import infer_card_id

    inferred_card = infer_card_id(
        card_id=str(args.get("card_id") or "").strip() or None,
        task_id=str(args.get("task_id") or "").strip() or None,
        prompts_file=prompts_file,
    )
    explicit_card = str(args.get("card_id") or args.get("task_id") or "").strip() or None
    card_id = explicit_card or inferred_card

    batch_kwargs = dict(
        prompts_file=prompts_file,
        deck_path=deck_path,
        slides=str(args.get("slides") or "all").strip(),
        analyze=bool(args.get("analyze")),
        prompt=str(args.get("prompt") or "").strip(),
        style=str(args.get("style") or "").strip(),
        provider=str(args.get("provider") or "nano").strip(),
        resolution=str(args.get("resolution") or "2K").strip(),
        output_dir=str(args.get("output_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or args.get("zip") or "").strip() or None,
        max_slides=max_slides,
        resume=bool(args.get("resume")),
        delay=delay,
        preview_count=preview_count,
        workers=workers,
        session_key=str(args.get("session_key") or "").strip(),
        timeout=max(120.0, timeout),
        allow_fallback=parse_allow_fallback_flag(args),
        model=str(args.get("model") or args.get("grok_model") or "").strip(),
        card_id=card_id,
        task_id=str(args.get("task_id") or args.get("card_id") or card_id or "").strip() or None,
        workdir=str(args.get("workdir") or "").strip() or None,
        auto_fix_prompts=not bool(args.get("no_auto_fix_prompts")),
    )
    force_sync = bool(args.get("sync")) or args.get("background") is False
    use_background = not batch_kwargs["analyze"] and not force_sync
    if use_background:
        return spawn_slides_batch_background(
            parent_job_id=str(
                args.get("parent_job_id") or args.get("progress_job_id") or ""
            ).strip()
            or None,
            display_key=str(
                args.get("display_key") or args.get("displayKey") or ""
            ).strip()
            or None,
            **batch_kwargs,
        )
    result = _run_slides_batch_images(
        progress_job_id=str(args.get("progress_job_id") or "").strip() or None,
        **batch_kwargs,
    )
    # Background failures land in the DB error log via fail_job; the synchronous
    # path returns straight to the caller, so log it here too — no failure path
    # should bypass the persistent log.
    if isinstance(result, dict) and not result.get("ok"):
        _log_sync_batch_failure(result, session_key=batch_kwargs.get("session_key") or "")
    return result


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "slides_batch_images",
            "description": (
                "Gera 100–200 slides como imagens PNG (batch) com um único modelo "
                "escolhido (provider + model + no_fallback) e empacota em ZIP para "
                "download. Aceita deck PPTX/HTML ou ficheiro de prompts JSON/JSONL. "
                "Corrige automaticamente empty_prompt (Excel Kanban truncado) quando "
                "card_id/task_id informado. Skill: qclaw-slides-batch-images. "
                "Equivalente a qclaw_slides_batch_images no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Deck PPTX/HTML para extrair prompts por slide",
                    },
                    "prompts_file": {
                        "type": "string",
                        "description": "JSON/JSONL com lista de prompts por slide",
                    },
                    "slides": {
                        "type": "string",
                        "description": "Filtro de slides: all, 3, 2-5 (só com path)",
                    },
                    "analyze": {
                        "type": "boolean",
                        "description": "Só listar prompts, sem gerar imagens",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["nano", "grok", "imagen"],
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["1K", "2K", "4K"],
                    },
                    "max_slides": {
                        "type": "integer",
                        "description": "Limite de segurança (default 200)",
                    },
                    "preview_count": {
                        "type": "integer",
                        "description": "Quantas imagens mostrar inline no chat (default 3)",
                    },
                    "allow_fallback": {
                        "type": "boolean",
                        "description": (
                            "Se false (ou no_fallback=true), mantém o provider escolhido "
                            "(ex.: grok) em todos os slides — sem trocar para nano/imagen."
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
                    "card_id": {
                        "type": "string",
                        "description": "Card Kanban para auto-correção de prompts/xlsx",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Alias de card_id",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Raiz do time (opcional)",
                    },
                    "no_auto_fix_prompts": {
                        "type": "boolean",
                        "description": "Desliga correção automática de empty_prompt",
                    },
                    "workers": {
                        "type": "integer",
                        "description": "Workers paralelos (default 1, max 8)",
                    },
                    "resume": {
                        "type": "boolean",
                        "description": "Só gerar slides em falta no output_dir",
                    },
                    "session_key": {
                        "type": "string",
                        "description": "Chave backend OpenClaw (opcional; herdada do job pai no chat)",
                    },
                    "display_key": {
                        "type": "string",
                        "description": "ID hash da conversa UI — isola progresso e mensagens do lote",
                    },
                },
            },
        }
    ]

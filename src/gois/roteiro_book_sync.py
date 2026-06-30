"""Synchronous book create / assemble / preview / publish — no book_worker queue."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .roteiro_viral_local.bootstrap import ensure_runtime_on_path

log = logging.getLogger(__name__)

_ASSEMBLE_STEPS = frozenset(
    {
        "chapters",
        "plan_sections",
        "write_sections",
        "metadata",
        "subsections",
        "cover",
        "images",
        "place_images",
        "from_text",
    }
)


def _bootstrap() -> None:
    ensure_runtime_on_path()


def _resolve_api_key(explicit: Optional[str] = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    shared = root / "skills" / "_shared"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    import rv_common  # type: ignore[import-untyped]

    key = rv_common.resolve_api_key(None)
    if not key:
        raise ValueError("API Key necessária (GEMINI_API_KEY / GOOGLE_API_KEY)")
    return key


def _book_chapters(book: dict[str, Any]) -> list[dict[str, Any]]:
    chapters = book.get("chapters")
    if isinstance(chapters, list) and chapters:
        return [c for c in chapters if isinstance(c, dict)]
    for container in (book, book.get("final_state") or {}):
        if not isinstance(container, dict):
            continue
        plan = container.get("book_plan") or {}
        if isinstance(plan, dict):
            for key in ("structure", "chapters"):
                raw = plan.get(key)
                if isinstance(raw, list) and raw:
                    return [c for c in raw if isinstance(c, dict)]
    return []


def _helper_job(
    book_id: str,
    job_type: str,
    topic: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    import database

    job_id = f"{job_type}_{book_id}_{uuid.uuid4().hex[:8]}"
    job_data = {
        "job_id": job_id,
        "status": "pending",
        "type": job_type,
        "topic": topic,
        "created_at": time.time(),
        "logs": [f"▶️ {topic}"],
        "request_payload": {"job_id": book_id, **payload},
    }
    database.upsert_job(job_id, job_data)
    return job_id, job_data


def _final_job_status(job_id: str) -> dict[str, Any]:
    import database

    row = database.get_job(job_id) or {}
    status = str(row.get("status") or "").lower()
    return {
        "ok": status in ("completed", "complete", "done", "success"),
        "job_id": job_id,
        "status": status or "unknown",
        "error": row.get("error"),
    }


def _run_inline_book_worker(
    book_id: str,
    step: str,
    handler: Any,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Executa handler do book_worker de forma síncrona (sem fila)."""
    _bootstrap()
    import database

    job_id = f"inline_{step}_{book_id}_{uuid.uuid4().hex[:8]}"
    job_data = {
        "job_id": job_id,
        "status": "pending",
        "type": f"book_{step}",
        "request_payload": {"job_id": book_id, **(payload or {})},
    }
    try:
        handler(job_id, job_data)
    except Exception as exc:
        log.exception("inline book worker %s failed for %s", step, book_id)
        return {"ok": False, "step": step, "book_id": book_id, "error": str(exc)}

    row = database.get_job(job_id) or {}
    status = str(row.get("status") or "").lower()
    ok = status in ("completed", "complete", "done", "success")
    out: dict[str, Any] = {
        "ok": ok,
        "step": step,
        "book_id": book_id,
        "inline": True,
    }
    if row.get("error"):
        out["error"] = row["error"]
    if row.get("final_state"):
        out["final_state"] = row["final_state"]
    try:
        database.delete_job(job_id)
    except Exception:
        pass
    return out


def _place_all_images_sync(book_id: str, api_key: str) -> dict[str, Any]:
    """Posiciona imagens em todas as secções que têm conteúdo + imagens."""
    _bootstrap()
    import database
    from workers import book_worker

    job_or_book = database.get_book_as_job(book_id)
    if not job_or_book:
        return {"ok": False, "error": "Livro não encontrado", "book_id": book_id}

    from services.book_service_helpers import resolve_book_plan_from_state, resolve_chapter_key

    state = job_or_book.get("final_state") or {}
    book_plan, _plan_key = resolve_book_plan_from_state(state)
    if not book_plan:
        book_plan = job_or_book if isinstance(job_or_book, dict) else {}
    chapters_key = resolve_chapter_key(book_plan)
    chapters = book_plan.get(chapters_key) or []

    placed = 0
    errors: list[str] = []
    for ch_idx, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            continue
        for sec_idx, sec in enumerate(ch.get("sections") or []):
            if not isinstance(sec, dict):
                continue
            content = str(sec.get("content") or "").strip()
            images = list(sec.get("images") or [])
            single_path = sec.get("image_path")
            if single_path and not any(
                (x.get("path") if isinstance(x, dict) else x) == single_path for x in images
            ):
                images.append({"path": single_path, "caption": ""})
            if not content or not images:
                continue
            result = _run_inline_book_worker(
                book_id,
                "place_images",
                book_worker.run_book_place_images_job,
                {
                    "api_key": api_key,
                    "chapter_index": ch_idx,
                    "section_index": sec_idx,
                },
            )
            if result.get("ok"):
                placed += 1
            else:
                errors.append(f"cap.{ch_idx + 1}.sec.{sec_idx + 1}: {result.get('error') or 'falha'}")

    if placed == 0 and errors:
        return {
            "ok": False,
            "step": "place_images",
            "book_id": book_id,
            "error": "; ".join(errors[:3]),
            "sections_placed": 0,
        }
    return {
        "ok": True,
        "step": "place_images",
        "book_id": book_id,
        "sections_placed": placed,
        "errors": errors[:5],
    }


def create_book_sync(
    *,
    topic: str = "",
    source_text: str = "",
    chapters: int = 8,
    sections_per_chapter: int = 4,
    audience: str = "Público geral",
    tone: str = "Didático e acessível",
    language: str = "Português (Brasil)",
    category: str = "Não-ficção",
    draft: str = "",
    api_key: Optional[str] = None,
    run_from_text: bool = True,
    defer_worker: bool = False,
) -> dict[str, Any]:
    """Create book stub in Mongo; optionally run from_text structure sync.

    defer_worker: cria rascunho (planning_draft) sem enfileirar plan_book_job — uso do chat inline.
    """
    _bootstrap()
    from services.book_service import book_service

    key = _resolve_api_key(api_key) if not defer_worker else (api_key or "").strip() or None
    text = (source_text or draft or "").strip()
    topic_clean = (topic or "").strip()

    if text and not topic_clean:
        topic_clean = text[:80].replace("\n", " ").strip() or "A partir de texto"
        if len(text) > 80:
            topic_clean += "…"

    if not topic_clean and not text:
        return {"ok": False, "error": "Informe topic ou source_text/draft"}

    if defer_worker:
        draft_req: dict[str, Any] = {
            "title": topic_clean,
            "topic": topic_clean,
            "draft": text or "",
            "audience": audience,
            "tone": tone,
            "language": language,
            "category": category,
            "num_chapters": max(1, int(chapters)),
        }
        if key:
            draft_req["api_key"] = key
        result = book_service.create_planning_draft_book(draft_req)
        book_id = str(result.get("job_id") or "").strip()
        if not book_id:
            return {"ok": False, "error": "create_planning_draft_book não devolveu book_id"}
        return {
            "ok": True,
            "book_id": book_id,
            "mode": "planning_draft",
            "steps": [],
        }

    req: dict[str, Any] = {
        "api_key": key,
        "language": language,
        "num_chapters": max(1, int(chapters)),
        "num_sections_per_chapter": max(1, int(sections_per_chapter)),
    }
    if text:
        req["draft"] = text

    result = book_service.create_book_job(
        topic=topic_clean,
        audience=audience,
        tone=tone,
        category=category,
        language=language,
        req_dict=req,
    )
    book_id = str(result.get("job_id") or "").strip()
    if not book_id:
        return {"ok": False, "error": "create_book_job não devolveu book_id"}

    out: dict[str, Any] = {
        "ok": True,
        "book_id": book_id,
        "mode": "from_text" if text and run_from_text else "topic",
        "steps": [],
    }

    if text and run_from_text:
        step = run_assemble_step_sync(
            book_id=book_id,
            step="from_text",
            api_key=key,
            source_text=text,
            language=language,
        )
        out["steps"].append(step)
        out["ok"] = bool(step.get("ok"))

    return out


def run_assemble_step_sync(
    *,
    book_id: str,
    step: str,
    api_key: Optional[str] = None,
    num_chapters: int = 8,
    sections_per_chapter: int = 4,
    model_name: str = "gemini-3.5-flash",
    min_reading_time: int = 2,
    cover_styles: str = "Cinematic",
    author: str = "",
    cover_prompt: str = "",
    source_text: str = "",
    language: str = "Português (Brasil)",
    max_workers: Optional[int] = None,
    progress: bool = False,
    on_progress: Any = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run one pipeline step inline (worker handler or direct service)."""
    _bootstrap()
    step = (step or "").strip().lower()
    if step not in _ASSEMBLE_STEPS:
        return {"ok": False, "error": f"step desconhecido: {step}"}

    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}

    key = _resolve_api_key(api_key)
    from services.book_service import book_service
    from workers import book_worker

    inline_kw = _sync_inline_kwargs(
        progress=progress,
        on_progress=on_progress,
        job_id=job_id,
        max_workers=max_workers,
    )

    if step == "from_text":
        text = (source_text or "").strip()
        if not text:
            return {"ok": False, "error": "source_text obrigatório para from_text"}
        import database

        prev = database.get_job(bid) or {}
        prev_payload = dict(prev.get("request_payload") or {})
        prev_payload.update({"creation_pipeline": "from_text", "source_text": text, "language": language})
        if key:
            prev_payload["api_key"] = key
        database.upsert_job(bid, {"status": "planning_draft", "request_payload": prev_payload, "error": None})
        try:
            database.update_book_standalone(bid, {"status": "planning_draft"})
        except Exception:
            pass
        result = _run_inline_book_worker(
            bid,
            "from_text",
            book_worker.run_book_create_from_text_job,
            {"book_id": bid, "source_text": text, "api_key": key, "language": language},
        )
        return {**result, "step": step}

    if step == "chapters":
        from .book_inline_generate import plan_chapters_inline, persist_chapters_to_book
        from .chat_book_editor import _fetch_book_detail, _normalize_chapters_from_book

        book = _fetch_book_detail(bid)
        topic = str(book.get("topic") or book.get("title") or "").strip()
        audience = str(book.get("audience") or book.get("target_audience") or "Público geral")
        tone = str(book.get("tone") or "Didático e acessível")
        widget_fields = {
            "topic": topic,
            "title": book.get("title"),
            "audience": audience,
            "tone": tone,
        }
        chapters_data = plan_chapters_inline(
            topic=topic or bid,
            chapters=max(1, int(num_chapters)),
            audience=audience,
            tone=tone,
            book_id=bid,
            widget_fields=widget_fields,
            **inline_kw,
        )
        persist_chapters_to_book(bid, widget_fields, chapters_data)
        return {"ok": True, "step": step, "book_id": bid, "chapters": len(chapters_data)}

    if step == "plan_sections":
        from .book_inline_generate import plan_sections_inline, persist_chapters_to_book
        from .chat_book_editor import _fetch_book_detail, _normalize_chapters_from_book

        book = _fetch_book_detail(bid)
        topic = str(book.get("topic") or book.get("title") or "").strip()
        chapters_data = _normalize_chapters_from_book(book)
        if not chapters_data:
            return {"ok": False, "error": "Livro sem capítulos — execute step=chapters primeiro"}
        widget_fields = {
            "topic": topic,
            "title": book.get("title"),
            "audience": book.get("audience") or book.get("target_audience"),
            "tone": book.get("tone"),
        }
        chapters_data = plan_sections_inline(
            chapters_data,
            topic=topic or bid,
            sections_per_chapter=max(1, int(sections_per_chapter)),
            book_id=bid,
            widget_fields=widget_fields,
            **inline_kw,
        )
        persist_chapters_to_book(bid, widget_fields, chapters_data)
        return {"ok": True, "step": step, "book_id": bid, "chapters": len(chapters_data)}

    if step == "write_sections":
        from .book_inline_generate import generate_all_content_inline, persist_chapters_to_book
        from .chat_book_editor import _fetch_book_detail, _normalize_chapters_from_book

        book = _fetch_book_detail(bid)
        topic = str(book.get("topic") or book.get("title") or "").strip()
        audience = str(book.get("audience") or book.get("target_audience") or "Público geral")
        tone = str(book.get("tone") or "Didático e acessível")
        chapters_data = _normalize_chapters_from_book(book)
        if not chapters_data:
            return {"ok": False, "error": "Livro sem capítulos"}
        widget_fields = {
            "topic": topic,
            "title": book.get("title"),
            "audience": audience,
            "tone": tone,
        }
        chapters_data = generate_all_content_inline(
            chapters_data,
            topic=topic or bid,
            audience=audience,
            tone=tone,
            include_chapters=True,
            include_sections=True,
            include_subsections=False,
            book_id=bid,
            widget_fields=widget_fields,
            **inline_kw,
        )
        persist_chapters_to_book(bid, widget_fields, chapters_data)
        return {"ok": True, "step": step, "book_id": bid, "chapters": len(chapters_data)}

    if step == "metadata":
        result = _run_inline_book_worker(
            bid,
            "metadata",
            book_worker.run_book_generate_metadata_job,
            {"api_key": key},
        )
        return {**result, "step": step}

    if step == "subsections":
        from .book_inline_generate import run_structure_pipeline_inline, persist_chapters_to_book
        from .chat_book_editor import _fetch_book_detail, _normalize_chapters_from_book

        book = _fetch_book_detail(bid)
        topic = str(book.get("topic") or book.get("title") or "").strip()
        audience = str(book.get("audience") or book.get("target_audience") or "Público geral")
        tone = str(book.get("tone") or "Didático e acessível")
        chapters_data = _normalize_chapters_from_book(book)
        if not chapters_data:
            return {"ok": False, "error": "Livro sem capítulos"}
        widget_fields = {
            "topic": topic,
            "title": book.get("title"),
            "audience": audience,
            "tone": tone,
        }
        chapters_data = run_structure_pipeline_inline(
            topic=topic or bid,
            audience=audience,
            tone=tone,
            chapters=len(chapters_data),
            sections_per_chapter=max(1, int(sections_per_chapter)),
            chapters_data=chapters_data,
            plan_chapters=False,
            plan_sections=False,
            plan_subsections=True,
            write_content=True,
            write_subsections_only=True,
            book_id=bid,
            widget_fields=widget_fields,
            **inline_kw,
        )
        persist_chapters_to_book(bid, widget_fields, chapters_data)
        return {"ok": True, "step": step, "book_id": bid, "chapters": len(chapters_data)}

    if step == "cover":
        styles = [s.strip() for s in (cover_styles or "").split(",") if s.strip()]
        plan = book_service.plan_book_cover(
            bid, styles or ["Cinematic"], "front", None, author or None
        )
        prompt = (cover_prompt or plan.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "plan_cover não devolveu prompt"}
        result = book_service.generate_book_cover(
            job_id=bid,
            prompt=prompt,
            api_key=key,
            target="front",
            model_name="imagen-4.0-ultra-generate-001",
        )
        ok = bool(result.get("file_path") or result.get("image_path") or result.get("status") == "success")
        return {"ok": ok, "step": step, "book_id": bid, "result": result}

    if step == "images":
        result = _run_inline_book_worker(
            bid,
            "images",
            book_worker.run_book_generate_all_section_images_job,
            {"api_key": key},
        )
        return {**result, "step": step}

    if step == "place_images":
        return _place_all_images_sync(bid, key)

    return {"ok": False, "error": f"step não implementado: {step}"}


def run_replan_objectives_sync(
    *,
    book_id: str,
    chapter_index: int = 0,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Replan section objectives for one chapter inline (no worker queue)."""
    _bootstrap()
    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}
    key = _resolve_api_key(api_key)
    job_id, job = _helper_job(
        bid,
        "book_replan_objectives",
        f"Replanejar objetivos cap. {chapter_index + 1}",
        {"job_id": bid, "chapter_index": chapter_index, "api_key": key},
    )
    from workers import book_worker

    book_worker.run_book_replan_objectives_job(job_id, job)
    fin = _final_job_status(job_id)
    return {"ok": fin["ok"], "book_id": bid, "chapter_index": chapter_index, "job_id": job_id, **fin}


def run_extract_objective_sync(
    *,
    book_id: str,
    draft: str = "",
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Extract book objective from draft inline (no worker queue)."""
    _bootstrap()
    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}
    key = _resolve_api_key(api_key)
    job_id, job = _helper_job(
        bid,
        "book_extract_objective",
        "Extrair objetivo do rascunho",
        {"job_id": bid, "draft": draft, "api_key": key},
    )
    from workers import book_worker

    book_worker.run_book_extract_objective_job(job_id, job)
    fin = _final_job_status(job_id)
    return {"ok": fin["ok"], "book_id": bid, "job_id": job_id, **fin}


def assemble_book_sync(
    *,
    book_id: str,
    steps: Optional[list[str]] = None,
    api_key: Optional[str] = None,
    num_chapters: int = 8,
    sections_per_chapter: int = 4,
    with_metadata: bool = True,
    with_subsections: bool = True,
    with_cover: bool = False,
    with_images: bool = False,
    with_place_images: bool = False,
    skip_structure: bool = False,
    max_workers: Optional[int] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run structure + optional extras synchronously."""
    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}

    pipeline: list[str] = []
    if not skip_structure:
        pipeline.extend(["chapters", "plan_sections", "write_sections"])
    if with_metadata:
        pipeline.append("metadata")
    if with_subsections:
        pipeline.append("subsections")
    if with_cover:
        pipeline.append("cover")
    if with_images:
        pipeline.append("images")
    if with_place_images:
        pipeline.append("place_images")
    if steps:
        pipeline = [s.strip().lower() for s in steps if s and s.strip()]

    results: list[dict[str, Any]] = []
    progress = bool(kwargs.pop("progress", False))
    on_progress = kwargs.pop("on_progress", None)
    job_id = kwargs.pop("job_id", None)
    for step in pipeline:
        r = run_assemble_step_sync(
            book_id=bid,
            step=step,
            api_key=api_key,
            num_chapters=num_chapters,
            sections_per_chapter=sections_per_chapter,
            progress=progress,
            on_progress=on_progress,
            job_id=job_id,
            max_workers=max_workers,
            **kwargs,
        )
        results.append(r)
        if not r.get("ok"):
            return {
                "ok": False,
                "book_id": bid,
                "failed_step": step,
                "steps": results,
                "error": r.get("error") or f"falhou em {step}",
            }

    return {"ok": True, "book_id": bid, "steps": results}


def preview_book_sync(
    *,
    book_id: str,
    mode: str = "book",
    chapter_index: int = 0,
    section_index: int = 0,
    max_chars: int = 12000,
    include_subsections: bool = True,
) -> dict[str, Any]:
    """Markdown preview of book / chapter / section (no worker, no UI)."""
    _bootstrap()
    import database

    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}

    book = database.get_book(bid) or database.get_book_as_job(bid)
    if not book:
        return {"ok": False, "error": f"Livro não encontrado: {bid}"}

    plan = book.get("book_plan") or (book.get("final_state") or {}).get("book_plan") or {}
    title = (
        (plan.get("title") if isinstance(plan, dict) else None)
        or book.get("title")
        or book.get("topic")
        or bid
    )
    chapters = _book_chapters(book)
    mode = (mode or "book").strip().lower()
    lines: list[str] = []
    written = 0
    total = 0

    def _section_body(sec: dict[str, Any]) -> str:
        nonlocal written, total
        total += 1
        body = str(sec.get("content") or sec.get("text") or "").strip()
        if body:
            written += 1
        return body

    if mode == "section":
        if chapter_index < 0 or chapter_index >= len(chapters):
            return {"ok": False, "error": f"chapter_index fora do intervalo (0–{max(0, len(chapters) - 1)})"}
        chapter = chapters[chapter_index]
        sections = chapter.get("sections") or []
        if section_index < 0 or section_index >= len(sections):
            return {"ok": False, "error": "section_index fora do intervalo"}
        sec = sections[section_index] if isinstance(sections[section_index], dict) else {}
        lines.append(f"# {title}")
        lines.append(f"## {chapter.get('title') or f'Capítulo {chapter_index + 1}'}")
        lines.append(f"### {sec.get('title') or f'Seção {section_index + 1}'}")
        obj = (sec.get("objective") or sec.get("purpose") or "").strip()
        if obj:
            lines.append(f"\n*{obj}*\n")
        lines.append(_section_body(sec))
        if include_subsections:
            for sub in sec.get("subsections") or []:
                if not isinstance(sub, dict):
                    continue
                sub_obj = (sub.get("objective") or "").strip()
                sub_body = (sub.get("content") or "").strip()
                if sub_obj:
                    lines.append(f"\n#### {sub_obj}\n")
                if sub_body:
                    lines.append(sub_body)
    elif mode == "chapter":
        if chapter_index < 0 or chapter_index >= len(chapters):
            return {"ok": False, "error": f"chapter_index fora do intervalo (0–{max(0, len(chapters) - 1)})"}
        chapter = chapters[chapter_index]
        lines.append(f"# {title}")
        lines.append(f"## {chapter.get('title') or f'Capítulo {chapter_index + 1}'}")
        intro = (chapter.get("introduction") or chapter.get("content") or "").strip()
        if intro:
            lines.append(intro)
        for sec in chapter.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            lines.append(f"\n### {sec.get('title') or 'Seção'}\n")
            lines.append(_section_body(sec))
    else:
        lines.append(f"# {title}")
        desc = (plan.get("description") if isinstance(plan, dict) else "") or ""
        if desc:
            lines.append(f"\n{desc.strip()}\n")
        for ch_idx, chapter in enumerate(chapters):
            lines.append(f"\n## {chapter.get('title') or f'Capítulo {ch_idx + 1}'}\n")
            for sec in chapter.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                lines.append(f"### {sec.get('title') or 'Seção'}\n")
                lines.append(_section_body(sec))

    markdown = "\n".join(lines).strip()
    truncated = len(markdown) > max(1, int(max_chars))
    if truncated:
        markdown = markdown[: int(max_chars)].rstrip() + "\n\n… *(preview truncado)*"

    try:
        from .chat_rv_screens import build_embed_url, rv_frontend_base

        embed_url = build_embed_url(f"/book/{bid}")
        frontend_base = rv_frontend_base(require_path=f"/book/{bid}")
    except Exception:
        embed_url = ""
        frontend_base = ""

    return {
        "ok": True,
        "book_id": bid,
        "mode": mode,
        "title": title,
        "chapters": len(chapters),
        "sections_written": written,
        "sections_total": total,
        "truncated": truncated,
        "markdown": markdown,
        "embed_url": embed_url,
        "frontend_url": f"{frontend_base}/book/{bid}" if frontend_base else "",
    }


def _markdown_to_simple_html(md: str) -> str:
    lines: list[str] = []
    for line in (md or "").splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("<br/>")
            continue
        if stripped.startswith("### "):
            lines.append(f"<h3>{html_lib.escape(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            lines.append(f"<h2>{html_lib.escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            lines.append(f"<h1>{html_lib.escape(stripped[2:])}</h1>")
        elif stripped.startswith("*") and stripped.endswith("*") and len(stripped) > 2:
            lines.append(f"<p><em>{html_lib.escape(stripped.strip('*').strip())}</em></p>")
        else:
            lines.append(f"<p>{html_lib.escape(stripped)}</p>")
    return "\n".join(lines)


def _resolve_epub_path(book_id: str) -> str:
    from .chat_book_editor import _resolve_book_epub_path

    return _resolve_book_epub_path(book_id)


def publish_epub_sync(
    *,
    book_id: str,
    author: str = "Autor",
    prologue: str = "",
    acknowledgments: str = "",
    generate_cover: bool = False,
    generate_images: bool = True,
    export_format: str = "",
    api_key: Optional[str] = None,
    session_key: str = "",
) -> dict[str, Any]:
    """Compile full EPUB inline (no worker queue)."""
    _bootstrap()
    import database
    from services.book_export_service import book_export_service

    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}
    if not database.get_book_as_job(bid) and not database.get_book(bid):
        return {"ok": False, "error": f"Livro não encontrado: {bid}"}

    key: Optional[str] = None
    if generate_images:
        try:
            key = _resolve_api_key(api_key)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    from .chat_book_editor import (
        book_epub_download_url,
        persist_book_epub_path,
        prepare_book_for_epub_export,
    )

    prepare_book_for_epub_export(bid)

    def _run_compile(*, with_images: bool) -> None:
        asyncio.run(
            book_export_service.generate_full_epub_task(
                bid,
                author=(author or "Autor").strip() or "Autor",
                prologue=(prologue or "").strip(),
                acknowledgments=(acknowledgments or "").strip(),
                generate_cover=bool(generate_cover),
                api_key=key if with_images else None,
            )
        )

    _run_compile(with_images=bool(generate_images and key))
    epub_path = _resolve_epub_path(bid)
    if not epub_path and generate_images and key:
        _run_compile(with_images=False)
        epub_path = _resolve_epub_path(bid)
    if not epub_path:
        return {"ok": False, "error": "EPUB não gerado — verifique se o livro tem conteúdo"}

    persist_book_epub_path(bid, epub_path)
    sk = str(session_key or "").strip()

    out: dict[str, Any] = {
        "ok": True,
        "book_id": bid,
        "format": "epub",
        "epub_path": epub_path,
        "bytes": os.path.getsize(epub_path),
        "download_url": book_epub_download_url(bid, epub_path=epub_path, session_key=sk),
    }
    if export_format:
        out["export_format"] = export_format
    if sk and out.get("download_url"):
        from .chat_artifacts import stage_artifact

        try:
            att = stage_artifact(Path(epub_path), session_key=sk)
            out["attachment"] = att
        except Exception:
            pass
    return out


def _chapter_has_content(chapter: dict[str, Any]) -> bool:
    intro = str(chapter.get("introduction") or chapter.get("content") or "").strip()
    if intro:
        return True
    for sec in chapter.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        sec_body = str(
            sec.get("content")
            or sec.get("section_text")
            or sec.get("draft")
            or sec.get("editedReigenText")
            or sec.get("reigenText")
            or ""
        ).strip()
        if sec_body:
            return True
        for sub in sec.get("subsections") or []:
            if not isinstance(sub, dict):
                continue
            if str(sub.get("content") or sub.get("draft") or "").strip():
                return True
    return False


def publish_chapter_sync(
    *,
    book_id: str,
    chapter_index: int = 0,
    num_sections: int = 3,
    author: str = "Autor",
    model_name: str = "gemini-3.5-flash",
    min_reading_time: int = 2,
    api_key: Optional[str] = None,
    session_key: str = "",
    force_rewrite: bool = False,
    max_workers: Optional[int] = None,
    progress: bool = False,
    on_progress: Any = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Plan sections, write chapter text, compile chapter EPUB — inline (no worker queue)."""
    _bootstrap()
    import database
    from agents.epub_agent import generate_chapter_epub

    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}
    if not database.get_book_as_job(bid) and not database.get_book(bid):
        return {"ok": False, "error": f"Livro não encontrado: {bid}"}

    try:
        key = _resolve_api_key(api_key)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    ch_idx = max(0, int(chapter_index))
    steps: list[dict[str, Any]] = []
    inline_kw = _sync_inline_kwargs(
        progress=progress,
        on_progress=on_progress,
        job_id=job_id,
        max_workers=max_workers,
    )

    jd = database.get_book_as_job(bid)
    if not jd:
        return {"ok": False, "error": f"Livro não encontrado: {bid}"}

    plan = (jd.get("final_state") or {}).get("book_plan") or {}
    chapters = plan.get("chapters") or plan.get("structure") or []
    if ch_idx >= len(chapters):
        return {
            "ok": False,
            "error": f"chapter_index fora do intervalo (0–{max(0, len(chapters) - 1)})",
        }

    chapter = chapters[ch_idx]
    if not chapter.get("sections"):
        try:
            from .book_inline_generate import plan_sections_inline, persist_chapters_to_book
            from .chat_book_editor import _normalize_chapters_from_book

            topic = str(plan.get("title") or jd.get("topic") or bid)
            audience = str(plan.get("target_audience") or plan.get("audience") or "Público geral")
            tone = str(plan.get("tone") or "Didático e acessível")
            chapters_data = _normalize_chapters_from_book({"book_plan": plan, **jd})
            if not chapters_data:
                chapters_data = _normalize_chapters_from_book({"chapters": chapters})
            widget_fields = {
                "topic": topic,
                "title": plan.get("title"),
                "audience": audience,
                "tone": tone,
            }
            if ch_idx < len(chapters_data):
                one = [chapters_data[ch_idx]]
                one = plan_sections_inline(
                    one,
                    topic=topic,
                    sections_per_chapter=max(1, int(num_sections)),
                    only_empty=True,
                    book_id=bid,
                    widget_fields=widget_fields,
                    **inline_kw,
                )
                chapters_data[ch_idx] = one[0]
                persist_chapters_to_book(bid, widget_fields, chapters_data)
            steps.append({"step": "plan_sections", "ok": True})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Falha ao planear seções: {exc}", "steps": steps}
        jd = database.get_book_as_job(bid) or jd
        plan = (jd.get("final_state") or {}).get("book_plan") or plan
        chapters = plan.get("chapters") or plan.get("structure") or chapters
        chapter = chapters[ch_idx]

    needs_write = force_rewrite or not _chapter_has_content(chapter)
    if needs_write:
        try:
            from .book_inline_generate import generate_all_content_inline, persist_chapters_to_book
            from .chat_book_editor import _normalize_chapters_from_book

            topic = str(plan.get("title") or jd.get("topic") or bid)
            audience = str(plan.get("target_audience") or plan.get("audience") or "Público geral")
            tone = str(plan.get("tone") or "Didático e acessível")
            chapters_data = _normalize_chapters_from_book({"book_plan": plan, **jd})
            widget_fields = {
                "topic": topic,
                "title": plan.get("title"),
                "audience": audience,
                "tone": tone,
            }
            if ch_idx < len(chapters_data):
                one = [dict(chapters_data[ch_idx])]
                one = generate_all_content_inline(
                    one,
                    topic=topic,
                    audience=audience,
                    tone=tone,
                    include_chapters=True,
                    include_sections=True,
                    include_subsections=False,
                    skip_existing=not force_rewrite,
                    book_id=bid,
                    widget_fields=widget_fields,
                    **inline_kw,
                )
                chapters_data[ch_idx] = one[0]
                persist_chapters_to_book(bid, widget_fields, chapters_data)
            steps.append({"step": "write_chapter", "ok": True})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Falha ao escrever capítulo: {exc}", "steps": steps}
        jd = database.get_book_as_job(bid) or jd
        plan = (jd.get("final_state") or {}).get("book_plan") or plan
        chapters = plan.get("chapters") or plan.get("structure") or chapters
        chapter = chapters[ch_idx]

    if not _chapter_has_content(chapter):
        return {
            "ok": False,
            "error": (
                "Capítulo sem conteúdo após escrita — créditos IA esgotados ou "
                "todos os modelos de texto falharam. Recarregue créditos Gemini ou "
                "configure Grok/DeepSeek/OpenRouter."
            ),
            "steps": steps,
        }

    book_title = str(plan.get("title") or jd.get("topic") or "Livro")
    book_author = str(plan.get("author") or author or "Autor").strip() or "Autor"

    try:
        epub_path = generate_chapter_epub(
            chapter_data=chapter,
            book_title=book_title,
            author=book_author,
            job_id=bid,
            log_callback=lambda m: log.info("%s", m),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Falha ao gerar EPUB: {exc}", "steps": steps}

    if not epub_path or not os.path.isfile(epub_path):
        return {"ok": False, "error": "EPUB do capítulo não gerado", "steps": steps}

    steps.append({"step": "epub", "ok": True, "epub_path": epub_path})
    out: dict[str, Any] = {
        "ok": True,
        "book_id": bid,
        "chapter_index": ch_idx,
        "format": "epub",
        "scope": "chapter",
        "epub_path": epub_path,
        "bytes": os.path.getsize(epub_path),
        "steps": steps,
    }
    if session_key:
        from .chat_artifacts import stage_artifact

        att = stage_artifact(Path(epub_path), session_key=session_key)
        out["attachment"] = att
        out["download_url"] = att.get("download_url")
    return out


def publish_portal_sync(
    *,
    book_id: str,
    author: str = "",
    session_key: str = "",
    output_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Build static HTML site from book content (no worker)."""
    bid = (book_id or "").strip()
    if not bid:
        return {"ok": False, "error": "book_id é obrigatório"}

    preview = preview_book_sync(book_id=bid, mode="book", max_chars=500_000)
    if not preview.get("ok"):
        return preview

    title = str(preview.get("title") or bid)
    body_html = _markdown_to_simple_html(str(preview.get("markdown") or ""))
    author_name = (author or "").strip() or "Autor"

    from .chat_artifacts import resolve_artifacts_dir, stage_artifact

    safe = re.sub(r"[^\w\-]+", "-", title).strip("-")[:48] or bid[:16]
    site_dir = Path(output_dir).expanduser() if output_dir else resolve_artifacts_dir() / f"book-portal-{bid}"
    site_dir.mkdir(parents=True, exist_ok=True)
    images_dir = site_dir / "images"
    images_dir.mkdir(exist_ok=True)

    cover_rel = ""
    try:
        _bootstrap()
        from services.book_service_library import resolve_book_cover_file_path

        cover_src = resolve_book_cover_file_path(bid)
        if cover_src and os.path.isfile(cover_src):
            cover_dest = images_dir / "cover.jpg"
            shutil.copy2(cover_src, cover_dest)
            cover_rel = "images/cover.jpg"
    except Exception:
        pass

    cover_html = (
        f'<img class="cover" src="{cover_rel}" alt="Capa"/>' if cover_rel else ""
    )
    index_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html_lib.escape(title)}</title>
  <style>
    body {{ font-family: Georgia, serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; color: #1a1a1a; }}
    h1 {{ font-size: 2rem; margin-bottom: 0.25rem; }}
    h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }}
    h3 {{ margin-top: 1.25rem; }}
    .meta {{ color: #555; margin-bottom: 2rem; }}
    .cover {{ max-width: 14rem; display: block; margin: 0 auto 2rem; border-radius: 4px; box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
  </style>
</head>
<body>
  {cover_html}
  <p class="meta">{html_lib.escape(author_name)}</p>
  <article>{body_html}</article>
</body>
</html>
"""
    (site_dir / "index.html").write_text(index_html, encoding="utf-8")

    zip_base = str(site_dir.parent / site_dir.name)
    shutil.make_archive(zip_base, "zip", root_dir=str(site_dir))
    zip_path = Path(f"{zip_base}.zip")

    out: dict[str, Any] = {
        "ok": True,
        "book_id": bid,
        "format": "portal",
        "title": title,
        "site_dir": str(site_dir),
        "index_html": str(site_dir / "index.html"),
        "zip_path": str(zip_path),
        "bytes": zip_path.stat().st_size if zip_path.is_file() else 0,
    }
    if session_key and zip_path.is_file():
        att = stage_artifact(zip_path, label=f"{safe}.zip", session_key=session_key)
        out["attachment"] = att
        out["download_url"] = att.get("download_url")
    return out


def _sync_workers(args: dict[str, Any]) -> Optional[int]:
    raw = args.get("workers")
    if raw is None:
        raw = args.get("max_workers")
    if raw is None:
        return None
    try:
        return max(1, min(int(raw), 8))
    except (TypeError, ValueError):
        return None


def _sync_job_id(args: dict[str, Any]) -> Optional[str]:
    raw = args.get("job_id") or args.get("progress_job_id")
    val = str(raw or "").strip()
    return val or None


def _sync_progress_handler(
    *,
    progress: bool = False,
    on_progress: Any = None,
) -> Any:
    if on_progress is not None:
        return on_progress
    if progress:
        from .book_inline_generate import make_cli_progress_handler

        return make_cli_progress_handler()
    return None


def _sync_inline_kwargs(
    *,
    progress: bool = False,
    on_progress: Any = None,
    job_id: Optional[str] = None,
    max_workers: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "max_workers": max_workers,
        "on_progress": _sync_progress_handler(progress=progress, on_progress=on_progress),
        "job_id": job_id,
    }


def dispatch_roteiro_book_sync_tool(args: dict[str, Any]) -> dict[str, Any]:
    """MCP/chat dispatcher for roteiro_book_sync."""
    action = str(args.get("action") or args.get("command") or "assemble").strip().lower()
    progress = bool(args.get("progress") or args.get("emit_progress"))
    job_id = _sync_job_id(args)
    sync_kw = {"progress": progress, "job_id": job_id}

    if action in ("preview", "show", "read"):
        return preview_book_sync(
            book_id=str(args.get("book_id") or "").strip(),
            mode=str(args.get("mode") or "book"),
            chapter_index=int(args.get("chapter_index") or 0),
            section_index=int(args.get("section_index") or 0),
            max_chars=int(args.get("max_chars") or 12000),
            include_subsections=bool(args.get("include_subsections", True)),
        )

    if action in ("create", "init", "new"):
        return create_book_sync(
            topic=str(args.get("topic") or args.get("tema") or "").strip(),
            source_text=str(args.get("source_text") or args.get("from_text") or args.get("draft") or "").strip(),
            chapters=int(args.get("chapters") or args.get("num_chapters") or 8),
            sections_per_chapter=int(args.get("sections_per_chapter") or 4),
            audience=str(args.get("audience") or "Público geral"),
            tone=str(args.get("tone") or "Didático e acessível"),
            language=str(args.get("language") or "Português (Brasil)"),
            api_key=str(args.get("api_key") or "").strip() or None,
            run_from_text=bool(args.get("run_from_text", True)),
        )

    if action in ("step", "run_step"):
        return run_assemble_step_sync(
            book_id=str(args.get("book_id") or "").strip(),
            step=str(args.get("step") or "").strip(),
            api_key=str(args.get("api_key") or "").strip() or None,
            num_chapters=int(args.get("chapters") or args.get("num_chapters") or 8),
            sections_per_chapter=int(args.get("sections_per_chapter") or 4),
            model_name=str(args.get("model") or "gemini-3.5-flash"),
            cover_styles=str(args.get("cover_styles") or "Cinematic"),
            author=str(args.get("author") or ""),
            source_text=str(args.get("source_text") or "").strip(),
            language=str(args.get("language") or "Português (Brasil)"),
            max_workers=_sync_workers(args),
            **sync_kw,
        )

    session_key = str(args.get("session_key") or "").strip()
    bid = str(args.get("book_id") or "").strip()
    author = str(args.get("author") or "Autor")

    if action in ("publish_chapter", "pub_chapter", "chapter_epub", "chapter_pub"):
        ch_arg = args.get("chapter_index")
        if ch_arg is None and args.get("chapter") is not None:
            ch_arg = int(args.get("chapter")) - 1
        return publish_chapter_sync(
            book_id=bid,
            chapter_index=int(ch_arg if ch_arg is not None else 0),
            num_sections=int(args.get("sections_per_chapter") or args.get("num_sections") or 3),
            author=author,
            model_name=str(args.get("model") or args.get("model_name") or "gemini-3.5-flash"),
            min_reading_time=int(args.get("min_reading_time") or 2),
            api_key=str(args.get("api_key") or "").strip() or None,
            session_key=session_key,
            force_rewrite=bool(args.get("force_rewrite", False)),
            max_workers=_sync_workers(args),
            **sync_kw,
        )

    if action in ("publish", "export", "pub", "epub", "export_epub"):
        fmt = str(args.get("format") or args.get("publish_as") or "epub").strip().lower()
        if action in ("epub", "export_epub"):
            fmt = "epub"
        ch_arg = args.get("chapter_index")
        if ch_arg is None and args.get("chapter") is not None:
            ch_arg = int(args.get("chapter")) - 1
        if ch_arg is not None:
            return publish_chapter_sync(
                book_id=bid,
                chapter_index=int(ch_arg),
                num_sections=int(args.get("sections_per_chapter") or args.get("num_sections") or 3),
                author=author,
                model_name=str(args.get("model") or args.get("model_name") or "gemini-3.5-flash"),
                min_reading_time=int(args.get("min_reading_time") or 2),
                api_key=str(args.get("api_key") or "").strip() or None,
                session_key=session_key,
                force_rewrite=bool(args.get("force_rewrite", False)),
                max_workers=_sync_workers(args),
                **sync_kw,
            )
        if fmt in ("portal", "site", "html", "web"):
            return publish_portal_sync(book_id=bid, author=author, session_key=session_key)
        if fmt in ("epub", "ebook"):
            return publish_epub_sync(
                book_id=bid,
                author=author,
                prologue=str(args.get("prologue") or "").strip(),
                acknowledgments=str(args.get("acknowledgments") or "").strip(),
                generate_cover=bool(args.get("generate_cover", False)),
                generate_images=bool(args.get("generate_images", True)),
                export_format=str(args.get("export_format") or "").strip(),
                api_key=str(args.get("api_key") or "").strip() or None,
                session_key=session_key,
            )
        return {"ok": False, "error": f"format desconhecido: {fmt}. Use epub ou portal."}

    if action in ("portal", "site"):
        return publish_portal_sync(book_id=bid, author=author, session_key=session_key)

    return assemble_book_sync(
        book_id=str(args.get("book_id") or "").strip(),
        steps=args.get("steps") if isinstance(args.get("steps"), list) else None,
        api_key=str(args.get("api_key") or "").strip() or None,
        num_chapters=int(args.get("chapters") or args.get("num_chapters") or 8),
        sections_per_chapter=int(args.get("sections_per_chapter") or 4),
        with_metadata=bool(args.get("with_metadata", True)),
        with_subsections=bool(args.get("with_subsections", False)),
        with_cover=bool(args.get("with_cover", False)),
        with_images=bool(args.get("with_images", False)),
        with_place_images=bool(args.get("with_place_images", False)),
        skip_structure=bool(args.get("skip_structure", False)),
        max_workers=_sync_workers(args),
        model_name=str(args.get("model") or "gemini-3.5-flash"),
        cover_styles=str(args.get("cover_styles") or "Cinematic"),
        author=str(args.get("author") or ""),
        **sync_kw,
    )


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "roteiro_book_sync",
            "description": (
                "[Conteúdo] Criar, montar, publicar ou pré-visualizar livro SEM book_worker: "
                "action=create|assemble|step|preview|publish|publish_chapter. Executa handlers inline (Mongo + LLM). "
                "Skill: qclaw-roteiro-livro-sync."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "assemble",
                            "step",
                            "preview",
                            "publish",
                            "publish_chapter",
                            "pub_chapter",
                            "epub",
                            "portal",
                        ],
                        "description": (
                            "create=novo livro; assemble=pipeline sync; step=uma etapa; "
                            "preview=markdown; publish/epub=EPUB livro completo; "
                            "publish_chapter=planear+escrever+EPUB de um capítulo; portal=site HTML"
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["epub", "portal"],
                        "description": "Com action=publish: epub (default) ou portal (site HTML)",
                    },
                    "book_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "author": {"type": "string"},
                    "prologue": {"type": "string"},
                    "acknowledgments": {"type": "string"},
                    "generate_cover": {"type": "boolean"},
                    "generate_images": {"type": "boolean"},
                    "source_text": {"type": "string", "description": "Texto longo / rascunho"},
                    "step": {
                        "type": "string",
                        "enum": sorted(_ASSEMBLE_STEPS),
                    },
                    "chapters": {"type": "integer"},
                    "sections_per_chapter": {"type": "integer"},
                    "with_metadata": {"type": "boolean"},
                    "with_subsections": {"type": "boolean"},
                    "with_cover": {"type": "boolean"},
                    "with_images": {"type": "boolean"},
                    "mode": {
                        "type": "string",
                        "enum": ["book", "chapter", "section"],
                        "description": "preview: escopo do markdown",
                    },
                    "chapter_index": {"type": "integer"},
                    "section_index": {"type": "integer"},
                    "max_chars": {"type": "integer", "description": "Limite do preview markdown"},
                    "workers": {
                        "type": "integer",
                        "description": "Threads paralelas para planeamento/escrita inline (default 4, max 8; env QCLAW_BOOK_INLINE_WORKERS)",
                    },
                    "progress": {
                        "type": "boolean",
                        "description": "Emitir QCLAW_BLOCK no stderr e logs de fase durante assemble/step/publish_chapter",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "ID de job chat para cancelamento cooperativo (jobs_cancel)",
                    },
                },
                "required": [],
            },
        }
    ]


def chat_tool_specs() -> list[dict[str, Any]]:
    spec = mcp_tool_specs()[0]
    return [
        {
            "type": "function",
            "function": {
                "name": "qclaw_roteiro_book_sync",
                "description": spec["description"],
                "parameters": spec["inputSchema"],
            },
        }
    ]

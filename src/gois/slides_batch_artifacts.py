"""Discover slide batch PNG/ZIP artifacts on disk — even when jobs report failure."""

from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_SLIDE_PNG_RE = re.compile(r"^slide-(\d+)\.png$", re.IGNORECASE)
_SLIDE_NUM_FILE_RE = re.compile(r"^slide-(\d+)", re.IGNORECASE)


def parse_slide_png_name(name: str) -> Optional[int]:
    match = _SLIDE_PNG_RE.match((name or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_slide_number_from_filename(name: str) -> Optional[int]:
    """Parse slide index from canonical or provider-suffixed batch PNG names."""
    match = _SLIDE_NUM_FILE_RE.match((name or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_canonical_slide_png(name: str, slide_no: int) -> bool:
    return (name or "").strip().lower() == f"slide-{slide_no:03d}.png"


def list_slide_pngs(output_dir: Path) -> list[tuple[int, Path]]:
    root = Path(output_dir).expanduser()
    if not root.is_dir():
        return []
    by_slide: dict[int, Path] = {}
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_size <= 0:
                continue
        except OSError:
            continue
        slide_no = parse_slide_number_from_filename(entry.name)
        if slide_no is None:
            continue
        resolved = entry.resolve()
        prev = by_slide.get(slide_no)
        if prev is None:
            by_slide[slide_no] = resolved
            continue
        if _is_canonical_slide_png(entry.name, slide_no):
            by_slide[slide_no] = resolved
    return sorted(by_slide.items(), key=lambda item: item[0])


def sync_batch_job_media_from_disk(job_id: str, output_dir: Path | str) -> int:
    """Align job partial_media + block progress with PNGs already on disk."""
    root = Path(output_dir).expanduser().resolve()
    slides = list_slide_pngs(root)
    if not slides or not (job_id or "").strip():
        return 0
    from .chat_jobs import get_job, set_block_progress, update_job_media

    images = [
        {"caption": f"Slide {slide_no}", "image_path": str(path.resolve())}
        for slide_no, path in slides
    ]
    update_job_media(
        job_id,
        {"images": images, "videos": [], "output_dir": str(root)},
    )
    job = get_job(job_id)
    total = int(job.block_max) if job is not None and job.block_max > 0 else len(slides)
    max_slide = max(slide_no for slide_no, _ in slides)
    if total > 0:
        set_block_progress(job_id, min(max_slide, total), total)
    return len(slides)


def _count_prompts(prompts_file: Optional[str]) -> Optional[int]:
    path = Path(str(prompts_file or "").strip()).expanduser()
    if not path.is_file():
        return None
    if path.suffix.lower() == ".jsonl":
        count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                count += 1
        return count or None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, list):
        return len(data) or None
    if isinstance(data, dict):
        slides = data.get("slides")
        if isinstance(slides, list):
            return len(slides) or None
    return None


def _missing_slides(existing: list[int], total: Optional[int]) -> list[int]:
    if not total or total <= 0:
        return []
    have = set(existing)
    return [n for n in range(1, total + 1) if n not in have]


def _zip_png_count(zip_path: Path) -> int:
    if not zip_path.is_file():
        return 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return sum(
                1
                for name in zf.namelist()
                if parse_slide_png_name(Path(name).name) is not None
            )
    except (OSError, zipfile.BadZipFile):
        return 0


def _rebuild_batch_zip(zip_path: Path, png_paths: list[Path]) -> Path:
    """Rewrite ZIP from on-disk PNGs when the archive is stale or partial."""
    dest = zip_path.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(png_paths, key=lambda p: p.name)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in ordered:
            if img.is_file() and img.stat().st_size > 0:
                zf.write(img, arcname=img.name)
    return dest


def scan_batch_artifacts(
    *,
    output_dir: Optional[str] = None,
    zip_path: Optional[str] = None,
    prompts_file: Optional[str] = None,
    job_id: Optional[str] = None,
    keyword: Optional[str] = None,
    session_key: Optional[str] = None,
) -> dict[str, Any]:
    """Scan disk for batch slide artifacts (PNGs, ZIP, gaps)."""
    resolved_output: Optional[Path] = None
    resolved_zip: Optional[Path] = None
    resolved_prompts: Optional[str] = None
    job_row: Optional[dict[str, Any]] = None
    matches: list[dict[str, Any]] = []

    if job_id:
        from .chat_jobs import get_job, job_to_dict

        job = get_job(job_id.strip())
        if job is None:
            return {"ok": False, "error": f"job not found: {job_id}"}
        job_row = job_to_dict(job)
        result = job.result if isinstance(job.result, dict) else {}
        if result.get("output_dir"):
            resolved_output = Path(str(result["output_dir"])).expanduser()
        if result.get("zip_path"):
            resolved_zip = Path(str(result["zip_path"])).expanduser()
        if not resolved_output and job.message_text:
            label = _label_from_message(job.message_text)
            if label:
                guess = _default_paths_for_label(label)
                resolved_output = guess[0]
                resolved_zip = resolved_zip or guess[1]

    if output_dir:
        resolved_output = Path(output_dir).expanduser()
    if zip_path:
        resolved_zip = Path(zip_path).expanduser()
    if prompts_file:
        resolved_prompts = str(Path(prompts_file).expanduser())

    kw = (keyword or "").strip().lower()
    if kw and resolved_output is None:
        for match in find_artifact_dirs_by_keyword(kw):
            images_dir = match / "images"
            pngs = list_slide_pngs(images_dir if images_dir.is_dir() else match)
            if not pngs:
                continue
            zip_candidates = sorted(match.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            matches.append(
                {
                    "dir": str(match.resolve()),
                    "output_dir": str((images_dir if images_dir.is_dir() else match).resolve()),
                    "zip_path": str(zip_candidates[0].resolve()) if zip_candidates else None,
                    "generated_count": len(pngs),
                    "slide_numbers": [n for n, _ in pngs[:20]],
                }
            )
        if matches and resolved_output is None:
            best = max(matches, key=lambda m: int(m.get("generated_count") or 0))
            resolved_output = Path(str(best["output_dir"]))
            if best.get("zip_path"):
                resolved_zip = Path(str(best["zip_path"]))

    if resolved_output is None:
        return {
            "ok": False,
            "error": "output_dir, job_id or keyword is required",
            "matches": matches,
        }

    pngs = list_slide_pngs(resolved_output)
    slide_numbers = [n for n, _ in pngs]
    total = _count_prompts(resolved_prompts)
    if total is None and resolved_prompts is None:
        prompts_guess = resolved_output.parent / f"{resolved_output.parent.name}.jsonl"
        if not prompts_guess.is_file():
            prompts_guess = resolved_output.parent / "prompts.jsonl"
        if prompts_guess.is_file():
            resolved_prompts = str(prompts_guess)
            total = _count_prompts(resolved_prompts)

    if resolved_zip is None and resolved_output.parent.is_dir():
        stem = resolved_output.parent.name
        guess_zip = resolved_output.parent / f"{stem}.zip"
        if guess_zip.is_file():
            resolved_zip = guess_zip

    missing = _missing_slides(slide_numbers, total)
    zip_count = _zip_png_count(resolved_zip) if resolved_zip else 0
    if resolved_zip and pngs and zip_count < len(pngs):
        try:
            _rebuild_batch_zip(resolved_zip, [path for _, path in pngs])
            zip_count = len(pngs)
            report_zip_rebuilt = True
        except OSError as exc:
            log.warning("batch artifacts: could not rebuild zip %s: %s", resolved_zip, exc)
            report_zip_rebuilt = False
    else:
        report_zip_rebuilt = False

    first_png = str(pngs[0][1]) if pngs else None
    last_png = str(pngs[-1][1]) if pngs else None

    report: dict[str, Any] = {
        "ok": True,
        "generated_count": len(pngs),
        "output_dir": str(resolved_output.resolve()),
        "zip_path": str(resolved_zip.resolve()) if resolved_zip and resolved_zip.is_file() else None,
        "zip_png_count": zip_count,
        "zip_rebuilt": report_zip_rebuilt,
        "prompts_file": resolved_prompts,
        "expected_total": total,
        "missing_count": len(missing),
        "missing_slides": missing[:50],
        "slide_numbers_sample": slide_numbers[:20],
        "first_image": first_png,
        "last_image": last_png,
        "partial": bool(missing),
        "job": job_row,
        "matches": matches,
    }
    if session_key and resolved_zip and resolved_zip.is_file():
        try:
            from .chat_artifacts import stage_artifact

            att = stage_artifact(
                resolved_zip,
                label=resolved_zip.name,
                session_key=session_key,
            )
            report["download_url"] = att.get("download_url")
            report["staged_zip_path"] = att.get("path")
        except OSError:
            pass
    return report


def find_artifact_dirs_by_keyword(keyword: str) -> list[Path]:
    from .chat_artifacts import resolve_artifacts_dir

    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    root = resolve_artifacts_dir()
    if not root.is_dir():
        return []
    hits: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        name = path.name.lower()
        if kw in name or kw in str(path).lower():
            if (path / "images").is_dir() or any(
                parse_slide_png_name(f.name) is not None for f in path.iterdir() if f.is_file()
            ):
                hits.append(path)
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits


def _label_from_message(message_text: str) -> str:
    text = (message_text or "").strip()
    if not text:
        return ""
    if text.startswith("[image_batch]"):
        text = text[len("[image_batch]") :].strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text


def _default_paths_for_label(label: str) -> tuple[Path, Path]:
    from .chat_artifacts import resolve_artifacts_dir

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:48]
    base = resolve_artifacts_dir() / safe
    return base / "images", base / f"{safe}.zip"


def enrich_batch_result_with_disk(
    result: dict[str, Any],
    *,
    output_dir: Path,
    zip_path: Path,
    prompts_file: Optional[str] = None,
    session_key: str = "",
) -> dict[str, Any]:
    """Attach on-disk artifact scan; upgrade partial batches to recoverable success."""
    scan = scan_batch_artifacts(
        output_dir=str(output_dir),
        zip_path=str(zip_path),
        prompts_file=prompts_file,
        session_key=session_key,
    )
    if not scan.get("ok"):
        return result

    on_disk = int(scan.get("generated_count") or 0)
    if on_disk <= 0:
        result["on_disk"] = scan
        return result

    merged = dict(result)
    merged["on_disk"] = scan
    merged["generated_count"] = max(int(merged.get("generated_count") or 0), on_disk)
    merged["output_dir"] = scan.get("output_dir") or str(output_dir)
    if scan.get("zip_path"):
        merged["zip_path"] = scan["zip_path"]
    if scan.get("download_url"):
        merged["download_url"] = scan["download_url"]
    merged["missing_count"] = int(scan.get("missing_count") or 0)
    merged["missing_slides"] = scan.get("missing_slides") or []
    merged["partial_success"] = bool(merged.get("failed_count") or merged.get("missing_count"))

    if not merged.get("ok"):
        merged["partial_success"] = True
        merged["ok"] = True
        merged["recovered_from_disk"] = True
        err = str(merged.get("error") or "").strip()
        if err:
            merged["batch_error"] = err
            merged["error"] = None
        if scan.get("zip_path") and session_key and not merged.get("download_url"):
            try:
                from .chat_artifacts import stage_artifact

                att = stage_artifact(
                    Path(str(scan["zip_path"])),
                    label=Path(str(scan["zip_path"])).name,
                    session_key=session_key,
                )
                merged["zip_path"] = att.get("path") or scan["zip_path"]
                merged["download_url"] = att.get("download_url")
                merged["attachments"] = [att]
            except OSError:
                pass
    return merged


def format_artifacts_summary(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        return f"Não foi possível localizar artefatos: {report.get('error', '?')}"
    count = int(report.get("generated_count") or 0)
    lines = [f"**{count}** slide(s) encontrado(s) em disco."]
    out = str(report.get("output_dir") or "").strip()
    if out:
        lines.append(f"Pasta: `{out}`")
    missing = int(report.get("missing_count") or 0)
    if missing:
        lines.append(f"Em falta: **{missing}** slide(s).")
        sample = report.get("missing_slides") or []
        if sample:
            shown = ", ".join(str(n) for n in sample[:15])
            lines.append(f"Números em falta (amostra): {shown}")
    zip_p = str(report.get("zip_path") or "").strip()
    if zip_p:
        lines.append(f"ZIP: `{Path(zip_p).name}`")
    download = str(report.get("download_url") or "").strip()
    if download:
        lines.append(f"Download: [{Path(zip_p).name if zip_p else 'slides.zip'}]({download})")
    matches = report.get("matches") or []
    if len(matches) > 1:
        lines.append(f"Outras pastas coincidentes: {len(matches) - 1}")
    return "\n".join(lines)


def dispatch_slides_batch_artifacts(args: dict[str, Any]) -> dict[str, Any]:
    report = scan_batch_artifacts(
        output_dir=str(args.get("output_dir") or args.get("images_dir") or "").strip() or None,
        zip_path=str(args.get("zip_path") or "").strip() or None,
        prompts_file=str(args.get("prompts_file") or "").strip() or None,
        job_id=str(args.get("job_id") or args.get("jobId") or "").strip() or None,
        keyword=str(args.get("keyword") or args.get("query") or args.get("task_id") or "").strip()
        or None,
        session_key=str(args.get("session_key") or args.get("session") or "").strip() or None,
    )
    if report.get("ok"):
        report["summary"] = format_artifacts_summary(report)
    return report


def mcp_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "slides_batch_artifacts",
            "description": (
                "Localiza slides PNG/ZIP já gerados em disco — mesmo quando o job "
                "falhou ou o chat diz 'nenhum gerado'. Procura por output_dir, job_id "
                "ou keyword (ex. d3, TASK-039). Skill: qclaw-chat-slides-batch-artifacts. "
                "Equivalente a qclaw_slides_batch_artifacts no chat QClaw."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": "Pasta com slide-NNN.png",
                    },
                    "zip_path": {"type": "string", "description": "Caminho do ZIP"},
                    "prompts_file": {
                        "type": "string",
                        "description": "JSONL para calcular slides em falta",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job image_batch (chat_send_jobs)",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Pesquisa em .stack/chat/artifacts (ex. d3, prompts-d3)",
                    },
                    "session_key": {
                        "type": "string",
                        "description": "Staging do ZIP para download no chat",
                    },
                },
                "required": [],
            },
        }
    ]

"""Render PDF and slide decks to PNG pages for inline chat preview."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

SLIDE_EXTENSIONS = {".pptx", ".ppt", ".odp", ".key"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = SLIDE_EXTENSIONS | PDF_EXTENSIONS


def find_soffice() -> str | None:
    for cmd in ("soffice", "libreoffice"):
        if shutil.which(cmd):
            return cmd
    mac_path = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if mac_path.is_file():
        return str(mac_path)
    return None


def find_pdftoppm() -> str | None:
    return shutil.which("pdftoppm")


def pdf_page_count(pdf_path: Path) -> int | None:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        return None
    result = subprocess.run(
        [pdfinfo, str(pdf_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        return None
    return int(match.group(1))


def parse_page_range(pages: str) -> tuple[int | None, int | None]:
    pages = (pages or "").strip()
    if not pages:
        return None, None
    if "-" in pages:
        start_s, end_s = pages.split("-", 1)
        return int(start_s), int(end_s)
    page = int(pages)
    return page, page


def convert_to_pdf(input_path: Path, out_dir: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice não encontrado. Instale: brew install --cask libreoffice"
        )
    result = subprocess.run(
        [
            soffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Conversão para PDF falhou")
    pdf = out_dir / f"{input_path.stem}.pdf"
    if pdf.is_file():
        return pdf
    pdfs = sorted(out_dir.glob("*.pdf"))
    if not pdfs:
        raise RuntimeError(f"Conversão falhou: {input_path}")
    return pdfs[0]


def render_pdf_pages(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int = 150,
    pages: str | None = None,
) -> list[Path]:
    pdftoppm = find_pdftoppm()
    if not pdftoppm:
        raise RuntimeError("pdftoppm não encontrado. Instale: brew install poppler")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    cmd = [pdftoppm, "-png", "-r", str(dpi)]
    if pages:
        first, last = parse_page_range(pages)
        if first is not None:
            cmd.extend(["-f", str(first)])
        if last is not None:
            cmd.extend(["-l", str(last)])
    cmd.extend([str(pdf_path), str(prefix)])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "pdftoppm falhou")
    return sorted(out_dir.glob("page-*.png"))


def _emit_page_progress(
    callback: Optional[Callable[[int, int, str], None]],
    *,
    page_num: int,
    total: int,
    message: str,
) -> None:
    if callback is None:
        return
    try:
        callback(page_num, total, message)
    except Exception:
        pass


def render_document_pages(
    input_path: Path,
    out_dir: Path,
    *,
    dpi: int = 150,
    pages: str | None = None,
    max_pages: int = 12,
    on_page_progress: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[list[Path], int | None]:
    input_path = input_path.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {input_path}")
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Formato não suportado: {ext}. Use PDF, PPTX, PPT, ODP ou KEY."
        )

    total_pages: int | None = None
    if ext in PDF_EXTENSIONS:
        total_pages = pdf_page_count(input_path)
        _emit_page_progress(
            on_page_progress,
            page_num=0,
            total=total_pages or max_pages,
            message="A renderizar PDF…",
        )
        rendered = render_pdf_pages(input_path, out_dir, dpi=dpi, pages=pages)
    else:
        _emit_page_progress(
            on_page_progress,
            page_num=0,
            total=max_pages,
            message="A converter para PDF…",
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = convert_to_pdf(input_path, Path(tmp))
            total_pages = pdf_page_count(pdf_path)
            _emit_page_progress(
                on_page_progress,
                page_num=0,
                total=total_pages or max_pages,
                message="A renderizar páginas…",
            )
            rendered = render_pdf_pages(pdf_path, out_dir, dpi=dpi, pages=pages)

    if max_pages > 0 and len(rendered) > max_pages:
        rendered = rendered[:max_pages]

    batch_total = max(1, len(rendered))
    doc_total = total_pages or batch_total
    for idx, png_path in enumerate(rendered, start=1):
        page_num = page_number_from_path(png_path) or idx
        msg = f"Página {page_num}/{batch_total}"
        if doc_total > batch_total:
            msg += f" (documento: {doc_total} páginas)"
        _emit_page_progress(
            on_page_progress,
            page_num=idx,
            total=batch_total,
            message=msg,
        )
    return rendered, total_pages


def page_number_from_path(path: Path) -> int | None:
    match = re.search(r"page-(\d+)\.png$", path.name)
    if not match:
        return None
    return int(match.group(1))

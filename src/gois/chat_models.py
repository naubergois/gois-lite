"""Multi-provider chat models + attachment helpers for the dashboard chat."""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Optional

import httpx
from openai import OpenAI

from .config import ChatModelEntry, OpenclawChatConfig
from .local_paths import media_chat_subdir, project_stack_root
from .secrets_fallback import is_placeholder, resolve_llm_api_key

log = logging.getLogger(__name__)

IMAGE_MIME_PREFIX = "image/"
JSON_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/x-json",
        "application/vnd.api+json",
    }
)
TEXT_MIME_SUFFIXES = (
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".sql",
    ".html",
    ".css",
    ".xml",
    ".csv",
    ".log",
    ".env",
    ".toml",
    ".ini",
    ".cfg",
)
AUDIO_MIME_PREFIX = "audio/"
AUDIO_MIME_SUFFIXES = (
    ".mp3",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".aac",
    ".flac",
    ".mp4",
    ".m4b",
)
PDF_MIME_TYPE = "application/pdf"
PDF_SUFFIX = ".pdf"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_SUFFIX = ".docx"
PDF_MAX_CHARS = 80_000


@dataclass(frozen=True)
class ResolvedChatModel:
    entry: ChatModelEntry
    api_key: str


@dataclass(frozen=True)
class ParsedAttachment:
    name: str
    mime_type: str
    data: bytes
    data_url: str
    path: Optional[Path] = None


def is_audio_attachment(att: ParsedAttachment) -> bool:
    if att.mime_type.startswith(AUDIO_MIME_PREFIX):
        return True
    lower = att.name.lower()
    return any(lower.endswith(sfx) for sfx in AUDIO_MIME_SUFFIXES)


def is_pdf_attachment(att: ParsedAttachment) -> bool:
    if att.mime_type == PDF_MIME_TYPE:
        return True
    return att.name.lower().endswith(PDF_SUFFIX)


def is_docx_attachment(att: ParsedAttachment) -> bool:
    if att.mime_type == DOCX_MIME_TYPE:
        return True
    return att.name.lower().endswith(DOCX_SUFFIX)


# Minimum embedded-text length below which a PDF is treated as scanned/image-only
# and an OCR fallback is attempted.
PDF_OCR_MIN_CHARS = 80
# Scanned PDFs often still carry a tiny per-page stamp of embedded text (page
# numbers, process ids). If the average characters per page is below this, the
# document is treated as image-only and OCR is attempted.
PDF_OCR_MIN_CHARS_PER_PAGE = 100
# Cap OCR work so a huge scanned PDF cannot block the request indefinitely.
PDF_OCR_MAX_PAGES = 40


def extract_pdf_embedded_text(data: bytes, *, max_chars: int = PDF_MAX_CHARS) -> str:
    """Read the PDF text layer only (no OCR).

    Uses pypdf to extract embedded/selectable text. Returns up to ``max_chars``.
    """
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            remaining = max_chars - total
            if remaining <= 0:
                break
            chunk = text[:remaining]
            if chunk.strip():
                parts.append(chunk)
            total += len(chunk)
        return "\n\n".join(parts).strip()
    except Exception as exc:
        raise ValueError(f"não foi possível extrair texto do PDF: {exc}") from exc


def _pdf_embedded_needs_ocr(extracted: str, page_count: int) -> bool:
    """True when embedded text looks like a scan (missing or stamp-only)."""
    sparse = bool(page_count) and len(extracted) < page_count * PDF_OCR_MIN_CHARS_PER_PAGE
    return len(extracted) < PDF_OCR_MIN_CHARS or sparse


def extract_pdf_text(
    data: bytes,
    *,
    max_chars: int = PDF_MAX_CHARS,
    allow_ocr: bool = True,
) -> str:
    """Extract plain text from a PDF.

    Always reads the embedded text layer first (``pypdf``). When ``allow_ocr``
    is true and the PDF looks scanned (little or no embedded text), falls back
    to OCR via the ``pdftoppm`` + ``tesseract`` CLIs when available.
    """
    try:
        import io
        from pypdf import PdfReader

        page_count = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        page_count = 0

    extracted = extract_pdf_embedded_text(data, max_chars=max_chars)
    if not allow_ocr or not _pdf_embedded_needs_ocr(extracted, page_count):
        return extracted

    # Scanned/image PDF — try OCR as a best-effort fallback.
    try:
        ocr_text = ocr_pdf_text(data, max_chars=max_chars)
    except Exception as exc:  # never fail the whole extraction on OCR errors
        log.warning("OCR fallback failed: %s", exc)
        ocr_text = ""
    return ocr_text.strip() if len(ocr_text.strip()) > len(extracted) else extracted


def ocr_pdf_text(data: bytes, *, max_chars: int = PDF_MAX_CHARS) -> str:
    """OCR a (scanned) PDF using the poppler ``pdftoppm`` and ``tesseract`` CLIs.

    Returns an empty string when the required binaries are not installed or no
    text could be recognised. Renders at most ``PDF_OCR_MAX_PAGES`` pages.
    """
    import shutil
    import subprocess
    import tempfile

    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        log.info("OCR skipped: pdftoppm/tesseract not available on PATH")
        return ""

    langs = os.getenv("OCR_LANGS", "por+eng")
    with tempfile.TemporaryDirectory(prefix="qcm-ocr-") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "input.pdf"
        src.write_bytes(data)
        # Render pages to PNGs at 200 DPI: input-1.png, input-2.png, ...
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "200",
                 "-l", str(PDF_OCR_MAX_PAGES), str(src), str(tmp_path / "page")],
                check=True, capture_output=True, timeout=180,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("pdftoppm render failed: %s", exc)
            return ""

        pages = sorted(tmp_path.glob("page*.png"))
        parts: list[str] = []
        total = 0
        for png in pages:
            if total >= max_chars:
                break
            try:
                proc = subprocess.run(
                    ["tesseract", str(png), "stdout", "-l", langs],
                    check=True, capture_output=True, timeout=120,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("tesseract failed on %s: %s", png.name, exc)
                continue
            text = proc.stdout.decode("utf-8", errors="ignore")
            chunk = text[: max_chars - total]
            if chunk.strip():
                parts.append(chunk)
            total += len(chunk)
        return "\n\n".join(parts).strip()


def extract_docx_text(data: bytes, *, max_chars: int = PDF_MAX_CHARS) -> str:
    """Extract plain text from .docx bytes (OOXML zip). Returns up to max_chars."""
    try:
        import io
        import xml.etree.ElementTree as ET
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        parts: list[str] = []
        for node in root.iter(f"{w_ns}t"):
            if node.text:
                parts.append(node.text)
            if node.tail:
                parts.append(node.tail)
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text[:max_chars]
    except Exception as exc:
        raise ValueError(f"não foi possível extrair texto do Word: {exc}") from exc


def transcribe_audio_bytes(
    *,
    data: bytes,
    name: str,
    mime_type: str,
    api_key: str,
    model: str = "whisper-1",
    timeout: float = 120.0,
) -> str:
    """Transcribe audio bytes via OpenAI and return normalized text."""
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for audio transcription")

    ext = Path(name).suffix or ".wav"
    with NamedTemporaryFile(suffix=ext) as tmp:
        tmp.write(data)
        tmp.flush()
        tmp.seek(0)
        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.audio.transcriptions.create(model=model, file=tmp)

    text = getattr(response, "text", None)
    if not isinstance(text, str):
        try:
            text = response.get("text")  # type: ignore[union-attr]
        except Exception:
            text = None
    transcript = str(text or "").strip()
    if not transcript:
        raise ValueError(f"transcrição vazia para {name!r} ({mime_type})")
    return transcript


def resolve_attachments_dir(chat_cfg: OpenclawChatConfig) -> Path:
    from .local_paths import media_storage_root

    if media_storage_root() is not None:
        return media_chat_subdir("attachments")

    raw = Path(chat_cfg.attachments_temp_dir).expanduser()
    if not raw.is_absolute():
        raw = (project_stack_root().parent / raw).resolve()
    raw.mkdir(parents=True, exist_ok=True)
    return raw


def _safe_session_dir(session_key: str) -> str:
    slug = re.sub(r"[^\w\-.]+", "_", (session_key or "").strip())
    return (slug[:96] or "session").strip("_") or "session"


def _safe_filename(name: str) -> str:
    base = Path(name).name or "anexo"
    safe = re.sub(r"[^\w\-.]+", "_", base).strip("._")
    return (safe[:120] or "anexo")


def _normalize_attachment_mime(name: str, mime: str) -> str:
    """Infer JSON/text MIME when browsers send application/octet-stream."""
    cleaned = str(mime or "").lower().split(";", 1)[0].strip()
    if cleaned and cleaned not in {"application/octet-stream", "binary/octet-stream"}:
        return cleaned
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    lower = name.lower()
    if lower.endswith(".json") or lower.endswith(".jsonl"):
        return "application/json"
    return cleaned or "application/octet-stream"


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def persist_attachments(
    attachments: list[ParsedAttachment],
    *,
    session_key: str,
    attachments_dir: Path,
) -> list[ParsedAttachment]:
    """Write attachment bytes to disk and return entries with absolute paths."""
    if not attachments:
        return []
    session_dir = attachments_dir / _safe_session_dir(session_key)
    session_dir.mkdir(parents=True, exist_ok=True)
    out: list[ParsedAttachment] = []
    for att in attachments:
        if att.path is not None and att.path.is_file():
            out.append(att)
            continue
        dest = session_dir / f"{int(time.time() * 1000)}_{_safe_filename(att.name)}"
        dest.write_bytes(att.data)
        out.append(
            ParsedAttachment(
                name=att.name,
                mime_type=att.mime_type,
                data=att.data,
                data_url=att.data_url,
                path=dest.resolve(),
            )
        )
    return out


def format_attachment_paths_for_model(attachments: list[ParsedAttachment]) -> str:
    lines: list[str] = []
    for att in attachments:
        if att.path is None:
            continue
        lines.append(
            f"- `{att.path}` — {att.name} ({att.mime_type}, {len(att.data)} bytes)"
        )
    if not lines:
        return ""
    return (
        "[Anexos guardados no disco]\n"
        + "\n".join(lines)
        + "\n\nUse `qclaw_run_shell` (cat, head, file, python3, etc.) "
        "para ler ou processar estes ficheiros."
    )


GEMINI_LEGACY_MODEL_ALIASES: dict[str, str] = {
    "gemini-2.0-flash": "gemini-3.5-flash",
    "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
}

CHAT_DEFAULT_MODEL_ID = "deepseek-chat"
V4_FLASH_MODEL_ID = "deepseek-v4-flash"
# DeepSeek ~1M token context; 800K chars ≈ ~200K tokens at ~4 chars/token (safety margin).
DEEPSEEK_MAX_CONTEXT_CHARS = 800_000

# Anthropic expõe um endpoint compatível com o SDK OpenAI (chat.completions),
# incluindo tool calling — é o que permite usar Claude no loop padrão do chat.
ANTHROPIC_OPENAI_COMPAT_BASE_URL = "https://api.anthropic.com/v1/"

# A camada compat da Anthropic exige max_tokens explícito (a Messages API
# não tem default implícito).
_ANTHROPIC_COMPAT_MAX_TOKENS = 8192


def effective_chat_default_model_id(model_id: Optional[str] = None) -> str:
    """Never use V4 Flash as the implicit default (high token burn)."""
    mid = (str(model_id or "").strip() or CHAT_DEFAULT_MODEL_ID)
    if mid == V4_FLASH_MODEL_ID:
        return CHAT_DEFAULT_MODEL_ID
    return mid


def static_chat_model_entries() -> list[ChatModelEntry]:
    """Built-in non-OpenRouter catalog — OpenRouter models live in MongoDB."""
    return [
        ChatModelEntry(
            id="deepseek-chat",
            label="DeepSeek Chat",
            provider="openai_compat",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            max_context_chars=DEEPSEEK_MAX_CONTEXT_CHARS,
            group="geral",
        ),
        ChatModelEntry(
            id="deepseek-v4-pro",
            label="DeepSeek V4 Pro",
            provider="openai_compat",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            max_context_chars=DEEPSEEK_MAX_CONTEXT_CHARS,
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="deepseek-v4-flash",
            label="DeepSeek V4 Flash",
            provider="openai_compat",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            max_context_chars=DEEPSEEK_MAX_CONTEXT_CHARS,
            group="geral",
        ),
        ChatModelEntry(
            id="gpt-4o-mini",
            label="OpenAI GPT-4o mini",
            provider="openai_compat",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        ChatModelEntry(
            id="openai-codex",
            label="OpenAI Codex (gpt-4o — API)",
            provider="openai_compat",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="codex-cli",
            label="Codex CLI (ChatGPT — programação)",
            provider="codex_cli",
            model="codex",
            base_url="",
            api_key_env="",
            coding=True,
            tools_enabled=False,
            supports_attachments=False,
            max_context_chars=120_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="gemini-3.5-flash",
            label="Google Gemini 3.5 Flash (GA)",
            provider="openai_compat",
            model="gemini-3.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="gemini-2.5-flash",
            label="Google Gemini 2.5 Flash",
            provider="openai_compat",
            model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            group="geral",
        ),
        # Claude via endpoint OpenAI-compatível da Anthropic — habilita o mesmo
        # loop assíncrono de ferramentas/progresso usado por DeepSeek/OpenAI.
        ChatModelEntry(
            id="claude-opus-4-8",
            label="Claude Opus 4.8 (raciocínio máximo)",
            provider="openai_compat",
            model="claude-opus-4-8",
            base_url=ANTHROPIC_OPENAI_COMPAT_BASE_URL,
            api_key_env="ANTHROPIC_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="claude-opus-4-7",
            label="Claude Opus 4.7",
            provider="openai_compat",
            model="claude-opus-4-7",
            base_url=ANTHROPIC_OPENAI_COMPAT_BASE_URL,
            api_key_env="ANTHROPIC_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="claude-sonnet",
            label="Claude Sonnet 4.6 (equilíbrio)",
            provider="openai_compat",
            model="claude-sonnet-4-6",
            base_url=ANTHROPIC_OPENAI_COMPAT_BASE_URL,
            api_key_env="ANTHROPIC_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="claude-haiku-4-5",
            label="Claude Haiku 4.5 (rápido/barato)",
            provider="openai_compat",
            model="claude-haiku-4-5-20251001",
            base_url=ANTHROPIC_OPENAI_COMPAT_BASE_URL,
            api_key_env="ANTHROPIC_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="perplexity-sonar",
            label="Perplexity Sonar",
            provider="openai_compat",
            model="sonar",
            base_url="https://api.perplexity.ai",
            api_key_env="PERPLEXITY_API_KEY",
            tools_enabled=False,
            group="geral",
        ),
        # ── OpenAI ─────────────────────────────────────────────────────────
        ChatModelEntry(
            id="gpt-5.5",
            label="OpenAI GPT-5.5 (mais recente)",
            provider="openai_compat",
            model="gpt-5.5",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="gpt-5",
            label="OpenAI GPT-5",
            provider="openai_compat",
            model="gpt-5",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="gpt-4o",
            label="OpenAI GPT-4o",
            provider="openai_compat",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        ChatModelEntry(
            id="gpt-4-turbo",
            label="OpenAI GPT-4 Turbo",
            provider="openai_compat",
            model="gpt-4-turbo",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        ChatModelEntry(
            id="gpt-4.1",
            label="OpenAI GPT-4.1",
            provider="openai_compat",
            model="gpt-4.1",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="gpt-4.1-mini",
            label="OpenAI GPT-4.1 Mini",
            provider="openai_compat",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        ChatModelEntry(
            id="gpt-4.1-nano",
            label="OpenAI GPT-4.1 Nano (ultra-rápido)",
            provider="openai_compat",
            model="gpt-4.1-nano",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        ChatModelEntry(
            id="o3",
            label="OpenAI o3 (raciocínio)",
            provider="openai_compat",
            model="o3",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="o3-mini",
            label="OpenAI o3-mini",
            provider="openai_compat",
            model="o3-mini",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="o4-mini",
            label="OpenAI o4-mini (raciocínio rápido)",
            provider="openai_compat",
            model="o4-mini",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            coding=True,
            max_context_chars=400_000,
            group="programacao",
        ),
        ChatModelEntry(
            id="gpt-4.5-preview",
            label="OpenAI GPT-4.5 Preview",
            provider="openai_compat",
            model="gpt-4.5-preview",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            max_context_chars=400_000,
            group="geral",
        ),
        # ── Grok (xAI) ────────────────────────────────────────────────────
        ChatModelEntry(
            id="grok-4",
            label="Grok 4 (xAI — mais recente)",
            provider="openai_compat",
            model="grok-4",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="grok-3",
            label="Grok 3 (xAI)",
            provider="openai_compat",
            model="grok-3",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="grok-3-fast",
            label="Grok 3 Fast (xAI)",
            provider="openai_compat",
            model="grok-3-fast",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="grok-3-mini",
            label="Grok 3 Mini (xAI — raciocínio)",
            provider="openai_compat",
            model="grok-3-mini",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="grok-3-mini-fast",
            label="Grok 3 Mini Fast (xAI)",
            provider="openai_compat",
            model="grok-3-mini-fast",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            group="geral",
        ),
        # ── Google Gemini ──────────────────────────────────────────────────
        ChatModelEntry(
            id="gemini-3.1-pro-preview",
            label="Google Gemini 3.1 Pro (preview)",
            provider="openai_compat",
            model="gemini-3.1-pro-preview",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="gemini-3-flash-preview",
            label="Google Gemini 3 Flash (preview)",
            provider="openai_compat",
            model="gemini-3-flash-preview",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="gemini-3.1-flash-lite",
            label="Google Gemini 3.1 Flash Lite (GA)",
            provider="openai_compat",
            model="gemini-3.1-flash-lite",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="gemini-2.5-pro",
            label="Google Gemini 2.5 Pro",
            provider="openai_compat",
            model="gemini-2.5-pro",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="gemini-1.5-pro",
            label="Google Gemini 1.5 Pro",
            provider="openai_compat",
            model="gemini-1.5-pro",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="gemini-1.5-flash",
            label="Google Gemini 1.5 Flash",
            provider="openai_compat",
            model="gemini-1.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            group="geral",
        ),
        # ── Qwen (Alibaba Cloud) ──────────────────────────────────────────
        ChatModelEntry(
            id="qwen-max",
            label="Qwen Max (Alibaba)",
            provider="openai_compat",
            model="qwen-max",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="qwen-plus",
            label="Qwen Plus (Alibaba)",
            provider="openai_compat",
            model="qwen-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="qwen-turbo",
            label="Qwen Turbo (Alibaba — rápido)",
            provider="openai_compat",
            model="qwen-turbo",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            group="geral",
        ),
        ChatModelEntry(
            id="qwen-coder-plus",
            label="Qwen Coder Plus (programação)",
            provider="openai_compat",
            model="qwen-coder-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            coding=True,
            group="programacao",
        ),
        ChatModelEntry(
            id="qwq-plus",
            label="QwQ Plus (raciocínio — Alibaba)",
            provider="openai_compat",
            model="qwq-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            coding=True,
            group="programacao",
        ),
        # ── Yi (01.AI) ────────────────────────────────────────────────────
        ChatModelEntry(
            id="yi-lightning",
            label="Yi Lightning (01.AI)",
            provider="openai_compat",
            model="yi-lightning",
            base_url="https://api.lingyiwanwu.com/v1",
            api_key_env="YI_API_KEY",
            group="geral",
        ),
        # ── Zhipu (GLM) ───────────────────────────────────────────────────
        ChatModelEntry(
            id="glm-4-plus",
            label="GLM-4 Plus (Zhipu AI)",
            provider="openai_compat",
            model="glm-4-plus",
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            api_key_env="ZHIPU_API_KEY",
            group="geral",
        ),
        # ── Moonshot (Kimi) ────────────────────────────────────────────────
        ChatModelEntry(
            id="moonshot-v1-128k",
            label="Moonshot v1 128K (Kimi)",
            provider="openai_compat",
            model="moonshot-v1-128k",
            base_url="https://api.moonshot.cn/v1",
            api_key_env="MOONSHOT_API_KEY",
            group="geral",
        ),
        # ── Baichuan ──────────────────────────────────────────────────────
        ChatModelEntry(
            id="baichuan4",
            label="Baichuan 4",
            provider="openai_compat",
            model="Baichuan4",
            base_url="https://api.baichuan-ai.com/v1/",
            api_key_env="BAICHUAN_API_KEY",
            group="geral",
        ),
    ]


def default_openclaw_chat_models() -> list[ChatModelEntry]:
    """Alias for static entries (tests and env_keys catalog)."""
    return static_chat_model_entries()


def _model_entries(chat_cfg: OpenclawChatConfig) -> list[ChatModelEntry]:
    if chat_cfg.models:
        return list(chat_cfg.models)
    from .llm_models_catalog import list_catalog_entries

    return list_catalog_entries()


def model_entry_available(entry: ChatModelEntry) -> bool:
    """Whether a catalog model can be selected (API key or Codex CLI auth)."""
    if entry.provider == "codex_cli":
        from .codex_cli import codex_cli_available

        return codex_cli_available()
    return bool(resolve_llm_api_key(entry.api_key_env))


def resolve_chat_model(
    chat_cfg: OpenclawChatConfig,
    model_id: Optional[str] = None,
) -> tuple[Optional[ResolvedChatModel], Optional[str]]:
    entries = _model_entries(chat_cfg)
    explicit = bool(model_id and str(model_id).strip())
    if explicit:
        mid = str(model_id).strip()
    else:
        mid = effective_chat_default_model_id(chat_cfg.default_model_id)
    entry = next((e for e in entries if e.id == mid), None)
    if entry is None:
        alias = GEMINI_LEGACY_MODEL_ALIASES.get(mid)
        if alias:
            entry = next((e for e in entries if e.id == alias or e.model == alias), None)
    if entry is None:
        from .openrouter_catalog import legacy_openrouter_model_id

        legacy_model = legacy_openrouter_model_id(mid)
        if legacy_model:
            entry = next((e for e in entries if e.model == legacy_model), None)
    if entry is None:
        # Perfis Hermes guardam o nome real do modelo (ex.: claude-sonnet-4-6),
        # que pode diferir do id do catálogo (claude-sonnet).
        entry = next((e for e in entries if e.model == mid), None)
    if entry is None:
        if explicit:
            known = ", ".join(e.id for e in entries[:8])
            return None, f"modelo desconhecido: {model_id!r} (disponíveis: {known}…)"
        if entries:
            entry = next((e for e in entries if e.id == "deepseek-chat"), entries[0])
            mid = entry.id
        else:
            return None, f"modelo desconhecido: {mid!r}"
    if entry.provider == "codex_cli":
        from .codex_cli import codex_cli_availability_error

        codex_err = codex_cli_availability_error()
        if codex_err:
            return None, codex_err
        return ResolvedChatModel(entry=entry, api_key=""), None
    key = resolve_llm_api_key(entry.api_key_env)
    if not key:
        # Allow usage attempt — the API call itself will fail with a clear error
        key = "MISSING_KEY"
    return ResolvedChatModel(entry=entry, api_key=key), None


_LATEX_AGENT_MODEL_FALLBACKS = (
    "deepseek-chat",
    "gpt-4o-mini",
    "claude-haiku-4-5",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
)


def resolve_latex_agent_model(
    chat_cfg: OpenclawChatConfig,
    model_id: Optional[str] = None,
) -> tuple[Optional[ResolvedChatModel], Optional[str], str]:
    """Pick a model for LaTeX agent steps (template apply, compile repair, …).

    Skips Codex CLI when it is not authenticated and falls back to API models so
    LaTeX operations never block on **Codex: login**.
    """
    cfg = effective_chat_cfg(chat_cfg)
    explicit = str(model_id or "").strip()
    candidates: list[str] = []
    if explicit and explicit != "auto":
        candidates.append(explicit)
    default = effective_chat_default_model_id(cfg.default_model_id)
    if default and default not in candidates and default != "auto":
        candidates.append(default)
    for mid in _LATEX_AGENT_MODEL_FALLBACKS:
        if mid not in candidates:
            candidates.append(mid)

    last_err: Optional[str] = None
    for mid in candidates:
        resolved, err = resolve_chat_model(cfg, mid)
        if resolved is not None:
            return resolved, None, mid
        last_err = err
    return None, last_err or "nenhum modelo de chat disponível para operações LaTeX", explicit


_RETRIABLE_LLM_STATUS_CODES = frozenset({402, 408, 409, 425, 429, 500, 502, 503, 504})

_BILLING_ERROR_TOKENS = (
    "insufficient balance",
    "insufficient_quota",
    "insufficient quota",
    "payment required",
    "credit balance",
    "out of credits",
    "billing",
)

_SWARM_LLM_FALLBACK_PREFERENCES = (
    "deepseek-chat",
    "gpt-4o-mini",
    "claude-haiku-4-5",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
)


def effective_chat_cfg(chat_cfg: Optional[OpenclawChatConfig]) -> OpenclawChatConfig:
    """Return a usable chat config (swarm may run with openclaw_chat disabled)."""
    if chat_cfg is None:
        return OpenclawChatConfig()
    return chat_cfg


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        nxt = cur.__cause__
        if nxt is None and not cur.__suppress_context__:
            nxt = cur.__context__
        cur = nxt
    return out


def llm_error_status_code(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from an LLM client exception."""
    import re

    for item in _iter_exception_chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
        msg = str(item)
        match = re.search(r"error code:\s*(\d{3})", msg, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def is_llm_billing_error(exc: BaseException) -> bool:
    """Payment / quota exhaustion — skip the billing account on fallback."""
    if llm_error_status_code(exc) == 402:
        return True
    for item in _iter_exception_chain(exc):
        msg = str(item).lower()
        if any(token in msg for token in _BILLING_ERROR_TOKENS):
            return True
    return False


def llm_billing_skip_keys(resolved: "ResolvedChatModel") -> set[str]:
    """Identifiers to skip after a billing failure (not whole openai_compat)."""
    keys: set[str] = set()
    env = str(resolved.entry.api_key_env or "").strip()
    if env:
        keys.add(f"env:{env}")
    base = str(resolved.entry.base_url or "").rstrip("/").lower()
    if base:
        keys.add(f"url:{base}")
    provider = str(resolved.entry.provider or "").strip()
    if provider and provider not in ("openai_compat", "auto", ""):
        keys.add(f"provider:{provider}")
    return keys


def is_llm_retriable_error(exc: BaseException) -> bool:
    """Whether an LLM API failure should trigger automatic model fallback."""
    if is_llm_billing_error(exc):
        return True
    for item in _iter_exception_chain(exc):
        status = llm_error_status_code(item)
        if status in _RETRIABLE_LLM_STATUS_CODES:
            return True
        name = type(item).__name__.lower()
        if any(token in name for token in ("timeout", "connection", "rate", "overload")):
            return True
        msg = str(item).lower()
        if any(
            token in msg
            for token in (
                "rate limit",
                "quota",
                "insufficient",
                "timeout",
                "connection",
                "overloaded",
                "temporarily unavailable",
                "service unavailable",
                "too many requests",
            )
        ):
            return True
    return False


def is_llm_retriable_error_text(text: str) -> bool:
    """Parse retriability from a serialized error string (chat return payloads)."""
    raw = str(text or "").strip()
    if not raw:
        return False
    import re

    match = re.search(r"error code:\s*(\d{3})", raw, flags=re.IGNORECASE)
    if match and int(match.group(1)) in _RETRIABLE_LLM_STATUS_CODES:
        return True
    low = raw.lower()
    if any(token in low for token in _BILLING_ERROR_TOKENS):
        return True
    if any(
        token in low
        for token in (
            "rate limit",
            "quota",
            "insufficient",
            "timeout",
            "connection",
            "overloaded",
            "temporarily unavailable",
            "service unavailable",
            "too many requests",
            "apistatuserror",
            "missing credentials",
            "missing api key",
        )
    ):
        return True
    return False


def iter_chat_model_fallback_ids(
    chat_cfg: Optional[OpenclawChatConfig],
    primary_model_id: Optional[str],
) -> list[str]:
    """Ordered unique model ids to try when the primary LLM fails."""
    cfg = effective_chat_cfg(chat_cfg)
    seen: set[str] = set()
    order: list[str] = []

    def add(mid: Optional[str]) -> None:
        mid = str(mid or "").strip()
        if not mid or mid in seen or mid == "auto":
            return
        resolved, _ = resolve_chat_model(cfg, mid)
        if resolved is None:
            return
        if resolved.entry.provider != "codex_cli":
            key = resolve_llm_api_key(resolved.entry.api_key_env)
            if not key:
                return
        seen.add(mid)
        order.append(mid)

    add(primary_model_id)
    add(effective_chat_default_model_id(cfg.default_model_id))
    for mid in _SWARM_LLM_FALLBACK_PREFERENCES:
        add(mid)
    for entry in _model_entries(cfg):
        add(entry.id)
    return order


_VISION_MODEL_FALLBACK_PREFERENCES: tuple[str, ...] = (
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet",
    "claude-haiku-4-5",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gpt-4.1",
    "gpt-4.1-mini",
    "openrouter-gemini-3.5-flash",
    "openrouter-gemini-2.5-flash",
    "openrouter-gpt-4o",
    "openrouter-claude-sonnet-4.6",
    "openrouter-claude-haiku-4.5",
    "openrouter-gpt-5.5",
    "openrouter-grok-4.3",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gpt-4-turbo",
    "gpt-5",
    "gpt-5.5",
    "openai-codex",
    "grok-4",
    "grok-3-fast",
    "grok-3",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "openrouter-claude-opus-4.8",
    "openrouter-gemini-2.5-pro",
    "qwen-max",
    "qwen-plus",
    "gpt-4.5-preview",
)

_VISION_FALLBACK_ERROR_TOKENS = (
    "image_url",
    "unknown variant",
    "expected `text`",
    "multimodal",
    "image input",
    "does not support images",
    "não suporta imagens",
)


def attachments_payload_include_images(attachments: Optional[list[Any]]) -> bool:
    """True when raw chat attachment payloads include at least one image."""
    if not attachments:
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "").lower()
        if mime.startswith("image/"):
            return True
        name = str(item.get("name") or item.get("filename") or "").lower()
        if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif")):
            return True
    return False


def is_llm_vision_fallback_error_text(text: str) -> bool:
    """Errors that warrant trying another vision-capable model."""
    low = str(text or "").lower()
    if not low:
        return False
    if any(token in low for token in _VISION_FALLBACK_ERROR_TOKENS):
        return True
    if "invalid_request_error" in low and "error code: 400" in low:
        return True
    return False


def iter_vision_model_fallback_ids(
    chat_cfg: Optional[OpenclawChatConfig],
    primary_model_id: Optional[str],
    *,
    require_tools: bool = True,
) -> list[str]:
    """Ordered vision-capable model ids to try when images are attached."""
    cfg = effective_chat_cfg(chat_cfg)
    seen: set[str] = set()
    order: list[str] = []

    def add(mid: Optional[str]) -> None:
        mid = str(mid or "").strip()
        if not mid or mid in seen or mid == "auto":
            return
        resolved, _ = resolve_chat_model(cfg, mid)
        if resolved is None or not model_supports_vision(resolved.entry):
            return
        if require_tools and not resolved.entry.tools_enabled:
            return
        if resolved.entry.provider == "codex_cli":
            return
        if not model_entry_available(resolved.entry):
            return
        key = resolve_llm_api_key(resolved.entry.api_key_env)
        if not key:
            return
        seen.add(mid)
        order.append(mid)

    add(primary_model_id)
    for mid in _VISION_MODEL_FALLBACK_PREFERENCES:
        add(mid)
    for mid in sorted(_VISION_MODEL_IDS):
        add(mid)
    for entry in _model_entries(cfg):
        add(entry.id)
    return order


def list_chat_models(chat_cfg: OpenclawChatConfig) -> list[dict[str, Any]]:
    from .llm_models_catalog import ensure_catalog_synced, get_catalog_doc, serialize_catalog_doc
    from .model_router import auto_model_catalog_entry

    ensure_catalog_synced()
    out: list[dict[str, Any]] = [auto_model_catalog_entry()]
    for entry in _model_entries(chat_cfg):
        if entry.provider == "codex_cli":
            available = model_entry_available(entry)
        else:
            key = resolve_llm_api_key(entry.api_key_env)
            available = bool(key)
        doc = get_catalog_doc(entry.id)
        if doc:
            row = serialize_catalog_doc(doc, available=available)
        else:
            row = {
                "id": entry.id,
                "label": entry.label,
                "provider": entry.provider,
                "model": entry.model,
                "api_key_env": entry.api_key_env,
                "available": available,
                "supports_attachments": entry.supports_attachments,
                "tools_enabled": entry.tools_enabled,
                "tools_label": "Sim" if entry.tools_enabled else "Não",
                "coding": entry.coding,
                "group": entry.group,
                "description": "",
                "price_label": "Preço no provider",
            }
        out.append(row)
    return out


def parse_attachments(
    raw: Any,
    *,
    max_count: int,
    max_bytes: int,
    attachments_dir: Optional[Path] = None,
) -> tuple[list[ParsedAttachment], Optional[str]]:
    if not raw:
        return [], None
    if not isinstance(raw, list):
        return [], "attachments must be a list"
    if len(raw) > max_count:
        return [], f"max {max_count} anexo(s) por mensagem"
    parsed: list[ParsedAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], "invalid attachment object"
        name = str(item.get("name") or item.get("filename") or "anexo").strip() or "anexo"
        mime = _normalize_attachment_mime(
            name,
            str(item.get("mime_type") or item.get("mimeType") or "application/octet-stream"),
        )
        path_raw = item.get("path") or item.get("file_path") or item.get("filePath")
        b64 = item.get("data_base64") or item.get("dataBase64") or item.get("content_base64")

        if isinstance(path_raw, str) and path_raw.strip():
            if attachments_dir is None:
                return [], f"attachment {name!r}: path refs require attachments_dir"
            path = Path(path_raw.strip()).expanduser().resolve()
            if not path.is_file():
                return [], f"attachment {name!r}: ficheiro não encontrado: {path}"
            if not _path_under_root(path, attachments_dir):
                return [], f"attachment {name!r}: caminho fora de {attachments_dir}"
            try:
                data = path.read_bytes()
            except OSError as e:
                return [], f"attachment {name!r}: {type(e).__name__}: {e}"
            if len(data) > max_bytes:
                return [], f"attachment {name!r} excede {max_bytes} bytes"
            enc = base64.b64encode(data).decode("ascii")
            data_url = f"data:{mime};base64,{enc}"
            parsed.append(
                ParsedAttachment(
                    name=name,
                    mime_type=mime,
                    data=data,
                    data_url=data_url,
                    path=path,
                )
            )
            continue

        if not isinstance(b64, str) or not b64.strip():
            return [], f"attachment {name!r} missing data_base64 or path"
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            return [], f"attachment {name!r}: base64 inválido"
        if len(data) > max_bytes:
            return [], f"attachment {name!r} excede {max_bytes} bytes"
        enc = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{enc}"
        parsed.append(
            ParsedAttachment(name=name, mime_type=mime, data=data, data_url=data_url)
        )
    return parsed, None


def _is_text_attachment(att: ParsedAttachment) -> bool:
    if att.mime_type.startswith("text/"):
        return True
    mime = att.mime_type.lower().split(";", 1)[0].strip()
    if mime in JSON_MIME_TYPES:
        return True
    lower = att.name.lower()
    return any(lower.endswith(sfx) for sfx in TEXT_MIME_SUFFIXES)


def build_user_content(
    text: str,
    attachments: list[ParsedAttachment],
    *,
    supports_attachments: bool,
) -> Any:
    """OpenAI-style user content (str or multimodal blocks)."""
    path_notice = format_attachment_paths_for_model(attachments)
    if not attachments:
        return text
    if not supports_attachments:
        extra = _attachments_as_text(attachments)
        parts = [p for p in (path_notice, text, extra) if p]
        return "\n\n".join(parts).strip() if parts else text
    blocks: list[dict[str, Any]] = []
    if path_notice:
        blocks.append({"type": "text", "text": path_notice})
    if text.strip():
        blocks.append({"type": "text", "text": text.strip()})
    for att in attachments:
        if att.mime_type.startswith(IMAGE_MIME_PREFIX):
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": att.data_url},
                }
            )
        elif _is_text_attachment(att):
            try:
                body = att.data.decode("utf-8", errors="replace")
            except Exception:
                body = att.data.decode("latin-1", errors="replace")
            loc = f" ({att.path})" if att.path else ""
            blocks.append(
                {
                    "type": "text",
                    "text": f"[Anexo: {att.name}{loc}]\n```\n{body[:12000]}\n```",
                }
            )
        else:
            loc = str(att.path) if att.path else f"{att.name} ({att.mime_type})"
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"[Anexo binário: {att.name} — caminho `{loc}`, "
                        f"{att.mime_type}, {len(att.data)} bytes]"
                    ),
                }
            )
    if not blocks:
        return text
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return str(blocks[0].get("text") or "")
    return blocks


def _attachments_as_text(attachments: list[ParsedAttachment]) -> str:
    parts: list[str] = []
    path_notice = format_attachment_paths_for_model(attachments)
    if path_notice:
        parts.append(path_notice)
    for att in attachments:
        loc = f" em `{att.path}`" if att.path else ""
        if _is_text_attachment(att):
            body = att.data.decode("utf-8", errors="replace")[:12000]
            parts.append(f"[Anexo: {att.name}{loc}]\n```\n{body}\n```")
        else:
            parts.append(
                f"[Anexo: {att.name}{loc} ({att.mime_type}, {len(att.data)} bytes)]"
            )
    return "\n\n".join(parts)


def coding_system_suffix(entry: ChatModelEntry) -> str:
    if not entry.coding:
        return ""
    return (
        "\n\nModo programação: responda como engenheiro de software sénior. "
        "Prefira patches concretos, comandos executáveis e diffs claros."
    )


def build_openai_client(resolved: ResolvedChatModel, *, timeout: float) -> OpenAI:
    from .llm_gateway import make_client

    return make_client(
        api_key=resolved.api_key,
        base_url=resolved.entry.base_url,
        timeout=timeout,
    )


def stream_openai_completion(
    resolved: ResolvedChatModel,
    *,
    system: str,
    messages: list[dict[str, Any]],
    extra_kwargs: Optional[dict[str, Any]] = None,
    temperature: float = 0.2,
    timeout: float = 120.0,
    on_delta: Optional[Callable[[str], None]] = None,
) -> str:
    """Stream an OpenAI-compatible completion, returning the full text.

    ``on_delta(text)`` is invoked for each content chunk as it arrives so callers
    can forward tokens to a UI. Falls back to a single non-streaming call if the
    provider rejects ``stream=True`` (or the streaming response yields nothing).
    """
    client = build_openai_client(resolved, timeout=timeout)
    oai_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for m in messages:
        oai_messages.append({"role": str(m["role"]), "content": str(m["content"])})
    kwargs: dict[str, Any] = {
        "model": resolved.entry.model,
        "messages": oai_messages,
        "temperature": temperature,
        **(extra_kwargs or {}),
    }
    parts: list[str] = []
    try:
        stream = client.chat.completions.create(stream=True, **kwargs)
        for chunk in stream:
            delta = None
            try:
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
            except Exception:
                delta = None
            if delta:
                parts.append(delta)
                if on_delta is not None:
                    try:
                        on_delta(delta)
                    except Exception:
                        pass
        if parts:
            return "".join(parts)
    except Exception:
        pass
    resp = client.chat.completions.create(**kwargs)
    if resp and resp.choices:
        return resp.choices[0].message.content or ""
    return ""


def _is_gemini3_openai_compat(entry: ChatModelEntry) -> bool:
    if "generativelanguage.googleapis.com" not in (entry.base_url or ""):
        return False
    mid = (entry.model or entry.id or "").strip().lower()
    return bool(re.match(r"gemini-3(?:\.\d+)?-(?:flash|pro)", mid))


def completion_extra_kwargs(entry: Optional[ChatModelEntry]) -> dict[str, Any]:
    """Extra kwargs per provider for chat.completions.create.

    O endpoint OpenAI-compat da Anthropic exige ``max_tokens`` explícito —
    sem ele a chamada falha (a Messages API não tem default).

    Gemini 3 Flash/Lite exige thinking mínimo (não dá para desligar); o
    OpenAI-compat aceita ``reasoning_effort: minimal``.
    """
    if entry is None:
        return {}
    if "api.anthropic.com" in (entry.base_url or ""):
        return {"max_tokens": _ANTHROPIC_COMPAT_MAX_TOKENS}
    if _is_gemini3_openai_compat(entry):
        return {"reasoning_effort": "minimal"}
    return {}


def anthropic_messages_create(
    resolved: ResolvedChatModel,
    *,
    system: str,
    messages: list[dict[str, Any]],
    timeout: float,
    max_tokens: int = 8192,
) -> str:
    """Minimal Anthropic Messages API client (httpx).

    A message carrying ``"_cache": True`` is sent as a structured text block with
    ``cache_control={"type": "ephemeral"}`` so Anthropic caches that prefix and
    later turns over the same content reuse it (lower latency and input cost).
    """
    api_messages: list[dict[str, Any]] = []
    used_cache = False
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": block.get("text") or ""})
                elif block.get("type") == "image_url":
                    url = (block.get("image_url") or {}).get("url") or ""
                    if url.startswith("data:") and ";base64," in url:
                        header, b64 = url.split(";base64,", 1)
                        media = header.replace("data:", "", 1) or "image/png"
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media,
                                    "data": b64,
                                },
                            }
                        )
            content = blocks or ""
        if not content:
            continue
        if msg.get("_cache") and isinstance(content, str):
            api_messages.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            )
            used_cache = True
        else:
            api_messages.append({"role": role, "content": content})

    payload = {
        "model": resolved.entry.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": api_messages,
    }
    headers = {
        "x-api-key": resolved.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if used_cache:
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
    url = f"{resolved.entry.base_url.rstrip('/')}/v1/messages"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    parts: list[str] = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def model_supports_tools(resolved: ResolvedChatModel) -> bool:
    if not resolved.entry.tools_enabled:
        return False
    if resolved.entry.provider in ("anthropic", "codex_cli"):
        return False
    return True


_VISION_MODEL_IDS = frozenset(
    {
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.5-preview",
        "gpt-5",
        "gpt-5.5",
        "openai-codex",
        "grok-3",
        "grok-3-fast",
        "grok-4",
        "qwen-max",
        "qwen-plus",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet",
        "claude-haiku-4-5",
        "openrouter-claude-sonnet-4.6",
        "openrouter-claude-opus-4.8",
        "openrouter-claude-haiku-4.5",
        "openrouter-gpt-5.5",
        "openrouter-gpt-4o",
        "openrouter-gemini-2.5-pro",
        "openrouter-gemini-2.5-flash",
        "openrouter-gemini-3.5-flash",
        "openrouter-grok-4.3",
    }
)


def model_supports_vision(entry: ChatModelEntry) -> bool:
    if not entry.supports_attachments:
        return False
    return entry.id in _VISION_MODEL_IDS


_VISION_MODEL_FALLBACK_PREFERENCES: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet",
    "claude-haiku-4-5",
    "gpt-4.1",
    "gpt-4.1-mini",
    "openrouter-gemini-2.5-flash",
    "openrouter-gemini-2.5-pro",
    "openrouter-gpt-4o",
    "openrouter-claude-sonnet-4.6",
    "openrouter-claude-haiku-4.5",
    "openrouter-grok-4.3",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
)

_VISION_FALLBACK_ERROR_TOKENS = (
    "image_url",
    "unknown variant",
    "expected `text`",
    "multimodal",
    "image input",
    "does not support images",
    "não suporta imagens",
)


def attachments_payload_include_images(attachments: Optional[list[Any]]) -> bool:
    """True when raw chat attachment payloads include at least one image."""
    if not attachments:
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "").lower()
        if mime.startswith("image/"):
            return True
        name = str(item.get("name") or item.get("filename") or "").lower()
        if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif")):
            return True
    return False


def is_llm_vision_fallback_error_text(text: str) -> bool:
    """Errors that warrant trying another vision-capable model."""
    low = str(text or "").lower()
    if not low:
        return False
    if any(token in low for token in _VISION_FALLBACK_ERROR_TOKENS):
        return True
    if "invalid_request_error" in low and "error code: 400" in low:
        return True
    return False


def iter_vision_model_fallback_ids(
    chat_cfg: Optional[OpenclawChatConfig],
    primary_model_id: Optional[str],
    *,
    require_tools: bool = True,
) -> list[str]:
    """Ordered vision-capable model ids to try when images are attached."""
    cfg = effective_chat_cfg(chat_cfg)
    seen: set[str] = set()
    order: list[str] = []

    def add(mid: Optional[str]) -> None:
        mid = str(mid or "").strip()
        if not mid or mid in seen or mid == "auto":
            return
        resolved, _ = resolve_chat_model(cfg, mid)
        if resolved is None or not model_supports_vision(resolved.entry):
            return
        if require_tools and not resolved.entry.tools_enabled:
            return
        if resolved.entry.provider == "codex_cli":
            return
        if not model_entry_available(resolved.entry):
            return
        key = resolve_llm_api_key(resolved.entry.api_key_env)
        if not key:
            return
        seen.add(mid)
        order.append(mid)

    add(primary_model_id)
    for mid in _VISION_MODEL_FALLBACK_PREFERENCES:
        add(mid)
    for mid in sorted(_VISION_MODEL_IDS):
        add(mid)
    for entry in _model_entries(cfg):
        add(entry.id)
    return order


def attachments_include_images(attachments: list[ParsedAttachment]) -> bool:
    return any(att.mime_type.startswith(IMAGE_MIME_PREFIX) for att in attachments)


def resolve_vision_model_for_attachments(
    chat_cfg: OpenclawChatConfig,
    resolved: ResolvedChatModel,
    parsed_attachments: list[ParsedAttachment],
    *,
    require_tools: bool = True,
) -> tuple[ResolvedChatModel, Optional[str]]:
    """Pick a vision-capable model when the user attached images."""
    if not attachments_include_images(parsed_attachments):
        return resolved, None
    if model_supports_vision(resolved.entry):
        return resolved, None

    chain = iter_vision_model_fallback_ids(
        chat_cfg,
        resolved.entry.id,
        require_tools=require_tools,
    )
    for mid in chain:
        if mid == resolved.entry.id:
            continue
        upgraded, err = resolve_chat_model(chat_cfg, mid)
        if upgraded and not err and model_supports_vision(upgraded.entry):
            return upgraded, upgraded.entry.label
    return resolved, None


def strip_image_blocks_from_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace ``image_url`` parts with a short text placeholder (non-vision models)."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        blocks: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                blocks.append(
                    {
                        "type": "text",
                        "text": "[imagem omitida — modelo sem visão]",
                    }
                )
            else:
                blocks.append(part)
        out.append({**msg, "content": blocks})
    return out

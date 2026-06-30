"""Register LaTeX article folders, compile PDFs, and serve previews."""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from .latex_tex_edit import _page_count_for_tex
except ImportError:
    _page_count_for_tex = None  # type: ignore[assignment]

from gois.local_paths import project_stack_root

_REGISTRY_NAME = "latex_workspaces.json"
_CROSSREF_CACHE_DIR = Path(os.getenv("XDG_CACHE_HOME", "~/.cache")).expanduser() / "qclaw_crossref"
_CROSSREF_CACHE_TTL_SECONDS = 86400  # 24 hours
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass\b")
_LATEX_INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")
_LATEX_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_LATEX_BIB_RE = re.compile(r"\\(?:bibliography|addbibresource)(?:\[[^\]]*\])?\{([^}]+)\}")
_MAX_SCAN_DEPTH = 6
_SKIP_TREE_DIRS = frozenset({
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
})
_FAST_LIST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FAST_LIST_TTL_SECONDS = 300.0


def invalidate_fast_list_cache(workspace_id: str = "") -> None:
    """Drop cached fast article listings after writes."""
    if workspace_id:
        _FAST_LIST_CACHE.pop(workspace_id, None)
    else:
        _FAST_LIST_CACHE.clear()
_UPLOAD_MAX_FILE_BYTES = 15_000_000
_UPLOAD_MAX_TOTAL_BYTES = 80_000_000
_UPLOAD_ALLOWED_SUFFIXES = frozenset(
    {
        ".tex",
        ".bib",
        ".bst",
        ".cls",
        ".sty",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".eps",
        ".svg",
        ".yaml",
        ".yml",
        ".json",
        ".csv",
        ".md",
        ".txt",
        ".bbx",
        ".cbx",
        ".def",
        ".cfg",
        ".clo",
        ".fd",
        ".tfm",
        ".map",
        ".ins",
        ".dtx",
        ".latexmkrc",
    }
)


def _extract_cite_keys(content: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\\cite[a-zA-Z*]*\s*\{([^}]+)\}", content or "", flags=re.IGNORECASE):
        for part in m.group(1).split(","):
            key = str(part or "").strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _citation_key_query(key: str) -> str:
    text = str(key or "").strip()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _crossref_bib_entry_from_item(key: str, item: dict[str, Any]) -> str:
    title = ""
    title_list = item.get("title") or []
    if isinstance(title_list, list) and title_list:
        title = str(title_list[0] or "").strip()

    authors: list[str] = []
    for row in item.get("author") or []:
        if not isinstance(row, dict):
            continue
        given = str(row.get("given") or "").strip()
        family = str(row.get("family") or "").strip()
        name = f"{given} {family}".strip()
        if name:
            authors.append(name)

    year = ""
    for field in ("published-print", "published-online", "issued", "published"):
        blob = item.get(field)
        if isinstance(blob, dict):
            parts = blob.get("date-parts") or []
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                year = str(parts[0][0])
                break

    doi = str(item.get("DOI") or "").strip()
    journal = ""
    container = item.get("container-title") or []
    if isinstance(container, list) and container:
        journal = str(container[0] or "").strip()

    entry_type = str(item.get("type") or "").lower()
    bib_type = "article" if "journal" in entry_type or "article" in entry_type else "misc"

    fields: list[str] = []
    if authors:
        fields.append(f"  author = {{{' and '.join(authors)}}}")
    if title:
        fields.append(f"  title = {{{title}}}")
    if journal:
        fields.append(f"  journal = {{{journal}}}")
    if year:
        fields.append(f"  year = {{{year}}}")
    if doi:
        fields.append(f"  doi = {{{doi}}}")
        fields.append(f"  url = {{https://doi.org/{doi}}}")
    fields.append("  note = {auto-generated from Crossref}")
    return f"@{bib_type}{{{key},\n" + ",\n".join(fields) + "\n}\n"


@functools.lru_cache(maxsize=256)
def _query_crossref_cached(query: str) -> Optional[dict[str, Any]]:
    """Cached Crossref API query. Returns first match or None.

    Caches responses in ~/.cache/qclaw_crossref with 24h TTL to avoid repeated
    API calls for same citations across requests.
    """
    if httpx is None or not query.strip():
        return None

    # Check file cache first
    cache_key = hashlib.sha256(query.encode()).hexdigest()
    cache_file = _CROSSREF_CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists():
        try:
            mtime = cache_file.stat().st_mtime
            age_seconds = time.time() - mtime
            if age_seconds < _CROSSREF_CACHE_TTL_SECONDS:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                return cached.get("data")
        except Exception:
            pass  # Cache miss on any error

    headers = {"User-Agent": "qclaw-latex-auto-bib/1.0 (mailto:qclaw@localhost)"}
    try:
        with httpx.Client(timeout=8.0, headers=headers) as client:
            resp = client.get(
                "https://api.crossref.org/works",
                params={"query": query, "rows": 1},
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            items = (((payload or {}).get("message") or {}).get("items") or [])
            result = items[0] if items else None

            # Store in cache for next request
            if result is not None:
                try:
                    _CROSSREF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(
                        json.dumps({"data": result}, default=str),
                        encoding="utf-8"
                    )
                except Exception:
                    pass  # Cache write failure is non-fatal

            return result
    except Exception:
        return None


def _placeholder_bib_entry(key: str) -> str:
    safe_key = str(key or "").strip()
    label = re.sub(r"\s+", " ", safe_key).strip() or "undefined-citation"
    label = label.replace("{", "").replace("}", "")
    # natbib author-year styles run \ifnum on the year, so it MUST be numeric.
    # Most citation keys embed the year (e.g. "smith2024foo"); fall back to a
    # neutral numeric year when none is present.
    year_match = re.search(r"(19|20)\d{2}", safe_key)
    year = year_match.group(0) if year_match else "2024"
    return (
        f"@misc{{{safe_key},\n"
        "  author = {Unknown},\n"
        f"  title = {{{label}}},\n"
        f"  year = {{{year}}},\n"
        "  note = {auto-generated placeholder from unresolved citation key}\n"
        "}\n"
    )


def _attempt_auto_bib_repair(*, tex: Path, diagnostics: dict[str, Any]) -> dict[str, Any]:
    undefined = diagnostics.get("undefined_citations") or []
    cite_keys = [str(k).strip() for k in undefined if str(k).strip()]

    try:
        content = tex.read_text(encoding="utf-8")
    except OSError as exc:
        return {"attempted": True, "applied": False, "error": str(exc)}

    # Always include every \cite key from the source. natbib year errors (from a
    # malformed bib) can suppress the "undefined" warnings, so the diagnostics
    # list may be empty even though the bibliography needs regeneration.
    for key in _extract_cite_keys(content):
        if key not in cite_keys:
            cite_keys.append(key)

    if not cite_keys:
        return {"attempted": False, "applied": False, "reason": "no_citation_keys"}

    crossref_rows: list[str] = []
    resolved_keys: set[str] = set()
    found = 0
    tried = 0

    for key in cite_keys[:40]:
        query = _citation_key_query(key)
        if not query:
            continue
        tried += 1
        item = _query_crossref_cached(query)
        if item is not None:
            crossref_rows.append(_crossref_bib_entry_from_item(key, item))
            resolved_keys.add(key)
            found += 1

    placeholder_keys = [key for key in cite_keys if key not in resolved_keys]
    placeholder_rows = [_placeholder_bib_entry(key) for key in placeholder_keys]

    if not crossref_rows and not placeholder_rows:
        return {
            "attempted": True,
            "applied": False,
            "reason": "no_bibliography_entries",
            "tried": tried,
            "found": found,
        }

    bib_rows = crossref_rows + placeholder_rows
    bib_path = tex.parent / "references_auto.bib"
    try:
        lines = [
            "% Auto-generated bibliography for unresolved citations",
            "% Sources: Crossref (when available) + placeholder entries",
            "",
        ]
        if crossref_rows:
            lines.extend(["% Crossref matches", ""])
            lines.extend(crossref_rows)
            lines.append("")
        if placeholder_rows:
            lines.extend(["% Placeholder entries (replace with real references)", ""])
            lines.extend(placeholder_rows)
        bib_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return {"attempted": True, "applied": False, "error": str(exc)}

    updated = content
    changed_tex = False
    has_bib = bool(re.search(r"\\bibliography\s*\{[^}]+\}", updated, flags=re.IGNORECASE))
    has_addbib = bool(re.search(r"\\addbibresource\s*\{[^}]+\}", updated, flags=re.IGNORECASE))

    if not has_bib and not has_addbib:
        updated = re.sub(
            r"\\end\{document\}",
            lambda _m: "\\bibliographystyle{plainnat}\n\\bibliography{references_auto}\n\\end{document}",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
        changed_tex = updated != content
    elif has_bib and "references_auto" not in updated:
        updated = re.sub(
            r"(\\bibliography\s*\{)([^}]+)(\})",
            lambda m: f"{m.group(1)}{m.group(2)},references_auto{m.group(3)}",
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
        changed_tex = updated != content

    if changed_tex:
        try:
            shutil.copy2(tex, tex.with_suffix(".tex.bak"))
            tex.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return {"attempted": True, "applied": False, "error": str(exc)}

    return {
        "attempted": True,
        "applied": True,
        "bib_file": str(bib_path),
        "entries": found,
        "placeholder_entries": len(placeholder_rows),
        "total_entries": len(bib_rows),
        "tried": tried,
        "tex_changed": changed_tex,
    }


_VERBATIM_ENVS = frozenset({"verbatim", "Verbatim", "lstlisting", "comment", "minted"})


def _find_unclosed_environments(content: str) -> list[str]:
    """Return a stack of environment names opened but not closed (LIFO order).

    Ignores the outer ``document`` environment and comment lines.
    """
    text = _latex_without_comments(content or "")
    stack: list[str] = []
    token = re.compile(r"\\(begin|end)\s*\{([^}]+)\}")
    for m in token.finditer(text):
        kind, name = m.group(1), m.group(2).strip()
        if name == "document":
            continue
        if kind == "begin":
            stack.append(name)
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    break
    return stack


def _document_is_incomplete(content: str) -> dict[str, Any]:
    """Detect a truncated/incomplete main .tex (missing \\end{document}, open envs)."""
    text = content or ""
    has_begin = bool(re.search(r"\\begin\s*\{document\}", text, flags=re.IGNORECASE))
    has_end = bool(re.search(r"\\end\s*\{document\}", text, flags=re.IGNORECASE))
    open_envs = _find_unclosed_environments(text) if has_begin else []
    incomplete = bool(has_begin and (not has_end or open_envs))
    return {
        "incomplete": incomplete,
        "has_begin_document": has_begin,
        "has_end_document": has_end,
        "open_environments": open_envs,
    }


_LLM_COMPLETE_SYSTEM_PROMPT = """Você é um agente LaTeX que COMPLETA um documento .tex truncado.
Recebe o conteúdo atual (que termina abruptamente) e deve gerar APENAS a CONTINUAÇÃO que falta
para o documento ficar completo e compilável.

Regras:
- NÃO repita o conteúdo já existente. Gere somente o texto novo que continua de onde parou.
- Se a última frase/seção estiver incompleta, conclua-a de forma coerente com o tema.
- Feche todos os ambientes abertos (\\end{...}) na ordem correta.
- Se houver citações (\\cite) e nenhuma bibliografia incluída, adicione antes de \\end{document}:
  \\bibliographystyle{plainnat} e \\bibliography{references_auto}
- Termine com \\end{document}.
- NÃO invente dados numéricos novos nem referências bibliográficas falsas.
- Mantenha o idioma do documento (português).
- Responda APENAS com JSON (sem cercas markdown):
  {"continuation": "<texto LaTeX que completa o documento>", "message": "resumo em português"}
"""


def _deterministic_document_closing(content: str, *, add_bibliography: bool) -> str:
    """Build the minimal LaTeX needed to close a truncated document."""
    parts: list[str] = []
    open_envs = _find_unclosed_environments(content)
    for name in reversed(open_envs):
        parts.append(f"\\end{{{name}}}")
    if add_bibliography:
        parts.append("\\bibliographystyle{plainnat}")
        parts.append("\\bibliography{references_auto}")
    parts.append("\\end{document}")
    return ("\n" if content and not content.endswith("\n") else "") + "\n".join(parts) + "\n"


def _ensure_references_auto_bib(*, tex: Path, content: str) -> Optional[Path]:
    """Create or backfill references_auto.bib with entries for all \\cite keys.

    Existing real entries are preserved; only missing keys get placeholder
    entries so BibTeX can resolve every citation. Returns the bib path or None.
    """
    cite_keys = _extract_cite_keys(content)
    if not cite_keys:
        return None
    bib_path = tex.parent / "references_auto.bib"
    existing = ""
    try:
        if bib_path.is_file():
            existing = bib_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    present = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)", existing))
    missing = [k for k in cite_keys if k not in present]
    if bib_path.is_file() and not missing:
        return bib_path
    rows: list[str] = []
    if existing.strip():
        rows.append(existing.rstrip())
        rows.append("")
    else:
        rows.append("% Auto-generated bibliography for unresolved citations")
        rows.append("% Placeholder entries (replace with real references)")
        rows.append("")
    rows.extend(_placeholder_bib_entry(k) for k in missing)
    try:
        bib_path.write_text("\n".join(rows), encoding="utf-8")
    except OSError:
        return None
    return bib_path


def _attempt_complete_document(
    *,
    tex: Path,
    root: Path,
    chat_cfg: Any = None,
    model_id: Optional[str] = None,
) -> dict[str, Any]:
    """Complete a truncated main .tex using an LLM, with a deterministic fallback.

    When the document lacks \\end{document} or has unclosed environments, asks the
    model to generate ONLY the missing continuation (finishing the text, closing
    environments, adding bibliography). Falls back to a deterministic closing so
    the document always compiles even if the model is unavailable.
    """
    try:
        content = tex.read_text(encoding="utf-8")
    except OSError as exc:
        return {"attempted": True, "applied": False, "error": str(exc)}

    info = _document_is_incomplete(content)
    if not info["incomplete"]:
        return {"attempted": False, "applied": False, "reason": "document_complete"}

    has_cite = bool(_extract_cite_keys(content))
    has_bib = bool(
        re.search(r"\\bibliography\s*\{[^}]+\}", content, flags=re.IGNORECASE)
        or re.search(r"\\addbibresource\s*\{[^}]+\}", content, flags=re.IGNORECASE)
        or re.search(r"\\printbibliography", content, flags=re.IGNORECASE)
        or re.search(r"\\begin\s*\{thebibliography\}", content, flags=re.IGNORECASE)
    )
    add_bibliography = has_cite and not has_bib

    # Ensure a usable references_auto.bib exists for all citation keys so that the
    # added \bibliography{references_auto} actually resolves on recompile.
    if add_bibliography:
        _ensure_references_auto_bib(tex=tex, content=content)

    continuation = ""
    source = "deterministic"
    model_used: Optional[str] = None
    message: Optional[str] = None

    payload = {
        "main_tex_name": tex.name,
        "open_environments": info["open_environments"],
        "has_end_document": info["has_end_document"],
        "needs_bibliography": add_bibliography,
        "citation_count": len(_extract_cite_keys(content)),
        "truncated_tail": content[-6000:],
    }
    try:
        call = _latex_llm_json_call(
            system=_LLM_COMPLETE_SYSTEM_PROMPT,
            user_payload=json.dumps(payload, ensure_ascii=False),
            chat_cfg=chat_cfg,
            model_id=model_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        call = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if call.get("ok"):
        data = call.get("data") or {}
        cand = data.get("continuation")
        if isinstance(cand, str) and cand.strip():
            continuation = cand.rstrip()
            source = "llm"
            model_used = call.get("model_id")
            message = data.get("message")

    if source == "llm":
        # Guarantee the document is actually closed even if the model forgot.
        if not re.search(r"\\end\s*\{document\}", continuation, flags=re.IGNORECASE):
            continuation += _deterministic_document_closing(
                content + "\n" + continuation, add_bibliography=False
            )
        sep = "" if content.endswith("\n") else "\n"
        new_content = content + sep + continuation + ("\n" if not continuation.endswith("\n") else "")
    else:
        new_content = content + _deterministic_document_closing(
            content, add_bibliography=add_bibliography
        )

    if new_content == content:
        return {"attempted": True, "applied": False, "reason": "no_change"}

    try:
        shutil.copy2(tex, tex.with_suffix(".tex.bak"))
        tex.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return {"attempted": True, "applied": False, "error": str(exc)}

    return {
        "attempted": True,
        "applied": True,
        "source": source,
        "model_id": model_used,
        "message": message,
        "added_bibliography": add_bibliography,
        "closed_environments": info["open_environments"],
        "added_end_document": not info["has_end_document"],
    }


_STUB_MARKERS = (
    "criado automaticamente",
    "auto-generated",
    "adicione suas referências aqui",
    "Arquivo incluso criado automaticamente",
)
_LLM_FILE_KINDS = (".bib", ".tex", ".sty", ".cls")


def _file_is_stub_or_empty(path: Path) -> bool:
    """True when a file is missing, empty, or only contains an auto-stub marker."""
    try:
        if not path.is_file():
            return True
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    body = text.strip()
    if not body:
        return True
    if body == "\\relax":
        return True
    meaningful = [
        ln
        for ln in body.splitlines()
        if ln.strip() and not ln.lstrip().startswith("%") and ln.strip() != "\\relax"
    ]
    if not meaningful:
        return True
    return any(marker in text for marker in _STUB_MARKERS)


def _latex_llm_json_call(
    *,
    system: str,
    user_payload: str,
    chat_cfg: Any,
    model_id: Optional[str],
) -> dict[str, Any]:
    """Single JSON-returning LLM call reusing the chat-model infrastructure."""
    from .chat_models import (
        anthropic_messages_create,
        build_openai_client,
        completion_extra_kwargs,
        effective_chat_cfg,
        resolve_latex_agent_model,
    )
    from .config import OpenclawChatConfig
    from .latex_tex_chat import _parse_llm_json
    from .model_router import resolve_effective_model_id

    cfg = chat_cfg if chat_cfg is not None else OpenclawChatConfig()
    cfg = effective_chat_cfg(cfg)
    effective_model_id = str(model_id or "").strip() or None
    try:
        effective_model_id, _route = resolve_effective_model_id(
            cfg,
            effective_model_id,
            message=user_payload[:2000],
            role="researcher",
            title="latex_fill_missing",
            require_tools=False,
        )
    except Exception:
        pass
    resolved, err, _used = resolve_latex_agent_model(cfg, effective_model_id)
    if err or resolved is None:
        return {"ok": False, "error": err or "modelo de chat indisponível"}

    messages = [{"role": "user", "content": user_payload}]
    try:
        if resolved.entry.provider == "anthropic":
            reply = anthropic_messages_create(
                resolved, system=system, messages=messages, timeout=120.0
            )
        else:
            client = build_openai_client(resolved, timeout=120.0)
            oai_messages = [{"role": "system", "content": system}] + messages
            kwargs: dict[str, Any] = {
                "model": resolved.entry.model,
                "messages": oai_messages,
                "temperature": 0.2,
                **completion_extra_kwargs(resolved.entry),
            }
            try:
                kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
            except Exception:
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            reply = ""
            if resp and resp.choices:
                reply = resp.choices[0].message.content or ""
        parsed = _parse_llm_json(reply)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "data": parsed, "model_id": resolved.entry.id}


_LLM_FILL_SYSTEM_PROMPT = """Você é um agente LaTeX que GERA o conteúdo real de arquivos auxiliares ausentes de um artigo.
Recebe o .tex principal e a lista de arquivos faltantes (stubs vazios) que precisam de conteúdo real.

Regras:
- Para cada arquivo .bib: gere entradas BibTeX válidas e completas para TODAS as chaves de citação usadas
  (campos author, title, journal/booktitle, year, etc.). Use referências plausíveis e bem formatadas;
  não deixe campos obrigatórios vazios.
- Para cada arquivo .tex (\\input/\\include): gere conteúdo LaTeX coerente com o artigo (sem \\documentclass,
  sem \\begin{document}); apenas o corpo da seção/trecho referenciado.
- Para .sty/.cls ausentes: gere uma implementação mínima funcional compatível com os comandos usados.
- NÃO altere o arquivo principal; apenas produza o conteúdo dos arquivos auxiliares.
- Mantenha o idioma do documento.
- Responda APENAS com um objeto JSON (sem cercas markdown):
  {"files": {"caminho/relativo.bib": "conteúdo completo", "...": "..."}, "message": "resumo em português"}
"""


def _attempt_llm_fill_missing_files(
    *,
    tex: Path,
    root: Path,
    diagnostics: dict[str, Any],
    stubs_created: Optional[list[str]],
    chat_cfg: Any = None,
    model_id: Optional[str] = None,
) -> dict[str, Any]:
    """Use an LLM to generate real content for missing/stub auxiliary files.

    Targets the .bib / \\input .tex stubs created during compilation and any
    missing .sty/.cls reported by diagnostics, then writes the generated
    content so the next compile pass can succeed.
    """
    base_dir = tex.parent.resolve(strict=False)
    targets: list[str] = []
    seen: set[str] = set()

    def _add_target(rel: str) -> None:
        rel = (rel or "").strip().replace("\\", "/")
        if not rel or rel in seen:
            return
        if Path(rel).suffix.lower() not in _LLM_FILE_KINDS:
            return
        candidate = (base_dir / rel).resolve(strict=False)
        if not _is_under(candidate, root):
            return
        if _file_is_stub_or_empty(candidate):
            seen.add(rel)
            targets.append(rel)

    for rel in stubs_created or []:
        _add_target(rel)
    if isinstance(diagnostics, dict):
        for rel in diagnostics.get("missing_files") or []:
            _add_target(str(rel))

    if not targets:
        return {"attempted": False, "applied": False, "reason": "no_fillable_files"}

    try:
        tex_content = tex.read_text(encoding="utf-8")
    except OSError as exc:
        return {"attempted": True, "applied": False, "error": str(exc)}

    cite_keys = _extract_cite_keys(tex_content)
    payload = {
        "main_tex_name": tex.name,
        "missing_files": targets,
        "citation_keys": cite_keys,
        "main_tex": tex_content[:60000],
    }
    call = _latex_llm_json_call(
        system=_LLM_FILL_SYSTEM_PROMPT,
        user_payload=json.dumps(payload, ensure_ascii=False),
        chat_cfg=chat_cfg,
        model_id=model_id,
    )
    if not call.get("ok"):
        return {"attempted": True, "applied": False, "error": call.get("error")}

    files = (call.get("data") or {}).get("files") or {}
    if not isinstance(files, dict) or not files:
        return {
            "attempted": True,
            "applied": False,
            "reason": "model_returned_no_files",
        }

    written: list[str] = []
    rejected: list[str] = []
    for rel, body in files.items():
        rel_norm = str(rel or "").strip().replace("\\", "/")
        if not rel_norm or not isinstance(body, str) or not body.strip():
            continue
        if Path(rel_norm).suffix.lower() not in _LLM_FILE_KINDS:
            continue
        target = (base_dir / rel_norm).resolve(strict=False)
        if not _is_under(target, root):
            rejected.append(rel_norm)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            written.append(rel_norm)
        except OSError:
            rejected.append(rel_norm)

    return {
        "attempted": True,
        "applied": bool(written),
        "written": written,
        "rejected": rejected or None,
        "requested": targets,
        "model_id": call.get("model_id"),
        "message": (call.get("data") or {}).get("message"),
    }


def _registry_path() -> Path:
    root = project_stack_root() / "latex"
    root.mkdir(parents=True, exist_ok=True)
    return root / _REGISTRY_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LatexWorkspace:
    id: str
    name: str
    path: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _workspace_name_key(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", text)


def _resolve_workspace_path(raw: str) -> Optional[Path]:
    folder = Path(raw).expanduser()
    try:
        return folder.resolve(strict=False)
    except OSError:
        return None


def dedupe_workspaces(*, save: bool = True) -> dict[str, Any]:
    """Collapse LaTeX workspaces that point to the same folder or share a name."""
    workspaces = _load_registry()
    if not workspaces:
        return {"ok": True, "removed": [], "kept": 0}

    kept: list[LatexWorkspace] = []
    removed: list[dict[str, str]] = []
    path_index: dict[str, int] = {}
    name_index: dict[str, int] = {}

    for ws in sorted(workspaces, key=lambda w: w.created_at):
        resolved = _resolve_workspace_path(ws.path)
        path_key = str(resolved) if resolved is not None else ws.path.strip()
        name_key = _workspace_name_key(ws.name)

        drop_id: Optional[str] = None
        drop_reason = ""
        if path_key and path_key in path_index:
            drop_id = ws.id
            drop_reason = "duplicate_path"
            kept_idx = path_index[path_key]
        elif name_key and name_key in name_index:
            drop_id = ws.id
            drop_reason = "duplicate_name"
            kept_idx = name_index[name_key]
        else:
            kept_idx = None

        if drop_id is not None and kept_idx is not None:
            removed.append(
                {"id": drop_id, "reason": drop_reason, "kept": kept[kept_idx].id}
            )
            continue

        idx = len(kept)
        kept.append(ws)
        if path_key:
            path_index[path_key] = idx
        if name_key:
            name_index[name_key] = idx

    if save and len(kept) != len(workspaces):
        _save_registry(kept)
    return {"ok": True, "removed": removed, "kept": len(kept)}


def _load_registry() -> list[LatexWorkspace]:
    path = _registry_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    rows = raw.get("workspaces") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[LatexWorkspace] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        wid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        wpath = str(row.get("path") or "").strip()
        created = str(row.get("created_at") or _now_iso())
        if wid and name and wpath:
            out.append(LatexWorkspace(id=wid, name=name, path=wpath, created_at=created))
    return out


def _save_registry(workspaces: list[LatexWorkspace]) -> None:
    path = _registry_path()
    payload = {"workspaces": [w.to_dict() for w in workspaces]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def list_workspaces() -> list[dict[str, str]]:
    dedupe_workspaces(save=True)
    rows: list[dict[str, str]] = []
    for ws in _load_registry():
        resolved = Path(ws.path).expanduser()
        rows.append(
            {
                **ws.to_dict(),
                "exists": resolved.is_dir(),
                "resolved_path": str(resolved.resolve(strict=False)) if resolved.exists() else ws.path,
            }
        )
    return rows


def _team_latex_candidate_dirs(team_info: dict[str, Any]) -> list[tuple[str, Path]]:
    """Return labeled folders to scan for team LaTeX projects."""
    team_id = str(team_info.get("id") or "").strip()
    team_name = str(team_info.get("name") or team_id or "Time").strip()
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(label: str, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        folder = Path(raw).expanduser()
        try:
            key = str(folder.resolve(strict=False))
        except OSError:
            key = str(folder)
        if key in seen or not folder.is_dir():
            return
        seen.add(key)
        out.append((label, folder))

    add(team_name, str(team_info.get("local_path") or ""))
    add(f"{team_name} artifacts", str(team_info.get("artifacts_dir") or ""))

    if team_id:
        from .team_articles_folder_ops import articles_folder_root

        art_root = articles_folder_root(team_id)
        add(f"{team_name} artigos", str(art_root))
        try:
            for sub in sorted(art_root.iterdir(), key=lambda p: p.name.lower()):
                if sub.is_dir() and not sub.name.startswith("."):
                    add(sub.name, str(sub))
        except OSError:
            pass
        stack_team = project_stack_root() / "accounts" / "teams" / team_id
        add(f"{team_name} workspace", str(stack_team / "workspace" / "artifacts"))
        add(f"{team_name} repo", str(stack_team / "repo"))

    return out


def discover_team_latex_workspaces(team_info: dict[str, Any]) -> list[str]:
    """Register team folders that contain LaTeX and return their workspace ids."""
    ids: list[str] = []
    for label, folder in _team_latex_candidate_dirs(team_info):
        if not _find_main_tex_files(folder):
            continue
        reg = register_workspace(name=label, path=str(folder))
        if not reg.get("ok"):
            continue
        ws = reg.get("workspace") or {}
        wid = str(ws.get("id") or "").strip()
        if wid and wid not in ids:
            ids.append(wid)
    return ids


def _workspace_has_main_tex(ws: dict[str, str]) -> bool:
    """True when a registered workspace folder contains at least one main .tex."""
    raw = str(ws.get("resolved_path") or ws.get("path") or "").strip()
    if not raw:
        return False
    folder = _resolve_workspace_path(raw)
    if folder is None or not folder.is_dir():
        return False
    return bool(_find_main_tex_files(folder))


def list_workspaces_for_team(team_info: Optional[dict[str, Any]] = None) -> list[dict[str, str]]:
    """Return LaTeX workspaces scoped to the active team when team context exists."""
    active = [w for w in list_workspaces() if w.get("exists") is not False]
    if not team_info:
        return active

    from .accounts import latex_workspace_ids_from_row

    linked = latex_workspace_ids_from_row(team_info)
    if linked:
        id_set = set(linked)
        scoped = [w for w in active if str(w.get("id") or "") in id_set]
        usable = [w for w in scoped if _workspace_has_main_tex(w)]
        if usable:
            return usable

    discovered = discover_team_latex_workspaces(team_info)
    if not discovered:
        return []

    id_set = set(discovered)
    return [
        w
        for w in list_workspaces()
        if str(w.get("id") or "") in id_set and w.get("exists") is not False
    ]


def register_workspace(*, name: str, path: str) -> dict[str, Any]:
    label = (name or "").strip()
    raw = (path or "").strip()
    if not label:
        return {"ok": False, "error": "informe um nome para a pasta"}
    if not raw:
        return {"ok": False, "error": "informe o caminho da pasta"}
    folder = Path(raw).expanduser()
    try:
        folder = folder.resolve(strict=False)
    except OSError as e:
        return {"ok": False, "error": f"caminho inválido: {e}"}
    if not folder.is_dir():
        return {"ok": False, "error": f"pasta não encontrada: {folder}"}

    workspaces = _load_registry()
    folder_s = str(folder)
    name_key = _workspace_name_key(label)
    for ws in workspaces:
        if Path(ws.path).expanduser().resolve(strict=False) == folder:
            return {
                "ok": True,
                "workspace": ws.to_dict(),
                "reused": True,
                "reason": "duplicate_path",
            }
        if name_key and _workspace_name_key(ws.name) == name_key:
            return {
                "ok": True,
                "workspace": ws.to_dict(),
                "reused": True,
                "reason": "duplicate_name",
            }

    entry = LatexWorkspace(
        id=uuid.uuid4().hex[:12],
        name=label,
        path=folder_s,
        created_at=_now_iso(),
    )
    workspaces.append(entry)
    _save_registry(workspaces)
    return {"ok": True, "workspace": entry.to_dict()}


def remove_workspace(workspace_id: str) -> dict[str, Any]:
    wid = (workspace_id or "").strip()
    if not wid:
        return {"ok": False, "error": "workspace_id obrigatório"}
    workspaces = _load_registry()
    kept = [w for w in workspaces if w.id != wid]
    if len(kept) == len(workspaces):
        return {"ok": False, "error": "pasta não encontrada"}
    _save_registry(kept)
    return {"ok": True, "removed": wid}


def _workspace_by_id(workspace_id: str) -> Optional[LatexWorkspace]:
    wid = (workspace_id or "").strip()
    if not wid:
        return None
    for ws in _load_registry():
        if ws.id == wid:
            return ws
    return None


def _workspace_ref_key(ref: str) -> str:
    """Normalized key for matching workspace names, slugs, and folder basenames."""
    text = str(ref or "").strip().lower().replace("-", " ").replace("_", " ")
    return _workspace_name_key(text)


def _workspace_by_ref(ref: str) -> Optional[LatexWorkspace]:
    """Resolve a workspace by id, display name, slug, or folder basename."""
    raw = (ref or "").strip()
    if not raw:
        return None
    ws = _workspace_by_id(raw)
    if ws is not None:
        return ws
    ref_key = _workspace_ref_key(raw)
    if not ref_key:
        return None
    matches: list[LatexWorkspace] = []
    lower = raw.lower()
    for candidate in _load_registry():
        if candidate.name.lower() == lower or candidate.id.lower() == lower:
            return candidate
        if _workspace_ref_key(candidate.name) == ref_key:
            matches.append(candidate)
            continue
        resolved = _resolve_workspace_path(candidate.path)
        if resolved is not None and _workspace_ref_key(resolved.name) == ref_key:
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        for candidate in matches:
            if candidate.name.lower() == lower:
                return candidate
        return matches[0]
    return None


def _resolve_workspace_for_article(
    workspace_ref: str,
    article_id: str = "",
) -> tuple[Optional[LatexWorkspace], Optional[str]]:
    ws = _workspace_by_ref(workspace_ref)
    if ws is not None:
        return ws, None
    aid = (article_id or "").strip().replace("\\", "/").lstrip("/")
    if not aid:
        return None, "pasta não cadastrada"
    ref_key = _workspace_ref_key(workspace_ref)
    candidates: list[LatexWorkspace] = []
    for candidate in _load_registry():
        _root, tex, _err = _resolve_article_tex_for(candidate, aid)
        if tex is None:
            continue
        if ref_key and _workspace_ref_key(candidate.name) == ref_key:
            return candidate, None
        candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0], None
    return None, "pasta não cadastrada"


def _resolve_workspace_root(workspace_id: str) -> tuple[Optional[Path], Optional[str]]:
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return None, "pasta não cadastrada"
    root = Path(ws.path).expanduser()
    try:
        root = root.resolve(strict=False)
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    if not root.is_dir():
        return None, f"pasta inexistente: {root}"
    return root, None


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _article_id_for(tex: Path, root: Path) -> str:
    rel = tex.relative_to(root)
    return rel.as_posix()


def _article_backup_path(tex: Path) -> Path:
    """Backup criado antes de gravar (.tex → .tex.bak)."""
    return tex.with_suffix(".tex.bak")


def _article_version_fields(tex: Path, root: Path) -> dict[str, Any]:
    aid = _article_id_for(tex, root)
    fields: dict[str, Any] = {
        "relative_tex": aid,
        "latest_file": aid,
        "tex_mtime": tex.stat().st_mtime,
        "has_backup": False,
    }
    backup = _article_backup_path(tex)
    if not backup.is_file():
        return fields
    try:
        rel_backup = backup.relative_to(root).as_posix()
    except ValueError:
        rel_backup = backup.name
    fields["has_backup"] = True
    fields["backup_path"] = str(backup)
    fields["relative_backup"] = rel_backup
    fields["backup_mtime"] = backup.stat().st_mtime
    return fields


def article_picker_label(
    art: dict[str, Any],
    *,
    prefix: str = "📄 ",
    ws_name: str = "",
    include_backup: bool = False,
) -> str:
    """Rótulo curto para seletores (editor, perguntas interativas).

    Por omissão só mostra o ``.tex`` editável. Backups entram no rótulo
    apenas quando ``include_backup=True`` (ex.: utilizador referenciou ``.bak``).
    """
    title = str(art.get("title") or art.get("id") or "artigo")
    latest = str(art.get("relative_tex") or art.get("id") or "")
    label = f"{prefix}{title}"
    if ws_name:
        label += f" ({ws_name})"
    ver_lines = [f"atual: {latest}"]
    if include_backup and art.get("has_backup"):
        ver_lines.append(f"backup: {art.get('relative_backup') or '…'}")
    return label + "\n" + "\n".join(ver_lines)


def _is_overly_broad_workspace_root(root: Path) -> bool:
    """True when the workspace points at a whole volume or home directory."""
    try:
        resolved = root.resolve(strict=False)
    except OSError:
        return False
    if resolved == Path("/"):
        return True
    parts = resolved.parts
    if len(parts) == 3 and parts[0] == "/" and parts[1].lower() == "volumes":
        return True
    try:
        if resolved == Path.home().resolve(strict=False):
            return True
    except OSError:
        pass
    return False


def _scan_main_tex_files(root: Path) -> list[Path]:
    found: list[Path] = []

    def walk(base: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_dir():
                if child.name in _SKIP_TREE_DIRS:
                    continue
                walk(child, depth + 1)
            elif child.suffix.lower() == ".tex":
                try:
                    head = child.read_text(encoding="utf-8", errors="ignore")[:8000]
                except OSError:
                    continue
                if _DOCUMENTCLASS_RE.search(head):
                    found.append(child)

    walk(root, 0)
    return sorted(found, key=lambda p: str(p).lower())


@functools.lru_cache(maxsize=128)
def _find_main_tex_files_cached(root_str: str) -> tuple[str, ...]:
    """Cached version working with resolved path string."""
    root = Path(root_str)
    return tuple(p.as_posix() for p in _scan_main_tex_files(root))


def _find_main_tex_files(root: Path) -> list[Path]:
    root = root.resolve(strict=False)
    cached = _find_main_tex_files_cached(str(root))
    return [Path(p) for p in cached]


def list_articles(workspace_id: str, fast_mode: bool = False) -> dict[str, Any]:
    if fast_mode:
        now = time.time()
        cached = _FAST_LIST_CACHE.get(workspace_id)
        if cached and now - cached[0] < _FAST_LIST_TTL_SECONDS:
            return cached[1]

    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    assert ws is not None
    articles: list[dict[str, Any]] = []

    for tex in _find_main_tex_files(root):
        aid = _article_id_for(tex, root)
        pdf = tex.with_suffix(".pdf")
        version = _article_version_fields(tex, root)

        # Optimized: single stat() call instead of is_file() + stat()
        pdf_mtime = None
        has_pdf = False
        try:
            pdf_mtime = pdf.stat().st_mtime
            has_pdf = True
        except OSError:
            pass

        entry = {
            "id": aid,
            "title": tex.stem,
            "tex_path": str(tex),
            "pdf_path": str(pdf) if has_pdf else None,
            "has_pdf": has_pdf,
            "pdf_mtime": pdf_mtime,
            **version,
        }
        if not fast_mode:
            from .latex_tex_edit import extract_objectives
            article_objective = ""
            section_objective_count = 0
            try:
                objectives = extract_objectives(tex.read_text(encoding="utf-8"))
                article_objective = str(objectives.get("article_objective") or "").strip()
                section_objective_count = len(objectives.get("section_objectives") or [])
            except OSError:
                article_objective = ""
                section_objective_count = 0
            entry["article_objective"] = article_objective
            entry["section_objective_count"] = section_objective_count
        articles.append(entry)
    result = {
        "ok": True,
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "root": str(root),
        "articles": articles,
        "count": len(articles),
        "broad_root": _is_overly_broad_workspace_root(root),
    }
    if fast_mode:
        _FAST_LIST_CACHE[workspace_id] = (time.time(), result)
    return result


def _find_all_tex_files(root: Path) -> list[Path]:
    """Return every ``.tex`` file under ``root`` (recursively), main or not."""
    found: list[Path] = []
    root = root.resolve(strict=False)

    def walk(base: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_dir():
                if child.name in _SKIP_TREE_DIRS:
                    continue
                walk(child, depth + 1)
            elif child.suffix.lower() == ".tex":
                found.append(child)

    walk(root, 0)
    return sorted(found, key=lambda p: str(p).lower())


def _find_all_bib_files(root: Path) -> list[Path]:
    """Return every ``.bib`` file under ``root`` (recursively)."""
    found: list[Path] = []
    root = root.resolve(strict=False)

    def walk(base: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_dir():
                if child.name in _SKIP_TREE_DIRS:
                    continue
                walk(child, depth + 1)
            elif child.suffix.lower() == ".bib":
                found.append(child)

    walk(root, 0)
    return sorted(found, key=lambda p: str(p).lower())


def list_workspace_bib_files(workspace_id: str) -> dict[str, Any]:
    """List every ``.bib`` file in a workspace so the user can view/edit it."""
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    files: list[dict[str, Any]] = []
    for bib in _find_all_bib_files(root):
        rel = bib.relative_to(root).as_posix()
        try:
            mtime = bib.stat().st_mtime
            size = bib.stat().st_size
        except OSError:
            mtime = None
            size = None
        entries = 0
        try:
            entries = len(re.findall(r"^[ \t]*@", bib.read_text(encoding="utf-8", errors="ignore"), flags=re.MULTILINE))
        except OSError:
            entries = 0
        files.append(
            {
                "path": rel,
                "name": bib.name,
                "dir": bib.parent.relative_to(root).as_posix() if bib.parent != root else "",
                "bib_path": str(bib),
                "entries": entries,
                "size": size,
                "mtime": mtime,
            }
        )
    return {
        "ok": True,
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "root": str(root),
        "files": files,
        "count": len(files),
    }


def list_workspace_tex_files(workspace_id: str) -> dict[str, Any]:
    """List every ``.tex`` file in a workspace so the user can pick one to open.

    Unlike :func:`list_articles` (which only returns ``\\documentclass`` main
    files), this returns all ``.tex`` files, flagging which ones are main
    documents via ``is_main``.
    """
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    files: list[dict[str, Any]] = []
    for tex in _find_all_tex_files(root):
        aid = _article_id_for(tex, root)
        try:
            head = tex.read_text(encoding="utf-8", errors="ignore")[:8000]
            is_main = bool(_DOCUMENTCLASS_RE.search(head))
        except OSError:
            is_main = False
        try:
            mtime = tex.stat().st_mtime
        except OSError:
            mtime = None
        files.append(
            {
                "id": aid,
                "name": tex.name,
                "dir": tex.parent.relative_to(root).as_posix() if tex.parent != root else "",
                "tex_path": str(tex),
                "is_main": is_main,
                "mtime": mtime,
            }
        )
    return {
        "ok": True,
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "root": str(root),
        "files": files,
        "count": len(files),
    }


_EDITABLE_EXTS = frozenset(
    {
        ".tex",
        ".bib",
        ".sty",
        ".cls",
        ".md",
        ".txt",
        ".rst",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".ts",
        ".tsx",
        ".py",
        ".sh",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
        ".aux",
        ".out",
    }
)


def _normalize_rel_path(rel: str) -> Optional[str]:
    cleaned = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        return ""
    if ".." in Path(cleaned).parts:
        return None
    return cleaned


def list_workspace_files(workspace_id: str, rel_path: str = "") -> dict[str, Any]:
    """List files and folders under a LaTeX workspace (one directory level)."""
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        return {"ok": False, "error": "caminho inválido"}
    target = (root / norm).resolve(strict=False) if norm else root.resolve(strict=False)
    if not _is_under(target, root):
        return {"ok": False, "error": "caminho fora da pasta"}
    if not target.is_dir():
        return {"ok": False, "error": "não é uma pasta"}
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    for child in children:
        name = child.name
        if child.is_dir():
            if name.startswith(".") or name in _SKIP_TREE_DIRS:
                continue
        elif name.startswith("."):
            continue
        try:
            rel = child.relative_to(root).as_posix()
        except ValueError:
            continue
        kind = "dir" if child.is_dir() else "file"
        entry: dict[str, Any] = {"name": name, "path": rel, "kind": kind}
        if kind == "file":
            ext = child.suffix.lower()
            entry["ext"] = ext
            entry["editable"] = ext in _EDITABLE_EXTS
            try:
                entry["size"] = child.stat().st_size
            except OSError:
                pass
        entries.append(entry)
    return {
        "ok": True,
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "root": str(root),
        "path": norm,
        "entries": entries,
        "count": len(entries),
    }


def _resolve_workspace_editable_file(workspace_id: str, rel_path: str) -> tuple[Optional[LatexWorkspace], Optional[Path], Optional[str]]:
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return None, None, err or "erro"
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return None, None, "pasta não cadastrada"
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        return None, None, "caminho inválido"
    if not norm:
        return None, None, "path é obrigatório"
    target = (root / norm).resolve(strict=False)
    if not _is_under(target, root):
        return None, None, "caminho fora da pasta"
    if not target.is_file():
        return None, None, "arquivo não encontrado"
    if target.suffix.lower() not in _EDITABLE_EXTS:
        return None, None, "arquivo não suportado para edição"
    return ws, target, None


def get_workspace_file(workspace_id: str, rel_path: str) -> dict[str, Any]:
    ws, target, err = _resolve_workspace_editable_file(workspace_id, rel_path)
    if ws is None or target is None:
        return {"ok": False, "error": err or "erro"}
    root, _ = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return {"ok": False, "error": "caminho fora da pasta"}
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "workspace_id": ws.id,
        "path": rel,
        "ext": target.suffix.lower(),
        "content": content,
        "length": len(content),
        "file_path": str(target),
    }


def save_workspace_file(
    workspace_id: str,
    rel_path: str,
    content: str,
    *,
    backup: bool = True,
) -> dict[str, Any]:
    ws, target, err = _resolve_workspace_editable_file(workspace_id, rel_path)
    if ws is None or target is None:
        return {"ok": False, "error": err or "erro"}
    root, _ = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return {"ok": False, "error": "caminho fora da pasta"}
    response: dict[str, Any] = {
        "ok": True,
        "workspace_id": ws.id,
        "path": rel,
        "file_path": str(target),
    }
    if backup:
        backup_path = target.with_suffix(f"{target.suffix}.bak")
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            return {"ok": False, "error": f"backup failed: {exc}"}
        response["backup_path"] = str(backup_path)
    try:
        target.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    response["bytes_written"] = len(str(content).encode("utf-8"))
    return response


_ARTICLE_DELETE_SUFFIXES = (
    ".tex",
    ".tex.bak",
    ".pdf",
    ".aux",
    ".log",
    ".out",
    ".bbl",
    ".blg",
    ".toc",
    ".lof",
    ".lot",
    ".fls",
    ".fdb_latexmk",
    ".synctex.gz",
    ".nav",
    ".snm",
    ".vrb",
    ".bcf",
    ".run.xml",
)


def delete_article(workspace_id: str, article_id: str) -> dict[str, Any]:
    """Delete a LaTeX article (main .tex and build artifacts in the same folder)."""
    root, tex, err = _resolve_article_tex(workspace_id, article_id)
    if tex is None:
        aid = (article_id or "").strip().replace("\\", "/").lstrip("/")
        if aid and not aid.lower().endswith(".tex"):
            root, tex, err = _resolve_article_tex(workspace_id, f"{aid}.tex")
    if tex is None or root is None:
        return {"ok": False, "error": err or "artigo não encontrado"}

    ws = _workspace_by_ref(workspace_id)
    resolved_id = _article_id_for(tex, root)
    parent = tex.parent
    stem = tex.stem
    candidates: set[Path] = {tex, parent / f"{tex.name}.bak"}
    for suffix in _ARTICLE_DELETE_SUFFIXES:
        if suffix in {".tex", ".tex.bak"}:
            continue
        candidates.add(parent / f"{stem}{suffix}")

    deleted: list[str] = []
    for path in sorted(candidates):
        if not path.is_file() or not _is_under(path, root):
            continue
        try:
            rel = path.relative_to(root).as_posix()
            path.unlink()
            deleted.append(rel)
        except OSError:
            continue

    if not deleted:
        return {"ok": False, "error": "nenhum arquivo encontrado para apagar"}

    _find_main_tex_files_cached.cache_clear()
    invalidate_fast_list_cache(workspace_id)

    return {
        "ok": True,
        "workspace_id": ws.id if ws else workspace_id,
        "article_id": resolved_id,
        "deleted": deleted,
        "message": "Artigo removido.",
    }


def delete_workspace_file(workspace_id: str, rel_path: str) -> dict[str, Any]:
    """Delete a single file inside a LaTeX workspace (not directories)."""
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        return {"ok": False, "error": "caminho inválido"}
    if not norm:
        return {"ok": False, "error": "path é obrigatório"}
    target = (root / norm).resolve(strict=False)
    if not _is_under(target, root):
        return {"ok": False, "error": "caminho fora da pasta"}
    if target.is_dir():
        return {"ok": False, "error": "só é possível apagar arquivos (não pastas)"}
    if not target.is_file():
        return {"ok": False, "error": "arquivo não encontrado"}
    try:
        target.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "workspace_id": ws.id,
        "path": norm,
        "deleted": norm,
    }


def upload_workspace_files(workspace_id: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    """Write uploaded files into a LaTeX workspace root."""
    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "erro"}
    ws = _workspace_by_ref(workspace_id)
    if ws is None:
        return {"ok": False, "error": "pasta não cadastrada"}
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "informe ao menos um arquivo"}

    written: list[str] = []
    total_bytes = 0
    for entry in files[:500]:
        if not isinstance(entry, dict):
            continue
        rel = _normalize_rel_path(
            str(
                entry.get("relative_path")
                or entry.get("path")
                or entry.get("rel_path")
                or entry.get("name")
                or ""
            )
        )
        if not rel:
            continue
        suffix = Path(rel).suffix.lower()
        if suffix and suffix not in _UPLOAD_ALLOWED_SUFFIXES:
            continue
        raw_b64 = str(entry.get("data_base64") or "").strip()
        if not raw_b64:
            continue
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            return {"ok": False, "error": f"data_base64 inválido em {rel}: {exc}"}
        if len(raw) > _UPLOAD_MAX_FILE_BYTES:
            return {"ok": False, "error": f"arquivo muito grande: {rel}"}
        total_bytes += len(raw)
        if total_bytes > _UPLOAD_MAX_TOTAL_BYTES:
            return {"ok": False, "error": "upload excede o tamanho máximo permitido"}
        target = (root / rel).resolve(strict=False)
        if not _is_under(target, root):
            return {"ok": False, "error": f"caminho fora da pasta: {rel}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(raw)
        except OSError as exc:
            return {"ok": False, "error": f"write failed for {rel}: {exc}"}
        written.append(rel)

    if not written:
        return {"ok": False, "error": "nenhum arquivo válido para upload"}

    return {
        "ok": True,
        "workspace_id": ws.id,
        "root": str(root),
        "files_written": written,
        "count": len(written),
    }


def _resolve_article_tex_for(
    ws: LatexWorkspace,
    article_id: str,
) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
    root = Path(ws.path).expanduser()
    try:
        root = root.resolve(strict=False)
    except OSError as e:
        return None, None, f"{type(e).__name__}: {e}"
    if not root.is_dir():
        return None, None, f"pasta inexistente: {root}"
    aid = (article_id or "").strip().replace("\\", "/").lstrip("/")
    if not aid or ".." in Path(aid).parts:
        return None, None, "artigo inválido"
    tex = (root / aid).resolve(strict=False)
    if not _is_under(tex, root):
        return None, None, "artigo fora da pasta cadastrada"
    if not tex.is_file() or tex.suffix.lower() != ".tex":
        return None, None, "arquivo .tex não encontrado"
    return root, tex, None


def _resolve_article_tex(workspace_id: str, article_id: str) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
    ws, err = _resolve_workspace_for_article(workspace_id, article_id)
    if ws is None:
        return None, None, err
    return _resolve_article_tex_for(ws, article_id)


def _latex_without_comments(content: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in (content or "").splitlines())


def _latex_ref_resolved(root: Path, base_dir: Path, ref: str, *, suffixes: tuple[str, ...] = ()) -> bool:
    text = str(ref or "").strip().strip("{}").replace("\\", "/")
    if not text or text.startswith(("http://", "https://", "data:")):
        return True
    if any(token in text for token in ("*", "?")):
        return True
    raw = Path(text)
    candidates: list[Path] = [raw]
    if not raw.suffix:
        candidates.extend(Path(f"{text}{ext}") for ext in suffixes)
    
    # Check direct candidates
    for candidate in candidates:
        target = (base_dir / candidate).resolve(strict=False)
        if _is_under(target, root) and target.is_file():
            return True
    
    # For .bib files, also search in common subdirectories
    if suffixes == (".bib",):
        common_dirs = ["bib", "bibs", "references", "refs", "bibliography", "bibliographies"]
        for dirname in common_dirs:
            for candidate in candidates:
                alt_target = (base_dir / dirname / candidate.name).resolve(strict=False)
                if _is_under(alt_target, root) and alt_target.is_file():
                    return True
    
    return False


def _create_missing_file_stubs(tex: Path, root: Path) -> dict[str, Any]:
    """Create minimal stub files for missing .bib, .tex inputs, and graphics so
    compilation can proceed.  Returns info about what was created.

    Optimized: batch mkdir operations to reduce syscalls.
    """
    try:
        content = tex.read_text(encoding="utf-8")
    except OSError:
        return {"created": [], "skipped": []}

    stripped = _latex_without_comments(content)
    base_dir = tex.parent.resolve(strict=False)
    created: list[str] = []
    skipped: list[str] = []

    def _resolve_target(ref: str, suffixes: tuple[str, ...]) -> Optional[Path]:
        text = ref.strip().strip("{}").replace("\\", "/")
        if not text or text.startswith(("http://", "https://", "data:")):
            return None
        raw = Path(text)
        candidates: list[Path] = [raw]
        if not raw.suffix:
            candidates.extend(Path(f"{text}{ext}") for ext in suffixes)

        # Check direct candidates first
        for candidate in candidates:
            target = (base_dir / candidate).resolve(strict=False)
            if _is_under(target, root) and target.is_file():
                return None  # already exists

        # For .bib files, also search in common subdirectories
        if suffixes == (".bib",):
            common_dirs = ["bib", "bibs", "references", "refs", "bibliography", "bibliographies"]
            for dirname in common_dirs:
                for candidate in candidates:
                    alt_target = (base_dir / dirname / candidate.name).resolve(strict=False)
                    if _is_under(alt_target, root) and alt_target.is_file():
                        return None  # found in alt location

        # Pick the canonical target (add first suffix if missing)
        target = (base_dir / (raw if raw.suffix else Path(f"{text}{suffixes[0]}"))).resolve(strict=False)
        if not _is_under(target, root):
            return None
        return target

    # Collect files to create (batched by directory)
    files_to_create: dict[Path, tuple[str, str]] = {}  # dir -> (file_path, content)
    dirs_to_create: set[Path] = set()

    # Collect missing .bib files
    bib_content = """% Arquivo .bib criado automaticamente — adicione suas referências aqui
% Exemplo de entrada BibTeX:
% @article{exemplo2024,
%   author = {Autor, Primeiro and Coautor, Segundo},
%   title = {Título do Artigo},
%   journal = {Nome do Periódico},
%   year = {2024},
%   volume = {1},
%   pages = {1--10}
% }
"""
    for match in _LATEX_BIB_RE.finditer(stripped):
        for ref in (part.strip() for part in match.group(1).split(",")):
            target = _resolve_target(ref, (".bib",))
            if target is None:
                continue
            files_to_create[target] = (str(target.relative_to(root)), bib_content)
            dirs_to_create.add(target.parent)

    # Collect missing .tex files
    tex_content = "% Arquivo incluso criado automaticamente\n\\relax\n"
    for match in _LATEX_INPUT_RE.finditer(stripped):
        ref = match.group(1).strip()
        target = _resolve_target(ref, (".tex",))
        if target is None:
            continue
        files_to_create[target] = (str(target.relative_to(root)), tex_content)
        dirs_to_create.add(target.parent)

    # Batch mkdir: create all directories first
    for dir_path in dirs_to_create:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # Then write all files
    for file_path, (rel_path, content) in files_to_create.items():
        try:
            file_path.write_text(content, encoding="utf-8")
            created.append(rel_path)
        except OSError:
            skipped.append(rel_path)

    return {"created": created, "skipped": skipped}


def _collect_missing_latex_files(tex: Path, root: Path) -> list[str]:
    try:
        content = tex.read_text(encoding="utf-8")
    except OSError:
        return []
    content = _latex_without_comments(content)
    base_dir = tex.parent.resolve(strict=False)
    missing: list[str] = []
    seen: set[str] = set()

    def add(ref: str) -> None:
        ref = ref.strip().strip("{}").replace("\\", "/")
        if not ref or ref in seen:
            return
        seen.add(ref)
        missing.append(ref)

    for match in _LATEX_INPUT_RE.finditer(content):
        ref = match.group(1).strip()
        if ref and not _latex_ref_resolved(root, base_dir, ref, suffixes=(".tex",)):
            add(ref)

    for match in _LATEX_GRAPHICS_RE.finditer(content):
        ref = match.group(1).strip()
        if ref and not _latex_ref_resolved(
            root,
            base_dir,
            ref,
            suffixes=(".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".eps", ".svg"),
        ):
            add(ref)

    for match in _LATEX_BIB_RE.finditer(content):
        for ref in (part.strip() for part in match.group(1).split(",")):
            if ref and not _latex_ref_resolved(root, base_dir, ref, suffixes=(".bib",)):
                add(ref)

    return missing


def compile_article(
    workspace_id: str,
    article_id: str,
    *,
    repair_on_failure: bool = False,
    chat_cfg: Any = None,
    model_id: Optional[str] = None,
) -> dict[str, Any]:
    _root, tex, err = _resolve_article_tex(workspace_id, article_id)
    if tex is None:
        return {"ok": False, "error": err or "erro"}
    assert _root is not None
    ws = _workspace_by_ref(workspace_id)
    resolved_wid = ws.id if ws is not None else workspace_id

    # Create stub files for missing .bib / \input references so the compiler
    # can at least start and produce a real log.  Graphics stubs are skipped —
    # a missing image is a warning, not a fatal error.
    stubs_info = _create_missing_file_stubs(tex, _root)

    engine = _pick_engine()
    if engine is None:
        return {
            "ok": False,
            "error": "nenhum compilador LaTeX encontrado (instale latexmk ou pdflatex)",
        }

    tex_deps = ensure_texlive_dependencies(template_id="", auto_install=True)
    if tex_deps.get("warning"):
        stubs_info["tex_deps_warning"] = tex_deps["warning"]

    cwd = tex.parent

    def run_engine() -> tuple[subprocess.CompletedProcess[str], str, dict[str, Any]]:
        log_chunks: list[str] = []
        try:
            tex_source = tex.read_text(encoding="utf-8", errors="replace")
        except OSError:
            tex_source = ""
        bib_backend = _bib_backend_for(tex_source)
        if engine[0] == "latexmk":
            cmd = [
                engine[1],
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
            ]
            # Force bibtex/biber so citations resolve (latexmk auto-detects biber
            # for biblatex documents; -bibtex enables the legacy bibtex pass).
            cmd.append("-bibtex")
            cmd.append(tex.name)
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            log_chunks.append(proc.stdout or "")
            log_chunks.append(proc.stderr or "")
        else:
            # When a bibliography backend is needed, the first pdflatex pass MUST
            # NOT halt on error: undefined citations make natbib raise non-fatal
            # \ifnum errors that, under -halt-on-error, abort the run before the
            # .aux is fully written — so bibtex never sees all \citation entries
            # and citations can never resolve. Run nonstopmode without halting so
            # the .aux completes; the final pass determines success.
            base_cmd = [engine[1], "-interaction=nonstopmode"]
            cmd = base_cmd + (["-halt-on-error"] if not bib_backend else []) + [tex.name]

            def _pdflatex() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    check=False,
                )

            proc = _pdflatex()
            log_chunks.append(proc.stdout or "")
            log_chunks.append(proc.stderr or "")
            if bib_backend:
                bib_proc, bib_log = _run_bib_backend(bib_backend, tex, cwd)
                if bib_log:
                    log_chunks.append(f"\n=== {bib_backend} ===\n{bib_log}")
                # Two more passes so citations/cross-refs resolve.
                proc = _pdflatex()
                log_chunks.append(proc.stdout or "")
                log_chunks.append(proc.stderr or "")
                proc = _pdflatex()
                log_chunks.append(proc.stdout or "")
                log_chunks.append(proc.stderr or "")
            elif proc.returncode == 0:
                proc = _pdflatex()
                log_chunks.append(proc.stdout or "")
                log_chunks.append(proc.stderr or "")
        tail = "\n".join(log_chunks)[-12000:]
        return proc, tail, _extract_compile_diagnostics(tail)

    try:
        proc, tail, diagnostics = run_engine()
        pdf = tex.with_suffix(".pdf")
        ok = proc.returncode == 0 and pdf.is_file()
        if ok:
            result: dict[str, Any] = {
                "ok": True,
                "pdf_path": str(pdf),
                "pdf_url": f"/latex/pdf?workspace_id={resolved_wid}&article_id={_article_id_for(tex, _root)}",
                "engine": engine[0],
                "log_tail": tail,
            }
            if stubs_info.get("created"):
                result["stubs_created"] = stubs_info["created"]
            if diagnostics.get("summary"):
                result["diagnostics"] = diagnostics
            if _page_count_for_tex is not None:
                try:
                    pages, _ = _page_count_for_tex(tex)
                    if pages is not None:
                        result["page_count"] = pages
                except Exception:
                    pass
            return result

        repair_info: Optional[dict[str, Any]] = None
        auto_bib_info: Optional[dict[str, Any]] = None
        complete_info: Optional[dict[str, Any]] = None

        # Root-cause first: a truncated/incomplete main .tex (missing
        # \end{document} or unclosed environments) must be completed before any
        # bibliography/citation repair can take effect.
        if repair_on_failure:
            try:
                doc_state = _document_is_incomplete(tex.read_text(encoding="utf-8"))
            except OSError:
                doc_state = {"incomplete": False}
            if doc_state.get("incomplete"):
                complete_info = _attempt_complete_document(
                    tex=tex,
                    root=_root,
                    chat_cfg=chat_cfg,
                    model_id=model_id,
                )
                if complete_info.get("applied"):
                    proc, tail, diagnostics = run_engine()
                    pdf = tex.with_suffix(".pdf")
                    ok = proc.returncode == 0 and pdf.is_file()
                    if ok:
                        result = {
                            "ok": True,
                            "pdf_path": str(pdf),
                            "pdf_url": f"/latex/pdf?workspace_id={resolved_wid}&article_id={_article_id_for(tex, _root)}",
                            "engine": engine[0],
                            "log_tail": tail,
                            "repair": {
                                "attempted": True,
                                "applied": True,
                                "mode": "llm_complete_document",
                                "complete": complete_info,
                            },
                        }
                        if diagnostics.get("summary"):
                            result["diagnostics"] = diagnostics
                        if _page_count_for_tex is not None:
                            try:
                                pages, _ = _page_count_for_tex(tex)
                                if pages is not None:
                                    result["page_count"] = pages
                            except Exception:
                                pass
                        return result

        bib_trigger = bool(
            isinstance(diagnostics, dict)
            and (
                diagnostics.get("undefined_citations")
                or diagnostics.get("natbib_year_errors")
            )
        )
        if repair_on_failure and bib_trigger:
            auto_bib_info = _attempt_auto_bib_repair(tex=tex, diagnostics=diagnostics)
            if auto_bib_info.get("applied"):
                proc, tail, diagnostics = run_engine()
                pdf = tex.with_suffix(".pdf")
                ok = proc.returncode == 0 and pdf.is_file()
                if ok:
                    result = {
                        "ok": True,
                        "pdf_path": str(pdf),
                        "pdf_url": f"/latex/pdf?workspace_id={resolved_wid}&article_id={_article_id_for(tex, _root)}",
                        "engine": engine[0],
                        "log_tail": tail,
                        "repair": {
                            "attempted": True,
                            "applied": True,
                            "mode": "auto_bib_crossref",
                            "bib": auto_bib_info,
                        },
                    }
                    if diagnostics.get("summary"):
                        result["diagnostics"] = diagnostics
                    if _page_count_for_tex is not None:
                        try:
                            pages, _ = _page_count_for_tex(tex)
                            if pages is not None:
                                result["page_count"] = pages
                        except Exception:
                            pass
                    return result

        llm_fill_info: Optional[dict[str, Any]] = None
        if repair_on_failure:
            needs_files = bool(stubs_info.get("created")) or bool(
                isinstance(diagnostics, dict)
                and (diagnostics.get("missing_files") or diagnostics.get("undefined_citations"))
            )
            if needs_files:
                try:
                    llm_fill_info = _attempt_llm_fill_missing_files(
                        tex=tex,
                        root=_root,
                        diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
                        stubs_created=stubs_info.get("created") or [],
                        chat_cfg=chat_cfg,
                        model_id=model_id,
                    )
                except Exception as exc:
                    llm_fill_info = {
                        "attempted": True,
                        "applied": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if llm_fill_info.get("applied"):
                    proc, tail, diagnostics = run_engine()
                    pdf = tex.with_suffix(".pdf")
                    ok = proc.returncode == 0 and pdf.is_file()
                    if ok:
                        result = {
                            "ok": True,
                            "pdf_path": str(pdf),
                            "pdf_url": f"/latex/pdf?workspace_id={resolved_wid}&article_id={_article_id_for(tex, _root)}",
                            "engine": engine[0],
                            "log_tail": tail,
                            "repair": {
                                "attempted": True,
                                "applied": True,
                                "mode": "llm_fill_missing_files",
                                "files": llm_fill_info,
                            },
                        }
                        if auto_bib_info is not None:
                            result["repair"]["auto_bib"] = auto_bib_info
                        if diagnostics.get("summary"):
                            result["diagnostics"] = diagnostics
                        if _page_count_for_tex is not None:
                            try:
                                pages, _ = _page_count_for_tex(tex)
                                if pages is not None:
                                    result["page_count"] = pages
                            except Exception:
                                pass
                        return result

        if repair_on_failure:
            try:
                repair_result = _attempt_agent_compile_repair(
                    workspace_id=workspace_id,
                    article_id=article_id,
                    tex=tex,
                    diagnostics=diagnostics,
                    log_tail=tail,
                    chat_cfg=chat_cfg,
                    model_id=model_id,
                    stubs_created=stubs_info.get("created") or [],
                )
                repair_info = {
                    "attempted": True,
                    "applied": bool(repair_result.get("ok") and repair_result.get("changed")),
                    "ok": bool(repair_result.get("ok")),
                    "changed": bool(repair_result.get("changed")),
                    "message": repair_result.get("message"),
                    "model_id": repair_result.get("model_id"),
                    "model_route": repair_result.get("model_route"),
                }
                if auto_bib_info is not None:
                    repair_info["auto_bib"] = auto_bib_info
                if llm_fill_info is not None:
                    repair_info["llm_fill"] = llm_fill_info
                if repair_result.get("error"):
                    repair_info["error"] = repair_result.get("error")
                if repair_info["applied"]:
                    proc, tail, diagnostics = run_engine()
                    pdf = tex.with_suffix(".pdf")
                    ok = proc.returncode == 0 and pdf.is_file()
                    if ok:
                        result = {
                            "ok": True,
                            "pdf_path": str(pdf),
                            "pdf_url": f"/latex/pdf?workspace_id={resolved_wid}&article_id={_article_id_for(tex, _root)}",
                            "engine": engine[0],
                            "log_tail": tail,
                            "repair": repair_info,
                        }
                        if diagnostics.get("summary"):
                            result["diagnostics"] = diagnostics
                        if _page_count_for_tex is not None:
                            try:
                                pages, _ = _page_count_for_tex(tex)
                                if pages is not None:
                                    result["page_count"] = pages
                            except Exception:
                                pass
                        return result
            except Exception as exc:
                repair_info = {
                    "attempted": True,
                    "applied": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if auto_bib_info is not None:
                    repair_info["auto_bib"] = auto_bib_info
                if llm_fill_info is not None:
                    repair_info["llm_fill"] = llm_fill_info

        if repair_info is None and llm_fill_info is not None:
            repair_info = {
                "attempted": True,
                "applied": bool(llm_fill_info.get("applied")),
                "llm_fill": llm_fill_info,
            }
            if auto_bib_info is not None:
                repair_info["auto_bib"] = auto_bib_info

        if complete_info is not None:
            if repair_info is None:
                repair_info = {
                    "attempted": True,
                    "applied": bool(complete_info.get("applied")),
                }
            repair_info["complete"] = complete_info

        return {
            "ok": False,
            "error": _compile_error_message(tail, diagnostics),
            "engine": engine[0],
            "returncode": proc.returncode,
            "log_tail": tail,
            "diagnostics": diagnostics,
            "repair": repair_info,
            "stubs_created": stubs_info.get("created") or [] or None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tempo esgotado na compilação"}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_TEX_BIN_DIRS: tuple[str, ...] = (
    "/Library/TeX/texbin",  # macOS MacTeX / BasicTeX
    "/usr/local/texlive/2026/bin/universal-darwin",
    "/usr/local/texlive/2025/bin/universal-darwin",
    "/usr/local/texlive/2024/bin/universal-darwin",
    "/usr/local/texlive/2023/bin/universal-darwin",
    "/usr/local/texlive/2026/bin/x86_64-linux",
    "/usr/local/texlive/2025/bin/x86_64-linux",
    "/usr/local/texlive/2024/bin/x86_64-linux",
    "/usr/local/texlive/2023/bin/x86_64-linux",
)


def _extract_compile_diagnostics(log_tail: str) -> dict[str, Any]:
    tail = log_tail or ""

    citations = sorted(set(re.findall(r"Citation `([^`']+)'[^\n]*undefined", tail)))[:80]
    references = sorted(set(re.findall(r"Reference `([^`']+)'[^\n]*undefined", tail)))[:80]

    missing_files = sorted(
        set(re.findall(r"LaTeX Error: File `([^`']+)' not found", tail))
    )[:40]

    fatal_lines: list[str] = []
    for line in tail.splitlines():
        s = line.strip()
        if not s.startswith("!"):
            continue
        s = s.lstrip("!").strip()
        if not s or s.lower().startswith("==>"):
            continue
        fatal_lines.append(s)
    fatal_lines = fatal_lines[:20]

    overfull_hboxes = len(re.findall(r"Overfull \\hbox", tail))
    warning_count = len(re.findall(r"Warning:", tail))

    # natbib raises these when a bibliography entry has a non-numeric year (e.g.
    # an auto-generated placeholder with "n.d.") or when \cite is processed
    # before the .bbl exists. They masquerade as fatal errors but signal a
    # broken/missing bibliography that bib regeneration can fix.
    natbib_year_errors = len(
        re.findall(r"Missing = inserted for \\ifnum|Missing number, treated as zero", tail)
    )

    summary: list[str] = []
    if citations:
        summary.append(
            f"{len(citations)} citação(ões) indefinida(s) (ex.: {', '.join(citations[:3])})"
        )
    if references:
        summary.append(
            f"{len(references)} referência(s) indefinida(s) (ex.: {', '.join(references[:3])})"
        )
    if missing_files:
        summary.append(
            f"arquivo(s) ausente(s): {', '.join(missing_files[:3])}"
        )
    if natbib_year_errors and not citations:
        summary.append("bibliografia malformada (anos não numéricos nas entradas .bib)")
    if fatal_lines:
        summary.append(f"erro fatal: {fatal_lines[0]}")
    if overfull_hboxes:
        summary.append(f"{overfull_hboxes} overfull hbox(es)")

    return {
        "warning_count": warning_count,
        "undefined_citations": citations,
        "undefined_references": references,
        "missing_files": missing_files,
        "fatal_lines": fatal_lines,
        "overfull_hboxes": overfull_hboxes,
        "natbib_year_errors": natbib_year_errors,
        "summary": summary,
    }


def _compile_error_message(log_tail: str, diagnostics: Optional[dict[str, Any]] = None) -> str:
    tail = log_tail or ""
    diag = diagnostics or {}
    if re.search(r"phvr\d*t|Font.*not loadable|can't find file `phv", tail, re.IGNORECASE):
        return (
            "compilação falhou — fonte Helvetica ausente; "
            "reaplique o template serpro-qualidade-vida (copia template_serpro.cls corrigido) "
            "ou instale: tlmgr install helvetic"
        )
    if "template_serpro.cls" in tail and "not found" in tail.lower():
        return "compilação falhou — template_serpro.cls ausente; reaplique o template ao artigo"
    undefined_citations = []
    undefined_references = []
    if isinstance(diag, dict):
        raw_citations = diag.get("undefined_citations") or []
        raw_refs = diag.get("undefined_references") or []
        if isinstance(raw_citations, list):
            undefined_citations = [str(x).strip() for x in raw_citations if str(x).strip()]
        if isinstance(raw_refs, list):
            undefined_references = [str(x).strip() for x in raw_refs if str(x).strip()]
    if undefined_citations:
        parts: list[str] = []
        parts.append(
            "O artigo não compila devido a citações indefinidas"
            + (
                f" ({len(undefined_citations)})"
                if len(undefined_citations) > 1
                else ""
            )
            + (
                " e referência(s) indefinida(s)"
                if undefined_references
                else ""
            )
            + "."
        )
        parts.append(
            "Causa raiz provável: arquivo .bib ausente ou bibliografia não incluída."
        )
        parts.append(
            "Escolha um arquivo .bib existente no workspace ou crie um .bib do zero; "
            "depois inclua \\bibliography{...} e \\bibliographystyle{plainnat} "
            "antes de \\end{document}."
        )
        if undefined_references:
            parts.append(
                "Também revise \\ref{...} para apontar apenas labels existentes no documento."
            )
        return " ".join(parts)
    summary = diag.get("summary") if isinstance(diag, dict) else None
    if isinstance(summary, list) and summary:
        return "compilação falhou — " + "; ".join(str(x) for x in summary[:3] if str(x).strip())
    return "compilação falhou"


def _compile_error_is_repairable(log_tail: str, diagnostics: Optional[dict[str, Any]] = None) -> bool:
    tail = (log_tail or "").lower()
    if not tail and not diagnostics:
        return False
    # Tool/system-level problems the LLM cannot fix
    if re.search(r"command not found|biber: not found|bibtex: not found|latexmk: not found|pdflatex: not found", tail):
        return False
    if re.search(r"permission denied|operation not permitted|read-only file system", tail):
        return False

    diag = diagnostics or {}
    if isinstance(diag, dict):
        if diag.get("undefined_citations") or diag.get("undefined_references"):
            return True
        if diag.get("missing_files"):
            # Missing .cls/.sty/images: LLM can remove or replace the offending \usepackage/\input
            return True
        fatal_lines = diag.get("fatal_lines") or []
        if isinstance(fatal_lines, list):
            fatal_text = " ".join(str(x) for x in fatal_lines)
            if re.search(
                r"undefined control sequence|missing \$ inserted|runaway argument|missing \}|missing \{|environment .* undefined|ended by \\end\{[^}]+\}|emergency stop",
                fatal_text,
                re.IGNORECASE,
            ):
                return True

    # "file not found" in the log — LLM can remove/replace the problematic include
    if re.search(r"file [`'][^`']+['`] not found|no file .+\.(bbl|aux)", tail, re.IGNORECASE):
        return True

    return bool(
        re.search(
            r"undefined control sequence|missing \$ inserted|runaway argument|missing \}|missing \{|environment .* undefined|ended by \\end\{[^}]+\}|emergency stop",
            tail,
            re.IGNORECASE,
        )
    )


def _agent_compile_repair_prompt(
    *,
    tex_path: Path,
    diagnostics: dict[str, Any],
    log_tail: str,
    stubs_created: Optional[list[str]] = None,
) -> str:
    summary = diagnostics.get("summary") if isinstance(diagnostics, dict) else None
    fatal_lines = diagnostics.get("fatal_lines") if isinstance(diagnostics, dict) else None
    missing_files = diagnostics.get("missing_files") if isinstance(diagnostics, dict) else None
    summary_text = (
        "\n".join(f"- {item}" for item in summary[:10])
        if isinstance(summary, list) and summary
        else "- sem resumo estruturado"
    )
    fatal_text = (
        "\n".join(f"- {item}" for item in fatal_lines[:8])
        if isinstance(fatal_lines, list) and fatal_lines
        else "- sem linha fatal isolada"
    )
    stubs_section = ""
    if stubs_created:
        stubs_section = (
            f"\nARQUIVOS CRIADOS AUTOMATICAMENTE (stubs vazios):\n"
            + "\n".join(f"- {f}" for f in stubs_created[:20])
            + "\n"
        )
    missing_section = ""
    if isinstance(missing_files, list) and missing_files:
        missing_section = (
            f"\nARQUIVOS AINDA AUSENTES:\n"
            + "\n".join(f"- {f}" for f in missing_files[:20])
            + "\nPara cada arquivo ausente que não possa ser criado: remova ou comente a linha "
            "\\input/\\include/\\usepackage/\\RequirePackage correspondente no .tex.\n"
        )
    return (
        "Você é um agente de recuperação de compilação LaTeX. Corrija o arquivo .tex para que ele compile sem erros. "
        "Priorize a causa raiz e preserve conteúdo, citações, labels, equações e estrutura. "
        "Regras:\n"
        "- Se um pacote/classe não existe no sistema, substitua por equivalente padrão (ex: article, IEEEtran) ou remova.\n"
        "- Se um \\input/\\include referenciar arquivo ausente que não possa ser criado, remova ou comente essa linha.\n"
        "- Se houver citações indefinidas, inclua \\bibliographystyle{plain}\\bibliography{references_auto} antes de \\end{document} "
        "e certifique-se de que references_auto.bib existe (mesmo que vazio já foi criado).\n"
        "- Não invente fatos, dados ou referências bibliográficas.\n"
        "- Devolva o .tex COMPLETO corrigido no campo content com changed=true.\n\n"
        f"ARQUIVO: {tex_path.name}\n"
        f"RESUMO DOS DIAGNÓSTICOS:\n{summary_text}\n"
        f"LINHAS FATAIS:\n{fatal_text}\n"
        f"{stubs_section}{missing_section}"
        f"LOG DE COMPILAÇÃO (final):\n{log_tail[-6000:]}"
    )


def _attempt_agent_compile_repair(
    *,
    workspace_id: str,
    article_id: str,
    tex: Path,
    diagnostics: dict[str, Any],
    log_tail: str,
    chat_cfg: Any = None,
    model_id: Optional[str] = None,
    stubs_created: Optional[list[str]] = None,
) -> dict[str, Any]:
    from .latex_tex_chat import latex_tex_chat

    prompt = _agent_compile_repair_prompt(
        tex_path=tex,
        diagnostics=diagnostics,
        log_tail=log_tail,
        stubs_created=stubs_created,
    )
    return latex_tex_chat(
        message=prompt,
        content=tex.read_text(encoding="utf-8"),
        history=None,
        chat_cfg=chat_cfg,
        model_id=model_id or "auto",
        workspace_id=workspace_id,
        article_id=article_id,
        save_after=True,
        backup=True,
    )


def _resolve_tex_tool(name: str, env_keys: tuple[str, ...]) -> Optional[str]:
    for key in env_keys:
        raw = os.environ.get(key, "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_file():
                return str(path)
    found = shutil.which(name)
    if found:
        return found
    for directory in _TEX_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def _kpsewhich(name: str) -> Optional[str]:
    """Resolve a TeX file via kpathsea (``kpsewhich``)."""
    tool = _resolve_tex_tool("kpsewhich", ("QCLAW_KPATHSEA", "KPATHSEA"))
    if not tool:
        return None
    try:
        proc = subprocess.run(
            [tool, name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = (proc.stdout or "").strip().splitlines()
    if proc.returncode == 0 and path and path[0]:
        return path[0]
    return None


# TeX Live packages commonly required by bundled Overleaf/Serpro templates.
# Only ``helvetic`` is checked here; ``latexmk`` is optional (pdflatex fallback).
_TEMPLATE_TEXLIVE_PACKAGES: dict[str, tuple[str, ...]] = {
    "serpro": ("helvetic",),
    "report-chapters": ("helvetic",),
    "serpro-qualidade-vida": ("helvetic",),
}


def _missing_template_texlive_packages(template_id: str = "") -> list[str]:
    """Return tlmgr package names that appear missing for template compilation."""
    tid = (template_id or "").strip().lower()
    wanted = _TEMPLATE_TEXLIVE_PACKAGES.get(tid, ())
    missing: list[str] = []
    for pkg in wanted:
        if pkg == "helvetic" and not _kpsewhich("phvr7t.vf"):
            missing.append(pkg)
    return missing


def ensure_texlive_dependencies(
    *,
    template_id: str = "",
    auto_install: bool = True,
) -> dict[str, Any]:
    """Ensure TeX Live packages needed for template apply/compile are present.

    Attempts ``tlmgr install`` when ``auto_install`` is true. Requires admin rights
    for system-wide TeX Live; on failure returns install commands for the operator.
    """
    missing = _missing_template_texlive_packages(template_id)
    result: dict[str, Any] = {
        "ok": not missing,
        "missing": missing,
        "installed": [],
        "skipped": [],
    }
    if not missing:
        return result

    tlmgr = _resolve_tex_tool("tlmgr", ("QCLAW_TLMGR", "TLMGR"))
    if not tlmgr:
        result["error"] = "tlmgr não encontrado — instale MacTeX/BasicTeX"
        result["install_cmd"] = f"tlmgr install {' '.join(missing)}"
        return result

    if not auto_install:
        result["install_cmd"] = f"sudo tlmgr install {' '.join(missing)}"
        return result

    try:
        proc = subprocess.run(
            [tlmgr, "install", *missing],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"tlmgr falhou: {exc}"
        result["install_cmd"] = f"sudo tlmgr install {' '.join(missing)}"
        return result

    log = "\n".join([proc.stdout or "", proc.stderr or ""]).strip()
    if proc.returncode == 0:
        result["installed"] = list(missing)
        result["ok"] = True
        result["missing"] = _missing_template_texlive_packages(template_id)
        result["ok"] = not result["missing"]
        if log:
            result["log"] = log[-2000:]
        return result

    result["error"] = log[-500:] if log else f"tlmgr exit {proc.returncode}"
    result["install_cmd"] = f"sudo tlmgr install {' '.join(missing)}"
    still_missing = _missing_template_texlive_packages(template_id)
    result["missing"] = still_missing
    # helvetic may remain missing on BasicTeX without sudo — template_serpro.cls
    # falls back to Latin Modern Sans, so compilation can still succeed.
    if still_missing == ["helvetic"]:
        result["ok"] = True
        result["warning"] = (
            "helvetic ausente — usando Latin Modern Sans como fallback; "
            "para Helvetica real: sudo tlmgr install helvetic"
        )
        result["skipped"] = ["helvetic"]
    return result


def _pick_engine() -> Optional[tuple[str, str]]:
    latexmk = _resolve_tex_tool("latexmk", ("QCLAW_LATEXMK", "LATEXMK"))
    if latexmk:
        return ("latexmk", latexmk)
    pdflatex = _resolve_tex_tool("pdflatex", ("QCLAW_PDFLATEX", "PDFLATEX"))
    if pdflatex:
        return ("pdflatex", pdflatex)
    return None


def _bib_backend_for(content: str) -> Optional[str]:
    """Return the bibliography backend needed for this document, or None.

    Detects biblatex (``\\addbibresource`` / ``backend=biber``) → biber, classic
    ``\\bibliography{}`` → bibtex.  Returns None when no bibliography is declared.
    """
    text = content or ""
    if re.search(r"\\addbibresource\s*\{", text, flags=re.IGNORECASE):
        return "biber"
    if re.search(r"backend\s*=\s*biber", text, flags=re.IGNORECASE):
        return "biber"
    if re.search(r"\\bibliography\s*\{[^}]+\}", text, flags=re.IGNORECASE):
        return "bibtex"
    return None


def _run_bib_backend(backend: str, tex: Path, cwd: Path) -> tuple[Optional[subprocess.CompletedProcess[str]], str]:
    """Run bibtex/biber on the document's aux/bcf file. Returns (proc, log)."""
    if backend == "biber":
        tool = _resolve_tex_tool("biber", ("QCLAW_BIBER", "BIBER"))
        target = tex.stem  # biber takes the jobname (no extension)
    else:
        tool = _resolve_tex_tool("bibtex", ("QCLAW_BIBTEX", "BIBTEX"))
        target = tex.stem  # bibtex takes the aux basename
    if not tool:
        return None, f"{backend}: not found"
    try:
        proc = subprocess.run(
            [tool, target],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{backend} failed: {exc}"
    return proc, "\n".join([proc.stdout or "", proc.stderr or ""])


def resolve_pdf_file(workspace_id: str, article_id: str) -> tuple[Optional[Path], Optional[str]]:
    root, tex, err = _resolve_article_tex(workspace_id, article_id)
    if tex is None:
        return None, err
    pdf = tex.with_suffix(".pdf")
    if not pdf.is_file():
        return None, "PDF ainda não gerado — compile o artigo primeiro"
    if not _is_under(pdf, root):  # type: ignore[arg-type]
        return None, "caminho PDF inválido"
    return pdf, None


def store_team_articles(workspace_id: str, team_id: str) -> dict[str, Any]:
    """Varre todos os artigos LaTeX de um workspace e os salva no ChromaDB do time.

    Lê as primeiras 4000 chars de cada .tex como documento. Usa o caminho relativo
    como ID estável para suportar upsert incremental.

    Args:
        workspace_id: ID do workspace cadastrado em list_workspaces().
        team_id: ID do time (tenancy key no TeamContextStore).

    Returns:
        dict com ok, added, total, errors.
    """
    from .team_context_store import TeamContextStore

    root, err = _resolve_workspace_root(workspace_id)
    if root is None:
        return {"ok": False, "error": err or "workspace não encontrado"}

    ws = _workspace_by_ref(workspace_id)
    assert ws is not None

    store = TeamContextStore()
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    ids: list[str] = []
    errors: list[str] = []

    for tex in _find_main_tex_files(root):
        aid = _article_id_for(tex, root)
        try:
            content = tex.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError as e:
            errors.append(f"{aid}: {e}")
            continue

        pdf = tex.with_suffix(".pdf")
        documents.append(content)
        metadatas.append(
            {
                "workspace_id": ws.id,
                "workspace_name": ws.name,
                "article_id": aid,
                "title": tex.stem,
                "tex_path": str(tex),
                "has_pdf": pdf.is_file(),
                "source": "latex_article",
            }
        )
        ids.append(f"latex-{ws.id}-{aid.replace('/', '-')}")

    if not documents:
        return {"ok": True, "added": 0, "total": 0, "errors": errors, "message": "nenhum artigo encontrado"}

    result = store.add(team_id=team_id, documents=documents, metadatas=metadatas, ids=ids)
    result["errors"] = errors
    return result


def remove_team_articles(workspace_id: str, team_id: str) -> dict[str, Any]:
    """Remove do ChromaDB do time todos os artigos de um workspace.

    Args:
        workspace_id: ID do workspace cujos artigos devem ser removidos.
        team_id: ID do time (tenancy key no TeamContextStore).

    Returns:
        dict com ok, removed, remaining.
    """
    from .team_context_store import TeamContextStore

    workspace_id = (workspace_id or "").strip()
    team_id = (team_id or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "workspace_id é obrigatório"}
    if not team_id:
        return {"ok": False, "error": "team_id é obrigatório"}

    store = TeamContextStore()
    return store.delete(
        team_id,
        where={"$and": [{"source": "latex_article"}, {"workspace_id": workspace_id}]},
    )

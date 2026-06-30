"""Structured read/patch helpers for LaTeX .tex article files."""

from __future__ import annotations

import functools
import json
import re
import shutil
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

BEGIN_DOC_RE = re.compile(r"\\begin\{document\}")
END_DOC_RE = re.compile(r"\\end\{document\}")
SECTION_RE = re.compile(
    r"\\(?P<level>part|chapter|section|subsection|subsubsection)\*?"
    r"(?:\[[^\]]*\])?\{(?P<title>[^}]*)\}",
    re.IGNORECASE,
)
METADATA_CMD_RE = re.compile(
    r"(\\(?:titulo|autor|coautor|tema|title|author|date|institute|thanks))\{([^}]*)\}",
    re.IGNORECASE,
)
METADATA_ALIASES: dict[str, list[str]] = {
    "title": ["title", "titulo", "tema"],
    "author": ["author", "autor", "coautor"],
    "date": ["date"],
    "institute": ["institute"],
    "thanks": ["thanks"],
}
OBJECTIVES_LINE_RE = re.compile(
    r"(?mi)^[ \t]*%[ \t]*qclaw-objectives[ \t]+(?P<payload>\{.*\})[ \t]*$"
)
OBJECTIVE_SECTION_TITLE_RE = re.compile(
    r"\\(?P<level>part|chapter|section|subsection|subsubsection)\*?"
    r"(?:\[[^\]]*\])?\{(?P<title>[^}]*)\}",
    re.IGNORECASE,
)


def _latex_cmd(name: str) -> str:
    clean = str(name or "").strip().lstrip("\\")
    return f"\\{clean}"


@dataclass
class TexStructure:
    preamble: str
    body: str
    full: str


def split_tex(content: str) -> TexStructure:
    cached = _split_tex_cache_get(content)
    if cached is not None:
        return cached
    begin = BEGIN_DOC_RE.search(content)
    end = END_DOC_RE.search(content)
    if not begin or not end or end.start() <= begin.end():
        result = TexStructure(preamble=content.strip(), body="", full=content)
    else:
        preamble = content[: begin.start()].strip()
        body = content[begin.end() : end.start()].strip()
        result = TexStructure(preamble=preamble, body=body, full=content)
    _split_tex_cache_put(content, result)
    return result


_SPLIT_TEX_CACHE: "OrderedDict[str, TexStructure]" = OrderedDict()
_SPLIT_TEX_CACHE_MAX = 8


def _split_tex_cache_get(content: str) -> TexStructure | None:
    key = _split_tex_cache_key(content)
    hit = _SPLIT_TEX_CACHE.get(key)
    if hit is not None:
        _SPLIT_TEX_CACHE.move_to_end(key)
    return hit


def _split_tex_cache_put(content: str, result: TexStructure) -> None:
    key = _split_tex_cache_key(content)
    _SPLIT_TEX_CACHE[key] = result
    _SPLIT_TEX_CACHE.move_to_end(key)
    while len(_SPLIT_TEX_CACHE) > _SPLIT_TEX_CACHE_MAX:
        _SPLIT_TEX_CACHE.popitem(last=False)


def _split_tex_cache_key(content: str) -> str:
    data = content.encode("utf-8", "surrogatepass")
    return f"{len(data)}:{hashlib.sha1(data).hexdigest()}"


def _join_tex(parts: TexStructure) -> str:
    if not parts.body and not BEGIN_DOC_RE.search(parts.full):
        return parts.preamble
    chunks = [parts.preamble, "", r"\begin{document}", parts.body, r"\end{document}", ""]
    return "\n".join(chunks)


def extract_metadata(content: str) -> dict[str, str]:
    parts = split_tex(content)
    combined = parts.preamble + "\n" + parts.body
    found: dict[str, str] = {}
    for match in METADATA_CMD_RE.finditer(combined):
        cmd = match.group(1).lower()
        value = match.group(2).strip()
        if value:
            found[cmd] = value
    normalized: dict[str, str] = {}
    for key, cmds in METADATA_ALIASES.items():
        for cmd in cmds:
            if cmd.lower() in found:
                normalized[key] = found[cmd.lower()]
                break
    for cmd, value in found.items():
        short = cmd.lstrip("\\")
        if short not in normalized:
            normalized[short] = value
    return normalized


def list_sections(content: str) -> list[dict[str, Any]]:
    parts = split_tex(content)
    sections: list[dict[str, Any]] = []
    for match in SECTION_RE.finditer(parts.body):
        start = match.start()
        line = content[: parts.preamble.count("\n") + 2 + parts.body[:start].count("\n")].count("\n") + 1
        sections.append(
            {
                "level": match.group("level").lower(),
                "title": match.group("title").strip(),
                "offset": start,
                "line": line,
            }
        )
    return sections


def analyze_tex(content: str) -> dict[str, Any]:
    """Structured analysis of a .tex buffer.

    Memoized by content hash: the editor calls this on every chat turn with the
    same (often large) document, and the regex parsing below is pure. A small
    bounded cache avoids re-parsing identical buffers across successive edits.
    """
    cached = _analyze_tex_cache_get(content)
    if cached is not None:
        return dict(cached)
    result = _analyze_tex_uncached(content)
    _analyze_tex_cache_put(content, result)
    return dict(result)


_ANALYZE_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_ANALYZE_CACHE_MAX = 16


def _analyze_tex_cache_get(content: str) -> Optional[dict[str, Any]]:
    key = _analyze_cache_key(content)
    hit = _ANALYZE_CACHE.get(key)
    if hit is not None:
        _ANALYZE_CACHE.move_to_end(key)
    return hit


def _analyze_tex_cache_put(content: str, result: dict[str, Any]) -> None:
    key = _analyze_cache_key(content)
    _ANALYZE_CACHE[key] = result
    _ANALYZE_CACHE.move_to_end(key)
    while len(_ANALYZE_CACHE) > _ANALYZE_CACHE_MAX:
        _ANALYZE_CACHE.popitem(last=False)


def _analyze_cache_key(content: str) -> str:
    data = content.encode("utf-8", "surrogatepass")
    return f"{len(data)}:{hashlib.sha1(data).hexdigest()}"


def _analyze_tex_uncached(content: str) -> dict[str, Any]:
    parts = split_tex(content)
    metadata = extract_metadata(content)
    sections = list_sections(content)
    lines = content.splitlines()
    return {
        "metadata": metadata,
        "sections": sections,
        "section_count": len(sections),
        "line_count": len(lines),
        "char_count": len(content),
        "has_document_env": bool(parts.body or BEGIN_DOC_RE.search(content)),
        "preamble_line_count": len(parts.preamble.splitlines()) if parts.preamble else 0,
        "body_line_count": len(parts.body.splitlines()) if parts.body else 0,
    }


@functools.lru_cache(maxsize=256)
def extract_objectives(content: str) -> dict[str, Any]:
    article_objective = ""
    section_objectives: list[dict[str, str]] = []
    mapped_objectives: list[tuple[Optional[str], str, str]] = []
    matches = list(OBJECTIVES_LINE_RE.finditer(content or ""))
    if matches:
        payload_raw = matches[-1].group("payload")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            article_objective = str(payload.get("article_objective") or "").strip()
            rows = payload.get("section_objectives")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    raw_title = str(row.get("title") or row.get("section") or "").strip()
                    objective = str(row.get("objective") or "").strip()
                    if not raw_title and not objective:
                        continue
                    section_match = OBJECTIVE_SECTION_TITLE_RE.fullmatch(raw_title)
                    level = (
                        section_match.group("level").lower()
                        if section_match
                        else None
                    )
                    title = (
                        str(section_match.group("title") or "").strip()
                        if section_match
                        else raw_title
                    )
                    mapped_objectives.append((level, title.lower(), objective))
                    section_objectives.append({"title": raw_title, "objective": objective})

    sections = list_sections(content or "")
    if sections:
        section_objectives = []
        for row in sections:
            level = str(row.get("level") or "").strip().lower()
            title = str(row.get("title") or "").strip()
            objective = ""
            title_key = title.lower()
            for mapped_level, mapped_title, mapped_objective in mapped_objectives:
                if mapped_level and mapped_level == level and mapped_title == title_key:
                    objective = mapped_objective
                    break
            if not objective:
                for mapped_level, mapped_title, mapped_objective in mapped_objectives:
                    if mapped_level is None and mapped_title == title_key:
                        objective = mapped_objective
                        break
            section_objectives.append(
                {
                    "title": f"\\{level}{{{title}}}",
                    "objective": objective,
                }
            )
    return {
        "article_objective": article_objective,
        "section_objectives": section_objectives,
    }


def _objective_title_key(title: str) -> str:
    """Normalized key for matching a section objective title across sources.

    ``\\section{Introdução}`` and a bare ``Introdução`` collapse to comparable
    keys so objectives stored in the database line up with the section structure
    extracted from the live ``.tex``.
    """
    raw = str(title or "").strip()
    if not raw:
        return ""
    match = OBJECTIVE_SECTION_TITLE_RE.fullmatch(raw)
    if not match:
        return raw.lower()
    return f"{match.group('level').lower()}::{str(match.group('title') or '').strip().lower()}"


def merge_objectives(
    tex_objectives: dict[str, Any],
    db_objectives: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Fill gaps in the ``.tex``-derived objectives from the database.

    ``tex_objectives`` carries the *current* section structure (from
    :func:`extract_objectives`); ``db_objectives`` is the durable store. The tex
    value wins when present, but any empty objective — including the whole
    article objective — falls back to the database so saved objectives survive
    the ``% qclaw-objectives`` comment being stripped from the source.
    """
    merged = {
        "article_objective": str(tex_objectives.get("article_objective") or "").strip(),
        "section_objectives": [
            {
                "title": str(row.get("title") or "").strip(),
                "objective": str(row.get("objective") or "").strip(),
            }
            for row in (tex_objectives.get("section_objectives") or [])
            if isinstance(row, dict)
        ],
    }
    if not isinstance(db_objectives, dict) or not db_objectives.get("found"):
        return merged

    if not merged["article_objective"]:
        merged["article_objective"] = str(
            db_objectives.get("article_objective") or ""
        ).strip()

    db_map: dict[str, str] = {}
    for row in db_objectives.get("section_objectives") or []:
        if not isinstance(row, dict):
            continue
        key = _objective_title_key(row.get("title") or row.get("section") or "")
        objective = str(row.get("objective") or "").strip()
        if key and objective and key not in db_map:
            db_map[key] = objective

    seen_keys: set[str] = set()
    for row in merged["section_objectives"]:
        key = _objective_title_key(row["title"])
        seen_keys.add(key)
        if not row["objective"] and key in db_map:
            row["objective"] = db_map[key]

    # Surface stored objectives whose section is no longer detected in the tex
    # (e.g. the heading was renamed) so they are not silently lost.
    for row in db_objectives.get("section_objectives") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("section") or "").strip()
        objective = str(row.get("objective") or "").strip()
        if not title or not objective:
            continue
        if _objective_title_key(title) in seen_keys:
            continue
        merged["section_objectives"].append({"title": title, "objective": objective})

    return merged


def apply_objectives(
    content: str,
    *,
    article_objective: Optional[str],
    section_objectives: Optional[list[dict[str, Any]]],
) -> str:
    base = OBJECTIVES_LINE_RE.sub("", content)
    base = re.sub(r"\n{3,}", "\n\n", base).strip() + "\n"

    article = str(article_objective or "").strip()
    sections: list[dict[str, str]] = []
    for row in section_objectives or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("section") or "").strip()
        objective = str(row.get("objective") or "").strip()
        if not title and not objective:
            continue
        sections.append({"title": title, "objective": objective})

    if not article and not sections:
        return base

    payload = {
        "article_objective": article,
        "section_objectives": sections,
    }
    json_blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    objectives_line = f"% qclaw-objectives {json_blob}\n"

    docclass_match = re.search(r"(?m)^\\documentclass[^\n]*\n", base)
    if docclass_match:
        idx = docclass_match.end()
        return base[:idx] + objectives_line + base[idx:]
    begin_match = BEGIN_DOC_RE.search(base)
    if begin_match:
        idx = begin_match.start()
        return base[:idx] + objectives_line + base[idx:]
    return objectives_line + base


def extract_bibliography_config(content: str) -> dict[str, Any]:
    """Extract current bibliography configuration (file and style)."""
    lower = content.lower()
    
    # Find bibliography file references
    bib_files: list[str] = []
    
    # Pattern: \bibliography{file1,file2,...}
    bib_pattern = re.compile(r"\\bibliography\s*\{([^}]+)\}", re.IGNORECASE)
    for match in bib_pattern.finditer(content):
        files = [f.strip() for f in match.group(1).split(",")]
        bib_files.extend(files)
    
    # Pattern: \addbibresource{file.bib}  (biblatex)
    addbib_pattern = re.compile(r"\\addbibresource\s*\{([^}]+)\}", re.IGNORECASE)
    for match in addbib_pattern.finditer(content):
        bib_files.append(match.group(1).strip())
    
    # Pattern: \bibliography[options]{file}  (sometimes used)
    alt_bib_pattern = re.compile(r"\\bibliography\s*\[[^\]]*\]\s*\{([^}]+)\}", re.IGNORECASE)
    for match in alt_bib_pattern.finditer(content):
        bib_files.append(match.group(1).strip())
    
    # Remove duplicates while preserving order
    seen_files: set[str] = set()
    unique_files: list[str] = []
    for f in bib_files:
        f_lower = f.lower()
        if f_lower not in seen_files:
            seen_files.add(f_lower)
            unique_files.append(f)
    
    # Find bibliography style
    bibstyle = ""
    style_patterns = [
        (r"\\bibliographystyle\s*\{([^}]+)\}", "bibliographystyle"),  # BibTeX
        (r"\\usepackage\[style=([^\],]+)", "biblatex_style"),  # biblatex in usepackage
        (r"\\usepackage\{biblatex\}", "biblatex_default"),  # biblatex default
    ]
    
    for pattern, kind in style_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            if kind == "biblatex_default":
                bibstyle = "biblatex"
            else:
                bibstyle = match.group(1).strip()
            break
    
    # Detect bibliography system type
    if "\\usepackage{biblatex}" in content or "\\usepackage[" in content and "biblatex" in content:
        bib_system = "biblatex"
    elif "\\bibliographystyle" in content or "\\bibliography{" in content:
        bib_system = "bibtex"
    elif "\\begin{thebibliography}" in content:
        bib_system = "manual"
    else:
        bib_system = "none"
    
    return {
        "bib_files": unique_files,
        "main_bib_file": unique_files[0] if unique_files else "",
        "bibstyle": bibstyle,
        "bib_system": bib_system,
        "has_bibliography": bool(unique_files or "\\printbibliography" in content or "\\bibliography{" in content),
    }


def set_bibliography_config(
    content: str,
    *,
    bib_file: Optional[str] = None,
    bibstyle: Optional[str] = None,
    bib_system: Optional[str] = None,
) -> tuple[str, bool]:
    """Update bibliography configuration (file and/or style).
    
    Args:
        content: LaTeX content
        bib_file: Bibliography file(s) to use (e.g. 'references.bib' or 'refs-1.bib,refs-2.bib')
        bibstyle: Bibliography style (e.g. 'plain', 'harvard', 'apalike', 'ieee')
        bib_system: System type: 'bibtex', 'biblatex', or None to auto-detect/preserve
    
    Returns:
        (modified_content, changed)
    """
    bib_file = str(bib_file or "").strip() if bib_file else None
    bibstyle = str(bibstyle or "").strip() if bibstyle else None
    bib_system = str(bib_system or "").strip().lower() if bib_system else None
    
    if not bib_file and not bibstyle and not bib_system:
        return content, False
    
    current_config = extract_bibliography_config(content)
    current_system = current_config.get("bib_system", "none")
    target_system = bib_system or current_system
    
    # Determine which system to use
    if target_system not in ("bibtex", "biblatex", "manual", "none"):
        # Auto-detect from content
        if "biblatex" in content:
            target_system = "biblatex"
        elif "\\bibliography{" in content:
            target_system = "bibtex"
        else:
            target_system = "bibtex"  # default to bibtex
    
    new_content = content
    
    # Remove old bibliography commands
    # Remove \bibliography{...} calls
    new_content = re.sub(
        r"\\bibliography\s*(?:\[[^\]]*\])?\s*\{[^}]+\}",
        "",
        new_content,
        flags=re.IGNORECASE,
    )
    # Remove \bibliographystyle{...}
    new_content = re.sub(
        r"\\bibliographystyle\s*\{[^}]+\}",
        "",
        new_content,
        flags=re.IGNORECASE,
    )
    # Remove \addbibresource{...} (biblatex)
    new_content = re.sub(
        r"\\addbibresource\s*\{[^}]+\}",
        "",
        new_content,
        flags=re.IGNORECASE,
    )
    # Clean up extra newlines
    new_content = re.sub(r"\n\s*\n\s*\n+", "\n\n", new_content)
    
    # Update bibstyle if provided
    if bibstyle:
        parts = split_tex(new_content)
        # Add or replace \bibliographystyle (for bibtex) or update biblatex options
        if target_system == "biblatex":
            # For biblatex, we'd need to update \usepackage options, which is more complex
            # For now, we'll document this limitation
            pass
        elif target_system == "bibtex":
            # Add \bibliographystyle before \begin{document}
            style_cmd = f"\\bibliographystyle{{{bibstyle}}}"
            if style_cmd not in parts.preamble:
                parts.preamble = parts.preamble.rstrip() + "\n" + style_cmd
                new_content = _join_tex(parts)
    
    # Update bibliography file
    if bib_file:
        parts = split_tex(new_content)
        
        if target_system == "biblatex":
            # For biblatex, use \addbibresource (goes in preamble)
            bib_cmd = f"\\addbibresource{{{bib_file}}}"
        else:  # bibtex
            # For bibtex, use \bibliography (goes near end of body, before \end{document})
            bib_cmd = f"\\bibliography{{{bib_file}}}"
        
        if target_system == "biblatex" or target_system == "bibtex":
            # Insert the bibliography command
            if target_system == "biblatex":
                # Add to preamble
                if bib_cmd not in parts.preamble:
                    parts.preamble = parts.preamble.rstrip() + "\n" + bib_cmd
            else:
                # Add before \end{document}
                if parts.body:
                    parts.body = parts.body.rstrip() + "\n" + bib_cmd
                else:
                    parts.body = bib_cmd
            
            new_content = _join_tex(parts)
    
    changed = new_content != content
    return new_content, changed


def section_slice_at_offset(content: str, offset: int) -> Optional[dict[str, Any]]:
    """Return character slice [start, end) and title for the LaTeX section at offset."""
    if offset < 0:
        offset = 0
    parts = split_tex(content)
    preamble_len = len(parts.preamble) + (2 if parts.preamble else 0) + len(r"\begin{document}")
    if parts.body:
        preamble_len += 1
    body = parts.body
    matches = list(SECTION_RE.finditer(body))
    if not matches:
        start = preamble_len
        end = len(content)
        if END_DOC_RE.search(content):
            end = content.find(r"\end{document}")
        return {"start": start, "end": end, "title": "Documento", "level": "document"}
    rel_offset = max(0, offset - preamble_len)
    target_idx = 0
    for i, match in enumerate(matches):
        if match.start() <= rel_offset:
            target_idx = i
        else:
            break
    start = preamble_len + matches[target_idx].start()
    end = (
        preamble_len + matches[target_idx + 1].start()
        if target_idx + 1 < len(matches)
        else (content.find(r"\end{document}") if END_DOC_RE.search(content) else len(content))
    )
    return {
        "start": start,
        "end": end,
        "title": matches[target_idx].group("title").strip(),
        "level": matches[target_idx].group("level").lower(),
    }


def sanitize_latex_text(content: str) -> str:
    """Remove noise/invalid chars from LaTeX source (Roteiro Viral «Limpar texto»)."""
    if not content:
        return ""
    invalid_control = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    cleaned = (
        content.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("&nbsp;", " ")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\ufffd", "")
    )
    cleaned = invalid_control.sub("", cleaned)
    lines = [
        re.sub(r"[ \t]+$", "", line.replace("\t", "  "))
        for line in cleaned.split("\n")
    ]
    lines = [line for line in lines if not re.match(r"^\s*(?:undefined|null|nan)\s*$", line, re.I)]
    return re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines)).strip()


def _replace_metadata(content: str, cmd: str, value: str) -> tuple[str, bool]:
    cmd_name = _latex_cmd(cmd)
    pattern = re.compile(re.escape(cmd_name) + r"\{[^}]*\}", re.IGNORECASE)
    replacement = f"{cmd_name}{{{value}}}"

    def _repl(_: re.Match[str]) -> str:
        return replacement

    if pattern.search(content):
        return pattern.sub(_repl, content, count=1), True
    return content, False


def _insert_metadata_in_preamble(content: str, cmd: str, value: str) -> str:
    parts = split_tex(content)
    cmd_name = _latex_cmd(cmd)
    insertion = f"{cmd_name}{{{value}}}"
    preamble = parts.preamble.rstrip()
    if preamble:
        parts.preamble = preamble + "\n" + insertion
    else:
        parts.preamble = insertion
    return _join_tex(parts)


def set_metadata(content: str, metadata: dict[str, str]) -> tuple[str, list[str]]:
    changed: list[str] = []
    out = content
    for key, value in metadata.items():
        if value is None:
            continue
        text = str(value)
        aliases = METADATA_ALIASES.get(key.lower(), [])
        if not aliases:
            aliases = [str(key).lstrip("\\")]
        applied = False
        for cmd in aliases:
            new_out, ok = _replace_metadata(out, cmd, text)
            if ok:
                out = new_out
                changed.append(f"{cmd}{{{text}}}")
                applied = True
                break
        if not applied:
            out = _insert_metadata_in_preamble(out, aliases[0], text)
            changed.append(f"inserted {aliases[0]}{{{text}}}")
    return out, changed


def replace_text(
    content: str,
    *,
    find: str,
    replace: str,
    regex: bool = False,
    count: int = 0,
) -> tuple[str, int]:
    if not find:
        return content, 0
    if regex:
        pattern = re.compile(find, flags=re.MULTILINE | re.DOTALL)
        new_content, n = pattern.subn(replace, content, count=count or 0)
        return new_content, n
    limit = count if count > 0 else -1
    return content.replace(find, replace, limit), content.count(find) if limit < 0 else min(content.count(find), count)


def insert_text(
    content: str,
    *,
    anchor: str,
    text: str,
    position: str = "after",
    regex: bool = False,
) -> tuple[str, bool]:
    if not anchor:
        return content, False
    if regex:
        match = re.search(anchor, content, flags=re.MULTILINE | re.DOTALL)
        if not match:
            return content, False
        idx = match.end() if position == "after" else match.start()
    else:
        idx = content.find(anchor)
        if idx < 0:
            return content, False
        idx = idx + len(anchor) if position == "after" else idx
    return content[:idx] + text + content[idx:], True


def edit_section(content: str, *, section: str, new_content: str) -> tuple[str, bool]:
    title = (section or "").strip()
    if not title:
        return content, False
    parts = split_tex(content)
    body = parts.body
    pattern = re.compile(
        r"\\(?P<level>part|chapter|section|subsection|subsubsection)\*?"
        r"(?:\[[^\]]*\])?\{(?P<title>[^}]*)\}",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(body))
    if not matches:
        return content, False

    target_idx: Optional[int] = None
    for i, match in enumerate(matches):
        if match.group("title").strip().lower() == title.lower():
            target_idx = i
            break
    if target_idx is None:
        return content, False

    start = matches[target_idx].start()
    end = matches[target_idx + 1].start() if target_idx + 1 < len(matches) else len(body)
    header = matches[target_idx].group(0)
    replacement = header
    if new_content.strip():
        replacement = f"{header}\n{new_content.strip()}\n"
    parts.body = body[:start] + replacement + body[end:]
    return _join_tex(parts), True


def set_line_range(content: str, *, start_line: int, end_line: int, new_content: str) -> tuple[str, bool]:
    if start_line < 1 or end_line < start_line:
        return content, False
    lines = content.splitlines(keepends=True)
    if start_line > len(lines):
        return content, False
    end_line = min(end_line, len(lines))
    replacement = new_content
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    updated = lines[: start_line - 1] + [replacement] + lines[end_line:]
    return "".join(updated), True


def apply_tex_edit(content: str, *, action: str, **kwargs: Any) -> dict[str, Any]:
    action = (action or "").strip().lower()
    if action == "analyze":
        return {"ok": True, "action": action, "analysis": analyze_tex(content), "changed": False}

    if action == "get_bibliography":
        bib_config = extract_bibliography_config(content)
        return {
            "ok": True,
            "action": action,
            "bibliography": bib_config,
            "changed": False,
        }

    new_content = content
    changed = False
    details: dict[str, Any] = {}

    if action == "replace":
        new_content, n = replace_text(
            content,
            find=str(kwargs.get("find") or ""),
            replace=str(kwargs.get("replace") or ""),
            regex=bool(kwargs.get("regex")),
            count=int(kwargs.get("count") or 0),
        )
        changed = n > 0
        details = {"replacements": n}
    elif action == "set_metadata":
        meta = kwargs.get("metadata") or {}
        if not isinstance(meta, dict) or not meta:
            return {"ok": False, "error": "metadata dict is required for set_metadata"}
        new_content, applied = set_metadata(content, {str(k): str(v) for k, v in meta.items()})
        changed = bool(applied)
        details = {"applied": applied}
    elif action == "set_bibliography":
        bib_file = kwargs.get("bib_file")
        bibstyle = kwargs.get("bibstyle")
        bib_system = kwargs.get("bib_system")
        new_content, changed = set_bibliography_config(
            content,
            bib_file=bib_file,
            bibstyle=bibstyle,
            bib_system=bib_system,
        )
        if not changed:
            details = {"message": "no bibliography changes applied"}
        else:
            details = {
                "bib_file": bib_file,
                "bibstyle": bibstyle,
                "bib_system": bib_system,
            }
    elif action == "edit_section":
        new_content, changed = edit_section(
            content,
            section=str(kwargs.get("section") or ""),
            new_content=str(kwargs.get("content") or ""),
        )
        if not changed:
            return {"ok": False, "error": f"section not found: {kwargs.get('section')}"}
    elif action == "insert":
        new_content, changed = insert_text(
            content,
            anchor=str(kwargs.get("anchor") or ""),
            text=str(kwargs.get("text") or ""),
            position=str(kwargs.get("position") or "after"),
            regex=bool(kwargs.get("regex")),
        )
        if not changed:
            return {"ok": False, "error": "anchor not found"}
    elif action == "set_lines":
        new_content, changed = set_line_range(
            content,
            start_line=int(kwargs.get("start_line") or 0),
            end_line=int(kwargs.get("end_line") or 0),
            new_content=str(kwargs.get("content") or ""),
        )
        if not changed:
            return {"ok": False, "error": "invalid line range"}
    elif action == "delete_match":
        new_content, n = replace_text(
            content,
            find=str(kwargs.get("find") or ""),
            replace="",
            regex=bool(kwargs.get("regex")),
            count=int(kwargs.get("count") or 0),
        )
        changed = n > 0
        details = {"deleted_matches": n}
        if not changed:
            return {"ok": False, "error": "pattern not found"}
    else:
        return {"ok": False, "error": f"unknown action: {action}"}

    return {
        "ok": True,
        "action": action,
        "changed": changed,
        "content": new_content,
        "length_before": len(content),
        "length_after": len(new_content),
        **details,
    }


def _page_count_for_tex(tex: Path) -> tuple[Optional[int], Optional[str]]:
    pdf = tex.with_suffix(".pdf")
    if not pdf.is_file():
        return None, "PDF not found — compile first or use compile_after=true"
    try:
        from .slides_pdf_preview import pdf_page_count

        pages = pdf_page_count(pdf)
    except Exception as exc:
        return None, str(exc)
    if pages is None:
        try:
            import io

            from pypdf import PdfReader

            pages = len(PdfReader(io.BytesIO(pdf.read_bytes())).pages)
        except Exception as exc:
            return None, f"could not count pages: {exc}"
    return pages, None


def edit_article_tex(
    workspace_id: str,
    article_id: str,
    *,
    action: str,
    backup: bool = True,
    dry_run: bool = False,
    compile_after: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    from .latex_articles import _resolve_article_tex, compile_article

    root, tex, err = _resolve_article_tex(workspace_id, article_id)
    if tex is None:
        return {"ok": False, "error": err or "artigo não encontrado"}

    try:
        original = tex.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    if action == "page_info":
        response: dict[str, Any] = {
            "ok": True,
            "workspace_id": workspace_id,
            "article_id": article_id,
            "path": str(tex),
            "action": action,
        }
        if compile_after or not tex.with_suffix(".pdf").is_file():
            compiled = compile_article(
                workspace_id,
                article_id,
                repair_on_failure=bool(compile_after),
            )
            response["compile"] = compiled
            if not compiled.get("ok"):
                return {**response, "ok": False, "error": compiled.get("error", "compile failed")}
        pages, page_err = _page_count_for_tex(tex)
        if pages is None:
            return {**response, "ok": False, "error": page_err or "page count unavailable"}
        response["page_count"] = pages
        return response

    result = apply_tex_edit(original, action=action, **kwargs)
    if not result.get("ok"):
        return result

    response: dict[str, Any] = {
        "ok": True,
        "workspace_id": workspace_id,
        "article_id": article_id,
        "path": str(tex),
        "action": action,
        "dry_run": dry_run,
        "changed": bool(result.get("changed")),
    }

    if action == "analyze":
        response["analysis"] = result.get("analysis")
        pages, page_err = _page_count_for_tex(tex)
        if pages is not None:
            response["page_count"] = pages
        elif page_err:
            response["page_count_note"] = page_err
        return response

    if not result.get("changed"):
        response["message"] = "no changes applied"
        return response

    new_content = str(result.get("content") or "")
    if dry_run:
        response["preview"] = new_content[:8000]
        response["preview_truncated"] = len(new_content) > 8000
        return response

    if backup:
        backup_path = tex.with_suffix(".tex.bak")
        try:
            shutil.copy2(tex, backup_path)
            response["backup_path"] = str(backup_path)
        except OSError as exc:
            return {"ok": False, "error": f"backup failed: {exc}"}

    try:
        tex.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    response["bytes_written"] = len(new_content.encode("utf-8"))
    response["length_before"] = result.get("length_before")
    response["length_after"] = result.get("length_after")

    if compile_after:
        compiled = compile_article(
            workspace_id,
            article_id,
            repair_on_failure=True,
        )
        response["compile"] = compiled
        if compiled.get("ok"):
            pages, _ = _page_count_for_tex(tex)
            if pages is not None:
                response["page_count"] = pages

    return response

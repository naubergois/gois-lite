"""Interactive chat questions — option chips and free-text input prompts.

When the assistant needs the user to choose between a few options or to type a
specific value, it can emit a hidden marker in its reply:

    <!--question: {"question": "...", "options": [{"label": "...", "value": "..."}]}-->

or, for a free-text answer:

    <!--question: {"question": "...", "type": "input", "placeholder": "...",
                   "prefix": "...", "submitLabel": "..."}-->

This module parses that marker, strips it from the visible text, and returns a
normalized question dict that the frontend renders as clickable option buttons
or as an inline input box. The normalized shape is stable so both the live send
path and the persisted history render identically.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# Hidden marker the LLM (or any backend) can embed anywhere in the reply text.
_QUESTION_RE = re.compile(r"<!--\s*question:\s*(\{.*?\})\s*-->", re.DOTALL)

# Question kinds rendered by the frontend.
TYPE_OPTIONS = "options"
TYPE_INPUT = "input"

_MAX_OPTIONS = 12
_MAX_LABEL = 200
_MAX_TITLE = 300


def _coerce_str(value: Any, *, limit: int) -> str:
    """Best-effort coerce to a trimmed string capped at ``limit`` chars."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text[:limit]


def normalize_question(data: Any) -> Optional[dict[str, Any]]:
    """Validate and normalize a raw question dict into a stable shape.

    Returns a dict with ``type`` of ``"options"`` or ``"input"``, or ``None``
    when the payload is not a usable question.
    """
    if not isinstance(data, dict):
        return None

    title = _coerce_str(data.get("question") or data.get("title"), limit=_MAX_TITLE)

    raw_options = data.get("options")
    options: list[dict[str, str]] = []
    if isinstance(raw_options, list):
        for opt in raw_options:
            if isinstance(opt, dict):
                value = _coerce_str(opt.get("value"), limit=_MAX_LABEL)
                label = _coerce_str(opt.get("label") or value, limit=_MAX_LABEL)
            else:
                value = _coerce_str(opt, limit=_MAX_LABEL)
                label = value
            if not value:
                continue
            options.append({"label": label or value, "value": value})
            if len(options) >= _MAX_OPTIONS:
                break

    declared = _coerce_str(data.get("type"), limit=20).lower()
    # Explicit input, or no usable options -> free-text input prompt.
    if declared == TYPE_INPUT or (declared != TYPE_OPTIONS and not options):
        if not title and not data.get("placeholder"):
            return None
        return {
            "type": TYPE_INPUT,
            "question": title,
            "placeholder": _coerce_str(data.get("placeholder"), limit=_MAX_LABEL),
            "prefix": _coerce_str(data.get("prefix"), limit=_MAX_LABEL),
            "submitLabel": _coerce_str(data.get("submitLabel"), limit=40) or "Enviar",
        }

    if not options:
        return None
    return {"type": TYPE_OPTIONS, "question": title, "options": options}


def extract_interactive_question(
    text: str,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Strip the hidden ``<!--question: ...-->`` marker from ``text``.

    Returns ``(clean_text, question)`` where ``question`` is the normalized
    question dict (or ``None`` when no valid marker is present). The original
    text is returned unchanged when nothing matches.
    """
    if not text:
        return text, None

    m = _QUESTION_RE.search(text)
    if not m:
        return text, None

    try:
        parsed = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        log.debug("interactive question marker had invalid JSON: %r", m.group(1)[:120])
        return text, None

    question = normalize_question(parsed)
    if question is None:
        return text, None

    clean = (text[: m.start()] + text[m.end():]).strip()
    if not clean:
        clean = _coerce_str(question.get("question"), limit=_MAX_TITLE)
    return clean, question

"""Gois chat page (assets/chat.html)."""

import json

from functools import lru_cache
from pathlib import Path
from typing import Optional

from .app_nav import _inject_app_nav
from .chat_rv_screens import rv_frontend_base
from .dashboard_theme import GOIS_MASCOT_SVG, GOIS_MOOD_SVGS
from .gois_lite import is_gois_lite
from .gois_lite_ui import LITE_CHAT_HEAD_INJECT
from .team_picker_ui import inject_team_picker
from .ui_assets import load_asset

_CHAT_HTML_PATH = Path(__file__).resolve().parent / "assets" / "chat.html"

CHAT_PAGE = load_asset("chat.html")


@lru_cache(maxsize=4)
def _chat_page_template(_chat_mtime: float) -> str:
    # Keep a small cache keyed by mtime so edits to assets/chat.html are picked
    # up without requiring a full service restart.
    _ = _chat_mtime
    return load_asset("chat.html")


def _chat_html_mtime() -> float:
    try:
        return _CHAT_HTML_PATH.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=4)
def _build_chat_html_cached(hermes_dashboard_url: str, _chat_mtime: float, lite: bool) -> str:
    html = (
        _chat_page_template(_chat_mtime).replace("__GOIS_MASCOT_SVG__", json.dumps(GOIS_MASCOT_SVG))
        .replace("__GOIS_MOOD_SVGS__", json.dumps(GOIS_MOOD_SVGS))
        .replace("__GOIS_MASCOT_INLINE__", GOIS_MASCOT_SVG)
        .replace(
            "__HERMES_DASHBOARD_URL__",
            json.dumps(hermes_dashboard_url or ""),
        )
        .replace(
            '"";//__RV_FRONTEND_BASE__',
            f"{json.dumps(rv_frontend_base() or '')};",
        )
    )
    if lite:
        html = LITE_CHAT_HEAD_INJECT + html
    return _inject_app_nav(
        inject_team_picker(html),
        "chat",
        hermes_dashboard_url=hermes_dashboard_url or None,
    )


def build_chat_html(hermes_dashboard_url: Optional[str] = None) -> str:
    return _build_chat_html_cached(
        hermes_dashboard_url or "",
        _chat_html_mtime(),
        is_gois_lite(),
    )

"""Kanban board page.

Extracted from dashboard.py; template lives in assets/kanban.html.
"""

import json
from typing import Optional

from .app_nav import _inject_app_nav
from .dashboard_theme import (
    APP_THEME_CSS,
    APP_UI_CSS,
    GOIS_MASCOT_SVG,
    GOIS_MOOD_SVGS,
)
from .hermes_cron_exec_ui import inject_hermes_cron_exec
from .hermes_mascots import HERMES_MASCOTS
from .team_picker_ui import inject_team_picker
from .ui_assets import load_asset

KANBAN_PAGE = load_asset("kanban.html")


def build_kanban_html(hermes_dashboard_url: Optional[str] = None) -> str:
    # Reload template each call so /kanban picks up asset edits without process restart.
    page = load_asset("kanban.html")
    mascots_json = json.dumps(HERMES_MASCOTS, ensure_ascii=False)
    html = inject_hermes_cron_exec(
        page.replace("__APP_THEME_CSS__", APP_THEME_CSS)
        .replace("__APP_UI_CSS__", APP_UI_CSS)
        .replace("__GOIS_MASCOT_SVG__", json.dumps(GOIS_MASCOT_SVG))
        .replace("__GOIS_MOOD_SVGS__", json.dumps(GOIS_MOOD_SVGS))
        .replace("__GOIS_MASCOT_INLINE__", GOIS_MASCOT_SVG)
        .replace("__HERMES_MASCOTS__", mascots_json)
    )
    return _inject_app_nav(
        inject_team_picker(html),
        "kanban",
        hermes_dashboard_url=hermes_dashboard_url,
    )

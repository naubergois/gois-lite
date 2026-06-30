"""Shared dashboard theme (CSS/JS/mascot SVGs) loaded from package assets."""

import json

from .ui_assets import load_asset

GOIS_EXTENDED_NAME = "Generative Orchestration Artificial Intelligence Swarm"
GOIS_SHORT_NAME = "gois"

APP_NAV_CSS = load_asset("app_nav.css")
APP_THEME_CSS = load_asset("app_theme.css")
APP_UI_CSS = load_asset("app_ui.css")
APP_SEARCH_JS = load_asset("app_search.js")
DARK_MODE_JS = load_asset("dark_mode.js")
GOIS_MASCOT_SVG = load_asset("gois_mascot.svg")
GOIS_MOOD_SVGS: dict[str, str] = json.loads(load_asset("gois_moods.json"))

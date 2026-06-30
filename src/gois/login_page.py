"""Login page.

Extracted from dashboard.py; template lives in assets/login.html.
"""

from .dashboard_theme import GOIS_MASCOT_SVG
from .ui_assets import load_asset

LOGIN_PAGE = load_asset("login.html")


def build_login_html() -> str:
    return LOGIN_PAGE.replace("__GOIS_MASCOT_INLINE__", GOIS_MASCOT_SVG)

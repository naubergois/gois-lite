"""Compatibility facade for the web dashboard UI.

Historically this module embedded every dashboard page inline (21k+ lines).
Each page now lives in its own ``*_page.py`` module with the HTML template
under ``assets/``; shared nav/theme live in ``app_nav.py`` and
``dashboard_theme.py``. This facade re-exports the public API so existing
imports (``from .dashboard import ...``) keep working unchanged.

In **gois-lite** only Chat + Kanban pages are imported at load time.
"""

from __future__ import annotations

from .app_nav import _NAV_GROUPS, _NAV_ITEM_AREAS, _NAV_ITEMS, _inject_app_nav, render_app_nav
from .dashboard_theme import (
    APP_NAV_CSS,
    APP_THEME_CSS,
    APP_UI_CSS,
    DARK_MODE_JS,
    GOIS_MASCOT_SVG,
    GOIS_MOOD_SVGS,
)
from .gois_lite import is_gois_lite
from .login_page import LOGIN_PAGE, build_login_html
from .chat_page import CHAT_PAGE, build_chat_html
from .kanban_page import KANBAN_PAGE, build_kanban_html

if not is_gois_lite():
    from .monitor_page import HTML, build_dashboard_html
    from .ruflo_chat_page import RUFLO_CHAT_PAGE, build_ruflo_chat_html
    from .perguntas_page import PERGUNTAS_PAGE, build_perguntas_html
    from .errors_page import ERRORS_PAGE, build_errors_html
    from .health_page import HEALTH_PAGE, build_health_html
    from .skills_page import SKILLS_PAGE, build_skills_html
    from .active_agents_page import ACTIVE_AGENTS_PAGE, build_active_agents_html
    from .cron_slots_page import CRON_SLOTS_PAGE, build_cron_slots_html
    from .cron_costs_page import CRON_COSTS_PAGE, build_cron_costs_html
    from .roles_page import ROLES_PAGE, build_roles_html
    from .users_page import USERS_PAGE, build_users_html
    from .teams_page import TEAMS_PAGE, build_teams_html
    from .team_create_page import TEAM_CREATE_PAGE, build_team_create_html
    from .metrics_page import METRICS_PAGE, build_metrics_html
    from .latex_page import LATEX_PAGE, build_latex_html
    from .allowlist_page import ALLOWLIST_PAGE, build_allowlist_html
    from .env_keys_page import ENV_KEYS_PAGE, build_env_keys_html
    from .agenda_page import AGENDA_PAGE, build_agenda_html
    from .swarm_page import SWARM_PAGE, build_swarm_html, build_swarm_profiles_html
    from .mcp_servers_page import MCP_SERVERS_PAGE, build_mcp_servers_html
else:
    HTML = ""
    RUFLO_CHAT_PAGE = ""
    PERGUNTAS_PAGE = ""
    ERRORS_PAGE = ""
    HEALTH_PAGE = ""
    SKILLS_PAGE = ""
    ACTIVE_AGENTS_PAGE = ""
    CRON_SLOTS_PAGE = ""
    CRON_COSTS_PAGE = ""
    ROLES_PAGE = ""
    USERS_PAGE = ""
    TEAMS_PAGE = ""
    TEAM_CREATE_PAGE = ""
    METRICS_PAGE = ""
    LATEX_PAGE = ""
    ALLOWLIST_PAGE = ""
    ENV_KEYS_PAGE = ""
    AGENDA_PAGE = ""
    SWARM_PAGE = ""
    MCP_SERVERS_PAGE = ""

    def build_dashboard_html(*_a, **_k) -> str:
        return build_login_html()

    def build_ruflo_chat_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_perguntas_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_errors_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_health_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_skills_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_active_agents_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_cron_slots_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_cron_costs_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_roles_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_users_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_teams_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_team_create_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_metrics_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_latex_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_allowlist_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_env_keys_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_agenda_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_swarm_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_swarm_profiles_html(*_a, **_k) -> str:
        return build_chat_html()

    def build_mcp_servers_html(*_a, **_k) -> str:
        return build_chat_html()

__all__ = [
    "_NAV_GROUPS",
    "_NAV_ITEM_AREAS",
    "_NAV_ITEMS",
    "_inject_app_nav",
    "render_app_nav",
    "APP_NAV_CSS",
    "APP_THEME_CSS",
    "APP_UI_CSS",
    "DARK_MODE_JS",
    "GOIS_MASCOT_SVG",
    "GOIS_MOOD_SVGS",
    "HTML",
    "build_dashboard_html",
    "LOGIN_PAGE",
    "build_login_html",
    "CHAT_PAGE",
    "build_chat_html",
    "RUFLO_CHAT_PAGE",
    "build_ruflo_chat_html",
    "PERGUNTAS_PAGE",
    "build_perguntas_html",
    "ERRORS_PAGE",
    "build_errors_html",
    "HEALTH_PAGE",
    "build_health_html",
    "SKILLS_PAGE",
    "build_skills_html",
    "ACTIVE_AGENTS_PAGE",
    "build_active_agents_html",
    "CRON_SLOTS_PAGE",
    "build_cron_slots_html",
    "CRON_COSTS_PAGE",
    "build_cron_costs_html",
    "ROLES_PAGE",
    "build_roles_html",
    "USERS_PAGE",
    "build_users_html",
    "TEAMS_PAGE",
    "build_teams_html",
    "TEAM_CREATE_PAGE",
    "build_team_create_html",
    "KANBAN_PAGE",
    "build_kanban_html",
    "METRICS_PAGE",
    "build_metrics_html",
    "LATEX_PAGE",
    "build_latex_html",
    "ALLOWLIST_PAGE",
    "build_allowlist_html",
    "ENV_KEYS_PAGE",
    "build_env_keys_html",
    "AGENDA_PAGE",
    "build_agenda_html",
    "SWARM_PAGE",
    "build_swarm_html",
    "build_swarm_profiles_html",
    "MCP_SERVERS_PAGE",
    "build_mcp_servers_html",
]

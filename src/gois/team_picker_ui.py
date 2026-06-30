"""Shared team picker dialog (modal + JS) for chat and kanban pages."""

from .ui_assets import load_asset


def inject_team_picker(html: str) -> str:
    """Insert team picker markup, styles, and script into a page template."""
    css = load_asset("team_picker.css")
    modal = load_asset("team_picker.html")
    js = load_asset("team_picker.js")
    return (
        html.replace("__TEAM_PICKER_CSS__", css)
        .replace("__TEAM_PICKER_HTML__", modal)
        .replace("/*__TEAM_PICKER_JS__*/", js)
    )

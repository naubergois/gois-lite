"""Inline SVG outline icons for the dashboard navigation.

Self-contained (no webfont / CDN dependency). Each entry is the *inner*
markup of a 24x24 ``stroke="currentColor"`` outline icon; :func:`nav_icon`
wraps it in the shared ``<svg>`` envelope so colour and size follow CSS.
"""

from __future__ import annotations

# name -> inner SVG markup (viewBox 0 0 24 24, stroke = currentColor)
_ICONS: dict[str, str] = {
    "chart-line": '<path d="M4 4v16h16"/><path d="M7 14l3-4 3 3 4-6"/>',
    "heartbeat": '<path d="M3 12h4l2-5 3 9 2-4h7"/>',
    "gauge": '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 14l3.5-3.5"/><circle cx="12" cy="14" r="1.1"/>',
    "coin": '<circle cx="12" cy="12" r="8"/><path d="M12 8v8M10 10h3M10 14h4"/>',
    "robot": '<rect x="5" y="8" width="14" height="11" rx="2.5"/><path d="M12 8V5M9 13h.01M15 13h.01M9.5 16h5"/><circle cx="12" cy="4" r="1"/>',
    "hexagons": '<path d="M12 3l7 4.5v9L12 21l-7-4.5v-9z"/>',
    "user-cog": '<circle cx="9" cy="8" r="3"/><path d="M4 20a5 5 0 0 1 8-3"/><circle cx="17" cy="17" r="2.4"/><path d="M17 13.6v1M17 19.4v1M20.4 17h-1M14.6 17h-1"/>',
    "message": '<path d="M4 5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-4 4v-4H5a1 1 0 0 1-1-1z"/>',
    "refresh": '<path d="M5 12a7 7 0 0 1 12-5l2 2M19 12a7 7 0 0 1-12 5l-2-2"/><path d="M19 4v5h-5M5 20v-5h5"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.6 2.3c-.9.4-1.1 1-1.1 1.8M12 16.5h.01"/>',
    "clipboard": '<rect x="5" y="5" width="14" height="16" rx="2.5"/><path d="M9 4h6v3H9z"/><path d="M9 11h6M9 14h6M9 17h4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>',
    "alert-triangle": '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    "badge": '<rect x="4" y="5" width="16" height="14" rx="2.5"/><circle cx="9" cy="11" r="2"/><path d="M6 16a3 3 0 0 1 6 0M14.5 10h4M14.5 13.5h4"/>',
    "user-plus": '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 11-3"/><path d="M16 11h6M19 8v6"/>',
    "clock-plus": '<circle cx="11" cy="11" r="7"/><path d="M11 7.5v3.5l2.4 1.5"/><path d="M16 18.5h6M19 15.5v6"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    "book": '<path d="M12 6c-1.6-1.2-4-2-7-2v13c3 0 5.4.8 7 2 1.6-1.2 4-2 7-2V4c-3 0-5.4.8-7 2z"/><path d="M12 6v13"/>',
    "database": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    "brain": '<path d="M12 5a3 3 0 0 0-5 2 3 3 0 0 0-1 5 3 3 0 0 0 3 4h3z"/><path d="M12 5a3 3 0 0 1 5 2 3 3 0 0 1 1 5 3 3 0 0 1-3 4h-3z"/><path d="M12 5v14"/>',
    "users": '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 6a3 3 0 0 1 0 5M18 20a6 6 0 0 0-3-5"/>',
    "kanban": '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M9 4v16M15 4v16M6 8h1.5M11.5 8h1.5M17 8h0M16.5 8h1.5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "plug": '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0z"/><path d="M12 16v5"/>',
    "server": '<rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/><path d="M8 7h.01M8 17h.01"/>',
    "signal": '<path d="M5 20v-4M10 20v-8M15 20v-12M20 20V6" stroke-linecap="round"/>',
    "bolt": '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
    "shield-check": '<path d="M12 3l8 3v6c0 4-3 7-8 9-5-2-8-5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "key": '<circle cx="8" cy="14" r="3.5"/><path d="M10.5 11.5L20 4M16 6l2 2M13 9l2 2"/>',
    "user": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    "mobile": '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 18h2"/>',
    "file-text": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 16h6M10 10h3"/>',
    "chart-bar": '<path d="M4 20h16"/><rect x="5" y="11" width="3" height="7"/><rect x="10.5" y="6" width="3" height="12"/><rect x="16" y="13" width="3" height="5"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    "cash": '<rect x="3" y="6" width="18" height="12" rx="2.5"/><circle cx="12" cy="12" r="2.5"/><path d="M6 9.5v5M18 9.5v5"/>',
    "trash": '<path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2.5"/><path d="M4 9.5h16M8 3v4M16 3v4"/>',
    "braces": '<path d="M8 4c-2 0-2 3.5-3.2 4C6 8.5 6 12 8 12M16 4c2 0 2 3.5 3.2 4C18 8.5 18 12 16 12"/>',
    "chart-dots": '<path d="M4 4v16h16"/><circle cx="8" cy="14" r="1.2"/><circle cx="12" cy="10" r="1.2"/><circle cx="16" cy="13" r="1.2"/><circle cx="19" cy="7" r="1.2"/>',
    "sparkles": '<path d="M12 4l1.5 4L18 9.5 13.5 11 12 15l-1.5-4L6 9.5 10.5 8z"/><path d="M18 15l.7 1.8 1.8.7-1.8.7L18 20l-.7-1.8-1.8-.7 1.8-.7z"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M16 16l4 4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
}

_SVG_OPEN = (
    '<svg class="qcm-ico{extra}" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true" focusable="false">'
)


def nav_icon(name: str, *, extra_class: str = "") -> str:
    """Return the inline ``<svg>`` for *name* (falls back to a generic dot)."""
    inner = _ICONS.get(name) or '<circle cx="12" cy="12" r="3"/>'
    extra = f" {extra_class}" if extra_class else ""
    return f"{_SVG_OPEN.format(extra=extra)}{inner}</svg>"


def has_icon(name: str) -> bool:
    return name in _ICONS

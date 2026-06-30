"""Sidebar navigation shared across dashboard pages."""

from typing import Optional

from .dashboard_theme import (
    APP_NAV_CSS,
    APP_SEARCH_JS,
    DARK_MODE_JS,
    GOIS_EXTENDED_NAME,
    GOIS_MASCOT_SVG,
    GOIS_SHORT_NAME,
)
from .nav_icons import nav_icon

_NAV_ITEMS = (
    ("monitor", "/ui", "Monitor", "chart-line"),
    ("health", "/saude", "Saúde", "heartbeat"),
    ("model_quotas", "/modelos", "Cotas", "gauge"),
    ("model_costs", "/modelos/custos", "Custos", "coin"),
    ("active_agents", "/agentes", "Agentes ativos", "robot"),
    ("swarm_robots", "/swarm", "Robôs do Swarm", "hexagons"),
    ("swarm_profiles", "/swarm/perfis", "Gerir perfis", "user-cog"),
    ("chat", "/chat", "Chat QClaw", "message"),
    ("ruflo_chat", "/chat/ruflo", "Chat RuFlo", "refresh"),
    ("perguntas_chat", "/chat/perguntas", "Responder Perguntas", "help"),
    ("ruflo_results", "/ruflo", "RuFlo Resultados", "clipboard"),
    ("ruflo_engines", "/ruflo/motores", "Motores RuFlo", "settings"),
    ("errors", "/erros", "Erros", "alert-triangle"),
    ("roles", "/roles", "Papéis", "badge"),
    ("agent_create", "/agents/novo", "Criar agente", "user-plus"),
    ("cron_create", "/jobs/novo", "Criar job", "clock-plus"),
    ("ide", "/ide", "IDE", "terminal"),
    ("knowledge", "/conhecimento", "Conhecimento", "book"),
    ("entity_db", "/entidades", "Entidades", "database"),
    ("project_memory", "/memoria", "Memória", "brain"),
    ("teams", "/times", "Times", "users"),
    ("kanban", "/kanban", "Kanban", "kanban"),
    ("projects", "/projetos", "Projetos", "folder"),
    ("mcp_cards", "/mcp-cards", "MCP Cards", "plug"),
    ("mcp_servers", "/mcp", "Servidores MCP", "server"),
    ("priority_queue", "/fila-prioridades", "Fila Prioridades", "signal"),
    ("skills", "/skills", "Skills OpenClaw", "bolt"),
    ("allowlist", "/allowlist", "Allowlist WhatsApp", "shield-check"),
    ("env_keys", "/chaves", "Chaves LLM", "key"),
    ("users", "/users", "Usuários", "user"),
    ("agenda", "/agenda", "Agenda WhatsApp", "mobile"),
    ("latex", "/latex", "Artigos LaTeX", "file-text"),
    ("backup_manager", "/backups", "Backups 📦", "archive"),
    ("article_quality", "/artigos/qualidade", "Qualidade Artigos", "chart-bar"),
    ("cron_slots", "/cron/slots", "Slots cron", "clock"),
    ("cron_costs", "/cron/custos", "Custos cron", "cash"),
    ("manage_delete", "/gerenciar/apagar", "Apagar", "trash"),
    ("calendario", "/calendario", "Calendário", "calendar"),
)

from .user_areas import _NAV_GROUPS, _NAV_ITEM_AREAS
from .gois_lite import LITE_TABS, is_gois_lite


def render_app_nav(
    active: str,
    *,
    extra_html: str = "",
    hermes_dashboard_url: Optional[str] = None,
    show_monitor_extras: bool = False,
) -> str:
    """Sidebar navigation shared across dashboard pages."""
    lite = is_gois_lite()
    brand_name = "Gois Lite" if lite else GOIS_SHORT_NAME
    brand_tag = "Chat + Kanban" if lite else GOIS_EXTENDED_NAME

    # Build lookup: key -> (href, label, icon)
    nav_lookup: dict[str, tuple[str, str, str]] = {
        key: (href, label, icon) for key, href, label, icon in _NAV_ITEMS
    }

    # Render grouped nav links (hidden in gois-lite — tabs only).
    groups_html: list[str] = []
    if not lite:
        for group_id, group_label, group_keys in _NAV_GROUPS:
            items_html: list[str] = []
            group_has_active = False
            for key in group_keys:
                if key not in nav_lookup:
                    continue
                href, label, icon = nav_lookup[key]
                is_active = key == active
                if is_active:
                    group_has_active = True
                cls = "nav-link active" if is_active else "nav-link"
                area = _NAV_ITEM_AREAS.get(key, group_label)
                items_html.append(
                    f'<a href="{href}" class="{cls}" title="{label} — {area}"'
                    f' data-user-area="{area}">'
                    f'<span class="nav-link-icon" aria-hidden="true">{nav_icon(icon)}</span>'
                    f'<span class="nav-link-label">{label}</span></a>'
                )
            if not items_html:
                continue
            # Group with active item is always open; others start closed (user can toggle)
            closed_cls = "" if group_has_active else " closed"
            groups_html.append(
                f'<div class="nav-group{closed_cls}" data-group="{group_id}">'
                f'<div class="nav-group-header" onclick="qcmToggleGroup(this)">'
                f'<span class="chevron">▼</span>{group_label}</div>'
                f'<div class="nav-group-items">{"".join(items_html)}</div></div>'
            )

    # Extra links (Hermes, status, etc.) — not shown in lite.
    extra_links: list[str] = []
    if not lite:
        if show_monitor_extras:
            extra_links.append(
                '<a href="#" id="hdr-hermes" class="nav-link" target="_blank" '
                'rel="noopener" style="display:none" title="Hermes">'
                f'<span class="nav-link-icon" aria-hidden="true">{nav_icon("sparkles")}</span>'
                '<span class="nav-link-label">Hermes</span></a>'
            )
        elif hermes_dashboard_url:
            extra_links.append(
                f'<a href="{hermes_dashboard_url}" class="nav-link" target="_blank" '
                f'rel="noopener" title="Hermes">'
                f'<span class="nav-link-icon" aria-hidden="true">{nav_icon("sparkles")}</span>'
                f'<span class="nav-link-label">Hermes</span></a>'
            )

        for key, href, label, icon in (
            ("status", "/status", "JSON", "braces"),
            ("metrics", "/metrics/ui", "Métricas", "chart-dots"),
        ):
            cls = "nav-link active" if key == active else "nav-link"
            extra_links.append(
                f'<a href="{href}" class="{cls}" title="{label}">'
                f'<span class="nav-link-icon" aria-hidden="true">{nav_icon(icon)}</span>'
                f'<span class="nav-link-label">{label}</span></a>'
            )

        if extra_links:
            groups_html.append(
                '<div class="nav-group" data-group="extra">'
                '<div class="nav-group-header" onclick="qcmToggleGroup(this)">'
                '<span class="chevron">▼</span>Sistema</div>'
                f'<div class="nav-group-items">{"".join(extra_links)}</div></div>'
            )

    # Primary top tab bar — the main workspaces of the app.
    _TABS = LITE_TABS if lite else (
        ("chat", "/chat", "Chat", "message"),
        ("kanban", "/kanban", "Kanban", "kanban"),
        ("swarm_robots", "/swarm", "Swarm", "hexagons"),
        ("monitor", "/ui", "Monitor", "chart-line"),
    )
    _tab_active_alias = {
        "swarm_profiles": "swarm_robots",
        "ruflo_results": "swarm_robots",
        "ruflo_engines": "swarm_robots",
        "active_agents": "swarm_robots",
        "ruflo_chat": "chat",
        "perguntas_chat": "chat",
        "health": "monitor",
        "errors": "monitor",
    }
    active_tab = _tab_active_alias.get(active, active)
    tabs_html: list[str] = []
    for key, href, label, icon in _TABS:
        cls = "qcm-tab active" if key == active_tab else "qcm-tab"
        tabs_html.append(
            f'<a href="{href}" class="{cls}" title="{label}">'
            f'<span class="qcm-tab-icon" aria-hidden="true">{nav_icon(icon)}</span>'
            f'<span class="qcm-tab-label">{label}</span></a>'
        )
    tabbar_html = (
        '<div class="qcm-tabbar" id="qcm-tabbar" aria-label="Telas principais">'
        f'{"".join(tabs_html)}</div>'
    )

    user_badge = (
        '<span class="qcm-user" id="qcm-user" hidden>'
        '<span id="qcm-user-name">—</span>'
        '</span>'
    )
    logout_button = (
        '<button type="button" class="logout-btn" id="qcm-logout-btn" hidden '
        'onclick="window.qcmLogout && window.qcmLogout()"><span>Sair</span></button>'
    )
    theme_btn = '<button type="button" class="theme-btn" id="qcm-theme-btn" title="Alternar tema" onclick="qcmToggleTheme()">🌙</button>'
    search_btn = (
        '<button type="button" class="search-btn" id="qcm-search-btn" '
        'title="Buscar páginas (⌘K)" aria-label="Buscar páginas" '
        'onclick="window.qcmGlobalSearch && window.qcmGlobalSearch()">'
        f'<span class="search-btn-ico" aria-hidden="true">{nav_icon("search")}</span>'
        '<span class="search-btn-label">Buscar</span></button>'
    )

    return f"""<div id="qcm-nav-overlay" onclick="window.qcmToggleNav()"></div>
<nav class="app-nav" id="qcm-app-nav" aria-label="Navegação principal">
  <button class="nav-toggle" id="qcm-nav-toggle" onclick="window.qcmToggleNav()" title="Expandir/recolher menu">◀</button>
  <div class="app-nav-inner">
    <a href="/chat" class="brand" title="{brand_tag}">
      <span class="brand-icon">{GOIS_MASCOT_SVG}</span>
      <span class="brand-text">
        <span class="brand-name">{brand_name}</span>
        <span class="brand-tag">{brand_tag}</span>
      </span>
    </a>
    <div class="nav-links-wrap">
      <div class="nav-links">
        {"".join(groups_html)}
      </div>
    </div>
    <div class="nav-extra">
      <div class="nav-extra-row">
        {search_btn}
        {theme_btn}
        {user_badge}
      </div>
      {logout_button}
    </div>
  </div>
</nav>
{tabbar_html}
<script>
(function () {{
  // Toggle nav group open/closed
  window.qcmToggleGroup = function(header) {{
    var group = header.parentElement;
    group.classList.toggle("closed");
    // Save state
    try {{
      var states = JSON.parse(localStorage.getItem("qcm-nav-groups") || "{{}}")
      states[group.dataset.group] = group.classList.contains("closed") ? "1" : "";
      localStorage.setItem("qcm-nav-groups", JSON.stringify(states));
    }} catch(e) {{}}
  }};
  // Restore group states (but keep active group open)
  (function() {{
    try {{
      var states = JSON.parse(localStorage.getItem("qcm-nav-groups") || "{{}}")
      document.querySelectorAll(".nav-group").forEach(function(g) {{
        var id = g.dataset.group;
        // If it has an active link, always keep open
        if (g.querySelector(".nav-link.active")) return;
        if (states[id] === "1") g.classList.add("closed");
        else if (states[id] === "") g.classList.remove("closed");
      }});
    }} catch(e) {{}}
  }})();

  // Toggle sidebar collapse
  window.qcmToggleNav = function qcmToggleNav() {{
    var nav = document.getElementById("qcm-app-nav");
    var ov = document.getElementById("qcm-nav-overlay");
    nav.classList.toggle("collapsed");
    if (ov) ov.classList.toggle("open", !nav.classList.contains("collapsed") && window.innerWidth <= 768);
    try {{ localStorage.setItem("qcm-nav-collapsed", nav.classList.contains("collapsed") ? "1" : ""); }} catch(e) {{}}
  }};
  // Restore state
  (function() {{
    var saved = localStorage.getItem("qcm-nav-collapsed");
    var nav = document.getElementById("qcm-app-nav");
    if (saved === "1" && window.innerWidth > 768) nav.classList.add("collapsed");
    if (window.innerWidth <= 768) nav.classList.add("collapsed");
  }})();

  const LOGIN_PATH = "/login";
  const PUBLIC_PATHS = new Set([LOGIN_PATH, "/auth/login", "/auth/register", "/auth/bootstrap"]);
  let authEnabled = null;
  function nextParam() {{
    const here = window.location.pathname + window.location.search + window.location.hash;
    return "?next=" + encodeURIComponent(here || "/");
  }}
  function redirectToLogin() {{
    if (window.location.pathname === LOGIN_PATH) return;
    window.location.replace(LOGIN_PATH + nextParam());
  }}
  function showUser(user) {{
    const wrap = document.getElementById("qcm-user");
    const name = document.getElementById("qcm-user-name");
    const btn = document.getElementById("qcm-logout-btn");
    if (authEnabled === false) {{
      if (wrap) wrap.hidden = true;
      if (btn) btn.hidden = true;
      return;
    }}
    if (wrap && name && user && user.username) {{
      name.textContent = user.username;
      wrap.hidden = false;
    }}
    if (btn) btn.hidden = false;
  }}
  async function fetchJsonTimeout(url, opt, ms) {{
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms != null ? ms : 12000);
    try {{
      const r = await fetch(url, Object.assign(
        {{ credentials: "same-origin", cache: "no-cache" }},
        opt || {{}},
        {{ signal: ctrl.signal }}
      ));
      const data = await r.json().catch(() => ({{}}));
      return {{ r, data }};
    }} finally {{
      clearTimeout(timer);
    }}
  }}
  async function authEnabledFlag() {{
    if (authEnabled !== null) return authEnabled;
    try {{
      const {{ data }} = await fetchJsonTimeout("/auth/bootstrap", {{}}, 8000);
      authEnabled = !!(data && data.enabled);
    }} catch (_) {{
      // Falha transitória no bootstrap não deve desligar auth no browser:
      // isso deixava o chat abrir sem sessão e APIs retornarem not authenticated.
      authEnabled = true;
    }}
    return authEnabled;
  }}
  window.qcmLogout = async function qcmLogout() {{
    try {{
      await fetch("/auth/logout", {{ method: "POST", cache: "no-cache" }});
    }} catch (_) {{ /* ignore network errors and force local reload */ }}
    window.location.assign(LOGIN_PATH);
  }};
  function _qcmToastType(t) {{ return typeof t==="string"?t:"info"; }}
  function _qcmToastDur(t,d) {{ return typeof d==="number"?d:(t==="error"?6000:3500); }}
  window.qcmNavToast = function qcmNavToast(msg, t) {{
    return qcmToast(msg, _qcmToastType(t));
  }};
  window.qcmToast = function qcmToast(msg, t, dur) {{
    t = _qcmToastType(t);
    dur = _qcmToastDur(t, dur);
    let container = document.getElementById("qcm-toast-container");
    if (!container) {{
      container = document.createElement("div");
      container.id = "qcm-toast-container";
      container.setAttribute("role", "status");
      container.setAttribute("aria-live", "polite");
      document.body.appendChild(container);
    }}
    const icons = {{ info: "i", success: "\u2713", warn: "\u26A0", error: "\u2717" }};
    const el = document.createElement("div");
    el.className = "qcm-toast " + t;
    el.innerHTML = '<span class="qcm-toast-icon">' + (icons[t] || "i") + '</span>'
      + '<span class="qcm-toast-body">' + String(msg || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + '</span>'
      + '<button class="qcm-toast-close" onclick="this.parentElement.classList.add(&#39;hiding&#39;);setTimeout(function(){{this.parentElement.remove()}}.bind(this),200)" aria-label="Fechar">&times;</button>';
    container.appendChild(el);
    requestAnimationFrame(function() {{ el.classList.add("show"); }});
    var timer = setTimeout(function() {{
      el.classList.add("hiding");
      setTimeout(function() {{ if (el.parentNode) el.remove(); }}, 200);
    }}, dur);
    el.addEventListener("click", function(e) {{
      if (e.target === el || e.target.closest(".qcm-toast-body")) {{
        clearTimeout(timer);
        el.classList.add("hiding");
        setTimeout(function() {{ if (el.parentNode) el.remove(); }}, 200);
      }}
    }});
    return el;
  }};
  // Monkey-patch native alert to use toasts
  var _origAlert = window.alert;
  window.alert = function(msg) {{
    qcmToast(String(msg || ""), "warn", 6000);
  }};
  function qcmPageLoading(show, label) {{
    let el = document.getElementById("qcm-page-loading");
    if (!el) {{
      el = document.createElement("div");
      el.id = "qcm-page-loading";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = '<div class="box"></div>';
      document.body.appendChild(el);
    }}
    const box = el.querySelector(".box");
    if (box) box.textContent = label || "Carregando a tela…";
    el.classList.toggle("show", !!show);
    document.body.classList.toggle("qcm-nav-busy", !!show);
  }}
  window.qcmPageLoading = qcmPageLoading;
  window.qcmHidePageLoading = function qcmHidePageLoading() {{
    qcmPageLoading(false);
    clearTimeout(window._qcmNavSlowTimer);
    document.querySelectorAll(".app-nav .nav-link.loading, .qcm-tabbar .qcm-tab.loading").forEach((n) => {{
      n.classList.remove("loading");
      n.removeAttribute("aria-busy");
    }});
  }};
  window.qcmShowPageLoading = function qcmShowPageLoading(label) {{
    qcmPageLoading(true, label || "Carregando a tela…");
    window.qcmNavToast(label || "Carregando a tela…");
  }};
  function setupNavLinkLoading() {{
    const here = window.location.pathname;
    const navLinks = ".app-nav a.nav-link[href], .qcm-tabbar a.qcm-tab[href]";
    document.querySelectorAll(navLinks).forEach((a) => {{
      const raw = (a.getAttribute("href") || "").trim();
      if (!raw || raw === "#" || a.target === "_blank" || a.classList.contains("offline")) return;
      let dest = raw;
      try {{
        dest = new URL(raw, window.location.origin).pathname;
      }} catch (_) {{ return; }}
      if (dest === here) {{
        a.addEventListener("click", (ev) => {{
          if (!a.classList.contains("active")) return;
          ev.preventDefault();
          window.qcmNavToast("Você já está nesta tela.");
        }});
        return;
      }}
      a.addEventListener("click", () => {{
        document.querySelectorAll(".app-nav .nav-link.loading, .qcm-tabbar .qcm-tab.loading").forEach((n) => {{
          n.classList.remove("loading");
          n.removeAttribute("aria-busy");
        }});
        a.classList.add("loading");
        a.setAttribute("aria-busy", "true");
        window.qcmShowPageLoading("Carregando a tela…");
        clearTimeout(window._qcmNavSlowTimer);
        window._qcmNavSlowTimer = setTimeout(() => {{
          window.qcmNavToast("Ainda carregando… aguarde um instante.");
          qcmPageLoading(true, "Ainda carregando…");
        }}, 2200);
      }});
    }});
  }}
  window.qcmAuthGate = function qcmAuthGate() {{
    if (window._qcmAuthGatePromise) return window._qcmAuthGatePromise;
    window._qcmAuthGatePromise = (async () => {{
      if (PUBLIC_PATHS.has(window.location.pathname)) return {{ ok: true, public: true }};
      const enabled = await authEnabledFlag();
      if (!enabled) {{
        showUser(null);
        return {{ ok: true, user: {{ username: "local" }}, authDisabled: true }};
      }}
      try {{
        const {{ r, data }} = await fetchJsonTimeout("/auth/me", {{}}, 10000);
        if (!r.ok || data.ok === false) {{
          redirectToLogin();
          return {{ ok: false }};
        }}
        showUser(data.user || null);
        return {{ ok: true, user: data.user || null }};
      }} catch (_) {{
        redirectToLogin();
        return {{ ok: false }};
      }}
    }})().finally(() => {{
      window._qcmAuthGatePromise = null;
    }});
    return window._qcmAuthGatePromise;
  }};
  function bootNav() {{
    window.qcmHidePageLoading();
    setupNavLinkLoading();
    window.qcmAuthGate();
  }}
  {DARK_MODE_JS}
  {APP_SEARCH_JS}
  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", bootNav);
  }} else {{
    bootNav();
  }}
}})();
</script>"""


def _inject_app_nav(
    html: str,
    active: str,
    *,
    hermes_dashboard_url: Optional[str] = None,
    show_monitor_extras: bool = False,
) -> str:
    return (
        html.replace("__APP_NAV_CSS__", APP_NAV_CSS)
        .replace(
            "__APP_NAV__",
            render_app_nav(
                active,
                hermes_dashboard_url=hermes_dashboard_url,
                show_monitor_extras=show_monitor_extras,
            ),
        )
    )

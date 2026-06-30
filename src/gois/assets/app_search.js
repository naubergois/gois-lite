// Global page search palette (Cmd+K / Ctrl+K) — shared across dashboard pages.
(function () {
  function escHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function navLinkLabel(a) {
    var label = a.querySelector(".nav-link-label");
    return label ? label.textContent.trim() : a.textContent.trim();
  }

  function buildIndex() {
    var idx = [];
    document.querySelectorAll(".app-nav a.nav-link[href]").forEach(function (a) {
      var href = (a.getAttribute("href") || "").trim();
      if (!href || href === "#" || a.target === "_blank" || a.classList.contains("offline")) {
        return;
      }
      var text = navLinkLabel(a);
      if (!text) return;
      var iconEl = a.querySelector(".nav-link-icon");
      var icon = iconEl ? (iconEl.innerHTML.trim() || iconEl.textContent.trim()) : "\u{1F4C4}";
      var area = (a.getAttribute("data-user-area") || "").trim();
      var groupEl = a.closest(".nav-group");
      var groupLabel = "";
      if (groupEl) {
        var header = groupEl.querySelector(".nav-group-header");
        if (header) {
          groupLabel = header.textContent.replace(/^\s*▼\s*/, "").trim();
        }
      }
      var desc = area || groupLabel || "P\u00e1gina";
      idx.push({ label: text, type: "page", icon: icon, url: href, desc: desc });
    });
    document.querySelectorAll(".qcm-tabbar a.qcm-tab[href]").forEach(function (a) {
      var href = (a.getAttribute("href") || "").trim();
      if (!href || href === "#") return;
      var labelEl = a.querySelector(".qcm-tab-label");
      var text = labelEl ? labelEl.textContent.trim() : a.textContent.trim();
      if (!text) return;
      var iconEl = a.querySelector(".qcm-tab-icon");
      var icon = iconEl ? (iconEl.innerHTML.trim() || iconEl.textContent.trim()) : "\u{1F4AC}";
      idx.push({ label: text, type: "page", icon: icon, url: href, desc: "Tela principal" });
    });
    document
      .querySelectorAll(".trello-card .card-title, .trello-card [class*=\"title\"]")
      .forEach(function (el) {
        var text = el.textContent.trim();
        if (text) {
          idx.push({
            label: text,
            type: "card",
            icon: "\u{1F4CB}",
            url: "",
            desc: "Card Kanban",
            el: el,
          });
        }
      });
    document
      .querySelectorAll(".agent-name, [class*=\"agent\"][class*=\"name\"]")
      .forEach(function (el) {
        var text = el.textContent.trim();
        if (text) {
          idx.push({
            label: text,
            type: "agent",
            icon: "\u{1F916}",
            url: "",
            desc: "Agente",
            el: el,
          });
        }
      });
    return idx;
  }

  var idx = [];
  var overlay = null;
  var highlightIdx = -1;

  function closeSearch() {
    if (!overlay) return;
    overlay.classList.remove("open");
    document.body.style.overflow = "";
    idx = buildIndex();
  }

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "qcm-search-overlay";
    overlay.className = "qcm-search-overlay";
    overlay.innerHTML =
      '<div class="qcm-search-modal" role="dialog" aria-modal="true" aria-label="Buscar">' +
      '<div class="qcm-search-input-wrap">' +
      '<span aria-hidden="true">\u{1F50D}</span>' +
      '<input type="search" id="qcm-search-input" placeholder="Buscar p\u00e1ginas, cards, agentes\u2026" autocomplete="off" />' +
      '<span class="qcm-search-shortcut">Esc</span>' +
      "</div>" +
      '<div class="qcm-search-results" id="qcm-search-results"></div>' +
      '<div class="qcm-search-footer">' +
      "<span><kbd>\u2191</kbd><kbd>\u2193</kbd> Navegar</span>" +
      "<span><kbd>Enter</kbd> Ir</span>" +
      "<span><kbd>Esc</kbd> Fechar</span>" +
      "</div></div>";
    document.body.appendChild(overlay);

    var input = document.getElementById("qcm-search-input");
    var results = document.getElementById("qcm-search-results");

    function doSearch(query) {
      query = query.toLowerCase().trim();
      if (!query) {
        results.innerHTML =
          '<div class="qcm-search-empty">Digite para buscar ou use \u2318K / Ctrl+K</div>';
        return;
      }
      var matches = idx.filter(function (i) {
        return (
          i.label.toLowerCase().includes(query) || i.desc.toLowerCase().includes(query)
        );
      });
      if (matches.length === 0) {
        results.innerHTML =
          '<div class="qcm-search-empty">Nenhum resultado para &quot;' +
          escHtml(query) +
          '&quot;</div>';
        return;
      }
      highlightIdx = -1;
      results.innerHTML = matches
        .map(function (m, i) {
          return (
            '<div class="qcm-search-result" data-idx="' +
            i +
            '" role="option">' +
            '<span class="icon" aria-hidden="true">' +
            (/^<svg/.test(m.icon) ? m.icon : escHtml(m.icon)) +
            "</span>" +
            '<div class="info"><div class="title">' +
            escHtml(m.label) +
            '</div><div class="desc">' +
            escHtml(m.desc) +
            "</div></div></div>"
          );
        })
        .join("");
    }

    function selectHighlight() {
      var items = results.querySelectorAll(".qcm-search-result");
      if (highlightIdx >= 0 && highlightIdx < items.length) {
        items[highlightIdx].click();
      }
    }

    input.addEventListener("input", function () {
      doSearch(this.value);
    });
    input.addEventListener("keydown", function (e) {
      var items = results.querySelectorAll(".qcm-search-result");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        items.forEach(function (el) {
          el.classList.remove("highlight");
        });
        highlightIdx = Math.min(highlightIdx + 1, items.length - 1);
        if (items[highlightIdx]) {
          items[highlightIdx].classList.add("highlight");
          items[highlightIdx].scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        items.forEach(function (el) {
          el.classList.remove("highlight");
        });
        highlightIdx = Math.max(highlightIdx - 1, 0);
        if (items[highlightIdx]) {
          items[highlightIdx].classList.add("highlight");
          items[highlightIdx].scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        selectHighlight();
      }
    });

    results.addEventListener("click", function (e) {
      var item = e.target.closest(".qcm-search-result");
      if (!item) return;
      var match = idx[parseInt(item.dataset.idx, 10)];
      if (!match) return;
      closeSearch();
      if (match.url) {
        window.location.href = match.url;
      } else if (match.el) {
        match.el.scrollIntoView({ behavior: "smooth", block: "center" });
        match.el.style.outline = "2px solid var(--accent)";
        setTimeout(function () {
          match.el.style.outline = "";
        }, 2000);
      }
    });

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeSearch();
    });
    return overlay;
  }

  window.qcmGlobalSearch = function qcmGlobalSearch() {
    ensureOverlay();
    idx = buildIndex();
    overlay.classList.add("open");
    document.body.style.overflow = "hidden";
    var input = document.getElementById("qcm-search-input");
    var results = document.getElementById("qcm-search-results");
    if (input) {
      input.value = "";
      input.focus();
    }
    if (results) {
      results.innerHTML =
        '<div class="qcm-search-empty">Digite para buscar ou use \u2318K / Ctrl+K</div>';
    }
    highlightIdx = -1;
  };

  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName || "").toLowerCase();
    var isEditor =
      tag === "input" ||
      tag === "textarea" ||
      tag === "select" ||
      (e.target && e.target.isContentEditable);
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      window.qcmGlobalSearch();
      return;
    }
    if (e.key === "Escape" && overlay && overlay.classList.contains("open")) {
      e.preventDefault();
      closeSearch();
      return;
    }
    if (e.key === "/" && !isEditor && !e.metaKey && !e.ctrlKey && !e.altKey) {
      var onKanban = document.getElementById("kb-filter-input");
      if (onKanban) return;
      e.preventDefault();
      window.qcmGlobalSearch();
    }
  });
})();

"""UI injections for gois-lite (no OpenClaw / QClaw chrome)."""

from __future__ import annotations

LITE_CHAT_HEAD_INJECT = """<script>window.GOIS_LITE=true;</script>
<style>
  html.gois-lite #openclaw-section,
  html.gois-lite #chat-openclaw-toggle,
  html.gois-lite #openclaw-control,
  html.gois-lite #agent-select,
  html.gois-lite #skills-panel,
  html.gois-lite #resize-skills,
  html.gois-lite #chat-academic-toggle,
  html.gois-lite #replicate-dialog-overlay,
  html.gois-lite .openclaw-only,
  html.gois-lite .main-head .panel-toggle-btn:not(#btn-toggle-sessions):not(#btn-toggle-top-panels):not(#btn-focus-chat),
  html.gois-lite button[class*="chat-"][class*="-trigger"],
  html.gois-lite div[class*="chat-"][class*="-dock"] { display: none !important; }
</style>
<script>
(function () {
  document.documentElement.classList.add("gois-lite");
  var layout = document.querySelector(".layout");
  if (layout) layout.classList.add("right-collapsed");
  function liteLabels() {
    var t = document.getElementById("chat-title");
    if (t && /openclaw/i.test(t.textContent || "")) {
      t.textContent = "Gois — selecione uma conversa";
    }
    var foot = document.getElementById("model-picker-foot");
    if (foot) {
      foot.querySelectorAll("span").forEach(function (el) {
        if (/qclaw|openclaw/i.test(el.textContent || "")) {
          el.textContent = "Ferramentas Kanban via gois_cards_*";
        }
      });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", liteLabels);
  } else {
    liteLabels();
  }
})();
</script>
"""

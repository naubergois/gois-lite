"""MCP Cards page (/mcp-cards) — boards + cards visualization with sidebar nav."""

from __future__ import annotations

from typing import Optional

from .dashboard import _inject_app_nav


_MCP_CARDS_PAGE = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<title>MCP Cards · gois</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
__APP_NAV_CSS__
  :root {
    --bg: #0f1115; --panel: #171a21; --panel-2: #1e222c; --border: #262b36;
    --fg: #e6e8ee; --muted: #8a93a6; --accent: #6aa9ff; --accent-dim: rgba(106,169,255,.12);
    --good: #3ddc84; --ok: #3ddc84; --warn: #ffb454; --bad: #ff5a5f;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --sidebar-w: 260px;
    --radius: 10px;
  }
  [data-theme="light"] { --bg:#f5f6fa; --panel:#fff; --panel-2:#f0f2f7; --border:#e1e4ec;
            --fg:#15171c; --muted:#5b6477; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body { background: var(--bg); color: var(--fg);
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; overflow: hidden; }

  .layout { display: flex; flex: 1; min-height: 0; overflow: hidden; }

  .sidebar { width: var(--sidebar-w); flex-shrink: 0; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
    transition: margin-left .25s cubic-bezier(.4,0,.2,1), opacity .2s;
    overflow: hidden; z-index: 800; }
  .sidebar.closed { margin-left: calc(-1 * var(--sidebar-w)); opacity: 0; pointer-events: none; }
  .sidebar-head { padding: 16px 18px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  .sidebar-head h2 { font-size: 15px; font-weight: 700; flex: 1; }
  .sidebar-head .close-btn { background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 18px; padding: 4px; border-radius: 6px; }
  .sidebar-head .close-btn:hover { background: var(--panel-2); color: var(--fg); }
  .sidebar-body { flex: 1; overflow-y: auto; padding: 12px 0; }
  .sidebar-section { padding: 6px 16px; font-size: 10px; text-transform: uppercase;
    letter-spacing: .8px; color: var(--muted); font-weight: 700; }
  .sidebar-item { display: flex; align-items: center; gap: 10px; padding: 10px 18px;
    font-size: 13px; color: var(--muted); cursor: pointer; border-left: 3px solid transparent;
    transition: all .12s; text-decoration: none; }
  .sidebar-item:hover { background: var(--panel-2); color: var(--fg); }
  .sidebar-item.active { color: var(--accent); border-left-color: var(--accent);
    background: var(--accent-dim); font-weight: 600; }
  .sidebar-item .badge { margin-left: auto; background: var(--accent-dim); color: var(--accent);
    font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
    font-family: var(--mono); }
  .sidebar-item .badge.zero { opacity: .4; }
  .sidebar-item .sidebar-delete { background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 13px; padding: 2px 6px; border-radius: 6px; flex-shrink: 0;
    opacity: 0; transition: opacity .12s, color .12s, background .12s; }
  .sidebar-item:hover .sidebar-delete { opacity: 1; }
  .sidebar-item .sidebar-delete:hover { color: var(--bad); background: rgba(255,90,95,.1); }

  .main-content { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
  .main-toolbar { padding: 12px 20px; border-bottom: 1px solid var(--border);
    background: var(--panel); display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
    flex-shrink: 0; }
  .main-toolbar .hamburger { display: none; background: none; border: 1px solid var(--border);
    color: var(--fg); font-size: 18px; cursor: pointer; padding: 8px 10px; border-radius: 8px; }
  .main-toolbar h1 { font-size: 17px; font-weight: 700; }
  .main-toolbar .search { flex: 1; min-width: 160px; max-width: 320px;
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 14px; color: var(--fg); font: inherit; }
  .main-toolbar .search::placeholder { color: var(--muted); }
  .main-toolbar .filter-select { background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 12px; color: var(--fg); font: inherit; }
  .main-toolbar .stats { margin-left: auto; color: var(--muted); font-family: var(--mono);
    font-size: 12px; white-space: nowrap; }

  .cards-area { flex: 1; overflow-y: auto; padding: 20px; }
  .cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 14px; }
  .card-item { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px 18px; display: flex; flex-direction: column; gap: 10px;
    transition: border-color .15s, box-shadow .15s; cursor: pointer; }
  .card-item:hover { border-color: var(--accent); box-shadow: 0 4px 16px rgba(106,169,255,.08); }
  .card-item .card-top { display: flex; align-items: flex-start; gap: 10px; }
  .card-item .card-id { font-family: var(--mono); font-size: 11px; color: var(--accent);
    background: var(--accent-dim); padding: 3px 8px; border-radius: 6px; white-space: nowrap;
    flex-shrink: 0; font-weight: 600; }
  .card-item .card-title { font-size: 14px; font-weight: 600; line-height: 1.35; flex: 1; }
  .card-item .card-desc { font-size: 12px; color: var(--muted); line-height: 1.4;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .card-item .card-meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .card-item .tag { font-size: 11px; padding: 3px 8px; border-radius: 6px;
    background: var(--panel-2); color: var(--muted); font-weight: 500; }
  .card-item .tag.col-todo { background: rgba(106,169,255,.12); color: var(--accent); }
  .card-item .tag.col-doing { background: rgba(255,180,84,.12); color: var(--warn); }
  .card-item .tag.col-done { background: rgba(61,220,132,.12); color: var(--ok); }
  .card-item .tag.col-review { background: rgba(200,130,255,.12); color: #c882ff; }
  .card-item .tag.col-backlog { background: var(--panel-2); color: var(--muted); }
  .card-item .card-assignees { display: flex; gap: 4px; flex-wrap: wrap; }
  .card-item .assignee { font-size: 11px; background: var(--panel-2); border: 1px solid var(--border);
    padding: 2px 8px; border-radius: 12px; color: var(--fg); }
  .card-item .card-priority { font-family: var(--mono); font-size: 11px; color: var(--warn); }

  .empty-state { text-align: center; padding: 60px 20px; color: var(--muted); }
  .empty-state .emoji { font-size: 48px; margin-bottom: 12px; }
  .empty-state p { font-size: 14px; max-width: 360px; margin: 0 auto; line-height: 1.5; }

  .detail-panel { display: none; position: fixed; top: 0; right: 0; bottom: 0;
    width: min(480px, 90vw); background: var(--panel); border-left: 1px solid var(--border);
    z-index: 950; box-shadow: -8px 0 40px rgba(0,0,0,.3); flex-direction: column;
    overflow-y: auto; padding: 24px; }
  .detail-panel.open { display: flex; }
  .detail-panel .dp-close { position: absolute; top: 14px; right: 14px; background: var(--panel-2);
    border: 1px solid var(--border); color: var(--fg); cursor: pointer; width: 32px; height: 32px;
    border-radius: 8px; font-size: 16px; display: flex; align-items: center; justify-content: center; }
  .detail-panel h2 { font-size: 18px; margin-bottom: 8px; padding-right: 40px; }
  .detail-panel .dp-id { font-family: var(--mono); color: var(--accent); font-size: 12px; margin-bottom: 12px; }
  .detail-panel .dp-field { margin-bottom: 14px; }
  .detail-panel .dp-label { font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
    color: var(--muted); margin-bottom: 4px; font-weight: 600; }
  .detail-panel .dp-value { font-size: 13px; line-height: 1.5; }
  .detail-panel .dp-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; padding-top: 14px;
    border-top: 1px solid var(--border); }
  .detail-panel .dp-btn { padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--panel-2); color: var(--fg); cursor: pointer; font-size: 12px; font-weight: 600; }
  .detail-panel .dp-btn:hover { border-color: var(--accent); color: var(--accent); }
  .detail-panel .dp-btn.primary { background: var(--accent); color: #0f1115; border-color: var(--accent); }

  .detail-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 940; }
  .detail-overlay.open { display: block; }

  @media (max-width: 900px) {
    .sidebar { position: fixed; top: 0; left: 0; bottom: 0; z-index: 1000;
      box-shadow: 8px 0 40px rgba(0,0,0,.4); }
    .sidebar.closed { margin-left: calc(-1 * var(--sidebar-w) - 10px); }
    .main-toolbar .hamburger { display: flex; align-items: center; justify-content: center; }
    .sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.35); z-index: 999; }
    .sidebar-overlay.open { display: block; }
  }
  @media (max-width: 600px) {
    .cards-grid { grid-template-columns: 1fr; }
    .main-toolbar { padding: 10px 14px; gap: 8px; }
    .cards-area { padding: 14px; }
  }

  .loading-spinner { display: flex; align-items: center; justify-content: center;
    padding: 60px; color: var(--muted); gap: 10px; }
  .loading-spinner::before { content: ""; width: 18px; height: 18px; border: 2px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
__APP_NAV__
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>
<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <h2>📋 Boards</h2>
      <button class="close-btn" onclick="toggleSidebar()" title="Fechar menu">✕</button>
    </div>
    <div class="sidebar-body" id="sidebar-boards">
      <div class="loading-spinner">Carregando...</div>
    </div>
  </aside>
  <div class="main-content">
    <div class="main-toolbar">
      <button class="hamburger" onclick="toggleSidebar()" title="Menu lateral">☰</button>
      <h1 id="page-title">MCP Cards</h1>
      <input type="search" class="search" id="search-input" placeholder="Buscar cards...">
      <select class="filter-select" id="filter-column">
        <option value="">Todas colunas</option>
        <option value="backlog">Backlog</option>
        <option value="todo">A fazer</option>
        <option value="doing">Em progresso</option>
        <option value="testes-usabilidade">Testes</option>
        <option value="review">Em revisão</option>
        <option value="done">Concluído</option>
      </select>
      <span class="stats" id="stats-label">—</span>
    </div>
    <div class="cards-area" id="cards-area">
      <div class="loading-spinner">Carregando cards...</div>
    </div>
  </div>
</div>
<div class="detail-overlay" id="detail-overlay" onclick="closeDetail()"></div>
<div class="detail-panel" id="detail-panel">
  <button class="dp-close" onclick="closeDetail()">✕</button>
  <div id="detail-content"></div>
</div>
<script>
(function() {
  let allBoards = [], currentCards = [], selectedBoard = null;
  function esc(s) { const d = document.createElement("div"); d.textContent = s||""; return d.innerHTML; }
  function colClass(col) {
    col = (col||"").toLowerCase();
    if (col === "todo" || col === "a fazer") return "todo";
    if (col === "doing" || col === "em progresso") return "doing";
    if (col === "done" || col.startsWith("conclu")) return "done";
    if (col === "review" || col.startsWith("em revis")) return "review";
    if (col === "backlog") return "backlog";
    return "";
  }
  window.toggleSidebar = function() {
    document.getElementById("sidebar").classList.toggle("closed");
    document.getElementById("sidebar-overlay").classList.toggle("open",
      !document.getElementById("sidebar").classList.contains("closed"));
  };
  window.closeDetail = function() {
    document.getElementById("detail-panel").classList.remove("open");
    document.getElementById("detail-overlay").classList.remove("open");
  };
  function showDetail(card) {
    const dp = document.getElementById("detail-panel");
    const assignees = (card.assignees||[]).map(a => '<span class="assignee">'+esc(a)+'</span>').join(" ");
    const skills = (card.skills||[]).map(s => '<span class="tag">'+esc(s)+'</span>').join(" ");
    document.getElementById("detail-content").innerHTML =
      '<h2>'+esc(card.title)+'</h2>'+
      '<div class="dp-id">'+esc(card.id)+'</div>'+
      '<div class="dp-field"><div class="dp-label">Coluna</div><div class="dp-value"><span class="tag col-'+colClass(card.column)+'">'+esc(card.column)+'</span></div></div>'+
      (card.description?'<div class="dp-field"><div class="dp-label">Descrição</div><div class="dp-value">'+esc(card.description)+'</div></div>':'')+
      (card.notes?'<div class="dp-field"><div class="dp-label">Notas</div><div class="dp-value" style="white-space:pre-wrap">'+esc(card.notes)+'</div></div>':'')+
      (card.priority!=null?'<div class="dp-field"><div class="dp-label">Prioridade</div><div class="dp-value card-priority">⚡ '+card.priority+'</div></div>':'')+
      ((card.assignees||[]).length?'<div class="dp-field"><div class="dp-label">Responsáveis</div><div class="dp-value card-assignees">'+assignees+'</div></div>':'')+
      ((card.skills||[]).length?'<div class="dp-field"><div class="dp-label">Skills</div><div class="dp-value card-meta">'+skills+'</div></div>':'')+
      (card.workdir?'<div class="dp-field"><div class="dp-label">Workdir</div><div class="dp-value" style="font-family:var(--mono);font-size:12px;word-break:break-all">'+esc(card.workdir)+'</div></div>':'')+
      (card.created_at?'<div class="dp-field"><div class="dp-label">Criado em</div><div class="dp-value">'+esc(String(card.created_at))+'</div></div>':'')+
      (card.completed_at?'<div class="dp-field"><div class="dp-label">Concluído em</div><div class="dp-value">'+esc(String(card.completed_at))+'</div></div>':'')+
      '<div class="dp-actions">'+
        '<button class="dp-btn" onclick="moveCard(\''+esc(card.id)+'\',\'doing\')">→ Em progresso</button>'+
        '<button class="dp-btn" onclick="moveCard(\''+esc(card.id)+'\',\'review\')">→ Revisão</button>'+
        '<button class="dp-btn primary" onclick="moveCard(\''+esc(card.id)+'\',\'done\')">✓ Concluir</button>'+
      '</div>';
    dp.classList.add("open");
    document.getElementById("detail-overlay").classList.add("open");
  }
  async function apiFetch(url, opt, ms) {
    const ctrl = new AbortController();
    const timer = setTimeout(function() { ctrl.abort(); }, ms != null ? ms : 20000);
    try {
      const res = await fetch(url, Object.assign({
        credentials: "same-origin",
        cache: "no-cache",
        headers: { "Accept": "application/json" }
      }, opt || {}, { signal: ctrl.signal }));
      if (!res.ok) throw new Error("HTTP " + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }
  window.moveCard = async function(cardId, column) {
    if (!selectedBoard) return;
    try {
      const data = await apiFetch("/api/mcp-cards/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workdir: selectedBoard.workdir, card_id: cardId, column })
      });
      if (data.ok) { closeDetail(); loadCards(selectedBoard); if(window.qcmNavToast) window.qcmNavToast("Card movido para "+column,"success"); }
      else { if(window.qcmNavToast) window.qcmNavToast(data.error||"Erro","error"); }
    } catch(e) { if(window.qcmNavToast) window.qcmNavToast("Erro: "+e.message,"error"); }
  };
  window.deleteBoard = async function(board, ev) {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    const label = board.team_id || board.workdir.split("/").pop();
    if (!confirm("Apagar o board \"" + label + "\"?\n\nO arquivo kanban.yaml será removido. Esta ação não pode ser desfeita.")) return;
    try {
      const data = await apiFetch("/api/mcp-cards/boards", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workdir: board.workdir, team_id: board.team_id || "" })
      });
      if (!data.ok) { if(window.qcmNavToast) window.qcmNavToast(data.error||"Erro ao apagar","error"); return; }
      if(window.qcmNavToast) window.qcmNavToast("Board apagado","success");
      closeDetail();
      const idx = allBoards.findIndex(b => b.kanban_file === board.kanban_file);
      if (idx >= 0) allBoards.splice(idx, 1);
      if (selectedBoard && selectedBoard.kanban_file === board.kanban_file) {
        selectedBoard = allBoards.length ? allBoards[Math.min(idx, allBoards.length - 1)] : null;
        if (selectedBoard) loadCards(selectedBoard);
        else renderEmpty();
      }
      renderSidebar();
    } catch(e) { if(window.qcmNavToast) window.qcmNavToast("Erro: "+e.message,"error"); }
  };
  async function loadBoards() {
    const sidebar = document.getElementById("sidebar-boards");
    try {
      const data = await apiFetch("/api/mcp-cards/boards?quick=1");
      if (data.ok === false) throw new Error(data.error || "Falha ao listar boards");
      allBoards = data.boards || [];
      allBoards.sort((a, b) => String(a.team_id || a.workdir).localeCompare(String(b.team_id || b.workdir)));
      renderSidebar();
      if (allBoards.length) { selectedBoard = allBoards[0]; loadCards(allBoards[0]); }
      else renderEmpty();
    } catch(e) {
      const msg = (e && e.name === "AbortError") ? "Tempo esgotado ao carregar boards" : (e.message || "Falha de rede");
      sidebar.innerHTML = '<div class="empty-state"><p>Erro: '+esc(msg)+'</p><p style="margin-top:10px;font-size:12px">Recarregue a página ou reinicie o gois.</p></div>';
      document.getElementById("cards-area").innerHTML = '<div class="empty-state"><p>Não foi possível carregar os cards.</p></div>';
    }
  }
  function renderSidebar() {
    const el = document.getElementById("sidebar-boards");
    if (!allBoards.length) { el.innerHTML = '<div class="empty-state" style="padding:30px"><p>Nenhum board com cards</p></div>'; return; }
    let html = '<div class="sidebar-section">Boards com cards</div>';
    for (const b of allBoards) {
      const label = b.team_id || b.workdir.split("/").pop();
      const active = selectedBoard && selectedBoard.kanban_file === b.kanban_file ? " active" : "";
      const badge = b.total_cards > 0 ? String(b.total_cards) : "…";
      html += '<a class="sidebar-item'+active+'" data-kf="'+esc(b.kanban_file)+'" title="'+esc(b.workdir)+'">'+
        '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(label)+'</span>'+
        '<button type="button" class="sidebar-delete" title="Apagar board" data-del-kf="'+esc(b.kanban_file)+'">🗑</button>'+
        '<span class="badge'+(b.total_cards===0?' zero':'')+'">'+badge+'</span></a>';
    }
    el.innerHTML = html;
    el.querySelectorAll(".sidebar-item").forEach(item => {
      item.onclick = function() { const kf = this.getAttribute("data-kf"); const board = allBoards.find(b=>b.kanban_file===kf); if(board){selectedBoard=board;renderSidebar();loadCards(board);if(window.innerWidth<=900){document.getElementById("sidebar").classList.add("closed");document.getElementById("sidebar-overlay").classList.remove("open");}} };
    });
    el.querySelectorAll(".sidebar-delete").forEach(btn => {
      btn.onclick = function(ev) { const kf = this.getAttribute("data-del-kf"); const board = allBoards.find(b=>b.kanban_file===kf); if(board) deleteBoard(board, ev); };
    });
  }
  async function loadCards(board) {
    const area = document.getElementById("cards-area");
    area.innerHTML = '<div class="loading-spinner">Carregando...</div>';
    document.getElementById("page-title").textContent = board.team_id || board.workdir.split("/").pop();
    try {
      const data = await apiFetch("/api/mcp-cards/cards?workdir="+encodeURIComponent(board.workdir));
      if (data.ok === false) throw new Error(data.error || "Falha ao carregar cards");
      currentCards = data.cards || [];
      board.total_cards = currentCards.length;
      renderSidebar();
      renderCards();
    } catch(e) { area.innerHTML = '<div class="empty-state"><p>Erro: '+esc(e.message)+'</p></div>'; }
  }
  function renderEmpty() {
    document.getElementById("cards-area").innerHTML = '<div class="empty-state"><div class="emoji">📭</div><p>Nenhum board com cards encontrado.</p></div>';
  }
  function renderCards() {
    const area = document.getElementById("cards-area");
    const query = (document.getElementById("search-input").value||"").toLowerCase().trim();
    const colFilter = document.getElementById("filter-column").value;
    let cards = currentCards;
    if (colFilter) cards = cards.filter(c=>(c.column||"").toLowerCase()===colFilter);
    if (query) cards = cards.filter(c=>((c.title||"")+" "+(c.description||"")+" "+(c.id||"")+" "+(c.assignees||[]).join(" ")).toLowerCase().includes(query));
    document.getElementById("stats-label").textContent = cards.length+" de "+currentCards.length+" cards";
    if (!cards.length) { area.innerHTML = '<div class="empty-state"><div class="emoji">🔍</div><p>Nenhum card encontrado.</p></div>'; return; }
    let html = '<div class="cards-grid">';
    for (const c of cards) {
      const assignees = (c.assignees||[]).map(a=>'<span class="assignee">'+esc(a)+'</span>').join("");
      html += '<div class="card-item" data-id="'+esc(c.id)+'"><div class="card-top"><span class="card-id">'+esc(c.id)+'</span><span class="card-title">'+esc(c.title)+'</span></div>'+
        (c.description?'<div class="card-desc">'+esc(c.description)+'</div>':'')+
        '<div class="card-meta"><span class="tag col-'+colClass(c.column)+'">'+esc(c.column)+'</span>'+
        (c.priority!=null?'<span class="card-priority">⚡'+c.priority+'</span>':'')+
        assignees+'</div></div>';
    }
    html += '</div>';
    area.innerHTML = html;
    area.querySelectorAll(".card-item").forEach(el => { el.onclick = function() { const card = currentCards.find(c=>c.id===this.getAttribute("data-id")); if(card) showDetail(card); }; });
  }
  document.getElementById("search-input").addEventListener("input", renderCards);
  document.getElementById("filter-column").addEventListener("change", renderCards);
  loadBoards();
})();
</script>
</body>
</html>
"""


def build_mcp_cards_html(hermes_dashboard_url: Optional[str] = None) -> str:
    return _inject_app_nav(_MCP_CARDS_PAGE, "mcp_cards", hermes_dashboard_url=hermes_dashboard_url)

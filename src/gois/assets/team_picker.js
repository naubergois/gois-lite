/* Shared team picker — expects host page shims (chat or kanban). */
let teamPickerState = {
  sessionKey: null,
  onAssign: null,
  required: false,
  selectedTeamId: null,
  filter: "",
  previewCache: {},
  articlesPreview: {
    wsId: null,
    selectedWsId: null,
    articles: [],
    selectedId: null,
    normas: [],
    selectedNorma: null,
  },
};
let teamPickerPreviewLoadSeq = 0;

function closeTeamPickerDialog() {
  const overlay = el("team-picker-overlay");
  if (overlay) overlay.hidden = true;
  teamPickerState.sessionKey = null;
  teamPickerState.onAssign = null;
  teamPickerState.required = false;
  teamPickerState.selectedTeamId = null;
}

function formatTeamModifiedAt(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch (_) {
    return String(iso);
  }
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / (1024 * 1024)).toFixed(1) + " MB";
}

function renderTeamPickerList() {
  const listEl = el("team-picker-list");
  if (!listEl) return;
  const q = (teamPickerState.filter || "").trim().toLowerCase();
  const teams = chatTeams.filter((t) => {
    if (!q) return true;
    const name = String(t.name || "").toLowerCase();
    const id = String(t.id || "").toLowerCase();
    return name.includes(q) || id.includes(q);
  });
  if (!teams.length) {
    listEl.innerHTML = '<div class="team-picker-preview-empty">'
      + (q ? "Nenhum time corresponde à busca." : "Nenhum time encontrado. Crie um abaixo.")
      + "</div>";
    return;
  }
  listEl.innerHTML = teams.map((t) => {
    const active = t.id === teamPickerState.selectedTeamId ? " active" : "";
    const desc = t.description ? escapeHtml(String(t.description).slice(0, 60)) : "";
    return '<button type="button" class="team-picker-item' + active + '" data-team-id="' + escapeHtml(t.id) + '">'
      + '<div class="team-picker-item-name">' + escapeHtml(t.name || t.id) + "</div>"
      + (desc ? '<div class="team-picker-item-meta">' + desc + "</div>" : "")
      + "</button>";
  }).join("");
}

async function loadTeamPickerPreview(teamId) {
  const preview = el("team-picker-preview");
  const selectBtn = el("team-picker-select-btn");
  if (!preview) return;
  const loadSeq = ++teamPickerPreviewLoadSeq;
  teamPickerState.selectedTeamId = teamId || null;
  renderTeamPickerList();
  if (selectBtn) selectBtn.disabled = !teamId;
  if (!teamId) {
    preview.innerHTML = '<div class="team-picker-preview-empty">Selecione um time à esquerda para ver o contexto completo.</div>';
    return;
  }
  const cached = teamPickerState.previewCache[teamId];
  if (cached && (Date.now() - cached._ts < 120000)) {
    preview.innerHTML = renderTeamPickerPreviewHtml(cached);
    bindTeamPickerManage(teamId);
    return;
  }
  const teamRow = chatTeams.find((t) => String(t.id) === String(teamId));
  const teamLabel = escapeHtml((teamRow && teamRow.name) || teamId);
  preview.innerHTML = '<div style="margin-bottom:12px;"><div style="font-size:16px;font-weight:700;">'
    + teamLabel + '</div></div><div class="team-picker-preview-loading">A carregar detalhes…</div>';
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamId) + "/context?quick=1",
      { method: "GET", background: true },
      20000
    );
    if (loadSeq !== teamPickerPreviewLoadSeq || teamPickerState.selectedTeamId !== teamId) return;
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      preview.innerHTML = '<div class="team-picker-preview-empty">' + escapeHtml(data.error || "Falha ao carregar") + "</div>";
      return;
    }
    data._ts = Date.now();
    teamPickerState.previewCache[teamId] = data;
    preview.innerHTML = renderTeamPickerPreviewHtml(data);
    bindTeamPickerManage(teamId);
  } catch (e) {
    if (loadSeq !== teamPickerPreviewLoadSeq || teamPickerState.selectedTeamId !== teamId) return;
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    preview.innerHTML = '<div class="team-picker-preview-empty">Erro: ' + escapeHtml(e.message) + "</div>";
  }
}

function renderTeamPickerPreviewHtml(ctx) {
  const team = ctx.team || {};
  const kanban = ctx.kanban_summary || {};
  const totalCards = kanban.total_tasks != null ? kanban.total_tasks : 0;
  const members = ctx.members || [];
  const contacts = ctx.contacts || [];
  const waGroups = ctx.whatsapp_groups || [];
  const files = ctx.recent_files || [];
  const normas = ctx.normas || [];
  const articles = ctx.articles || [];
  const articlesCount = ctx.articles_count != null ? ctx.articles_count : articles.length;
  let html = "";

  html += '<div style="margin-bottom:12px;">';
  html += '<div style="font-size:16px;font-weight:700;">' + escapeHtml(team.name || team.id || "—") + "</div>";
  if (team.description) {
    html += '<div style="font-size:12px;color:var(--muted);margin-top:4px;">' + escapeHtml(team.description) + "</div>";
  }
  html += "</div>";

  html += '<div class="team-picker-stats">';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + totalCards + '</div><div class="team-picker-stat-lbl">Cards</div></div>';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + members.length + '</div><div class="team-picker-stat-lbl">Integrantes</div></div>';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + waGroups.length + '</div><div class="team-picker-stat-lbl">Grupos WA</div></div>';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + files.length + '</div><div class="team-picker-stat-lbl">Arquivos</div></div>';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + articlesCount + '</div><div class="team-picker-stat-lbl">Artigos</div></div>';
  html += '<div class="team-picker-stat"><div class="team-picker-stat-val">' + normas.length + '</div><div class="team-picker-stat-lbl">Normas</div></div>';
  html += "</div>";

  if (kanban.columns && Object.keys(kanban.columns).length) {
    html += '<div class="team-picker-section"><h4>Kanban por coluna</h4><div class="team-picker-kanban-cols">';
    for (const [colName, count] of Object.entries(kanban.columns)) {
      html += '<div class="team-picker-kanban-col"><strong>' + count + "</strong>" + escapeHtml(colName) + "</div>";
    }
    html += "</div></div>";
  }

  if (members.length) {
    html += '<div class="team-picker-section"><h4>Integrantes</h4><div class="team-picker-chips">';
    for (const m of members) {
      const label = m.username || m.email || m.user_id || "—";
      html += '<span class="team-picker-chip' + (m.is_owner ? " owner" : "") + '">'
        + escapeHtml(label) + (m.is_owner ? " 👑" : "") + "</span>";
    }
    html += "</div></div>";
  }

  if (contacts.length) {
    html += '<div class="team-picker-section"><h4>Contatos</h4><div class="team-picker-chips">';
    for (const c of contacts) {
      const name = c.name || c.email || c.whatsapp || c.id || "—";
      html += '<span class="team-picker-chip">' + escapeHtml(name) + (c.whatsapp ? " 📱" : "") + "</span>";
    }
    html += "</div></div>";
  }

  html += renderTeamPickerManageHtml(team, contacts, normas, ctx.whatsapp_allowlist || []);

  if (waGroups.length) {
    html += '<div class="team-picker-section"><h4>Grupos WhatsApp</h4>';
    for (const g of waGroups) {
      html += '<div class="team-picker-wa-group">';
      html += '<h5>' + escapeHtml(g.name || "Grupo") + "</h5>";
      html += '<div class="team-picker-wa-meta">' + (g.member_count || 0) + " membros</div>";
      if (g.members && g.members.length) {
        html += '<div class="team-picker-chips">';
        for (const name of g.members.slice(0, 16)) {
          html += '<span class="team-picker-chip">' + escapeHtml(name) + "</span>";
        }
        if (g.members.length > 16) {
          html += '<span class="team-picker-chip" style="opacity:.6;">+' + (g.members.length - 16) + "</span>";
        }
        html += "</div>";
      }
      html += "</div>";
    }
    html += "</div>";
  }

  if (files.length) {
    const ctxEligibleCount = files.filter((f) => isContextDocumentName(f.name || f.relative_path || "")).length;
    html += '<div class="team-picker-section">';
    html += '<div class="team-picker-section-head"><h4>Arquivos recentes</h4>';
    if (ctxEligibleCount && (team.id || teamPickerState.selectedTeamId)) {
      html += '<button type="button" class="team-picker-file-ctx team-picker-section-action" data-team-recent-ctx-all'
        + ' title="Extrair texto (PDF/Word incl.) e fixar todos os arquivos elegíveis no contexto desta conversa">'
        + "📎 Indexar no contexto</button>";
    }
    html += "</div><div class=\"team-picker-files\">";
    for (const f of files) {
      const meta = [
        f.root_label || "",
        f.size_bytes ? formatBytes(f.size_bytes) : "",
        formatTeamModifiedAt(f.modified_at),
      ].filter(Boolean).join(" · ");
      html += '<div class="team-picker-file">';
      html += '<div class="team-picker-file-info">';
      html += '<div class="team-picker-file-name" title="' + escapeHtml(f.relative_path || f.name || "") + '">'
        + escapeHtml(f.name || f.relative_path || "arquivo") + "</div>";
      if (meta) html += '<div class="team-picker-file-meta">' + escapeHtml(meta) + "</div>";
      html += "</div>";
      html += renderTeamFileActionButtons(f, team.id || teamPickerState.selectedTeamId || "");
      html += "</div>";
    }
    html += "</div></div>";
  }

  if (articles.length) {
    html += '<div class="team-picker-section"><h4>Artigos LaTeX (' + articlesCount + ')</h4><div class="team-picker-files">';
    for (const a of articles.slice(0, 12)) {
      const meta = [
        a.workspace_name || a.workspace_id || "",
        a.has_pdf ? "PDF" : "sem PDF",
      ].filter(Boolean).join(" · ");
      html += '<div class="team-picker-file">';
      html += '<div class="team-picker-file-info">';
      html += '<div class="team-picker-file-name" title="' + escapeHtml(a.id || "") + '">'
        + escapeHtml(a.title || a.id || "artigo") + "</div>";
      if (meta) html += '<div class="team-picker-file-meta">' + escapeHtml(meta) + "</div>";
      html += "</div></div>";
    }
    if (articles.length > 12) {
      html += '<div class="team-picker-file" style="color:var(--muted);font-size:11px;padding:10px 12px;">… e mais '
        + (articles.length - 12) + " artigo(s) na secção de gestão abaixo.</div>";
    }
    html += "</div></div>";
  }

  if (normas.length) {
    html += '<div class="team-picker-section"><h4>Normas do time</h4><div class="team-picker-files">';
    for (const n of normas) {
      const meta = [
        n.mime_type || "",
        n.size_bytes ? formatBytes(n.size_bytes) : "",
        formatTeamModifiedAt(n.modified_at),
        teamNormaIndexLabel(n),
      ].filter(Boolean).join(" · ");
      html += '<div class="team-picker-file">';
      html += '<div class="team-picker-file-info">';
      html += '<div class="team-picker-file-name team-picker-norma-open" data-preview-norma="'
        + escapeHtml(n.name || "") + '" title="Clique para preview">'
        + escapeHtml(n.name || "norma") + "</div>";
      if (meta) html += '<div class="team-picker-file-meta">' + escapeHtml(meta) + "</div>";
      html += "</div>";
      const tid = team.id || teamPickerState.selectedTeamId || "";
      if (isContextDocumentName(n.name || "") && tid) {
        html += '<button type="button" class="team-picker-file-ctx" data-team-id="' + escapeHtml(tid)
          + '" data-team-file-ctx="'
          + escapeHtml(n.relative_path || ("normas/" + (n.name || ""))) + '" data-team-file-name="'
          + escapeHtml(n.name || "norma") + '" title="Extrair texto e fixar no contexto desta conversa">📎 Contexto</button>';
      }
      if (tid && (n.name || "")) {
        const normaDl = teamNormaDownloadUrl(tid, n.name);
        html += '<button type="button" class="team-picker-file-dl" data-team-file-dl="'
          + escapeHtml(normaDl) + '" data-team-file-name="' + escapeHtml(n.name || "norma")
          + '" title="Pré-visualizar e baixar">⬇ Baixar</button>';
      }
      html += "</div>";
    }
    html += "</div></div>";
  }

  if (!members.length && !waGroups.length && !files.length && !normas.length && !articles.length && !totalCards) {
    html += '<div class="team-picker-preview-empty" style="padding:24px 0;">Pouco contexto disponível para este time.</div>';
  }
  return html;
}

function suggestTeamPickerProjectFolderName(team) {
  const raw = String((team && (team.name || team.id)) || "").trim();
  if (!raw) return "";
  return raw.replace(/[<>:"/\\|?*\x00-\x1f]/g, "").trim().slice(0, 64);
}

function joinTeamPickerPath(parent, child) {
  const base = String(parent || "").replace(/\/+$/, "");
  const name = String(child || "").trim().replace(/^\/+/, "");
  if (!name) return base;
  if (!base) return "/" + name;
  return base + "/" + name;
}

function updateTeamPickerProjectPathPreview() {
  const input = el("team-picker-local-path");
  const nameInput = el("team-picker-project-new-name");
  if (!input) return;
  const newName = (nameInput && nameInput.value || "").trim();
  const browse = String(teamPickerPathBrowse || "").trim();
  const current = (input.value || "").trim();
  if (newName && browse) {
    input.value = joinTeamPickerPath(browse, newName);
  } else if (!current && browse) {
    input.value = browse;
  }
}

function resolveTeamPickerProjectPath(createDir) {
  const input = el("team-picker-local-path");
  const nameInput = el("team-picker-project-new-name");
  const manual = (input && input.value || "").trim();
  const newName = (nameInput && nameInput.value || "").trim();
  const browse = String(teamPickerPathBrowse || "").trim();
  if (!createDir) return manual;
  if (newName) {
    const parent = browse || manual || "";
    const target = joinTeamPickerPath(parent, newName);
    if (input) input.value = target;
    return target;
  }
  return manual;
}

function teamAllowlistBadgeHtml(flags) {
  const teamOk = !!(flags && flags.in_team_allowlist);
  const globalOk = !!(flags && (flags.in_global_allowlist || flags.in_allowlist));
  const canSend = !!(flags && (flags.can_send_whatsapp || teamOk));
  let html = '<div class="team-picker-contact-badges">';
  html += '<span class="team-picker-allow-badge ' + (teamOk ? "ok" : "warn") + '">time ' + (teamOk ? "✓" : "✗") + "</span>";
  if (globalOk) {
    html += '<span class="team-picker-allow-badge ok">geral ✓</span>';
  }
  html += '<span class="team-picker-allow-badge ' + (canSend ? "ok" : "bad") + '">envio ' + (canSend ? "ok" : "bloq") + "</span>";
  html += "</div>";
  return html;
}

function renderTeamPickerWaAllowlistHtml(rows) {
  if (!rows || !rows.length) {
    return '<div class="team-picker-contact-empty">Nenhum destino WhatsApp no time. Use os botões abaixo para cadastrar número ou grupo.</div>';
  }
  return rows.map((row) => {
    const title = row.name || row.dest || row.phone || row.jid || "—";
    const sub = row.kind === "group" ? (row.jid || row.dest || "") : ("+" + (row.phone || row.dest || ""));
    const dest = row.dest || row.jid || row.phone || "";
    return '<div class="team-picker-wa-allow-row">'
      + '<div class="meta"><div class="name">' + escapeHtml(title) + "</div>"
      + '<div class="sub">' + escapeHtml(sub || "—") + "</div>"
      + teamAllowlistBadgeHtml(row)
      + "</div>"
      + '<button type="button" class="ctrl danger" data-remove-wa-allow="' + escapeHtml(dest)
      + '" style="font-size:10px;padding:3px 7px;flex-shrink:0;" title="Remover da allowlist do time">✕</button>'
      + "</div>";
  }).join("");
}

function renderTeamPickerManageHtml(team, contacts, normas, waAllowlist) {
  const normasList = Array.isArray(normas) ? normas : [];
  const teamId = team.id || teamPickerState.selectedTeamId || "";
  const localPath = String(team.local_path || "");
  const suggestName = localPath ? "" : suggestTeamPickerProjectFolderName(team);
  let html = '<div class="team-picker-section team-picker-manage">';
  html += "<h4>📁 Diretório local do projeto</h4>";
  html += '<p class="team-picker-articles-hint">Navegue até a pasta pai, informe o nome da nova pasta e use «Criar e vincular» para criar no disco (com kanban.yaml). «Salvar» vincula um caminho que já existe.</p>';
  html += '<label class="team-picker-articles-label" for="team-picker-project-new-name">Nome da nova pasta</label>';
  html += '<input type="text" id="team-picker-project-new-name" placeholder="ex: Najara" value="'
    + escapeHtml(suggestName) + '" '
    + 'style="width:100%;background:var(--panel-2);color:var(--fg);border:1px solid var(--border);'
    + 'border-radius:8px;padding:7px 10px;font:inherit;font-size:11px;margin-bottom:8px;">';
  html += '<label class="team-picker-articles-label" for="team-picker-local-path">Caminho do projeto</label>';
  html += '<div class="team-picker-path-row">';
  html += '<input type="text" id="team-picker-local-path" placeholder="/caminho/do/projeto" value="'
    + escapeHtml(localPath) + '">';
  html += '<button type="button" class="ctrl" id="team-picker-path-browse">Navegar</button>';
  html += '<button type="button" class="ctrl" id="team-picker-path-save">Salvar</button>';
  html += '<button type="button" class="ctrl primary" id="team-picker-path-create">Criar e vincular</button>';
  html += "</div>";
  html += '<div class="team-picker-path-browser" id="team-picker-path-browser" hidden>';
  html += '<div class="path" id="team-picker-path-current" style="font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:6px;">—</div>';
  html += '<button type="button" class="ctrl entry" id="team-picker-path-up" style="width:100%;margin-bottom:4px;font-size:11px;">⬆ Subir</button>';
  html += '<div id="team-picker-path-folder-list"></div>';
  html += '<button type="button" class="ctrl primary" id="team-picker-path-create-here" '
    + 'style="width:100%;margin-top:8px;font-size:11px;">Criar pasta aqui e vincular</button>';
  html += "</div>";
  html += '<div class="team-picker-path-status" id="team-picker-path-status"></div>';
  html += "</div>";

  const latexWsIds = getTeamLatexWorkspaceIds(team);
  html += '<div class="team-picker-section team-picker-manage">';
  html += "<h4>📄 Artigos do time</h4>";
  html += '<p class="team-picker-articles-hint">Cadastre ou vincule pastas LaTeX — pode associar várias ao mesmo time. Clique num artigo ou use 📎 para incluí-lo no contexto desta conversa. Use «Selecionar pasta» para navegar no disco ou enviar uma pasta do computador.</p>';
  html += '<label class="team-picker-articles-label">Pastas vinculadas</label>';
  html += '<div class="team-picker-articles-linked" id="team-picker-articles-linked" data-team-id="'
    + escapeHtml(teamId) + '">';
  html += renderTeamPickerLinkedWorkspacesHtml(team, []);
  html += "</div>";
  html += '<label class="team-picker-articles-label" for="team-picker-articles-ws">Adicionar pasta existente</label>';
  html += '<select class="team-picker-articles-select" id="team-picker-articles-ws"><option value="">A carregar…</option></select>';
  html += '<div class="team-picker-contact-actions">';
  html += '<button type="button" class="ctrl primary" id="team-picker-articles-save">Adicionar ao time</button>';
  html += '<button type="button" class="ctrl" id="team-picker-articles-folder-dialog">📁 Selecionar pasta</button>';
  html += '<button type="button" class="ctrl" id="team-picker-articles-clear">Remover todas</button>';
  html += "</div>";
  html += '<div class="team-picker-path-status" id="team-picker-articles-status"></div>';
  html += '<div class="team-picker-articles-preview" id="team-picker-articles-preview">';
  html += '<div class="team-picker-articles-list" id="team-picker-articles-list">';
  html += latexWsIds.length
    ? '<div class="team-picker-articles-preview-empty">A carregar artigos…</div>'
    : '<div class="team-picker-articles-preview-empty">Adicione uma pasta ou envie um PDF ao contexto (📎).</div>';
  html += "</div>";
  html += '<div class="team-picker-articles-pdf pdf-viewer-container" id="team-picker-articles-pdf">';
  html += '<div class="pdf-viewer-header">';
  html += '<span class="pdf-title" id="team-picker-pdf-title">Nenhum PDF selecionado</span>';
  html += '<div class="pdf-actions">';
  html += '<button type="button" class="pdf-btn" id="team-picker-pdf-upload" title="Enviar documento (PDF, Word, texto) ao contexto do time">📎</button>';
  html += '<button type="button" class="pdf-btn" id="team-picker-pdf-compile" disabled title="Compilar PDF">⚙️</button>';
  html += '<button type="button" class="pdf-btn" id="team-picker-pdf-log" disabled title="Ver log">📄</button>';
  html += '<button type="button" class="pdf-btn" id="team-picker-pdf-expand" disabled title="Maximizar">🔍</button>';
  html += '<a href="#" target="_blank" rel="noopener" class="pdf-btn" id="team-picker-pdf-open" '
    + 'style="display:none;" title="Abrir em nova aba">↗️</a>';
  html += "</div></div>";
  html += '<pre class="pdf-log-box" id="team-picker-pdf-log-box"></pre>';
  html += '<div class="pdf-empty-state" id="team-picker-pdf-empty">Escolha um artigo ou envie um PDF ao contexto (📎)</div>';
  html += '<iframe class="pdf-iframe" id="team-picker-pdf-frame" style="display:none;" title="Preview PDF do artigo"></iframe>';
  html += "</div></div>";
  html += '<input type="file" id="team-picker-articles-pdf-input" hidden accept=".pdf,application/pdf,.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.md,.txt,.tex,.json,.yaml,.yml,.csv,text/plain,text/markdown">';
  html += "</div>";

  html += '<div class="team-picker-section team-picker-manage">';
  html += "<h4>📋 Normas do time</h4>";
  html += '<p class="team-picker-articles-hint">PDF, Markdown ou texto — ficam na área do time e entram no contexto do chat (vetorial + prompt). Clique em «Enviar norma» para escolher o arquivo.</p>';
  html += '<div class="team-picker-contact-actions">';
  html += '<button type="button" class="ctrl primary" id="team-picker-normas-upload-btn">📎 Enviar norma</button>';
  html += '<button type="button" class="ctrl" id="team-picker-normas-wa-btn">📱 WhatsApp</button>';
  html += '<input type="file" id="team-picker-normas-input" hidden accept=".pdf,application/pdf,.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.md,.txt,.tex,.json,.yaml,.yml,.csv,text/plain,text/markdown">';
  html += "</div>";
  html += '<div class="team-picker-files" id="team-picker-normas-list">';
  html += renderTeamPickerNormasListHtml(normasList);
  html += "</div>";
  html += '<div class="team-picker-path-status" id="team-picker-normas-status"></div>';
  html += "</div>";

  html += '<div class="team-picker-section team-picker-manage">';
  html += "<h4>⚖️ Avaliações jurídicas (PDF)</h4>";
  html += '<p class="team-picker-articles-hint">Pareceres salvos no MongoDB após análise de normas/PDFs. Clique num item para ver o conteúdo.</p>';
  html += '<div class="team-picker-contact-actions">';
  html += '<button type="button" class="ctrl" id="team-picker-legal-eval-refresh">↻ Atualizar</button>';
  html += "</div>";
  html += '<div class="team-picker-files" id="team-picker-legal-eval-list">';
  html += '<div class="team-picker-contact-empty">A carregar avaliações…</div>';
  html += "</div>";
  html += '<pre class="team-picker-legal-eval-detail" id="team-picker-legal-eval-detail" hidden></pre>';
  html += '<div class="team-picker-path-status" id="team-picker-legal-eval-status"></div>';
  html += "</div>";

  html += '<div class="team-picker-section team-picker-manage">';
  html += "<h4>📱 WhatsApp — allowlist do time</h4>";
  html += '<p class="team-picker-articles-hint">Nesta conversa, WhatsApp só pode ser enviado para destinos na allowlist do time. Cadastre números ou grupos abaixo.</p>';
  html += '<div class="team-picker-contact-actions">';
  html += '<button type="button" class="ctrl primary" id="team-picker-add-wa-allow">🔍 Buscar na agenda</button>';
  html += '<button type="button" class="ctrl" id="team-picker-add-wa-allow-manual">+ Número manual</button>';
  html += "</div>";
  html += '<div class="team-picker-wa-allowlist" id="team-picker-wa-allowlist" data-team-id="'
    + escapeHtml(teamId) + '">';
  html += renderTeamPickerWaAllowlistHtml(waAllowlist || []);
  html += "</div>";
  html += '<div class="team-picker-path-status" id="team-picker-wa-allowlist-status"></div></div>';

  html += '<div class="team-picker-section team-picker-manage">';
  html += "<h4>👥 Contatos do time</h4>";
  html += '<div class="team-picker-contact-actions">';
  html += '<button type="button" class="ctrl primary" id="team-picker-add-contact">🔍 Buscar na agenda</button>';
  html += '<button type="button" class="ctrl" id="team-picker-add-contact-manual">+ Manual</button>';
  html += "</div>";
  html += '<div class="team-picker-contacts" id="team-picker-contacts-list" data-team-id="'
    + escapeHtml(teamId) + '">';
  html += renderTeamPickerContactsListHtml(contacts || []);
  html += "</div></div>";
  return html;
}

function renderTeamPickerContactsListHtml(contacts) {
  if (!contacts || !contacts.length) {
    return '<div class="team-picker-contact-empty">Nenhum contato. Use a busca na agenda ou adicione manualmente.</div>';
  }
  return contacts.map((c) => {
    const title = c.name || c.email || c.whatsapp || c.phone || "?";
    const sub = [c.email, c.whatsapp || c.phone, c.role].filter(Boolean).join(" · ");
    const cid = c.id || c.email || "";
    const badges = (c.whatsapp || c.phone) ? teamAllowlistBadgeHtml(c) : "";
    return '<div class="team-picker-contact-row">'
      + '<div class="meta"><div class="name">' + escapeHtml(title) + "</div>"
      + '<div class="sub">' + escapeHtml(sub || "—") + "</div>"
      + badges
      + "</div>"
      + '<button type="button" class="ctrl danger" data-remove-team-contact="' + escapeHtml(cid)
      + '" style="font-size:10px;padding:3px 7px;">✕</button></div>';
  }).join("");
}

function renderTeamPickerNormasListHtml(normas) {
  if (!normas || !normas.length) {
    return '<div class="team-picker-contact-empty">Nenhuma norma. Envie PDF ou Markdown acima.</div>';
  }
  return normas.map((n) => {
    const meta = [
      n.mime_type || "",
      n.size_bytes ? formatBytes(n.size_bytes) : "",
      formatTeamModifiedAt(n.modified_at),
      teamNormaIndexLabel(n),
    ].filter(Boolean).join(" · ");
    return '<div class="team-picker-file">'
      + '<div class="team-picker-file-info team-picker-norma-open" data-preview-norma="'
      + escapeHtml(n.name || "") + '" title="Clique para preview">'
      + '<div class="team-picker-file-name" title="' + escapeHtml(n.relative_path || n.name || "") + '">'
      + escapeHtml(n.name || "norma") + "</div>"
      + (meta ? '<div class="team-picker-file-meta">' + escapeHtml(meta) + "</div>" : "")
      + "</div>"
      + '<div class="team-picker-file-actions">'
      + (isContextDocumentName(n.name || "") && teamPickerState.selectedTeamId
        ? '<button type="button" class="team-picker-file-ctx" data-team-id="' + escapeHtml(teamPickerState.selectedTeamId)
          + '" data-team-file-ctx="'
          + escapeHtml(n.relative_path || ("normas/" + (n.name || ""))) + '" data-team-file-name="'
          + escapeHtml(n.name || "norma") + '" title="Extrair texto e fixar no contexto desta conversa">📎 Contexto</button>'
        : "")
      + (teamPickerState.selectedTeamId && (n.name || "")
        ? '<button type="button" class="team-picker-file-dl" data-team-file-dl="'
          + escapeHtml(teamNormaDownloadUrl(teamPickerState.selectedTeamId, n.name)) + '" data-team-file-name="'
          + escapeHtml(n.name || "norma") + '" title="Pré-visualizar e baixar">⬇ Baixar</button>'
        : "")
      + '<button type="button" class="ctrl danger" data-remove-team-norma="' + escapeHtml(n.name || "")
      + '" style="font-size:10px;padding:3px 7px;">✕</button></div></div>';
  }).join("");
}

function setTeamPickerLegalEvalStatus(msg, kind) {
  const elStatus = el("team-picker-legal-eval-status");
  if (!elStatus) return;
  elStatus.textContent = msg || "";
  elStatus.className = "team-picker-path-status" + (kind ? " " + kind : "");
}

function renderTeamPickerLegalEvalListHtml(evaluations) {
  if (!evaluations || !evaluations.length) {
    return '<div class="team-picker-contact-empty">Nenhuma avaliação salva para este time.</div>';
  }
  return evaluations.map((ev) => {
    const title = ev.title || ev.source_name || ev.id || "Parecer";
    const meta = [ev.evaluation_type, ev.updated_at || ev.created_at, ev.source_name]
      .filter(Boolean).join(" · ");
    return '<div class="team-picker-file-row team-picker-legal-eval-row" data-legal-eval-id="'
      + escapeHtml(ev.id || "") + '">'
      + '<div class="meta"><div class="name">' + escapeHtml(title) + "</div>"
      + '<div class="sub">' + escapeHtml(meta || "—") + "</div></div>"
      + '<button type="button" class="ctrl" data-open-legal-eval="' + escapeHtml(ev.id || "")
      + '" style="font-size:10px;padding:3px 7px;">Ver</button></div>';
  }).join("");
}

async function loadTeamPickerLegalEvaluations(teamId) {
  const box = el("team-picker-legal-eval-list");
  if (!box || !teamId) return;
  setTeamPickerLegalEvalStatus("", "");
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamId) + "/legal-evaluations",
      { method: "GET" },
      15000,
    );
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      box.innerHTML = '<div class="team-picker-contact-empty">' + escapeHtml(data.error || "falha") + "</div>";
      return;
    }
    const rows = Array.isArray(data.evaluations) ? data.evaluations : [];
    box.innerHTML = renderTeamPickerLegalEvalListHtml(rows);
    box.querySelectorAll("[data-open-legal-eval]").forEach((btn) => {
      btn.onclick = () => {
        void openTeamPickerLegalEvaluation(teamId, btn.getAttribute("data-open-legal-eval") || "");
      };
    });
    setTeamPickerLegalEvalStatus(rows.length ? (rows.length + " avaliação(ões)") : "", "");
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    box.innerHTML = '<div class="team-picker-contact-empty">Erro: ' + escapeHtml(e.message) + "</div>";
  }
}

async function openTeamPickerLegalEvaluation(teamId, evaluationId) {
  const detail = el("team-picker-legal-eval-detail");
  if (!teamId || !evaluationId || !detail) return;
  setTeamPickerLegalEvalStatus("A carregar parecer…", "");
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamId) + "/legal-evaluations/" + encodeURIComponent(evaluationId),
      { method: "GET" },
      15000,
    );
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      throw new Error(data.error || "falha ao carregar");
    }
    const ev = data.evaluation || {};
    const header = [
      ev.title || "",
      ev.evaluation_type ? ("Tipo: " + ev.evaluation_type) : "",
      ev.source_name || ev.source_path || "",
      ev.updated_at || ev.created_at || "",
    ].filter(Boolean).join("\n");
    detail.hidden = false;
    detail.textContent = header + "\n\n" + (ev.content || "");
    setTeamPickerLegalEvalStatus("", "");
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    setTeamPickerLegalEvalStatus("Erro: " + e.message, "err");
  }
}

function bindTeamPickerLegalEvaluations(teamId) {
  const refreshBtn = el("team-picker-legal-eval-refresh");
  if (refreshBtn) {
    refreshBtn.onclick = () => {
      void loadTeamPickerLegalEvaluations(teamPickerActiveTeamId(teamId));
    };
  }
  void loadTeamPickerLegalEvaluations(teamPickerActiveTeamId(teamId));
}

function setTeamPickerNormasStatus(msg, kind) {
  const elStatus = el("team-picker-normas-status");
  if (!elStatus) return;
  elStatus.textContent = msg || "";
  elStatus.className = "team-picker-path-status" + (kind ? " " + kind : "");
}

function invalidateTeamPickerPreviewCache(teamId) {
  if (teamId && teamPickerState.previewCache[teamId]) {
    delete teamPickerState.previewCache[teamId];
  }
  if (teamId && teamContextCache[teamId]) {
    delete teamContextCache[teamId];
  }
}

async function loadTeamPickerNormas(teamId) {
  const box = el("team-picker-normas-list");
  if (!box || !teamId) return;
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", {
      method: "GET",
    }, 15000);
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      box.innerHTML = '<div class="team-picker-contact-empty">' + escapeHtml(data.error || "falha") + "</div>";
      return;
    }
    const normas = Array.isArray(data.normas) ? data.normas : [];
    box.innerHTML = renderTeamPickerNormasListHtml(normas);
    box.querySelectorAll("[data-remove-team-norma]").forEach((btn) => {
      btn.onclick = () => {
        void deleteTeamPickerNorma(teamId, btn.getAttribute("data-remove-team-norma") || "");
      };
    });
    bindTeamFileContextButtons(box, teamId);
    bindTeamPickerNormaPreviewClicks(box, teamId);
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    box.innerHTML = '<div class="team-picker-contact-empty">Erro: ' + escapeHtml(e.message) + "</div>";
  }
}

function teamNormaIndexLabel(n) {
  if (!n) return "sem texto";
  if (n.chromadb_indexed || n.has_text) return "indexado";
  return "sem texto";
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const raw = String(reader.result || "");
      const comma = raw.indexOf(",");
      resolve(comma >= 0 ? raw.slice(comma + 1) : raw);
    };
    reader.onerror = () => reject(reader.error || new Error("falha ao ler arquivo"));
    reader.readAsDataURL(file);
  });
}

async function uploadTeamPickerNorma(teamId, file) {
  if (!teamId || !file) return null;
  setTeamPickerNormasStatus("A enviar " + (file.name || "norma") + "…", "");
  try {
    const dataBase64 = await readFileAsBase64(file);
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: file.name,
        mime_type: file.type || "",
        data_base64: dataBase64,
      }),
    }, 120000);
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return null;
      throw new Error(data.error || "falha no upload");
    }
    const savedName = (data.norma && data.norma.name) || file.name || "";
    setTeamPickerNormasStatus(
      data.warning ? ("⚠ " + data.warning) : "✅ Norma enviada e indexada no contexto do time.",
      data.warning ? "err" : "ok",
    );
    if (data.warning && window.qcmNavToast) window.qcmNavToast(data.warning, "err");
    if (window.qcmNavToast) window.qcmNavToast("Norma adicionada ao time.", "ok");
    invalidateTeamPickerPreviewCache(teamId);
    await loadTeamPickerNormas(teamId);
    if (teamPickerState.selectedTeamId === teamId) {
      await loadTeamPickerPreview(teamId);
    }
    return savedName;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return null;
    setTeamPickerNormasStatus("Erro: " + e.message, "err");
    return null;
  }
}

async function deleteTeamPickerNorma(teamId, name) {
  if (!teamId || !name) return false;
  if (!confirm("Remover a norma \"" + name + "\" do time?")) return false;
  setTeamPickerNormasStatus("A remover…", "");
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }, 20000);
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return false;
      throw new Error(data.error || "falha ao remover");
    }
    setTeamPickerNormasStatus("✅ Norma removida.", "ok");
    invalidateTeamPickerPreviewCache(teamId);
    await loadTeamPickerNormas(teamId);
    if (teamPickerState.selectedTeamId === teamId) {
      await loadTeamPickerPreview(teamId);
    }
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamPickerNormasStatus("Erro: " + e.message, "err");
    return false;
  }
}

function teamPickerActiveTeamId(fallback) {
  return teamPickerState.selectedTeamId || fallback || "";
}

function base64ToBlob(b64, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime || "application/octet-stream" });
}

function bindTeamPickerNormas(teamId) {
  const uploadBtn = el("team-picker-normas-upload-btn");
  const waBtn = el("team-picker-normas-wa-btn");
  const input = el("team-picker-normas-input");
  if (uploadBtn) {
    uploadBtn.onclick = () => {
      const tid = teamPickerActiveTeamId(teamId);
      if (!tid) {
        if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
        return;
      }
      if (input) input.click();
      else openTeamNormaPreview({ teamId: tid, tab: "upload" });
    };
  }
  if (waBtn) {
    waBtn.onclick = () => {
      const tid = teamPickerActiveTeamId(teamId);
      if (!tid) {
        if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
        return;
      }
      openTeamNormaPreview({ teamId: tid, tab: "whatsapp" });
    };
  }
  if (input) {
    input.onchange = () => {
      const file = input.files && input.files[0];
      input.value = "";
      const tid = teamPickerActiveTeamId(teamId);
      if (!file || !tid) return;
      const isPdf = (file.type === "application/pdf") || /\.pdf$/i.test(file.name || "");
      if (isPdf) {
        void (async () => {
          const savedName = await uploadTeamPickerNorma(tid, file);
          if (!savedName) return;
          await loadTeamPickerArticlesPreview(tid);
          selectTeamPickerNorma(savedName);
        })();
        return;
      }
      openTeamNormaPreview({ teamId: tid, tab: "upload", file });
    };
  }
  void loadTeamPickerNormas(teamPickerActiveTeamId(teamId));
}

let teamNormaPreviewState = {
  teamId: "",
  tab: "upload",
  source: "upload",
  file: null,
  blobUrl: "",
  dataBase64: "",
  name: "",
  mimeType: "",
  excerpt: "",
  isPdf: false,
  msgId: "",
  waMessage: null,
  existingName: "",
};

function teamNormaDownloadUrl(teamId, name) {
  const params = new URLSearchParams({ name: name || "" });
  return "/teams/" + encodeURIComponent(teamId) + "/normas/download?" + params.toString();
}

function setTeamNormaPreviewStatus(msg, kind) {
  const box = el("team-norma-preview-status");
  if (!box) return;
  box.textContent = msg || "";
  box.className = "team-norma-preview-status" + (kind ? " " + kind : "");
}

function resetTeamNormaPreviewViewer() {
  const iframe = el("team-norma-preview-pdf");
  const empty = el("team-norma-preview-viewer-empty");
  const excerpt = el("team-norma-preview-excerpt");
  if (teamNormaPreviewState.blobUrl) {
    try { URL.revokeObjectURL(teamNormaPreviewState.blobUrl); } catch (_e) {}
    teamNormaPreviewState.blobUrl = "";
  }
  if (iframe) {
    iframe.removeAttribute("src");
    iframe.style.display = "none";
  }
  if (empty) empty.style.display = "flex";
  if (excerpt) excerpt.textContent = "";
}

function updateTeamNormaPreviewActions() {
  const saveBtn = el("team-norma-preview-save");
  const ctxBtn = el("team-norma-preview-context");
  const ready = !!(teamNormaPreviewState.name && (
    teamNormaPreviewState.dataBase64 || teamNormaPreviewState.existingName || teamNormaPreviewState.msgId
  ));
  if (saveBtn) saveBtn.disabled = !ready;
  if (ctxBtn) ctxBtn.disabled = !ready;
}

function renderTeamNormaPreviewContent() {
  const iframe = el("team-norma-preview-pdf");
  const empty = el("team-norma-preview-viewer-empty");
  const excerpt = el("team-norma-preview-excerpt");
  const nameInput = el("team-norma-preview-name");
  if (nameInput && teamNormaPreviewState.name) nameInput.value = teamNormaPreviewState.name;

  const showPdf = teamNormaPreviewState.isPdf;
  let pdfUrl = "";
  if (showPdf) {
    if (teamNormaPreviewState.blobUrl) pdfUrl = teamNormaPreviewState.blobUrl;
    else if (teamNormaPreviewState.existingName && teamNormaPreviewState.teamId) {
      pdfUrl = teamNormaDownloadUrl(teamNormaPreviewState.teamId, teamNormaPreviewState.existingName) + "&_=" + Date.now();
    }
  }
  if (iframe && pdfUrl) {
    iframe.src = pdfUrl;
    iframe.style.display = "block";
    if (empty) empty.style.display = "none";
  } else if (iframe) {
    iframe.removeAttribute("src");
    iframe.style.display = "none";
    if (empty) {
      empty.textContent = teamNormaPreviewState.excerpt
        ? "Preview textual abaixo — arquivo não é PDF."
        : "Selecione um arquivo ou mensagem do WhatsApp.";
      empty.style.display = "flex";
    }
  }
  if (excerpt) {
    excerpt.textContent = teamNormaPreviewState.excerpt || "(sem texto extraível para preview)";
  }
  updateTeamNormaPreviewActions();
}

async function applyTeamNormaPreviewFile(file) {
  if (!file) return;
  teamNormaPreviewState.source = "upload";
  teamNormaPreviewState.file = file;
  teamNormaPreviewState.existingName = "";
  teamNormaPreviewState.msgId = "";
  teamNormaPreviewState.waMessage = null;
  teamNormaPreviewState.name = file.name || "norma";
  teamNormaPreviewState.mimeType = file.type || "";
  teamNormaPreviewState.isPdf = (file.type === "application/pdf") || /\.pdf$/i.test(file.name || "");
  resetTeamNormaPreviewViewer();
  setTeamNormaPreviewStatus("A ler arquivo…", "");
  try {
    teamNormaPreviewState.dataBase64 = await readFileAsBase64(file);
    if (teamNormaPreviewState.isPdf) {
      const blob = base64ToBlob(teamNormaPreviewState.dataBase64, "application/pdf");
      teamNormaPreviewState.blobUrl = URL.createObjectURL(blob);
      if (teamNormaPreviewState.teamId) {
        const { data: preview } = await chatFetchJson(
          "/teams/" + encodeURIComponent(teamNormaPreviewState.teamId) + "/normas/preview",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: teamNormaPreviewState.name,
              mime_type: teamNormaPreviewState.mimeType || "application/pdf",
              data_base64: teamNormaPreviewState.dataBase64,
            }),
          },
          60000,
        );
        if (preview && preview.ok) {
          teamNormaPreviewState.excerpt = preview.excerpt || "";
        }
      }
    }
    if (!teamNormaPreviewState.isPdf && (file.type.startsWith("text/") || /\.(md|txt|tex|json|ya?ml|csv)$/i.test(file.name || ""))) {
      const blob = base64ToBlob(teamNormaPreviewState.dataBase64, file.type || "text/plain");
      teamNormaPreviewState.excerpt = await blob.text();
      teamNormaPreviewState.excerpt = teamNormaPreviewState.excerpt.slice(0, 2500);
    } else if (!teamNormaPreviewState.isPdf) {
      teamNormaPreviewState.excerpt = "(arquivo binário — use Salvar no time para indexar)";
    }
    setTeamNormaPreviewStatus("", "");
    renderTeamNormaPreviewContent();
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
  }
}

async function openTeamNormaPreviewExisting(teamId, name) {
  if (!teamId || !name) return;
  openTeamNormaPreview({ teamId, tab: "upload" });
  teamNormaPreviewState.source = "existing";
  teamNormaPreviewState.existingName = name;
  teamNormaPreviewState.name = name;
  teamNormaPreviewState.dataBase64 = "";
  teamNormaPreviewState.msgId = "";
  teamNormaPreviewState.isPdf = /\.pdf$/i.test(name);
  resetTeamNormaPreviewViewer();
  setTeamNormaPreviewStatus("A carregar norma…", "");
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", { method: "GET" }, 15000);
    const row = (data.normas || []).find((n) => n.name === name) || {};
    teamNormaPreviewState.excerpt = row.excerpt || "";
    teamNormaPreviewState.mimeType = row.mime_type || "";
    setTeamNormaPreviewStatus("", "");
    renderTeamNormaPreviewContent();
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
  }
}

function renderTeamNormaWaList(messages) {
  const box = el("team-norma-preview-wa-list");
  if (!box) return;
  if (!messages || !messages.length) {
    box.innerHTML = '<div class="team-norma-preview-empty">Nenhuma mensagem encontrada.</div>';
    return;
  }
  box.innerHTML = messages.map((m, idx) => {
    const title = (m.sender || "?") + " · " + (m.chat_name || m.chat_jid || "chat");
    const body = String(m.body || "").slice(0, 120);
    const kind = m.import_kind === "media" ? "📎 mídia" : "💬 texto";
    const active = teamNormaPreviewState.msgId === m.msg_id ? " active" : "";
    return '<div class="team-norma-preview-wa-item' + active + '" data-wa-idx="' + idx + '">'
      + '<div class="title">' + escapeHtml(title) + " · " + escapeHtml(kind) + "</div>"
      + '<div class="sub">' + escapeHtml(body || m.type || "—") + "</div></div>";
  }).join("");
  box._rows = messages;
  box.querySelectorAll(".team-norma-preview-wa-item").forEach((node) => {
    node.onclick = () => {
      const row = box._rows[Number(node.dataset.waIdx)];
      if (row) void selectTeamNormaWaMessage(row);
    };
  });
}

async function searchTeamNormaWaMessages() {
  const q = (el("team-norma-preview-wa-query") && el("team-norma-preview-wa-query").value || "").trim();
  const chat = (el("team-norma-preview-wa-chat") && el("team-norma-preview-wa-chat").value || "").trim();
  setTeamNormaPreviewStatus("A buscar no WhatsApp…", "");
  try {
    const params = new URLSearchParams();
    if (q) params.set("query", q);
    if (chat) params.set("chat", chat);
    params.set("limit", "25");
    const { data } = await chatFetchJson("/whatsapp/messages/norma-candidates?" + params.toString(), { method: "GET" }, 45000);
    if (!data.ok) throw new Error(data.error || "falha na busca");
    renderTeamNormaWaList(data.messages || []);
    setTeamNormaPreviewStatus((data.count || 0) + " mensagem(ns) encontrada(s).", "ok");
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
    renderTeamNormaWaList([]);
  }
}

async function selectTeamNormaWaMessage(msg) {
  if (!msg || !teamNormaPreviewState.teamId) return;
  teamNormaPreviewState.source = "whatsapp";
  teamNormaPreviewState.waMessage = msg;
  teamNormaPreviewState.msgId = msg.msg_id || "";
  teamNormaPreviewState.existingName = "";
  teamNormaPreviewState.dataBase64 = "";
  teamNormaPreviewState.name = "";
  resetTeamNormaPreviewViewer();
  setTeamNormaPreviewStatus("A carregar mensagem…", "");
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamNormaPreviewState.teamId) + "/normas/whatsapp-preview",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          msg_id: msg.msg_id,
          body: msg.body,
          sender: msg.sender,
          chat_name: msg.chat_name,
          date: msg.date,
        }),
      },
      120000,
    );
    if (!data.ok) throw new Error(data.error || "falha no preview");
    teamNormaPreviewState.name = data.name || teamNormaPreviewState.name || ("whatsapp-" + (msg.msg_id || "").slice(0, 8) + ".md");
    teamNormaPreviewState.mimeType = data.mime_type || "";
    teamNormaPreviewState.excerpt = data.excerpt || msg.body || "";
    teamNormaPreviewState.isPdf = !!data.is_pdf;
    teamNormaPreviewState.dataBase64 = data.data_base64 || "";
    if (teamNormaPreviewState.isPdf && teamNormaPreviewState.dataBase64) {
      const bytes = Uint8Array.from(atob(teamNormaPreviewState.dataBase64), (c) => c.charCodeAt(0));
      teamNormaPreviewState.blobUrl = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    }
    setTeamNormaPreviewStatus("", "");
    const waList = el("team-norma-preview-wa-list");
    if (waList && waList._rows) renderTeamNormaWaList(waList._rows);
    renderTeamNormaPreviewContent();
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
  }
}

function setTeamNormaPreviewTab(tab) {
  teamNormaPreviewState.tab = tab;
  const uploadPanel = el("team-norma-preview-upload-panel");
  const waPanel = el("team-norma-preview-wa-panel");
  const tabs = el("team-norma-preview-tabs");
  if (uploadPanel) uploadPanel.hidden = tab !== "upload";
  if (waPanel) waPanel.hidden = tab !== "whatsapp";
  if (tabs) {
    tabs.querySelectorAll("button[data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === tab);
    });
  }
}

function openTeamNormaPreview(opts) {
  const options = opts || {};
  const teamId = options.teamId || teamPickerState.selectedTeamId || "";
  if (!teamId) {
    if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
    return;
  }
  teamNormaPreviewState.teamId = teamId;
  setTeamNormaPreviewTab(options.tab || "upload");
  setTeamNormaPreviewStatus("", "");
  const overlay = el("team-norma-preview-overlay");
  if (overlay) overlay.hidden = false;
  if (options.existingName) {
    void openTeamNormaPreviewExisting(teamId, options.existingName);
    return;
  }
  if (options.file) {
    void applyTeamNormaPreviewFile(options.file);
    return;
  }
  resetTeamNormaPreviewViewer();
  updateTeamNormaPreviewActions();
  if ((options.tab || "upload") === "whatsapp") void searchTeamNormaWaMessages();
}

function closeTeamNormaPreview() {
  const overlay = el("team-norma-preview-overlay");
  if (overlay) overlay.hidden = true;
  resetTeamNormaPreviewViewer();
  teamNormaPreviewState = {
    teamId: "",
    tab: "upload",
    source: "upload",
    file: null,
    blobUrl: "",
    dataBase64: "",
    name: "",
    mimeType: "",
    excerpt: "",
    isPdf: false,
    msgId: "",
    waMessage: null,
    existingName: "",
  };
}

async function confirmTeamNormaSaveToTeam() {
  const teamId = teamNormaPreviewState.teamId;
  const nameInput = el("team-norma-preview-name");
  const name = (nameInput && nameInput.value || teamNormaPreviewState.name || "").trim();
  if (!teamId || !name) return;
  setTeamNormaPreviewStatus("A salvar no time…", "");
  try {
    let data;
    if (teamNormaPreviewState.source === "whatsapp") {
      const msg = teamNormaPreviewState.waMessage || {};
      const { data: resp } = await chatFetchJson(
        "/teams/" + encodeURIComponent(teamId) + "/normas/from-whatsapp",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            msg_id: teamNormaPreviewState.msgId,
            name,
            data_base64: teamNormaPreviewState.dataBase64,
            mime_type: teamNormaPreviewState.mimeType,
            body: msg.body,
            sender: msg.sender,
            chat_name: msg.chat_name,
            date: msg.date,
          }),
        },
        120000,
      );
      data = resp;
    } else if (teamNormaPreviewState.existingName) {
      setTeamNormaPreviewStatus("Já existe no time.", "ok");
      closeTeamNormaPreview();
      return;
    } else {
      const { data: resp } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          mime_type: teamNormaPreviewState.mimeType,
          data_base64: teamNormaPreviewState.dataBase64,
        }),
      }, 120000);
      data = resp;
    }
    if (!data.ok) throw new Error(data.error || "falha ao salvar");
    setTeamNormaPreviewStatus("✅ Salvo no time.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Norma salva no time.", "ok");
    invalidateTeamPickerPreviewCache(teamId);
    await loadTeamPickerNormas(teamId);
    if (teamPickerState.selectedTeamId === teamId) await loadTeamPickerPreview(teamId);
    closeTeamNormaPreview();
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
  }
}

async function confirmTeamNormaAddToContext() {
  const teamId = teamNormaPreviewState.teamId;
  const name = (el("team-norma-preview-name") && el("team-norma-preview-name").value || teamNormaPreviewState.name || "").trim();
  if (!teamId) return;
  setTeamNormaPreviewStatus("A fixar no contexto da conversa…", "");
  try {
    if (teamNormaPreviewState.existingName) {
      await addTeamFilesToSessionContext(teamId, [{
        relative_path: "normas/" + teamNormaPreviewState.existingName,
        name: teamNormaPreviewState.existingName,
      }]);
    } else if (teamNormaPreviewState.dataBase64) {
      if (!activeKey) await startNewChatAsync();
      const { data } = await chatFetchJson("/openclaw/sessions/context-documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_key: activeKey,
          action: "add",
          attachments: [{
            name,
            mime_type: teamNormaPreviewState.mimeType || "application/octet-stream",
            data_base64: teamNormaPreviewState.dataBase64,
          }],
        }),
      }, 120000);
      if (!data || !data.ok) throw new Error((data && data.error) || "falha");
      chatContextDocs = Array.isArray(data.documents) ? data.documents.slice() : chatContextDocs;
      renderChatContextDocs();
      if (typeof window._renderArticlesContextPanel === "function") {
        window._renderArticlesContextPanel();
      }
      if (window.qcmNavToast) window.qcmNavToast("Documento fixado no contexto.", "ok");
    } else {
      throw new Error("nada para adicionar ao contexto");
    }
    setTeamNormaPreviewStatus("✅ No contexto da conversa.", "ok");
    closeTeamNormaPreview();
  } catch (e) {
    setTeamNormaPreviewStatus("Erro: " + e.message, "err");
  }
}

function bindTeamNormaPreviewDialog() {
  const overlay = el("team-norma-preview-overlay");
  if (!overlay || overlay._bound) return;
  overlay._bound = true;
  el("team-norma-preview-close").onclick = closeTeamNormaPreview;
  el("team-norma-preview-cancel").onclick = closeTeamNormaPreview;
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) closeTeamNormaPreview(); });
  el("team-norma-preview-tabs").querySelectorAll("button[data-tab]").forEach((btn) => {
    btn.onclick = () => {
      setTeamNormaPreviewTab(btn.getAttribute("data-tab") || "upload");
      if (btn.getAttribute("data-tab") === "whatsapp") void searchTeamNormaWaMessages();
    };
  });
  el("team-norma-preview-pick-file").onclick = () => el("team-norma-preview-file").click();
  el("team-norma-preview-file").addEventListener("change", () => {
    const file = el("team-norma-preview-file").files && el("team-norma-preview-file").files[0];
    el("team-norma-preview-file").value = "";
    if (file) void applyTeamNormaPreviewFile(file);
  });
  el("team-norma-preview-wa-search").onclick = () => { void searchTeamNormaWaMessages(); };
  el("team-norma-preview-save").onclick = () => { void confirmTeamNormaSaveToTeam(); };
  el("team-norma-preview-context").onclick = () => { void confirmTeamNormaAddToContext(); };
  el("team-norma-preview-name").addEventListener("input", () => {
    teamNormaPreviewState.name = el("team-norma-preview-name").value || "";
    updateTeamNormaPreviewActions();
  });
}

function bindTeamPickerNormaPreviewClicks(root, teamId) {
  if (!root || !teamId) return;
  root.querySelectorAll("[data-preview-norma]").forEach((node) => {
    node.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const name = node.getAttribute("data-preview-norma") || "";
      if (name) openTeamNormaPreview({ teamId, existingName: name });
    };
  });
}

let teamPickerPathBrowse = "";
let teamPickerArtBrowsePath = "";
let teamPickerArticlesState = {
  workspaces: [],
  articles: [],
  activeWsId: "",
  activeArticleId: "",
};

function getTeamLatexWorkspaceIds(team) {
  if (!team) return [];
  const ids = Array.isArray(team.latex_workspace_ids)
    ? team.latex_workspace_ids.map((x) => String(x || "").trim()).filter(Boolean)
    : [];
  const single = String(team.latex_workspace_id || "").trim();
  if (single && !ids.includes(single)) ids.unshift(single);
  return ids;
}

function renderTeamPickerLinkedWorkspacesHtml(team, wsList) {
  const ids = getTeamLatexWorkspaceIds(team);
  if (!ids.length) {
    return '<span class="team-picker-articles-linked-empty">Nenhuma pasta vinculada.</span>';
  }
  const byId = {};
  (wsList || []).forEach((ws) => { byId[ws.id] = ws; });
  return ids.map((wsId) => {
    const ws = byId[wsId] || {};
    const label = ws.name || wsId;
    return '<span class="team-picker-articles-ws-chip" title="' + escapeHtml(wsId) + '">'
      + '<span class="name">📁 ' + escapeHtml(label) + "</span>"
      + '<button type="button" data-remove-team-ws="' + escapeHtml(wsId)
      + '" aria-label="Remover pasta">✕</button></span>';
  }).join("");
}

async function refreshTeamPickerLinkedWorkspaces(teamId) {
  const box = el("team-picker-articles-linked");
  if (!box) return;
  const team = getTeamPickerSelectedTeam();
  let wsList = teamPickerArticlesState.workspaces;
  if (!wsList.length) {
    try {
      const { data } = await chatFetchJson("/latex/workspaces", { method: "GET" }, 15000);
      wsList = Array.isArray(data.workspaces) ? data.workspaces : [];
      teamPickerArticlesState.workspaces = wsList;
    } catch (_e) {
      wsList = [];
    }
  }
  box.innerHTML = renderTeamPickerLinkedWorkspacesHtml(team, wsList);
  box.querySelectorAll("[data-remove-team-ws]").forEach((btn) => {
    btn.onclick = () => {
      void removeTeamPickerArticlesWorkspace(teamId, btn.getAttribute("data-remove-team-ws") || "");
    };
  });
}

function applyTeamPickerArticlesTeam(teamId, team) {
  if (!teamId || !team) return;
  const cached = teamPickerState.previewCache[teamId];
  if (cached && cached.team) Object.assign(cached.team, team);
  const t = chatTeams.find((x) => x.id === teamId);
  if (t) Object.assign(t, team);
}

async function patchTeamPickerArticlesTeam(teamId, payload) {
  if (!teamId) return null;
  const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 20000);
  if (!data.ok) {
    if (chatRedirectIfNotAuthenticated(data.error)) return null;
    throw new Error(data.error || "falha ao atualizar pastas");
  }
  if (data.team) applyTeamPickerArticlesTeam(teamId, data.team);
  return data.team || null;
}

async function addTeamPickerArticlesWorkspace(teamId, wsId) {
  if (!teamId || !wsId) {
    setTeamPickerArticlesStatus("Selecione uma pasta.", "err");
    return false;
  }
  const team = getTeamPickerSelectedTeam();
  if (getTeamLatexWorkspaceIds(team).includes(wsId)) {
    setTeamPickerArticlesStatus("Esta pasta já está vinculada ao time.", "err");
    return false;
  }
  setTeamPickerArticlesStatus("A adicionar…", "");
  try {
    await patchTeamPickerArticlesTeam(teamId, { add_latex_workspace_id: wsId });
    setTeamPickerArticlesStatus("✅ Pasta adicionada ao time.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Pasta de artigos adicionada.", "ok");
    await loadTeamPickerArticlesWorkspaces(teamId);
    await loadTeamPickerArticlesPreview(teamId);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamPickerArticlesStatus("Erro: " + e.message, "err");
    return false;
  }
}

async function removeTeamPickerArticlesWorkspace(teamId, wsId) {
  if (!teamId || !wsId) return false;
  setTeamPickerArticlesStatus("A remover…", "");
  try {
    await patchTeamPickerArticlesTeam(teamId, { remove_latex_workspace_id: wsId });
    setTeamPickerArticlesStatus("✅ Pasta removida do time.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Pasta removida do time.", "ok");
    await loadTeamPickerArticlesWorkspaces(teamId);
    await loadTeamPickerArticlesPreview(teamId);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamPickerArticlesStatus("Erro: " + e.message, "err");
    return false;
  }
}

async function clearTeamPickerArticlesWorkspaces(teamId) {
  if (!teamId) return false;
  setTeamPickerArticlesStatus("A remover todas…", "");
  try {
    await patchTeamPickerArticlesTeam(teamId, { latex_workspace_ids: [] });
    setTeamPickerArticlesStateReset();
    setTeamPickerArticlesStatus("✅ Todas as pastas foram desvinculadas.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Pastas desvinculadas.", "ok");
    await loadTeamPickerArticlesWorkspaces(teamId);
    await loadTeamPickerArticlesPreview(teamId);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamPickerArticlesStatus("Erro: " + e.message, "err");
    return false;
  }
}

function setTeamPickerArticlesStateReset() {
  teamPickerState.articlesPreview.wsId = null;
  teamPickerState.articlesPreview.selectedWsId = null;
  teamPickerState.articlesPreview.articles = [];
  teamPickerState.articlesPreview.selectedId = null;
  teamPickerState.articlesPreview.normas = [];
  teamPickerState.articlesPreview.selectedNorma = null;
}

function setTeamPickerArticlesStatus(msg, kind) {
  const elStatus = el("team-picker-articles-status");
  if (!elStatus) return;
  elStatus.textContent = msg || "";
  elStatus.className = "team-picker-path-status" + (kind ? " " + kind : "");
}

function getTeamPickerArticlesWsId() {
  const team = getTeamPickerSelectedTeam();
  const ids = getTeamLatexWorkspaceIds(team);
  if (ids.length) return ids[0];
  const select = el("team-picker-articles-ws");
  return (select && select.value || "").trim();
}

function resetTeamPickerPdfView() {
  const title = el("team-picker-pdf-title");
  const frame = el("team-picker-pdf-frame");
  const empty = el("team-picker-pdf-empty");
  const logBox = el("team-picker-pdf-log-box");
  const btnCompile = el("team-picker-pdf-compile");
  const btnLog = el("team-picker-pdf-log");
  const btnExpand = el("team-picker-pdf-expand");
  const btnOpen = el("team-picker-pdf-open");
  if (title) title.textContent = "Nenhum PDF selecionado";
  if (btnCompile) btnCompile.disabled = true;
  if (btnLog) btnLog.disabled = true;
  if (btnExpand) btnExpand.disabled = true;
  if (btnOpen) btnOpen.style.display = "none";
  if (frame) {
    frame.removeAttribute("src");
    frame.style.display = "none";
  }
  if (empty) {
    empty.textContent = "Escolha um artigo ou envie um PDF ao contexto (📎)";
    empty.style.display = "flex";
  }
  if (logBox) {
    logBox.textContent = "";
    logBox.classList.remove("show");
  }
}

function renderTeamPickerArticlesList() {
  const listEl = el("team-picker-articles-list");
  if (!listEl) return;
  const { articles, normas, selectedId, selectedWsId, selectedNorma } = teamPickerState.articlesPreview;
  const pdfNormas = Array.isArray(normas) ? normas : [];
  if (!articles.length && !pdfNormas.length) {
    listEl.innerHTML = '<div class="team-picker-articles-preview-empty">Nenhum .tex nem PDF no contexto. Envie um PDF com 📎 ou adicione uma pasta.</div>';
    return;
  }
  let html = "";
  if (articles.length) {
    html += articles.map((a) => {
      const active = a.id === selectedId && a.workspace_id === selectedWsId && !selectedNorma ? " active" : "";
      const pdfBadge = a.has_pdf ? '<span class="badge-pdf">PDF</span>' : "";
      const wsLabel = a.workspace_name ? '<span>' + escapeHtml(a.workspace_name) + "</span>" : "";
      const ctxBadge = (typeof isLatexArticleInContext === "function" && isLatexArticleInContext(a))
        ? '<span class="badge-ctx">no contexto</span>' : "";
      const ctxBtns = [];
      if (a.has_pdf) {
        ctxBtns.push('<button type="button" class="ctx-add-btn" data-ctx-article-ws="'
          + escapeHtml(a.workspace_id || "") + '" data-ctx-article-id="' + escapeHtml(a.id)
          + '" data-ctx-include-pdf="1">📎 PDF</button>');
      }
      ctxBtns.push('<button type="button" class="ctx-add-btn" data-ctx-article-ws="'
        + escapeHtml(a.workspace_id || "") + '" data-ctx-article-id="' + escapeHtml(a.id)
        + '" data-ctx-include-pdf="0">📎 TeX</button>');
      const ctxRow = '<div class="ctx-actions">' + ctxBtns.join("") + "</div>";
      return '<button type="button" class="articles-tab-item' + active + '" data-article-id="'
        + escapeHtml(a.id) + '" data-workspace-id="' + escapeHtml(a.workspace_id || "") + '">'
        + '<div class="title">' + escapeHtml(a.title || a.id) + "</div>"
        + '<div class="meta">' + wsLabel + pdfBadge + ctxBadge + "</div>"
        + latexArticleVersionBadgesHtml(a)
        + ctxRow + "</button>";
    }).join("");
  }
  if (pdfNormas.length) {
    if (articles.length) {
      html += '<div style="padding:8px 8px 4px;font-size:9px;font-weight:700;color:var(--muted);'
        + 'text-transform:uppercase;letter-spacing:.04em;">PDFs no contexto</div>';
    }
    html += pdfNormas.map((n) => {
      const active = n.name === selectedNorma ? " active" : "";
      const meta = teamNormaIndexLabel(n);
      return '<button type="button" class="articles-tab-item' + active + '" data-norma-name="'
        + escapeHtml(n.name || "") + '">'
        + '<div class="title">' + escapeHtml(n.name || "PDF") + "</div>"
        + '<div class="meta"><span>contexto do time</span><span class="badge-ctx">' + escapeHtml(meta) + "</span></div></button>";
    }).join("");
  }
  listEl.innerHTML = html;
  listEl.querySelectorAll("[data-article-id]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      if (ev.target.closest(".ctx-add-btn")) return;
      selectTeamPickerArticle(
        btn.getAttribute("data-article-id") || "",
        btn.getAttribute("data-workspace-id") || "",
      );
    });
  });
  listEl.querySelectorAll(".ctx-add-btn[data-ctx-article-id]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const wsId = btn.getAttribute("data-ctx-article-ws") || "";
      const artId = btn.getAttribute("data-ctx-article-id") || "";
      const includePdf = btn.getAttribute("data-ctx-include-pdf") === "1";
      const art = teamPickerState.articlesPreview.articles.find(
        (a) => a.id === artId && (!wsId || a.workspace_id === wsId),
      );
      if (!wsId || !artId) return;
      void addLatexArticleToSessionContext(wsId, artId, art, {
        toast: true,
        includePdf: includePdf,
      }).then(() => renderTeamPickerArticlesList());
    });
  });
  listEl.querySelectorAll("[data-norma-name]").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectTeamPickerNorma(btn.getAttribute("data-norma-name") || "");
    });
  });
}

function selectTeamPickerArticle(articleId, wsId) {
  const art = teamPickerState.articlesPreview.articles.find(
    (a) => a.id === articleId && (!wsId || a.workspace_id === wsId),
  );
  const activeWs = (art && art.workspace_id) || wsId;
  if (!activeWs || !articleId) return;
  teamPickerState.articlesPreview.wsId = activeWs;
  teamPickerState.articlesPreview.selectedWsId = activeWs;
  teamPickerState.articlesPreview.selectedId = articleId;
  teamPickerState.articlesPreview.selectedNorma = null;
  renderTeamPickerArticlesList();
  if (!art) return;
  const title = el("team-picker-pdf-title");
  const frame = el("team-picker-pdf-frame");
  const empty = el("team-picker-pdf-empty");
  const btnCompile = el("team-picker-pdf-compile");
  const btnLog = el("team-picker-pdf-log");
  const btnExpand = el("team-picker-pdf-expand");
  const btnOpen = el("team-picker-pdf-open");
  const logBox = el("team-picker-pdf-log-box");
  if (title) title.textContent = art.title || art.id;
  if (btnCompile) btnCompile.disabled = false;
  if (btnLog) btnLog.disabled = false;
  if (btnExpand) btnExpand.disabled = !art.has_pdf;
  if (logBox) logBox.classList.remove("show");
  if (art.has_pdf) {
    const pdfUrl = "/latex/pdf?workspace_id=" + encodeURIComponent(activeWs)
      + "&article_id=" + encodeURIComponent(articleId) + "&_=" + Date.now();
    if (frame) {
      frame.src = pdfUrl;
      frame.style.display = "block";
    }
    if (empty) empty.style.display = "none";
    if (btnOpen) {
      btnOpen.href = pdfUrl;
      btnOpen.style.display = "inline-block";
    }
  } else {
    if (frame) {
      frame.removeAttribute("src");
      frame.style.display = "none";
    }
    if (empty) {
      empty.textContent = "Sem PDF — use ⚙️ para compilar";
      empty.style.display = "flex";
    }
    if (btnOpen) btnOpen.style.display = "none";
  }
  void addLatexArticleToSessionContext(activeWs, articleId, art, { toast: true });
}

function selectTeamPickerNorma(normaName) {
  const name = String(normaName || "").trim();
  if (!name) return;
  const teamId = teamPickerState.selectedTeamId;
  const norma = (teamPickerState.articlesPreview.normas || []).find((n) => n.name === name);
  teamPickerState.articlesPreview.selectedNorma = name;
  teamPickerState.articlesPreview.selectedId = null;
  teamPickerState.articlesPreview.selectedWsId = null;
  renderTeamPickerArticlesList();
  const title = el("team-picker-pdf-title");
  const frame = el("team-picker-pdf-frame");
  const empty = el("team-picker-pdf-empty");
  const btnCompile = el("team-picker-pdf-compile");
  const btnLog = el("team-picker-pdf-log");
  const btnExpand = el("team-picker-pdf-expand");
  const btnOpen = el("team-picker-pdf-open");
  const logBox = el("team-picker-pdf-log-box");
  if (title) title.textContent = name;
  if (btnCompile) btnCompile.disabled = true;
  if (btnLog) btnLog.disabled = true;
  if (logBox) logBox.classList.remove("show");
  const isPdf = (norma && (norma.mime_type || "").includes("pdf")) || /\.pdf$/i.test(name);
  if (isPdf && teamId) {
    const pdfUrl = teamNormaDownloadUrl(teamId, name) + "&_=" + Date.now();
    if (frame) {
      frame.src = pdfUrl;
      frame.style.display = "block";
    }
    if (empty) empty.style.display = "none";
    if (btnExpand) btnExpand.disabled = false;
    if (btnOpen) {
      btnOpen.href = pdfUrl;
      btnOpen.style.display = "inline-block";
    }
  } else {
    if (frame) {
      frame.removeAttribute("src");
      frame.style.display = "none";
    }
    if (empty) {
      empty.textContent = norma && norma.has_text
        ? "Norma indexada no contexto — preview textual na seção Normas."
        : "Sem preview para este arquivo.";
      empty.style.display = "flex";
    }
    if (btnExpand) btnExpand.disabled = true;
    if (btnOpen) btnOpen.style.display = "none";
  }
}

async function loadTeamPickerNormasForPreview(teamId) {
  if (!teamId) return [];
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/normas", {
      method: "GET",
    }, 15000);
    if (!data.ok) return [];
    return (Array.isArray(data.normas) ? data.normas : []).filter((n) =>
      (n.mime_type || "").includes("pdf")
      || /\.(pdf|docx|md|txt|tex|json|ya?ml|csv)$/i.test(n.name || ""),
    );
  } catch (_e) {
    return [];
  }
}

async function loadTeamPickerArticlesPreview(teamId) {
  const listEl = el("team-picker-articles-list");
  const team = getTeamPickerSelectedTeam();
  const wsIds = teamId ? getTeamLatexWorkspaceIds(team) : [];
  teamPickerState.articlesPreview.wsId = wsIds[0] || null;
  teamPickerState.articlesPreview.selectedWsId = null;
  teamPickerState.articlesPreview.articles = [];
  teamPickerState.articlesPreview.selectedId = null;
  teamPickerState.articlesPreview.selectedNorma = null;
  resetTeamPickerPdfView();
  const normas = await loadTeamPickerNormasForPreview(teamId);
  teamPickerState.articlesPreview.normas = normas;
  if (!wsIds.length) {
    if (listEl) {
      if (normas.length) {
        renderTeamPickerArticlesList();
        selectTeamPickerNorma(normas[0].name);
      } else {
        listEl.innerHTML = '<div class="team-picker-articles-preview-empty">Adicione uma pasta ou envie um PDF ao contexto (📎).</div>';
      }
    }
    return;
  }
  if (listEl) listEl.innerHTML = '<div class="team-picker-articles-preview-empty">A carregar artigos…</div>';
  const wsById = {};
  (teamPickerArticlesState.workspaces || []).forEach((ws) => { wsById[ws.id] = ws; });
  const allArticles = [];
  const errors = [];
  for (const wsId of wsIds) {
    try {
      const { data } = await chatFetchJson(
        "/latex/articles?workspace_id=" + encodeURIComponent(wsId),
        { method: "GET" },
        20000,
      );
      if (!data.ok && data.ok !== undefined) throw new Error(data.error || "falha ao carregar artigos");
      const wsName = (wsById[wsId] && wsById[wsId].name) || wsId;
      (Array.isArray(data.articles) ? data.articles : []).forEach((a) => {
        allArticles.push({ ...a, workspace_id: wsId, workspace_name: wsName });
      });
    } catch (e) {
      errors.push(wsId + ": " + e.message);
    }
  }
  teamPickerState.articlesPreview.articles = allArticles;
  renderTeamPickerArticlesList();
  if (!allArticles.length && errors.length) {
    if (listEl && !normas.length) {
      listEl.innerHTML = '<div class="team-picker-articles-preview-empty" style="color:var(--bad);">Erro: '
        + escapeHtml(errors.join(" · ")) + "</div>";
    }
    if (normas.length) selectTeamPickerNorma(normas[0].name);
    return;
  }
  const firstWithPdf = allArticles.find((a) => a.has_pdf);
  const first = firstWithPdf || allArticles[0];
  if (first) {
    selectTeamPickerArticle(first.id, first.workspace_id);
  } else if (normas.length) {
    selectTeamPickerNorma(normas[0].name);
  }
}

function bindTeamPickerArticlePreview(teamId) {
  void loadTeamPickerArticlesPreview(teamId);
  const btnUpload = el("team-picker-pdf-upload");
  const pdfInput = el("team-picker-articles-pdf-input");
  if (btnUpload) {
    btnUpload.onclick = () => {
      const tid = teamPickerActiveTeamId(teamId);
      if (!tid) {
        if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
        return;
      }
      if (pdfInput) pdfInput.click();
    };
  }
  if (pdfInput) {
    pdfInput.onchange = () => {
      const file = pdfInput.files && pdfInput.files[0];
      pdfInput.value = "";
      const tid = teamPickerActiveTeamId(teamId);
      if (!file || !tid) return;
      void (async () => {
        setTeamPickerArticlesStatus("A enviar " + (file.name || "PDF") + " ao contexto do time…", "");
        const savedName = await uploadTeamPickerNorma(tid, file);
        if (!savedName) return;
        setTeamPickerArticlesStatus("✅ PDF adicionado ao contexto do time.", "ok");
        await loadTeamPickerArticlesPreview(tid);
        selectTeamPickerNorma(savedName);
      })();
    };
  }
  const btnCompile = el("team-picker-pdf-compile");
  const btnLog = el("team-picker-pdf-log");
  const btnExpand = el("team-picker-pdf-expand");
  if (btnCompile) {
    btnCompile.onclick = async () => {
      const activeWs = teamPickerState.articlesPreview.selectedWsId || teamPickerState.articlesPreview.wsId;
      const articleId = teamPickerState.articlesPreview.selectedId;
      if (!activeWs || !articleId) return;
      const title = el("team-picker-pdf-title");
      btnCompile.disabled = true;
      if (title) title.textContent = "Compilando…";
      const logBox = el("team-picker-pdf-log-box");
      if (logBox) logBox.classList.remove("show");
      try {
        const { data } = await chatFetchJson("/latex/compile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: activeWs, article_id: articleId }),
        }, 120000);
        const logText = data.log_tail || data.log || "";
        if (logText && logBox) {
          logBox.textContent = logText;
          logBox.classList.add("show");
        }
        if (data.ok) {
          await loadTeamPickerArticlesPreview(teamId);
          selectTeamPickerArticle(articleId, activeWs);
        } else {
          if (title) title.textContent = "Erro na compilação";
          if (window.qcmNavToast) window.qcmNavToast(data.error || "Erro na compilação do LaTeX");
        }
      } catch (e) {
        if (title) title.textContent = "Erro de rede";
        if (window.qcmNavToast) window.qcmNavToast(e.message || String(e));
      } finally {
        btnCompile.disabled = false;
      }
    };
  }
  if (btnLog) {
    btnLog.onclick = () => {
      const logBox = el("team-picker-pdf-log-box");
      if (logBox) logBox.classList.toggle("show");
    };
  }
  if (btnExpand) {
    btnExpand.onclick = () => {
      const selectedNorma = teamPickerState.articlesPreview.selectedNorma;
      const activeTeamId = teamPickerState.selectedTeamId;
      if (selectedNorma && activeTeamId) {
        const pdfUrl = teamNormaDownloadUrl(activeTeamId, selectedNorma);
        const modal = el("chat-pdf-modal");
        const modalTitle = el("chat-pdf-modal-title");
        const modalFrame = el("chat-pdf-modal-frame");
        if (modalTitle) modalTitle.textContent = selectedNorma;
        if (modalFrame) modalFrame.src = pdfUrl;
        if (modal) modal.style.display = "flex";
        return;
      }
      const activeWs = teamPickerState.articlesPreview.selectedWsId || teamPickerState.articlesPreview.wsId;
      const articleId = teamPickerState.articlesPreview.selectedId;
      if (!activeWs || !articleId) return;
      const art = teamPickerState.articlesPreview.articles.find(
        (a) => a.id === articleId && a.workspace_id === activeWs,
      );
      if (!art || !art.has_pdf) return;
      const pdfUrl = "/latex/pdf?workspace_id=" + encodeURIComponent(activeWs)
        + "&article_id=" + encodeURIComponent(articleId);
      const modal = el("chat-pdf-modal");
      const modalTitle = el("chat-pdf-modal-title");
      const modalFrame = el("chat-pdf-modal-frame");
      if (modalTitle) modalTitle.textContent = art.title || art.id;
      if (modalFrame) modalFrame.src = pdfUrl;
      if (modal) modal.style.display = "flex";
    };
  }
}

function getTeamPickerSelectedTeam() {
  const teamId = teamPickerState.selectedTeamId;
  if (!teamId) return null;
  const cached = teamPickerState.previewCache[teamId];
  if (cached && cached.team) return cached.team;
  return chatTeams.find((t) => t.id === teamId) || null;
}

async function loadTeamPickerArticlesWorkspaces(teamId) {
  const select = el("team-picker-articles-ws");
  if (!select) return;
  const team = getTeamPickerSelectedTeam();
  const linked = new Set(getTeamLatexWorkspaceIds(team));
  select.innerHTML = '<option value="">A carregar…</option>';
  try {
    const { data } = await chatFetchJson("/latex/workspaces", { method: "GET" }, 15000);
    if (!data.ok && data.ok !== undefined) throw new Error(data.error || "falha ao carregar workspaces");
    const wsList = Array.isArray(data.workspaces) ? data.workspaces : [];
    teamPickerArticlesState.workspaces = wsList;
    const available = wsList.filter((ws) => !linked.has(ws.id));
    select.innerHTML = available.length
      ? '<option value="">— Selecione uma pasta —</option>'
        + available.map((ws) =>
          '<option value="' + escapeHtml(ws.id) + '">'
          + escapeHtml(ws.name || ws.id) + "</option>"
        ).join("")
      : '<option value="">— Todas as pastas já estão no time —</option>';
    await refreshTeamPickerLinkedWorkspaces(teamId);
    await loadTeamPickerArticlesPreview(teamId);
  } catch (e) {
    select.innerHTML = '<option value="">Erro ao carregar</option>';
    setTeamPickerArticlesStatus("Erro ao carregar pastas: " + e.message, "err");
  }
}

function renderTeamPickerArtFolderList(data) {
  const current = String(data.current || "");
  const parent = String(data.parent || "");
  const dirs = Array.isArray(data.directories) ? data.directories : [];
  teamPickerArtBrowsePath = current;
  const curEl = el("team-article-folder-current");
  const upBtn = el("team-article-folder-up");
  const pathInput = el("team-article-folder-path");
  const nameInput = el("team-article-folder-name");
  if (curEl) curEl.textContent = current || "—";
  if (upBtn) upBtn.disabled = !parent || parent === current;
  if (pathInput) pathInput.value = current || "";
  if (nameInput && !nameInput.value.trim() && current) {
    const parts = current.split("/").filter(Boolean);
    nameInput.value = parts.length ? parts[parts.length - 1] : "";
  }
  const listEl = el("team-article-folder-list");
  if (!listEl) return;
  if (!dirs.length) {
    listEl.innerHTML = '<div style="padding:6px;color:var(--muted);font-size:11px;">Nenhuma subpasta.</div>';
    return;
  }
  listEl.innerHTML = dirs.map((d) =>
    '<button type="button" class="ctrl entry" data-path="' + escapeHtml(String(d.path || "")) + '">📁 '
    + escapeHtml(String(d.name || d.path || "")) + "</button>"
  ).join("");
  listEl.querySelectorAll("[data-path]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const picked = btn.getAttribute("data-path") || "";
      if (!picked) return;
      try {
        await browseTeamPickerArtFolders(picked);
      } catch (e) {
        setTeamArticleFolderStatus("Erro: " + e.message, "err");
      }
    });
  });
}

async function browseTeamPickerArtFolders(path, goUp) {
  const q = new URLSearchParams();
  if (path) q.set("path", path);
  if (goUp) q.set("up", "1");
  const url = "/hermes/local-folders" + (q.toString() ? "?" + q.toString() : "");
  const { data } = await chatFetchJson(url, { method: "GET" }, 15000);
  if (!data.ok) throw new Error(data.error || "falha ao listar pastas");
  renderTeamPickerArtFolderList(data);
}

let teamArticleFolderState = {
  teamId: "",
  tab: "disk",
  uploadFiles: [],
};

function setTeamArticleFolderStatus(msg, kind) {
  const box = el("team-article-folder-status");
  if (!box) return;
  box.textContent = msg || "";
  box.className = "team-article-folder-status" + (kind ? " " + kind : "");
}

function setTeamArticleFolderTab(tab) {
  teamArticleFolderState.tab = tab === "upload" ? "upload" : "disk";
  const tabs = el("team-article-folder-tabs");
  if (tabs) {
    tabs.querySelectorAll("button[data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === teamArticleFolderState.tab);
    });
  }
  const diskPanel = el("team-article-folder-disk-panel");
  const uploadPanel = el("team-article-folder-upload-panel");
  if (diskPanel) diskPanel.hidden = teamArticleFolderState.tab !== "disk";
  if (uploadPanel) uploadPanel.hidden = teamArticleFolderState.tab !== "upload";
}

async function openTeamArticleFolderDialog(teamId, tab) {
  const tid = teamPickerActiveTeamId(teamId);
  if (!tid) {
    if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
    return;
  }
  teamArticleFolderState.teamId = tid;
  teamArticleFolderState.uploadFiles = [];
  setTeamArticleFolderStatus("", "");
  const summary = el("team-article-folder-upload-summary");
  if (summary) summary.textContent = "";
  const uploadName = el("team-article-folder-upload-name");
  if (uploadName) uploadName.value = "";
  const team = getTeamPickerSelectedTeam();
  const guess = (team && team.local_path) || teamPickerArtBrowsePath || "";
  setTeamArticleFolderTab(tab || "disk");
  const overlay = el("team-article-folder-overlay");
  if (overlay) overlay.hidden = false;
  try {
    await browseTeamPickerArtFolders(guess);
  } catch (e) {
    setTeamArticleFolderStatus("Erro ao listar pastas: " + e.message, "err");
  }
}

function closeTeamArticleFolderDialog() {
  const overlay = el("team-article-folder-overlay");
  if (overlay) overlay.hidden = true;
  teamArticleFolderState.uploadFiles = [];
}

async function confirmTeamArticleFolderDisk(teamId) {
  const tid = teamPickerActiveTeamId(teamId || teamArticleFolderState.teamId);
  if (!teamPickerArtBrowsePath) {
    setTeamArticleFolderStatus("Selecione uma pasta no disco.", "err");
    return false;
  }
  const nameInput = el("team-article-folder-name");
  const name = (nameInput && nameInput.value || "").trim()
    || teamPickerArtBrowsePath.split("/").filter(Boolean).pop()
    || "Artigos";
  setTeamArticleFolderStatus("A cadastrar pasta…", "");
  try {
    const { data: reg } = await chatFetchJson("/latex/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path: teamPickerArtBrowsePath }),
    }, 20000);
    const wsId = (reg.workspace && reg.workspace.id) || reg.workspace_id || "";
    if (!reg.ok && !wsId) throw new Error(reg.error || "falha ao cadastrar pasta");
    const linked = await addTeamPickerArticlesWorkspace(tid, wsId);
    if (!linked) return false;
    setTeamArticleFolderStatus('✅ Pasta "' + name + '" vinculada ao time.', "ok");
    if (window.qcmNavToast) window.qcmNavToast("Pasta de artigos vinculada.", "ok");
    closeTeamArticleFolderDialog();
    await loadTeamPickerArticlesWorkspaces(tid);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamArticleFolderStatus("Erro: " + e.message, "err");
    return false;
  }
}

async function confirmTeamArticleFolderUpload(teamId) {
  const tid = teamPickerActiveTeamId(teamId || teamArticleFolderState.teamId);
  const files = teamArticleFolderState.uploadFiles || [];
  if (!files.length) {
    setTeamArticleFolderStatus("Escolha uma pasta do computador primeiro.", "err");
    return false;
  }
  const nameInput = el("team-article-folder-upload-name");
  const folderName = (nameInput && nameInput.value || "").trim()
    || (files[0] && files[0].webkitRelativePath
      ? files[0].webkitRelativePath.split("/")[0] : "")
    || "Artigos";
  setTeamArticleFolderStatus("A enviar " + files.length + " ficheiro(s)…", "");
  try {
    const payloadFiles = [];
    for (const file of files) {
      const rel = String(file.webkitRelativePath || file.name || "").replace(/\\/g, "/");
      const dataBase64 = await readFileAsBase64(file);
      payloadFiles.push({ relative_path: rel, data_base64: dataBase64 });
    }
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(tid) + "/articles/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: folderName, files: payloadFiles }),
    }, 300000);
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return false;
      throw new Error(data.error || "falha ao enviar pasta");
    }
    if (data.team) applyTeamPickerArticlesTeam(tid, data.team);
    setTeamArticleFolderStatus(
      data.warning ? ("⚠ " + data.warning) : "✅ Pasta enviada e vinculada ao time.",
      data.warning ? "err" : "ok",
    );
    if (window.qcmNavToast) window.qcmNavToast("Pasta de artigos vinculada.", "ok");
    closeTeamArticleFolderDialog();
    invalidateTeamPickerPreviewCache(tid);
    await loadTeamPickerArticlesWorkspaces(tid);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamArticleFolderStatus("Erro: " + e.message, "err");
    return false;
  }
}

async function confirmTeamArticleFolderDialog() {
  if (teamArticleFolderState.tab === "upload") {
    await confirmTeamArticleFolderUpload(teamArticleFolderState.teamId);
  } else {
    await confirmTeamArticleFolderDisk(teamArticleFolderState.teamId);
  }
}

function bindTeamArticleFolderDialog() {
  const overlay = el("team-article-folder-overlay");
  if (!overlay || overlay._bound) return;
  overlay._bound = true;
  el("team-article-folder-close").onclick = closeTeamArticleFolderDialog;
  el("team-article-folder-cancel").onclick = closeTeamArticleFolderDialog;
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) closeTeamArticleFolderDialog(); });
  el("team-article-folder-tabs").querySelectorAll("button[data-tab]").forEach((btn) => {
    btn.onclick = () => setTeamArticleFolderTab(btn.getAttribute("data-tab") || "disk");
  });
  el("team-article-folder-up").onclick = async () => {
    if (!teamPickerArtBrowsePath) return;
    try {
      await browseTeamPickerArtFolders(teamPickerArtBrowsePath, true);
    } catch (e) {
      setTeamArticleFolderStatus("Erro: " + e.message, "err");
    }
  };
  el("team-article-folder-confirm").onclick = () => { void confirmTeamArticleFolderDialog(); };
  el("team-article-folder-pick").onclick = () => el("team-article-folder-input").click();
  el("team-article-folder-input").addEventListener("change", () => {
    const input = el("team-article-folder-input");
    const picked = input && input.files ? Array.from(input.files) : [];
    if (input) input.value = "";
    teamArticleFolderState.uploadFiles = picked;
    const summary = el("team-article-folder-upload-summary");
    const uploadName = el("team-article-folder-upload-name");
    if (!picked.length) {
      if (summary) summary.textContent = "";
      return;
    }
    const root = picked[0].webkitRelativePath
      ? picked[0].webkitRelativePath.split("/")[0]
      : "pasta";
    if (uploadName && !uploadName.value.trim()) uploadName.value = root;
    const texCount = picked.filter((f) => /\.tex$/i.test(f.name || "")).length;
    if (summary) {
      summary.textContent = picked.length + " ficheiro(s) · " + texCount + " .tex · raiz: " + root;
    }
    setTeamArticleFolderTab("upload");
  });
}

bindTeamArticleFolderDialog();

let chatTeamContactPicker = {
  filter: "all",
  query: "",
  selected: null,
  manualPhone: "",
  contacts: [],
  mode: "contact",
  bound: false,
};

function setTeamPickerWaAllowlistStatus(msg, kind) {
  const elStatus = el("team-picker-wa-allowlist-status");
  if (!elStatus) return;
  elStatus.textContent = msg || "";
  elStatus.className = "team-picker-path-status" + (kind ? " " + kind : "");
}

async function addTeamWaAllowlistDest(teamId, number) {
  if (!teamId || !String(number || "").trim()) return false;
  setTeamPickerWaAllowlistStatus("A cadastrar…", "");
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamId) + "/whatsapp/numbers/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number: String(number).trim() }),
      },
      15000,
    );
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return false;
      throw new Error(data.error || "falha ao cadastrar");
    }
    invalidateTeamPickerPreviewCache(teamId);
    setTeamPickerWaAllowlistStatus("✅ Destino cadastrado na allowlist do time.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Allowlist do time atualizada", "ok");
    void loadTeamPickerPreview(teamId);
    return true;
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return false;
    setTeamPickerWaAllowlistStatus("Erro: " + e.message, "err");
    if (window.qcmNavToast) window.qcmNavToast("Erro: " + e.message, "warn");
    return false;
  }
}

function addTeamWaAllowlistManual(teamId) {
  const raw = prompt("WhatsApp / telefone ou JID de grupo (@g.us):");
  if (!raw || !raw.trim()) return;
  void addTeamWaAllowlistDest(teamId, raw.trim());
}

async function removeTeamWaAllowlistDest(teamId, number) {
  if (!teamId || !String(number || "").trim()) return;
  setTeamPickerWaAllowlistStatus("A remover…", "");
  try {
    const { data } = await chatFetchJson(
      "/teams/" + encodeURIComponent(teamId) + "/whatsapp/numbers/remove",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number: String(number).trim() }),
      },
      15000,
    );
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      throw new Error(data.error || "falha ao remover");
    }
    invalidateTeamPickerPreviewCache(teamId);
    setTeamPickerWaAllowlistStatus("✅ Destino removido.", "ok");
    if (window.qcmNavToast) window.qcmNavToast("Removido da allowlist do time", "ok");
    void loadTeamPickerPreview(teamId);
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    setTeamPickerWaAllowlistStatus("Erro: " + e.message, "err");
    if (window.qcmNavToast) window.qcmNavToast("Erro: " + e.message, "warn");
  }
}

function chatTeamContactPickerDest(c) {
  if (!c) return "";
  if (c.kind === "group") return String(c.jid || "").trim();
  return String(c.phone || c.jid || "").trim();
}

function updateChatTeamContactPickerChrome() {
  const isWa = chatTeamContactPicker.mode === "wa_allowlist";
  const title = el("chat-team-contact-title");
  const sub = el("chat-team-contact-sub");
  const confirmBtn = el("chat-team-contact-confirm");
  const manualHint = document.querySelector("#chat-team-contact-overlay .chat-team-contact-manual .hint");
  if (title) title.textContent = isWa ? "Cadastrar na allowlist do time" : "Buscar contato";
  if (sub) {
    sub.textContent = isWa
      ? "Selecione número ou grupo para a allowlist de envio WhatsApp deste time"
      : "Agenda WhatsApp e contatos dos times";
  }
  if (confirmBtn) {
    confirmBtn.textContent = isWa ? "Adicionar à allowlist" : "Adicionar ao time";
  }
  if (manualHint) {
    manualHint.textContent = isWa
      ? "Número ou grupo não listado — será cadastrado na allowlist do time."
      : "Use se o contato não aparecer na agenda — será adicionado ao time e, se marcado abaixo, à allowlist geral.";
  }
  updateChatTeamContactAllowlistRow();
  updateChatTeamContactSelection();
}

function setTeamPickerPathStatus(msg, kind) {
  const elStatus = el("team-picker-path-status");
  if (!elStatus) return;
  elStatus.textContent = msg || "";
  elStatus.className = "team-picker-path-status" + (kind ? " " + kind : "");
}

async function saveTeamPickerLocalPath(teamId, createDir) {
  const path = resolveTeamPickerProjectPath(!!createDir);
  if (!teamId) return;
  if (!path) {
    setTeamPickerPathStatus(
      createDir ? "Informe o nome da nova pasta ou um caminho completo." : "Informe um caminho local.",
      "err"
    );
    return;
  }
  setTeamPickerPathStatus("A guardar…", "");
  try {
    const payload = {
      local_path: path,
      project_source: "local",
    };
    if (createDir) payload.create_local_path = true;
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 20000);
    if (!data.ok) {
      if (chatRedirectIfNotAuthenticated(data.error)) return;
      throw new Error(data.error || "falha ao salvar");
    }
    invalidateTeamPickerPreviewCache(teamId);
    const t = chatTeams.find((x) => x.id === teamId);
    if (t && data.team) Object.assign(t, data.team);
    setTeamPickerPathStatus(
      createDir ? "✅ Pasta criada e vinculada ao time." : "✅ Caminho local atualizado.",
      "ok"
    );
    if (window.qcmNavToast) {
      window.qcmNavToast(createDir ? "Diretório criado e vinculado." : "local_path atualizado.", "ok");
    }
    void loadTeamPickerPreview(teamId);
  } catch (e) {
    if (chatRedirectIfNotAuthenticated(e.message)) return;
    setTeamPickerPathStatus("Erro: " + e.message, "err");
  }
}

function renderTeamPickerPathBrowser(data) {
  const current = String(data.current || "");
  const parent = String(data.parent || "");
  const dirs = Array.isArray(data.directories) ? data.directories : [];
  teamPickerPathBrowse = current;
  const curEl = el("team-picker-path-current");
  const upBtn = el("team-picker-path-up");
  const listEl = el("team-picker-path-folder-list");
  if (curEl) curEl.textContent = current || "—";
  if (upBtn) upBtn.disabled = !parent || parent === current;
  updateTeamPickerProjectPathPreview();
  if (!listEl) return;
  if (!dirs.length) {
    listEl.innerHTML = '<div style="padding:6px;color:var(--muted);font-size:11px;">Nenhuma subpasta.</div>';
    return;
  }
  listEl.innerHTML = dirs.map((d) =>
    '<button type="button" class="ctrl entry" data-path="' + escapeHtml(String(d.path || "")) + '" '
    + 'style="width:100%;text-align:left;margin-bottom:3px;font-size:11px;">📁 '
    + escapeHtml(String(d.name || d.path || "")) + "</button>"
  ).join("");
  listEl.querySelectorAll("[data-path]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const picked = btn.getAttribute("data-path") || "";
      if (!picked) return;
      const nameInput = el("team-picker-project-new-name");
      if (nameInput) nameInput.value = "";
      const input = el("team-picker-local-path");
      if (input) input.value = picked;
      await browseTeamPickerPathFolders(picked);
    });
  });
}

async function browseTeamPickerPathFolders(path, goUp) {
  const q = new URLSearchParams();
  if (path) q.set("path", path);
  if (goUp) q.set("up", "1");
  const url = "/hermes/local-folders" + (q.toString() ? "?" + q.toString() : "");
  const { data } = await chatFetchJson(url, { method: "GET" }, 15000);
  if (!data.ok) throw new Error(data.error || "falha ao listar pastas");
  renderTeamPickerPathBrowser(data);
}

function bindTeamPickerManage(teamId) {
  const browseBtn = el("team-picker-path-browse");
  const saveBtn = el("team-picker-path-save");
  const createBtn = el("team-picker-path-create");
  const createHereBtn = el("team-picker-path-create-here");
  const projectNameInput = el("team-picker-project-new-name");
  const upBtn = el("team-picker-path-up");
  const browser = el("team-picker-path-browser");
  const addContactBtn = el("team-picker-add-contact");
  const addManualBtn = el("team-picker-add-contact-manual");
  const addWaAllowBtn = el("team-picker-add-wa-allow");
  const addWaAllowManualBtn = el("team-picker-add-wa-allow-manual");
  const contactsList = el("team-picker-contacts-list");
  const waAllowList = el("team-picker-wa-allowlist");

  if (browseBtn) {
    browseBtn.onclick = async () => {
      if (!browser) return;
      const input = el("team-picker-local-path");
      const guess = (input && input.value || "").trim() || teamPickerPathBrowse;
      browser.hidden = false;
      try {
        await browseTeamPickerPathFolders(guess);
      } catch (e) {
        setTeamPickerPathStatus("Erro: " + e.message, "err");
      }
    };
  }
  if (upBtn) {
    upBtn.onclick = async () => {
      if (!teamPickerPathBrowse) return;
      try {
        await browseTeamPickerPathFolders(teamPickerPathBrowse, true);
      } catch (e) {
        setTeamPickerPathStatus("Erro: " + e.message, "err");
      }
    };
  }
  if (saveBtn) saveBtn.onclick = () => { void saveTeamPickerLocalPath(teamId, false); };
  if (createBtn) createBtn.onclick = () => { void saveTeamPickerLocalPath(teamId, true); };
  if (createHereBtn) createHereBtn.onclick = () => { void saveTeamPickerLocalPath(teamId, true); };
  if (projectNameInput && !projectNameInput._boundPreview) {
    projectNameInput._boundPreview = true;
    projectNameInput.addEventListener("input", updateTeamPickerProjectPathPreview);
  }
  if (addContactBtn) addContactBtn.onclick = () => { void openChatTeamContactPicker(teamId, "contact"); };
  if (addManualBtn) addManualBtn.onclick = () => { addChatTeamContactManual(teamId); };
  if (addWaAllowBtn) addWaAllowBtn.onclick = () => { void openChatTeamContactPicker(teamId, "wa_allowlist"); };
  if (addWaAllowManualBtn) addWaAllowManualBtn.onclick = () => { addTeamWaAllowlistManual(teamId); };
  if (contactsList) {
    contactsList.querySelectorAll("[data-remove-team-contact]").forEach((btn) => {
      btn.onclick = () => {
        void removeChatTeamContact(teamId, btn.getAttribute("data-remove-team-contact") || "");
      };
    });
  }
  if (waAllowList) {
    waAllowList.querySelectorAll("[data-remove-wa-allow]").forEach((btn) => {
      btn.onclick = () => {
        void removeTeamWaAllowlistDest(teamId, btn.getAttribute("data-remove-wa-allow") || "");
      };
    });
  }

  const artSaveBtn = el("team-picker-articles-save");
  const artClearBtn = el("team-picker-articles-clear");
  const artFolderBtn = el("team-picker-articles-folder-dialog");
  void loadTeamPickerArticlesWorkspaces(teamId);
  if (artSaveBtn) {
    artSaveBtn.onclick = async () => {
      const wsId = (el("team-picker-articles-ws") && el("team-picker-articles-ws").value || "").trim();
      await addTeamPickerArticlesWorkspace(teamId, wsId);
    };
  }
  if (artClearBtn) {
    artClearBtn.onclick = async () => {
      await clearTeamPickerArticlesWorkspaces(teamId);
    };
  }
  if (artFolderBtn) {
    artFolderBtn.onclick = () => { void openTeamArticleFolderDialog(teamId, "disk"); };
  }
  bindTeamPickerArticlePreview(teamId);
  bindTeamPickerNormas(teamId);
  bindTeamPickerLegalEvaluations(teamId);

  const preview = el("team-picker-preview");
  if (preview) {
    bindTeamFileContextButtons(preview, teamId);
    bindTeamRecentFilesBulkContext(preview, teamId);
    bindTeamPickerNormaPreviewClicks(preview, teamId);
  }
}

function chatTeamContactHaystack(c) {
  return [c.name, c.phone, c.jid, c.label, c.team_name, c.source, c.chat_type].join(" ").toLowerCase();
}

function filterChatTeamContacts() {
  const q = chatTeamContactPicker.query.trim().toLowerCase();
  return chatTeamContactPicker.contacts.filter((c) => {
    if (chatTeamContactPicker.filter === "dm" && c.kind !== "dm") return false;
    if (chatTeamContactPicker.filter === "group" && c.kind !== "group") return false;
    if (q && !chatTeamContactHaystack(c).includes(q)) return false;
    return true;
  });
}

function chatTeamContactNeedsAllowlist(c) {
  if (!c) return false;
  return !(c.in_global_allowlist || c.in_allowlist);
}

function chatTeamContactBadgesHtml(c) {
  if (!c) return "";
  const teamOk = !!(c.in_team_allowlist);
  const globalOk = !!(c.in_global_allowlist || c.in_allowlist);
  const canSend = !!(c.can_send_whatsapp || teamOk);
  let html = "";
  if (!teamOk) html += '<span class="chat-team-contact-badge warn">fora do time</span>';
  else html += '<span class="chat-team-contact-badge ok">no time</span>';
  if (globalOk) html += '<span class="chat-team-contact-badge ok">geral ✓</span>';
  if (!canSend) html += '<span class="chat-team-contact-badge bad">envio bloq</span>';
  return html;
}

function chatTeamContactSelection() {
  const manual = String(chatTeamContactPicker.manualPhone || "").replace(/\D/g, "");
  if (manual.length >= 10) {
    return {
      name: "+" + manual,
      phone: manual,
      jid: manual + "@s.whatsapp.net",
      kind: "dm",
      in_allowlist: false,
      manual: true,
    };
  }
  return chatTeamContactPicker.selected;
}

function updateChatTeamContactAllowlistRow() {
  const row = el("chat-team-contact-allowlist-row");
  const cb = el("chat-team-contact-allowlist");
  const c = chatTeamContactSelection();
  if (!row || !cb) return;
  if (chatTeamContactPicker.mode === "wa_allowlist") {
    row.hidden = true;
    return;
  }
  const needs = chatTeamContactNeedsAllowlist(c);
  row.hidden = !c || !needs;
  cb.checked = needs;
}

function renderChatTeamContactPickerList() {
  const box = el("chat-team-contact-list");
  if (!box) return;
  const rows = filterChatTeamContacts();
  if (!rows.length) {
    const manual = String(chatTeamContactPicker.manualPhone || "").replace(/\D/g, "");
    if (manual.length >= 10) {
      box.innerHTML = '<div class="team-picker-contact-empty">Número manual pronto para adicionar: +' + escapeHtml(manual) + "</div>";
      updateChatTeamContactSelection();
      return;
    }
    box.innerHTML = '<div class="team-picker-contact-empty">Nenhum resultado. Tente outro termo ou informe o número acima.</div>';
    updateChatTeamContactSelection();
    return;
  }
  box.innerHTML = rows.map((c, i) => {
    const sel = chatTeamContactPicker.selected === c;
    const sub = c.kind === "group" ? (c.jid || "") : ("+" + (c.phone || c.jid || ""));
    const badge = chatTeamContactBadgesHtml(c);
    return '<div class="chat-team-contact-item' + (sel ? " selected" : "") + '" data-idx="' + i + '">'
      + '<div class="avatar">' + (c.kind === "group" ? "👥" : "👤") + "</div>"
      + '<div class="meta"><div class="title">' + escapeHtml(c.name || sub) + badge + "</div>"
      + '<div class="sub">' + escapeHtml(sub) + "</div></div></div>";
  }).join("");
  box._rows = rows;
  box.querySelectorAll(".chat-team-contact-item").forEach((node) => {
    node.onclick = () => {
      chatTeamContactPicker.selected = box._rows[Number(node.dataset.idx)];
      const manual = el("chat-team-contact-manual-phone");
      if (manual) manual.value = "";
      chatTeamContactPicker.manualPhone = "";
      renderChatTeamContactPickerList();
      updateChatTeamContactSelection();
    };
  });
  updateChatTeamContactSelection();
}

function updateChatTeamContactSelection() {
  const sel = el("chat-team-contact-selection");
  const btn = el("chat-team-contact-confirm");
  if (!sel || !btn) return;
  sel.classList.remove("err");
  const c = chatTeamContactSelection();
  if (!c) {
    sel.textContent = "Nenhum selecionado";
    btn.disabled = true;
    updateChatTeamContactAllowlistRow();
    return;
  }
  const kind = c.kind === "group" ? "grupo" : "contato";
  sel.textContent = (c.name || c.phone || c.jid || "—") + " (" + kind + ")";
  btn.disabled = false;
  updateChatTeamContactAllowlistRow();
}

async function loadChatTeamContactPickerContacts(teamId) {
  const q = teamId ? ("?team_id=" + encodeURIComponent(teamId)) : "";
  const { data } = await chatFetchJson("/whatsapp/allowlist/contacts" + q, { method: "GET" }, 20000);
  if (!data.ok && data.ok !== undefined) throw new Error(data.error || "falha ao carregar contatos");
  chatTeamContactPicker.contacts = data.contacts || [];
}

async function openChatTeamContactPicker(teamId, mode) {
  const overlay = el("chat-team-contact-overlay");
  if (!overlay) return;
  chatTeamContactPicker.mode = mode === "wa_allowlist" ? "wa_allowlist" : "contact";
  chatTeamContactPicker.filter = "all";
  chatTeamContactPicker.query = "";
  chatTeamContactPicker.selected = null;
  chatTeamContactPicker.manualPhone = "";
  chatTeamContactPicker._teamId = teamId;
  const search = el("chat-team-contact-search");
  const manual = el("chat-team-contact-manual-phone");
  if (search) search.value = "";
  if (manual) manual.value = "";
  updateChatTeamContactPickerChrome();
  overlay.hidden = false;
  overlay.classList.add("show");
  try {
    await loadChatTeamContactPickerContacts(teamId);
    renderChatTeamContactPickerList();
    updateChatTeamContactSelection();
    if (search) setTimeout(() => search.focus(), 30);
  } catch (e) {
    if (window.qcmNavToast) window.qcmNavToast("Erro ao carregar agenda: " + e.message, "warn");
  }
}

function closeChatTeamContactPicker() {
  const overlay = el("chat-team-contact-overlay");
  if (overlay) {
    overlay.hidden = true;
    overlay.classList.remove("show");
  }
  chatTeamContactPicker.selected = null;
  chatTeamContactPicker.manualPhone = "";
  chatTeamContactPicker._teamId = null;
  chatTeamContactPicker.mode = "contact";
}

async function confirmChatTeamContactPicker() {
  const teamId = chatTeamContactPicker._teamId;
  const c = chatTeamContactSelection();
  if (!teamId || !c) return;

  if (chatTeamContactPicker.mode === "wa_allowlist") {
    const dest = chatTeamContactPickerDest(c);
    if (!dest) return;
    const ok = await addTeamWaAllowlistDest(teamId, dest);
    if (ok) closeChatTeamContactPicker();
    return;
  }

  const allowCb = el("chat-team-contact-allowlist");
  const addToAllowlist = !!(allowCb && allowCb.checked && chatTeamContactNeedsAllowlist(c));
  const contact = {
    name: c.name || c.phone || c.jid || "Contato",
    whatsapp: c.kind === "group" ? (c.jid || "") : (c.phone || c.jid || ""),
    email: c.email || "",
    phone: c.phone || "",
  };
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contact, add_to_allowlist: addToAllowlist }),
    }, 15000);
    if (!data.ok) throw new Error(data.error || "falha ao adicionar contato");
    invalidateTeamPickerPreviewCache(teamId);
    closeChatTeamContactPicker();
    const msg = addToAllowlist
      ? "Contato adicionado (allowlist do time + geral opcional): " + contact.name
      : "Contato adicionado (allowlist do time): " + contact.name;
    if (window.qcmNavToast) window.qcmNavToast(msg, "ok");
    void loadTeamPickerPreview(teamId);
  } catch (e) {
    const sel = el("chat-team-contact-selection");
    if (sel) {
      sel.classList.add("err");
      sel.textContent = "Erro: " + e.message;
    }
  }
}

function addChatTeamContactManual(teamId) {
  const name = prompt("Nome do contato:");
  if (!name || !name.trim()) return;
  const email = prompt("Email (opcional):") || "";
  const whatsapp = prompt("WhatsApp / telefone (opcional):") || "";
  const addToAllowlist = !!whatsapp.trim();
  void chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/contacts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contact: { name: name.trim(), email: email.trim(), whatsapp: whatsapp.trim() },
      add_to_allowlist: addToAllowlist,
    }),
  }, 15000).then(({ data }) => {
    if (!data.ok) throw new Error(data.error || "falha");
    invalidateTeamPickerPreviewCache(teamId);
    if (window.qcmNavToast) window.qcmNavToast("Contato adicionado", "ok");
    void loadTeamPickerPreview(teamId);
  }).catch((e) => {
    if (window.qcmNavToast) window.qcmNavToast("Erro: " + e.message, "warn");
  });
}

async function removeChatTeamContact(teamId, contactId) {
  if (!teamId || !contactId) return;
  try {
    const { data } = await chatFetchJson("/teams/" + encodeURIComponent(teamId) + "/contacts", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: contactId }),
    }, 15000);
    if (!data.ok) throw new Error(data.error || "falha ao remover");
    invalidateTeamPickerPreviewCache(teamId);
    if (window.qcmNavToast) window.qcmNavToast("Contato removido", "ok");
    void loadTeamPickerPreview(teamId);
  } catch (e) {
    if (window.qcmNavToast) window.qcmNavToast("Erro: " + e.message, "warn");
  }
}

function bindChatTeamContactPicker() {
  if (chatTeamContactPicker.bound) return;
  chatTeamContactPicker.bound = true;
  const overlay = el("chat-team-contact-overlay");
  const closeBtn = el("chat-team-contact-close");
  const cancelBtn = el("chat-team-contact-cancel");
  const confirmBtn = el("chat-team-contact-confirm");
  const search = el("chat-team-contact-search");
  const filters = el("chat-team-contact-filters");
  if (closeBtn) closeBtn.addEventListener("click", closeChatTeamContactPicker);
  if (cancelBtn) cancelBtn.addEventListener("click", closeChatTeamContactPicker);
  if (confirmBtn) confirmBtn.addEventListener("click", () => { void confirmChatTeamContactPicker(); });
  if (overlay) {
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) closeChatTeamContactPicker();
    });
    const dialog = overlay.querySelector(".chat-team-contact-dialog");
    if (dialog) dialog.addEventListener("click", (ev) => ev.stopPropagation());
  }
  if (search) {
    search.addEventListener("input", () => {
      chatTeamContactPicker.query = search.value || "";
      renderChatTeamContactPickerList();
    });
  }
  const manualPhone = el("chat-team-contact-manual-phone");
  if (manualPhone) {
    manualPhone.addEventListener("input", () => {
      chatTeamContactPicker.manualPhone = manualPhone.value || "";
      if (chatTeamContactPicker.manualPhone.trim()) {
        chatTeamContactPicker.selected = null;
      }
      renderChatTeamContactPickerList();
    });
  }
  if (filters) {
    filters.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-filter]");
      if (!btn) return;
      chatTeamContactPicker.filter = btn.dataset.filter || "all";
      filters.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b.dataset.filter === chatTeamContactPicker.filter);
      });
      renderChatTeamContactPickerList();
    });
  }
}

function isTeamPickerDialogOpen() {
  const overlay = el("team-picker-overlay");
  return !!(overlay && !overlay.hidden);
}

async function confirmTeamPickerSelection() {
  const teamId = teamPickerState.selectedTeamId;
  if (!teamId) return;
  if (window.__qcmTeamPickerMode === "kanban") {
    const onAssign = teamPickerState.onAssign;
    closeTeamPickerDialog();
    if (typeof onAssign === "function") onAssign(teamId);
    return;
  }
  const sessionKey = teamPickerState.sessionKey || activeKey;
  if (!sessionKey) return;
  const result = await assignTeamToSession(sessionKey, teamId);
  if (!result.ok) {
    if (window.qcmNavToast) window.qcmNavToast("Erro ao alocar time: " + (result.error || "falha"), "warn");
    return;
  }
  syncChatTeamSelectToSession(sessionKey);
  updateTeamPickerButton();
  const onAssign = teamPickerState.onAssign;
  closeTeamPickerDialog();
  if (typeof onAssign === "function") onAssign(teamId);
}

function openTeamPickerDialog(opts) {
  opts = opts || {};
  const overlay = el("team-picker-overlay");
  if (!overlay) return;
  ensureTeamPickerCreateUi();
  teamPickerState.sessionKey = opts.sessionKey || activeKey || null;
  teamPickerState.onAssign = opts.onAssign || null;
  teamPickerState.required = !!opts.required;
  teamPickerState.filter = "";
  const sess = teamPickerState.sessionKey
    ? sessions.find((s) => s.key === teamPickerState.sessionKey)
    : null;
  teamPickerState.selectedTeamId = opts.initialTeamId
    || ((sess && sess.team_id) ? sess.team_id : null);
  const search = el("team-picker-search");
  if (search) search.value = "";
  const sub = el("team-picker-sub");
  if (sub) {
    sub.textContent = opts.subtitle || (teamPickerState.required
      ? "Esta conversa precisa de um time. Configure diretório local, artigos e contatos antes de confirmar."
      : (window.__qcmTeamPickerMode === "kanban"
        ? "Veja contexto, vincule pasta local, cadastre artigos e gerencie contatos do time antes de abrir o kanban."
        : "Veja contexto, vincule pasta local, cadastre artigos e gerencie contatos do time antes de alocar a conversa."));
  }
  const selectBtnLabel = el("team-picker-select-btn");
  if (selectBtnLabel && opts.confirmButtonLabel) {
    selectBtnLabel.textContent = opts.confirmButtonLabel;
  }
  overlay.hidden = false;
  const listEl = el("team-picker-list");
  const hasCachedTeams = chatTeamsLoaded && chatTeams.length > 0;
  const teamsStale = !chatTeamsLoaded || (Date.now() - chatTeamsLoadedAt > CHAT_TEAMS_STALE_MS);

  function finishTeamPickerOpen() {
    reapplyLastChatTeamsCreated();
    renderTeamPickerList();
    if (teamPickerState.selectedTeamId) {
      void loadTeamPickerPreview(teamPickerState.selectedTeamId);
    } else {
      const preview = el("team-picker-preview");
      const selectBtn = el("team-picker-select-btn");
      if (preview) {
        preview.innerHTML = '<div class="team-picker-preview-empty">Selecione um time à esquerda para ver o contexto completo.</div>';
      }
      if (selectBtn) selectBtn.disabled = true;
    }
    if (search) setTimeout(() => search.focus(), 0);
  }

  if (hasCachedTeams) {
    finishTeamPickerOpen();
  } else if (listEl) {
    listEl.innerHTML = '<div class="team-picker-preview-loading">A carregar times…</div>';
  }

  if (hasCachedTeams && !teamsStale) return;

  void Promise.resolve(chatTeamsRefreshPromise)
    .then(() => loadChatTeams({ force: true }))
    .then(() => {
      if (!hasCachedTeams || teamsStale) finishTeamPickerOpen();
    })
    .catch(() => {
      if (!hasCachedTeams) finishTeamPickerOpen();
    });
}

function teamPickerToast(msg, kind) {
  const text = String(msg || "").trim();
  if (!text) return;
  if (typeof window.qcmNavToast === "function") {
    window.qcmNavToast(text, kind || "info");
    return;
  }
  if (typeof kbToast === "function") {
    kbToast(text);
    return;
  }
  alert(text);
}

async function submitTeamPickerCreate() {
  const createInput = el("team-picker-new-name");
  const createBtn = el("team-picker-create-btn");
  const name = (createInput && createInput.value || "").trim();
  if (!name) {
    if (createInput) createInput.focus();
    teamPickerToast("Digite o nome do novo time.", "warn");
    return;
  }
  if (createBtn && createBtn._qcmCreating) return;
  const prevLabel = createBtn ? createBtn.textContent : "";
  if (createBtn) {
    createBtn._qcmCreating = true;
    createBtn.disabled = true;
    createBtn.textContent = "Criando…";
  }
  try {
    const { data } = await chatFetchJson("/teams", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    }, 15000);
    if (!data.ok || !data.team) {
      teamPickerToast(data.error || "Erro ao criar time", "err");
      return;
    }
    upsertChatTeam(data.team);
    if (typeof invalidateTeamPickerPreviewCache === "function") {
      invalidateTeamPickerPreviewCache(data.team.id);
    }
    if (createInput) createInput.value = "";
    teamPickerState.filter = "";
    const search = el("team-picker-search");
    if (search) search.value = "";
    renderTeamPickerList();
    void loadTeamPickerPreview(data.team.id);
    teamPickerToast("Time criado: " + (data.team.name || data.team.id), "ok");
  } catch (e) {
    teamPickerToast("Erro: " + (e.message || e), "err");
  } finally {
    if (createBtn) {
      createBtn._qcmCreating = false;
      createBtn.disabled = false;
      createBtn.textContent = prevLabel || "Criar";
    }
  }
}

function ensureTeamPickerCreateUi() {
  const createBtn = el("team-picker-create-btn");
  const createInput = el("team-picker-new-name");
  if (createBtn && !createBtn._qcmTeamPickerBound) {
    createBtn._qcmTeamPickerBound = true;
    createBtn.addEventListener("click", () => { void submitTeamPickerCreate(); });
  }
  if (createInput && !createInput._qcmTeamPickerBound) {
    createInput._qcmTeamPickerBound = true;
    createInput.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      void submitTeamPickerCreate();
    });
  }
}

function bindTeamPickerDialog() {
  bindChatTeamContactPicker();
  bindTeamFileDownloadPreviewModal();
  bindTeamFileActionDelegation(el("team-picker-preview"), "");
  bindTeamFileActionDelegation(el("team-context-body"), "");
  bindTeamWaAllowlistDelegation(el("team-picker-preview"));
  const btn = el("team-picker-btn");
  const overlay = el("team-picker-overlay");
  const closeBtn = el("team-picker-close");
  const cancelBtn = el("team-picker-cancel");
  const selectBtn = el("team-picker-select-btn");
  const search = el("team-picker-search");
  const listEl = el("team-picker-list");
  const reloadBtn = el("team-picker-reload-btn");
  if (reloadBtn && !reloadBtn._qcmTeamPickerBound) {
    reloadBtn._qcmTeamPickerBound = true;
    reloadBtn.addEventListener("click", () => {
      if (reloadBtn._qcmReloading) return;
      reloadBtn._qcmReloading = true;
      reloadBtn.style.opacity = "0.5";
      const listEl2 = el("team-picker-list");
      if (listEl2) listEl2.innerHTML = '<div class="team-picker-preview-loading">A recarregar times…</div>';
      loadChatTeams({ force: true }).then(() => {
        reapplyLastChatTeamsCreated();
        renderTeamPickerList();
        if (teamPickerState.selectedTeamId) {
          invalidateTeamPickerPreviewCache(teamPickerState.selectedTeamId);
          void loadTeamPickerPreview(teamPickerState.selectedTeamId);
        }
      }).catch(() => {
        renderTeamPickerList();
      }).finally(() => {
        reloadBtn._qcmReloading = false;
        reloadBtn.style.opacity = "";
      });
    });
  }
  if (btn && !btn._qcmTeamPickerBound) {
    btn._qcmTeamPickerBound = true;
    btn.addEventListener("click", () => {
      if (!activeKey) {
        if (window.qcmNavToast) window.qcmNavToast("Abra ou crie uma conversa primeiro.", "warn");
        return;
      }
      openTeamPickerDialog({ sessionKey: activeKey, onAssign: () => {
        renderSessions();
        updateChatTeamIndicator();
      }});
    });
  }
  if (closeBtn && !closeBtn._qcmTeamPickerBound) {
    closeBtn._qcmTeamPickerBound = true;
    closeBtn.addEventListener("click", () => closeTeamPickerDialog());
  }
  if (cancelBtn && !cancelBtn._qcmTeamPickerBound) {
    cancelBtn._qcmTeamPickerBound = true;
    cancelBtn.addEventListener("click", () => closeTeamPickerDialog());
  }
  if (overlay && !overlay._qcmTeamPickerBound) {
    overlay._qcmTeamPickerBound = true;
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay && !teamPickerState.required) closeTeamPickerDialog();
    });
  }
  if (selectBtn && !selectBtn._qcmTeamPickerBound) {
    selectBtn._qcmTeamPickerBound = true;
    selectBtn.addEventListener("click", () => { void confirmTeamPickerSelection(); });
  }
  if (search && !search._qcmTeamPickerBound) {
    search._qcmTeamPickerBound = true;
    search.addEventListener("input", () => {
      teamPickerState.filter = search.value || "";
      renderTeamPickerList();
    });
  }
  if (listEl && !listEl._qcmTeamPickerBound) {
    listEl._qcmTeamPickerBound = true;
    listEl.addEventListener("click", (ev) => {
      const item = ev.target.closest(".team-picker-item[data-team-id]");
      if (!item) return;
      void loadTeamPickerPreview(item.getAttribute("data-team-id"));
    });
  }
  ensureTeamPickerCreateUi();
}

function showTeamModal(sessionKey, opts) {
  openTeamPickerDialog({
    sessionKey: sessionKey,
    required: true,
    onAssign: (opts && opts.onAssign) ? opts.onAssign : function() {},
  });
}

const CONTEXT_DOC_SUFFIXES = [
  ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx",
  ".sh", ".sql", ".html", ".css", ".xml", ".csv", ".log", ".pdf", ".docx", ".tex", ".bib",
];

function isContextDocumentName(name) {
  const lower = String(name || "").toLowerCase();
  return CONTEXT_DOC_SUFFIXES.some((sfx) => lower.endsWith(sfx));
}

const NORMA_FILE_SUFFIXES = [".pdf", ".docx"];

const CHAT_FILE_EXT_GROUP =
  "pdf|docx?|tex|bib|png|jpe?g|gif|webp|svg|mp3|m4a|wav|webm|ogg|aac|flac|" +
  "mp4|mov|mkv|txt|md|json|jsonl|yaml|yml|csv|xlsx|xls|pptx?|zip|log|html|css|xml";

const CHAT_FILENAME_MENTION_RE = new RegExp(
  "(?<![\\w.\\-/\\\\])([\\w\\u00C0-\\u024F][\\w\\u00C0-\\u024F.\\-]*\\.(?:" + CHAT_FILE_EXT_GROUP + "))\\b",
  "gi",
);
const CHAT_FILE_NOUN_MENTION_RE = new RegExp(
  "\\b(?:arquivo|ficheiro|documento|anexo)\\s+[\"'«]?([\\w.\\-]+\\.(?:" + CHAT_FILE_EXT_GROUP + "))[\"'»]?",
  "gi",
);
const CHAT_PATH_MENTION_RE = new RegExp(
  "(?:~[/\\\\][^\\s\"']+|/[^\\s\"']+\\.(?:" + CHAT_FILE_EXT_GROUP + ")|[A-Za-z]:\\\\[^\\s\"']+)",
  "gi",
);
const CHAT_GENERIC_FILE_INTENT_RE = new RegExp(
  "(?:\\b(?:analisa|analise|abre|abra|envia|envie|mande|manda|processa|processe|" +
  "leia|lê|transcreve|transcreva|resume|resuma|corrija|corrigir|compila|compile|" +
  "extraia|extrair|revise|revisar|traduz|traduzir|converta|converter)\\s+" +
  "(?:o|a|este|esse|meu|seu|um|uma)?\\s*(?:arquivo|ficheiro|documento|anexo|pdf)\\b|" +
  "\\b(?:falar|falo|falei|mencion(?:ar|o|ei)|citei|citar)\\b.*?" +
  "\\b(?:arquivo|ficheiro|documento|anexo)(?:\\s+pdf|\\b))",
  "i",
);
const CHAT_FILE_MENTION_SKIP_RE =
  /^(?:abrir|mostrar|exibir|ver|editar|abre)\s+(?:o\s+)?editor(?:\s+de)?\s+latex\b/i;
const CHAT_LATEX_WIZARD_RE =
  /^(?:editar\s+(?:artigo|latex)\s+\S+\s+na\s+pasta\s+\S+|compilar\s+artigo\s+\S+\s+na\s+pasta\s+\S+|listar\s+artigos\s+da\s+pasta\s+\S+)$/i;
const CHAT_LATEX_PROJECT_FILE_INTENT_RE = new RegExp(
  "(?:\\b(?:re?compil(?:a|ar|e)|compil(?:a|ar|e)|compile|build|export(?:a|ar|e)|" +
  "ger(?:a|ar|e)|cri(?:a|ar|e)|produz(?:a|ir|e)|transform(?:a|ar|e))\\b" +
  "(?:\\s+\\w+){0,8}\\s*(?:\\.tex|pdf)\\b|" +
  "\\b(?:re?compil(?:a|ar|e)|compil(?:a|ar|e)|compile|ger(?:a|ar|e))\\s+" +
  "[\\w.\\-/\\\\]+\\.tex\\b|" +
  "\\b(?:onde|qual|caminho|localiz(?:a|ar|ação|e)|encontr(?:a|ar|ei)|" +
  "ach(?:a|ar|ei)|fica|est[aá])\\b" +
  "(?:\\s+\\w+){0,10}\\s*(?:\\.tex|pdf)\\b|" +
  "\\b(?:pdf|artigo)\\b.*?\\b(?:onde|qual\\s+(?:o\\s+)?caminho|localiz)\\b)",
  "i",
);
const CHAT_LATEX_DEP_MISSING_RE = new RegExp(
  "(?:\\b(?:falt(?:a|ando|am)|ausente|not\\s+found|n[aã]o\\s+(?:encontr(?:ad[oa]|ei)|ach(?:ei|ar)))" +
  "(?:\\s+\\w+){0,4}\\s*(?:\\.(?:cls|sty|bst)|pacote|biblioteca|template|documentclass)|" +
  "\\b(?:pacote|biblioteca|template|documentclass|tlmgr)\\b.*?" +
  "\\b(?:falt(?:a|ando|am)|ausente|not\\s+found|instal(?:ar|e))|" +
  "\\breaplic(?:ar|a)\\s+(?:o\\s+)?template\\b|" +
  "\\bcompila(?:r|c(?:a|ã)o)\\s+falhou\\b)",
  "i",
);
const CHAT_LATEX_ASSET_EXT = new Set([".cls", ".sty", ".bst", ".bbx", ".cbx", ".def", ".clo", ".fd"]);

function chatSkipFileUploadCheck(text) {
  const raw = String(text || "").trim();
  if (!raw) return true;
  if (CHAT_FILE_MENTION_SKIP_RE.test(raw) || CHAT_LATEX_WIZARD_RE.test(raw)) return true;
  if (CHAT_LATEX_PROJECT_FILE_INTENT_RE.test(raw)) return true;
  if (CHAT_LATEX_DEP_MISSING_RE.test(raw)) return true;
  return false;
}

function chatIsLatexAssetName(name) {
  const base = String(name || "").trim();
  if (!base) return false;
  const dot = base.lastIndexOf(".");
  if (dot < 0) return false;
  return CHAT_LATEX_ASSET_EXT.has(base.slice(dot).toLowerCase());
}

function chatAttachmentBasenames(attachments) {
  const names = new Set();
  (attachments || []).forEach((att) => {
    ["name", "path"].forEach((key) => {
      const raw = String((att && att[key]) || "").trim();
      if (!raw) return;
      const base = raw.replace(/\\/g, "/").split("/").pop();
      if (base) names.add(base.toLowerCase());
    });
  });
  return names;
}

function findMentionedChatFiles(text) {
  const raw = String(text || "").trim();
  if (!raw || chatSkipFileUploadCheck(raw)) return [];
  const found = [];
  const seen = new Set();
  [CHAT_FILENAME_MENTION_RE, CHAT_FILE_NOUN_MENTION_RE, CHAT_PATH_MENTION_RE].forEach((re) => {
    re.lastIndex = 0;
    let match;
    while ((match = re.exec(raw)) !== null) {
      let token = String((match[1] != null ? match[1] : match[0]) || "").trim();
      token = token.replace(/^["'«»]+|["'«»]+$/g, "");
      if (!token) continue;
      const base = token.replace(/\\/g, "/").split("/").pop();
      const key = base.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      found.push(base);
    }
  });
  return found;
}

const CHAT_INLINE_UPLOAD_MESSAGE =
  "Selecione o ficheiro anexo para continuar.";

function chatHasInlineFileUpload(text) {
  const raw = String(text || "").trim();
  if (!raw) return false;
  if (/data:(?:image|application|audio|video|text)\/[^;\s]+;base64,/i.test(raw)) return true;
  if (raw.length >= 600 && /(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{400,}={0,2})(?![A-Za-z0-9+/=])/.test(raw)) {
    return true;
  }
  return false;
}

function checkChatFileUploadRequired(text, attachments) {
  const raw = String(text || "").trim();
  if (chatHasInlineFileUpload(raw)) {
    return {
      required: true,
      files: [],
      message: CHAT_INLINE_UPLOAD_MESSAGE,
      inline_upload: true,
    };
  }
  const attached = chatAttachmentBasenames(attachments);
  if (chatExplicitAttachIntent(raw) && !attached.size) {
    return {
      required: true,
      files: findMentionedChatFiles(raw),
      message: "Selecione o ficheiro para anexar antes de enviar.",
    };
  }
  return { required: false, files: [], message: "" };
}

const CHAT_EXPLICIT_ATTACH_INTENT_RE = new RegExp(
  "(?:\\b(?:anexa|anexar|anexei|attach|upload|subir)\\b|" +
  "\\b(?:vou|quero|preciso)\\s+(?:de\\s+)?(?:enviar|mandar|subir|anexar)\\s+" +
  "(?:o|um|este|esse|meu)?\\s*(?:ficheiro|arquivo|documento|anexo|pdf|imagem|foto)\\b|" +
  "\\bsegue\\s+(?:em\\s+)?anexo\\b)",
  "i",
);

function chatExplicitAttachIntent(text) {
  const raw = String(text || "").trim();
  if (!raw || chatSkipFileUploadCheck(raw)) return false;
  return CHAT_EXPLICIT_ATTACH_INTENT_RE.test(raw);
}

function promptChatFileUpload(message, opts) {
  opts = opts || {};
  const msg = String(message || CHAT_INLINE_UPLOAD_MESSAGE);
  if (window.qcmNavToast) window.qcmNavToast(msg);
  if (typeof openChatFileUploadDialog === "function") {
    openChatFileUploadDialog(msg, { autoSend: true, files: opts.files });
    return;
  }
  const status = el("chat-status");
  if (status) status.textContent = msg;
  const fileInput = el("chat-attachment-input");
  if (fileInput) fileInput.click();
}

function isNormaEligibleFileName(name) {
  const lower = String(name || "").toLowerCase();
  return NORMA_FILE_SUFFIXES.some((sfx) => lower.endsWith(sfx));
}

function collectTeamFilesForContext(files) {
  const seen = new Set();
  const rows = [];
  for (const f of (Array.isArray(files) ? files : [])) {
    if (!f) continue;
    const fname = String(f.name || f.relative_path || "").trim();
    if (!fname || !isContextDocumentName(fname)) continue;
    const rel = String(f.relative_path || fname).trim();
    const key = String(f.path || rel).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({ relative_path: rel, name: fname });
  }
  return rows;
}

async function addTeamFilesToSessionContext(teamId, files) {
  const rows = (Array.isArray(files) ? files : [files]).filter((f) => f && (f.relative_path || f.name));
  if (!rows.length || !teamId) return;
  if (!activeKey) await startNewChatAsync();
  const sessionKey = activeKey;
  if (!sessionKey) throw new Error("sem sessão ativa");
  const team_files = rows.map((f) => ({
    team_id: teamId,
    relative_path: String(f.relative_path || f.name || "").trim(),
    display_name: String(f.name || f.relative_path || "arquivo").trim(),
  }));
  const { data } = await chatFetchJson("/openclaw/sessions/context-documents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_key: sessionKey,
      action: "add",
      team_files,
    }),
  });
  if (!data || !data.ok) {
    throw new Error((data && data.error) || "falha ao adicionar documentos do time");
  }
  chatContextDocs = Array.isArray(data.documents) ? data.documents.slice() : chatContextDocs;
  renderChatContextDocs();
  if (typeof window._renderArticlesContextPanel === "function") {
    window._renderArticlesContextPanel();
  }
  if (data.errors && data.errors.length && window.qcmNavToast) {
    window.qcmNavToast(data.errors.join("; "));
  } else if (window.qcmNavToast) {
    const n = (data.added || []).length;
    if (n) window.qcmNavToast(n + " documento(s) do time no contexto");
  }
  return data;
}

async function copyTeamFileToNorma(teamId, fileInfo) {
  if (!teamId || !fileInfo) return null;
  const rel = String(fileInfo.relative_path || fileInfo.name || "").trim();
  const name = String(fileInfo.name || rel || "arquivo").trim();
  const path = String(fileInfo.path || "").trim();
  if (!rel && !path) throw new Error("caminho do arquivo em falta");
  const payload = { relative_path: rel, name };
  if (path) payload.path = path;
  const { data } = await chatFetchJson(
    "/teams/" + encodeURIComponent(teamId) + "/normas/from-file",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    120000,
  );
  if (!data || !data.ok) {
    if (chatRedirectIfNotAuthenticated(data && data.error)) return null;
    throw new Error((data && data.error) || "falha ao copiar para normas");
  }
  const savedName = (data.norma && data.norma.name) || name;
  if (data.warning && window.qcmNavToast) window.qcmNavToast(data.warning, "err");
  if (window.qcmNavToast) {
    if (data.already_in_normas) {
      window.qcmNavToast("Já está nas normas indexadas: " + savedName, "ok");
    } else if (data.renamed_to) {
      window.qcmNavToast("Copiado como " + savedName + " (nome já existia)", "ok");
    } else {
      window.qcmNavToast("Copiado para normas indexadas: " + savedName, "ok");
    }
  }
  invalidateTeamPickerPreviewCache(teamId);
  await loadTeamPickerNormas(teamId);
  if (teamPickerState.selectedTeamId === teamId) {
    await loadTeamPickerPreview(teamId);
  }
  return savedName;
}

async function createCardFromTeamFile(teamId, fileInfo) {
  if (!teamId || !fileInfo) return null;
  const rel = String(fileInfo.relative_path || fileInfo.name || "").trim();
  const name = String(fileInfo.name || rel || "arquivo").trim();
  const path = String(fileInfo.path || "").trim();
  const title = String(fileInfo.title || name || "Novo card").trim();
  if (!rel && !path) throw new Error("caminho do arquivo em falta");
  const payload = { relative_path: rel, name, title };
  if (path) payload.path = path;
  const { data } = await chatFetchJson(
    "/teams/" + encodeURIComponent(teamId) + "/cards/from-file",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    120000,
  );
  if (!data || !data.ok) {
    if (chatRedirectIfNotAuthenticated(data && data.error)) return null;
    throw new Error((data && (data.attach_error || data.error)) || "falha ao criar card");
  }
  if (window.qcmNavToast) {
    const cardId = String(data.task_id || "").trim();
    window.qcmNavToast(
      "Card criado" + (cardId ? " " + cardId : "") + " com " + name + " anexado",
      "ok",
    );
  }
  return data;
}

function teamFileActionTeamId(btn, fallbackTeamId) {
  return String(btn.getAttribute("data-team-id") || fallbackTeamId || teamPickerActiveTeamId("") || "").trim();
}

const teamFileDlPreviewState = { url: "", name: "", blob: null, previewBlobUrl: "", popup: null, popupWatchTimer: null };

function clearTeamFilePreviewBlobUrl() {
  if (teamFileDlPreviewState.previewBlobUrl) {
    URL.revokeObjectURL(teamFileDlPreviewState.previewBlobUrl);
    teamFileDlPreviewState.previewBlobUrl = "";
  }
}

function clearTeamFilePreviewCache() {
  clearTeamFilePreviewBlobUrl();
  teamFileDlPreviewState.blob = null;
}

function teamFilePreviewBlobUrl(blob) {
  clearTeamFilePreviewBlobUrl();
  const url = URL.createObjectURL(blob);
  teamFileDlPreviewState.previewBlobUrl = url;
  return url;
}

function escTeamFilePreviewHtml(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function teamFileDlPopupFeatures() {
  const sw = (window.screen && window.screen.availWidth) || 1200;
  const sh = (window.screen && window.screen.availHeight) || 800;
  const w = Math.min(1100, Math.round(sw * 0.82));
  const h = Math.min(860, Math.round(sh * 0.88));
  const l = Math.max(0, Math.round((sw - w) / 2));
  const t = Math.max(0, Math.round((sh - h) / 2));
  return "popup=yes,width=" + w + ",height=" + h + ",left=" + l + ",top=" + t
    + ",menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes";
}

function getTeamFilePreviewPopup() {
  const p = teamFileDlPreviewState.popup;
  return (p && !p.closed) ? p : null;
}

function teamFilePreviewPopupDoc() {
  const popup = getTeamFilePreviewPopup();
  return popup ? popup.document : null;
}

function stopTeamFilePreviewPopupWatch() {
  if (teamFileDlPreviewState.popupWatchTimer) {
    clearInterval(teamFileDlPreviewState.popupWatchTimer);
    teamFileDlPreviewState.popupWatchTimer = null;
  }
}

function drainTeamFilePreviewPopupCommands() {
  try {
    const raw = localStorage.getItem("qcm-tfdl-cmd");
    if (!raw) return;
    const cmd = JSON.parse(raw);
    if (!cmd || !cmd.t || Date.now() - cmd.t > 60000) {
      localStorage.removeItem("qcm-tfdl-cmd");
      return;
    }
    localStorage.removeItem("qcm-tfdl-cmd");
    if (cmd.cmd === "close" || cmd.cmd === "closed") {
      closeTeamFileDownloadPreview();
      return;
    }
    if (cmd.cmd === "download" && cmd.ok !== true) {
      void confirmTeamFileDownloadFromPreview(!!cmd.saveAs);
    }
  } catch (_) {
    try { localStorage.removeItem("qcm-tfdl-cmd"); } catch (_2) {}
  }
}

function startTeamFilePreviewPopupWatch(popup) {
  stopTeamFilePreviewPopupWatch();
  teamFileDlPreviewState.popupWatchTimer = setInterval(() => {
    drainTeamFilePreviewPopupCommands();
    if (!teamFileDlPreviewState.popup || teamFileDlPreviewState.popup.closed) {
      stopTeamFilePreviewPopupWatch();
      if (teamFileDlPreviewState.popup === popup) {
        teamFileDlPreviewState.popup = null;
        clearTeamFilePreviewCache();
        teamFileDlPreviewState.url = "";
        teamFileDlPreviewState.name = "";
      }
    }
  }, 350);
}

function buildTeamFilePreviewPopupHtml(name, url) {
  const origin = window.location.origin || "http://127.0.0.1:9101";
  const safeName = escTeamFilePreviewHtml(name);
  const safeUrl = escTeamFilePreviewHtml(url);
  const safeOrigin = escTeamFilePreviewHtml(origin);
  const payload = JSON.stringify({ name: String(name || "arquivo"), url: String(url || ""), origin })
    .replace(/</g, "\\u003c");
  return '<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8"><base href="' + safeOrigin + '/"><title>' + safeName + '</title><style>'
    + 'html,body{margin:0;height:100%;background:#171717;color:#ececec;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;}'
    + 'body{display:flex;flex-direction:column;overflow:hidden;}'
    + '.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 18px;background:#171717;border-bottom:1px solid #3a3a3a;}'
    + '.head h1{margin:0;font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:calc(100vw - 80px);}'
    + '.sub{margin:4px 0 0;font-size:11px;color:#9b9b9b;font-family:ui-monospace,Menlo,monospace;word-break:break-all;}'
    + '.close{background:transparent;border:none;color:#9b9b9b;font-size:22px;cursor:pointer;line-height:1;padding:0 4px;}'
    + '.close:hover{color:#ececec;}'
    + '.body{flex:1;min-height:0;display:flex;flex-direction:column;padding:16px 18px;}'
    + '.viewer{flex:1;min-height:200px;border:1px solid #3a3a3a;border-radius:10px;background:#2f2f2f;overflow:hidden;position:relative;display:flex;flex-direction:column;}'
    + '.viewer iframe,.viewer img{flex:1;width:100%;min-height:280px;border:0;object-fit:contain;background:#525659;}'
    + '.viewer pre{margin:0;padding:12px 14px;height:100%;overflow:auto;font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.45;white-space:pre-wrap;color:#ececec;}'
    + '.empty{display:flex;align-items:center;justify-content:center;flex:1;min-height:200px;padding:24px;text-align:center;color:#9b9b9b;font-size:12px;font-style:italic;}'
    + '.foot{display:flex;justify-content:flex-end;align-items:center;gap:8px;padding:12px 18px 14px;background:#171717;border-top:1px solid #3a3a3a;}'
    + '.status{flex:1;font-size:11px;color:#9b9b9b;min-height:16px;margin-right:auto;}'
    + '.status.err{color:#ff5a5f;}.status.ok{color:#3ddc84;}'
    + '.btn{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid #3a3a3a;background:#2f2f2f;color:#ececec;cursor:pointer;}'
    + '.btn:hover{border-color:#6aa9ff;color:#6aa9ff;}'
    + '.btn.primary{background:#6aa9ff;color:#0d1117;border-color:#6aa9ff;font-weight:700;}'
    + '.btn:disabled{opacity:.5;cursor:not-allowed;}'
    + '</style></head><body>'
    + '<div class="head"><div><h1>' + safeName + '</h1><p class="sub">' + safeUrl + '</p></div>'
    + '<button type="button" class="close" id="qcm-tfdl-close" aria-label="Fechar">&times;</button></div>'
    + '<div class="body"><div class="viewer" id="qcm-tfdl-viewer">'
    + '<div class="empty" id="qcm-tfdl-empty">A carregar pré-visualização…</div>'
    + '<iframe id="qcm-tfdl-frame" style="display:none;" title="Pré-visualização"></iframe>'
    + '<img id="qcm-tfdl-img" alt="" style="display:none;">'
    + '<pre id="qcm-tfdl-text" style="display:none;"></pre></div></div>'
    + '<div class="foot"><span class="status" id="qcm-tfdl-status" aria-live="polite"></span>'
    + '<button type="button" class="btn" id="qcm-tfdl-cancel">Fechar</button>'
    + '<button type="button" class="btn" id="qcm-tfdl-save">💾 Salvar como…</button>'
    + '<button type="button" class="btn primary" id="qcm-tfdl-download">⬇ Baixar</button></div>'
    + '<script>(function(){'
    + 'var QCM_TFDL=' + payload + ';'
    + 'function absUrl(p){if(/^https?:\\/\\//i.test(p))return p;return QCM_TFDL.origin+(p.charAt(0)==="/"?p:"/"+p);}'
    + 'function setStatus(msg,kind){var box=document.getElementById("qcm-tfdl-status");if(!box)return;box.textContent=msg||"";box.className="status"+(kind?" "+kind:"");}'
    + 'function notifyParent(cmd,extra){try{if(window.opener&&!window.opener.closed){if(cmd==="close"&&window.opener.qcmCloseTeamFileDownloadPreview){window.opener.qcmCloseTeamFileDownloadPreview();return;}if(cmd==="download"&&window.opener.qcmConfirmTeamFileDownloadFromPreview){window.opener.qcmConfirmTeamFileDownloadFromPreview(extra&&extra.saveAs);return;}if(cmd==="closed"&&window.opener.qcmOnTeamFilePreviewPopupClosed){window.opener.qcmOnTeamFilePreviewPopupClosed();return;}}}catch(e){}try{localStorage.setItem("qcm-tfdl-cmd",JSON.stringify(Object.assign({cmd:cmd,t:Date.now()},extra||{})));}catch(e2){}}'
    + 'function closePreview(){notifyParent("close");try{window.close();}catch(e){}}'
    + 'function triggerNavDownload(){var url=absUrl(QCM_TFDL.url);var iframe=document.createElement("iframe");iframe.style.display="none";iframe.name="qcm-tfdl-dl-"+Date.now();document.body.appendChild(iframe);var form=document.createElement("form");form.method="GET";form.action=url;form.target=iframe.name;form.style.display="none";document.body.appendChild(form);form.submit();setTimeout(function(){try{form.remove();iframe.remove();}catch(e){}},120000);}'
    + 'async function fetchFileBlob(){var resp=await fetch(absUrl(QCM_TFDL.url),{method:"GET",credentials:"include"});if(!resp.ok){var err="HTTP "+resp.status;try{var j=await resp.json();if(j&&j.error)err=String(j.error);}catch(e){}throw new Error(err);}var mime=(resp.headers.get("Content-Type")||"application/octet-stream").split(";")[0].trim();return new Blob([await resp.arrayBuffer()],{type:mime||"application/octet-stream"});}'
    + 'async function writeBlob(blob,saveAs){var name=QCM_TFDL.name||"download";if(saveAs&&typeof window.showSaveFilePicker==="function"&&window.isSecureContext){try{var ext=name.indexOf(".")>=0?name.split(".").pop().toLowerCase():"";var opts={suggestedName:name};if(ext){var mime=blob.type&&blob.type!=="application/octet-stream"?blob.type:"application/octet-stream";opts.types=[{description:"Arquivo",accept:{}}];opts.types[0].accept[mime]=["."+ext];}var handle=await window.showSaveFilePicker(opts);var writable=await handle.createWritable();await writable.write(blob);await writable.close();return "picker";}catch(e){if(e&&e.name==="AbortError")throw e;}}var objectUrl=URL.createObjectURL(blob);try{var anchor=document.createElement("a");anchor.href=objectUrl;anchor.download=name;anchor.style.display="none";document.body.appendChild(anchor);anchor.click();anchor.remove();}finally{setTimeout(function(){URL.revokeObjectURL(objectUrl);},1500);}return "anchor";}'
    + 'async function onDownload(saveAs){var dlBtn=document.getElementById("qcm-tfdl-download");var saveBtn=document.getElementById("qcm-tfdl-save");if(dlBtn)dlBtn.disabled=true;if(saveBtn)saveBtn.disabled=true;setStatus(saveAs?"A preparar ficheiro…":"A preparar download…","");try{var blob=null;try{blob=await fetchFileBlob();}catch(fetchErr){if(!saveAs){triggerNavDownload();setStatus("Download iniciado: "+QCM_TFDL.name,"ok");notifyParent("download",{saveAs:false,ok:true});return;}throw fetchErr;}if(saveAs)setStatus("A abrir diálogo Salvar como…","");var mode=await writeBlob(blob,!!saveAs);setStatus((mode==="picker"?"Guardado: ":"Download iniciado: ")+QCM_TFDL.name,"ok");notifyParent("download",{saveAs:!!saveAs,ok:true});}catch(e){if(e&&e.name==="AbortError"){setStatus("Salvar cancelado.","");return;}var msg=e&&e.message?e.message:String(e);setStatus("Erro: "+msg,"err");notifyParent("download",{saveAs:saveAs,ok:false,error:msg});}finally{if(dlBtn)dlBtn.disabled=false;if(saveBtn)saveBtn.disabled=false;}}'
    + 'document.getElementById("qcm-tfdl-close").onclick=closePreview;'
    + 'document.getElementById("qcm-tfdl-cancel").onclick=closePreview;'
    + 'document.getElementById("qcm-tfdl-download").onclick=function(){onDownload(false);};'
    + 'document.getElementById("qcm-tfdl-save").onclick=function(){onDownload(true);};'
    + 'window.addEventListener("beforeunload",function(){notifyParent("closed");});'
    + '})();<' + '/script></body></html>';
}

function teamFilePreviewPopupLaunchUrl(name, url) {
  const origin = window.location.origin || "http://127.0.0.1:9101";
  const safeName = String(name || "arquivo");
  const safeUrl = String(url || "");
  try {
    localStorage.setItem("qcm-tfdl-payload", JSON.stringify({
      name: safeName,
      url: safeUrl,
      t: Date.now(),
    }));
  } catch (_) {}
  const params = new URLSearchParams();
  params.set("name", safeName);
  try {
    params.set("url_b64", btoa(safeUrl));
  } catch (_) {
    params.set("url", safeUrl);
  }
  return origin + "/popup/team-file-preview?" + params.toString();
}

function openTeamFilePreviewPopupWindow(name, url) {
  const existing = getTeamFilePreviewPopup();
  if (existing) {
    try { existing.close(); } catch (_) {}
    stopTeamFilePreviewPopupWatch();
  }
  const popup = window.open(
    teamFilePreviewPopupLaunchUrl(name, url),
    "qcm-team-file-preview",
    teamFileDlPopupFeatures(),
  );
  if (!popup) {
    if (window.qcmNavToast) window.qcmNavToast("Permita pop-ups para pré-visualizar o arquivo.", "err");
    return null;
  }
  teamFileDlPreviewState.popup = popup;
  teamFileDlPreviewState.url = String(url || "");
  teamFileDlPreviewState.name = String(name || "arquivo");
  try { popup.focus(); } catch (_) {}
  startTeamFilePreviewPopupWatch(popup);
  return popup;
}

function resetTeamFilePreviewPopupViewer(clearCache) {
  const doc = teamFilePreviewPopupDoc();
  if (!doc) return;
  const frame = doc.getElementById("qcm-tfdl-frame");
  const img = doc.getElementById("qcm-tfdl-img");
  const text = doc.getElementById("qcm-tfdl-text");
  const empty = doc.getElementById("qcm-tfdl-empty");
  if (frame) { frame.removeAttribute("src"); frame.style.display = "none"; }
  if (img) { img.removeAttribute("src"); img.style.display = "none"; }
  if (text) { text.textContent = ""; text.style.display = "none"; }
  if (empty) { empty.textContent = "A carregar pré-visualização…"; empty.style.display = "flex"; }
  if (clearCache !== false) clearTeamFilePreviewCache();
}

function setTeamFileDownloadPreviewStatus(msg, kind) {
  const doc = teamFilePreviewPopupDoc();
  const box = doc ? doc.getElementById("qcm-tfdl-status") : null;
  if (!box) return;
  box.textContent = msg || "";
  box.className = "status" + (kind ? " " + kind : "");
}

function setTeamFilePreviewPopupButtonsDisabled(disabled) {
  const doc = teamFilePreviewPopupDoc();
  if (!doc) return;
  ["qcm-tfdl-download", "qcm-tfdl-save"].forEach((id) => {
    const btn = doc.getElementById(id);
    if (btn) btn.disabled = !!disabled;
  });
}

function closeTeamFileDownloadPreview() {
  const popup = getTeamFilePreviewPopup();
  if (popup) {
    try { popup.close(); } catch (_) {}
  }
  stopTeamFilePreviewPopupWatch();
  teamFileDlPreviewState.popup = null;
  clearTeamFilePreviewCache();
  teamFileDlPreviewState.url = "";
  teamFileDlPreviewState.name = "";
}

window.qcmCloseTeamFileDownloadPreview = closeTeamFileDownloadPreview;
window.qcmOnTeamFilePreviewPopupClosed = function () {
  stopTeamFilePreviewPopupWatch();
  teamFileDlPreviewState.popup = null;
  clearTeamFilePreviewCache();
  teamFileDlPreviewState.url = "";
  teamFileDlPreviewState.name = "";
};

function teamFileSavePickerSupported() {
  return typeof window.showSaveFilePicker === "function" && !!window.isSecureContext;
}

function isTeamDownloadUrl(href) {
  const url = normalizeDownloadHref(href);
  if (!url) return false;
  return /\/hermes\/teams\/download\b|\/teams\/[^/]+\/normas\/download\b|\/openclaw\/team-files\/download\b/.test(url);
}

function stripDownloadPreviewParams(href) {
  const raw = String(href || "").trim();
  if (!raw) return "";
  try {
    const u = new URL(raw, window.location.origin || "http://127.0.0.1:9101");
    u.searchParams.delete("preview");
    u.searchParams.delete("_");
    const qs = u.searchParams.toString();
    return u.pathname + (qs ? "?" + qs : "");
  } catch (_) {
    return raw.replace(/([?&])(preview|_)=[^&]*&?/g, "$1").replace(/[?&]$/, "");
  }
}

function teamFilePreviewUrl(downloadUrl) {
  const base = stripDownloadPreviewParams(normalizeDownloadHref(downloadUrl));
  if (!base) return "";
  const sep = base.includes("?") ? "&" : "?";
  return base + sep + "preview=1&_=" + Date.now();
}

async function openTeamFileDownloadPreview(downloadUrl, filename) {
  bindTeamFileDownloadPreviewModal();
  const url = stripDownloadPreviewParams(normalizeDownloadHref(downloadUrl));
  if (!url) return;
  const name = String(filename || "arquivo").trim() || "arquivo";
  const popup = openTeamFilePreviewPopupWindow(name, url);
  if (!popup) return;
  clearTeamFilePreviewCache();
}

async function confirmTeamFileDownloadFromPreview(saveAs) {
  const url = teamFileDlPreviewState.url;
  const name = teamFileDlPreviewState.name;
  if (!url) {
    setTeamFileDownloadPreviewStatus("Nenhum arquivo selecionado.", "err");
    return;
  }
  setTeamFilePreviewPopupButtonsDisabled(true);
  setTeamFileDownloadPreviewStatus(saveAs ? "A preparar ficheiro…" : "A preparar download…", "");
  try {
    if (saveAs) {
      let blob = teamFileDlPreviewState.blob;
      if (!blob) blob = await fetchTeamFileDownloadBlob(url);
      setTeamFileDownloadPreviewStatus("A abrir diálogo Salvar como…", "");
      const mode = await writeTeamFileBlobToDisk(blob, name, true);
      setTeamFileDownloadPreviewStatus(
        mode === "picker" ? ("Guardado: " + name) : ("Download iniciado: " + name),
        "ok",
      );
      if (window.qcmNavToast) {
        window.qcmNavToast(mode === "picker" ? ("Guardado: " + name) : ("Download: " + name), "ok");
      }
    } else {
      await downloadTeamFileToDisk(url, name, { saveAs: false, blob: teamFileDlPreviewState.blob });
      setTeamFileDownloadPreviewStatus("Download iniciado: " + name, "ok");
      if (window.qcmNavToast) window.qcmNavToast("Download: " + name, "ok");
    }
  } catch (e) {
    if (e && e.name === "AbortError") {
      setTeamFileDownloadPreviewStatus("Salvar cancelado.", "");
      return;
    }
    const msg = e && e.message ? e.message : String(e);
    setTeamFileDownloadPreviewStatus("Erro: " + msg, "err");
    if (window.qcmNavToast) window.qcmNavToast("Erro ao baixar: " + msg, "err");
  } finally {
    setTeamFilePreviewPopupButtonsDisabled(false);
  }
}

function bindTeamFileDownloadPreviewModal() {
  if (window._qcmTeamFileDlModalBound) return;
  window._qcmTeamFileDlModalBound = true;
  window.qcmConfirmTeamFileDownloadFromPreview = function (saveAs) {
    void confirmTeamFileDownloadFromPreview(!!saveAs);
  };
}

function triggerSyncTeamFileDownload(url, filename) {
  const cleanUrl = stripDownloadPreviewParams(normalizeDownloadHref(url));
  const name = String(filename || "download").trim() || "download";
  if (!cleanUrl) return false;
  try {
    const anchor = document.createElement("a");
    anchor.href = cleanUrl;
    anchor.download = name;
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    return true;
  } catch (_) {}
  try {
    const iframe = document.createElement("iframe");
    iframe.style.display = "none";
    iframe.name = "qcm-team-dl-" + Date.now();
    document.body.appendChild(iframe);
    const form = document.createElement("form");
    form.method = "GET";
    form.action = cleanUrl;
    form.target = iframe.name;
    form.style.display = "none";
    document.body.appendChild(form);
    form.submit();
    form.remove();
    setTimeout(() => iframe.remove(), 120000);
    return true;
  } catch (_) {
    return false;
  }
}

async function fetchTeamFileDownloadBlob(url, options) {
  const opts = options || {};
  const cleanUrl = stripDownloadPreviewParams(normalizeDownloadHref(url));
  if (!cleanUrl) throw new Error("URL de download inválida");
  if (!opts.forceRefresh && teamFileDlPreviewState.blob && teamFileDlPreviewState.url === cleanUrl) {
    return teamFileDlPreviewState.blob;
  }
  const resp = await chatFetch(opts.previewUrl || cleanUrl, { method: "GET" }, 120000);
  if (!resp.ok) {
    let errMsg = "HTTP " + resp.status;
    try {
      const j = await resp.json();
      if (j && j.error) errMsg = String(j.error);
    } catch (_) {}
    throw new Error(errMsg);
  }
  const mimeHeader = String(resp.headers.get("Content-Type") || "application/octet-stream").split(";")[0].trim();
  const blob = new Blob([await resp.arrayBuffer()], { type: mimeHeader || "application/octet-stream" });
  teamFileDlPreviewState.url = cleanUrl;
  teamFileDlPreviewState.blob = blob;
  return blob;
}

async function writeTeamFileBlobToDisk(blob, filename, saveAs) {
  const name = String(filename || "download").trim() || "download";
  if (saveAs && teamFileSavePickerSupported()) {
    try {
      const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
      const pickerOpts = { suggestedName: name };
      if (ext) {
        const mime = blob.type && blob.type !== "application/octet-stream" ? blob.type : "application/octet-stream";
        pickerOpts.types = [{ description: "Arquivo", accept: { [mime]: ["." + ext] } }];
      }
      const handle = await window.showSaveFilePicker(pickerOpts);
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return "picker";
    } catch (e) {
      if (e && e.name === "AbortError") throw e;
    }
  }
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = name;
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
  }
  return "anchor";
}

async function downloadTeamFileToDisk(url, filename, options) {
  const opts = options || {};
  const cleanUrl = stripDownloadPreviewParams(normalizeDownloadHref(url));
  const name = String(filename || "download").trim() || "download";
  if (!cleanUrl) throw new Error("URL de download inválida");
  if (!opts.saveAs && !opts.forceBlob && !opts.blob) {
    if (triggerSyncTeamFileDownload(cleanUrl, name)) return;
  }
  const blob = opts.blob || await fetchTeamFileDownloadBlob(cleanUrl, { forceRefresh: !!opts.forceRefresh });
  if (opts.saveAs) {
    await writeTeamFileBlobToDisk(blob, name, true);
    return;
  }
  await writeTeamFileBlobToDisk(blob, name, false);
}

function handleTeamFileActionClick(ev, fallbackTeamId) {
  const normaBtn = ev.target.closest("[data-team-file-norma]");
  if (normaBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const teamId = teamFileActionTeamId(normaBtn, fallbackTeamId);
    if (!teamId) {
      if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
      return;
    }
    const rel = normaBtn.getAttribute("data-team-file-norma") || "";
    const name = normaBtn.getAttribute("data-team-file-name") || rel || "arquivo";
    const path = normaBtn.getAttribute("data-team-file-path") || "";
    normaBtn.disabled = true;
    copyTeamFileToNorma(teamId, { relative_path: rel, name, path })
      .catch((e) => {
        if (window.qcmNavToast) window.qcmNavToast(e.message || String(e));
      })
      .finally(() => {
        normaBtn.disabled = false;
      });
    return;
  }
  const ctxBtn = ev.target.closest("[data-team-file-ctx]");
  if (ctxBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const teamId = teamFileActionTeamId(ctxBtn, fallbackTeamId);
    if (!teamId) {
      if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
      return;
    }
    const rel = ctxBtn.getAttribute("data-team-file-ctx") || "";
    const name = ctxBtn.getAttribute("data-team-file-name") || rel || "arquivo";
    addTeamFilesToSessionContext(teamId, [{ relative_path: rel, name }]).catch((e) => {
      if (window.qcmNavToast) window.qcmNavToast(e.message || String(e));
    });
    return;
  }
  const newCardBtn = ev.target.closest("[data-team-file-newcard]");
  if (newCardBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const teamId = teamFileActionTeamId(newCardBtn, fallbackTeamId);
    if (!teamId) {
      if (window.qcmNavToast) window.qcmNavToast("Selecione um time primeiro.", "err");
      return;
    }
    const rel = newCardBtn.getAttribute("data-team-file-newcard") || "";
    const name = newCardBtn.getAttribute("data-team-file-name") || rel || "arquivo";
    const path = newCardBtn.getAttribute("data-team-file-path") || "";
    const suggested = name.replace(/\.[^.]+$/, "") || name;
    const title = (window.prompt("Título do novo card:", suggested) || "").trim();
    if (!title) return;
    newCardBtn.disabled = true;
    createCardFromTeamFile(teamId, { relative_path: rel, name, path, title })
      .catch((e) => {
        if (window.qcmNavToast) window.qcmNavToast(e.message || String(e), "err");
      })
      .finally(() => {
        newCardBtn.disabled = false;
      });
    return;
  }
  const dlBtn = ev.target.closest("[data-team-file-dl]");
  if (dlBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    openTeamFileDownloadPreview(
      dlBtn.getAttribute("data-team-file-dl") || "",
      dlBtn.getAttribute("data-team-file-name") || "download",
    );
    return;
  }
}

function bindTeamFileActionDelegation(root, fallbackTeamId) {
  if (!root || root._qcmFileActionsDelegated) return;
  root._qcmFileActionsDelegated = true;
  root.addEventListener("click", (ev) => handleTeamFileActionClick(ev, fallbackTeamId));
}

function bindTeamWaAllowlistDelegation(root) {
  if (!root || root._qcmWaAllowDelegated) return;
  root._qcmWaAllowDelegated = true;
  root.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-remove-wa-allow]");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const wrap = btn.closest("[data-team-id]");
    const teamId = (wrap && wrap.getAttribute("data-team-id"))
      || teamPickerState.selectedTeamId || "";
    const dest = btn.getAttribute("data-remove-wa-allow") || "";
    if (!teamId || !dest) return;
    void removeTeamWaAllowlistDest(teamId, dest);
  });
}

function bindTeamFileContextButtons(root, teamId) {
  if (!root) return;
  const stableId = root.id || "";
  if (stableId !== "team-picker-preview" && stableId !== "team-context-body") return;
  bindTeamFileActionDelegation(root, teamId);
}

function bindTeamRecentFilesBulkContext(root, teamId) {
  if (!root || !teamId) return;
  const btn = root.querySelector("[data-team-recent-ctx-all]");
  if (!btn || btn._boundRecentCtxAll) return;
  btn._boundRecentCtxAll = true;
  btn.onclick = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const cached = teamPickerState.previewCache[teamId] || {};
    const rows = collectTeamFilesForContext(cached.recent_files || []);
    if (!rows.length) {
      if (window.qcmNavToast) window.qcmNavToast("Nenhum arquivo elegível para contexto.");
      return;
    }
    btn.disabled = true;
    addTeamFilesToSessionContext(teamId, rows)
      .catch((e) => {
        if (window.qcmNavToast) window.qcmNavToast(e.message || String(e));
      })
      .finally(() => {
        btn.disabled = false;
      });
  };
}

function renderTeamFileActionButtons(f, teamId) {
  const href = normalizeDownloadHref(f.download_url || "");
  const fname = f.name || f.relative_path || "arquivo";
  const rel = f.relative_path || fname;
  const fpath = f.path || "";
  const canCtx = isContextDocumentName(fname);
  const canNorma = isNormaEligibleFileName(fname);
  let html = '<div class="team-picker-file-actions">';
  if (canNorma && teamId) {
    html += '<button type="button" class="team-picker-file-norma" data-team-id="' + escapeHtml(teamId)
      + '" data-team-file-norma="' + escapeHtml(rel) + '" data-team-file-name="' + escapeHtml(fname)
      + '" data-team-file-path="' + escapeHtml(fpath)
      + '" title="Copiar e indexar nas normas do time (PDF/Word)">📋 Indexar</button>';
  }
  if (canCtx && teamId) {
    html += '<button type="button" class="team-picker-file-ctx" data-team-id="' + escapeHtml(teamId)
      + '" data-team-file-ctx="' + escapeHtml(rel) + '" data-team-file-name="' + escapeHtml(fname)
      + '" title="Extrair texto (PDF/Word incl.) e fixar no contexto desta conversa">📎 Contexto</button>';
  }
  if (teamId) {
    html += '<button type="button" class="team-picker-file-newcard" data-team-id="' + escapeHtml(teamId)
      + '" data-team-file-newcard="' + escapeHtml(rel) + '" data-team-file-name="' + escapeHtml(fname)
      + '" data-team-file-path="' + escapeHtml(fpath)
      + '" title="Criar um novo card no Kanban e anexar este arquivo">➕ Novo card</button>';
  }
  if (href) {
    html += '<button type="button" class="team-picker-file-dl" data-team-file-dl="'
      + escapeHtml(href) + '" data-team-file-name="' + escapeHtml(fname)
      + '" title="Pré-visualizar e baixar">⬇ Baixar</button>';
  }
  html += "</div>";
  return html;
}

function normalizeDownloadHref(href) {
  const raw = String(href || "").trim();
  if (!raw) return "";
  try {
    const base = window.location.origin || "http://127.0.0.1:9101";
    const u = new URL(raw, base);
    if (u.pathname === "/homes/teams/download") {
      u.pathname = "/hermes/teams/download";
    }
    if (u.searchParams.has("relativo_path") && !u.searchParams.has("relative_path")) {
      u.searchParams.set("relative_path", u.searchParams.get("relativo_path") || "");
      u.searchParams.delete("relativo_path");
    }
    const legacy = u.pathname.match(/\/team-files\/download\/([^/]+)/);
    if (legacy) {
      const params = new URLSearchParams(u.search);
      if (!params.get("team_id")) params.set("team_id", legacy[1]);
      return "/hermes/teams/download?" + params.toString();
    }
    if (u.pathname === "/openclaw/team-files/download") {
      return "/hermes/teams/download" + u.search;
    }
    if (u.pathname.startsWith("/openclaw/") || u.pathname.startsWith("/hermes/teams/download")) {
      return u.pathname + u.search;
    }
    if (u.pathname.startsWith("/teams/") && u.pathname.includes("/normas/download")) {
      return u.pathname + u.search;
    }
  } catch (_) {
    if (raw.startsWith("/openclaw/")) return raw;
  }
  return raw;
}

function initQcmTeamPicker(opts) {
  opts = opts || {};
  window.__qcmTeamPickerMode = opts.mode || "chat";
  const confirmBtn = el("team-picker-select-btn");
  if (confirmBtn && opts.confirmButtonLabel) {
    confirmBtn.textContent = opts.confirmButtonLabel;
  }
  bindTeamNormaPreviewDialog();
  bindTeamPickerDialog();
}

window.initQcmTeamPicker = initQcmTeamPicker;
window.openTeamPickerDialog = openTeamPickerDialog;
window.closeTeamPickerDialog = closeTeamPickerDialog;
window.isTeamPickerDialogOpen = isTeamPickerDialogOpen;
window.showTeamModal = showTeamModal;

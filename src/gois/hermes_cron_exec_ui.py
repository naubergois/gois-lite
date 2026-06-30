"""Shared Hermes cron run dialog (modal + JS) for monitor and kanban pages."""

HERMES_CRON_EXEC_MODAL_CSS = """
  @keyframes qcm-pulse {
    0%, 100% { opacity: .35; transform: scale(.85); }
    50%      { opacity: 1;   transform: scale(1.15); }
  }
  .pulse { width: 8px; height: 8px; border-radius: 50%; background: currentColor;
           animation: qcm-pulse 1.2s ease-in-out infinite; }
  .chat-modal { position: fixed; inset: 0; z-index: 1000; display: flex;
                align-items: center; justify-content: center; padding: 24px; }
  .chat-modal[hidden] { display: none; }
  .chat-modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.55);
                         backdrop-filter: blur(2px); }
  .chat-modal-dialog { position: relative; width: min(960px, 96vw); height: min(88vh, 820px);
                       display: flex; flex-direction: column; background: var(--panel);
                       border: 1px solid var(--border); border-radius: 12px;
                       box-shadow: 0 24px 48px rgba(0,0,0,.45); overflow: hidden; }
  .chat-modal-header { display: flex; align-items: flex-start; justify-content: space-between;
                       gap: 12px; padding: 16px 20px; border-bottom: 1px solid var(--border);
                       flex-shrink: 0; }
  .chat-modal-header h2 { margin: 0; font-size: 18px; }
  .chat-modal-header .chat-hint { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
  .chat-modal-close { background: transparent; border: 0; color: var(--muted);
                      font-size: 28px; line-height: 1; cursor: pointer; padding: 0 4px; }
  .chat-modal-close:hover { color: var(--fg); }
  .chat-modal-body { flex: 1; min-height: 0; padding: 16px 20px 20px;
                     display: flex; flex-direction: column; overflow: hidden; }
  .cron-result-runs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px;
                      flex-shrink: 0; }
  .cron-result-runs button { background: var(--panel); color: var(--muted);
                             border: 1px solid var(--border); border-radius: 6px;
                             padding: 4px 8px; font-size: 11px; font-family: var(--mono);
                             cursor: pointer; }
  .cron-result-runs button.active { color: var(--accent); border-color: var(--accent); }
  .cron-result-scroll { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
                         display: flex; flex-direction: column; gap: 10px;
                         padding-right: 2px; }
  .cron-result-footer { flex-shrink: 0; display: flex; flex-direction: column; gap: 10px;
                         padding-top: 2px; border-top: 1px solid var(--border); margin-top: 2px; }
  .cron-result-modal pre.cron-result-panel-body,
  .cron-result-modal pre.cron-result-body {
    max-height: none; overflow: visible;
  }
  .cron-result-panel-log .cron-result-panel-body {
    max-height: min(38vh, 340px); overflow-y: auto; overflow-x: hidden;
  }
  .cron-result-body { margin: 0; padding: 12px; background: var(--panel);
                      border: 1px solid var(--border); border-radius: 6px;
                      font-family: var(--mono); font-size: 13px; line-height: 1.5;
                      white-space: pre-wrap; word-break: break-word;
                      max-height: min(22vh, 200px); overflow-y: auto; }
  .cron-result-modal-dialog { width: min(1120px, 96vw); height: min(90vh, 900px); }
  .cron-result-modal-body { gap: 0; }
  .cron-exec-status { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .cron-exec-status .pill { font-size: 11px; }
  .cron-exec-status .meta { font-size: 11px; color: var(--muted); font-family: var(--mono); }
  body.qcm-cron-exec-open { overflow: hidden; }
  .chat-modal.cron-result-modal { z-index: 1200; }
  .cron-generated-files { margin: 0; flex-shrink: 0; }
  .cron-generated-files[hidden] { display: none; }
  .cron-generated-files h5 { margin: 0 0 6px; font-size: 12px; color: var(--muted); font-weight: 600; }
  .cron-generated-files .workdir { display: block; margin-bottom: 6px; font-size: 11px; color: var(--muted);
                                     font-family: var(--mono); word-break: break-all; }
  .cron-generated-files ul { list-style: none; margin: 0; padding: 0; font-family: var(--mono); font-size: 11px;
                            border: 1px solid var(--border); border-radius: 6px;
                            padding: 6px 8px; background: var(--panel); }
  .cron-generated-files li { display: flex; align-items: baseline; gap: 8px; padding: 2px 0; }
  .cron-generated-files .path { flex: 1; word-break: break-all; }
  .cron-generated-files .source-btn { flex-shrink: 0; font-size: 10px; padding: 2px 6px; }
  .cron-generated-files .badge { flex-shrink: 0; font-size: 9px; text-transform: uppercase; letter-spacing: .03em;
                                 padding: 1px 5px; border-radius: 4px; border: 1px solid var(--border);
                                 color: var(--muted); }
  .cron-generated-files .badge-modified { color: #fbbf24; border-color: #b45309; }
  .cron-generated-files .badge-added { color: #4ade80; border-color: #15803d; }
  .cron-generated-files .badge-untracked { color: #93c5fd; border-color: #1d4ed8; }
  .cron-generated-files .badge-mentioned { color: var(--muted); }
  .cron-result-panel { margin: 0; flex-shrink: 0; }
  .cron-result-panel[hidden] { display: none; }
  .cron-result-panel h5 { margin: 0 0 6px; font-size: 12px; color: var(--muted); font-weight: 600; }
  .cron-result-panel-error h5 { color: #f87171; }
  .cron-result-panel-body { margin: 0; padding: 10px 12px; background: var(--panel);
                            border: 1px solid var(--border); border-radius: 6px;
                            font-family: var(--mono); font-size: 12px; line-height: 1.5;
                            white-space: pre-wrap; word-break: break-word; }
  .cron-result-panel-error .cron-result-panel-body {
    border-color: rgba(248,113,113,.35);
    max-height: min(28vh, 220px); overflow-y: auto; overflow-x: hidden;
  }
  .cron-result-details-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 8px 12px;
    margin: 0 0 8px;
  }
  .cron-result-details-grid div {
    border: 1px dashed var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    min-height: 48px;
  }
  .cron-result-details-grid dt {
    margin: 0;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--muted);
  }
  .cron-result-details-grid dd {
    margin: 4px 0 0;
    font-size: 12px;
    font-family: var(--mono);
    color: var(--fg);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .cron-result-prompt {
    margin: 0 0 8px;
    padding: 10px 12px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: min(20vh, 170px);
    overflow-y: auto;
  }
  .cron-result-source-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .cron-result-source-actions .meta {
    font-size: 11px;
    font-family: var(--mono);
    color: var(--muted);
  }
  .cron-result-response-label { margin: 0; font-size: 12px; color: var(--muted); font-weight: 600; }
"""

HERMES_CRON_EXEC_MODAL_HTML = """
<div class="chat-modal cron-result-modal" id="hermes-cron-result-modal" hidden aria-hidden="true">
  <div class="chat-modal-backdrop" id="hermes-cron-result-backdrop"></div>
  <div class="chat-modal-dialog cron-result-modal-dialog" role="dialog" aria-modal="true"
       aria-labelledby="hermes-cron-result-title">
    <header class="chat-modal-header">
      <div>
        <h2 id="hermes-cron-result-title">Resultado do job</h2>
        <p class="chat-hint" id="hermes-cron-result-meta">—</p>
      </div>
      <button type="button" class="chat-modal-close" id="hermes-cron-result-close" aria-label="Fechar">×</button>
    </header>
    <div class="chat-modal-body cron-result-modal-body">
      <div class="cron-result-runs" id="hermes-cron-result-runs"></div>
      <div class="cron-result-scroll" id="hermes-cron-result-scroll">
        <div id="hermes-cron-result-details" class="cron-result-panel" hidden>
          <h5>Detalhes do agente</h5>
          <div class="cron-result-details-grid" id="hermes-cron-result-details-grid"></div>
          <pre class="cron-result-prompt" id="hermes-cron-result-prompt"></pre>
          <div class="cron-result-source-actions">
            <a class="ctrl" id="hermes-cron-result-source-link" target="_blank" rel="noopener">código-fonte</a>
            <span class="meta" id="hermes-cron-result-source-note">—</span>
          </div>
        </div>
        <div id="hermes-cron-result-error" class="cron-result-panel cron-result-panel-error" hidden>
          <h5>Erro</h5>
          <pre class="cron-result-panel-body" id="hermes-cron-result-error-body"></pre>
        </div>
        <div id="hermes-cron-result-log" class="cron-result-panel cron-result-panel-log" hidden>
          <h5>Log de execução</h5>
          <pre class="cron-result-panel-body" id="hermes-cron-result-log-body"></pre>
        </div>
      </div>
      <div class="cron-result-footer">
        <h5 class="cron-result-response-label" id="hermes-cron-result-response-label">Resposta</h5>
        <pre class="cron-result-body" id="hermes-cron-result-body">—</pre>
        <div id="hermes-cron-generated-files" class="cron-generated-files" hidden></div>
      </div>
    </div>
  </div>
</div>
"""

HERMES_CRON_EXEC_JS = r"""
function qcmCronEl(id) { return document.getElementById(id); }

  function qcmCronScrollResultIntoView() {
    const footer = document.querySelector(".cron-result-modal .cron-result-footer");
    if (footer) {
      requestAnimationFrame(() => {
        footer.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    }
  }

  function qcmFocusCronResultDetails() {
    const details = qcmCronEl("hermes-cron-result-details");
    const sourceLink = qcmCronEl("hermes-cron-result-source-link");
    if (details) {
      requestAnimationFrame(() => {
        details.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    }
    if (sourceLink && !sourceLink.hidden) {
      setTimeout(() => {
        try { sourceLink.focus({ preventScroll: true }); }
        catch (_) { sourceLink.focus(); }
      }, 40);
    }
  }

function qcmClickClosest(ev, selector) {
  let node = ev && ev.target;
  if (!(node instanceof Element)) node = node && node.parentElement;
  return node instanceof Element ? node.closest(selector) : null;
}

function qcmEsc(str) {
    if (typeof escapeHtml === "function") return escapeHtml(str);
    if (typeof esc === "function") return esc(str);
    return String(str).replace(/[&<>"']/g, (c) => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
    ));
  }

  function qcmCronSnapshot() {
    if (typeof kbCronSnapshot !== "undefined" && kbCronSnapshot) return kbCronSnapshot;
    return window._lastHermesCron || null;
  }

  function qcmSetCronSnapshot(data) {
    if (!data || data.ok === false) return;
    window._lastHermesCron = data;
    if (typeof kbCronSnapshot !== "undefined") kbCronSnapshot = data;
  }

  async function qcmCronFetchJson(url, opt, timeoutMs) {
    const timeout = timeoutMs != null ? timeoutMs : 30000;
    if (typeof monitorFetchJson === "function") {
      return monitorFetchJson(url, opt, timeout);
    }
    const method = (opt && opt.method) || "GET";
    let payload = null;
    if (opt && opt.body) {
      try { payload = JSON.parse(opt.body); } catch (_) { payload = null; }
    }
    if (typeof api === "function") {
      return api(url, method, payload, timeout);
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const r = await fetch(url, Object.assign({}, opt || {}, {
        signal: ctrl.signal, cache: "no-cache", credentials: "same-origin",
      }));
      clearTimeout(timer);
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.ok === false) {
        throw new Error(data.error || data.reason || data.summary || ("HTTP " + r.status));
      }
      return data;
    } catch (e) {
      clearTimeout(timer);
      throw e;
    }
  }

  async function qcmRefreshCronUi() {
    if (typeof poll === "function") {
      await poll();
      return;
    }
    if (typeof loadKanbanCron === "function") {
      await loadKanbanCron(true);
    }
  }

  function qcmRenderCronTables(cron) {
    if (!cron) return;
    if (typeof renderHermesCron === "function") renderHermesCron(cron);
    else if (typeof renderKanbanCron === "function") renderKanbanCron();
  }

  let hermesCronResultId = null;
  let hermesCronResultCache = {};
  let hermesCronSourceMetaCache = {};
  let hermesCronResultModalOpen = false;
  let cronExecPollTimer = null;
  let cronExecActiveJobId = null;

  function sourceNoteFromMeta(meta, job) {
    if (meta && meta.ok) {
      const matched = String(meta.matched_candidate || "").toLowerCase();
      const sourcePath = String(meta.source_path || "");
      const profile = String(meta.source_profile || (job && job.source_profile) || "").trim();
      if (matched === "prompt" || sourcePath === "<cron-prompt>") {
        return "origem: prompt do cron";
      }
      if (matched.startsWith("profile:")) {
        return profile
          ? "origem: profile " + profile
          : "origem: profile";
      }
      if (sourcePath) {
        return "origem: arquivo-fonte";
      }
    }
    const prompt = String((job && job.prompt) || "").trim();
    if (prompt) {
      return "origem: prompt do cron (fallback)";
    }
    return "sem prompt detalhado; fonte pode não estar disponível";
  }

  async function ensureCronSourceMeta(jobId) {
    const key = String(jobId || "").trim();
    if (!key) return null;
    if (Object.prototype.hasOwnProperty.call(hermesCronSourceMetaCache, key)) {
      return hermesCronSourceMetaCache[key];
    }
    try {
      const data = await qcmCronFetchJson(
        "/hermes/cron/" + encodeURIComponent(key) + "/source",
        {},
        12000
      );
      hermesCronSourceMetaCache[key] = data || null;
      return data || null;
    } catch (e) {
      hermesCronSourceMetaCache[key] = {
        ok: false,
        error: (e && e.message) ? e.message : "erro ao carregar origem da fonte",
      };
      return hermesCronSourceMetaCache[key];
    }
  }

  function cronJobIdMatch(a, b) {
    const sa = String(a || "");
    const sb = String(b || "");
    return sa === sb || sa.endsWith(sb) || sb.endsWith(sa);
  }

  function qcmCronJobById(jobId) {
    const cron = qcmCronSnapshot();
    if (!cron || !cron.jobs) return null;
    return cron.jobs.find((j) => cronJobIdMatch(j.id, jobId)) || null;
  }

  function cronJobDisplayName(jobId) {
    const job = qcmCronJobById(jobId);
    return job ? (job.name || job.id || jobId) : jobId;
  }

  function renderGeneratedFilesList(containerEl, files, workdir) {
    if (!containerEl) return;
    const list = Array.isArray(files) ? files : [];
    if (!list.length) {
      containerEl.hidden = true;
      containerEl.innerHTML = "";
      return;
    }
    containerEl.hidden = false;
    const wdLine = workdir
      ? '<span class="workdir">' + qcmEsc(workdir) + "</span>"
      : "";
    const items = list.map((f) => {
      const st = String(f.status || "mentioned");
      const badgeClass = "badge badge-" + st.replace(/[^a-z0-9_-]/gi, "");
      const pathValue = String(f.path || "");
      const canOpen = typeof window.openGeneratedSourceViewer === "function";
      const btn = canOpen
        ? '<button type="button" class="ctrl source-btn generated-source-btn" data-path="' +
          qcmEsc(pathValue) + '" data-workdir="' + qcmEsc(workdir || "") + '">fonte</button>'
        : "";
      return (
        "<li><span class=\"" + badgeClass + "\">" + qcmEsc(st) +
        "</span><span class=\"path\">" + qcmEsc(pathValue) + "</span>" + btn + "</li>"
      );
    }).join("");
    containerEl.innerHTML =
      "<h5>Arquivos gerados</h5>" + wdLine + "<ul>" + items + "</ul>";
  }

function openCronResultModal(jobId) {
  const modal = qcmCronEl("hermes-cron-result-modal");
  if (!modal) return false;
  if (modal.parentElement !== document.body) {
    document.body.appendChild(modal);
  }
  modal.hidden = false;
  modal.removeAttribute("hidden");
  modal.setAttribute("aria-hidden", "false");
  modal.style.display = "flex";
  hermesCronResultModalOpen = true;
  document.body.classList.add("qcm-cron-exec-open");
  document.body.style.overflow = "hidden";
  const title = qcmCronEl("hermes-cron-result-title");
  if (title) title.textContent = cronJobDisplayName(jobId);
  return true;
}

  function stopCronExecPoll() {
    if (cronExecPollTimer) {
      clearInterval(cronExecPollTimer);
      cronExecPollTimer = null;
    }
    cronExecActiveJobId = null;
  }

  function findRunningCronJob(jobId, cron) {
    const running = (cron && cron.running) || [];
    return running.find((j) => cronJobIdMatch(j.job_id || j.id, jobId)) || null;
  }

  function buildCronExecBodyText(exec) {
    const parts = [];
    if (exec.command) parts.push("$ " + exec.command);
    if (exec.phase === "starting") parts.push("", "A iniciar execução…");
    if (exec.stdout) parts.push("", "--- stdout ---", exec.stdout);
    if (exec.stderr) parts.push("", "--- stderr ---", exec.stderr);
    if (exec.summary && exec.phase !== "starting") {
      parts.push("", "--- resumo ---", exec.summary);
    }
    if (exec.logLines && exec.logLines.length) {
      parts.push("", "--- agent.log (ao vivo) ---");
      parts.push.apply(parts, exec.logLines);
    }
    if (exec.error) parts.push("", "Erro: " + exec.error);
    return parts.join("\n");
  }

  function buildCronResultBodyText(data) {
    if (!data) return "—";
    const err = String(data.error || data.last_error || "").trim();
    const log = data.execution_log || [];
    const body = String(data.response || "").trim();
    const preview = String(data.preview || "").trim();
    const response = body || (preview && preview !== err ? preview : "");
    if (response && response !== err) return response;
    if (log.length) return log.join("\n");
    if (err) return err;
    return "—";
  }

  function qcmFmtCronWhen(value) {
    if (!value) return "—";
    if (typeof fmtCronWhen === "function") return fmtCronWhen(value);
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
  }

  function renderCronResultDetails(jobId, data) {
    const wrap = qcmCronEl("hermes-cron-result-details");
    const grid = qcmCronEl("hermes-cron-result-details-grid");
    const promptEl = qcmCronEl("hermes-cron-result-prompt");
    const sourceLink = qcmCronEl("hermes-cron-result-source-link");
    const sourceNote = qcmCronEl("hermes-cron-result-source-note");
    if (!wrap || !grid || !promptEl || !sourceLink || !sourceNote) return;

    const job = qcmCronJobById(jobId) || {};
    const model = job.modelLabel || job.modelId || "—";
    const rows = [
      ["ID", job.id || jobId || "—"],
      ["Perfil", job.profile || "—"],
      ["Modelo", model],
      ["Agenda", job.schedule_display || "—"],
      ["Próxima", qcmFmtCronWhen(job.next_run_at)],
      ["Última", qcmFmtCronWhen(job.last_run_at || job.last_result_at)],
      ["Status", (data && (data.last_status || data.status)) || job.last_status || "—"],
      ["Run", (data && (data.run_time || data.file)) || "—"],
    ];
    grid.innerHTML = rows.map((row) => {
      return '<div><dt>' + qcmEsc(row[0]) + '</dt><dd>' + qcmEsc(row[1]) + '</dd></div>';
    }).join("");

    const prompt = String(job.prompt || "").trim();
    if (prompt) {
      promptEl.hidden = false;
      promptEl.textContent = prompt;
    } else {
      promptEl.hidden = true;
      promptEl.textContent = "";
    }

    sourceLink.href = "/hermes/cron/" + encodeURIComponent(String(jobId || "")) + "/source?raw=1";
    sourceLink.hidden = !jobId;
    const cachedMeta = hermesCronSourceMetaCache[String(jobId || "")] || null;
    sourceNote.textContent = sourceNoteFromMeta(cachedMeta, job);
    if (jobId) {
      sourceNote.dataset.jobId = String(jobId);
      if (!cachedMeta) sourceNote.textContent = "origem: a verificar…";
      ensureCronSourceMeta(jobId).then((meta) => {
        if (
          !hermesCronResultModalOpen ||
          String(hermesCronResultId || "") !== String(jobId) ||
          sourceNote.dataset.jobId !== String(jobId)
        ) {
          return;
        }
        sourceNote.textContent = sourceNoteFromMeta(meta, job);
      });
    }
    wrap.hidden = false;
  }

  function setCronResponseLabel(text, visible) {
    const label = qcmCronEl("hermes-cron-result-response-label");
    if (!label) return;
    label.hidden = visible === false;
    if (text) label.textContent = text;
  }

  function renderCronResultPanels(data) {
    const errWrap = qcmCronEl("hermes-cron-result-error");
    const errBody = qcmCronEl("hermes-cron-result-error-body");
    const logWrap = qcmCronEl("hermes-cron-result-log");
    const logBody = qcmCronEl("hermes-cron-result-log-body");
    const err = String((data && (data.error || data.last_error)) || "").trim();
    const log = (data && data.execution_log) || [];
    if (errWrap && errBody) {
      if (err) {
        errWrap.hidden = false;
        errBody.textContent = err;
      } else {
        errWrap.hidden = true;
        errBody.textContent = "";
      }
    }
    if (logWrap && logBody) {
      if (log.length) {
        logWrap.hidden = false;
        logBody.textContent = log.join("\n");
      } else {
        logWrap.hidden = true;
        logBody.textContent = "";
      }
    }
  }

  function renderCronExecModal(jobId, exec) {
    const metaEl = qcmCronEl("hermes-cron-result-meta");
    const runsEl = qcmCronEl("hermes-cron-result-runs");
    const bodyEl = qcmCronEl("hermes-cron-result-body");
    if (!metaEl || !runsEl || !bodyEl) return;
    hermesCronResultId = jobId;
    const title = qcmCronEl("hermes-cron-result-title");
    if (title) {
      title.textContent = "Execução — " + cronJobDisplayName(jobId);
    }
    const phaseLabel = {
      starting: "Iniciando",
      dispatched: "Disparado",
      running: "Executando",
      done: "Concluído",
      error: "Erro",
    };
    const label = phaseLabel[exec.phase] || exec.phase || "—";
    metaEl.innerHTML =
      '<span class="cron-exec-status"><span class="pill ' +
      (exec.phase === "running" ? "running" : exec.phase === "error" ? "error" : "idle") +
      '"><span class="pulse"></span>' + qcmEsc(label) + "</span>" +
      (exec.summary ? '<span class="meta">' + qcmEsc(exec.summary) + "</span>" : "") +
      "</span>";
    runsEl.innerHTML = "";
    runsEl.style.display = exec.showRuns ? "" : "none";
    renderCronResultPanels(null);
    setCronResponseLabel("Saída", true);
    bodyEl.textContent = buildCronExecBodyText(exec);
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

function openCronExecModal(jobId) {
  if (!openCronResultModal(jobId)) return false;
  const title = qcmCronEl("hermes-cron-result-title");
  if (title) title.textContent = "Execução — " + cronJobDisplayName(jobId);
  return true;
}

  async function pollCronExecRunning(jobId) {
    try {
      const data = await qcmCronFetchJson("/hermes/cron?fresh=1");
      qcmSetCronSnapshot(data);
      return { cron: data, runningJob: findRunningCronJob(jobId, data) };
    } catch (e) {
      return { cron: null, runningJob: null, error: e.message };
    }
  }

  function renderCronResultModalContent(jobId, data) {
    const metaEl = qcmCronEl("hermes-cron-result-meta");
    const runsEl = qcmCronEl("hermes-cron-result-runs");
    const filesEl = qcmCronEl("hermes-cron-generated-files");
    const bodyEl = qcmCronEl("hermes-cron-result-body");
    if (!metaEl || !runsEl || !bodyEl) return;
    const title = qcmCronEl("hermes-cron-result-title");
    if (title) title.textContent = cronJobDisplayName(jobId);
    if (!data) {
      metaEl.textContent = "carregando…";
      runsEl.innerHTML = "";
      renderCronResultDetails(jobId, data);
      renderCronResultPanels(null);
      setCronResponseLabel("Resposta", false);
      if (filesEl) renderGeneratedFilesList(filesEl, [], null);
      bodyEl.textContent = "carregando resultado…";
      return;
    }
    if (!data.ok) {
      metaEl.textContent = data.error || "sem resultado";
      runsEl.innerHTML = "";
      renderCronResultDetails(jobId, data);
      renderCronResultPanels(data);
      setCronResponseLabel("Resposta", false);
      if (filesEl) renderGeneratedFilesList(filesEl, [], null);
      bodyEl.textContent = buildCronResultBodyText(data);
      return;
    }
    const status = data.last_status || (data.error ? "error" : "ok");
    const statusPill =
      status === "error"
        ? '<span class="pill error"><span class="pulse"></span>erro</span>'
        : status === "skipped"
          ? '<span class="pill" style="opacity:.85">ignorado</span>'
          : '<span class="pill idle">ok</span>';
    metaEl.innerHTML =
      statusPill +
      '<span class="meta">' +
      qcmEsc(data.run_time || data.file || "—") +
      "</span>";
    const id = qcmEsc(jobId || "");
    runsEl.innerHTML = (data.runs || []).map((run) => {
      const active = run.file === data.file ? " active" : "";
      return (
        '<button type="button" class="cron-result-pick' + active + '" data-id="' + id +
          '" data-run="' + qcmEsc(run.file || "") + '">' +
          qcmEsc(run.run_time || run.file || "?") +
        "</button>"
      );
    }).join("");
    runsEl.style.display = "";
    renderCronResultDetails(jobId, data);
    renderCronResultPanels(data);
    if (filesEl) {
      renderGeneratedFilesList(filesEl, data.generated_files, data.workdir);
    }
    const bodyText = buildCronResultBodyText(data);
    setCronResponseLabel("Resposta", bodyText && bodyText !== "—");
    bodyEl.textContent = bodyText;
    qcmCronScrollResultIntoView();
  }

  async function loadCronResult(jobId, runFile) {
    let url = "/hermes/cron/" + encodeURIComponent(jobId) + "/result";
    if (runFile) url += "?run=" + encodeURIComponent(runFile);
    return qcmCronFetchJson(url);
  }

  async function finishCronExecDialog(jobId, exec, btn) {
    stopCronExecPoll();
    try {
      const result = await loadCronResult(jobId);
      if (result && result.ok) {
        exec.showRuns = true;
        hermesCronResultCache[jobId] = result;
        renderCronResultModalContent(jobId, result);
        if (btn) {
          btn.textContent = btn.dataset._oldLabel || "▶";
          btn.disabled = false;
        }
        return;
      }
    } catch (_) { /* fall through */ }
    exec.phase = exec.phase === "error" ? "error" : "done";
    if (!exec.summary) {
      exec.summary = "Execução terminou (resultado ainda não disponível no disco).";
    }
    renderCronExecModal(jobId, exec);
    if (btn) {
      btn.textContent = btn.dataset._oldLabel || "▶";
      btn.disabled = false;
    }
  }

  function shlexQuote(s) {
    const t = String(s || "");
    if (/^[A-Za-z0-9_./:-]+$/.test(t)) return t;
    return "'" + t.replace(/'/g, "'\\''") + "'";
  }

  function isIsoTimestampOnly(text) {
    const t = String(text || "").trim();
    return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/.test(t);
  }

  function pickCronExecError(data) {
    const parts = [];
    const summary = String((data && data.summary) || "").trim();
    const reason = String((data && data.reason) || "").trim();
    const err = String((data && data.error) || "").trim();
    const stderr = String((data && data.stderr_tail) || "").trim();
    const stdout = String((data && data.stdout_tail) || "").trim();
    if (summary && !isIsoTimestampOnly(summary)) parts.push(summary);
    if (reason && reason !== summary && !isIsoTimestampOnly(reason)) parts.push(reason);
    if (err && err !== summary && err !== reason && !isIsoTimestampOnly(err)) parts.push(err);
    for (const block of [stderr, stdout]) {
      if (!block) continue;
      const failed = block.split("\n").find((line) => line.trim().startsWith("Failed to "));
      if (failed && !parts.includes(failed.trim())) parts.push(failed.trim());
    }
    if (!parts.length && stderr && !isIsoTimestampOnly(stderr)) parts.push(stderr.slice(-800));
    if (!parts.length && stdout && !isIsoTimestampOnly(stdout)) parts.push(stdout.slice(-800));
    return parts.join("\n") || "falha ao executar cron";
  }

function closeCronResultModal() {
  const modal = qcmCronEl("hermes-cron-result-modal");
  if (!modal || !hermesCronResultModalOpen) return;
  stopCronExecPoll();
  modal.hidden = true;
  modal.setAttribute("hidden", "");
  modal.setAttribute("aria-hidden", "true");
  modal.style.display = "";
  hermesCronResultModalOpen = false;
  hermesCronResultId = null;
  document.body.classList.remove("qcm-cron-exec-open");
  document.body.style.overflow = "";
  const runsEl = qcmCronEl("hermes-cron-result-runs");
  if (runsEl) runsEl.style.display = "";
  qcmRenderCronTables(qcmCronSnapshot());
}

  async function cronRunWithExecDialog(jobId, btn) {
    const id = String(jobId || "").trim();
    if (!id) return;
    const job = qcmCronJobById(id);
    if (job && (job.active === false || job.paused === true)) {
      const msg = "Este cron está pausado. Retome o job antes de executar manualmente.";
      if (typeof kbToast === "function") kbToast(msg);
      else alert(msg);
      return;
    }
    const modelRef = job ? (job.modelId || job.modelLabel || "") : "";
    if (modelRef && typeof isModelEnabled === "function" && !isModelEnabled(modelRef)) {
      const msg = "Este cron usa um modelo pausado nesta interface. Reative o modelo antes de executar.";
      if (typeof kbToast === "function") kbToast(msg);
      else alert(msg);
      return;
    }
    if (!qcmCronEl("hermes-cron-result-modal")) {
      if (typeof kbToast === "function") kbToast("Modal de execução indisponível — recarregue a página.");
      else alert("Modal de execução indisponível — recarregue a página.");
      return;
    }
    stopCronExecPoll();
    cronExecActiveJobId = id;
    const exec = {
      phase: "starting",
      command: "",
      stdout: "",
      stderr: "",
      summary: "",
      logLines: [],
      showRuns: false,
      error: "",
    };
  openCronExecModal(id);
  renderCronExecModal(id, exec);
    const oldLabel = btn ? btn.textContent : "";
    if (btn) {
      btn.disabled = true;
      btn.dataset._oldLabel = oldLabel;
      btn.textContent = "…";
    }
    try {
      const r = await fetch(
        "/hermes/cron/" + encodeURIComponent(id) + "/run",
        { method: "POST", credentials: "same-origin", cache: "no-cache" }
      );
      const data = await r.json().catch(() => ({}));
      exec.command = data.command || ("hermes cron run " + shlexQuote(id));
      exec.stdout = data.stdout_tail || "";
      exec.stderr = data.stderr_tail || "";
      exec.summary = data.summary || "";
      if (!r.ok || !data.ok) {
        exec.phase = "error";
        exec.error = pickCronExecError(data);
        renderCronExecModal(id, exec);
        if (btn) {
          btn.textContent = btn.dataset._oldLabel || "▶";
          btn.disabled = false;
        }
        await qcmRefreshCronUi();
        return;
      }
      if (data.timeout) {
        exec.phase = "error";
        exec.error = data.reason || "timeout";
        renderCronExecModal(id, exec);
        if (btn) {
          btn.textContent = btn.dataset._oldLabel || "▶";
          btn.disabled = false;
        }
        await qcmRefreshCronUi();
        return;
      }
      exec.phase = "dispatched";
      renderCronExecModal(id, exec);
      await qcmRefreshCronUi();
      let sawRunning = false;
      let polls = 0;
      const started = Date.now();
      const maxMs = 3600 * 1000;
      cronExecPollTimer = setInterval(async () => {
        if (cronExecActiveJobId !== id || !hermesCronResultModalOpen) {
          stopCronExecPoll();
          if (btn) {
            btn.textContent = btn.dataset._oldLabel || "▶";
            btn.disabled = false;
          }
          return;
        }
        polls += 1;
        const { cron, runningJob } = await pollCronExecRunning(id);
        qcmRenderCronTables(cron);
        if (runningJob) {
          sawRunning = true;
          exec.phase = "running";
          exec.logLines = runningJob.log_tail || [];
          if (runningJob.last_message) exec.summary = runningJob.last_message;
          renderCronExecModal(id, exec);
          return;
        }
        if (sawRunning || polls >= 3) {
          await finishCronExecDialog(id, exec, btn);
          await qcmRefreshCronUi();
          return;
        }
        if (Date.now() - started > maxMs) {
          exec.phase = "error";
          exec.error = "Tempo máximo de acompanhamento excedido.";
          renderCronExecModal(id, exec);
          stopCronExecPoll();
          if (btn) {
            btn.textContent = btn.dataset._oldLabel || "▶";
            btn.disabled = false;
          }
        }
      }, 2000);
    } catch (e) {
      exec.phase = "error";
      exec.error = e.message || String(e);
      renderCronExecModal(id, exec);
      stopCronExecPoll();
      if (btn) {
        btn.textContent = btn.dataset._oldLabel || "▶";
        btn.disabled = false;
      }
    }
  }

  async function openCronResult(jobId, options) {
    options = options || {};
    hermesCronResultId = jobId;
    hermesCronResultCache[jobId] = null;
    openCronResultModal(jobId);
    renderCronResultModalContent(jobId, null);
    if (options.focusDetails) qcmFocusCronResultDetails();
    qcmRenderCronTables(qcmCronSnapshot());
    try {
      const data = await loadCronResult(jobId);
      hermesCronResultCache[jobId] = data;
      if (hermesCronResultId === jobId) {
        renderCronResultModalContent(jobId, data);
        if (options.focusDetails) qcmFocusCronResultDetails();
      }
    } catch (e) {
      hermesCronResultCache[jobId] = { ok: false, error: e.message };
      if (hermesCronResultId === jobId) {
        renderCronResultModalContent(jobId, hermesCronResultCache[jobId]);
        if (options.focusDetails) qcmFocusCronResultDetails();
      }
    }
  }

  async function pickCronResultRun(jobId, runFile) {
    hermesCronResultId = jobId;
    hermesCronResultCache[jobId] = null;
    renderCronResultModalContent(jobId, null);
    try {
      const data = await loadCronResult(jobId, runFile);
      hermesCronResultCache[jobId] = data;
      if (hermesCronResultId === jobId) renderCronResultModalContent(jobId, data);
    } catch (e) {
      hermesCronResultCache[jobId] = { ok: false, error: e.message };
      if (hermesCronResultId === jobId) {
        renderCronResultModalContent(jobId, hermesCronResultCache[jobId]);
      }
    }
  }

function bindCronPlayButtons(root) {
  const scope = root || document;
  scope.querySelectorAll(
    "button.cron-act[data-action='run'], button.kb-cron-act[data-action='run'], button.cron-play[data-id]"
  ).forEach((btn) => {
    if (btn._qcmRunBound || btn.disabled) return;
    btn._qcmRunBound = true;
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const jobId = btn.dataset.id || btn.getAttribute("data-id") || "";
      if (!jobId) return;
      cronRunWithExecDialog(jobId, btn);
    });
  });
}

function initCronResultModal() {
    const closeBtn = qcmCronEl("hermes-cron-result-close");
    const backdrop = qcmCronEl("hermes-cron-result-backdrop");
    if (closeBtn && !closeBtn._qcmCronBound) {
      closeBtn._qcmCronBound = true;
      closeBtn.addEventListener("click", closeCronResultModal);
    }
    if (backdrop && !backdrop._qcmCronBound) {
      backdrop._qcmCronBound = true;
      backdrop.addEventListener("click", closeCronResultModal);
    }
    if (!document._qcmCronEscBound) {
      document._qcmCronEscBound = true;
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && hermesCronResultModalOpen) closeCronResultModal();
      });
    }
  if (!document._qcmCronRunCaptureBound) {
    document._qcmCronRunCaptureBound = true;
    document.addEventListener("click", (ev) => {
      const btn = qcmClickClosest(
        ev,
        "button.cron-act[data-action='run'], button.kb-cron-act[data-action='run'], button.cron-play[data-id], #task-detail-run-cron"
      );
      if (!btn || btn.disabled) return;
      ev.preventDefault();
      ev.stopPropagation();
      const jobId = btn.dataset.id || btn.getAttribute("data-id") || "";
      if (!jobId) return;
      cronRunWithExecDialog(jobId, btn);
    }, true);
  }
  if (!document._qcmCronPickBound) {
    document._qcmCronPickBound = true;
    document.addEventListener("click", async (ev) => {
      const pick = qcmClickClosest(ev, "button.cron-result-pick");
      if (!pick) return;
      ev.preventDefault();
      await pickCronResultRun(pick.dataset.id, pick.dataset.run);
    });
  }
  if (!document._qcmGeneratedSourceBound) {
    document._qcmGeneratedSourceBound = true;
    document.addEventListener("click", (ev) => {
      const btn = qcmClickClosest(ev, "button.generated-source-btn");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      if (typeof window.openGeneratedSourceViewer !== "function") return;
      window.openGeneratedSourceViewer(
        btn.dataset.path || "",
        btn.dataset.workdir || ""
      );
    });
  }
}

window.cronRunWithExecDialog = cronRunWithExecDialog;
window.openCronResult = openCronResult;
window.buildCronResultBodyText = buildCronResultBodyText;
window.qcmCronJobById = qcmCronJobById;
window.bindCronPlayButtons = bindCronPlayButtons;
window.renderGeneratedFilesList = renderGeneratedFilesList;
initCronResultModal();
"""


def inject_hermes_cron_exec(html: str) -> str:
    """Insert cron exec modal markup, styles, and script into a page template."""
    return (
        html.replace("__HERMES_CRON_EXEC_CSS__", HERMES_CRON_EXEC_MODAL_CSS)
        .replace("__HERMES_CRON_EXEC_MODAL__", HERMES_CRON_EXEC_MODAL_HTML)
        .replace("__HERMES_CRON_EXEC_JS__", HERMES_CRON_EXEC_JS)
    )

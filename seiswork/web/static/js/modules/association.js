class AssociationModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.cfmMethod    = null;          // catalog-filter: method being filtered
    this.alPhaseJobId = null;          // phase modal: job currently shown
    this.alPhase      = {};            // { [jobId]: { page, per_page, total, hasMore, loading, meth } }
  }

  static get CFM_STEPMAP() {
    return { gamma: ['assoc', 'gamma'], real: ['assoc', 'real'],
             pyocto: ['assoc', 'pyocto'], glass3: ['assoc', 'glass3'],
             nlloc: ['locate', 'nlloc'], locsat: ['locate', 'locsat'] };
  }
  static get CFM_FIELDS() {
    return ['min_mag', 'max_mag', 'max_rms', 'min_phase', 'max_gap',
            'min_lat', 'max_lat', 'min_lon', 'max_lon',
            'min_depth', 'max_depth', 'start_time', 'end_time'];
  }
  // Default criteria (matching Jailolo dissertation filter: min_mag .1 / max_rms 5 /
  // min_phase 4 / max_gap 220 / depth 0–100). Unregistered fields = empty.
  static get CFM_DEFAULTS() {
    return { min_mag: 0.1, max_rms: 5, min_phase: 4, max_gap: 220, min_depth: 0, max_depth: 100 };
  }
  static get PHASE_PAGE() { return 200; }

  // --- Controller (tab switching, called from index.html) ---
  switchTab(method) {
    ['gamma', 'real', 'pyocto', 'glass3', 'nlloc', 'locsat'].forEach(m => {
      document.getElementById(`altab-${m}-btn`)?.classList.toggle('active', m === method);
      const p = document.getElementById(`altab-${m}-pane`);
      if (p) p.style.display = m === method ? '' : 'none';
    });
    if (method === 'gamma') this.core.loadPicksFiles('ga-input');
    else if (method === 'real') this.core.loadPicksFiles('rl-input');
    else if (method === 'pyocto') this.core.loadPicksFiles('po-input');
    else if (method === 'glass3') this.core.loadPicksFiles('g3-input');
    else if (method === 'nlloc') this.core.loadCatalogFiles('nl-input', 'locate');
    else if (method === 'locsat') this.core.loadCatalogFiles('ls-input', 'locate');
    this.refreshSessions(method);
  }

  // Toggle magnitude input parameter (checkbox on the association page)
  toggleMag(on) {
    const p = document.getElementById('am-mag-params');
    if (p) p.style.display = on ? '' : 'none';
  }

  // --- View+Controller (completed-session list per method) ---
  async refreshSessions(method) {
    const [step, meth] = AssociationModule.CFM_STEPMAP[method] || [];
    if (!step) return;
    const wrap = document.getElementById(`al-sessions-${method}`);
    const list = document.getElementById(`al-sess-list-${method}`);
    if (!wrap || !list) return;

    if (!this.core.activeConfigId) {
      list.innerHTML = '<div class="al-sess-empty">No active project</div>';
      return;
    }
    // Shimmer immediately — card stays visible
    list.innerHTML = this.core.skelRows(3);

    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=${step}&cfg_id=${this.core.activeConfigId}`)).json();
      const done = jobs.filter(j => j.method === meth && j.state === 'done');
      if (!done.length) {
        list.innerHTML = '<div class="al-sess-empty">No completed sessions yet</div>';
        return;
      }
      list.innerHTML = done.slice(0, 20).map(j => {
        const ts = (j.finished || j.started || '').slice(0, 16).replace('T', ' ');
        const ev = j.events != null ? j.events : '?';
        return `<div class="al-sess-row" style="cursor:pointer"
          onclick="AL.openPhaseModal('${j.id}','${meth}')">
          <span class="al-sess-id" title="${j.id}">${j.id.slice(0, 10)}…</span>
          <span class="al-sess-meth">${meth}${j.filtered ? '<span class="cat-filter-tag" title="filter result">_filter</span>' : ''}</span>
          <span class="al-sess-ts">${ts}</span>
          <span class="al-sess-ev"><b>${ev}</b> ev</span>
          <button class="btn-sw btn-ghost-sw btn-sm-sw" style="pointer-events:auto"
            title="Statistik & plot"
            onclick="event.stopPropagation();AL.openStatsModal('${j.id}','${meth}')">
            <i class="bi bi-bar-chart-fill"></i>
          </button>
          <button class="btn-sw btn-ghost-sw btn-sm-sw" style="pointer-events:auto"
            onclick="event.stopPropagation();viewPipeJobLog('${step}-${meth}','${step}','${j.id}')">log</button>
          <button class="btn-sw btn-danger-sw btn-sm-sw" style="pointer-events:auto"
            title="Delete this association session"
            onclick="event.stopPropagation();AL.deleteSession('${j.id}','${method}','${ev}')">
            <i class="bi bi-trash"></i>
          </button>
        </div>`;
      }).join('');
    } catch { wrap.style.display = 'none'; }
  }

  // Delete one association/location session (job) — so the user can keep only the best.
  async deleteSession(jobId, method, ev) {
    if (!confirm(`Delete session ${method.toUpperCase()} ${jobId} (${ev} events)?\n\n` +
                 `Catalog results & job files will be permanently deleted.`)) return;
    try {
      await fetch(`/api/pipeline/jobs/${jobId}`, { method: 'DELETE' });
    } catch (e) { alert('Failed to delete: ' + e.message); }
    // Reset terminal/viewed-state if this job is currently displayed
    const [step, meth] = AssociationModule.CFM_STEPMAP[method] || [];
    const ch = `${step}-${meth}`;
    if (this.core.pipeViewedJob[ch] === jobId) { delete this.core.pipeViewedJob[ch]; delete this.core.pipeActiveJob[ch]; }
    await this.refreshSessions(method);
    if (step) this.core.refreshPipeJobsCh(ch, step);
  }

  // --- Controller (Catalog Filter modal — adopsi simulflow filter_pha) ---
  // Refresh all downstream catalog dropdowns so the _filter catalog appears immediately.
  refreshAllCatalogDropdowns() {
    [['nl-input', 'locate'], ['ls-input', 'locate'],
     ['hd-input', 'relocation'], ['vel-catalog', 'velocity']
    ].forEach(([id, step]) => { if (document.getElementById(id)) this.core.loadCatalogFiles(id, step); });
  }

  async openCatFilter(method) {
    this.cfmMethod = method;
    const [step, meth] = AssociationModule.CFM_STEPMAP[method] || [];
    if (!step) return;
    const modal = document.getElementById('cat-filter-modal');
    document.getElementById('cfm-method-badge').textContent = meth;
    document.getElementById('cfm-msg').textContent = '';
    const defaults = AssociationModule.CFM_DEFAULTS;
    AssociationModule.CFM_FIELDS.forEach(f => { const el = document.getElementById('cfm-' + f); if (el) el.value = defaults[f] != null ? defaults[f] : ''; });
    // Populate source-catalog dropdown with this method's completed sessions
    const jobSel = document.getElementById('cfm-job');
    if (!this.core.activeConfigId) {
      jobSel.innerHTML = '<option value="">— no active project —</option>';
      return;
    }
    jobSel.innerHTML = '<option value="">⟳ Loading…</option>';
    modal.style.display = 'flex';
    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=${step}&cfg_id=${this.core.activeConfigId}`)).json();
      const done = jobs.filter(j => j.method === meth && j.state === 'done');
      if (!done.length) { jobSel.innerHTML = '<option value="">— no completed sessions yet —</option>'; return; }
      jobSel.innerHTML = done.map(j => {
        const ts = (j.finished || j.started || '').slice(0, 16).replace('T', ' ');
        return `<option value="${j.id}">[${j.id.slice(0, 8)}] ${meth}${j.filtered ? '_filter' : ''} — ${j.events != null ? j.events : '?'} ev (${ts})</option>`;
      }).join('');
    } catch (e) { jobSel.innerHTML = `<option value="">Error: ${this.core.esc(e.message)}</option>`; }
  }

  closeCatFilter() {
    const m = document.getElementById('cat-filter-modal');
    if (m) m.style.display = 'none';
  }

  async runCatFilter() {
    const jobId = (document.getElementById('cfm-job') || {}).value || '';
    const msg = document.getElementById('cfm-msg');
    const btn = document.getElementById('cfm-run');
    if (!jobId) { msg.style.color = '#f87171'; msg.textContent = 'Select a source catalog first.'; return; }
    const criteria = {};
    AssociationModule.CFM_FIELDS.forEach(f => {
      const v = (document.getElementById('cfm-' + f) || {}).value;
      if (v !== '' && v != null) criteria[f] = v;
    });
    btn.disabled = true; msg.style.color = '#94a3b8'; msg.textContent = 'Memfilter…';
    try {
      const res = await fetch('/api/pipeline/filter-catalog', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cfg_id: this.core.activeConfigId, job_id: jobId, criteria }),
      });
      const d = await res.json();
      if (!res.ok) { msg.style.color = '#f87171'; msg.textContent = d.error || 'Failed'; btn.disabled = false; return; }
      msg.style.color = '#34d399';
      msg.textContent = `✓ ${d.passed}/${d.total} events passed → session ${d.job_id} (_filter)`;
      if (this.cfmMethod) await this.refreshSessions(this.cfmMethod);
      // refresh downstream catalog dropdowns so the _filter catalog is immediately available
      this.refreshAllCatalogDropdowns();
      setTimeout(() => { btn.disabled = false; this.closeCatFilter(); }, 1100);
    } catch (e) {
      msg.style.color = '#f87171'; msg.textContent = 'Error: ' + e.message; btn.disabled = false;
    }
  }

  // --- Controller+View (phase-text modal, pagination on scroll) ---
  async openPhaseModal(jobId, meth) {
    this.alPhaseJobId = jobId;
    const m    = document.getElementById('al-phase-modal');
    const pre  = document.getElementById('al-pm-pre');
    const body = document.getElementById('al-pm-body');
    const badge = document.getElementById('al-pm-badge');
    const meta  = document.getElementById('al-pm-meta');
    const src   = document.getElementById('al-pm-src');
    if (!m) return;
    m.style.display = 'flex';
    badge.textContent = meth || '';

    // Show progress in meta
    meta.innerHTML = `<span class="load-pct-wrap">
      <span class="load-pct-bar" style="width:100px"><span class="load-pct-fill" id="alpm-pct-fill" style="width:0%"></span></span>
      <span id="alpm-pct-txt" style="font-size:.62rem;color:#64748b;font-family:monospace">0%</span>
      <span style="font-size:.62rem;color:#334155"> Loading phase data…</span>
    </span>`;
    pre.textContent = '⧗ Loading phase data…';

    // Remove stale load-more strip
    const staleMore = document.getElementById('al-pm-load-more');
    if (staleMore) staleMore.remove();

    // Files link
    const filesLink = document.getElementById('al-pm-dl-csv');
    if (filesLink) { filesLink.href = `/api/pipeline/jobs/${jobId}/files`; filesLink.target = '_blank'; }

    const PHASE_PAGE = AssociationModule.PHASE_PAGE;
    this.alPhase[jobId] = { page: 0, per_page: PHASE_PAGE, total: 0, hasMore: false, loading: true, meth };

    try {
      const d = await this.core.fetchJSON(
        `/api/pipeline/jobs/${jobId}/phase_text?page=0&per_page=${PHASE_PAGE}`,
        pct => {
          const f = document.getElementById('alpm-pct-fill');
          const t = document.getElementById('alpm-pct-txt');
          if (f) f.style.width = pct + '%';
          if (t) t.textContent = pct + '%';
        }
      );
      if (d.error) { pre.textContent = '⚠ ' + d.error; meta.textContent = ''; return; }

      this.alPhase[jobId] = { page: 0, per_page: PHASE_PAGE, total: d.n_events,
                          hasMore: d.has_more, loading: false, meth };

      badge.textContent = `${meth || ''} · ${d.n_events || 0} ev`;
      meta.textContent  = `Job: ${jobId.slice(0, 12)}…  |  ${d.n_events || 0} events  |  source: ${d.source || '?'}`;
      if (src) src.textContent = d.source === 'pha' ? 'pre-built .pha' : 'generated from CSV';

      // Download blob of first page only (full download via Files button)
      const dl = document.getElementById('al-pm-dl-pha');
      if (dl) {
        const blob = new Blob([d.content], { type: 'text/plain' });
        dl.href = URL.createObjectURL(blob);
        dl.download = `${jobId}_phase.pha`;
      }

      // Render first page
      pre.innerHTML = this.phaseHtml(d.content);

      // Attach load-more strip if more events exist
      if (d.has_more && body) this.attachPhaseMoreStrip(jobId, body);

    } catch (e) {
      pre.textContent = '⚠ Error: ' + e.message;
      meta.textContent = '';
    }
  }

  phaseHtml(content) {
    return (content || '').split('\n').map(line => {
      if (line.startsWith('#')) return `<span class="ph-hdr">${this.core.esc(line)}</span>`;
      const parts = line.trimEnd().split(/\s+/);
      const ph = parts[parts.length - 1];
      if (ph === 'P') return `<span class="ph-p">${this.core.esc(line)}</span>`;
      if (ph === 'S') return `<span class="ph-s">${this.core.esc(line)}</span>`;
      return this.core.esc(line);
    }).join('\n') || '(empty)';
  }

  attachPhaseMoreStrip(jobId, bodyEl) {
    // Remove old strip
    const old = document.getElementById('al-pm-load-more');
    if (old) old.remove();

    const st = this.alPhase[jobId];
    const loaded = Math.min((st.page + 1) * st.per_page, st.total);
    const strip = document.createElement('div');
    strip.id = 'al-pm-load-more';
    strip.className = 'phase-load-more';
    strip.innerHTML = `<span id="al-pm-load-more-txt">${loaded.toLocaleString()} / ${st.total.toLocaleString()} events — scroll ↓ to load more</span>`;
    bodyEl.appendChild(strip);

    // Sentinel div (1px) at bottom of body to trigger IntersectionObserver
    const oldSent = document.getElementById('al-pm-body-sentinel');
    if (oldSent) oldSent.remove();
    const sent = document.createElement('div');
    sent.id = 'al-pm-body-sentinel'; sent.style.height = '1px';
    bodyEl.appendChild(sent);

    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { obs.disconnect(); this.loadMorePhase(jobId, bodyEl); }
    }, { root: bodyEl, threshold: 0 });
    obs.observe(sent);
  }

  async loadMorePhase(jobId, bodyEl) {
    const st = this.alPhase[jobId];
    if (!st || st.loading || !st.hasMore) return;
    st.loading = true;

    const pre   = document.getElementById('al-pm-pre');
    const strip = document.getElementById('al-pm-load-more');
    const sent  = document.getElementById('al-pm-body-sentinel');
    if (strip) strip.innerHTML = '<span class="sw-spinner"></span> Loading…';
    if (sent)  sent.remove();

    const nextPage = st.page + 1;
    try {
      const d = await this.core.fetchJSON(
        `/api/pipeline/jobs/${jobId}/phase_text?page=${nextPage}&per_page=${st.per_page}`
      );
      st.page    = nextPage;
      st.hasMore = d.has_more;
      st.loading = false;

      if (pre) pre.insertAdjacentHTML('beforeend', '\n' + this.phaseHtml(d.content));

      if (d.has_more) {
        this.attachPhaseMoreStrip(jobId, bodyEl);
      } else {
        if (strip) {
          strip.innerHTML = `<span style="color:#5cdb7a">✓ All ${st.total.toLocaleString()} events loaded</span>`;
          strip.style.cursor = 'default';
        }
      }
    } catch {
      st.loading = false;
      if (strip) {
        strip.innerHTML = `<span style="color:#f87171">Failed — click to retry</span>`;
        strip.onclick = () => this.loadMorePhase(jobId, bodyEl);
      }
    }
  }

  closePhaseModal() {
    const m = document.getElementById('al-phase-modal');
    if (m) m.style.display = 'none';
    this.alPhaseJobId = null;
    // Cleanup sentinel
    const sent = document.getElementById('al-pm-body-sentinel');
    if (sent) sent.remove();
  }

  // --- View+Controller (Association Stats Modal — time series/phase/RMS/depth/mag/FMD/Wadati/map) ---
  async openStatsModal(jobId, meth) {
    const modal   = document.getElementById('al-stats-modal');
    const loading = document.getElementById('al-sm-loading');
    const plots   = document.getElementById('al-sm-plots');
    const badge   = document.getElementById('al-sm-badge');
    const meta    = document.getElementById('al-sm-meta');
    const summary = document.getElementById('al-sm-summary');
    if (!modal) return;

    modal.style.display = 'flex';
    loading.style.display = 'block';
    plots.style.display = 'none';
    badge.textContent = meth || '';
    meta.textContent  = `Job: ${jobId}`;
    summary.innerHTML = '';

    try {
      await this.core.ensurePlotly();
      const d = await (await fetch(`/api/pipeline/jobs/${jobId}/assoc_stats`)).json();
      if (d.error) { loading.innerHTML = `<span style="color:#ef4444">⚠ ${this.core.esc(d.error)}</span>`; return; }

      badge.textContent = `${meth || ''} · ${d.n_events} ev`;
      meta.textContent  = `Job: ${jobId.slice(0,12)}…`;

      // Summary bar
      const mcTxt = d.mc != null ? d.mc.toFixed(1) : '–';
      const bTxt  = d.gr_b != null ? d.gr_b.toFixed(2) : '–';
      const vpvsTxt = d.vp_vs != null ? d.vp_vs.toFixed(3) : '–';
      const r2Txt   = d.vp_vs_r2 != null ? `R²=${d.vp_vs_r2.toFixed(2)}` : '';
      summary.innerHTML = [
        [d.n_events,                     'Events'],
        [d.n_picks_p,                    'P Phase'],
        [d.n_picks_s,                    'S Phase'],
        [d.rms?.length ? (d.rms.reduce((a,b)=>a+b,0)/d.rms.length).toFixed(3)+'s' : '–', 'RMS Mean'],
        [mcTxt,                          'Mc'],
        [bTxt,                           'b-value'],
        [`${vpvsTxt} ${r2Txt}`,          'Vp/Vs'],
      ].map(([v,l]) => `<div class="al-sm-stat"><div class="val">${v}</div><div class="lbl">${l}</div></div>`
      ).join('<div style="width:1px;background:#334155;margin:.1rem 0"></div>');

      const pc = { responsive: true, displayModeBar: false };

      // 1. Time series
      Plotly.newPlot('al-sm-ts', [{
        type: 'bar', x: d.ts_dates, y: d.ts_counts,
        marker: { color: '#6366f1' }, name: 'Events/day',
      }], this.core.smLayout({ title: { text: 'Event Time Distribution', font: { size: 11, color: '#94a3b8' } },
        yaxis: { title: { text: 'N', font: { size: 9 } }, gridcolor: '#1e293b' },
        margin: { t: 26, b: 30, l: 42, r: 8 } }), pc);

      // 2. Phase P vs S bar
      Plotly.newPlot('al-sm-ps', [{
        type: 'bar', x: ['P', 'S'], y: [d.n_picks_p, d.n_picks_s],
        marker: { color: ['#ef4444', '#3b82f6'] },
      }], this.core.smLayout({ showlegend: false,
        yaxis: { title: { text: 'Pick Count', font: { size: 9 } }, gridcolor: '#1e293b' },
      }), pc);

      // 3. RMS histogram
      Plotly.newPlot('al-sm-rms', [{
        type: 'histogram', x: d.rms, nbinsx: 30,
        marker: { color: '#f59e0b', opacity: 0.85 },
      }], this.core.smLayout({
        xaxis: { title: { text: 'RMS (s)', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'N', font: { size: 9 } }, gridcolor: '#1e293b' },
      }), pc);

      // 4. Depth histogram (rotated: depth on y-axis inverted)
      Plotly.newPlot('al-sm-dep', [{
        type: 'histogram', y: d.depths, nbinsy: 30,
        marker: { color: '#22c55e', opacity: 0.85 }, orientation: 'h',
      }], this.core.smLayout({
        xaxis: { title: { text: 'N', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'Depth (km)', font: { size: 9 } },
                 autorange: 'reversed', gridcolor: '#1e293b' },
      }), pc);

      // 5. Magnitude histogram
      Plotly.newPlot('al-sm-mag', [{
        type: 'histogram', x: d.mags, xbins: { size: 0.1 },
        marker: { color: '#a78bfa', opacity: 0.85 },
      }], this.core.smLayout({
        xaxis: { title: { text: 'Magnitude', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'N', font: { size: 9 } }, gridcolor: '#1e293b' },
        shapes: d.mc != null ? [{ type: 'line', x0: d.mc, x1: d.mc, y0: 0, y1: 1,
          yref: 'paper', line: { color: '#f87171', dash: 'dot', width: 1.5 } }] : [],
        annotations: d.mc != null ? [{ x: d.mc, y: 1, yref: 'paper', text: `Mc=${d.mc}`,
          showarrow: false, font: { color: '#f87171', size: 9 }, xanchor: 'left', yanchor: 'top' }] : [],
      }), pc);

      // 6. FMD + GR law
      const fmdTraces = [];
      if (d.fmd_m?.length) {
        fmdTraces.push({ type: 'bar', x: d.fmd_m, y: d.fmd_n_inc,
          marker: { color: '#818cf8', opacity: 0.6 }, name: 'Non-cumulative', yaxis: 'y' });
        fmdTraces.push({ type: 'scatter', mode: 'lines+markers', x: d.fmd_m, y: d.fmd_n_cum,
          marker: { size: 4, color: '#f59e0b' }, line: { color: '#f59e0b' },
          name: 'Cumulative', yaxis: 'y2' });
        if (d.gr_a != null && d.mc != null) {
          const grM = d.fmd_m.filter(m => m >= d.mc);
          const grN = grM.map(m => Math.pow(10, d.gr_a - d.gr_b * m));
          fmdTraces.push({ type: 'scatter', mode: 'lines', x: grM, y: grN,
            line: { color: '#f87171', dash: 'dot', width: 1.5 },
            name: `GR b=${d.gr_b}`, yaxis: 'y2' });
        }
      }
      Plotly.newPlot('al-sm-mc', fmdTraces, Object.assign({}, this.core.smLayout({
        xaxis: { title: { text: 'Magnitude', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'N (non-cum)', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis2: { title: { text: 'N (cumulative)', font: { size: 9 } }, overlaying: 'y',
                  side: 'right', type: 'log', showgrid: false, color: '#94a3b8' },
        showlegend: true,
        legend: { x: 0.55, y: 1, font: { size: 8 }, bgcolor: 'rgba(0,0,0,0)' },
        shapes: d.mc != null ? [{ type: 'line', x0: d.mc, x1: d.mc, y0: 0, y1: 1,
          yref: 'paper', line: { color: '#f87171', dash: 'dot', width: 1.5 } }] : [],
      }), { margin: { t: 8, b: 28, l: 42, r: 48 } }), pc);

      // 7. Wadati diagram
      const wadatTraces = [];
      if (d.wadati_tp?.length) {
        wadatTraces.push({ type: 'scatter', mode: 'markers',
          x: d.wadati_tp, y: d.wadati_ts_tp,
          marker: { size: 3, color: '#38bdf8', opacity: 0.5 }, name: 'Pick pairs' });
        if (d.wadati_fit_tp?.length) {
          const vpvsLbl = d.vp_vs != null ? `Vp/Vs = ${d.vp_vs}` : 'Fit';
          wadatTraces.push({ type: 'scatter', mode: 'lines',
            x: d.wadati_fit_tp, y: d.wadati_fit_y,
            line: { color: '#f87171', width: 2 }, name: vpvsLbl });
        }
      }
      Plotly.newPlot('al-sm-wadati', wadatTraces, this.core.smLayout({
        xaxis: { title: { text: 'tp (s)', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'ts − tp (s)', font: { size: 9 } }, gridcolor: '#1e293b' },
        showlegend: true,
        legend: { x: 0.02, y: 0.98, font: { size: 8 }, bgcolor: 'rgba(0,0,0,0)' },
        annotations: d.vp_vs_r2 != null ? [{ x: 0.97, y: 0.06, xref: 'paper', yref: 'paper',
          text: `R² = ${d.vp_vs_r2}`, showarrow: false,
          font: { color: '#94a3b8', size: 8 }, xanchor: 'right' }] : [],
      }), pc);

      // ── 2D map (basemap) + 3D — using shared helpers (same as relocation) ──────
      const map2dHint = document.getElementById('al-sm-map2d-hint');
      if (map2dHint) map2dHint.textContent = d.lons?.length ? `${d.lons.length} event` : '';
      await this.core.ensureMapOverlays();
      this.core.plotEpicenterMap2D('al-sm-map2d', d);
      this.core.plotHypoMap3D('al-sm-map3d', d);

      loading.style.display = 'none';
      plots.style.display = 'block';
    } catch (e) {
      loading.innerHTML = `<span style="color:#ef4444">⚠ Error: ${this.core.esc(e.message)}</span>`;
    }
  }

  closeStatsModal() {
    const m = document.getElementById('al-stats-modal');
    if (m) m.style.display = 'none';
    // Purge all plots to avoid memory leaks when reopened
    ['al-sm-ts','al-sm-ps','al-sm-rms','al-sm-dep','al-sm-mag',
     'al-sm-mc','al-sm-wadati','al-sm-map2d','al-sm-map3d'].forEach(id => {
      const el = document.getElementById(id);
      if (el && window.Plotly) Plotly.purge(el);
    });
    const plots = document.getElementById('al-sm-plots');
    const loading = document.getElementById('al-sm-loading');
    if (plots) plots.style.display = 'none';
    if (loading) { loading.style.display = 'block'; loading.innerHTML = '<span class="sw-spinner"></span> Loading statistics…'; }
  }

  // --- Controller (external import — picks/catalog from a local path) ---
  async importPicks() {
    const path = document.getElementById('al-ext-picks-path')?.value?.trim();
    const label = document.getElementById('al-ext-picks-label')?.value?.trim() || 'external';
    const status = document.getElementById('al-ext-picks-status');
    if (!path) { if (status) { status.style.color = '#ef4444'; status.textContent = 'Path cannot be empty.'; } return; }
    if (status) { status.style.color = '#94a3b8'; status.textContent = '⟳ Importing…'; }
    try {
      const r = await fetch('/api/pipeline/import-picks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, label, cfg_id: this.core.activeConfigId || '' }),
      });
      const d = await r.json();
      if (!r.ok) { if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + (d.error || 'Failed'); } return; }
      const p = d.picks || {};
      if (status) { status.style.color = '#22c55e'; status.textContent = `✓ Import successful [${d.job_id}] — ${p.total || 0} picks (P:${p.P || 0} S:${p.S || 0})`; }
      ['ga-input', 'rl-input'].forEach(id => this.core.loadPicksFiles(id));
      const pi = document.getElementById('al-ext-picks-path'); if (pi) pi.value = '';
    } catch (e) { if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + e.message; } }
  }

  async importCatalog() {
    const path = document.getElementById('al-ext-cat-path')?.value?.trim();
    const label = document.getElementById('al-ext-cat-label')?.value?.trim() || 'external';
    const status = document.getElementById('al-ext-cat-status');
    if (!path) { if (status) { status.style.color = '#ef4444'; status.textContent = 'Path cannot be empty.'; } return; }
    if (status) { status.style.color = '#94a3b8'; status.textContent = '⟳ Importing…'; }
    try {
      const r = await fetch('/api/pipeline/import-catalog', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, label, cfg_id: this.core.activeConfigId || '' }),
      });
      const d = await r.json();
      if (!r.ok) { if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + (d.error || 'Failed'); } return; }
      if (status) {
        status.style.color = '#22c55e';
        status.textContent = `✓ Import successful [${d.job_id}] — ${d.events || 0} events`
          + (d.picks_imported ? ' (picks found)' : ' (no companion picks found)');
      }
      // Registered as an "assoc" result — refresh every downstream dropdown that accepts it.
      ['nl-input', 'ls-input', 'vel-catalog', 'hd-input'].forEach(id => {
        if (document.getElementById(id)) this.core.loadCatalogFiles(id,
          id === 'nl-input' || id === 'ls-input' ? 'locate' :
          id === 'vel-catalog' ? 'velocity' : 'relocation');
      });
      const pi = document.getElementById('al-ext-cat-path'); if (pi) pi.value = '';
    } catch (e) { if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + e.message; } }
  }

  // Dead code carried over verbatim from the pre-refactor file (no DOM element
  // with id ext-picks-path/ext-picks-status exists — superseded by importPicks()
  // which targets al-ext-picks-path). Kept as-is; not wired to any onclick.
  async importPicksLegacy() {
    const path = document.getElementById('ext-picks-path')?.value?.trim();
    const label = document.getElementById('ext-picks-label')?.value?.trim() || 'external';
    const status = document.getElementById('ext-picks-status');
    if (!path) { if (status) { status.style.color = '#ef4444'; status.textContent = 'Path cannot be empty.'; } return; }
    if (status) { status.style.color = '#94a3b8'; status.textContent = '⟳ Importing…'; }
    try {
      const r = await fetch('/api/pipeline/import-picks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, label, cfg_id: this.core.activeConfigId || '' }),
      });
      const d = await r.json();
      if (!r.ok) { if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + (d.error || 'Failed'); } return; }
      const p = d.picks || {};
      if (status) {
        status.style.color = '#22c55e';
        status.textContent = `✓ Import successful [${d.job_id}] — ${p.total || 0} picks (P:${p.P || 0} S:${p.S || 0})`;
      }
      // Refresh picks selects in both GaMMA and REAL tabs
      ['ga-input', 'rl-input'].forEach(id => this.core.loadPicksFiles(id));
      // Clear input
      const pi = document.getElementById('ext-picks-path');
      if (pi) pi.value = '';
    } catch (e) {
      if (status) { status.style.color = '#ef4444'; status.textContent = '✗ ' + e.message; }
    }
  }
}

const AL = new AssociationModule(SW);

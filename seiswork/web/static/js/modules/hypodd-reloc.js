// HypoDD .inp EDITOR + RELOCATION SESSION RESULTS/RMS RESIDUAL STATS — by HakimBMKG
// Preview ph2dt.inp + hypoDD.inp from the form parameters (adapted from
// run_hypodd_rekomendasi.ipynb), and the list of finished relocations + residual stats.
class HypoddRelocModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.inpOverride = null;   // {ph2dt, hypodd} when running with an edited config
    this.inpWired     = false; // editor overlay has listener attached?
  }

  // --- View (color one .inp line → colored HTML: comment / path / number) ---
  highlightLine(line) {
    const safe = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const t = line.trimStart();
    if (t.startsWith('*')) {
      // section header ('*--- ...' or comment containing UPPERCASE PARAMETER NAME)
      const cls = (/^\*\s*-{2,}/.test(t) || /[A-Z]{3,}/.test(t))
        ? 'inp-tok-section' : 'inp-tok-comment';
      return `<span class="${cls}">${safe(line)}</span>`;
    }
    // baris data: warnai token angka vs kata/path
    return safe(line).replace(/(-?\d+\.?\d*(?:[eE][+-]?\d+)?)|([^\s]+)/g,
      (m, num, word) => num != null
        ? `<span class="inp-tok-num">${num}</span>`
        : `<span class="inp-tok-path">${word}</span>`);
  }

  highlight(text) {
    return (text || '').split('\n').map(l => this.highlightLine(l)).join('\n');
  }

  // Render highlight from textarea → <pre>, then synchronize scroll.
  refreshHL(taId, hlId) {
    const ta = document.getElementById(taId);
    const hl = document.getElementById(hlId);
    if (!ta || !hl) return;
    hl.innerHTML = this.highlight(ta.value) + '\n';
    hl.scrollTop  = ta.scrollTop;
    hl.scrollLeft = ta.scrollLeft;
  }

  wireEditors() {
    if (this.inpWired) return;
    [['hd-ph2dt-inp', 'hd-ph2dt-hl'], ['hd-hypodd-inp', 'hd-hypodd-hl']].forEach(([taId, hlId]) => {
      const ta = document.getElementById(taId);
      if (!ta) return;
      const sync = () => this.refreshHL(taId, hlId);
      ta.addEventListener('input', sync);
      ta.addEventListener('scroll', () => {
        const hl = document.getElementById(hlId);
        if (hl) { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; }
      });
    });
    this.inpWired = true;
  }

  // --- Controller (.inp editor modal) ---
  async openInpEditor() {
    if (!this.core.wpCfgId) { alert('Open a configuration first'); return; }
    const m = document.getElementById('hd-inp-modal');
    if (!m) return;
    m.style.display = 'flex';
    this.wireEditors();
    const mode = (document.getElementById('hd-mode') || {}).value || 'catalog';
    if (mode === 'growclust') {
      document.getElementById('hd-inp-status').innerHTML =
        '<span style="color:#f59e0b"><i class="bi bi-exclamation-triangle"></i> GrowClust mode does not use hypoDD.inp — this editor is only for catalog/crosscorr mode.</span>';
    }
    await this.reloadInp();
  }

  closeInpEditor() {
    const m = document.getElementById('hd-inp-modal');
    if (m) m.style.display = 'none';
  }

  async reloadInp() {
    const st = document.getElementById('hd-inp-status');
    const taP = document.getElementById('hd-ph2dt-inp');
    const taH = document.getElementById('hd-hypodd-inp');
    if (st) st.innerHTML = '<span class="sw-spinner"></span> Building .inp from parameters…';
    taP.value = ''; taH.value = '';
    this.refreshHL('hd-ph2dt-inp', 'hd-ph2dt-hl');
    this.refreshHL('hd-hypodd-inp', 'hd-hypodd-hl');
    try {
      const params = _getPipeParams('relocation', 'hypodd');
      // do not send override text (always fresh from parameters)
      delete params.ph2dt_inp_text; delete params.hypodd_inp_text;
      const res = await fetch('/api/pipeline/relocation/preview-inp', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cfg_id: this.core.wpCfgId, params }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || 'failed');
      taP.value = d.ph2dt_inp || '';
      taH.value = d.hypodd_inp || '';
      this.refreshHL('hd-ph2dt-inp', 'hd-ph2dt-hl');
      this.refreshHL('hd-hypodd-inp', 'hd-hypodd-hl');
      if (st) st.innerHTML = '<span style="color:#34d399"><i class="bi bi-check-circle"></i> .inp built from form parameters. Correct as needed, then "Run with this config".</span>';
    } catch (e) {
      if (st) st.innerHTML = `<span style="color:#ef4444"><i class="bi bi-x-circle"></i> ${this.core.esc(e.message)}</span>`;
    }
  }

  runWithEditedInp() {
    const taP = document.getElementById('hd-ph2dt-inp');
    const taH = document.getElementById('hd-hypodd-inp');
    const ph2dt  = (taP?.value || '').trim();
    const hypodd = (taH?.value || '').trim();
    if (!ph2dt || !hypodd) { alert('ph2dt.inp / hypoDD.inp text is empty.'); return; }
    this.inpOverride = { ph2dt, hypodd };
    try {
      startPipeJob('relocation', 'hypodd');
    } finally {
      this.inpOverride = null;   // single-use; next run reverts to auto-generate
    }
    this.closeInpEditor();
  }

  // --- View+Controller (completed relocation session list) ---
  async refreshSessions() {
    const list = document.getElementById('reloc-sess-list');
    if (!list) return;
    if (!this.core.activeConfigId) {
      list.innerHTML = '<div class="al-sess-empty">No active project</div>';
      return;
    }
    list.innerHTML = this.core.skelRows(2);
    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=relocation&cfg_id=${this.core.activeConfigId}`)).json();
      const done = jobs.filter(j => j.state === 'done');
      if (!done.length) { list.innerHTML = '<div class="al-sess-empty">No completed relocations yet</div>'; return; }
      list.innerHTML = done.slice(0, 25).map(j => {
        const ts = (j.finished || j.started || '').slice(0, 16).replace('T', ' ');
        const ev = j.events != null ? j.events : '?';
        const mode = j.mode || j.method || 'hypodd';
        const hasDtcc = j.has_dtcc === true;   // dt.cc non-empty exists in job dir
        const showCC  = hasDtcc || ['crosscorr', 'growclust'].includes(mode) || j.method === 'hypodd';
        const modeLabel = hasDtcc ? 'catalog_cc' : mode;
        const modeBadgeStyle = hasDtcc
          ? 'background:#134e26;color:#4ade80;border:1px solid #166534;font-weight:600'
          : '';
        const modeBadgeTitle = hasDtcc
          ? 'Relocation using cross-correlation dt.cc'
          : '';
        return `<div class="reloc-sess-row">
          <span class="reloc-sess-id" title="${j.id}">${j.id.slice(0, 10)}…</span>
          <span class="reloc-sess-mode" style="${modeBadgeStyle}" title="${modeBadgeTitle}">${hasDtcc ? '<i class="bi bi-link-45deg"></i> ' : ''}${this.core.esc(modeLabel)}</span>
          <span class="reloc-sess-ev"><b>${ev}</b> ev</span>
          <span class="reloc-sess-ts">${ts}</span>
          <span style="flex:1"></span>
          ${showCC ? `<button class="btn-sw btn-sm-sw" style="background:#0c4a6e;color:#7dd3fc;border:1px solid #075985" title="Cross-Correlation: CC statistik, waveform, RMS"
            onclick="CC.openModal('${j.id}','${this.core.esc(modeLabel)}')"><i class="bi bi-activity"></i> CC</button>` : ''}
          <button class="btn-sw btn-ghost-sw btn-sm-sw" title="RMS residual statistics"
            onclick="HD.openStatsModal('${j.id}','${this.core.esc(modeLabel)}')"><i class="bi bi-graph-up"></i></button>
          <button class="btn-sw btn-ghost-sw btn-sm-sw" title="View log"
            onclick="viewPipeJobLog('relocation-hypodd','relocation','${j.id}')">log</button>
          <button class="btn-sw btn-danger-sw btn-sm-sw" title="Delete relocation session"
            onclick="HD.deleteSession('${j.id}','${ev}')"><i class="bi bi-trash"></i></button>
        </div>`;
      }).join('');
    } catch { list.innerHTML = '<div class="al-sess-empty">Failed to load sessions</div>'; }
  }

  async deleteSession(jobId, ev) {
    if (!confirm(`Delete relocation session ${jobId} (${ev} events)?\n\nResults & job files will be permanently deleted.`)) return;
    try { await fetch(`/api/pipeline/jobs/${jobId}`, { method: 'DELETE' }); }
    catch (e) { alert('Failed: ' + e.message); }
    const ch = 'relocation-hypodd';
    if (this.core.pipeViewedJob[ch] === jobId) { delete this.core.pipeViewedJob[ch]; delete this.core.pipeActiveJob[ch]; }
    await this.refreshSessions();
    this.core.refreshPipeJobsCh(ch, 'relocation');
  }

  // --- Controller+View (RMS Residual stats modal) ---
  closeStatsModal() {
    const m = document.getElementById('reloc-stats-modal');
    if (m) m.style.display = 'none';
  }

  async openStatsModal(jobId, mode) {
    const modal = document.getElementById('reloc-stats-modal');
    const load  = document.getElementById('reloc-sm-loading');
    const body  = document.getElementById('reloc-sm-body');
    const badge = document.getElementById('reloc-sm-badge');
    const meta  = document.getElementById('reloc-sm-meta');
    if (!modal) return;
    modal.style.display = 'flex';
    load.style.display = 'block';
    body.style.display = 'none';
    badge.textContent = mode || 'hypodd';
    meta.textContent  = `Job: ${jobId}`;

    try {
      await this.core.ensurePlotly();
      const d = await (await fetch(`/api/pipeline/jobs/${jobId}/reloc_stats`)).json();
      if (d.error) { load.innerHTML = `<span style="color:#f59e0b"><i class="bi bi-exclamation-triangle"></i> ${this.core.esc(d.error)}</span>`; return; }

      // Summary chips
      const chips = [
        [d.n_obs,    'Observations'],
        [d.n_p,      'P Phase'],
        [d.n_s,      'S Phase'],
        [d.n_events, 'Event'],
        [d.n_pairs,  'Pairs'],
        [d.n_sta,    'Station'],
        [`${this.core.fmt(d.dist_min,2)}–${this.core.fmt(d.dist_max,2)}`, 'Distance (km)'],
      ];
      document.getElementById('reloc-sm-summary').innerHTML = chips.map(([v, l]) =>
        `<div class="reloc-sm-chip"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');

      // Stats table (per fase)
      const hdr = ['Phase', 'N', 'Mean (s)', 'Median (s)', 'Std (s)', 'RMS (s)', 'Skew', 'Kurtosis', '|Res|>0.3s'];
      const rowFor = (label, s) => s ? `<tr>
        <td>${label}</td><td>${s.n}</td><td>${this.core.fmt(s.mean)}</td><td>${this.core.fmt(s.median)}</td>
        <td>${this.core.fmt(s.std)}</td><td>${this.core.fmt(s.rms)}</td><td>${this.core.fmt(s.skew, 3)}</td>
        <td>${this.core.fmt(s.kurtosis, 2)}</td><td>${this.core.fmt(s.pct_out, 1)}%</td></tr>` : '';
      document.getElementById('reloc-sm-table').innerHTML =
        `<thead><tr>${hdr.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
        rowFor('All', d.all) + rowFor('P (cat=3)', d.p) + rowFor('S (cat=4)', d.s) + '</tbody>';

      load.style.display = 'none';
      body.style.display = 'block';

      const pc = { responsive: true, displayModeBar: false };

      // 1. Histogram of ALL phases — mean line (red) & ideal 0 (gray)
      const h = d.hist_all, sa = d.all;
      if (h && h.x.length) {
        const shapes = [{ type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1,
                          line: { color: '#94a3b8', width: 1, dash: 'dot' } }];
        if (sa && sa.mean != null) shapes.push({ type: 'line', x0: sa.mean, x1: sa.mean,
          yref: 'paper', y0: 0, y1: 1, line: { color: '#ef4444', width: 1.6, dash: 'dash' } });
        Plotly.newPlot('reloc-sm-hist', [{ type: 'bar', x: h.x, y: h.y, marker: { color: '#6366f1' } }],
          this.core.smLayout({
            title: { text: `All Phases — RMS=${this.core.fmt(sa?.rms)}s · Mean=${this.core.fmt(sa?.mean)}s · Skew=${this.core.fmt(sa?.skew, 2)}`,
                     font: { size: 10.5, color: '#94a3b8' } },
            shapes, bargap: 0.02,
            xaxis: { title: { text: 'Residual (s)', font: { size: 9 } }, gridcolor: '#1e293b', zeroline: false },
            yaxis: { title: { text: 'Frequency', font: { size: 9 } }, gridcolor: '#1e293b' },
            margin: { t: 28, b: 36, l: 46, r: 10 },
          }), pc);
      } else { Plotly.purge('reloc-sm-hist'); }

      // 2. Residual box plot per station (quartiles precomputed on server)
      const bs = d.box_station || [];
      if (bs.length) {
        const trace = {
          type: 'box',
          x: bs.map(b => b.sta),
          q1: bs.map(b => b.q1), median: bs.map(b => b.median), q3: bs.map(b => b.q3),
          lowerfence: bs.map(b => b.lo), upperfence: bs.map(b => b.hi),
          mean: bs.map(b => b.mean),
          marker: { color: '#5eead4' }, line: { width: 1.2 },
          fillcolor: 'rgba(94,234,212,.18)', boxmean: true,
          hovertext: bs.map(b => `${b.sta} · N=${b.n}`), hoverinfo: 'text+y',
        };
        const rmsAll = sa ? sa.rms : null;
        const shapes = [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0, y1: 0,
                          line: { color: '#94a3b8', width: 1, dash: 'dash' } }];
        if (rmsAll != null) {
          shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: rmsAll, y1: rmsAll,
                        line: { color: '#fbbf24', width: 1, dash: 'dot' } });
          shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: -rmsAll, y1: -rmsAll,
                        line: { color: '#fbbf24', width: 1, dash: 'dot' } });
        }
        Plotly.newPlot('reloc-sm-box', [trace], this.core.smLayout({
          title: { text: `Residual per station (sorted by median) · yellow line = ±RMS ${this.core.fmt(rmsAll)}s`,
                   font: { size: 10.5, color: '#94a3b8' } },
          shapes, showlegend: false,
          xaxis: { title: { text: 'Station', font: { size: 9 } }, gridcolor: '#1e293b', tickangle: -45, tickfont: { size: 8 } },
          yaxis: { title: { text: 'Residual (s)', font: { size: 9 } }, gridcolor: '#1e293b', zeroline: false },
          margin: { t: 28, b: 54, l: 48, r: 10 },
        }), pc);
      } else { Plotly.purge('reloc-sm-box'); }

      // 3. Peta episenter 2D (basemap) + hiposenter 3D event relokasi
      await this.core.ensureMapOverlays();
      this.core.plotEpicenterMap2D('reloc-sm-map2d', d);
      this.core.plotHypoMap3D('reloc-sm-map3d', d);
    } catch (e) {
      load.innerHTML = `<span style="color:#ef4444">⚠ ${this.core.esc(e.message)}</span>`;
    }
  }
}

const HD = new HypoddRelocModule(SW);

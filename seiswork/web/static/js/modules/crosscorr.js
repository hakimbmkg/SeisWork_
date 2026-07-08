class CrosscorrModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.jobId   = '';
    this.data    = null;  // crosscorr_report response
    this.tab     = 'stats';
    this.rmsInit = false;
  }

  // --- Controller (modal open/close/tabs, called from job-card & index.html) ---
  closeModal() {
    const m = document.getElementById('cc-modal');
    if (m) m.style.display = 'none';
    try { Plotly.purge('cc-rms-map3d'); } catch(_){}
  }

  switchTab(tab) {
    this.tab = tab;
    ['stats', 'wv', 'rms'].forEach(t => {
      const btn = document.getElementById('cc-tab-' + t + '-btn');
      const pnl = document.getElementById('cc-tab-' + t);
      if (btn) btn.classList.toggle('active', t === tab);
      if (pnl) pnl.style.display = t === tab ? '' : 'none';
    });
    if (tab === 'rms' && !this.rmsInit && this.jobId) {
      this.drawRMS(this.jobId);
      this.rmsInit = true;
    }
    if (tab === 'wv' && this.data?.top_pairs?.length && !document.getElementById('cc-pair-sel').options.length > 1) {
      this.fillPairs(this.data.top_pairs);
    }
  }

  async openModal(jobId, mode) {
    const modal = document.getElementById('cc-modal');
    if (!modal) return;
    this.jobId   = jobId;
    this.data    = null;
    this.rmsInit = false;

    modal.style.display = 'flex';
    document.getElementById('cc-loading').style.display = 'block';
    document.getElementById('cc-body').style.display    = 'none';
    document.getElementById('cc-mode-badge').textContent = mode || 'crosscorr';
    document.getElementById('cc-job-meta').textContent   = `Job: ${jobId}`;

    // reset tabs
    this.switchTab('stats');

    try {
      await this.core.ensurePlotly();
      const r = await (await fetch(`/api/pipeline/jobs/${jobId}/crosscorr_report`)).json();
      if (!r.ok) {
        document.getElementById('cc-loading').innerHTML =
          `<span style="color:#f59e0b"><i class="bi bi-exclamation-triangle"></i> ${this.core.esc(r.error)}</span>`;
        return;
      }
      this.data = r;

      // Summary chips
      const s = r.summary;
      const chips = [
        [s.n_obs,    'CC Observations'],
        [s.n_p,      'P Phase'],
        [s.n_s,      'S Phase'],
        [s.n_pairs,  'Event Pairs'],
        [s.n_events, 'Event'],
        [s.n_sta,    'Station'],
        [this.core.fmt(s.mean_cc, 3),   'CC Mean (all)'],
        [this.core.fmt(s.mean_cc_p, 3), 'CC Mean P'],
        [this.core.fmt(s.mean_cc_s, 3), 'CC Mean S'],
      ];
      document.getElementById('cc-summary').innerHTML = chips.map(([v, l]) =>
        `<div class="reloc-sm-chip"><div class="v">${v ?? '–'}</div><div class="l">${l}</div></div>`
      ).join('');

      document.getElementById('cc-loading').style.display = 'none';
      document.getElementById('cc-body').style.display    = 'block';

      this.drawStats(r);
      this.fillPairs(r.top_pairs || []);

    } catch (e) {
      document.getElementById('cc-loading').innerHTML =
        `<span style="color:#ef4444">⚠ ${this.core.esc(e.message)}</span>`;
    }
  }

  // --- View (Plotly charts + tables) ---
  drawStats(r) {
    const pc = { responsive: true, displayModeBar: false };
    const darkLayout = (extra) => Object.assign({
      paper_bgcolor: '#0b1120', plot_bgcolor: '#111827',
      font: { color: '#94a3b8', size: 9 },
      margin: { t: 22, b: 36, l: 48, r: 10 },
    }, extra);

    // CC stats table
    const hdr = ['Phase', 'N', 'Mean', 'Median', 'Std', 'Min', 'Max', '≥0.70 (%)'];
    const tblRows = (r.stats || []).map(s => s.n ? `<tr>
      <td>${s.fase}</td><td>${s.n}</td>
      <td>${this.core.fmt(s.mean, 3)}</td><td>${this.core.fmt(s.median, 3)}</td>
      <td>${this.core.fmt(s.std, 3)}</td><td>${this.core.fmt(s.min, 3)}</td>
      <td>${this.core.fmt(s.max, 3)}</td><td>${this.core.fmt(s.pct_good, 1)}%</td>
    </tr>` : '').join('');
    document.getElementById('cc-stats-table').innerHTML =
      `<thead><tr>${hdr.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${tblRows}</tbody>`;

    // Histogram of all phases (bar, CC 0-1)
    const ha = r.hist?.all || {};
    if ((ha.edges || []).length > 1) {
      const xmid = ha.edges.slice(0, -1).map((e, i) => +((e + ha.edges[i + 1]) / 2).toFixed(4));
      const vline07 = { type: 'line', x0: 0.7, x1: 0.7, yref: 'paper', y0: 0, y1: 1,
                        line: { color: '#22c55e', width: 1.2, dash: 'dash' } };
      Plotly.newPlot('cc-hist-all',
        [{ type: 'bar', x: xmid, y: ha.counts, name: 'All',
           marker: { color: '#6366f1' }, hovertemplate: 'CC=%{x:.3f}<br>n=%{y}<extra></extra>' }],
        darkLayout({
          xaxis: { title: { text: 'CC', font: { size: 9 } }, range: [0, 1], gridcolor: '#1e293b' },
          yaxis: { title: { text: 'Frequency', font: { size: 9 } }, gridcolor: '#1e293b' },
          shapes: [vline07], bargap: 0.05,
        }), pc);
    }

    // Histogram P vs S overlay
    const hp = r.hist?.p || {};
    const hs = r.hist?.s || {};
    const tracesPS = [];
    if ((hp.edges || []).length > 1) {
      const x = hp.edges.slice(0, -1).map((e, i) => +((e + hp.edges[i + 1]) / 2).toFixed(4));
      tracesPS.push({ type: 'bar', x, y: hp.counts, name: 'P', opacity: 0.72,
        marker: { color: '#3b82f6' }, hovertemplate: 'CC=%{x:.3f}<br>n=%{y}<extra></extra>' });
    }
    if ((hs.edges || []).length > 1) {
      const x = hs.edges.slice(0, -1).map((e, i) => +((e + hs.edges[i + 1]) / 2).toFixed(4));
      tracesPS.push({ type: 'bar', x, y: hs.counts, name: 'S', opacity: 0.72,
        marker: { color: '#ef4444' }, hovertemplate: 'CC=%{x:.3f}<br>n=%{y}<extra></extra>' });
    }
    if (tracesPS.length) {
      Plotly.newPlot('cc-hist-ps', tracesPS,
        darkLayout({
          barmode: 'overlay', bargap: 0.05,
          xaxis: { title: { text: 'CC', font: { size: 9 } }, range: [0, 1], gridcolor: '#1e293b' },
          yaxis: { title: { text: 'Frequency', font: { size: 9 } }, gridcolor: '#1e293b' },
          legend: { orientation: 'h', y: -0.2, x: 0.3, font: { size: 9 } },
          shapes: [{ type: 'line', x0: 0.7, x1: 0.7, yref: 'paper', y0: 0, y1: 1,
                     line: { color: '#22c55e', width: 1.2, dash: 'dash' } }],
        }), pc);
    }

    // CC per station — grouped P & S bars
    const ps = r.per_station || [];
    if (ps.length) {
      const stas   = ps.map(s => s.sta);
      const ccP    = ps.map(s => s.p ? s.p.mean : null);
      const ccS    = ps.map(s => s.s ? s.s.mean : null);
      const nP     = ps.map(s => s.p ? s.p.n : 0);
      const nS     = ps.map(s => s.s ? s.s.n : 0);
      Plotly.newPlot('cc-per-sta', [
        { type: 'bar', name: 'P', x: stas, y: ccP, marker: { color: '#3b82f6' },
          text: nP.map(n => `n=${n}`), textposition: 'none',
          hovertemplate: '%{x}<br>CC P=%{y:.3f}<br>n=%{text}<extra></extra>' },
        { type: 'bar', name: 'S', x: stas, y: ccS, marker: { color: '#ef4444' },
          text: nS.map(n => `n=${n}`), textposition: 'none',
          hovertemplate: '%{x}<br>CC S=%{y:.3f}<br>n=%{text}<extra></extra>' },
      ], darkLayout({
        barmode: 'group', bargap: 0.15,
        xaxis: { title: { text: 'Station', font: { size: 9 } }, gridcolor: '#1e293b',
                 tickangle: -45, tickfont: { size: 8 } },
        yaxis: { title: { text: 'Mean CC', font: { size: 9 } }, range: [0, 1.05],
                 gridcolor: '#1e293b' },
        shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0.7, y1: 0.7,
                   line: { color: '#22c55e', width: 1, dash: 'dot' } }],
        legend: { orientation: 'h', y: -0.28, x: 0.35, font: { size: 9 } },
        margin: { t: 22, b: 68, l: 52, r: 10 },
      }), pc);
    }

    // CC vs distance scatter (P biru, S merah)
    const spP = r.scatter_p || {}; const spS = r.scatter_s || {};
    const trScatter = [];
    if ((spP.x || []).length)
      trScatter.push({ type: 'scattergl', mode: 'markers', name: 'P', x: spP.x, y: spP.y,
        marker: { color: '#60a5fa', size: 3, opacity: 0.55 },
        hovertemplate: 'dist=%{x:.1f}km<br>CC=%{y:.3f}<extra>P</extra>' });
    if ((spS.x || []).length)
      trScatter.push({ type: 'scattergl', mode: 'markers', name: 'S', x: spS.x, y: spS.y,
        marker: { color: '#f87171', size: 3, opacity: 0.55 },
        hovertemplate: 'dist=%{x:.1f}km<br>CC=%{y:.3f}<extra>S</extra>' });
    if (trScatter.length)
      Plotly.newPlot('cc-scatter', trScatter, darkLayout({
        xaxis: { title: { text: 'Distance (km)', font: { size: 9 } }, gridcolor: '#1e293b' },
        yaxis: { title: { text: 'CC', font: { size: 9 } }, range: [0, 1.05], gridcolor: '#1e293b' },
        shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0.7, y1: 0.7,
                   line: { color: '#22c55e', width: 1, dash: 'dot' } }],
        legend: { orientation: 'h', y: -0.22, x: 0.35, font: { size: 9 } },
        margin: { t: 22, b: 50, l: 52, r: 10 },
      }), pc);
  }

  fillPairs(pairs) {
    const sel = document.getElementById('cc-pair-sel');
    if (!sel) return;
    sel.innerHTML = '<option value="">— select pair —</option>' +
      (pairs || []).map((p, i) =>
        `<option value="${i}">${p.ev1}↔${p.ev2}  CC=${p.mean_cc.toFixed(3)}  ${p.dist_km}km</option>`
      ).join('');

    // render top-pairs grid cards
    const grid = document.getElementById('cc-pairs-list');
    if (grid) {
      grid.innerHTML = (pairs || []).slice(0, 20).map((p, i) => {
        const cls = p.mean_cc >= 0.7 ? 'color:#4ade80' : (p.mean_cc >= 0.5 ? 'color:#fbbf24' : 'color:#f87171');
        return `<div class="cc-pair-card" onclick="CC.selectPair(${i})" title="Click to view waveform">
          <span style="font-weight:700;font-size:.78rem;${cls}">${p.mean_cc.toFixed(3)}</span>
          <span style="font-size:.65rem;color:#94a3b8">${p.ev1}↔${p.ev2}</span>
          <span style="font-size:.6rem;color:#64748b">${p.dist_km} km · ${p.n_obs} obs</span>
        </div>`;
      }).join('');
    }
  }

  // --- Controller (waveform-pair tab) ---
  selectPair(idx) {
    const sel = document.getElementById('cc-pair-sel');
    if (sel) { sel.value = String(idx); this.onPairChange(); }
    this.switchTab('wv');
  }

  async onPairChange() {
    const sel   = document.getElementById('cc-pair-sel');
    const staSel = document.getElementById('cc-sta-sel');
    if (!sel || !staSel || !this.data) return;
    const idx = parseInt(sel.value);
    if (isNaN(idx)) { staSel.innerHTML = '<option value="">—</option>'; return; }
    const pair = (this.data.top_pairs || [])[idx];
    if (!pair) return;

    staSel.innerHTML = '<option value="">Loading…</option>';
    try {
      const r = await (await fetch(
        `/api/pipeline/jobs/${this.jobId}/crosscorr_waveforms?ev1=${pair.ev1}&ev2=${pair.ev2}`
      )).json();
      const stas = (r.stations || []);
      staSel.innerHTML = '<option value="">— select sta —</option>' +
        stas.map(([s, ph, c]) => `<option value="${s}|${ph}">${s} ${ph} (CC=${c.toFixed(3)})</option>`).join('');
      if (stas.length) {
        staSel.selectedIndex = 1;
        this.onStaChange();
      }
    } catch {
      staSel.innerHTML = '<option value="">Failed</option>';
    }
  }

  async onStaChange() {
    const pairSel = document.getElementById('cc-pair-sel');
    const staSel  = document.getElementById('cc-sta-sel');
    const phaSel  = document.getElementById('cc-pha-sel');
    const info    = document.getElementById('cc-wv-info');
    const loading = document.getElementById('cc-wv-loading');
    const plot    = document.getElementById('cc-wv-plot');
    if (!pairSel || !staSel || !this.data) return;

    const idx = parseInt(pairSel.value);
    const pair = isNaN(idx) ? null : (this.data.top_pairs || [])[idx];
    const staVal = staSel.value;
    if (!pair || !staVal) return;

    let [sta, defaultPha] = staVal.split('|');
    const pha = (phaSel?.value || defaultPha || 'P').toUpperCase();
    if (phaSel && defaultPha) phaSel.value = defaultPha;

    loading.style.display = 'block';
    plot.style.display = 'none';
    if (info) info.textContent = `EV${pair.ev1} ↔ EV${pair.ev2} · ${sta} · ${pha}`;

    try {
      const r = await (await fetch(
        `/api/pipeline/jobs/${this.jobId}/crosscorr_waveforms?ev1=${pair.ev1}&ev2=${pair.ev2}&sta=${encodeURIComponent(sta)}&phase=${pha}`
      )).json();

      loading.style.display = 'none';
      plot.style.display = '';

      if (!r.ok) {
        plot.innerHTML = `<div style="color:#f59e0b;padding:1rem;font-size:.73rem"><i class="bi bi-info-circle"></i> ${this.core.esc(r.error)}</div>`;
        return;
      }

      this.drawWaveformPair(r, pair);
    } catch (e) {
      loading.style.display = 'none';
      plot.innerHTML = `<div style="color:#ef4444;padding:1rem;font-size:.73rem">⚠ ${this.core.esc(e.message)}</div>`;
    }
  }

  drawWaveformPair(r, pair) {
    const ccVal = r.cc  != null ? `CC=${r.cc.toFixed(3)}` : '';
    const dtVal = r.dt_s != null ? `ΔT=${r.dt_s.toFixed(4)}s` : '';
    const label1 = `EV${r.ev1} (t0=${r.ot1?.slice(0, 19) || '?'})`;
    const label2 = `EV${r.ev2} (t0=${r.ot2?.slice(0, 19) || '?'})`;

    const traces = [
      { x: r.t1, y: r.y1, type: 'scattergl', mode: 'lines', name: label1,
        line: { color: '#38bdf8', width: 1.1 },
        hovertemplate: 't=%{x:.3f}s<br>A=%{y:.3f}<extra>EV' + r.ev1 + '</extra>' },
      { x: r.t2, y: r.y2, type: 'scattergl', mode: 'lines', name: label2,
        line: { color: '#fb923c', width: 1.1 },
        hovertemplate: 't=%{x:.3f}s<br>A=%{y:.3f}<extra>EV' + r.ev2 + '</extra>' },
    ];

    // vertical line at phase pick time
    const pPick = r.tt1 ? r.tt1 - (r.win_before || 1.0) : null;
    const shapes = [];
    if (pPick != null) shapes.push({
      type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1,
      line: { color: '#4ade80', width: 1.2, dash: 'dot' }
    });

    Plotly.newPlot('cc-wv-plot', traces, {
      paper_bgcolor: '#0b1120', plot_bgcolor: '#111827',
      font: { color: '#94a3b8', size: 9 },
      title: { text: `${r.sta} · phase ${r.phase} · ${ccVal} · ${dtVal} · dist=${(pair?.dist_km || 0).toFixed(1)} km`,
               font: { color: '#94a3b8', size: 10.5 }, x: 0 },
      xaxis: { title: { text: 'Time relative to EV1 origin (s)', font: { size: 9 } },
               gridcolor: '#1e293b', zeroline: true, zerolinecolor: '#334155' },
      yaxis: { title: { text: 'Amplitude (norm)', font: { size: 9 } },
               range: [-1.15, 1.15], gridcolor: '#1e293b', fixedrange: true },
      legend: { orientation: 'h', y: -0.22, font: { size: 9 } },
      shapes,
      margin: { t: 36, b: 60, l: 52, r: 10 },
    }, { responsive: true, displayModeBar: true, displaylogo: false,
         modeBarButtonsToRemove: ['autoScale2d', 'resetScale2d'] });
  }

  // --- Controller + View (RMS Residual tab) ---
  async drawRMS(jobId) {
    const loading = document.getElementById('cc-rms-loading');
    const body    = document.getElementById('cc-rms-body');
    if (!loading || !body) return;
    loading.style.display = 'block';
    body.style.display    = 'none';

    try {
      const [d, reloc] = await Promise.all([
        fetch(`/api/pipeline/jobs/${jobId}/reloc_stats`).then(r => r.json()),
        fetch(`/api/result/${this.core.activeConfigId || '_'}/residual?job_id=${jobId}`).then(r => r.json()).catch(() => null),
      ]);

      if (d.error) {
        loading.innerHTML = `<span style="color:#f59e0b"><i class="bi bi-info-circle"></i> ${this.core.esc(d.error)}</span>`;
        return;
      }

      // summary chips (reuse reloc stats data)
      const chips = [
        [d.n_obs,    'Observations'],
        [d.n_p,      'P Phase'],
        [d.n_s,      'S Phase'],
        [d.n_events, 'Event'],
        [d.n_pairs,  'Pairs'],
        [d.n_sta,    'Station'],
        [d.all ? this.core.fmt(d.all.rms) + 's' : '–', 'RMS All'],
        [d.p   ? this.core.fmt(d.p.rms)   + 's' : '–', 'RMS P'],
        [d.s   ? this.core.fmt(d.s.rms)   + 's' : '–', 'RMS S'],
      ];
      document.getElementById('cc-rms-summary').innerHTML = chips.map(([v, l]) =>
        `<div class="reloc-sm-chip"><div class="v">${v ?? '–'}</div><div class="l">${l}</div></div>`).join('');

      // stats table
      const hdr = ['Phase', 'N', 'Mean (s)', 'Median (s)', 'Std (s)', 'RMS (s)', 'Skew', '|Res|>0.3s'];
      const trow = (lbl, s) => s ? `<tr>
        <td>${lbl}</td><td>${s.n}</td><td>${this.core.fmt(s.mean)}</td><td>${this.core.fmt(s.median)}</td>
        <td>${this.core.fmt(s.std)}</td><td>${this.core.fmt(s.rms)}</td><td>${this.core.fmt(s.skew, 3)}</td>
        <td>${this.core.fmt(s.pct_out, 1)}%</td></tr>` : '';
      document.getElementById('cc-rms-table').innerHTML =
        `<thead><tr>${hdr.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
        trow('All', d.all) + trow('P (cat=3)', d.p) + trow('S (cat=4)', d.s) + '</tbody>';

      loading.style.display = 'none';
      body.style.display    = 'block';

      const pc = { responsive: true, displayModeBar: false };
      const dark = ex => Object.assign({
        paper_bgcolor: '#0b1120', plot_bgcolor: '#111827',
        font: { color: '#94a3b8', size: 9 },
        margin: { t: 24, b: 40, l: 52, r: 10 },
      }, ex);

      // histogram residu
      const ha = d.hist_all || {};
      if ((ha.x || []).length) {
        const mean0 = d.all?.mean;
        const shapes = [{ type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1,
                          line: { color: '#94a3b8', width: 1, dash: 'dot' } }];
        if (mean0 != null) shapes.push({ type: 'line', x0: mean0, x1: mean0, yref: 'paper',
          y0: 0, y1: 1, line: { color: '#ef4444', width: 1.4, dash: 'dash' } });
        Plotly.newPlot('cc-rms-hist',
          [{ type: 'bar', x: ha.x, y: ha.y, marker: { color: '#6366f1' }, bargap: 0.05 }],
          dark({ xaxis: { title: { text: 'Residual (s)', font: { size: 9 } }, gridcolor: '#1e293b' },
                 yaxis: { title: { text: 'Frequency',   font: { size: 9 } }, gridcolor: '#1e293b' },
                 shapes }), pc);
      }

      // convergence (from residual endpoint /api/result/<cfg>/residual if available)
      const conv = (reloc?.convergence) || [];
      if (conv.length) {
        Plotly.newPlot('cc-rms-conv', [
          { x: conv.map(c => c.it), y: conv.map(c => c.rmsct_ms), type: 'scatter', mode: 'lines+markers',
            name: 'RMS CT (ms)', line: { color: '#38bdf8' } },
          { x: conv.map(c => c.it), y: conv.map(c => c.rmsst_ms), type: 'scatter', mode: 'lines+markers',
            name: 'RMS ST (ms)', line: { color: '#fb923c' } },
        ], dark({
          xaxis: { title: { text: 'Iteration', font: { size: 9 } }, gridcolor: '#1e293b' },
          yaxis: { title: { text: 'RMS (ms)', font: { size: 9 } }, gridcolor: '#1e293b' },
          legend: { orientation: 'h', y: -0.28, font: { size: 9 } },
          margin: { t: 24, b: 58, l: 52, r: 10 },
        }), pc);
      } else {
        document.getElementById('cc-rms-conv').innerHTML =
          '<div style="color:#475569;font-size:.7rem;padding:.7rem">No convergence data (hypoDD.log not readable)</div>';
      }

      // box plot per station
      const bs = d.box_station || [];
      if (bs.length) {
        Plotly.newPlot('cc-rms-box', [{
          type: 'box', x: bs.map(b => b.sta),
          q1: bs.map(b => b.q1), median: bs.map(b => b.median), q3: bs.map(b => b.q3),
          lowerfence: bs.map(b => b.lo), upperfence: bs.map(b => b.hi),
          mean: bs.map(b => b.mean), marker: { color: '#5eead4' },
          line: { width: 1.2 }, fillcolor: 'rgba(94,234,212,.18)', boxmean: true,
          hovertext: bs.map(b => `${b.sta} · N=${b.n}`), hoverinfo: 'text+y',
        }], dark({
          xaxis: { title: { text: 'Station', font: { size: 9 } }, gridcolor: '#1e293b',
                   tickangle: -45, tickfont: { size: 8 } },
          yaxis: { title: { text: 'Residual (s)', font: { size: 9 } }, gridcolor: '#1e293b' },
          shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0, y1: 0,
                     line: { color: '#94a3b8', width: 1, dash: 'dash' } }],
          margin: { t: 24, b: 64, l: 54, r: 10 },
        }), pc);
      }

      // 2D & 3D map — reuse from reloc_stats
      if ((d.lons || []).length) {
        await this.core.ensureMapOverlays();
        this.core.plotEpicenterMap2D('cc-rms-map2d', d);
        this.core.plotHypoMap3D('cc-rms-map3d', d);
      }
    } catch (e) {
      loading.innerHTML = `<span style="color:#ef4444">⚠ ${this.core.esc(e.message)}</span>`;
    }
  }
}

const CC = new CrosscorrModule(SW);

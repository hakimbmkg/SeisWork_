// Imaging (SIMUL2000) — figure gallery + 3D Vp/DWS model viewer (per-layer
// Plotly `surface` checklist OR a single interpolated `volume` trace, same
// lon/lat-from-km convention as the SimulFlow PNG plots, + always-on
// event/station markers, optional raypath overlay, topography and coastline
// for orientation — reusing the same /api/topo/grid + /api/footages/coastlines
// feeds as the Result Viewer 3D modal, but cached separately: this modal has
// no dependency on a Result Viewer catalog session being open) — by HakimBMKG
class TomographyModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.figJob = null;   // job currently shown in the figure gallery
    this.v3d = {
      jobId: null, data: null, es: null, init: false,
      rays: null, raysJob: null, topo: null, coast: null,
      iso: null, isoJob: null, isoThreshold: null,
      volcanoes: null,        // [{lon,lat,name}] fetched from Volcano.geojson
      fieldTraceStart: 0, nFieldTraces: 0,
      _hdrRo: null,
    };
  }

  // --- Controller (figure gallery — restore on step open / job completion) ---
  async restoreFigures() {
    if (!this.core.activeConfigId) return;
    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=imaging&cfg_id=${this.core.activeConfigId}`)).json();
      const last = Array.isArray(jobs) && jobs.find(j => j.state === 'done');
      if (last) this.showFigures(last.id);
    } catch { }
  }

  async showFigures(jobId) {
    this.figJob = jobId;
    const card = document.getElementById('tomo-fig-card');
    const gal  = document.getElementById('tomo-fig-gallery');
    const title = document.getElementById('tomo-fig-title');
    if (!card || !gal) return;
    try {
      const figs = await (await fetch(`/api/imaging/${jobId}/figures`)).json();
      if (!Array.isArray(figs) || !figs.length) {
        gal.innerHTML = `<div style="font-size:.66rem;color:var(--text-muted)">No figures produced yet — check the run log.</div>`;
        card.style.display = '';
        if (title) title.textContent = `job ${jobId}`;
        return;
      }
      gal.innerHTML = figs.map(f => {
        const url = `/api/imaging/${jobId}/figure?name=${encodeURIComponent(f.rel)}`;
        const body = f.interactive
          ? `<iframe src="${url}" style="display:block;width:100%;height:360px;border:0;border-radius:4px;background:#fff"></iframe>
             <a href="${url}" target="_blank" style="font-size:.6rem;color:#60a5fa">↗ open 3D in new tab</a>`
          : `<a href="${url}" target="_blank" title="Open full size">
               <img src="${url}&t=${Date.now()}" style="display:block;width:100%;border-radius:4px;background:#fff"/>
             </a>`;
        return `<div style="background:#0a0f1e;border-radius:5px;padding:.4rem">
          <div style="font-size:.62rem;color:var(--text-muted);margin-bottom:.3rem">${this.core.esc(f.label)}${f.interactive ? ' <span style="color:#34d399">· interactive</span>' : ''}</div>
          ${body}
        </div>`;
      }).join('');
      card.style.display = '';
      if (title) title.textContent = `job ${jobId} · ${figs.length} figure${figs.length > 1 ? 's' : ''}`;
    } catch (e) {
      gal.innerHTML = `<div style="font-size:.66rem;color:#f87171">Failed to load figures: ${this.core.esc(e.message)}</div>`;
      card.style.display = '';
    }
  }

  refreshFigures() {
    if (this.figJob) this.showFigures(this.figJob);
  }

  // --- Model (fetch with streaming byte-progress + network badge update) ---
  async fetchWithProgress(url, opts = {}) {
    // opts: { onPct(pct, received, total) }
    // pct = -1 means indeterminate (no Content-Length)
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    if (!r.body) { const j = await r.json(); if (opts.onPct) opts.onPct(100, 0, 0); return j; }
    const total = +r.headers.get('content-length') || 0;
    const reader = r.body.getReader();
    const chunks = [];
    let received = 0;
    const t0 = performance.now();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (opts.onPct) opts.onPct(total ? Math.min(95, (received / total) * 100) : -1, received, total);
    }
    const elapsed = (performance.now() - t0) / 1000;
    if (received > 4096 && elapsed > 0.05) this.netBadgeSpeed(received, elapsed);
    if (opts.onPct) opts.onPct(98, received, received || total);
    const text = await new Blob(chunks).text();
    if (opts.onPct) opts.onPct(100, received, received || total);
    return JSON.parse(text);
  }

  // Update network badge with measured throughput (called after each significant fetch)
  netBadgeSpeed(bytes, seconds) {
    const kbs  = bytes / 1024 / seconds;
    const lbl  = document.getElementById('sw-net-lbl');
    const dot  = document.getElementById('sw-net-dot');
    if (!lbl || !dot) return;
    dot.style.background = '#22c55e';
    lbl.textContent = kbs >= 1024 ? `${(kbs / 1024).toFixed(1)} MB/s` : `${Math.round(kbs)} KB/s`;
  }

  // --- View (progress bar helpers for the tomo-3d-loading overlay) ---
  setProgress(pct, msg) {
    if (msg) { const el = document.getElementById('tomo-3d-load-msg'); if (el) el.textContent = msg; }
    const wrap = document.getElementById('tomo-3d-prog-wrap');
    const bar  = document.getElementById('tomo-3d-prog-bar');
    const pctEl = document.getElementById('tomo-3d-prog-pct');
    if (!wrap) return;
    wrap.style.display = 'block';
    if (pct < 0) {
      bar.className  = 'sw-prog-bar indeterminate';
      if (pctEl) pctEl.style.display = 'none';
    } else {
      bar.className  = 'sw-prog-bar';
      bar.style.width = Math.round(pct) + '%';
      if (pctEl) { pctEl.style.display = 'inline'; pctEl.textContent = Math.round(pct) + '%'; }
    }
  }
  clearProgress() {
    const wrap = document.getElementById('tomo-3d-prog-wrap');
    const bar  = document.getElementById('tomo-3d-prog-bar');
    const pctEl = document.getElementById('tomo-3d-prog-pct');
    if (wrap)  { wrap.style.display = 'none'; }
    if (bar)   { bar.className = 'sw-prog-bar'; bar.style.width = '0%'; }
    if (pctEl) { pctEl.style.display = 'none'; pctEl.textContent = ''; }
  }

  // Dynamically adjust plot-div inset to match actual header height
  // (the header is flex-column with variable rows depending on mode)
  fixInset() {
    const hdr = document.querySelector('#tomo-3d-m > .rm-3d-hdr');
    if (!hdr) return;
    const h = Math.ceil(hdr.getBoundingClientRect().height) + 2;
    for (const id of ['tomo-3d-plt', 'tomo-3d-loading']) {
      const el = document.getElementById(id);
      if (el) el.style.inset = `${h}px 0 0 0`;
    }
  }

  // --- Controller (3D viewer modal open/close) ---
  async open3d(jobId) {
    document.getElementById('tomo-3d-bd').classList.remove('hidden');
    document.getElementById('tomo-3d-job-lbl').textContent = `job ${jobId}`;
    // Setup ResizeObserver once so the plot div always fills below the header
    if (!this.v3d._hdrRo) {
      const hdr = document.querySelector('#tomo-3d-m > .rm-3d-hdr');
      if (hdr && window.ResizeObserver) {
        this.v3d._hdrRo = new ResizeObserver(() => this.fixInset());
        this.v3d._hdrRo.observe(hdr);
      }
    }
    this.fixInset();
    try { await this.core.ensurePlotly(); } catch (e) { alert('Plotly failed to load: ' + e.message); return; }
    if (this.v3d.jobId === jobId && this.v3d.data) { this.draw3d(); return; }
    const ovl = document.getElementById('tomo-3d-loading');
    if (ovl) ovl.classList.remove('hidden');
    this.setProgress(-1, 'Loading Vp/DWS 3D grid…');
    // New job → drop cached raypaths/events/iso/volcanoes (topo/coast region-cached)
    this.v3d.rays = null; this.v3d.raysJob = null; this.v3d.es = null;
    this.v3d.iso  = null; this.v3d.isoJob  = null; this.v3d.isoThreshold = null;
    this.v3d.volcanoes = null;
    document.getElementById('tomo-3d-rays').checked = false;
    document.getElementById('tomo-3d-rays-info').textContent = '';
    document.getElementById('tomo-3d-cells').checked = false;
    document.getElementById('tomo-3d-events').checked = true;
    try {
      // Parallel fetch — velocity_grid drives the bar (larger), events_stations is minor
      let p1 = 0, p2 = 0;
      const combineProgress = () => {
        if (p1 < 0) { this.setProgress(-1); return; }
        this.setProgress(p1 * 0.8 + (p2 < 0 ? 0 : p2) * 0.2);
      };
      const [d, es] = await Promise.all([
        this.fetchWithProgress(`/api/imaging/${jobId}/velocity_grid`,
          { onPct: p => { p1 = p; combineProgress(); } }),
        this.fetchWithProgress(`/api/imaging/${jobId}/events_stations`,
          { onPct: p => { p2 = p; combineProgress(); } }),
      ]);
      if (d.error) throw new Error(d.error);
      this.setProgress(100, 'Rendering…');
      this.v3d.jobId = jobId;
      this.v3d.data  = d;
      this.v3d.es    = es.error ? null : es;
      const fieldSel = document.getElementById('tomo-3d-field');
      fieldSel.querySelector('option[value="dv"]').disabled  = !d.dv;
      fieldSel.querySelector('option[value="dws"]').disabled = !d.dws;
      if (!d.dv  && fieldSel.value === 'dv')  fieldSel.value = 'vp';
      if (!d.dws && fieldSel.value === 'dws') fieldSel.value = d.dv ? 'dv' : 'vp';
      this.buildLayersChecklist();
      if (this.v3d.topo      === null) this.loadTopo();
      if (this.v3d.coast     === null) this.loadCoast();
      if (this.v3d.volcanoes === null) this.loadVolcanoes();
      await this.draw3d();   // await so overlay hides only after Plotly renders
    } catch (e) {
      this.clearProgress();
      if (ovl) ovl.classList.add('hidden');
      alert(`Failed to load the 3D grid: ${e.message}`);
      return;
    }
    this.clearProgress();
    if (ovl) ovl.classList.add('hidden');
  }

  close3d() {
    document.getElementById('tomo-3d-bd').classList.add('hidden');
  }

  // One checkbox per depth node. Default-checked = layers near the actual
  // seismicity (event depth range ± a margin) so the first view isn't cluttered
  // with deep damping-only layers that have no earthquakes in them — the
  // "Select All" / "Select None" buttons let the user override in one click.
  buildLayersChecklist() {
    const d = this.v3d.data, es = this.v3d.es;
    const list = document.getElementById('tomo-3d-layers-list');
    let lo = -Infinity, hi = Infinity;
    if (es) {
      const span = Math.max(5, es.depth_max - es.depth_min);
      lo = es.depth_min - span * 0.25; hi = es.depth_max + span * 0.25;
    }
    list.innerHTML = d.depths.map((dep, iz) => {
      const checked = !es || (dep >= lo && dep <= hi);
      return `<label class="rm-lay-tog"><input type="checkbox" class="tomo-3d-lyr" value="${iz}"
                ${checked ? 'checked' : ''} onchange="TOMO.draw3d()"><span>${dep.toFixed(1)} km</span></label>`;
    }).join('');
  }

  layersAll(on) {
    document.querySelectorAll('.tomo-3d-lyr').forEach(cb => cb.checked = on);
    this.draw3d();
  }

  checkedLayers() {
    return [...document.querySelectorAll('.tomo-3d-lyr')].filter(cb => cb.checked).map(cb => +cb.value);
  }

  modeChange() {
    const mode = document.querySelector('input[name="tomo-3d-mode"]:checked').value;
    document.getElementById('tomo-3d-layers-row').style.display = (mode === 'layers') ? '' : 'none';
    document.getElementById('tomo-3d-iso-row').style.display    = (mode === 'iso')    ? '' : 'none';
    this.fixInset();  // iso-row visibility changes header height
    if (mode === 'iso') { this.isoFetch(); return; }
    this.draw3d();
  }

  // --- Controller+View (Isosurface 3D — lazy fetch + render) ---
  async isoFetch() {
    const thr = +document.getElementById('tomo-3d-thr').value;
    if (this.v3d.isoJob === this.v3d.jobId && this.v3d.isoThreshold === thr && this.v3d.iso) {
      this.draw3d(); return;
    }
    const ovl = document.getElementById('tomo-3d-loading');
    if (ovl) ovl.classList.remove('hidden');
    this.setProgress(-1, `Interpolating 3D isosurface (DWS ∩ ΔVp, thr=${thr}%)…`);
    try {
      const r = await this.fetchWithProgress(
        `/api/imaging/${this.v3d.jobId}/velocity_isosurface?threshold=${thr}`,
        { onPct: p => this.setProgress(p) });
      if (r.error) throw new Error(r.error);
      this.v3d.iso          = r;
      this.v3d.isoJob       = this.v3d.jobId;
      this.v3d.isoThreshold = thr;
      const info = document.getElementById('tomo-3d-iso-info');
      if (info) info.textContent = `${r.n_events} ev | ${r.dep_min.toFixed(0)}–${r.dep_max.toFixed(0)} km`;
      this.setProgress(100, 'Rendering…');
      await this.draw3d();
    } catch (e) {
      alert('Failed to load isosurface: ' + e.message);
    }
    this.clearProgress();
    if (ovl) ovl.classList.add('hidden');
  }

  isoTraces() {
    const iso = this.v3d.iso;
    if (!iso) return [];
    const thr = iso.threshold;
    return [{
      type: 'isosurface',
      x: iso.x, y: iso.y, z: iso.z, value: iso.value,
      isomin: -thr, isomax: thr,
      surface: { count: 2, fill: 1.0 },
      // Plotly's built-in "RdBu" is low=blue/high=red — the OPPOSITE of
      // matplotlib's RdBu (low=red/high=blue) used by plot_velocity PNGs.
      // reversescale:true flips it to match: Red=slow/negative, Blue=fast/positive.
      colorscale: 'RdBu', reversescale: true,
      cmin: -thr, cmax: thr,
      opacity: 0.55,
      caps: { x: { show: false }, y: { show: false }, z: { show: false } },
      showscale: true,
      colorbar: {
        title: { text: `ΔVp (%)<br>±${thr}%`, side: 'right' },
        len: 0.6, thickness: 14,
        tickfont: { size: 10, color: '#94a3b8' },
        title_font: { size: 10, color: '#94a3b8' },
      },
      name: 'Vp Anomaly', showlegend: false,
      hovertemplate: 'ΔVp %{value:.1f}%<extra></extra>',
      lighting: { ambient: 0.8, diffuse: 0.6, specular: 0.3, roughness: 0.5 },
      lightposition: { x: 1, y: 1, z: -1 },
    }];
  }

  // --- Model (fetch + cache raypaths for the current job — shared by the
  //     "Raypaths" line toggle and the "Grid Cells" cube toggle, since cube
  //     placement is now derived from the raypath corridor) ---
  async ensureRays() {
    if (this.v3d.raysJob === this.v3d.jobId && this.v3d.rays) return this.v3d.rays;
    const ovl = document.getElementById('tomo-3d-loading');
    if (ovl) ovl.classList.remove('hidden');
    this.setProgress(-1, 'Loading raypaths…');
    try {
      const d = await this.fetchWithProgress(
        `/api/imaging/${this.v3d.jobId}/raypaths?max_rays=3000`,
        { onPct: p => this.setProgress(p) });
      if (d.error) throw new Error(d.error);
      this.v3d.rays    = d;
      this.v3d.raysJob = this.v3d.jobId;
      const info = document.getElementById('tomo-3d-rays-info');
      if (info) info.textContent = `${d.n_shown}/${d.n_total} rays`;
      this.setProgress(100, 'Rendering raypaths…');
      return d;
    } finally {
      this.clearProgress();
      if (ovl) ovl.classList.add('hidden');
    }
  }

  // --- Controller+View (Raypaths — curved lines only; event/station markers
  //     are always shown via esTraces(), independent of this toggle) ---
  async raysToggle() {
    const on = document.getElementById('tomo-3d-rays').checked;
    if (!on) { this.draw3d(); return; }
    try {
      await this.ensureRays();
      await this.draw3d();
    } catch (e) {
      document.getElementById('tomo-3d-rays').checked = false;
      alert(`Failed to load raypaths: ${e.message}`);
    }
  }

  rayLineTrace() {
    const r = this.v3d.rays;
    if (!r) return [];
    const lon = [], lat = [], dep = [];
    for (const ray of r.rays) {
      for (const [lo, la, de] of ray) { lon.push(lo); lat.push(la); dep.push(de); }
      lon.push(null); lat.push(null); dep.push(null);
    }
    return [{
      type: 'scatter3d', mode: 'lines',
      x: lon, y: lat, z: dep,
      line: { color: 'dodgerblue', width: 1 }, opacity: 0.12,
      hoverinfo: 'skip', name: 'Raypaths', showlegend: false,
    }];
  }

  // --- Controller (Grid Cells toggle — below Raypaths; needs raypath data to
  //     know which corridor of cells to draw, so it shares ensureRays()) ---
  async cellsToggle() {
    const on = document.getElementById('tomo-3d-cells').checked;
    if (!on) { this.draw3d(); return; }
    try {
      await this.ensureRays();
      await this.draw3d();
    } catch (e) {
      document.getElementById('tomo-3d-cells').checked = false;
      alert(`Failed to load raypaths: ${e.message}`);
    }
  }

  // --- Controller (Earthquakes toggle — below Raypaths) ---
  eventsToggle() { this.draw3d(); }

  // 12 triangles (2 per face) covering a cube, as vertex-index triplets
  // relative to its own 8 corners (0..7): see CUBE_CORNERS below for the
  // corner layout this indexes into.
  static get CUBE_FACES() {
    return [
      [0, 1, 2], [0, 2, 3],   // bottom (z0)
      [4, 5, 6], [4, 6, 7],   // top (z1)
      [0, 1, 5], [0, 5, 4],   // front (y0)
      [3, 2, 6], [3, 6, 7],   // back (y1)
      [0, 3, 7], [0, 7, 4],   // left (x0)
      [1, 2, 6], [1, 6, 5],   // right (x1)
    ];
  }

  // --- View (ΔVp grid cells — one solid cube per SIMUL2000 grid cell that
  //     the raypath corridor actually passes through, coloured red=slow/
  //     negative · blue=fast/positive, same RdBu convention as the Volume/
  //     Layer/Isosurface ΔVp views). Computed entirely client-side from the
  //     dv grid (already fetched for those other modes) + the cached
  //     raypaths fetch (ensureRays()) — no extra network round-trip beyond
  //     that. Drawing every cell in the full grid made the cubes nearly
  //     invisible (too many, too small relative to the scene); restricting
  //     to cells a raypath sample falls inside keeps only the
  //     event-to-station corridor, which is both more legible and closer to
  //     "raypath nodes" the feature was asked for. ---
  cellsTrace() {
    const d = this.v3d.data, rays = this.v3d.rays;
    if (!d || !d.dv || !rays) return [];
    const lons = d.lons, lats = d.lats, deps = d.depths;
    const nx = lons.length, ny = lats.length, nz = deps.length;
    if (nx < 2 || ny < 2 || nz < 2) return [];
    const nxC = nx - 1, nyC = ny - 1;

    // Index of the grid cell containing `val` along a sorted edges[] axis.
    const cellIndex = (val, edges) => {
      if (val <= edges[0]) return 0;
      if (val >= edges[edges.length - 1]) return edges.length - 2;
      for (let i = 0; i < edges.length - 1; i++) if (val < edges[i + 1]) return i;
      return edges.length - 2;
    };

    // Mark every cell that any raypath sample falls inside — densify each
    // ray's bend-curve segments so the corridor doesn't skip cells between
    // the curve's original sample points.
    const marked = new Set();
    const steps = 6;
    for (const ray of rays.rays) {
      for (let p = 0; p < ray.length - 1; p++) {
        const [lo0, la0, de0] = ray[p], [lo1, la1, de1] = ray[p + 1];
        for (let s = 0; s <= steps; s++) {
          const t = s / steps;
          const ix = cellIndex(lo0 + (lo1 - lo0) * t, lons);
          const iy = cellIndex(la0 + (la1 - la0) * t, lats);
          const iz = cellIndex(de0 + (de1 - de0) * t, deps);
          marked.add(iz * nyC * nxC + iy * nxC + ix);
        }
      }
    }
    if (!marked.size) return [];

    const X = [], Y = [], Z = [], I = [], J = [], K = [], C = [];
    const faces = TomographyModule.CUBE_FACES;
    const cellVals = [];

    for (const key of marked) {
      const iz = Math.floor(key / (nyC * nxC));
      const iy = Math.floor((key - iz * nyC * nxC) / nxC);
      const ix = key - iz * nyC * nxC - iy * nxC;
      const v = (d.dv[iz][iy][ix] + d.dv[iz][iy][ix + 1] + d.dv[iz][iy + 1][ix] + d.dv[iz][iy + 1][ix + 1]
               + d.dv[iz + 1][iy][ix] + d.dv[iz + 1][iy][ix + 1] + d.dv[iz + 1][iy + 1][ix] + d.dv[iz + 1][iy + 1][ix + 1]) / 8;
      cellVals.push(v);

      const x0 = lons[ix], x1 = lons[ix + 1], y0 = lats[iy], y1 = lats[iy + 1], z0 = deps[iz], z1 = deps[iz + 1];
      const base = X.length;
      // Corner order matches CUBE_FACES: 0-3 bottom (z0) CCW, 4-7 top (z1) CCW.
      X.push(x0, x1, x1, x0, x0, x1, x1, x0);
      Y.push(y0, y0, y1, y1, y0, y0, y1, y1);
      Z.push(z0, z0, z0, z0, z1, z1, z1, z1);
      for (let f = 0; f < faces.length; f++) {
        I.push(base + faces[f][0]); J.push(base + faces[f][1]); K.push(base + faces[f][2]);
      }
      for (let c = 0; c < 8; c++) C.push(v);
    }
    const absMax = Math.max(...cellVals.map(Math.abs)) || 1;

    return [{
      type: 'mesh3d', x: X, y: Y, z: Z, i: I, j: J, k: K,
      intensity: C, colorscale: 'RdBu', reversescale: true,
      cmin: -absMax, cmax: absMax,
      opacity: 0.45, flatshading: true,
      lighting: { ambient: 0.7, diffuse: 0.4 },
      showscale: true, colorbar: { title: { text: 'ΔVp (%)<br>(grid cells)' }, len: 0.6, thickness: 12,
        tickfont: { size: 10, color: '#94a3b8' }, title_font: { size: 10, color: '#94a3b8' } },
      hoverinfo: 'skip', name: 'Grid Cells (ΔVp)', showlegend: false,
    }];
  }

  // --- View (event/station/volcano markers — earthquakes gated by the
  //     "Earthquakes" checkbox; stations/volcanoes stay always-on) ---
  esTraces(showEvents = true) {
    const es = this.v3d.es, d = this.v3d.data;
    if (!es || !d) return [];

    // Bilinear interpolation of topo elevation → z for on-surface anchoring.
    // Applies vertical exaggeration so markers ride the same scaled terrain
    // surface that topoTrace() renders.
    const ex = this.exag();
    const topoZ = (lo, la) => {
      const t = this.v3d.topo;
      if (!t?.elev?.length) return 0;
      const ni = t.lats.length, nj = t.lons.length;
      const jf = (lo - t.lons[0]) / (t.lons[nj-1] - t.lons[0]) * (nj-1);
      const if_ = (la - t.lats[0]) / (t.lats[ni-1] - t.lats[0]) * (ni-1);
      const j = Math.max(0, Math.min(nj-2, Math.floor(jf)));
      const i = Math.max(0, Math.min(ni-2, Math.floor(if_)));
      const fj = jf - j, fi = if_ - i;
      const e = (1-fi)*(1-fj)*(t.elev[i]?.[j]  ?? 0)
              + (1-fi)*fj   *(t.elev[i]?.[j+1] ?? 0)
              + fi   *(1-fj)*(t.elev[i+1]?.[j]  ?? 0)
              + fi   *fj    *(t.elev[i+1]?.[j+1] ?? 0);
      return -e * ex;  // exaggerated: same scale as the terrain surface
    };

    const eqTr = {
      type: 'scatter3d', mode: 'markers',
      x: es.events.map(e => e.lon), y: es.events.map(e => e.lat), z: es.events.map(e => e.depth),
      marker: { size: 2.5, color: '#facc15', line: { color: '#000', width: 0.3 } },
      hovertemplate: 'Event %{z:.1f} km<extra></extra>',
      name: 'Earthquakes', showlegend: false,
    };

    // Stations — small magenta squares sitting on the terrain surface
    const staTr = es.stations.length ? {
      type: 'scatter3d', mode: 'markers+text',
      x: es.stations.map(s => s.lon),
      y: es.stations.map(s => s.lat),
      z: es.stations.map(s => topoZ(s.lon, s.lat) - 0.15),
      text: es.stations.map(s => s.code),
      textposition: 'top center', textfont: { size: 7, color: '#f0abfc' },
      hovertemplate: '%{text}<extra></extra>',
      marker: { symbol: 'square', size: 4, color: '#d946ef',
                line: { color: '#fff', width: 0.5 } },
      name: 'Stations', showlegend: false,
    } : null;

    // Volcanoes — orange cones from this.v3d.volcanoes (loaded async by
    // loadVolcanoes(); empty array if not yet available or none in AOI).
    const loSpan = Math.max(...d.lons) - Math.min(...d.lons);
    const depSpan = es.depth_max > es.depth_min ? es.depth_max - es.depth_min : 10;
    const vols = this.v3d.volcanoes || [];
    // Cone height: 10% of depth span; minimum 5 km so the cone is always legible.
    const coneH = Math.max(5, depSpan * 0.10);
    const volPts = vols.map(v => [v.lon, v.lat, topoZ(v.lon, v.lat)]);
    const volTrs = vols.length ? _rmPyramidMesh(
      volPts, Math.max(0.015, loSpan * 0.015), coneH,
      '#d97706', 'Volcanoes', vols.map(v => v.name), true) : [];

    // Volcano name labels — shown permanently at the apex (not just on hover).
    // Positioned slightly above the apex so the text clears the cone tip.
    const volLblTr = vols.length ? {
      type: 'scatter3d', mode: 'text',
      x: vols.map(v => v.lon),
      y: vols.map(v => v.lat),
      z: vols.map(v => topoZ(v.lon, v.lat) - coneH - 1.5),
      text: vols.map(v => v.name),
      textposition: 'top center',
      textfont: { size: 10, color: '#fbbf24', family: 'Arial,sans-serif' },
      hoverinfo: 'skip', showlegend: false, name: 'Vol. names',
    } : null;

    return [...(showEvents ? [eqTr] : []), ...(staTr ? [staTr] : []), ...volTrs, ...(volLblTr ? [volLblTr] : [])];
  }

  // --- Model (topography + coastline — self-contained fetchers; do NOT touch
  //     the Result Viewer's _RM globals; this modal can be opened without a
  //     Result Viewer catalog session ever having been loaded) ---
  async loadTopo() {
    const d = this.v3d.data;
    if (!d || this.v3d.topo !== null) return;
    this.v3d.topo = undefined;  // pending
    const lo0 = Math.min(...d.lons), lo1 = Math.max(...d.lons);
    const la0 = Math.min(...d.lats), la1 = Math.max(...d.lats);
    try {
      const url = `/api/topo/grid?lon_min=${lo0}&lat_min=${la0}&lon_max=${lo1}&lat_max=${la1}&G=16`;
      const t = await (await fetch(url)).json();
      if (!t.lons || !t.lats || !t.elev) { this.v3d.topo = null; return; }
      this.v3d.topo = { lons: t.lons, lats: t.lats, elev: t.elev.map(row => row.map(v => Math.max(0, v) / 1000)) };
    } catch (_) { this.v3d.topo = null; }
    this.draw3d();
  }

  async loadCoast() {
    const d = this.v3d.data;
    if (!d || this.v3d.coast !== null) return;
    this.v3d.coast = [];
    const lo0 = Math.min(...d.lons), lo1 = Math.max(...d.lons);
    const la0 = Math.min(...d.lats), la1 = Math.max(...d.lats);
    try {
      const url = `/api/footages/coastlines?lon_min=${lo0}&lat_min=${la0}&lon_max=${lo1}&lat_max=${la1}`;
      const c = await (await fetch(url)).json();
      if (c.lines && c.lines.length) this.v3d.coast = c.lines;
    } catch (_) { }
    this.draw3d();
  }

  async loadVolcanoes() {
    const d = this.v3d.data;
    if (!d || this.v3d.volcanoes !== null) return;
    this.v3d.volcanoes = [];   // pending / empty fallback
    const loMin = Math.min(...d.lons) - 0.8, loMax = Math.max(...d.lons) + 0.8;
    const laMin = Math.min(...d.lats) - 0.8, laMax = Math.max(...d.lats) + 0.8;
    try {
      const gj = await (await fetch('/footages/Volcano.geojson')).json();
      this.v3d.volcanoes = (gj.features || [])
        .filter(f => f.geometry?.type === 'Point')
        .map(f => ({ lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1],
                     name: f.properties?.name || f.properties?.NAME || '' }))
        .filter(v => v.lon >= loMin && v.lon <= loMax && v.lat >= laMin && v.lat <= laMax);
    } catch (_) { this.v3d.volcanoes = []; }
    if (this.v3d.volcanoes.length) this.draw3d();
  }

  topoToggle() { this.draw3d(); }
  coastToggle() { this.draw3d(); }

  // Real-time opacity update via Plotly.restyle (no full redraw needed).
  // Only the field traces (volume / layer surfaces / isosurface) are restyled;
  // topo, stations, volcanoes, and coastline keep their own fixed opacity.
  opChange() {
    const val = (+document.getElementById('tomo-3d-op').value) / 100;
    const lbl = document.getElementById('tomo-3d-op-val');
    if (lbl) lbl.textContent = Math.round(val * 100) + '%';
    if (!this.v3d.init || !(this.v3d.nFieldTraces > 0)) return;
    const idxs = Array.from({length: this.v3d.nFieldTraces}, (_, i) => this.v3d.fieldTraceStart + i);
    Plotly.restyle('tomo-3d-plt', { opacity: val }, idxs);
  }

  // Vertical exaggeration so mountains (typically 1–3 km) appear at ~15% of the
  // total visual depth span.  Formula: peak*ex / (peak*ex + depMax) ≈ 15%
  // → ex ≈ 0.18 * depMax / peak  (0.15/0.85 rounded up slightly).
  exag() {
    const t = this.v3d.topo;
    const flat = t?.elev ? t.elev.flat().filter(v => isFinite(v) && v > 0) : [];
    const peak = flat.length ? Math.max(...flat) : 1;
    const depMax = this.v3d.es ? Math.max(20, this.v3d.es.depth_max) : 100;
    return Math.max(4, Math.min(50, 0.18 * depMax / peak));
  }

  topoTrace() {
    const t = this.v3d.topo;
    if (!t || !t.elev) return [];
    const ex = this.exag();
    const z  = t.elev.map(row => row.map(e => -e * ex));
    return [{
      type: 'surface', x: t.lons, y: t.lats, z,
      colorscale: [[0, '#1e3a5f'], [0.5, '#3f6212'], [0.8, '#a16207'], [1, '#e7e5e4']],
      showscale: false, opacity: 0.5,
      hovertemplate: 'Topo %{customdata:.0f} m<extra></extra>',
      customdata: t.elev.map(row => row.map(e => Math.round(e * 1000))),
      lighting: { ambient: 0.8, diffuse: 0.5 },
      name: 'Topography', showlegend: false,
    }];
  }

  coastTrace() {
    if (!this.v3d.coast || !this.v3d.coast.length) return [];
    const xs = [], ys = [], zs = [];
    for (const seg of this.v3d.coast) {
      if (!seg.length) continue;
      for (const [lo, la] of seg) { xs.push(lo); ys.push(la); zs.push(-0.05); }
      xs.push(null); ys.push(null); zs.push(null);
    }
    return [{
      type: 'scatter3d', mode: 'lines',
      x: xs, y: ys, z: zs,
      line: { color: '#38bdf8', width: 2 },
      hoverinfo: 'skip', name: 'Coastline', showlegend: false,
    }];
  }

  // One interpolated Plotly `volume` trace across the full irregular node grid
  // ("interpolasi gabungan layer" — a continuous 3D body instead of discrete
  // flat layers). Plotly accepts non-uniformly spaced x/y/z point coordinates
  // for volume/isosurface; it builds its own internal sampling grid for the
  // marching-cubes render, so the rectilinear-but-irregular SIMUL2000 node
  // spacing (denser near the center/surface) does not need pre-resampling.
  //
  // NOTE on "empty" nodes — three iterations to get here, each confirmed by
  // direct test (forced opacity 1.0, surface.count 30):
  //   1. NaN-masking individual near-zero-DWS nodes (~95% of the grid): blank.
  //      Working theory was "too sparse for marching-cubes".
  //   2. NaN-masking a much denser (~60%-kept) contiguous bounding-box crop —
  //      same full x/y/z arrays, only `value` NaN'd outside it: STILL fully
  //      blank. Proved #1's theory wrong: this Plotly build's `volume` trace
  //      breaks entirely the moment ANY NaN is in `value`, sparse or not.
  //   3. Shrinking the loop ranges (no NaN at all) fixed rendering, but Vp
  //      then filled the whole cropped bounding box as a solid prism — Vp
  //      has no "empty" value of its own, so there was nothing to carve a
  //      real contour out of; it never looked like a shaped volume, just a
  //      smaller box (unlike DWS, which is zero exactly where unilluminated).
  // Fix: use DWS coverage to carve a shape out of EITHER field — for nodes
  // with no ray coverage, substitute a sentinel far *outside* [isomin,isomax]
  // (a real finite number, never NaN) so marching-cubes draws no surface
  // there, while covered nodes keep their real value (Vp or DWS) and get
  // coloured normally. isomin/isomax are computed from the covered subset
  // only, so the sentinel never pollutes the colour/iso range. This gives Vp
  // the same illuminated-region contour as DWS, instead of a solid box.
  volumeTrace(fieldCfg, fieldName) {
    const d = this.v3d.data, es = this.v3d.es;
    const nz = d.depths.length, ny = d.lats.length, nx = d.lons.length;

    // Crop to the event/station footprint (±25% margin) first — shrinks the
    // array and keeps the sentinel trick below from carving out a shape
    // that's mostly numerical SIMUL2000 padding rather than the study area.
    let ix0 = 0, ix1 = nx - 1, iy0 = 0, iy1 = ny - 1, iz0 = 0, iz1 = nz - 1;
    if (es) {
      const loSpan = Math.max(0.05, es.lon_max - es.lon_min);
      const laSpan = Math.max(0.05, es.lat_max - es.lat_min);
      const deSpan = Math.max(5, es.depth_max - es.depth_min);
      const loLo = es.lon_min - loSpan * 0.25, loHi = es.lon_max + loSpan * 0.25;
      const laLo = es.lat_min - laSpan * 0.25, laHi = es.lat_max + laSpan * 0.25;
      const deLo = es.depth_min - deSpan * 0.25, deHi = es.depth_max + deSpan * 0.25;
      const idxRange = (arr, lo, hi) => {
        let i0 = arr.findIndex(v => v >= lo); if (i0 < 0) i0 = 0;
        let i1 = arr.length - 1 - [...arr].reverse().findIndex(v => v <= hi); if (i1 < i0) i1 = arr.length - 1;
        return [Math.max(0, i0 - 1), Math.min(arr.length - 1, i1 + 1)];  // +1 node of padding on each side
      };
      [ix0, ix1] = idxRange(d.lons, loLo, loHi);
      [iy0, iy1] = idxRange(d.lats, laLo, laHi);
      [iz0, iz1] = idxRange(d.depths, deLo, deHi);
    }

    const hasDws = !!d.dws;
    const X = [], Y = [], Z = [], V = [];
    const real = [];
    for (let iz = iz0; iz <= iz1; iz++) {
      for (let iy = iy0; iy <= iy1; iy++) {
        for (let ix = ix0; ix <= ix1; ix++) {
          X.push(d.lons[ix]); Y.push(d.lats[iy]); Z.push(d.depths[iz]);
          const covered = !hasDws || d.dws[iz][iy][ix] > 0;
          const v = fieldCfg.data[iz][iy][ix];
          V.push(covered ? v : null);   // placeholder, backfilled with the sentinel below
          if (covered) real.push(v);
        }
      }
    }
    const isomin = real.length ? Math.min(...real) : fieldCfg.min;
    const isomax = real.length ? Math.max(...real) : fieldCfg.max;
    const sentinel = isomin - (isomax - isomin || 1) - 1;  // real number, always outside [isomin,isomax]
    for (let i = 0; i < V.length; i++) if (V[i] === null) V[i] = sentinel;

    // A flat/'uniform' opacityscale with a low surface.count is what made the
    // first cropped-but-unshaped Vp attempt read as a stack of distinct flat
    // horizontal slabs: Vp here is overwhelmingly depth-driven (lateral
    // variation is small — the same reason single Vp layers looked
    // near-solid-colour earlier), so its iso-value shells ARE close to
    // horizontal planes; with few of them at one constant opacity each, gaps
    // between shells show through as banding. A graded opacityscale (shells
    // blend into each other) and more, thinner shells smooth this out.
    let opacityscale = [[0, 0.18], [0.5, 0.3], [1, 0.5]];
    let surfaceCount = 40;
    let isominFinal = isomin;
    let isomaxFinal = isomax;
    if (fieldName === 'dws') {
      // DWS is still heavily skewed toward its low end WITHIN the covered
      // subset — clip isomax to a percentile (the raw max is often one
      // outlier node right under a station) and fade the low end further so
      // the faint fringe of the illuminated region tapers off smoothly.
      const sorted = real.slice().sort((a, b) => a - b);
      isomaxFinal = sorted.length ? sorted[Math.floor(sorted.length * 0.92)] : isomax;
      opacityscale = [[0, 0.1], [0.3, 0.3], [1, 0.85]];
      surfaceCount = 30;
    } else if (fieldName === 'dv') {
      // Perturbation % is symmetric around 0.  Force symmetric isomin/isomax
      // so the RdBu centre (white/neutral) maps exactly to 0 % anomaly.
      // V-shaped opacityscale: nodes near 0 % (no anomaly) are transparent;
      // nodes near ±extremes (real anomaly) are opaque — exactly the effect
      // plot_velocity achieves via the diverging contourf hiding the background.
      const absMax = Math.max(Math.abs(isomin), Math.abs(isomax));
      isominFinal = -absMax; isomaxFinal = absMax;
      opacityscale = [[0, 0.85], [0.35, 0.08], [0.5, 0], [0.65, 0.08], [1, 0.85]];
      surfaceCount = 50;
    }
    return [{
      type: 'volume', x: X, y: Y, z: Z, value: V,
      isomin: isominFinal, isomax: isomaxFinal,
      opacity: 0.5, opacityscale, surface: { count: surfaceCount },
      colorscale: fieldCfg.colorscale, reversescale: fieldCfg.reverse,
      showscale: true, colorbar: { title: { text: fieldCfg.title }, len: 0.7 },
      caps: { x: { show: false }, y: { show: false }, z: { show: false } },
      name: fieldCfg.title, showlegend: false,
      hovertemplate: `${fieldCfg.title} %{value:${fieldCfg.fmt}}${fieldCfg.unit}<extra></extra>`,
    }];
  }

  // --- View (main 3D render dispatch) ---
  async draw3d() {
    const d = this.v3d.data;
    const plotDiv = document.getElementById('tomo-3d-plt');
    if (!d || !plotDiv) return;

    const mode = document.querySelector('input[name="tomo-3d-mode"]:checked').value;
    const opacity = (+document.getElementById('tomo-3d-op').value) / 100;
    const field = document.getElementById('tomo-3d-field').value;
    const showRays = document.getElementById('tomo-3d-rays').checked;
    const showTopo = document.getElementById('tomo-3d-topo').checked;
    const showCoast = document.getElementById('tomo-3d-coast').checked;
    const showCells = document.getElementById('tomo-3d-cells').checked;
    const showEvents = document.getElementById('tomo-3d-events').checked;
    const ny = d.lats.length, nx = d.lons.length;

    const fieldCfg =
      field === 'dws'
        ? { data: d.dws, min: d.dws_min, max: d.dws_max, colorscale: 'RdBu', reverse: false,
            title: 'DWS', unit: '', fmt: '.0f' }
      : field === 'dv'
        // Perturbation % — mirrors Simul2000Plotter.plot_velocity (matplotlib RdBu:
        // Red=slow/negative, Blue=fast/positive). Plotly's built-in "RdBu" colorscale
        // is the OPPOSITE of matplotlib's (low=blue/high=red) — reverse:true flips it
        // back to match the same Red=slow/Blue=fast convention as the 2D PNGs.
        ? { data: d.dv, min: d.dv_min, max: d.dv_max, colorscale: 'RdBu', reverse: true,
            title: 'ΔVp (%)', unit: '%', fmt: '.1f' }
        : { data: d.vp, min: d.vmin, max: d.vmax, colorscale: 'RdYlBu', reverse: true,
            title: 'Vp (km/s)', unit: ' km/s', fmt: '.2f' };

    let fieldTraces, checkedDepths;
    if (mode === 'iso') {
      fieldTraces  = this.isoTraces();
      const iso    = this.v3d.iso;
      checkedDepths = iso ? [iso.dep_min, iso.dep_max] : d.depths;
    } else if (mode === 'volume') {
      fieldTraces = this.volumeTrace(fieldCfg, field);
      checkedDepths = d.depths;
    } else {
      const layers = this.checkedLayers();
      checkedDepths = layers.map(iz => d.depths[iz]);
      fieldTraces = layers.map((iz, k) => {
        // Per-layer min/max (not the global vmin/vmax across all depths) — a
        // single shallow layer's actual Vp range is a small slice of the full
        // 0-60km range, so a global colorscale makes it look flat/homogeneous.
        // Auto-contrast each layer to its own range instead.
        // Exception: dv (perturbation %) uses a symmetric global range so that
        // the zero-anomaly point is always the same colour across all layers —
        // matching the RdBu diverging convention of plot_velocity PNGs.
        const flat = fieldCfg.data[iz].flat();
        let lmin = Math.min(...flat), lmax = Math.max(...flat);
        if (field === 'dv') {
          const absMax = Math.max(Math.abs(d.dv_min), Math.abs(d.dv_max));
          lmin = -absMax; lmax = absMax;
        }
        return {
          type: 'surface',
          x: d.lons, y: d.lats,
          z: Array(ny).fill(0).map(() => Array(nx).fill(d.depths[iz])),
          surfacecolor: fieldCfg.data[iz],
          cmin: lmin, cmax: lmax,
          colorscale: fieldCfg.colorscale, reversescale: fieldCfg.reverse,
          showscale: k === 0,
          colorbar: { title: { text: `${fieldCfg.title}<br>(layer ${d.depths[iz].toFixed(1)} km)` }, len: 0.7 },
          opacity: layers.length > 1 ? opacity : Math.max(opacity, 0.95),
          lighting: { ambient: 0.75, diffuse: 0.5 },
          name: `${d.depths[iz].toFixed(1)} km`, showlegend: false,
          hovertemplate: `Depth ${d.depths[iz].toFixed(1)} km<br>${fieldCfg.title} %{surfacecolor:${fieldCfg.fmt}}${fieldCfg.unit}<extra></extra>`,
        };
      });
    }

    // Topography is a flat `surface` trace too — in Volume mode it visually
    // reads as just another "layer" floating on top of the interpolated body
    // and hides the view into it, so it's suppressed there regardless of the
    // checkbox (which stays enabled/usable again back in Layer mode).
    const topoTraces = (showTopo && this.v3d.topo) ? this.topoTrace() : [];
    const coastTraces = (showCoast && this.v3d.coast) ? this.coastTrace() : [];
    const rayTraces = showRays ? this.rayLineTrace() : [];
    const cellTraces = showCells ? this.cellsTrace() : [];
    const esTraces = this.esTraces(showEvents);
    const traces = [...topoTraces, ...fieldTraces, ...rayTraces, ...cellTraces, ...esTraces, ...coastTraces];

    const loMin = Math.min(...d.lons), loMax = Math.max(...d.lons);
    const laMin = Math.min(...d.lats), laMax = Math.max(...d.lats);
    // Depth axis is focused on where the actual seismicity is (+ whatever
    // layers/volume are visible), not the full velocity-model grid extent —
    // the grid often reaches much deeper (damping/ray-coverage padding) than
    // any earthquake in the catalog.
    let depLo = Math.min(0, ...checkedDepths), depHi = Math.max(...checkedDepths);
    if (this.v3d.es) { depLo = Math.min(depLo, this.v3d.es.depth_min, 0); depHi = Math.max(depHi, this.v3d.es.depth_max); }
    // Grid Cells is now confined to the raypath corridor (event depth → 0 at
    // the station), so it already sits inside the event-depth range above —
    // no extra widening needed.
    // Extend top-of-scene to include exaggerated terrain (mountains go negative).
    if (showTopo && this.v3d.topo?.elev) {
      const exT = this.exag();
      const peakKm = Math.max(...this.v3d.topo.elev.flat().filter(isFinite));
      depLo = Math.min(depLo, -peakKm * exT - 2);
    }
    const depMargin = Math.max(2, (depHi - depLo) * 0.08);
    const depMin = depLo - depMargin, depMax = depHi + depMargin;

    // Track field trace indices for real-time opacity restyle (see opChange).
    this.v3d.fieldTraceStart = topoTraces.length;
    this.v3d.nFieldTraces    = fieldTraces.length;

    const layout = {
      paper_bgcolor: '#0a0f1e',
      font: { color: '#94a3b8', size: 9, family: 'Segoe UI,Arial,sans-serif' },
      margin: { l: 0, r: 0, t: 0, b: 0 },
      scene: {
        bgcolor: '#0a0f1e',
        uirevision: 'tomo3d',
        xaxis: { title: { text: 'Longitude', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#1e3a5f', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, range: [loMin, loMax] },
        yaxis: { title: { text: 'Latitude', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#1e3a5f', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, range: [laMin, laMax] },
        zaxis: { title: { text: 'Depth (km)', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#22c55e', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, range: [depMin, depMax], autorange: 'reversed' },
        camera: { eye: { x: 1.5, y: -1.5, z: 0.8 } },
        aspectmode: 'manual',
        aspectratio: this.core.geoAspect3d(loMin, loMax, laMin, laMax, depMax - depMin),
      },
      showlegend: false,
    };

    if (!this.v3d.init) {
      await Plotly.newPlot('tomo-3d-plt', traces, layout, {
        responsive: true, displaylogo: false,
        toImageButtonOptions: { filename: 'seiswork_tomo3d', scale: 2 },
      });
      this.v3d.init = true;
    } else {
      const liveScene = plotDiv.layout && plotDiv.layout.scene;
      if (liveScene && liveScene.camera) layout.scene.camera = liveScene.camera;
      await Plotly.react('tomo-3d-plt', traces, layout);
    }
  }

  // --- Controller (camera presets) ---
  cam3d(mode) {
    const cams = {
      '3d': { eye: { x: 1.5, y: -1.5, z: 0.8 } },
      'top': { eye: { x: 0.001, y: 0.001, z: 2.5 } },
      'ns': { eye: { x: 0, y: -2.5, z: 0.4 } },
      'ew': { eye: { x: 2.5, y: 0, z: 0.4 } },
    };
    try { Plotly.relayout('tomo-3d-plt', { 'scene.camera': cams[mode] }); } catch (_) { }
  }
}

const TOMO = new TomographyModule(SW);

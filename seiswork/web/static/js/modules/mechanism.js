// Focal Mechanism — SKHASH (first-motion) + FocoNet (transformer DL)
// Tabs: SKHASH | FocoNet.  Combined 2D map: SKHASH = red/white, FocoNet = blue/white.
// 3D viewer: events coloured by SKHASH quality; click = side panel.
// Station-polarity modal: map + per-station mini-seismogram.
// — by HakimBMKG
const MECH_QUALITY_COLOR = { A: '#22c55e', B: '#60a5fa', C: '#f59e0b', D: '#ef4444', F: '#6b7280' };
const MECH_OUT_FIELDS = [
  ['Strike', 'strike', '°'], ['Dip', 'dip', '°'], ['Rake', 'rake', '°'],
  ['Quality', 'quality', ''],
  ['Fault plane uncertainty', 'fault_plane_uncertainty', '°'],
  ['Aux plane uncertainty', 'aux_plane_uncertainty', '°'],
  ['N P-polarities', 'num_p_pol', ''], ['N S/P ratios', 'num_sp_ratios', ''],
  ['Azimuthal gap', 'azimuthal_gap', '°'], ['Takeoff gap', 'takeoff_gap', '°'],
  ['Polarity misfit', 'polarity_misfit', ''], ['Prob. mechanism', 'prob_mech', '%'],
  ['Station distribution ratio', 'sta_distribution_ratio', '%'],
  ['S/P misfit', 'sp_misfit', ''], ['Multiple solutions?', 'mult_solution_flag', ''],
];
const MECH_QUALITY_LABEL = {
  A: 'excellent (well constrained)',
  B: 'good',
  C: 'fair',
  D: 'poor (use with caution)',
  F: 'unreliable / insufficient constraint',
};

class MechanismModule {
  constructor(core) {
    this.core = core;
    this.jobId = null;
    this.cfgId = null;   // config ID — checklist is keyed on this (stable across re-runs)
    // SKHASH data
    this.results = [];
    this.byEvent = {};
    this.figRel = {};
    // FocoNet data
    this.fnResults = [];
    this.fnByEvent = {};
    this.fnFigRel = {};
    // shared
    this.map2d = null;
    this.markers2d = null;
    this.markers2dFn = null;
    this.v3d = { init: false };
    this._activeTab = 'skhash';   // 'skhash' | 'foconet'
    // Nodal-plane similarity filter
    this._npFilterOn  = false;
    this._npThreshold = 30;       // degrees
    // S/D/R close-match filter — per event, keep it only if SKHASH and FocoNet's
    // strike, dip AND rake are each within ±tol degrees of one another (so the
    // two beachballs actually look alike, not just "close" by an angle metric).
    this._sdrTolFilter = {
      on: false, open: false,
      tol: 5,   // degrees — max per-component difference (strike/dip/rake)
    };
    // Manual checklist selection
    this._checkedEids  = new Set();
    // Table view mode: what the table shows on load/re-render — never dump
    // every event by default. 'checklist' | 'filter' | 'all'. Resolved lazily
    // in _loadChecked() (defaults to 'checklist' if anything is checked, else 'filter').
    this._tableViewMode = null;
  }

  // ── Nodal-plane geometry helpers ────────────────────────────────────────────

  _sdrToVecs(s, d, r) {
    const phi   = s * Math.PI / 180;
    const delta = d * Math.PI / 180;
    const lam   = r * Math.PI / 180;
    // Fault normal (Aki & Richards NED convention)
    const n = [
      -Math.sin(delta) * Math.sin(phi),
       Math.sin(delta) * Math.cos(phi),
      -Math.cos(delta),
    ];
    // Slip vector (auxiliary-plane normal)
    const v = [
      Math.cos(lam) * Math.cos(phi) + Math.sin(lam) * Math.cos(delta) * Math.sin(phi),
      Math.cos(lam) * Math.sin(phi) - Math.sin(lam) * Math.cos(delta) * Math.cos(phi),
     -Math.sin(lam) * Math.sin(delta),
    ];
    return { n, v };
  }

  _vecAngleDeg(a, b) {
    const dot = a.reduce((s, ai, i) => s + ai * b[i], 0);
    return Math.acos(Math.min(1, Math.abs(dot))) * 180 / Math.PI;
  }

  /** Minimum angle (deg) between any combination of nodal planes of two mechanisms. */
  _npAngle(s1, d1, r1, s2, d2, r2) {
    if (s1 == null || s2 == null) return null;
    const m1 = this._sdrToVecs(s1, d1, r1);
    const m2 = this._sdrToVecs(s2, d2, r2);
    return Math.min(
      this._vecAngleDeg(m1.n, m2.n),
      this._vecAngleDeg(m1.n, m2.v),
      this._vecAngleDeg(m1.v, m2.n),
      this._vecAngleDeg(m1.v, m2.v),
    );
  }

  toggleNpFilter() {
    this._npFilterOn = !this._npFilterOn;
    this._renderUnifiedTable();
    this._renderMap2d();
  }

  setNpThreshold(val) {
    this._npThreshold = +val;
    document.getElementById('mech-np-thresh-label').textContent = `≤ ${val}°`;
    if (this._npFilterOn) { this._renderUnifiedTable(); this._renderMap2d(); }
  }

  // ── S/D/R close-match filter helpers ────────────────────────────────────────
  // Per event: keep it only if SKHASH and FocoNet's strike, dip AND rake are
  // each within ±tol degrees — i.e. the two beachballs actually look alike.

  toggleSdrTolPanel() {
    this._sdrTolFilter.open = !this._sdrTolFilter.open;
    this._renderUnifiedTable();
  }

  setSdrTol(val) {
    this._sdrTolFilter.tol = +val;
    const lbl = document.getElementById('mech-sdrtol-label');
    if (lbl) lbl.textContent = `± ${val}°`;
    if (this._sdrTolFilter.on) { this._renderUnifiedTable(); this._renderMap2d(); }
  }

  toggleSdrTolFilter() {
    this._sdrTolFilter.on = !this._sdrTolFilter.on;
    this._renderUnifiedTable();
    this._renderMap2d();
  }

  resetSdrTolFilter() {
    Object.assign(this._sdrTolFilter, { on: false, tol: 5 });
    this._renderUnifiedTable();
    this._renderMap2d();
  }

  /** Test if event eid's SKHASH vs FocoNet strike/dip/rake are each within ±tol°. */
  _eidSdrClose(eid) {
    const sk = this.byEvent[eid];
    const fn = this.fnByEvent[eid];
    if (!sk || !fn || sk.strike == null || fn.strike == null) return false;
    const tol = this._sdrTolFilter.tol;
    let dStrike = Math.abs(sk.strike - fn.strike) % 360;
    if (dStrike > 180) dStrike = 360 - dStrike;
    const dDip = Math.abs(sk.dip - fn.dip);
    let dRake = Math.abs(sk.rake - fn.rake) % 360;
    if (dRake > 180) dRake = 360 - dRake;
    return dStrike <= tol && dDip <= tol && dRake <= tol;
  }

  // ── Manual checklist ────────────────────────────────────────────────────────
  // Keyed on cfgId (stable across mechanism re-runs) instead of jobId (a new
  // random ID minted by the server on every run, which would orphan the
  // checklist). Falls back to jobId-based key when no cfgId is known.

  _lsKey() {
    return this.cfgId ? `mechChecked_cfg_${this.cfgId}` : `mechChecked_${this.jobId}`;
  }
  _lsKeyTableMode() {
    return this.cfgId ? `mechTableMode_cfg_${this.cfgId}` : `mechTableMode_${this.jobId}`;
  }

  _saveChecked() {
    try {
      localStorage.setItem(this._lsKey(),          JSON.stringify([...this._checkedEids]));
      localStorage.setItem(this._lsKeyTableMode(), JSON.stringify(this._tableViewMode));
    } catch {}
  }

  _loadChecked() {
    // Always reset first so switching jobs never leaks stale IDs
    this._checkedEids = new Set();
    this._tableViewMode = null;
    try {
      const raw = localStorage.getItem(this._lsKey());
      if (raw) this._checkedEids = new Set(JSON.parse(raw));
      const modeRaw = localStorage.getItem(this._lsKeyTableMode());
      if (modeRaw) this._tableViewMode = JSON.parse(modeRaw);
    } catch {}
    // Default: show the checklist if anything is checked, else show filter
    // results (which renders empty until a filter is actually turned on) —
    // never dump every event on load.
    if (this._tableViewMode == null) {
      this._tableViewMode = this._checkedEids.size > 0 ? 'checklist' : 'filter';
    }
  }

  setTableViewMode(mode) {
    this._tableViewMode = mode;
    this._saveChecked();
    this._renderUnifiedTable();
    this._renderMap2d();
  }

  /**
   * Recover a checklist saved under an old (orphaned) per-job key — happens
   * when the mechanism step is re-run and the server mints a new job_id.
   * Picks the legacy key whose saved event IDs overlap the most with the
   * events actually present in this job, so checklists from unrelated
   * configs are never pulled in by mistake.
   */
  _migrateLegacyChecklist(validEids) {
    if (this._checkedEids.size > 0) return;
    try {
      let bestKey = null, bestOverlap = 0, bestIds = null;
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || !k.startsWith('mechChecked_') || k.startsWith('mechChecked_cfg_')) continue;
        let ids;
        try { ids = JSON.parse(localStorage.getItem(k)); } catch { continue; }
        if (!Array.isArray(ids) || !ids.length) continue;
        const overlap = ids.filter(id => validEids.has(id)).length;
        if (overlap > bestOverlap) { bestOverlap = overlap; bestKey = k; bestIds = ids; }
      }
      if (bestKey && bestOverlap > 0) {
        this._checkedEids = new Set(bestIds.filter(id => validEids.has(id)));
        this._saveChecked();
        console.info(`[mechanism] Checklist restored from ${bestKey}: ${this._checkedEids.size} events`);
      }
    } catch {}
  }

  toggleCheck(eid) {
    if (this._checkedEids.has(eid)) this._checkedEids.delete(eid);
    else this._checkedEids.add(eid);
    this._saveChecked();
    // Update only the checkbox and counter without full re-render
    const cb = document.getElementById(`mech-cb-${CSS.escape(eid)}`);
    if (cb) cb.checked = this._checkedEids.has(eid);
    this._updateCheckBar();
    this._renderMap2d();
  }

  checkVisible() {
    // Check all currently visible rows
    document.querySelectorAll('[data-mech-eid]').forEach(el => {
      this._checkedEids.add(el.dataset.mechEid);
    });
    this._saveChecked();
    this._rerenderCheckboxes();
    this._updateCheckBar();
    this._renderMap2d();
  }

  uncheckAll() {
    this._checkedEids.clear();
    this._saveChecked();
    this._rerenderCheckboxes();
    this._updateCheckBar();
    this._renderMap2d();
  }

  invertVisible() {
    document.querySelectorAll('[data-mech-eid]').forEach(el => {
      const eid = el.dataset.mechEid;
      if (this._checkedEids.has(eid)) this._checkedEids.delete(eid);
      else this._checkedEids.add(eid);
    });
    this._saveChecked();
    this._rerenderCheckboxes();
    this._updateCheckBar();
    this._renderMap2d();
  }

  _rerenderCheckboxes() {
    document.querySelectorAll('[data-mech-eid]').forEach(el => {
      const cb = el.querySelector('input[type=checkbox]');
      if (cb) cb.checked = this._checkedEids.has(el.dataset.mechEid);
    });
    // Sync header checkbox
    const allVis = [...document.querySelectorAll('[data-mech-eid]')].map(e => e.dataset.mechEid);
    const hdr = document.getElementById('mech-cb-all');
    if (hdr) hdr.indeterminate = false,
              hdr.checked = allVis.length > 0 && allVis.every(e => this._checkedEids.has(e));
  }

  _updateCheckBar() {
    const n = this._checkedEids.size;
    const lbl = document.getElementById('mech-check-count');
    if (lbl) lbl.textContent = `${n} dipilih`;
    const saved = document.getElementById('mech-saved-info');
    if (saved) saved.innerHTML =
      `💾 <b style="color:${n ? '#f59e0b' : '#64748b'}">${n}</b> saved`;
  }

  // --- Controller ---
  async restoreResults() {
    if (!this.core.activeConfigId) return;
    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=mechanism&cfg_id=${this.core.activeConfigId}`)).json();
      const last = Array.isArray(jobs) && jobs.find(j => j.state === 'done');
      if (last) this.showResults(last.id, last.cfg_id);
    } catch { }
  }

  async showResults(jobId, cfgId) {
    this.jobId = jobId;
    this.cfgId = cfgId || this.core.activeConfigId || null;
    this._loadChecked();
    const card = document.getElementById('mech-results-card');
    const title = document.getElementById('mech-results-title');
    if (!card) return;
    try {
      // Load SKHASH + FocoNet in parallel
      const [results, figs, fnResults, fnFigs] = await Promise.all([
        fetch(`/api/mechanism/${jobId}/results`).then(r => r.json()),
        fetch(`/api/mechanism/${jobId}/figures`).then(r => r.json()),
        fetch(`/api/mechanism/${jobId}/foconet/results`).then(r => r.json()).catch(() => []),
        fetch(`/api/mechanism/${jobId}/foconet/figures`).then(r => r.json()).catch(() => []),
      ]);

      this.results = Array.isArray(results) ? results : [];
      this.figRel = {};
      (figs || []).forEach(f => { this.figRel[f.event_id] = f.rel; });
      this._pickBestPerEvent();

      this.fnResults = Array.isArray(fnResults) ? fnResults : [];
      this.fnFigRel = {};
      (fnFigs || []).forEach(f => { this.fnFigRel[f.event_id] = f.rel; });
      this._pickFnPerEvent();

      // Recover checklist orphaned under an old per-job key (e.g. after a re-run)
      const validEids = new Set([...Object.keys(this.byEvent), ...Object.keys(this.fnByEvent)]);
      this._migrateLegacyChecklist(validEids);
      // Re-resolve default view mode if migration just populated an empty checklist
      if (this._checkedEids.size > 0 && this._tableViewMode === 'filter') {
        this._tableViewMode = 'checklist';
      }

      card.style.display = '';
      const nFn = Object.keys(this.fnByEvent).length;
      if (title) title.textContent =
        `job ${jobId} · SKHASH: ${this.results.length} sol / ${Object.keys(this.byEvent).length} events` +
        (nFn ? `  ·  FocoNet: ${nFn} events` : '');

      this._renderUnifiedTable();
      await this._renderMap2d();
    } catch (e) {
      if (title) title.textContent = `Failed to load: ${this.core.esc(e.message)}`;
      card.style.display = '';
    }
  }

  // --- Model helpers ---
  _pickBestPerEvent() {
    this.byEvent = {};
    const rank = { A: 0, B: 1, C: 2, D: 3, F: 4 };
    for (const r of this.results) {
      const cur = this.byEvent[r.event_id];
      if (!cur) { this.byEvent[r.event_id] = r; continue; }
      const a = rank[r.quality] ?? 9, b = rank[cur.quality] ?? 9;
      if (a < b || (a === b && (r.prob_mech || 0) > (cur.prob_mech || 0)))
        this.byEvent[r.event_id] = r;
    }
  }

  _pickFnPerEvent() {
    this.fnByEvent = {};
    for (const r of this.fnResults) {
      this.fnByEvent[r.event_id] = r;  // FocoNet: one row per event
    }
  }

  // ══════════════════════════════════════════
  // Unified table: SKHASH + FocoNet per event, inline beachball thumbnails
  // ══════════════════════════════════════════

  /**
   * Single source of truth for "what's currently visible" — shared by the
   * table and the 2D map so they never disagree about what the user selected.
   */
  _computeVisibleEids() {
    const allEids = [...new Set([
      ...Object.keys(this.byEvent),
      ...Object.keys(this.fnByEvent),
    ])].sort();

    const npAngles = {};
    for (const eid of allEids) {
      const sk = this.byEvent[eid];
      const fn = this.fnByEvent[eid];
      if (sk && fn) {
        npAngles[eid] = this._npAngle(sk.strike, sk.dip, sk.rake,
                                       fn.strike, fn.dip, fn.rake);
      }
    }

    // ── Filter pipeline — each layer applied cumulatively ───────────────────
    const layer0 = allEids;                                          // all events
    const layer1 = this._npFilterOn                                  // Δ° filter
      ? layer0.filter(eid => { const a = npAngles[eid]; return a != null && a <= this._npThreshold; })
      : layer0;
    const layer2 = this._sdrTolFilter.on                              // S/D/R close-match
      ? layer1.filter(eid => this._eidSdrClose(eid))
      : layer1;

    // ── View mode — decides which of layer2 actually gets shown.
    // Never dump every event by default: only 'all' does that, explicitly.
    const filterActive = this._npFilterOn || this._sdrTolFilter.on;
    const viewMode = this._tableViewMode;
    const eids = viewMode === 'checklist' ? layer2.filter(eid => this._checkedEids.has(eid))
               : viewMode === 'all'       ? layer2
               : /* 'filter' */             (filterActive ? layer2 : []);

    return { allEids, npAngles, layer0, layer1, layer2, eids, filterActive, viewMode };
  }

  _renderUnifiedTable() {
    const wrap = document.getElementById('mech-table-wrap');
    if (!wrap) return;

    const { allEids, npAngles, layer0, layer1, layer2, eids, viewMode } = this._computeVisibleEids();

    if (!allEids.length) {
      wrap.innerHTML = `<div class="pick-term-placeholder">No focal mechanism results.</div>`;
      return;
    }

    const nBoth    = layer0.filter(eid => npAngles[eid] != null).length;
    const nSimilar = layer1.length;   // count after Δ° filter (for badge)

    const emptyMsg = viewMode === 'checklist'
      ? 'No events checked yet.'
      : viewMode === 'all'
      ? 'No events.'
      : 'No active filter (Δ° / S/D/R). Enable a filter, or pick another mode above.';

    const rows = eids.length ? eids.map(eid => {
      const sk = this.byEvent[eid];
      const fn = this.fnByEvent[eid];
      const skRel = this.figRel[eid];
      const fnRel = this.fnFigRel[eid];
      const skColor  = MECH_QUALITY_COLOR[sk?.quality] || '#94a3b8';
      const fnMethod = fn?.method || 'FocoNet';

      let skImg;
      if (skRel) {
        const url = `/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(skRel)}`;
        skImg = `<img class="mech-thumb" src="${url}" loading="lazy" onclick="MECH.openStationModal('${this.core.esc(eid)}')"
          style="width:52px;height:52px;border-radius:3px;border:2px solid ${skColor};background:#fff;cursor:pointer;display:block" title="SKHASH Q${sk?.quality||'–'}"/>`;
      } else if (sk) {
        skImg = `<div class="mech-thumb" onclick="MECH.openStationModal('${this.core.esc(eid)}')"
          style="width:52px;height:52px;border-radius:3px;border:2px solid ${skColor};background:#0f172a;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.5rem;color:${skColor}">SK</div>`;
      } else {
        skImg = `<span style="color:#334155;font-size:.7rem">–</span>`;
      }

      let fnImg;
      if (fnRel) {
        const url = `/api/mechanism/${this.jobId}/foconet/figure?name=${encodeURIComponent(fnRel)}`;
        fnImg = `<img class="mech-thumb" src="${url}" loading="lazy" onclick="MECH.openFnStationModal('${this.core.esc(eid)}')"
          style="width:52px;height:52px;border-radius:3px;border:2px solid #1e40af;background:#fff;cursor:pointer;display:block" title="${this.core.esc(fnMethod)}"/>`;
      } else if (fn) {
        fnImg = `<div class="mech-thumb" onclick="MECH.openFnStationModal('${this.core.esc(eid)}')"
          style="width:52px;height:52px;border-radius:3px;border:2px solid #1e40af;background:#0f172a;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.5rem;color:#60a5fa">FN</div>`;
      } else {
        fnImg = `<span style="color:#334155;font-size:.7rem">–</span>`;
      }

      const skSdr = sk ? `${this.core.fmt(sk.strike,0)}/${this.core.fmt(sk.dip,0)}/${this.core.fmt(sk.rake,0)}` : '–';
      const fnSdr = fn ? `${this.core.fmt(fn.strike,0)}/${this.core.fmt(fn.dip,0)}/${this.core.fmt(fn.rake,0)}` : '–';
      const skQ   = sk ? `<span style="color:${skColor};font-weight:700">${this.core.esc(sk.quality||'–')}</span>` : '–';
      const mag   = sk?.magnitude ?? fn?.magnitude;
      const depth = sk?.origin_depth_km ?? fn?.origin_depth_km;

      // Nodal-plane angle badge
      const ang = npAngles[eid];
      let angCell;
      if (ang == null) {
        angCell = `<span style="color:#334155;font-size:.65rem">–</span>`;
      } else {
        const angColor = ang <= 20  ? '#22c55e'
                       : ang <= 35  ? '#86efac'
                       : ang <= 50  ? '#f59e0b'
                       : ang <= 70  ? '#ef4444'
                       :              '#6b7280';
        angCell = `<span style="color:${angColor};font-weight:700;font-size:.65rem" title="Min nodal-plane angle SKHASH vs ${fnMethod}">${ang.toFixed(0)}°</span>`;
      }

      const isChecked = this._checkedEids.has(eid);
      const rowBg = isChecked ? 'background:rgba(245,158,11,.07)' : '';
      return `<tr class="mech-row" data-mech-eid="${this.core.esc(eid)}" style="vertical-align:middle;${rowBg}">
        <td style="padding:.15rem .3rem;text-align:center" onclick="event.stopPropagation()">
          <input type="checkbox" id="mech-cb-${this.core.esc(eid)}"
            ${isChecked ? 'checked' : ''}
            onchange="MECH.toggleCheck('${this.core.esc(eid)}')"
            style="accent-color:#f59e0b;cursor:pointer;width:13px;height:13px">
        </td>
        <td style="padding:.2rem .4rem .2rem 0;white-space:nowrap;font-size:.6rem">${this.core.esc(eid)}</td>
        <td style="padding:.15rem .25rem">${skImg}</td>
        <td style="padding:.15rem .3rem;white-space:nowrap">${skSdr}</td>
        <td style="padding:.15rem .3rem">${skQ}</td>
        <td style="padding:.15rem .3rem;color:#94a3b8">${sk?.num_p_pol ?? '–'}</td>
        <td style="padding:.15rem .4rem;color:#94a3b8">${sk ? this.core.fmt(sk.azimuthal_gap,0) : '–'}</td>
        <td style="padding:.15rem .25rem;padding-left:.5rem">${fnImg}</td>
        <td style="padding:.15rem .3rem;white-space:nowrap">${fnSdr}</td>
        <td style="padding:.15rem .4rem;color:#94a3b8">${fn?.n_pol ?? '–'}</td>
        <td style="padding:.15rem .3rem;text-align:center">${angCell}</td>
        <td style="padding:.15rem .3rem;color:#64748b">${mag != null ? 'M'+this.core.fmt(mag,1) : '–'}</td>
        <td style="padding:.15rem .3rem;color:#64748b">${depth != null ? this.core.fmt(depth,1)+' km' : '–'}</td>
      </tr>`;
    }).join('') : `<tr><td colspan="13" style="padding:1rem;text-align:center;color:#64748b;font-size:.65rem">
      ${emptyMsg}
    </td></tr>`;

    const qLeg = Object.entries(MECH_QUALITY_COLOR).map(([q, c]) =>
      `<span style="margin-right:.8rem"><span style="color:${c};font-weight:700">${q}</span> ${MECH_QUALITY_LABEL[q]}</span>`
    ).join('');

    const fnMethodName  = this.core.esc(Object.values(this.fnByEvent)[0]?.method || 'FocoNet');
    const npOn          = this._npFilterOn;
    const thresh        = this._npThreshold;
    const tf            = this._sdrTolFilter;
    const nChecked      = this._checkedEids.size;
    const allVisChecked = eids.length > 0 && eids.every(e => this._checkedEids.has(e));
    const nClose        = layer1.filter(eid => this._eidSdrClose(eid)).length;

    wrap.innerHTML = `
      <div style="background:#0d1526;border:1px solid #1e293b;border-radius:6px;padding:.4rem .6rem;margin-bottom:.45rem">

        <!-- Row 0: table view mode — what gets rendered below -->
        <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.4rem;
                    border-bottom:1px solid #1e293b;padding-bottom:.4rem">
          <span style="font-size:.6rem;color:#64748b">Show:</span>
          <button onclick="MECH.setTableViewMode('checklist')"
            style="font-size:.6rem;padding:.18rem .5rem;border-radius:4px;cursor:pointer;
                   border:1px solid ${viewMode==='checklist' ? '#f59e0b' : '#334155'};
                   background:${viewMode==='checklist' ? 'rgba(245,158,11,.15)' : 'transparent'};
                   color:${viewMode==='checklist' ? '#f59e0b' : '#94a3b8'}">
            ☑ Checklist (${nChecked})
          </button>
          <button onclick="MECH.setTableViewMode('filter')"
            style="font-size:.6rem;padding:.18rem .5rem;border-radius:4px;cursor:pointer;
                   border:1px solid ${viewMode==='filter' ? '#22c55e' : '#334155'};
                   background:${viewMode==='filter' ? 'rgba(34,197,94,.15)' : 'transparent'};
                   color:${viewMode==='filter' ? '#22c55e' : '#94a3b8'}">
            ▼ Filter Results
          </button>
          <button onclick="MECH.setTableViewMode('all')"
            style="font-size:.6rem;padding:.18rem .5rem;border-radius:4px;cursor:pointer;
                   border:1px solid ${viewMode==='all' ? '#60a5fa' : '#334155'};
                   background:${viewMode==='all' ? 'rgba(96,165,250,.15)' : 'transparent'};
                   color:${viewMode==='all' ? '#60a5fa' : '#94a3b8'}">
            ▦ All (${allEids.length})
          </button>
          <span style="font-size:.59rem;color:#475569;margin-left:auto">
            <b style="color:#94a3b8">${eids.length}</b> rows shown
          </span>
        </div>

        <!-- Row 1: Δ° filter -->
        <div style="display:flex;align-items:center;gap:.55rem;flex-wrap:wrap">
          <button onclick="MECH.toggleNpFilter()"
            style="font-size:.63rem;padding:.22rem .55rem;border-radius:4px;border:1px solid ${npOn ? '#22c55e' : '#334155'};
                   background:${npOn ? 'rgba(34,197,94,.12)' : 'transparent'};color:${npOn ? '#22c55e' : '#94a3b8'};cursor:pointer;white-space:nowrap">
            ${npOn ? '✓' : '○'} Δ° Mirip
          </button>
          <div style="display:flex;align-items:center;gap:.3rem">
            <input id="mech-np-slider" type="range" min="5" max="80" value="${thresh}"
              oninput="MECH.setNpThreshold(this.value)"
              style="width:80px;accent-color:#22c55e;cursor:pointer">
            <span id="mech-np-thresh-label" style="font-size:.62rem;color:#22c55e;min-width:2.8rem">≤ ${thresh}°</span>
          </div>
          <span style="font-size:.6rem;color:#64748b">${nSimilar}/${nBoth} mirip</span>
          <span style="font-size:.59rem;color:#475569;margin-left:auto">
            <span style="color:#22c55e">■</span>&lt;20
            <span style="color:#86efac;margin-left:.3rem">■</span>20–35
            <span style="color:#f59e0b;margin-left:.3rem">■</span>35–50
            <span style="color:#ef4444;margin-left:.3rem">■</span>&gt;50°
          </span>
        </div>

        <!-- Row 2: S/D/R close-match filter header -->
        <div style="display:flex;align-items:center;gap:.55rem;margin-top:.3rem;flex-wrap:wrap">
          <button onclick="MECH.toggleSdrTolPanel()"
            style="font-size:.63rem;padding:.22rem .55rem;border-radius:4px;border:1px solid ${tf.on ? '#f59e0b' : '#334155'};
                   background:${tf.on ? 'rgba(245,158,11,.12)' : 'transparent'};color:${tf.on ? '#f59e0b' : '#94a3b8'};cursor:pointer;white-space:nowrap">
            ${tf.on ? '✓' : '○'} S/D/R Hampir Sama ${tf.open ? '▲' : '▼'}
          </button>
          <div style="display:flex;align-items:center;gap:.3rem">
            <input id="mech-sdrtol-slider" type="range" min="1" max="30" value="${tf.tol}"
              oninput="MECH.setSdrTol(this.value)"
              style="width:80px;accent-color:#f59e0b;cursor:pointer">
            <span id="mech-sdrtol-label" style="font-size:.62rem;color:#f59e0b;min-width:2.8rem">± ${tf.tol}°</span>
          </div>
          <span style="font-size:.6rem;color:#64748b">${nClose}/${nBoth} cocok</span>
          ${tf.open ? `
          <button onclick="MECH.toggleSdrTolFilter()"
            style="font-size:.6rem;padding:.15rem .45rem;border-radius:3px;cursor:pointer;margin-left:.2rem;
                   border:1px solid ${tf.on ? '#f59e0b' : '#475569'};
                   background:${tf.on ? 'rgba(245,158,11,.18)' : 'transparent'};
                   color:${tf.on ? '#f59e0b' : '#94a3b8'}">
            ${tf.on ? 'Nonaktifkan' : 'Terapkan'}
          </button>
          <button onclick="MECH.resetSdrTolFilter()"
            style="font-size:.6rem;padding:.15rem .45rem;border-radius:3px;cursor:pointer;
                   border:1px solid #334155;background:transparent;color:#64748b">Reset</button>
          ` : ''}
        </div>

        <!-- S/D/R close-match panel (collapsible): direct strike/dip/rake comparison -->
        ${tf.open ? `
        <div style="margin-top:.35rem;padding:.35rem .4rem;background:#0a1020;border-radius:4px;border:1px solid #1e293b">
          <div style="font-size:.58rem;color:#475569">
            Per event, SKHASH and ${fnMethodName} pass only when the strike, dip, AND rake difference
            masing-masing ≤ ±${tf.tol}° — sehingga kedua bola fokal terlihat hampir identik.
          </div>
        </div>` : ''}

        <!-- Filter pipeline breadcrumb -->
        <div style="display:flex;align-items:center;gap:.3rem;margin-top:.35rem;flex-wrap:wrap;font-size:.6rem">
          <!-- Layer 0: total -->
          <span style="color:#475569;padding:.15rem .4rem;border-radius:3px;border:1px solid #1e293b;background:#0a1020">
            All <b style="color:#94a3b8">${layer0.length}</b>
          </span>
          ${this._npFilterOn ? `
          <span style="color:#64748b">→</span>
          <span onclick="MECH.toggleNpFilter()" title="Click to disable"
            style="color:#22c55e;padding:.15rem .5rem;border-radius:3px;border:1px solid #22c55e;
                   background:rgba(34,197,94,.08);cursor:pointer;white-space:nowrap">
            Δ°≤${this._npThreshold} <b>${layer1.length}</b> ✕
          </span>` : ''}
          ${this._sdrTolFilter.on ? `
          <span style="color:#64748b">→</span>
          <span onclick="MECH.toggleSdrTolFilter()" title="Click to disable"
            style="color:#f59e0b;padding:.15rem .5rem;border-radius:3px;border:1px solid #f59e0b;
                   background:rgba(245,158,11,.08);cursor:pointer;white-space:nowrap">
            S/D/R ±${this._sdrTolFilter.tol}° <b>${layer2.length}</b> ✕
          </span>` : ''}
          ${!this._npFilterOn && !this._sdrTolFilter.on ? `
          <span style="color:#64748b">→</span>
          <span style="color:#334155;font-style:italic">aktifkan filter di atas</span>` : ''}
          ${(this._npFilterOn || this._sdrTolFilter.on) ? `
          <span style="color:#64748b;margin-left:.2rem">= <b style="color:#e2e8f0">${eids.length}</b> events shown</span>` : ''}
        </div>

        <!-- Summary & checklist bar -->
        <div style="display:flex;align-items:center;gap:.55rem;margin-top:.3rem;flex-wrap:wrap;
                    border-top:1px solid #1e293b;padding-top:.3rem">
          <span style="font-size:.6rem;color:#94a3b8">
            <span style="color:#e2e8f0;font-weight:600">${eids.length}</span>/${allEids.length} terlihat
          </span>
          <div style="width:1px;height:12px;background:#1e293b"></div>
          <!-- Checklist controls -->
          <span id="mech-check-count" style="font-size:.6rem;color:${nChecked ? '#f59e0b' : '#64748b'};font-weight:${nChecked ? '600' : '400'}">${nChecked} dipilih</span>
          <button onclick="MECH.checkVisible()"
            style="font-size:.6rem;padding:.15rem .4rem;border-radius:3px;border:1px solid #334155;
                   background:transparent;color:#94a3b8;cursor:pointer" title="Check all visible rows">
            ☑ Select Visible
          </button>
          <button onclick="MECH.invertVisible()"
            style="font-size:.6rem;padding:.15rem .4rem;border-radius:3px;border:1px solid #334155;
                   background:transparent;color:#94a3b8;cursor:pointer" title="Balik pilihan">
            ⇄ Balik
          </button>
          <button onclick="MECH.uncheckAll()"
            style="font-size:.6rem;padding:.15rem .4rem;border-radius:3px;border:1px solid #334155;
                   background:transparent;color:#64748b;cursor:pointer">
            ✕ Clear All
          </button>
          <span id="mech-saved-info" style="font-size:.6rem;color:#64748b"
            title="Checklist is saved automatically in the browser">
            💾 <b style="color:${nChecked ? '#f59e0b' : '#64748b'}">${nChecked}</b> saved
          </span>
          <span style="font-size:.59rem;color:#475569;margin-left:auto" title="The map always follows the rows currently shown in the table">
            🗺 Map mengikuti tabel (${eids.length})
          </span>
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="text-align:left;font-size:.58rem">
            <th rowspan="2" style="padding:.2rem .3rem;text-align:center">
              <input id="mech-cb-all" type="checkbox" ${allVisChecked ? 'checked' : ''}
                onchange="this.checked ? MECH.checkVisible() : MECH.uncheckAll()"
                style="accent-color:#f59e0b;cursor:pointer;width:13px;height:13px"
                title="Select/clear all visible">
            </th>
            <th rowspan="2" style="padding:.2rem .4rem .2rem 0;color:var(--text-muted)">Event</th>
            <th colspan="5" style="padding:.18rem .4rem;background:rgba(220,38,38,.08);color:#f87171;border-bottom:2px solid #dc2626">SKHASH</th>
            <th colspan="3" style="padding:.18rem .4rem;background:rgba(30,64,175,.10);color:#60a5fa;border-bottom:2px solid #1e40af;padding-left:.5rem">${fnMethodName}</th>
            <th rowspan="2" style="padding:.2rem .3rem;color:#22c55e;text-align:center" title="Min angle between any nodal plane pair (SKHASH vs ${fnMethodName})">Δ°</th>
            <th rowspan="2" style="padding:.2rem .3rem;color:var(--text-muted)">Mag</th>
            <th rowspan="2" style="padding:.2rem .3rem;color:var(--text-muted)">Depth</th>
          </tr>
          <tr style="color:var(--text-muted);text-align:left;font-size:.56rem">
            <th style="padding:.1rem .25rem"></th>
            <th style="padding:.1rem .3rem">S/D/R</th>
            <th style="padding:.1rem .3rem">Q</th>
            <th style="padding:.1rem .3rem">n<sub>pol</sub></th>
            <th style="padding:.1rem .4rem">Gap°</th>
            <th style="padding:.1rem .25rem;padding-left:.5rem"></th>
            <th style="padding:.1rem .3rem">S/D/R</th>
            <th style="padding:.1rem .4rem">n<sub>pol</sub></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <div style="margin-top:.4rem;font-size:.61rem;color:var(--text-muted)">
        SKHASH Q: ${qLeg} · Δ° = minimum angle between an SKHASH nodal plane and ${fnMethodName} · Click a thumbnail for detail
      </div>`;
  }

  // --- Tab rendering (kept for backward compat, no longer called) ---
  _renderTabs() {
    const wrap = document.getElementById('mech-tabs');
    if (!wrap) return;
    const tabs = [
      { key: 'skhash',  label: `SKHASH <span style="font-size:.6rem;color:#94a3b8">(${Object.keys(this.byEvent).length} events)</span>` },
      { key: 'foconet', label: `FocoNet <span style="font-size:.6rem;color:#94a3b8">(${Object.keys(this.fnByEvent).length} events)</span>` },
      { key: 'combined', label: 'Both on Map' },
    ];
    wrap.innerHTML = tabs.map(t =>
      `<button class="sw-tab-btn ${this._activeTab === t.key ? 'active' : ''}"
         onclick="MECH.switchTab('${t.key}')">${t.label}</button>`
    ).join('');
  }

  switchTab(key) {
    this._activeTab = key;
    this._renderTabs();
    this._renderTabContent(key);
    if (key === 'combined') this._renderCombinedMap();
    else this._renderMap2d();
  }

  _renderTabContent(key) {
    const sk = document.getElementById('mech-tab-skhash');
    const fn = document.getElementById('mech-tab-foconet');
    if (!sk || !fn) return;
    if (key === 'skhash' || key === 'combined') {
      sk.style.display = '';
      fn.style.display = 'none';
      if (key === 'skhash') { this._renderTable(); this._renderGallery(); }
    } else {
      sk.style.display = 'none';
      fn.style.display = '';
      this._renderFnTable();
      this._renderFnGallery();
    }
  }

  // ══════════════════════════════════════════
  // SKHASH tab
  // ══════════════════════════════════════════

  _renderTable() {
    const wrap = document.getElementById('mech-table-wrap');
    if (!wrap) return;
    if (!this.results.length) {
      wrap.innerHTML = `<div class="pick-term-placeholder">No mechanisms met the quality criteria — try relaxing max azimuthal gap or min polarities.</div>`;
      return;
    }
    const rows = this.results.map(r => {
      const color = MECH_QUALITY_COLOR[r.quality] || '#94a3b8';
      return `<tr class="mech-row" onclick="MECH.openStationModal('${this.core.esc(r.event_id)}')" style="cursor:pointer">
        <td>${this.core.esc(r.event_id)}</td>
        <td>${this.core.fmt(r.strike, 0)}</td>
        <td>${this.core.fmt(r.dip, 0)}</td>
        <td>${this.core.fmt(r.rake, 0)}</td>
        <td><span style="color:${color};font-weight:700">${this.core.esc(r.quality || '–')}</span></td>
        <td>${r.num_p_pol ?? '–'}</td>
        <td>${this.core.fmt(r.azimuthal_gap, 0)}</td>
      </tr>`;
    }).join('');
    const legend = Object.entries(MECH_QUALITY_COLOR).map(([q, c]) =>
      `<span style="margin-right:.9rem"><span style="color:${c};font-weight:700">${q}</span> ${MECH_QUALITY_LABEL[q]}</span>`
    ).join('');
    wrap.innerHTML = `<table style="width:100%;border-collapse:collapse">
      <thead><tr style="color:var(--text-muted);text-align:left">
        <th>Event</th><th>Strike</th><th>Dip</th><th>Rake</th>
        <th title="Quality grade A-F">Q</th><th>n_pol</th><th>Gap°</th>
      </tr></thead>
      <tbody>${rows}</tbody></table>
      <div style="margin-top:.5rem;font-size:.68rem;color:var(--text-muted)">Q: ${legend}</div>`;
  }

  _renderGallery() {
    const gal = document.getElementById('mech-gallery');
    if (!gal) return;
    const entries = Object.entries(this.figRel);
    if (!entries.length) {
      gal.innerHTML = `<div class="pick-term-placeholder">No beachball plots produced.</div>`;
      return;
    }
    const legend = Object.entries(MECH_QUALITY_COLOR).map(([q, c]) =>
      `<span style="margin-right:.9rem"><span style="color:${c};font-weight:700">${q}</span> ${MECH_QUALITY_LABEL[q]}</span>`
    ).join('');
    gal.innerHTML = `<div style="margin-bottom:.5rem;font-size:.68rem;color:var(--text-muted)">Border color = quality grade: ${legend}</div>` +
      `<div style="display:flex;flex-wrap:wrap;gap:.4rem">` + entries.map(([eid, rel]) => {
        const r = this.byEvent[eid];
        const q = r?.quality || '–';
        const color = MECH_QUALITY_COLOR[r?.quality] || '#94a3b8';
        const label = MECH_QUALITY_LABEL[r?.quality] || 'unknown';
        const url = `/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(rel)}`;
        return `<div style="display:block;width:96px;text-align:center">
          <img src="${url}" onclick="MECH.openStationModal('${this.core.esc(eid)}')"
               title="SKHASH · Q${this.core.esc(q)}: ${this.core.esc(label)}"
               style="width:90px;height:90px;border-radius:4px;border:2px solid ${color};background:#fff;cursor:pointer" loading="lazy"/>
          <a href="${url}" target="_blank" style="font-size:.58rem;color:var(--text-muted);margin-top:2px;text-decoration:none;display:block">${this.core.esc(eid)}</a>
        </div>`;
      }).join('') + `</div>`;
  }

  // ══════════════════════════════════════════
  // FocoNet tab
  // ══════════════════════════════════════════

  _renderFnTable() {
    const wrap = document.getElementById('mech-fn-table-wrap');
    if (!wrap) return;
    if (!this.fnResults.length) {
      wrap.innerHTML = `<div class="pick-term-placeholder">FocoNet has not run yet or produced no output for this job.</div>`;
      return;
    }
    const rows = this.fnResults.map(r =>
      `<tr class="mech-row" onclick="MECH.openFnStationModal('${this.core.esc(r.event_id)}')" style="cursor:pointer">
        <td>${this.core.esc(r.event_id)}</td>
        <td>${this.core.fmt(r.strike, 0)}</td>
        <td>${this.core.fmt(r.dip, 0)}</td>
        <td>${this.core.fmt(r.rake, 0)}</td>
        <td>${r.n_pol ?? '–'}</td>
        <td style="color:#60a5fa;font-size:.6rem">${this.core.esc(r.method || 'FocoNet_O')}</td>
      </tr>`
    ).join('');
    wrap.innerHTML = `<table style="width:100%;border-collapse:collapse">
      <thead><tr style="color:var(--text-muted);text-align:left">
        <th>Event</th><th>Strike</th><th>Dip</th><th>Rake</th><th>n_pol</th><th>Method</th>
      </tr></thead>
      <tbody>${rows}</tbody></table>
      <div style="margin-top:.4rem;font-size:.66rem;color:var(--text-muted)">${this.fnResults[0]?.method || 'FocoNet'}: transformer neural network — P first-motion + S/P amplitude ratios (Song et al., 2026). No quality grade — single best solution per event.</div>`;
  }

  _renderFnGallery() {
    const gal = document.getElementById('mech-fn-gallery');
    if (!gal) return;
    const entries = Object.entries(this.fnFigRel);
    if (!entries.length) {
      gal.innerHTML = `<div class="pick-term-placeholder">No FocoNet beachball plots produced.</div>`;
      return;
    }
    gal.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:.4rem">` +
      entries.map(([eid, rel]) => {
        const url = `/api/mechanism/${this.jobId}/foconet/figure?name=${encodeURIComponent(rel)}`;
        const r = this.fnByEvent[eid];
        return `<div style="display:block;width:96px;text-align:center">
          <img src="${url}" onclick="MECH.openFnStationModal('${this.core.esc(eid)}')"
               title="FocoNet · S/D/R ${this.core.fmt(r?.strike,0)}/${this.core.fmt(r?.dip,0)}/${this.core.fmt(r?.rake,0)}"
               style="width:90px;height:90px;border-radius:4px;border:2px solid #1e40af;background:#fff;cursor:pointer" loading="lazy"/>
          <a href="${url}" target="_blank" style="font-size:.58rem;color:var(--text-muted);margin-top:2px;text-decoration:none;display:block">${this.core.esc(eid)}</a>
        </div>`;
      }).join('') + `</div>`;
  }

  // ══════════════════════════════════════════
  // 2D Leaflet map (shared, combined mode shows both layers)
  // ══════════════════════════════════════════

  async _ensureMap2d() {
    if (this.map2d) return;
    const el = document.getElementById('mech-map2d');
    if (!el || typeof L === 'undefined') return;
    this.map2d = L.map(el, { center: [1.05, 127.5], zoom: 10, zoomControl: true });
    L.tileLayer('/tiles/carto_dark/{z}/{x}/{y}.png',
      { attribution: '© CartoDB', maxZoom: 19 }).addTo(this.map2d);
    this.markers2d    = L.layerGroup().addTo(this.map2d);
    this.markers2dFn  = L.layerGroup().addTo(this.map2d);
    this.stationLines = L.layerGroup().addTo(this.map2d);  // station lines per event
    this.faultLayer   = null;
    this._loadFaultLayer();
  }

  async _loadFaultLayer() {
    if (!this.map2d || this.faultLayer) return;
    try {
      const fd = await (await fetch('/footages/indogigis.geojson')).json();
      const FAULT_STYLE = window.FAULT_STYLE || {};
      const FAULT_DEFAULT = window.FAULT_DEFAULT || { color: '#f97316', weight: 1.2, opacity: 0.65 };
      this.faultLayer = L.geoJSON(fd, {
        style: feat => (FAULT_STYLE[feat.properties?.tipe || ''] || FAULT_DEFAULT),
      }).addTo(this.map2d);
    } catch (_) {}
  }

  async _renderMap2d() {
    await this._ensureMap2d();
    if (!this.map2d) return;
    this.markers2d.clearLayers();
    this.markers2dFn.clearLayers();
    this.stationLines.clearLayers();

    // Map always mirrors the table's current view (checklist / filter / all)
    // — no separate toggle, so the two never disagree about what's selected.
    const visible = new Set(this._computeVisibleEids().eids);

    const pts = [];

    {
      for (const [eid, r] of Object.entries(this.byEvent)) {
        if (r.origin_lat == null || r.origin_lon == null) continue;
        if (!visible.has(eid)) continue;
        pts.push([r.origin_lat, r.origin_lon]);
        const rel   = this.figRel[eid];
        const color = MECH_QUALITY_COLOR[r.quality] || '#94a3b8';
        let marker;
        if (rel) {
          const url = `/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(rel)}`;
          const icon = L.divIcon({ html: `<img class="mech-map-thumb" src="${url}">`,
            iconSize: [30, 30], iconAnchor: [15, 15], className: 'mech-beachball-divicon' });
          marker = L.marker([r.origin_lat, r.origin_lon], { icon });
        } else {
          marker = L.circleMarker([r.origin_lat, r.origin_lon],
            { radius: 6, color, fillColor: color, fillOpacity: .8, weight: 1 });
        }
        marker.bindPopup(
          `<b>${this.core.esc(eid)}</b> <span style="color:#dc2626;font-size:.65rem">SKHASH</span><br>` +
          `S/D/R: ${this.core.fmt(r.strike,0)}/${this.core.fmt(r.dip,0)}/${this.core.fmt(r.rake,0)}<br>` +
          `Q: <b style="color:${color}">${this.core.esc(r.quality||'–')}</b> · n_pol=${r.num_p_pol??'–'} · gap=${this.core.fmt(r.azimuthal_gap,0)}°` +
          (r.magnitude != null ? `<br>M${this.core.fmt(r.magnitude,2)} · depth ${this.core.fmt(r.origin_depth_km,1)} km` : ''));
        marker.on('popupopen', () => this._drawStationLines(eid, r.origin_lat, r.origin_lon, 'skhash'));
        marker.on('popupclose', () => this.stationLines.clearLayers());
        marker.addTo(this.markers2d);
      }
    }

    {
      for (const [eid, r] of Object.entries(this.fnByEvent)) {
        if (r.origin_lat == null || r.origin_lon == null) continue;
        if (!visible.has(eid)) continue;
        pts.push([r.origin_lat, r.origin_lon]);
        const rel = this.fnFigRel[eid];
        let marker;
        if (rel) {
          const url = `/api/mechanism/${this.jobId}/foconet/figure?name=${encodeURIComponent(rel)}`;
          const icon = L.divIcon({ html: `<img class="mech-map-thumb" src="${url}">`,
            iconSize: [30, 30], iconAnchor: [15, 15], className: 'mech-beachball-divicon' });
          marker = L.marker([r.origin_lat, r.origin_lon], { icon });
        } else {
          marker = L.circleMarker([r.origin_lat, r.origin_lon],
            { radius: 6, color: '#1e40af', fillColor: '#1e40af', fillOpacity: .8, weight: 1 });
        }
        marker.bindPopup(
          `<b>${this.core.esc(eid)}</b> <span style="color:#1e40af;font-size:.65rem">FocoNet</span><br>` +
          `S/D/R: ${this.core.fmt(r.strike,0)}/${this.core.fmt(r.dip,0)}/${this.core.fmt(r.rake,0)}<br>` +
          `n_pol=${r.n_pol??'–'}` +
          (r.magnitude != null ? `<br>M${this.core.fmt(r.magnitude,2)} · depth ${this.core.fmt(r.origin_depth_km,1)} km` : ''));
        marker.on('popupopen', () => this._drawStationLines(eid, r.origin_lat, r.origin_lon, 'foconet'));
        marker.on('popupclose', () => this.stationLines.clearLayers());
        marker.addTo(this.markers2dFn);
      }
    }

    if (pts.length) this.map2d.fitBounds(pts, { padding: [20, 20] });
    setTimeout(() => this.map2d && this.map2d.invalidateSize(), 50);
  }

  async _drawStationLines(eid, evLat, evLon, src) {
    if (!this.map2d) return;
    this.stationLines.clearLayers();
    try {
      const url = src === 'foconet'
        ? `/api/mechanism/${this.jobId}/foconet/event/${encodeURIComponent(eid)}/stations`
        : `/api/mechanism/${this.jobId}/event/${encodeURIComponent(eid)}/stations`;
      const d = await (await fetch(url)).json();
      const stations = d.stations || d;
      if (!Array.isArray(stations)) return;
      const color = src === 'foconet' ? '#60a5fa' : '#f87171';
      for (const s of stations) {
        if (s.lat == null || s.lon == null) continue;
        L.polyline([[evLat, evLon], [s.lat, s.lon]], {
          color, weight: 1, opacity: 0.55, dashArray: '4,4',
        }).addTo(this.stationLines);
        const polColor = s.polarity > 0 ? '#22c55e' : s.polarity < 0 ? '#ef4444' : '#94a3b8';
        L.circleMarker([s.lat, s.lon], {
          radius: 5, color: polColor, fillColor: polColor, fillOpacity: 0.9, weight: 1,
        }).bindTooltip(`<b>${this.core.esc(s.station)}</b><br>pol=${s.polarity > 0 ? '+' : s.polarity < 0 ? '−' : '?'} dist=${s.dist_km?.toFixed(1) ?? '–'} km`, { className: 'rm-tip', sticky: true }).addTo(this.stationLines);
      }
    } catch (_) {}
  }

  async _renderCombinedMap() {
    await this._renderMap2d();
  }

  // ══════════════════════════════════════════
  // 3D viewer (SKHASH quality colours)
  // ══════════════════════════════════════════

  open3d() {
    const bd = document.getElementById('mech-3d-bd');
    if (!bd) return;
    bd.classList.remove('hidden');
    document.getElementById('mech-3d-job-lbl').textContent = this.jobId ? `job ${this.jobId}` : '';
    this.core.ensurePlotly().then(() => this._draw3d());
  }

  close3d() { document.getElementById('mech-3d-bd')?.classList.add('hidden'); }

  // ── 3D focal-sphere (beachball) mesh ────────────────────────────────────────
  // Builds a small UV-sphere once, reused (translated/scaled) for every event.
  // Per-face color is the sign of the double-couple P-wave radiation pattern:
  // dot(dir,n)*dot(dir,v) >= 0 → compressional quadrant, else dilatational.

  _sphereTemplate(nTheta = 8, nPhi = 14) {
    if (this.__sphereTpl) return this.__sphereTpl;
    const verts = [];   // unit vectors in [north, east, down] basis
    for (let i = 0; i <= nTheta; i++) {
      const theta = Math.PI * i / nTheta;
      const sinT = Math.sin(theta), cosT = Math.cos(theta);
      for (let j = 0; j < nPhi; j++) {
        const phi = 2 * Math.PI * j / nPhi;
        verts.push([sinT * Math.cos(phi), sinT * Math.sin(phi), cosT]);  // [n, e, d]
      }
    }
    const faces = [];
    for (let i = 0; i < nTheta; i++) {
      for (let j = 0; j < nPhi; j++) {
        const j1 = (j + 1) % nPhi;
        const a = i * nPhi + j, b = i * nPhi + j1;
        const c = (i + 1) * nPhi + j, d = (i + 1) * nPhi + j1;
        faces.push([a, b, c]);
        faces.push([b, d, c]);
      }
    }
    this.__sphereTpl = { verts, faces };
    return this.__sphereTpl;
  }

  /** One combined mesh3d trace: a small focal-sphere beachball per row. */
  _buildBeachballMesh(rows, darkColor, name) {
    const tpl = this._sphereTemplate();
    const rDeg = 0.012, rKm = 1.1;   // visual radius of each focal sphere
    const x = [], y = [], z = [], fi = [], fj = [], fk = [], facecolor = [];
    let vOffset = 0;
    for (const r of rows) {
      if (r.strike == null) continue;
      const { n, v } = this._sdrToVecs(r.strike, r.dip, r.rake);
      const lonScale = Math.cos(r.origin_lat * Math.PI / 180) || 1;
      const cz = -Math.abs(r.origin_depth_km || 0);
      for (const [vn, ve, vd] of tpl.verts) {
        x.push(r.origin_lon + (ve * rDeg) / lonScale);
        y.push(r.origin_lat + vn * rDeg);
        z.push(cz - vd * rKm);
      }
      for (const [a, b, c] of tpl.faces) {
        fi.push(vOffset + a); fj.push(vOffset + b); fk.push(vOffset + c);
        const va = tpl.verts[a], vb = tpl.verts[b], vc = tpl.verts[c];
        const cn = (va[0]+vb[0]+vc[0])/3, ce = (va[1]+vb[1]+vc[1])/3, cd = (va[2]+vb[2]+vc[2])/3;
        const dotN = cn*n[0] + ce*n[1] + cd*n[2];
        const dotV = cn*v[0] + ce*v[1] + cd*v[2];
        facecolor.push(dotN * dotV >= 0 ? darkColor : '#ffffff');
      }
      vOffset += tpl.verts.length;
    }
    if (!x.length) return null;
    return {
      type: 'mesh3d', x, y, z, i: fi, j: fj, k: fk, facecolor,
      flatshading: true, lighting: { ambient: 0.8, diffuse: 0.35, specular: 0.05 },
      hoverinfo: 'skip', name, showlegend: false,
    };
  }

  _draw3d() {
    const plt = document.getElementById('mech-3d-plt');
    if (!plt || typeof Plotly === 'undefined') return;

    // Mirror whatever the table/map are currently showing (checklist/filter/all)
    const visible = new Set(this._computeVisibleEids().eids);
    const rows   = Object.values(this.byEvent).filter(r => r.origin_lat != null && visible.has(r.event_id));
    const fnRows = Object.values(this.fnByEvent).filter(r => r.origin_lat != null && visible.has(r.event_id));

    const MAX_BEACHBALL_3D = 150;
    const tooMany = (rows.length + fnRows.length) > MAX_BEACHBALL_3D;

    // Small click-target markers (kept for hit-testing / hover popups)
    const traceSK = {
      type: 'scatter3d', mode: 'markers',
      x: rows.map(r => r.origin_lon), y: rows.map(r => r.origin_lat),
      z: rows.map(r => -Math.abs(r.origin_depth_km || 0)),
      marker: { size: tooMany ? 5 : 3, color: rows.map(r => MECH_QUALITY_COLOR[r.quality] || '#94a3b8'),
                opacity: tooMany ? 0.85 : 0.9, line: { width: .5, color: '#0a0f1e' } },
      text: rows.map(r => `${r.event_id}<br>Q${r.quality} S/D/R ${Math.round(r.strike)}/${Math.round(r.dip)}/${Math.round(r.rake)}`),
      hovertemplate: '%{text}<extra></extra>', name: 'SKHASH',
    };
    const fnTrace = fnRows.length ? {
      type: 'scatter3d', mode: 'markers',
      x: fnRows.map(r => r.origin_lon), y: fnRows.map(r => r.origin_lat),
      z: fnRows.map(r => -Math.abs(r.origin_depth_km || 0)),
      marker: { size: tooMany ? 4 : 2.5, color: '#1e40af', opacity: tooMany ? 0.7 : 0.5, symbol: 'diamond',
                line: { width: .5, color: '#0a0f1e' } },
      text: fnRows.map(r => `${r.event_id}<br>FocoNet S/D/R ${Math.round(r.strike)}/${Math.round(r.dip)}/${Math.round(r.rake)}`),
      hovertemplate: '%{text}<extra></extra>', name: 'FocoNet',
    } : null;

    const traces = [traceSK];
    if (fnTrace) traces.push(fnTrace);
    if (!tooMany) {
      const meshSK = this._buildBeachballMesh(rows,   '#dc2626', 'SKHASH beachball');
      const meshFn = this._buildBeachballMesh(fnRows, '#1e40af', 'FocoNet beachball');
      if (meshSK) traces.push(meshSK);
      if (meshFn) traces.push(meshFn);
    }

    Plotly.react(plt, traces, {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      scene: {
        xaxis: { title: 'Lon', color: '#94a3b8', gridcolor: '#1e3a5f' },
        yaxis: { title: 'Lat', color: '#94a3b8', gridcolor: '#1e3a5f' },
        zaxis: { title: 'Depth (km)', color: '#94a3b8', gridcolor: '#1e3a5f' },
        bgcolor: 'rgba(0,0,0,0)',
      },
      margin: { l: 0, r: 0, t: 10, b: 0 },
      legend: { font: { color: '#94a3b8' }, bgcolor: 'rgba(0,0,0,0)' },
    }, { displaylogo: false, responsive: true });

    const status = document.getElementById('mech-3d-status');
    if (status) {
      if (!rows.length && !fnRows.length) {
        status.style.display = '';
        status.textContent = 'No events shown — adjust the Checklist/Filter Results/All mode in the table.';
      } else if (tooMany) {
        status.style.display = '';
        status.textContent = `${rows.length + fnRows.length} events — too many to render as 3D beachballs (max ${MAX_BEACHBALL_3D}). Showing plain points; narrow it down via Checklist/Filter.`;
      } else {
        status.style.display = 'none';
      }
    }

    plt.on('plotly_click', (ev) => {
      const idx = ev.points?.[0]?.pointIndex;
      const traceIdx = ev.points?.[0]?.curveNumber;
      if (idx == null) return;
      if (traceIdx === 0) this._showSide(rows[idx], 'skhash');
      else if (traceIdx === 1 && fnTrace) this._showSideFn(fnRows[idx]);
    });
  }

  _showSide(r, src = 'skhash') {
    const side = document.getElementById('mech-3d-side');
    if (!side) return;
    const rel = this.figRel[r.event_id];
    const color = MECH_QUALITY_COLOR[r.quality] || '#94a3b8';
    const img = rel ? `<img src="/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(rel)}" style="width:100%;border-radius:4px;background:#fff;border:2px solid ${color}"/>` : '';
    side.innerHTML = `${img}
      <div style="margin-top:.4rem"><b>${this.core.esc(r.event_id)}</b>
        <span style="color:#dc2626;font-size:.58rem;margin-left:.3rem">SKHASH</span></div>
      <div>S/D/R: ${this.core.fmt(r.strike,0)}/${this.core.fmt(r.dip,0)}/${this.core.fmt(r.rake,0)}</div>
      <div>Q: <b style="color:${color}">${this.core.esc(r.quality||'–')}</b></div>
      <div>n_pol: ${r.num_p_pol??'–'} · gap: ${this.core.fmt(r.azimuthal_gap,0)}°</div>
      <div>Depth: ${this.core.fmt(r.origin_depth_km,1)} km</div>`;
    const fnR = this.fnByEvent[r.event_id];
    if (fnR) {
      const fnRel = this.fnFigRel[r.event_id];
      const fnImg = fnRel ? `<img src="/api/mechanism/${this.jobId}/foconet/figure?name=${encodeURIComponent(fnRel)}" style="width:100%;border-radius:4px;background:#fff;border:2px solid #1e40af;margin-top:.4rem"/>` : '';
      side.innerHTML += `<hr style="border:none;border-top:1px solid #334155;margin:.5rem 0">${fnImg}
        <div style="color:#60a5fa;font-size:.62rem">FocoNet S/D/R: ${this.core.fmt(fnR.strike,0)}/${this.core.fmt(fnR.dip,0)}/${this.core.fmt(fnR.rake,0)} · n_pol=${fnR.n_pol??'–'}</div>`;
    }
  }

  _showSideFn(r) {
    const side = document.getElementById('mech-3d-side');
    if (!side) return;
    const rel = this.fnFigRel[r.event_id];
    const img = rel ? `<img src="/api/mechanism/${this.jobId}/foconet/figure?name=${encodeURIComponent(rel)}" style="width:100%;border-radius:4px;background:#fff;border:2px solid #1e40af"/>` : '';
    side.innerHTML = `${img}
      <div style="margin-top:.4rem"><b>${this.core.esc(r.event_id)}</b>
        <span style="color:#1e40af;font-size:.58rem;margin-left:.3rem">FocoNet</span></div>
      <div>S/D/R: ${this.core.fmt(r.strike,0)}/${this.core.fmt(r.dip,0)}/${this.core.fmt(r.rake,0)}</div>
      <div>n_pol: ${r.n_pol??'–'}</div>
      <div>Depth: ${this.core.fmt(r.origin_depth_km,1)} km</div>`;
    const skR = this.byEvent[r.event_id];
    if (skR) {
      const skRel = this.figRel[r.event_id];
      const skColor = MECH_QUALITY_COLOR[skR.quality] || '#94a3b8';
      const skImg = skRel ? `<img src="/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(skRel)}" style="width:100%;border-radius:4px;background:#fff;border:2px solid ${skColor};margin-top:.4rem"/>` : '';
      side.innerHTML += `<hr style="border:none;border-top:1px solid #334155;margin:.5rem 0">${skImg}
        <div style="color:#dc2626;font-size:.62rem">SKHASH S/D/R: ${this.core.fmt(skR.strike,0)}/${this.core.fmt(skR.dip,0)}/${this.core.fmt(skR.rake,0)} · Q${skR.quality}</div>`;
    }
  }

  cam3d(mode) {
    const plt = document.getElementById('mech-3d-plt');
    if (!plt || typeof Plotly === 'undefined') return;
    const eye = mode === 'top' ? { x: 0, y: 0, z: 2.2 } : { x: 1.4, y: -1.4, z: 1.0 };
    Plotly.relayout(plt, { 'scene.camera.eye': eye });
  }

  // ══════════════════════════════════════════
  // SKHASH station polarity modal
  // ══════════════════════════════════════════

  openStationModal(eventId) {
    this._openStationModalGeneric(eventId, 'skhash');
  }

  openFnStationModal(eventId) {
    this._openStationModalGeneric(eventId, 'foconet');
  }

  _openStationModalGeneric(eventId, src) {
    const bd = document.getElementById('mech-station-bd');
    if (!bd) return;
    this._stationEventId = eventId;
    this._stationSrc = src;
    bd.classList.remove('hidden');
    const isFn = (src === 'foconet');
    const r = isFn ? this.fnByEvent[eventId] : this.byEvent[eventId];
    const color = isFn ? '#1e40af' : (MECH_QUALITY_COLOR[r?.quality] || '#94a3b8');
    const srcLabel = isFn ? 'FocoNet' : 'SKHASH';
    const sdr = r ? ` · S/D/R ${this.core.fmt(r.strike,0)}/${this.core.fmt(r.dip,0)}/${this.core.fmt(r.rake,0)}` : '';
    const qLabel = (!isFn && r?.quality) ? ` · Q${r.quality}` : '';
    document.getElementById('mech-station-lbl').textContent = `${eventId} [${srcLabel}]${sdr}${qLabel}`;
    const rel = isFn ? this.fnFigRel[eventId] : this.figRel[eventId];
    const thumb = document.getElementById('mech-station-thumb');
    if (thumb) {
      thumb.style.border = `2px solid ${color}`;
      const baseUrl = isFn
        ? `/api/mechanism/${this.jobId}/foconet/figure`
        : `/api/mechanism/${this.jobId}/figure`;
      thumb.src = rel ? `${baseUrl}?name=${encodeURIComponent(rel)}` : '';
    }
    this._loadStationData(eventId, src);
  }

  closeStationModal() { document.getElementById('mech-station-bd')?.classList.add('hidden'); }

  async _loadStationData(eventId, src) {
    const listEl = document.getElementById('mech-station-list');
    listEl.innerHTML = `<div class="pick-term-placeholder"><span class="sw-spinner"></span> Loading station polarities…</div>`;
    try {
      const isFn = (src === 'foconet');
      const url = isFn
        ? `/api/mechanism/${this.jobId}/foconet/event/${encodeURIComponent(eventId)}/stations`
        : `/api/mechanism/${this.jobId}/event/${encodeURIComponent(eventId)}/stations`;
      const data = await (await fetch(url)).json();
      if (data.error) throw new Error(data.error);
      this._renderStationMap(data, isFn);
      if (isFn) this._renderFnStationList(data);
      else this._renderStationList(data);
    } catch (e) {
      listEl.innerHTML = `<div class="pick-term-placeholder">Failed to load: ${this.core.esc(e.message)}</div>`;
    }
  }

  // SKHASH station map
  _renderStationMap(data, isFn = false) {
    const el = document.getElementById('mech-station-map');
    if (!el || typeof L === 'undefined') return;
    if (this._stMap) { this._stMap.remove(); this._stMap = null; }
    this._stMap = L.map(el, { zoomControl: true });
    L.tileLayer('/tiles/carto_dark/{z}/{x}/{y}.png',
      { attribution: '© CartoDB', maxZoom: 19 }).addTo(this._stMap);

    const pts = [[data.event.lat, data.event.lon]];
    const eid = this._stationEventId;
    const rel = isFn ? this.fnFigRel[eid] : this.figRel[eid];
    const baseUrl = isFn ? `/api/mechanism/${this.jobId}/foconet/figure` : `/api/mechanism/${this.jobId}/figure`;
    const epiIcon = rel
      ? L.divIcon({ html: `<img class="mech-map-thumb" src="${baseUrl}?name=${encodeURIComponent(rel)}">`,
                    iconSize: [44, 44], iconAnchor: [22, 22], className: 'mech-beachball-divicon' })
      : L.divIcon({ className: '', html: '<div style="font-size:20px;color:#fbbf24;text-shadow:0 0 3px #000">★</div>',
                    iconSize: [20, 20], iconAnchor: [10, 10] });
    L.marker([data.event.lat, data.event.lon], { icon: epiIcon, zIndexOffset: 1000 })
      .on('click', () => this.openEpiDetail())
      .addTo(this._stMap);

    (data.stations || []).forEach(s => {
      pts.push([s.lat, s.lon]);
      const isComp = (s.polarity ?? 0) > 0;
      const isDil  = (s.polarity ?? 0) < 0;
      const compColor = isFn ? '#1e40af' : '#dc2626';
      const fill = isComp ? compColor : (isDil ? '#ffffff' : '#6b7280');
      const marker = L.circleMarker([s.lat, s.lon], {
        radius: 7, color: '#1f1f1f', weight: 1.2,
        fillColor: fill, fillOpacity: isDil ? 0.85 : 0.95,
      });
      const label = isComp ? 'Compression (up)' : isDil ? 'Dilatation (down)' : 'Unknown';
      const distLine = s.dist_km != null ? `<br>dist ${this.core.fmt(s.dist_km,1)} km · az ${this.core.fmt(s.azimuth,0)}°` : '';
      const takeoffLine = s.takeoff != null ? ` · takeoff ${this.core.fmt(s.takeoff,0)}°` : '';
      marker.bindPopup(`<b>${this.core.esc(s.station)}</b><br>${label} (p=${this.core.fmt(s.polarity,2)})${distLine}${takeoffLine}`);
      marker.addTo(this._stMap);
    });
    if (pts.length) this._stMap.fitBounds(pts, { padding: [25, 25] });
    setTimeout(() => this._stMap && this._stMap.invalidateSize(), 60);
  }

  openEpiDetail() {
    const modal = document.getElementById('mech-epi-modal');
    const body  = document.getElementById('mech-epi-body');
    const lbl   = document.getElementById('mech-epi-lbl');
    if (!modal || !body) return;
    if (lbl) lbl.textContent = this._stationEventId || '';
    body.innerHTML = this._epiDetailHtml();
    modal.style.display = 'flex';
  }
  closeEpiDetail() { const m = document.getElementById('mech-epi-modal'); if (m) m.style.display = 'none'; }

  _epiDetailHtml() {
    const eid = this._stationEventId;
    const isFn = (this._stationSrc === 'foconet');
    const rel = isFn ? this.fnFigRel[eid] : this.figRel[eid];
    const baseUrl = isFn ? `/api/mechanism/${this.jobId}/foconet/figure` : `/api/mechanism/${this.jobId}/figure`;
    const url = rel ? `${baseUrl}?name=${encodeURIComponent(rel)}` : '';
    const borderColor = isFn ? '#1e40af' : '#334155';
    const fmtVal = v => {
      if (v === null || v === undefined || v === '') return '–';
      if (typeof v === 'boolean') return v ? 'yes' : 'no';
      if (typeof v === 'number') return this.core.fmt(v, Number.isInteger(v) ? 0 : 1);
      return this.core.esc(String(v));
    };

    let solBlocks = '';
    if (isFn) {
      const r = this.fnByEvent[eid];
      solBlocks = r ? `<table style="border-collapse:collapse;width:100%">
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Method</td><td style="font-weight:600;color:#60a5fa;font-size:.72rem">${this.core.esc(r.method||'FocoNet')}</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Strike</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">${fmtVal(r.strike)}°</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Dip</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">${fmtVal(r.dip)}°</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Rake</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">${fmtVal(r.rake)}°</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">N polarities</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">${fmtVal(r.n_pol)}</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Depth</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">${fmtVal(r.origin_depth_km)} km</td></tr>
        <tr><td style="color:#94a3b8;font-size:.7rem;padding:2px 10px 2px 0">Magnitude</td><td style="font-weight:600;color:#e2e8f0;font-size:.72rem">M${fmtVal(r.magnitude)}</td></tr>
      </table>
      <div style="margin-top:.5rem;font-size:.62rem;color:#64748b">No quality grade — ${this.core.esc(r.method||'FocoNet')} predicts the single most probable focal mechanism using a transformer neural network (Song et al., 2026).</div>` : '';
      // also show SKHASH if available for the same event
      const skR = this.byEvent[eid];
      if (skR) {
        const skRel = this.figRel[eid];
        const skColor = MECH_QUALITY_COLOR[skR.quality] || '#94a3b8';
        const skUrl = skRel ? `/api/mechanism/${this.jobId}/figure?name=${encodeURIComponent(skRel)}` : '';
        solBlocks += `<hr style="border:none;border-top:1px solid #334155;margin:.7rem 0">
          <div style="font-size:.65rem;color:#dc2626;font-weight:700;margin-bottom:.3rem">SKHASH comparison</div>
          ${skUrl ? `<img src="${skUrl}" style="width:80px;height:80px;background:#fff;border-radius:6px;border:2px solid ${skColor};margin-bottom:.3rem"/>` : ''}
          <div style="font-size:.65rem;color:#e2e8f0">S/D/R: ${this.core.fmt(skR.strike,0)}/${this.core.fmt(skR.dip,0)}/${this.core.fmt(skR.rake,0)} · Q<b style="color:${skColor}">${skR.quality}</b></div>`;
      }
    } else {
      const solutions = this.results.filter(x => String(x.event_id) === String(eid));
      solBlocks = solutions.map((s, i) => {
        const rows = MECH_OUT_FIELDS.map(([flbl, key, unit]) => {
          const color = key === 'quality' ? (MECH_QUALITY_COLOR[s[key]] || '#94a3b8') : '#e2e8f0';
          return `<tr><td style="color:#94a3b8;padding:2px 10px 2px 0;white-space:nowrap;font-size:.7rem">${flbl}</td>
            <td style="font-weight:600;color:${color};font-size:.72rem">${fmtVal(s[key])}${unit && s[key] != null ? unit : ''}</td></tr>`;
        }).join('');
        const hdr = solutions.length > 1
          ? `<div style="margin:.5rem 0 .25rem;font-size:.68rem;font-weight:700;color:#60a5fa">Solution ${i+1} / ${solutions.length}</div>` : '';
        return `${hdr}<table style="border-collapse:collapse;width:100%">${rows}</table>`;
      }).join('<hr style="border:none;border-top:1px solid #334155;margin:.6rem 0">');
    }

    const head = isFn ? this.fnByEvent[eid] : this.results.find(x => String(x.event_id) === String(eid));
    return `<div style="display:flex;gap:1.2rem;flex-wrap:wrap">
      <div style="flex-shrink:0;text-align:center;margin:0 auto">
        ${url ? `<img src="${url}" style="width:280px;height:280px;background:#fff;border-radius:10px;border:2px solid ${borderColor}"/>`
              : `<div style="width:280px;height:280px;display:flex;align-items:center;justify-content:center;color:#64748b;border:1px dashed #334155;border-radius:10px">No plot</div>`}
        <div style="margin-top:.5rem;font-size:.78rem;font-weight:700;color:#e2e8f0">${this.core.esc(eid)}</div>
        ${head ? `<div style="margin-top:.2rem;color:#94a3b8;font-size:.72rem">${this.core.esc(head.time||'')}<br>
          depth ${fmtVal(head.origin_depth_km)} km · M${fmtVal(head.magnitude)}<br>
          lat ${fmtVal(head.origin_lat)}, lon ${fmtVal(head.origin_lon)}</div>` : ''}
        <div style="margin-top:.3rem;font-size:.62rem;font-weight:700;color:${isFn ? '#1e40af' : '#dc2626'}">${isFn ? this.core.esc(head?.method||'FocoNet') : 'SKHASH'}</div>
      </div>
      <div style="flex:1;min-width:260px">${solBlocks || '<span style="color:#64748b">No output found.</span>'}</div>
    </div>`;
  }

  // SKHASH station list (with waveforms)
  _renderStationList(data) {
    const listEl = document.getElementById('mech-station-list');
    if (!listEl) return;
    const stations = data.stations || [];
    if (!stations.length) {
      listEl.innerHTML = `<div class="pick-term-placeholder">No station polarity data.</div>`;
      return;
    }
    const sorted = [...stations].sort((a, b) => (a.azimuth ?? 999) - (b.azimuth ?? 999));
    listEl.innerHTML = sorted.map(s => {
      const isComp = (s.polarity ?? 0) > 0;
      const isDil  = (s.polarity ?? 0) < 0;
      const badge = isComp ? `<span style="color:#f87171;font-weight:700">▲ Compression</span>`
        : isDil ? `<span style="color:#e5e7eb;font-weight:700">▽ Dilatation</span>`
        : `<span style="color:#6b7280">– Unknown</span>`;
      const conf = s.polarity != null
        ? (Math.abs(s.polarity) >= 1 ? '(confident, SNR≥4)' : '(weak, SNR<4)') : '';
      return `<div class="ov-card" style="padding:.45rem .6rem" data-mech-sta="${this.core.esc(s.station)}">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:.3rem;font-size:.66rem;gap:.4rem;flex-wrap:wrap">
          <b>${this.core.esc(s.station)}</b>
          <span style="color:var(--text-muted)">dist ${this.core.fmt(s.dist_km,1)} km · az ${this.core.fmt(s.azimuth,0)}° · takeoff ${this.core.fmt(s.takeoff,0)}°</span>
          <span style="flex:1"></span>
          ${badge}<span style="font-size:.56rem;color:var(--text-muted);margin-left:.3rem">${conf}</span>
        </div>
        <canvas class="mech-wave-cv" width="640" height="84" style="width:100%;height:84px;display:block;background:#0a0f1e;border-radius:4px"></canvas>
      </div>`;
    }).join('');
    listEl.querySelectorAll('[data-mech-sta]').forEach(card => {
      this._loadWaveform(card.dataset.mechSta, card.querySelector('canvas'));
    });
  }

  // FocoNet station list (with waveforms — same API as SKHASH, blue compression colour)
  _renderFnStationList(data) {
    const listEl = document.getElementById('mech-station-list');
    if (!listEl) return;
    const stations = data.stations || [];
    if (!stations.length) {
      listEl.innerHTML = `<div class="pick-term-placeholder">No station polarity data.</div>`;
      return;
    }
    const sorted = [...stations].sort((a, b) => (a.azimuth ?? 999) - (b.azimuth ?? 999));
    listEl.innerHTML = sorted.map(s => {
      const isComp = (s.polarity ?? 0) > 0;
      const isDil  = (s.polarity ?? 0) < 0;
      const badge = isComp ? `<span style="color:#60a5fa;font-weight:700">▲ Compression</span>`
        : isDil ? `<span style="color:#e5e7eb;font-weight:700">▽ Dilatation</span>`
        : `<span style="color:#6b7280">– Unknown</span>`;
      const conf = s.snr != null
        ? (s.snr >= 4 ? '(confident, SNR≥4)' : '(weak, SNR<4)') : '';
      return `<div class="ov-card" style="padding:.45rem .6rem" data-mech-sta="${this.core.esc(s.station)}">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:.3rem;font-size:.66rem;gap:.4rem;flex-wrap:wrap">
          <b>${this.core.esc(s.station)}</b>
          <span style="color:var(--text-muted)">dist ${this.core.fmt(s.dist_km,1)} km · az ${this.core.fmt(s.azimuth,0)}°</span>
          <span style="flex:1"></span>
          ${badge}<span style="font-size:.56rem;color:var(--text-muted);margin-left:.3rem">${conf}</span>
        </div>
        <canvas class="mech-wave-cv" width="640" height="84" style="width:100%;height:84px;display:block;background:#0a0f1e;border-radius:4px"></canvas>
      </div>`;
    }).join('');
    listEl.querySelectorAll('[data-mech-sta]').forEach(card => {
      this._loadWaveform(card.dataset.mechSta, card.querySelector('canvas'), true);
    });
  }

  // Waveform loading — isFn=true uses blue compression colour for FocoNet
  async _loadWaveform(station, canvas, isFn = false) {
    if (!canvas) return;
    try {
      const wv = await (await fetch(
        `/api/mechanism/${this.jobId}/event/${encodeURIComponent(this._stationEventId)}/waveform?station=${encodeURIComponent(station)}`
      )).json();
      if (wv.error) throw new Error(wv.error);
      this._drawWave(canvas, wv, isFn);
    } catch (e) {
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#475569'; ctx.font = '11px sans-serif';
      ctx.fillText(`waveform unavailable (${e.message})`, 6, canvas.height / 2);
    }
  }

  _drawWave(canvas, wv, isFn = false) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    const times = wv.times, amps = wv.amps;
    if (!times || !times.length) return;
    const tMin = times[0], tMax = times[times.length - 1];
    const span = Math.max(1e-6, tMax - tMin);
    const x = t => (t - tMin) / span * W;
    const maxAbs = Math.max(1e-9, ...amps.map(Math.abs));
    const midY = H / 2, scaleY = (H * 0.42) / maxAbs;
    const y = a => midY - a * scaleY;
    const isComp = (wv.polarity ?? 0) > 0;
    const isDil  = (wv.polarity ?? 0) < 0;
    // FocoNet: blue compression; SKHASH: red compression
    const compColor = isFn ? '#60a5fa' : '#f87171';
    const compBg    = isFn ? 'rgba(96,165,250,.18)' : 'rgba(220,38,38,.20)';

    ctx.fillStyle = 'rgba(148,163,184,.10)';
    ctx.fillRect(x(-wv.noise_win), 0, x(0) - x(-wv.noise_win), H);
    ctx.fillStyle = isComp ? compBg : isDil ? 'rgba(229,231,235,.16)' : 'rgba(107,114,128,.12)';
    ctx.fillRect(x(0), 0, x(wv.sig_win) - x(0), H);

    ctx.strokeStyle = '#334155'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, midY); ctx.lineTo(W, midY); ctx.stroke();

    ctx.strokeStyle = '#fbbf24'; ctx.lineWidth = 1.3; ctx.setLineDash([3, 2]);
    ctx.beginPath(); ctx.moveTo(x(0), 0); ctx.lineTo(x(0), H); ctx.stroke();
    ctx.setLineDash([]);

    ctx.strokeStyle = isComp ? compColor : isDil ? '#e5e7eb' : '#93c5fd';
    ctx.lineWidth = 1.4; ctx.beginPath();
    for (let i = 0; i < times.length; i++) {
      const px = x(times[i]), py = y(amps[i]);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    ctx.font = '10px sans-serif';
    ctx.fillStyle = isComp ? compColor : isDil ? '#e5e7eb' : '#93c5fd';
    ctx.fillText(isComp ? 'Compression (up)' : isDil ? 'Dilatation (down)' : 'Unknown', 4, 12);
    ctx.fillStyle = '#64748b';
    ctx.fillText('P', x(0) - 3, H - 4);
  }
}

const MECH = new MechanismModule(SW);

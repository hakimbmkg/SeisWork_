// Result Viewer — Event Waveform Canvas (waveform + picks + spectrogram per
// station, pan/zoom via drag+wheel) — by HakimBMKG
// Note: loadAllSpecs() reads _RM.cfgId/_RM.activeJob (Result Viewer core state,
// still global in seiswork.js) — read-only, this module never writes to _RM.
class ResultViewerWaveformModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.data = null;   // {event_id, traces, origin_time, ...}
    this.good = [];     // filtered+sorted traces
    this.specs = {};    // {sta: {freqs, times, Sxx, vmin, vmax, cha}}
    this.specOn = false;
    this.view = { t0: -10, t1: 60 };   // 70-second visible window
    this.drag = { on: false, startX: 0, t0: -10, t1: 60 };
    this.eventsAttached = false;
    this.hoverT = null;   // relative seconds under cursor, or null when not hovering
    // Layout constants
    this.PRE = 10; this.POST = 80;  // data range
    this.WIN = 70;    // initial visible window (seconds)
    this.LW  = 76;    // label column width (px)
    this.RW  = 4;     // right margin
    this.WH  = 52;    // waveform row height
    this.SH  = 66;    // spectrogram row height
    this.AX  = 38;    // time axis height (room for 2 rows: relative + wall-clock)
  }

  // Reset all state — called when the modal closes or before a fresh fetch.
  reset() {
    this.data = null; this.good = []; this.specs = {}; this.specOn = false;
    this.eventsAttached = false; this.hoverT = null;
  }

  // --- Controller+View (draw from freshly-fetched waveform data) ---
  drawCanvas(data) {
    const good = data.traces.filter(t => t.times && t.times.length > 0);
    if (!good.length) {
      _rmWvErr('No waveform data. ' + (data.traces.filter(t => t.error).map(t => `${t.sta}:${t.error}`).join(', ') || 'all empty'));
      return;
    }
    const phOrd = c => c.endsWith('Z') ? 0 : c.endsWith('N') ? 1 : c.endsWith('E') ? 2 : 3;
    // Sort by earliest pick (P or S) → earliest-arriving station first
    const _earliest = tr => {
      const p = tr.picks;
      const times = p ? [p.P, p.S].filter(t => t != null) : [];
      return times.length ? Math.min(...times) : Infinity;
    };
    good.sort((a, b) => {
      const ea = _earliest(a), eb = _earliest(b);
      if (ea !== eb) return ea - eb;
      return a.sta !== b.sta ? (a.sta < b.sta ? -1 : 1) : phOrd(a.cha) - phOrd(b.cha);
    });

    this.data   = data;
    this.good   = good;
    this.specs  = {};
    this.specOn = false;
    this.view   = { t0: -this.PRE, t1: -this.PRE + this.WIN };

    document.getElementById('rm-wv-loading').style.display = 'none';
    document.getElementById('rm-wv-err').style.display     = 'none';
    document.getElementById('rm-wv-canvas-outer').style.display = 'block';

    // When the catalog's event ids/times don't line up with any picks (e.g. manually
    // injected relocation catalog), the backend falls back to the nearest stations —
    // waveforms show but without P/S markers. Tell the user so the absence is clear.
    const metaEl = document.getElementById('rm-wv-meta');
    if (metaEl) {
      const old = metaEl.querySelector('.rm-wv-fallback-note');
      if (old) old.remove();
      if (data.picks_fallback) {
        const note = document.createElement('span');
        note.className = 'rm-wv-fallback-note';
        note.style.cssText = 'color:#fbbf24';
        note.innerHTML = '<i class="bi bi-info-circle"></i> picks not linked to this event — showing nearest stations (no P/S markers)';
        metaEl.appendChild(note);
      }
    }

    this.attachEvents();
    this.renderCanvas();

    // Auto-load spec if toggle already on
    const tog = document.getElementById('rm-wv-spec-tog');
    if (tog && tog.checked) this.loadAllSpecs();
  }

  renderCanvas() {
    const outer  = document.getElementById('rm-wv-canvas-outer');
    const canvas = document.getElementById('rm-wv-canvas');
    if (!canvas || !this.good.length) return;

    const W   = outer.clientWidth || 820;
    const pW  = W - this.LW - this.RW;  // plot width

    // Row layout
    const nSta  = this.good.length;
    const rowH  = this.WH + (this.specOn ? this.SH + 1 : 0);
    const H     = nSta * (rowH + 1) + this.AX + 2;

    canvas.width = W; canvas.height = H;
    canvas.style.cssText = 'display:block;width:100%;cursor:grab';
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);

    const { t0, t1 } = this.view;
    const tSpan = t1 - t0 || this.WIN;

    // Helper: time → pixel x
    const toPx = t => this.LW + ((t - t0) / tSpan) * pW;

    this.good.forEach((tr, i) => {
      const wy0 = i * (rowH + 1);
      const wy1 = wy0 + this.WH;
      const wyM = wy0 + this.WH / 2;

      // Waveform row bg
      ctx.fillStyle = i % 2 === 0 ? '#f8fafc' : '#f1f5f9';
      ctx.fillRect(0, wy0, W, this.WH);

      // Label
      ctx.textAlign = 'left';
      ctx.font = 'bold 9px monospace'; ctx.fillStyle = '#374151';
      ctx.fillText(`${tr.net}.${tr.sta}`, 2, wy0 + this.WH * 0.42);
      ctx.font = '8px monospace'; ctx.fillStyle = '#6b7280';
      ctx.fillText(tr.cha, 2, wy0 + this.WH * 0.72);

      // Vertical clipping region
      ctx.save(); ctx.beginPath(); ctx.rect(this.LW, wy0, pW, this.WH); ctx.clip();

      // OT dashed line (t=0)
      const ox = toPx(0);
      ctx.strokeStyle = 'rgba(34,197,94,0.55)'; ctx.lineWidth = 1.2;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(ox, wy0); ctx.lineTo(ox, wy1); ctx.stroke();
      ctx.setLineDash([]);

      // Waveform polyline
      const amp = tr.data.reduce((m, v) => Math.max(m, Math.abs(v)), 0) || 1;
      const scale = (this.WH * 0.42) / amp;
      ctx.strokeStyle = '#3a3a3a'; ctx.lineWidth = 0.85;
      ctx.beginPath();
      let fp = true;
      for (let j = 0; j < tr.times.length; j++) {
        const x = toPx(tr.times[j]);
        if (x < this.LW - 2 || x > W + 2) { fp = true; continue; }
        const y = wyM - tr.data[j] * scale;
        if (fp) { ctx.moveTo(x, y); fp = false; } else ctx.lineTo(x, y);
      }
      ctx.stroke();

      // P pick
      if (tr.picks && tr.picks.P != null) {
        const px = toPx(tr.picks.P);
        if (px >= this.LW && px <= W - this.RW) {
          ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1.8;
          ctx.beginPath(); ctx.moveTo(px, wy0 + 2); ctx.lineTo(px, wy1 - 2); ctx.stroke();
          ctx.font = 'bold 8px monospace'; ctx.fillStyle = '#ef4444'; ctx.textAlign = 'left';
          ctx.fillText(`P ${tr.picks.P.toFixed(2)}s`, px + 2, wy0 + 11);
        }
      }
      // S pick
      if (tr.picks && tr.picks.S != null) {
        const sx = toPx(tr.picks.S);
        if (sx >= this.LW && sx <= W - this.RW) {
          ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 1.8;
          ctx.beginPath(); ctx.moveTo(sx, wy0 + 2); ctx.lineTo(sx, wy1 - 2); ctx.stroke();
          ctx.font = 'bold 8px monospace'; ctx.fillStyle = '#3b82f6'; ctx.textAlign = 'left';
          ctx.fillText(`S ${tr.picks.S.toFixed(2)}s`, sx + 2, wy0 + 22);
        }
      }

      // Predicted P/S arrivals from the local velocity model (dashed, QC reference
      // for how far the actual pick sits from the theoretical arrival).
      if (tr.syn_picks) {
        const _synMark = (t, color, label, labelY) => {
          if (t == null) return;
          const x = toPx(t);
          if (x < this.LW || x > W - this.RW) return;
          ctx.strokeStyle = color; ctx.lineWidth = 1.2;
          ctx.setLineDash([2, 2]);
          ctx.beginPath(); ctx.moveTo(x, wy0 + 2); ctx.lineTo(x, wy1 - 2); ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = '7px monospace'; ctx.fillStyle = color; ctx.textAlign = 'left';
          ctx.fillText(label, x + 2, labelY);
        };
        _synMark(tr.syn_picks.P, 'rgba(239,68,68,0.55)', 'synP', wy1 - 14);
        _synMark(tr.syn_picks.S, 'rgba(59,130,246,0.55)', 'synS', wy1 - 4);
      }
      ctx.restore();

      // Row bottom separator
      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(0, wy1); ctx.lineTo(W, wy1); ctx.stroke();

      // ── Spectrogram row (below waveform) ─────────────────────────────────
      if (this.specOn) {
        const sy0 = wy1 + 1;
        const sy1 = sy0 + this.SH;
        const sd  = this.specs[tr.sta];

        ctx.fillStyle = '#0a0f1e'; ctx.fillRect(0, sy0, W, this.SH);
        // Spec label
        ctx.font = '7.5px monospace'; ctx.fillStyle = '#475569'; ctx.textAlign = 'left';
        ctx.fillText(sd ? `${tr.sta}.${sd.cha || tr.cha}` : `${tr.sta} loading…`, 2, sy0 + 10);

        if (sd) {
          const { freqs, times: sTimes, Sxx, vmin, vmax } = sd;
          const nf = freqs.length, nt = sTimes.length;
          const range = vmax - vmin || 1;
          ctx.save(); ctx.beginPath(); ctx.rect(this.LW, sy0, pW, this.SH); ctx.clip();
          for (let ti = 0; ti < nt; ti++) {
            const x0 = toPx(sTimes[ti]);
            const x1 = ti + 1 < nt ? toPx(sTimes[ti + 1]) : x0 + 2;
            if (x1 < this.LW || x0 > W - this.RW) continue;
            for (let fi = 0; fi < nf; fi++) {
              const val = Math.max(0, Math.min(1, (Sxx[fi][ti] - vmin) / range));
              const [r, g, b] = _viridis(val);
              ctx.fillStyle = `rgb(${r},${g},${b})`;
              const cellH = this.SH / nf;
              const y = sy0 + this.SH - (fi + 1) * cellH;
              ctx.fillRect(x0, y, Math.max(1, x1 - x0) + 0.5, Math.ceil(cellH) + 0.5);
            }
          }
          // Frequency labels
          ctx.font = '7.5px monospace'; ctx.textAlign = 'left';
          [1, 5, 10, 20, 50].forEach(hz => {
            const idx = freqs.findIndex(f => f >= hz); if (idx < 0 || idx >= nf) return;
            const y = sy0 + this.SH - (idx / nf) * this.SH;
            if (y > sy0 + 8 && y < sy1 - 3) {
              ctx.fillStyle = 'rgba(255,255,255,0.15)'; ctx.fillRect(this.LW, y, pW, 0.5);
              ctx.fillStyle = 'rgba(160,210,240,0.75)'; ctx.fillText(hz + 'Hz', this.LW + 2, y - 1);
            }
          });
          ctx.restore();
        }
        // Spec bottom separator
        ctx.strokeStyle = '#1e3a5f'; ctx.lineWidth = 0.5;
        ctx.beginPath(); ctx.moveTo(0, sy1); ctx.lineTo(W, sy1); ctx.stroke();
      }
    });

    // ── Time axis ──────────────────────────────────────────────────────────────
    // Row 1: relative seconds (offset from OT).  Row 2: wall-clock HH:MM:SS.
    const axY = H - this.AX + 2;
    ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(this.LW, axY); ctx.lineTo(W - this.RW, axY); ctx.stroke();
    const step = tSpan > 60 ? 10 : 5;
    const firstTick = Math.ceil(t0 / step) * step;

    // Parse origin_time to compute wall-clock labels
    let otEpoch = null;
    if (this.data?.origin_time) {
      try { otEpoch = new Date(this.data.origin_time.replace(' ', 'T') + 'Z').getTime(); } catch (_) {}
    }
    const _tzH = (typeof _TZ !== 'undefined' ? _TZ.h : 7);
    const _tzLabel = _tzH === 0 ? 'UTC' : `WIB`;
    const _wallClock = (relS) => {
      if (otEpoch == null) return '';
      const ms = otEpoch + relS * 1000 + _tzH * 3600000;
      const d = new Date(ms);
      const hh = String(d.getUTCHours()).padStart(2, '0');
      const mm = String(d.getUTCMinutes()).padStart(2, '0');
      const ss = String(d.getUTCSeconds()).padStart(2, '0');
      return `${hh}:${mm}:${ss}`;
    };

    ctx.textAlign = 'center';
    for (let t = firstTick; t <= t1 + 0.01; t += step) {
      const tx = toPx(t);
      if (tx < this.LW + 2 || tx > W - this.RW - 2) continue;
      ctx.strokeStyle = '#94a3b8'; ctx.lineWidth = 0.8;
      ctx.beginPath(); ctx.moveTo(tx, axY); ctx.lineTo(tx, axY + 4); ctx.stroke();
      // Row 1: relative (+Xs)
      ctx.font = '8px monospace'; ctx.fillStyle = '#6b7280';
      ctx.fillText(`${t > 0 ? '+' : ''}${t}s`, tx, axY + 13);
      // Row 2: wall-clock (UTC)
      const wc = _wallClock(t);
      if (wc) {
        ctx.font = '7.5px monospace'; ctx.fillStyle = '#475569';
        ctx.fillText(wc, tx, axY + 25);
      }
    }
    // OT marker on axis
    const otx = toPx(0);
    if (otx >= this.LW && otx <= W - this.RW) {
      ctx.strokeStyle = 'rgba(34,197,94,0.8)'; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(otx, axY - 3); ctx.lineTo(otx, axY + 6); ctx.stroke();
      ctx.font = 'bold 7.5px monospace'; ctx.fillStyle = 'rgba(34,197,94,0.9)'; ctx.textAlign = 'center';
      ctx.fillText('OT', otx, axY + 35);
    }
    // Timezone label (bottom-right)
    ctx.font = '7px monospace'; ctx.fillStyle = '#94a3b8'; ctx.textAlign = 'right';
    ctx.fillText(_tzLabel, W - this.RW - 2, H - 2);
    ctx.textAlign = 'left';

    // Pan progress bar (mini-map) at very top
    const barH = 3, barW = pW;
    const tMin = -this.PRE, tMax = this.POST;
    const bx0  = this.LW + ((t0 - tMin) / (tMax - tMin)) * barW;
    const bx1  = this.LW + ((t1 - tMin) / (tMax - tMin)) * barW;
    ctx.fillStyle = '#e2e8f0'; ctx.fillRect(this.LW, 0, barW, barH);
    ctx.fillStyle = '#60a5fa'; ctx.fillRect(bx0, 0, bx1 - bx0, barH);

    // ── Hover crosshair + time readout ────────────────────────────────────────
    if (this.hoverT != null && !this.drag.on) {
      const hx = toPx(this.hoverT);
      if (hx >= this.LW && hx <= W - this.RW) {
        ctx.strokeStyle = 'rgba(15,23,42,0.35)'; ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(hx, barH); ctx.lineTo(hx, axY); ctx.stroke();
        ctx.setLineDash([]);

        const relLabel  = `${this.hoverT >= 0 ? '+' : ''}${this.hoverT.toFixed(2)}s`;
        const wcLabel   = _wallClock(this.hoverT);
        const labelText = wcLabel ? `${relLabel}  ${wcLabel} ${_tzLabel}` : relLabel;
        ctx.font = 'bold 8.5px monospace';
        const tw  = ctx.measureText(labelText).width;
        let lx = hx + 5;
        if (lx + tw + 8 > W - this.RW) lx = hx - tw - 13;
        const ly = barH + 2;
        ctx.fillStyle = 'rgba(15,23,42,0.85)';
        ctx.fillRect(lx, ly, tw + 8, 14);
        ctx.fillStyle = '#f8fafc'; ctx.textAlign = 'left';
        ctx.fillText(labelText, lx + 4, ly + 10);
        ctx.textAlign = 'left';
      }
    }
  }

  // --- Controller (pan / zoom event handlers) ---
  attachEvents() {
    if (this.eventsAttached) return;
    this.eventsAttached = true;
    const outer = document.getElementById('rm-wv-canvas-outer');
    if (!outer) return;

    outer.addEventListener('mousedown', e => {
      if (e.button !== 0) return;
      this.drag = { on: true, startX: e.clientX, t0: this.view.t0, t1: this.view.t1 };
      outer.style.cursor = 'grabbing';
    });
    outer.addEventListener('mousemove', e => {
      const canvas = document.getElementById('rm-wv-canvas');
      if (!canvas) return;
      const pW   = (outer.clientWidth || 820) - this.LW - this.RW;
      if (this.drag.on) {
        const tSpan = this.drag.t1 - this.drag.t0;
        const dt   = (e.clientX - this.drag.startX) / pW * tSpan * -1;
        let t0 = this.drag.t0 + dt, t1 = this.drag.t1 + dt;
        const tMin = -this.PRE, tMax = this.POST;
        if (t0 < tMin) { t1 += tMin - t0; t0 = tMin; }
        if (t1 > tMax) { t0 -= t1 - tMax; t1 = tMax; }
        this.view = { t0, t1 };
        this.renderCanvas();
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const cx   = e.clientX - rect.left;
      if (cx < this.LW || cx > rect.width - this.RW) { this.hoverT = null; this.renderCanvas(); return; }
      const { t0, t1 } = this.view;
      this.hoverT = t0 + (cx - this.LW) / pW * (t1 - t0);
      this.renderCanvas();
    });
    const endDrag = () => { this.drag.on = false; outer.style.cursor = 'grab'; };
    outer.addEventListener('mouseup', endDrag);
    outer.addEventListener('mouseleave', () => {
      endDrag();
      this.hoverT = null;
      this.renderCanvas();
    });

    // Scroll wheel: zoom around cursor
    outer.addEventListener('wheel', e => {
      e.preventDefault();
      const canvas = document.getElementById('rm-wv-canvas');
      if (!canvas) return;
      const rect  = canvas.getBoundingClientRect();
      const pW    = (outer.clientWidth || 820) - this.LW - this.RW;
      const { t0, t1 } = this.view;
      const tSpan = t1 - t0;
      const cx    = (e.clientX - rect.left - this.LW) / pW;
      const tCur  = t0 + cx * tSpan;
      const factor = e.deltaY > 0 ? 1.15 : 0.87;
      let nt0 = tCur - (tCur - t0) * factor;
      let nt1 = tCur + (t1 - tCur) * factor;
      const newSpan = nt1 - nt0;
      if (newSpan < 15) { nt0 = tCur - 7.5; nt1 = tCur + 7.5; }
      if (newSpan > this.PRE + this.POST) { nt0 = -this.PRE; nt1 = this.POST; }
      const tMin = -this.PRE, tMax = this.POST;
      if (nt0 < tMin) { nt1 += tMin - nt0; nt0 = tMin; }
      if (nt1 > tMax) { nt0 -= nt1 - tMax; nt1 = tMax; }
      this.view = { t0: nt0, t1: nt1 };
      this.renderCanvas();
    }, { passive: false });
  }

  // --- Controller (spectrogram for all stations) ---
  async toggleEventSpec(on) {
    this.specOn = on;
    if (on && !Object.keys(this.specs).length && this.data) {
      this.loadAllSpecs();
    } else {
      this.renderCanvas();
    }
  }

  async loadAllSpecs() {
    if (!_RM.cfgId || !_RM.activeJob || !this.data) return;
    const infoEl = document.getElementById('rm-wv-spec-info');
    const rowEl  = document.getElementById('rm-wv-spec-row');
    if (rowEl) rowEl.style.display = 'flex';
    if (infoEl) infoEl.textContent = '⟳ Computing spectrogram for all stations…';
    this.renderCanvas();  // show loading placeholders
    try {
      const url = `/api/result/${_RM.cfgId}/waveform/spectro_all?job_id=${_RM.activeJob.job_id}&event_id=${encodeURIComponent(this.data.event_id)}&post_s=70`;
      const res = await fetch(url); const d = await res.json();
      if (!res.ok) { if (infoEl) infoEl.textContent = '⚠ ' + (d.error || 'error'); return; }
      (d.specs || []).forEach(s => { this.specs[s.sta] = s; });
      const n = Object.keys(this.specs).length;
      if (infoEl) infoEl.textContent = `Spectrogram ${n} stations  ·  viridis`;
      this.renderCanvas();
    } catch (e) { if (infoEl) infoEl.textContent = '⚠ ' + e.message; }
  }

  // --- Controller (modal close) ---
  closeModal() {
    document.getElementById('rm-wv-bd').classList.add('hidden');
    const outer = document.getElementById('rm-wv-canvas-outer');
    if (outer) outer.style.display = 'none';
    const togEl = document.getElementById('rm-wv-spec-tog');
    if (togEl) togEl.checked = false;
    const rowEl = document.getElementById('rm-wv-spec-row');
    if (rowEl) rowEl.style.display = 'none';
    this.reset();
  }
}

const RMWV = new ResultViewerWaveformModule(SW);

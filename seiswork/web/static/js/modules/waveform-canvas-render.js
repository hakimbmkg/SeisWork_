/**
 * waveform-canvas-render.js — by HakimBMKG
 * Pure functions (no state/class) to draw the live waveform+spectrogram
 * canvas, 1 row per station. Shared by the main dashboard
 * (online-monitor.js, OM._drawWaveformAll) AND the full-page zoom view
 * (waveform-fullpage.js) — so the look always matches, from one source.
 *
 * The time axis is SEPARATE from the row canvas (renderTimeAxis) — placed in
 * its own element OUTSIDE the scroll container, so it stays visible (fixed)
 * even when the user scrolls down through many stations, and can be
 * refreshed quickly (1s) for the "now" line without redrawing every heavy row.
 *
 * Standalone — deliberately does not depend on seiswork.js (_viridis is
 * redefined here) so the full-page view stays light without loading every module.
 *
 * Render optimizations (scrttv RecordPolyline::pushRecord style):
 *   - Min-max per pixel column: samples landing on the same pixel x collapse
 *     into a single min→max vertical line. This is O(px_width), not O(n_samples).
 *   - Viewport culling: rows outside the scroll viewport only get a background
 *     clear — the trace/spectrogram are not drawn, saving 3-5× for 47 stations.
 *   - Canvas reuse: width/height are only reset when changed; at the same size
 *     a plain clearRect suffices (avoids GPU backing-store teardown every tick).
 */
const WV_LW = 84, WV_RW = 4, WV_WIN = 1800;  // shared with renderTimeAxis
// Right headroom: show 2 minutes PAST the current time so the trace is not flush
// against the box edge (the "now" line sits ~2 minutes from the right, easy to see).
// Total horizontal span = WV_WIN + WV_FUTURE_PAD. Used by renderWaveformCanvas & renderTimeAxis.
const WV_FUTURE_PAD = 120;

function _wvViridis(t) {
  const s = [[68, 1, 84], [71, 17, 100], [72, 33, 115], [70, 48, 126], [66, 64, 134], [59, 79, 139], [52, 94, 141], [46, 108, 142], [40, 122, 142], [35, 136, 142], [31, 150, 139], [32, 163, 134], [40, 175, 127], [62, 188, 115], [90, 200, 100], [122, 209, 81], [157, 217, 59], [194, 223, 35], [230, 228, 25], [253, 231, 37]];
  const i = Math.min(s.length - 2, Math.floor(t * (s.length - 1))), f = t * (s.length - 1) - i;
  return [Math.round(s[i][0] + f * (s[i + 1][0] - s[i][0])), Math.round(s[i][1] + f * (s[i + 1][1] - s[i][1])), Math.round(s[i][2] + f * (s[i + 1][2] - s[i][2]))];
}

/**
 * Render the waveform — two modes just like the offline Waveform Viewer (seiswork.js):
 *
 * SPARSE (< 1.5 samples/pixel):
 *   Direct polyline through each sample in time order — identical to the
 *   offline renderer's `tr.samples` path. Produces a sinusoidal waveform.
 *
 * DENSE (≥ 1.5 samples/pixel):
 *   Min-max per pixel column → filled envelope (forward min + backward max) —
 *   identical to the offline renderer's `tr.maxs` path. Produces a solid band
 *   showing the amplitude range at each pixel.
 *
 * No "flat spine" or zigzag artifacts.
 */
function _drawMinMaxTrace(ctx, pts, wyM, WH, toPx, pxPerSec, LW, pW) {
  if (pts.length < 2) return;

  const mean  = pts.reduce((s, p) => s + p.v, 0) / pts.length;
  const amp   = pts.reduce((m, p) => Math.max(m, Math.abs(p.v - mean)), 0) || 1;
  const scale = (WH * 0.42) / amp;

  const dts = [];
  for (let j = 1; j < pts.length; j++) dts.push(pts[j].t - pts[j - 1].t);
  dts.sort((a, b) => a - b);
  const medDt    = dts[Math.floor(dts.length / 2)] || 1.0;
  const maxGapPx = Math.max(medDt * 8 * pxPerSec, 4);

  const density = pts.length / pW;  // samples per pixel in visible window

  // ── SPARSE: direct polyline ───────────────────────────────────────────────
  if (density < 1.5) {
    ctx.beginPath();
    let started = false, prevX = null;
    for (let i = 0; i < pts.length; i++) {
      const x = toPx(pts[i].t);
      if (x < LW - 2 || x > LW + pW + 2) continue;
      const y    = wyM - (pts[i].v - mean) * scale;
      // Putus path bila ada gap maju ATAU loncatan mundur (x < prevX) — backward
      // jump = out-of-order data; drawn with lineTo it becomes a sweeping line.
      const isGap = prevX !== null && ((x - prevX) > maxGapPx || x < prevX - 1);
      if (isGap || !started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
      prevX = x;
    }
    ctx.stroke();
    return;
  }

  // ── DENSE: filled envelope (min forward + max backward) ───────────────────
  const cols     = new Map();
  const colOrder = [];
  for (let i = 0; i < pts.length; i++) {
    const x = Math.round(toPx(pts[i].t));
    if (x < LW - 2 || x > LW + pW + 2) continue;
    const y = wyM - (pts[i].v - mean) * scale;
    if (!cols.has(x)) { cols.set(x, { min: y, max: y }); colOrder.push(x); }
    else { const c = cols.get(x); if (y < c.min) c.min = y; if (y > c.max) c.max = y; }
  }
  if (colOrder.length === 0) return;

  // Draw each gapless segment as a closed polygon
  const flushSeg = (from, to) => {
    if (from > to) return;
    if (from === to) {
      // Kolom tunggal → garis vertikal
      const x = colOrder[from], c = cols.get(x);
      ctx.beginPath(); ctx.moveTo(x, c.min); ctx.lineTo(x, c.max); ctx.stroke();
      return;
    }
    ctx.beginPath();
    for (let ci = from; ci <= to; ci++) {
      const x = colOrder[ci], c = cols.get(x);
      ci === from ? ctx.moveTo(x, c.min) : ctx.lineTo(x, c.min);
    }
    for (let ci = to; ci >= from; ci--)
      ctx.lineTo(colOrder[ci], cols.get(colOrder[ci]).max);
    ctx.closePath(); ctx.fill();
  };

  ctx.fillStyle = 'rgba(90,90,90,0.88)';
  let segStart = 0;
  for (let ci = 1; ci < colOrder.length; ci++) {
    if ((colOrder[ci] - colOrder[ci - 1]) > maxGapPx) {
      flushSeg(segStart, ci - 1); segStart = ci;
    }
  }
  flushSeg(segStart, colOrder.length - 1);
}

/**
 * Draw the stacked waveform+spectrogram canvas, 1 row per station (WITHOUT the
 * time axis — that is drawn separately by renderTimeAxis in another element).
 * @param {HTMLCanvasElement} canvas
 * @param {HTMLElement|{clientWidth,scrollTop?,clientHeight?}} outer
 * @param {Array} good - streams already filtered (>1 point) & sorted
 * @param {Object} specsByKey - {key: spec} from /api/online/spectrogram/all
 * @param {number} [win=WV_WIN] - display time window in seconds (default 1800)
 * @param {boolean} [showSpec=true] - show the spectrogram strip below each row
 * @param {number} [nowSec] - "now" in epoch seconds (default Date.now()/1000).
 *        Used to pin the right edge to the SERVER clock (not the browser clock,
 *        which can skew) — same as scrttv, which aligns to data time, not local time.
 * @returns {{rowH:number, rowOrderKeys:string[], t0:number, WIN:number}}
 */
function renderWaveformCanvas(canvas, outer, good, specsByKey, win, showSpec, nowSec) {
  const WIN      = (win != null && win > 0) ? win : WV_WIN;
  const SHOW_SPEC = (showSpec !== false);
  if (!outer || !canvas || !good.length) return { rowH: 0, rowOrderKeys: [], t0: 0, WIN };

  const LW = WV_LW, RW = WV_RW;
  // ── DYNAMIC row height — few stations → tall rows (large, easy to read);
  // many stations → short rows (down to MIN, then scroll). Computed from the
  // panel height (outer.clientHeight). Modals/fakeOuter without clientHeight
  // → fall back to the fixed size (95px) as before. ─────────────────────────
  const MIN_ROW = 64, MAX_ROW = 170, DEF_ROW = 95, SEP = 1;
  const availH = (outer && typeof outer.clientHeight === 'number') ? outer.clientHeight : 0;
  let rowH = DEF_ROW;
  if (availH > 40 && good.length > 0) {
    rowH = Math.max(MIN_ROW, Math.min(MAX_ROW, Math.floor(availH / good.length)));
  }
  // Bagi rowH ke waveform (WH) + spectrogram (SH) + 1px pemisah, jaga rasio ~½.
  let SH, WH;
  if (SHOW_SPEC) {
    SH = Math.round((rowH - SEP) * (48 / 94));
    WH = rowH - SEP - SH;
  } else {
    SH = 0; WH = rowH - SEP;
  }
  const rowOrderKeys = good.map(s => s.key);

  const W  = outer.clientWidth || 600;
  const pW = W - LW - RW;
  const H  = good.length * rowH;

  // Canvas reuse: only reset the size when changed — avoids GPU backing-store
  // teardown (canvas.width=X always clears and reinits, costly every tick).
  if (canvas.width !== W || canvas.height !== H) {
    canvas.width  = W;
    canvas.height = H;
  }
  canvas.style.cssText = 'display:block;width:100%';

  const ctx = canvas.getContext('2d');

  // Waveform axis: anchored to the SERVER clock (nowSec, defaults to the browser clock) —
  // the window keeps moving forward each render; data latency only makes the right
  // end of the trace stop just short of the "now" line (correct behavior, like scrttv).
  // Horizontal span = WIN (the past) + WV_FUTURE_PAD (5 minutes of right headroom)
  // → the "now" line sits ~5 minutes from the box edge; the trace is not flush.
  const tNow     = (nowSec != null && nowSec > 0) ? nowSec : Date.now() / 1000;
  const FUTURE   = Math.round(WIN / 15);  // ~6.7% headroom, scales with the window
  const SPAN     = WIN + FUTURE;
  const t0       = tNow - WIN;
  const pxPerSec = pW / SPAN;
  const toPx     = t => LW + (t - t0) * pxPerSec;

  // Viewport culling: rows outside the visible area only need the background fill,
  // not the trace/spectrogram. For an outer without scrollTop (modal/fakeOuter),
  // treat every row as visible.
  const scrollTop = (typeof outer.scrollTop === 'number') ? outer.scrollTop : 0;
  const viewH     = (typeof outer.clientHeight === 'number' && outer.clientHeight > 0)
                    ? outer.clientHeight : H;

  // Clear the whole canvas once up front (faster than a fillRect per row)
  ctx.fillStyle = '#f8fafc';
  ctx.fillRect(0, 0, W, H);

  good.forEach((s, i) => {
    const wy0 = i * rowH;
    const wy1 = wy0 + WH;
    const wyM = wy0 + WH / 2;
    const spY0 = wy1;
    const spY1 = spY0 + SH;

    // Warna latar baris selang-seling (scrttv alternateBackground)
    if (i % 2 !== 0) {
      ctx.fillStyle = '#f1f5f9';
      ctx.fillRect(0, wy0, W, rowH);
    }

    // ── Faint time grid (scrttv gridPen alpha~32) — drawn before the trace ───
    ctx.save();
    ctx.beginPath(); ctx.rect(LW, wy0, pW, WH); ctx.clip();
    ctx.strokeStyle = 'rgba(0,0,32,0.07)'; ctx.lineWidth = 0.5; ctx.setLineDash([2,2]);
    const _gMin = Math.ceil(t0 / 60) * 60;
    const _minStp = WIN <= 300 ? 10 : 60;
    for (let gm = _gMin; gm <= tNow + FUTURE; gm += _minStp) {
      const gx = toPx(gm);
      ctx.beginPath(); ctx.moveTo(gx, wy0); ctx.lineTo(gx, wy1); ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(0,0,32,0.13)'; ctx.lineWidth = 0.6; ctx.setLineDash([]);
    const _majStp = WIN <= 300 ? 60 : WIN <= 900 ? 120 : 300;
    const _gMaj = Math.ceil(t0 / _majStp) * _majStp;
    for (let gm = _gMaj; gm <= tNow + FUTURE; gm += _majStp) {
      const gx = toPx(gm);
      ctx.beginPath(); ctx.moveTo(gx, wy0); ctx.lineTo(gx, wy1); ctx.stroke();
    }
    ctx.restore();

    // Station label (scrttv: bold text in a bg-backed box, left)
    const _lblSta = `${s.net}.${s.sta}`;
    const _lblCha = s.cha;
    ctx.font = 'bold 9px monospace';
    const _staW = ctx.measureText(_lblSta).width;
    ctx.font = '8px monospace';
    const _chaW = ctx.measureText(_lblCha).width;
    const _boxW = Math.max(_staW, _chaW) + 5;
    const _boxH = 27;
    const _boxY = wy0 + WH / 2 - _boxH / 2;
    ctx.fillStyle = (i % 2 === 0) ? '#f8fafc' : '#f1f5f9';
    ctx.fillRect(1, _boxY, _boxW + 2, _boxH);
    ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 0.5;
    ctx.strokeRect(1, _boxY, _boxW + 2, _boxH);
    ctx.textAlign = 'left';
    ctx.font = 'bold 9px monospace'; ctx.fillStyle = '#374151';
    ctx.fillText(_lblSta, 3, _boxY + 11);
    ctx.font = '8px monospace'; ctx.fillStyle = '#6b7280';
    ctx.fillText(_lblCha, 3, _boxY + 22);

    // Viewport culling
    const visMargin = rowH;
    if (wy1 < scrollTop - visMargin || wy0 > scrollTop + viewH + visMargin) {
      if (SHOW_SPEC) {
        ctx.fillStyle = '#eef2f6';
        ctx.fillRect(0, spY0, W, SH);
      }
      return;
    }

    // ── Waveform ──────────────────────────────────────────────────────────
    ctx.save();
    ctx.beginPath();
    ctx.rect(LW, wy0, pW, WH);
    ctx.clip();

    // Zero line — very faint, only a baseline reference
    ctx.strokeStyle = 'rgba(180,180,240,0.25)'; ctx.lineWidth = 0.4;
    ctx.beginPath(); ctx.moveTo(LW, wyM); ctx.lineTo(W - RW, wyM); ctx.stroke();

    const pts = s.points.filter(p => p.t >= t0 - 1 && p.t <= tNow + 1);

    // Amplitude label bottom-right (scrttv "amax: X" at the bottom-right of each row)
    if (pts.length > 1) {
      const _mean = pts.reduce((acc, p) => acc + p.v, 0) / pts.length;
      const _amax = pts.reduce((m, p) => Math.max(m, Math.abs(p.v - _mean)), 0);
      const _amStr = _amax >= 1e6 ? `${(_amax/1e6).toFixed(1)}M`
                   : _amax >= 1e3 ? `${(_amax/1e3).toFixed(1)}k`
                   : _amax.toFixed(0);
      ctx.font = '7px monospace'; ctx.fillStyle = 'rgba(100,116,139,0.7)';
      ctx.textAlign = 'right';
      ctx.fillText(`amax: ${_amStr}`, W - RW - 2, wy1 - 2);
      ctx.textAlign = 'left';
    }

    if (pts.length > 1) {
      ctx.strokeStyle = 'rgb(128,128,128)';
      ctx.lineWidth   = 1.0;
      _drawMinMaxTrace(ctx, pts, wyM, WH, toPx, pxPerSec, LW, pW);
    }

    // Pick overlay — DISTINGUISHED & EMPHASIZED between 2 sources:
    //  • scautopick (SeisComP STA/LTA, /api/online/trigger bridge): SOLID line,
    //    tag "SC", P=red / S=blue.
    //  • AI / PhaseNet (real-time detection, _realtime_pipeline.py): DASHED line,
    //    tag "AI", P=orange / S=purple.
    // Each pick has a phase badge (white letter in a colored box) + a source label.
    (s.picks || []).forEach(pk => {
      if (pk.t < t0 || pk.t > tNow) return;
      const px = toPx(pk.t);
      const isP  = (pk.phase || 'P').toUpperCase().startsWith('P');
      const isAI = pk.source === 'phasenet';
      const col  = isAI ? (isP ? '#ea580c' : '#9333ea') : (isP ? '#dc2626' : '#2563eb');
      // Garis pick (solid=scautopick, dashed=AI), full tinggi waveform
      ctx.strokeStyle = col; ctx.lineWidth = 2.2;
      if (isAI) ctx.setLineDash([5, 3]);
      ctx.beginPath(); ctx.moveTo(px, wy0 + 1); ctx.lineTo(px, wy1 - 1); ctx.stroke();
      ctx.setLineDash([]);
      // Badge fase (P/S) — kotak warna, huruf putih
      const lbl = pk.phase || (isP ? 'P' : 'S');
      const bw = 11, bh = 11;
      ctx.fillStyle = col; ctx.fillRect(px + 1, wy0 + 1, bw, bh);
      ctx.fillStyle = '#fff'; ctx.font = 'bold 9px monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(lbl, px + 1 + bw / 2, wy0 + 1 + bh / 2 + 0.5);
      // Label sumber: "AI" atau "SC" di samping badge
      ctx.fillStyle = col; ctx.font = 'bold 7px monospace';
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillText(isAI ? 'AI' : 'SC', px + bw + 3, wy0 + 9);
    });

    ctx.restore();

    // ── Spectrogram (below the waveform, only when SHOW_SPEC) ──────────────
    if (SHOW_SPEC) {
      // Slightly white background (light gray) — not dark slate — to blend with
      // the panel's light theme; empty spectrogram areas stay low-contrast.
      ctx.fillStyle = '#eef2f6'; ctx.fillRect(0, spY0, W, SH);
      const spec = specsByKey?.[s.key];
      ctx.save(); ctx.beginPath(); ctx.rect(LW, spY0, pW, SH); ctx.clip();
      if (spec && spec.freqs?.length) {
        const { freqs, times, Sxx, vmin, vmax } = spec;
        const nf = freqs.length, nt = times.length;
        const range = vmax - vmin || 1;
        for (let ti = 0; ti < nt; ti++) {
          const x0c = toPx(times[ti]);
          const x1c = ti + 1 < nt ? toPx(times[ti + 1]) : x0c + 2;
          if (x1c < LW || x0c > W - RW) continue;
          for (let fi = 0; fi < nf; fi++) {
            const val = Math.max(0, Math.min(1, (Sxx[fi][ti] - vmin) / range));
            const [r, g, b] = _wvViridis(val);
            ctx.fillStyle = `rgb(${r},${g},${b})`;
            const cellH = SH / nf;
            const y = spY0 + SH - (fi + 1) * cellH;
            ctx.fillRect(x0c, y, Math.max(1, x1c - x0c) + 0.5, Math.ceil(cellH) + 0.5);
          }
        }
        ctx.font = '7px monospace'; ctx.fillStyle = '#94a3b8'; ctx.textAlign = 'left';
        ctx.fillText('spec', LW + 2, spY0 + SH - 2);
      } else if (!spec) {
        ctx.font = '8px monospace'; ctx.fillStyle = '#94a3b8'; ctx.textAlign = 'left';
        ctx.fillText('spec…', LW + 4, spY0 + SH / 2 + 3);
      }
      ctx.restore();
      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(0, spY1); ctx.lineTo(W, spY1); ctx.stroke();
    } else {
      ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(0, wy1); ctx.lineTo(W, wy1); ctx.stroke();
    }
  });

  // ── Red "now" line ACROSS all rows (waveform 1 → N) ───────────────────────
  // Drawn ONCE after all rows, full-height, so the user easily sees the current
  // time position across the whole station column. It sits ~5 minutes from the
  // right edge due to WV_FUTURE_PAD (headroom). Not clipped per row.
  const nowX = toPx(tNow);
  if (nowX >= LW && nowX <= W - RW) {
    ctx.save();
    ctx.strokeStyle = 'rgba(239,68,68,.9)'; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(nowX, 0); ctx.lineTo(nowX, H); ctx.stroke();
    // Label "now" kecil di atas
    ctx.fillStyle = '#ef4444'; ctx.font = 'bold 8px monospace'; ctx.textAlign = 'left';
    ctx.fillText('now', nowX + 3, 9);
    ctx.restore();
  }

  return { rowH, rowOrderKeys, t0, WIN };
}

/**
 * Draw the time axis (hour:minute + red "now" line) on a SEPARATE canvas that
 * stays fixed in position (a sibling outside the scroll container). t0 is
 * computed FRESH from Date.now() on every call (not cached from the waveform
 * draw) — so when called every 1s (see its setInterval caller), the whole axis
 * keeps "flowing" forward with the system clock, not just the "now" line
 * moving over a static grid. WIN must match what renderWaveformCanvas uses
 * (WV_WIN, 30 minutes) so it stays horizontally aligned with the waveform column above it.
 * @param {HTMLCanvasElement} axisCanvas
 * @param {number} width - must match the waveform canvas's outer.clientWidth
 * @param {number} [nowSec] - "now" in epoch seconds (default the browser clock) — must
 *        match the nowSec used by renderWaveformCanvas to stay aligned.
 */
/**
 * @param {HTMLCanvasElement} axisCanvas
 * @param {number} width
 * @param {number} [nowSec]
 * @param {number} [win] - window size in seconds; default WV_WIN. Must match
 *        the win passed to renderWaveformCanvas so axis aligns with the canvas.
 */
function renderTimeAxis(axisCanvas, width, nowSec, win) {
  if (!axisCanvas || !width) return;
  const LW = WV_LW, RW = WV_RW, H = 30;
  const WIN    = (win != null && win > 0) ? win : WV_WIN;
  const FUTURE = Math.round(WIN / 15);   // identical to renderWaveformCanvas
  const SPAN   = WIN + FUTURE;
  const W = width;
  const pW = W - LW - RW;

  if (axisCanvas.width !== W || axisCanvas.height !== H) {
    axisCanvas.width  = W;
    axisCanvas.height = H;
  }
  axisCanvas.style.cssText = 'display:block;width:100%';

  const tNow = (nowSec != null && nowSec > 0) ? nowSec : Date.now() / 1000;
  const tEnd = tNow + FUTURE;
  const t0   = tNow - WIN;
  const ctx  = axisCanvas.getContext('2d');
  ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);
  const toPx = t => LW + ((t - t0) / SPAN) * pW;
  const pad2 = n => String(n).padStart(2, '0');

  ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(LW, 1); ctx.lineTo(W - RW, 1); ctx.stroke();

  // Minor ticks
  const minStep  = WIN <= 300 ? 10 : 60;
  const firstMin = Math.ceil(t0 / minStep) * minStep;
  ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 0.6;
  for (let tm = firstMin; tm <= tEnd; tm += minStep) {
    const x = toPx(tm);
    ctx.beginPath(); ctx.moveTo(x, 1); ctx.lineTo(x, 6); ctx.stroke();
  }

  // Major ticks + labels (adaptive label format per window size)
  const majStep  = WIN <= 300 ? 60 : WIN <= 900 ? 120 : 300;
  const firstMaj = Math.ceil(t0 / majStep) * majStep;
  const showSec  = WIN <= 900;  // show seconds for windows ≤ 15 minutes
  ctx.font = 'bold 9px monospace'; ctx.fillStyle = '#475569'; ctx.textAlign = 'center';
  for (let tm = firstMaj; tm <= tEnd; tm += majStep) {
    const x = toPx(tm);
    ctx.strokeStyle = '#94a3b8'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 1); ctx.lineTo(x, 10); ctx.stroke();
    const d = new Date(tm * 1000);
    ctx.fillText(showSec
      ? `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`
      : `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`,
      x, H - 6);
  }
  ctx.textAlign = 'left';

  // Garis merah "now" + label HH:MM:SS UTC
  const nowX = toPx(tNow);
  ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(nowX, 0); ctx.lineTo(nowX, H); ctx.stroke();
  const dn = new Date(tNow * 1000);
  ctx.font = 'bold 8px monospace'; ctx.fillStyle = '#ef4444'; ctx.textAlign = 'right';
  ctx.fillText(`${pad2(dn.getUTCHours())}:${pad2(dn.getUTCMinutes())}:${pad2(dn.getUTCSeconds())}`, nowX - 3, 9);
  ctx.textAlign = 'left';
}

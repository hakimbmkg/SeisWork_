class VelestModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.modJobId = null;
    this.rmsJobId = null;
  }

  // --- Controller (upload/reset custom velocity model, called from index.html) ---
  async uploadModel(file) {
    if (!file) return;
    const stat = document.getElementById('vel-model-status');
    const pathEl = document.getElementById('vel-vp-model-path');
    if (stat) stat.textContent = 'Uploading…';
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await fetch('/api/upload/velocity_model', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.path) {
        if (pathEl) pathEl.value = d.path;
        if (stat) stat.textContent = `✓ ${d.filename}  (${d.lines || 0} baris)`;
        // Reset file input so same file can be re-uploaded
        const fi = document.getElementById('vel-vp-model-file');
        if (fi) fi.value = '';
      } else {
        if (stat) stat.textContent = `✗ Upload failed: ${d.error || 'unknown'}`;
      }
    } catch (e) {
      if (stat) stat.textContent = `✗ ${e.message}`;
    }
  }

  clearModel() {
    const pathEl = document.getElementById('vel-vp-model-path');
    const stat   = document.getElementById('vel-model-status');
    if (pathEl) pathEl.value = '';
    if (stat)   stat.textContent = 'Reset ke Halmahera 1D (default)';
    const fi = document.getElementById('vel-vp-model-file');
    if (fi) fi.value = '';
    setTimeout(() => { if (stat) stat.textContent = ''; }, 2000);
  }

  // --- Controller (velocity model plot, called from a job-card onclick) ---
  async showModelPlot(jobId) {
    this.modJobId = jobId;
    const card = document.getElementById('vel-mod-plot-card');
    if (!card) return;
    card.style.display = '';
    const titleEl = document.getElementById('vel-mod-plot-title');
    if (titleEl) titleEl.textContent = `Job: ${jobId.slice(0, 8)}…`;
    try {
      const d = await (await fetch(`/api/pipeline/jobs/${jobId}/velocity_model`)).json();
      if (d.error) { if (titleEl) titleEl.textContent = d.error; return; }
      this.drawModel(d);
      if (titleEl) {
        const hasOut = !!d.output;
        titleEl.textContent = `${(d.output || d.initial || {}).title || ''} · Job: ${jobId.slice(0,8)}…`;
      }
      // Scroll into view
      card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
      if (titleEl) titleEl.textContent = `Error: ${e.message}`;
    }
  }

  closeModelPlot() {
    const card = document.getElementById('vel-mod-plot-card');
    if (card) card.style.display = 'none';
    this.modJobId = null;
  }

  // --- Controller (VELEST RMS convergence plot, called from a job-card onclick) ---
  async showRmsPlot(jobId) {
    this.rmsJobId = jobId;
    const card = document.getElementById('vel-rms-plot-card');
    if (!card) return;
    card.style.display = '';
    const titleEl = document.getElementById('vel-rms-plot-title');
    if (titleEl) titleEl.textContent = `Job: ${jobId}`;
    try {
      const d = await (await fetch(`/api/pipeline/jobs/${jobId}/velest_rms`)).json();
      if (d.error) { if (titleEl) titleEl.textContent = `Error: ${d.error}`; return; }
      if (!d.iterations || d.iterations.length === 0) {
        if (titleEl) titleEl.textContent = 'No iteration data in main.OUT';
        return;
      }
      this.drawRms(d.iterations);
      if (titleEl) titleEl.textContent = `${d.n} iterations · Final RMS: ${d.iterations[d.n-1].rms.toFixed(4)} s`;
    } catch (e) {
      if (titleEl) titleEl.textContent = `Error: ${e.message}`;
    }
  }

  closeRmsPlot() {
    const card = document.getElementById('vel-rms-plot-card');
    if (card) card.style.display = 'none';
    this.rmsJobId = null;
  }

  // --- View (canvas render) ---
  drawRms(iters) {
    const canvas = document.getElementById('vel-rms-canvas');
    if (!canvas) return;
    const W = canvas.offsetWidth || 500;
    const H = Math.round(W * 0.38);
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);

    const PAD = { top: 28, bot: 36, left: 58, right: 20 };
    const PW = W - PAD.left - PAD.right;
    const PH = H - PAD.top  - PAD.bot;

    // Skip iter 0 (initial RMS often huge; distorts scale); plot iter 1..N
    const pts = iters.filter(d => d.iter > 0);
    if (pts.length === 0) return;

    const xs = pts.map(d => d.iter);
    const ys = pts.map(d => d.rms);
    const xMin = xs[0], xMax = xs[xs.length - 1];
    const yMin = Math.min(...ys) * 0.92;
    const yMax = Math.max(...ys) * 1.08;

    const xAt = i  => PAD.left + (xs[i] - xMin) / Math.max(xMax - xMin, 1) * PW;
    const yAt = v  => PAD.top  + (1 - (v - yMin) / (yMax - yMin)) * PH;

    // Background grid
    ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1;
    const nTick = 5;
    for (let i = 0; i <= nTick; i++) {
      const v = yMin + (yMax - yMin) * i / nTick;
      const y = yAt(v);
      ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(PAD.left + PW, y); ctx.stroke();
      ctx.fillStyle = '#94a3b8'; ctx.font = '9px sans-serif'; ctx.textAlign = 'right';
      ctx.fillText(v.toFixed(3), PAD.left - 5, y + 3);
    }
    // X ticks
    ctx.fillStyle = '#94a3b8'; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
    xs.forEach((x, i) => {
      const px = xAt(i);
      ctx.beginPath(); ctx.strokeStyle = '#1e293b'; ctx.moveTo(px, PAD.top); ctx.lineTo(px, PAD.top + PH); ctx.stroke();
      ctx.fillText(x, px, PAD.top + PH + 13);
    });

    // Line
    ctx.beginPath(); ctx.strokeStyle = '#a78bfa'; ctx.lineWidth = 2;
    pts.forEach((d, i) => {
      const px = xAt(i), py = yAt(d.rms);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();

    // Dots + value labels
    pts.forEach((d, i) => {
      const px = xAt(i), py = yAt(d.rms);
      ctx.beginPath(); ctx.fillStyle = '#a78bfa'; ctx.arc(px, py, 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.fillStyle = '#e2e8f0'; ctx.font = '8px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText(d.rms.toFixed(3), px, py - 7);
    });

    // Axes labels
    ctx.fillStyle = '#94a3b8'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('Iteration', PAD.left + PW / 2, H - 4);
    ctx.save(); ctx.translate(12, PAD.top + PH / 2); ctx.rotate(-Math.PI / 2);
    ctx.fillText('RMS Residual (s)', 0, 0); ctx.restore();

    // Title
    ctx.fillStyle = '#c4b5fd'; ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('VELEST RMS Residual per Iteration', PAD.left, 14);
  }

  drawModel(data) {
    const canvas = document.getElementById('vel-mod-canvas');
    if (!canvas) return;

    // Collect all layers
    const ini  = data.initial || {};
    const out  = data.output  || {};
    const iniP = ini.p || [];
    const iniS = ini.s || [];
    const outP = out.p || [];
    const outS = out.s || [];

    // Determine depth and velocity ranges
    const allDepths = [...iniP, ...iniS, ...outP, ...outS].map(l => l.depth);
    const allVels   = [...iniP, ...iniS, ...outP, ...outS].map(l => l.vel);
    const dMin = Math.min(...allDepths, -5);
    const dMax = Math.max(...allDepths,  65);
    const vMin = Math.max(0, Math.min(...allVels) - 0.5);
    const vMax = Math.min(10, Math.max(...allVels) + 0.5);

    // Canvas sizing
    const DPR  = window.devicePixelRatio || 1;
    const W    = canvas.offsetWidth || 560;
    const H    = Math.round(W * 0.52);
    canvas.width  = W * DPR;
    canvas.height = H * DPR;
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.scale(DPR, DPR);

    // Fixed-size fonts — do not scale with canvas width to prevent overflow
    const FONT_LBL   = '9px sans-serif';
    const FONT_TITLE = 'bold 10px sans-serif';
    const FONT_UNIT  = '8px sans-serif';

    // Layout — right=20 so right panel ticks are not clipped
    const PAD  = { top: 26, bot: 34, left: 44, right: 20 };
    const GAP  = 16;               // gap between two panels
    const pw   = (W - PAD.left - PAD.right - GAP) / 2;
    const ph   = H - PAD.top - PAD.bot;

    // Panel origins (top-left of plot area)
    const PX   = [PAD.left, PAD.left + pw + GAP];
    const PY   = PAD.top;

    // ── Background ──────────────────────────────────────────────────────────────
    ctx.fillStyle = '#0a0f1e';
    ctx.fillRect(0, 0, W, H);

    // ── Helper: convert data → canvas coords ────────────────────────────────────
    function xc(vel, panelIdx) {
      return PX[panelIdx] + ((vel - vMin) / (vMax - vMin)) * pw;
    }
    function yc(depth) {
      return PY + ((depth - dMin) / (dMax - dMin)) * ph;
    }

    // ── Grid + axes ─────────────────────────────────────────────────────────────
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth   = 1;
    // Depth grid (horizontal)
    const depthTicks = [];
    for (let d = Math.ceil(dMin / 10) * 10; d <= dMax; d += 10) depthTicks.push(d);
    depthTicks.forEach(d => {
      const y = yc(d);
      PX.forEach((px, pi) => {
        ctx.beginPath(); ctx.moveTo(px, y); ctx.lineTo(px + pw, y); ctx.stroke();
      });
    });
    // Velocity grid (vertical)
    const velTicks = [];
    for (let v = Math.ceil(vMin); v <= vMax; v++) velTicks.push(v);
    velTicks.forEach(v => {
      PX.forEach((px, pi) => {
        const x = xc(v, pi);
        ctx.beginPath(); ctx.moveTo(x, PY); ctx.lineTo(x, PY + ph); ctx.stroke();
      });
    });

    // ── Panel borders ────────────────────────────────────────────────────────────
    ctx.strokeStyle = '#334155';
    ctx.lineWidth   = 1;
    PX.forEach(px => {
      ctx.strokeRect(px, PY, pw, ph);
    });

    // ── Panel titles ─────────────────────────────────────────────────────────────
    ctx.fillStyle = '#94a3b8';
    ctx.font = FONT_TITLE;
    ctx.textAlign = 'center';
    ctx.fillText('P-wave velocity', PX[0] + pw / 2, PY - 8);
    ctx.fillText('S-wave velocity', PX[1] + pw / 2, PY - 8);

    // ── Axis labels ──────────────────────────────────────────────────────────────
    ctx.fillStyle = '#64748b';
    ctx.font = FONT_LBL;
    ctx.textAlign = 'center';
    // X-axis tick labels (velocity) — above bottom edge, within canvas
    velTicks.forEach(v => {
      PX.forEach((px, pi) => {
        ctx.fillText(v.toFixed(0), xc(v, pi), PY + ph + 13);
      });
    });
    // Shared depth tick labels (left of panel 0)
    ctx.textAlign = 'right';
    depthTicks.forEach(d => {
      ctx.fillText(d, PX[0] - 5, yc(d) + 3);
    });
    // X-axis unit label — within bottom padding, does not overflow canvas
    ctx.font = FONT_UNIT;
    ctx.fillStyle = '#475569';
    ctx.textAlign = 'center';
    ctx.fillText('km/s', PX[0] + pw / 2, PY + ph + 26);
    ctx.fillText('km/s', PX[1] + pw / 2, PY + ph + 26);
    // Y-axis unit label (rotated) — sufficient space from left
    ctx.save();
    ctx.font = FONT_UNIT;
    ctx.fillStyle = '#475569';
    ctx.translate(8, PY + ph / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Depth (km)', 0, 0);
    ctx.restore();

    // ── Draw step-function profile ───────────────────────────────────────────────
    function _drawProfile(ctx, layers, panelIdx, color, dashed) {
      if (!layers || layers.length === 0) return;
      ctx.save();
      ctx.beginPath();
      ctx.rect(PX[panelIdx], PY, pw, ph);
      ctx.clip();
      ctx.strokeStyle = color;
      ctx.lineWidth   = dashed ? 1.5 : 2;
      if (dashed) ctx.setLineDash([5, 4]);
      else        ctx.setLineDash([]);
      ctx.beginPath();
      for (let i = 0; i < layers.length; i++) {
        const vel   = layers[i].vel;
        const depth = layers[i].depth;
        const nextD = (i + 1 < layers.length) ? layers[i + 1].depth : dMax + 5;
        const x = xc(vel, panelIdx);
        const y1 = yc(depth);
        const y2 = yc(nextD);
        if (i === 0) ctx.moveTo(x, y1); else ctx.lineTo(x, y1);
        ctx.lineTo(x, y2);
      }
      ctx.stroke();
      ctx.restore();
    }

    // Initial model (grey dashed)
    _drawProfile(ctx, iniP, 0, '#64748b', true);
    _drawProfile(ctx, iniS, 1, '#64748b', true);

    // VELEST output (colored solid) — only if output exists
    if (outP.length) _drawProfile(ctx, outP, 0, '#3b82f6', false);
    if (outS.length) _drawProfile(ctx, outS, 1, '#f43f5e', false);

    // ── Layer depth markers (dots) ────────────────────────────────────────────────
    function _drawDots(ctx, layers, panelIdx, color) {
      if (!layers || !layers.length) return;
      ctx.save();
      ctx.beginPath();
      ctx.rect(PX[panelIdx], PY, pw, ph);
      ctx.clip();
      ctx.fillStyle = color;
      layers.forEach(({ vel, depth }) => {
        const x = xc(vel, panelIdx), y = yc(depth);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    }
    if (outP.length) _drawDots(ctx, outP, 0, '#3b82f6');
    if (outS.length) _drawDots(ctx, outS, 1, '#f43f5e');
    _drawDots(ctx, iniP, 0, '#475569');
    _drawDots(ctx, iniS, 1, '#475569');

    // ── Legend update ─────────────────────────────────────────────────────────────
    const leg = document.getElementById('vel-mod-legend');
    if (leg) {
      const hasOut = outP.length > 0 || outS.length > 0;
      leg.style.display = '';
      if (!hasOut) {
        leg.innerHTML = `<span style="color:#64748b">Initial model (Halmahera 1D / custom) — no VELEST output yet</span>`;
      }
    }
  }
}

const VEL = new VelestModule(SW);

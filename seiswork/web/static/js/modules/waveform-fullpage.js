/**
 * waveform-fullpage.js — by HakimBMKG
 * Dedicated full-page zoom view for the live waveform+spectrogram.
 * Uses the same snapshot/delta approach as the dashboard (online-monitor.js) —
 * it does not use /api/online/waveform/all (which fetches the full payload every 2s).
 * The spectrogram is fetched separately (5s) so it never blocks waveform rendering.
 */
(() => {
  // ── State ──────────────────────────────────────────────────────────────────
  const _bufs        = new Map();   // key → {pts, picks, meta, cursor}
  let   _snapDone     = false;      // initial snapshot done?
  let   _sessionEpoch = null;       // server-restart detection
  let   _serverOffset = 0;          // server clock − browser clock (axis anchor)
  let   _lastSpecs   = {};
  let   _lastGood    = null;
  let   rowOrderKeys = [];
  let   rowH         = 0;
  // View toggles (spectrogram strip on/off, DeepDenoiser overlay on/off).
  let   _showSpec      = true;
  let   _showDenoise   = false;
  const _denoisedByKey = {};
  let   _denoiseTimer  = null;
  // Pick store = the AUTHORITATIVE pick source ('NET.STA' key), filled from the
  // /api/online/picks/recent pick log — same as the dashboard, rather than the
  // fragile per-stream buf.picks deltas.
  const _pickStore   = new Map();
  let   _pickSince    = 0.0;

  // "Now" per the server clock (axis anchor) — the 'misaligned plot' fix.
  function nowSec() { return Date.now() / 1000 + (_serverOffset || 0); }
  function updateServerOffset(serverTime) {
    if (!serverTime) return;
    const off = serverTime - Date.now() / 1000;
    _serverOffset = _serverOffset ? _serverOffset * 0.8 + off * 0.2 : off;
  }
  function buildCursors() {
    const c = {};
    for (const [key, buf] of _bufs) if (buf.cursor != null) c[key] = buf.cursor;
    return c;
  }
  function segmentsToPts(segments, into) {
    for (const seg of (segments || [])) {
      const step = seg.step > 0 ? seg.step : 1.0;
      const vs = seg.vs || [];
      for (let i = 0; i < vs.length; i++) into.push({ t: seg.t0 + i * step, v: vs[i] });
    }
  }

  // ── Fetch helper with a timeout ────────────────────────────────────────────
  function _fetchTimeout(url, ms, opts) {
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), ms);
    return fetch(url, { ...(opts || {}), signal: ctrl.signal })
      .then(r => { clearTimeout(tid); return r.json(); })
      .catch(e => { clearTimeout(tid); throw e; });
  }

  // ── Visible keys for spectrogram lazy-loading ──────────────────────────────
  function visibleKeys() {
    const outer = document.getElementById('wf-plot');
    if (!outer || !rowOrderKeys.length || !rowH) return rowOrderKeys;
    const first = Math.max(0, Math.floor(outer.scrollTop / rowH) - 1);
    const last  = Math.min(rowOrderKeys.length - 1,
                            Math.ceil((outer.scrollTop + outer.clientHeight) / rowH) + 1);
    return rowOrderKeys.slice(first, last + 1);
  }

  // ── Apply snapshot (identical to online-monitor.js _applySnapshot) ─────────
  function applySnapshot(snap) {
    _bufs.clear();
    for (const s of (snap.streams || [])) {
      const pts = [];
      segmentsToPts(s.segments, pts);
      if (!pts.length) continue;
      _bufs.set(s.key, {
        pts, picks: s.picks || [],
        meta: { net: s.net, sta: s.sta, loc: s.loc, cha: s.cha, sr: s.sr },
        cursor: (s.last_t != null) ? s.last_t : pts[pts.length - 1].t,
      });
    }
    _snapDone     = true;
    _sessionEpoch = (snap.session_epoch != null) ? snap.session_epoch : _sessionEpoch;
    updateServerOffset(snap.server_time);
  }

  // ── Apply delta (identical to online-monitor.js _applyDelta) ───────────────
  function applyDelta(delta) {
    if (delta.session_epoch != null && _sessionEpoch != null
        && delta.session_epoch !== _sessionEpoch) {
      _bufs.clear(); _pickStore.clear(); _pickSince = 0.0;
      _snapDone = false; _sessionEpoch = null; return;
    }
    const MAX_PTS = 4200;     // envelope min/max ~2 titik/dtk × 1800s + headroom
    const cutoff  = nowSec() - 1800;
    for (const s of (delta.streams || [])) {
      let buf = _bufs.get(s.key);
      if (!buf) {
        buf = { pts: [], picks: [], meta: { net: s.net, sta: s.sta, loc: s.loc, cha: s.cha, sr: s.sr },
                cursor: null };
        _bufs.set(s.key, buf);
      }
      segmentsToPts(s.segments_new, buf.pts);
      if (s.last_t != null) buf.cursor = s.last_t;
      if (buf.pts.length > MAX_PTS) buf.pts.splice(0, buf.pts.length - MAX_PTS);
      if (s.picks_new?.length > 0) {
        const seen = new Set(buf.picks.map(p => `${Number(p.t).toFixed(1)}_${p.phase}`));
        for (const p of s.picks_new) {
          const k = `${Number(p.t).toFixed(1)}_${p.phase}`;
          if (!seen.has(k)) { buf.picks.push(p); seen.add(k); }
        }
        buf.picks = buf.picks.filter(p => p.t >= cutoff);
      }
    }
    updateServerOffset(delta.server_time);
  }

  // ── Convert buf → streams for renderWaveformCanvas ────────────────────────
  function bufsToStreams() {
    const out = [];
    for (const [key, buf] of _bufs) {
      const m = buf.meta;
      const storePicks = _pickStore.get(`${m.net}.${m.sta}`) || [];
      const picks = storePicks.length ? storePicks : (buf.picks || []);
      out.push({ key, net: m.net, sta: m.sta, loc: m.loc, cha: m.cha,
                 points: buf.pts, picks });
    }
    return out;
  }

  // ── Poll the pick log → fill the pick store (key NET.STA) ───────────────────
  async function pollPickLog() {
    try {
      const data = await _fetchTimeout(
        `/api/online/picks/recent?since=${_pickSince}&n=100`, 5000);
      const picks = data.picks || [];
      if (!picks.length) return;
      _pickSince = Math.max(...picks.map(p => p.t));
      const cutoff = nowSec() - 1800;
      for (const p of picks) {
        const k = `${p.net}.${p.sta}`;
        let arr = _pickStore.get(k);
        if (!arr) { arr = []; _pickStore.set(k, arr); }
        const dk = `${Number(p.t).toFixed(1)}_${p.phase}`;
        if (!arr.some(x => `${Number(x.t).toFixed(1)}_${x.phase}` === dk))
          arr.push({ t: p.t, phase: p.phase, source: p.source });
        if (arr.length > 200) {
          const f = arr.filter(x => x.t >= cutoff);
          _pickStore.set(k, f.length ? f : arr.slice(-50));
        }
      }
    } catch (_) { /* transient */ }
  }

  // ── Draw ───────────────────────────────────────────────────────────────────
  function draw(good, specsByKey) {
    const canvas = document.getElementById('wf-canvas');
    const outer  = document.getElementById('wf-plot');
    const streams = _showDenoise ? good.map(s => {
      const pts = _denoisedByKey[s.key];
      return (pts && pts.length > 1) ? Object.assign({}, s, { points: pts }) : s;
    }) : good;
    const r = renderWaveformCanvas(canvas, outer, streams, specsByKey,
                                   undefined, _showSpec, nowSec());
    rowH = r.rowH; rowOrderKeys = r.rowOrderKeys;
    redrawAxis();
  }

  function redrawAxis() {
    const outer = document.getElementById('wf-plot');
    const ax    = document.getElementById('wf-axis');
    if (!outer || !ax) return;
    renderTimeAxis(ax, outer.clientWidth || 600, nowSec());
  }

  // ── Tick waveform (2s) ─────────────────────────────────────────────────────
  async function tick() {
    const wasSnapshot = !_snapDone;
    const wfPromise = wasSnapshot
      ? _fetchTimeout('/api/online/waveform/snapshot', 5000)
      : _fetchTimeout('/api/online/waveform/delta', 5000, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cursors: buildCursors() }),
        });
    try {
      const [st, wfData] = await Promise.all([
        _fetchTimeout('/api/online/status', 5000),
        wfPromise,
      ]);
      const statusEl = document.getElementById('wf-status');
      // Never show the raw SeedLink host:port here either — same reasoning
      // as the dashboard's _addrBadge (online-monitor.js): this page can be
      // opened in its own tab/window and shared/screenshotted independently.
      if (st.connected) {
        statusEl.textContent = `Live · ${st.n_packets} pkt`;
      } else {
        statusEl.textContent = st.error ? `Lost: ${st.error}` : 'No active live session';
      }
      if (wasSnapshot) { applySnapshot(wfData); } else { applyDelta(wfData); }
      const streams = bufsToStreams();
      const good = streams.filter(s => s.points.length > 1);
      good.sort((a, b) => (a.net + a.sta + a.cha) < (b.net + b.sta + b.cha) ? -1 : 1);
      if (!good.length) return;
      _lastGood = good;
      rowOrderKeys = good.map(s => s.key);
      rowH = 46 + 48 + 1;
      draw(good, _lastSpecs);
      document.getElementById('wf-loading').style.display = 'none';
    } catch (_) { /* transient */ }
  }

  // ── Tick spectrogram (5s, terpisah) ───────────────────────────────────────
  async function tickSpec() {
    if (!_showSpec) return;                 // spectrogram view is off
    if (!_lastGood) return;
    const keys = visibleKeys();
    if (!keys.length) return;
    try {
      const sp = await _fetchTimeout(
        `/api/online/spectrogram/all?window=1800&keys=${encodeURIComponent(keys.join(','))}`,
        6000
      );
      const merged = Object.assign({}, _lastSpecs);
      (sp.specs || []).forEach(s => { merged[s.key] = s; });
      _lastSpecs = merged;
      if (_lastGood) draw(_lastGood, merged);
    } catch (_) { /* transient */ }
  }

  // ── Tick denoise (DeepDenoiser overlay, visible rows only) ─────────────────
  async function tickDenoise() {
    if (!_showDenoise || !_lastGood) return;
    const keys = visibleKeys();
    if (!keys.length) return;
    const st = document.getElementById('wf-denoise-status');
    try {
      const j = await _fetchTimeout(
        `/api/online/waveform/denoised?window=1800&keys=${encodeURIComponent(keys.join(','))}`,
        15000);
      if (!j.ready) { if (st) st.textContent = j.loading ? 'loading model…' : 'n/a'; return; }
      Object.entries(j.denoised || {}).forEach(([k, pts]) => { _denoisedByKey[k] = pts; });
      if (st) st.textContent = 'on';
      if (_lastGood) draw(_lastGood, _lastSpecs);
    } catch (e) { if (st) st.textContent = (e.name === 'AbortError') ? 'timeout' : 'error'; }
  }

  // ── View-toggle wiring (checkboxes in the topbar) ──────────────────────────
  const _specTog = document.getElementById('wf-spec-tog');
  if (_specTog) _specTog.addEventListener('change', () => {
    _showSpec = _specTog.checked;
    if (_lastGood) draw(_lastGood, _lastSpecs);
    if (_showSpec) tickSpec();
  });
  const _dnTog = document.getElementById('wf-denoise-tog');
  if (_dnTog) _dnTog.addEventListener('change', () => {
    _showDenoise = _dnTog.checked;
    clearInterval(_denoiseTimer);
    const st = document.getElementById('wf-denoise-status');
    if (_showDenoise) {
      if (st) st.textContent = 'loading…';
      tickDenoise();
      _denoiseTimer = setInterval(tickDenoise, 6000);
    } else {
      _denoiseTimer = null;
      for (const k of Object.keys(_denoisedByKey)) delete _denoisedByKey[k];
      if (st) st.textContent = '';
      if (_lastGood) draw(_lastGood, _lastSpecs);
    }
  });

  // ── Init ───────────────────────────────────────────────────────────────────
  let scrollDebounce = null;
  document.getElementById('wf-plot').addEventListener('scroll', () => {
    clearTimeout(scrollDebounce);
    scrollDebounce = setTimeout(() => { tickSpec(); if (_showDenoise) tickDenoise(); }, 200);
  });

  tick();
  tickSpec();
  pollPickLog();
  setInterval(tick,       2000);
  setInterval(redrawAxis, 1000);
  setInterval(tickSpec,   5000);
  setInterval(pollPickLog, 2000);
})();

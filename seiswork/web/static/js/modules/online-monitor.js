/**
 * online-monitor.js — by HakimBMKG
 * Tab "Online Project": wizard 3-step SeisWork ⇄ SeisComP.
 *
 * Step 1 (New Config): Import inventory XML → preview map → save as an online cfg_id.
 * Step 2 (Sync): Load config → fill in agent+seedlink → sync SeisComP via the agent HTTP API.
 * Step 3 (Monitor): Connect to seedlink (local SeisComP or a direct remote) → live waveform.
 *
 * Buffer: 1 day (86400s) in-memory on the server, no SDS archive.
 */
const _DIR_FULL = {
  'U':'Utara','TL':'Timur Laut','T':'Timur','TG':'Tenggara',
  'S':'Selatan','BD':'Barat Daya','B':'Barat','BL':'Barat Laut',
  'UTL':'Utara-Timur Laut','TTL':'Timur-Timur Laut',
  'SBD':'Selatan-Barat Daya','BBD':'Barat-Barat Daya',
  'BBL':'Barat-Barat Laut','UBL':'Utara-Barat Laut',
};
function _dirFull(d) { return _DIR_FULL[d] || d; }
function _ncText(nc) {
  if (!nc || nc.dist_km == null) return '';
  return `${Math.round(nc.dist_km)} km ${_dirFull(nc.direction)} ${nc.city}`;
}

// UTC epoch (seconds) from an event datetime string (UTC naive) — parsed the SAME as _TZ.fmt.
function _eventEpoch(isoStr) {
  if (!isoStr) return NaN;
  const d = new Date(String(isoStr).replace(' ', 'T').replace(/([+-]\d{2}:\d{2}|Z)$/, '') + 'Z');
  return d.getTime() / 1000;
}

// "Time since the quake" (elapsed since Origin Time) relative to nowSec
// (leading data edge). Format: "2h 5m" / "13m" / "—".
function _sinceQuakeStr(isoStr, nowSec) {
  const ot = _eventEpoch(isoStr);
  if (isNaN(ot) || !nowSec) return '';
  let ds = nowSec - ot;
  if (ds < 0) ds = 0;
  const h = Math.floor(ds / 3600);
  const m = Math.floor((ds % 3600) / 60);
  if (h > 0)  return `${h}h ${m}m`;
  if (m > 0)  return `${m} mnt`;
  return `${Math.floor(ds)} dtk`;
}

// Bold colored badge "⏱ Xh Ym since OT" next to the OT time.
function _sinceQuakeBadge(isoStr, nowSec) {
  const s = _sinceQuakeStr(isoStr, nowSec);
  if (!s) return '';
  return `<b class="om-since-ot"><i class="bi bi-stopwatch"></i> ${s} sejak OT</b>`;
}

class OnlineMonitorModule {
  constructor(core) {
    this.core        = core;
    this._statusTimer = null;  // light status poll every 5 s (waveform via SSE)
    this._waveformSSE = null;  // EventSource SSE waveform stream
    this._station    = '';        // NET.STA.LOC.CHA currently displayed
    this._stationList = [];       // [{net,sta,lat,lon,name,channels,default_channel}]
    this._map        = null;      // Leaflet map (dashboard)
    this._newMap     = null;      // Leaflet map (wizard new config preview)
    this._markers    = {};        // { 'NET.STA': L.circleMarker } di dashboard
    this._activeStreams = [];
    this._currentCfg = null;      // meta.json of the currently open config
    this._currentCfgId = null;
    // ── Client-side ring buffer (scrttv-style delta fetch) ───────────────────
    // After the first snapshot, the server only sends deltas (new data).
    // The client accumulates here and renders from here — not from the
    // server payload each tick. Result: payload drops 8-10× per tick.
    //
    // The delta cursor is DATA-TIME based (buf.cursor = last_t), not wall clock,
    // so seedlink latency cannot make the delta skip/empty out data
    // (cf. scrttv _lastRecordTime).
    this._clientBufs   = new Map(); // key → {pts:[{t,v}], picks, meta, cursor}
    this._snapDone     = false;     // initial snapshot done? (false → request one)
    this._sessionEpoch = null;      // server session_epoch → detects restarts
    this._serverOffset = 0;         // (server clock − browser clock) seconds, anchors the axis
    this._dataOffset   = 0;         // (leading data edge − server clock) seconds. Vital for
                                    // msrtsimul: simulated data often RUNS AHEAD of the server clock,
                                    // so the "now" line must follow the data, not the server clock.
    // ── Pick Log View ────────────────────────────────────────────────────────
    this._feedTab        = 'event'; // active tab: 'event' | 'list' | 'picks'
    this._pickLogSince   = 0.0;      // incremental fetch epoch
    this._pickLogTotal   = 0;        // session pick total (from the server)
    this._pickLogTimer   = null;
    // Pick store = the AUTHORITATIVE pick source for waveform rendering (key 'NET.STA' →
    // [{t,phase,source}]). Filled from the /recent pick log (not from per-stream deltas,
    // fragile against the window cursor) — the fix for 'picks not plotted in realtime'.
    this._pickStore      = new Map();
    // ── Assoc log (Task 3) ─────────────────────────────────────────────────────
    this._assocLogSeen   = 0;        // last assoc_log length already rendered
    // ── Events + detail + focal ────────────────────────────────────────────────
    this._events           = [];      // latest events from the associator
    this._selEventIdx      = 0;       // index of the event shown in the detail card
    this._eventLayer       = null;    // L.layerGroup of epicenters on the map
    this._stationLineLayer = null;    // L.layerGroup of station→epicenter lines
    this._lastSdsPath      = '';
    this._evwData          = null;    // latest event-waveform API data (for the tz toggle)
    this._evwTzUtc         = true;    // true=UTC, false=WIB for time labels in the evw modal
    this._cfgLoaded        = false;   // config form already loaded from the server while running?
    // ── Station Map blink (pick + association feedback) ─────────────────────
    // A station blinks the SAME color both when it produces a raw pick and
    // (again) when that pick ends up inside an associated event — one color
    // means "this station contributed a detection", so the two stages read
    // as one continuous visual story instead of two unrelated colors.
    this._blinkTimers  = {};   // 'NET.STA' → setTimeout id (so a 2nd blink restarts the clock)
    this._seenEventIds = new Set();   // event_id already blinked for, so a re-poll doesn't re-blink

    // Close the catalog modal with Esc
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') this.closeCatalogModal();
    });
  }

  /* ── Helper: "now" = the leading DATA edge (anchors the axis & "now" line) ─
   * = browser clock + (server clock−browser) + (data edge−server clock)
   * = browser clock + combined offset → flows smoothly every 1s, BUT the "now" line
   * lands exactly at the right edge of the data. Critical for msrtsimul (data leads the
   * server clock by ~minutes) as well as real-time (data lags by seconds, scrttv behavior). */
  _nowSec() {
    return Date.now() / 1000 + (this._serverOffset || 0) + (this._dataOffset || 0);
  }

  // Association backend display info — SINGLE source of truth for the label/tag/color
  // shown across the engine card, status line, event feed and event detail panel.
  // Mirrors RealtimeAssociator._backend_label() on the Python side (_realtime_pipeline.py).
  // Historically this was a `backend === 'gamma' ? 'GaMMA' : 'REAL'` ternary repeated in
  // 5 places — correct only for the original 2 backends, silently mislabeling PyOcto/glass3
  // as "REAL" everywhere once those backends were added.
  _beInfo(backend) {
    const map = {
      real  : { label: 'REAL',   tag: 'REAL', color: '#22d3ee', icon: '⊞' },
      gamma : { label: 'GaMMA',  tag: 'GMM',  color: '#a78bfa', icon: '⊞' },
      pyocto: { label: 'PyOcto', tag: 'PYO',  color: '#f59e0b', icon: '⊞' },
      glass3: { label: 'glass3', tag: 'GL3',  color: '#34d399', icon: '⊞' },
    };
    return map[backend] || map.real;
  }

  // Server−browser clock offset from server_time in each response (lightly smoothed
  // so the "now" line does not jump). Fixes 'misaligned plot' caused by a
  // user PC clock skewed relative to the data time.
  _updateServerOffset(serverTime) {
    if (!serverTime) return;
    const off = serverTime - Date.now() / 1000;
    this._serverOffset = this._serverOffset
      ? this._serverOffset * 0.8 + off * 0.2 : off;
  }

  // Offset (leading data edge − server clock). Computed from the largest cursor
  // across streams (last sample time). >0 = data AHEAD of the server clock
  // (typical for msrtsimul replays); <0 = data lagging (normal real-time). Without this,
  // the server-clock "now" line drifts off the waveform+spectrogram block (the "offside" bug).
  _updateDataOffset(serverTime) {
    if (!serverTime) return;
    let maxT = 0;
    for (const buf of this._clientBufs.values()) {
      if (buf.cursor != null && buf.cursor > maxT) maxT = buf.cursor;
    }
    if (maxT <= 0) return;
    const off = maxT - serverTime;
    this._dataOffset = this._dataOffset
      ? this._dataOffset * 0.8 + off * 0.2 : off;
  }

  // Per-stream cursor (last sample data time) to send to /delta.
  _buildCursors() {
    const c = {};
    for (const [key, buf] of this._clientBufs)
      if (buf.cursor != null) c[key] = buf.cursor;
    return c;
  }

  /* ── Panel visibility ─────────────────────────────────────────────────────── */
  _showPanel(id) {
    const panels = ['om-wizard-new', 'om-wizard-cfg', 'om-direct-form',
                    'om-home', 'om-dashboard'];
    panels.forEach(p => {
      const el = document.getElementById(p);
      if (el) el.classList.toggle('hidden', p !== id);
    });
  }

  showHome() { this._showPanel('om-home'); }
  showNewConfig() { this._showPanel('om-wizard-new'); this._clearNewMap(); }

  /* Sidebar "Live" button for a config whose session is already running —
     jump straight to the dashboard (restoreIfLive already does exactly
     this, picking up cfg_id/host/port from the server, including the
     project-mode switch) instead of forcing the user through
     openConfig() → wizard → Connect again. */
  async viewLive(cfgId) {
    await this.restoreIfLive();
    document.querySelectorAll('.cfg-item').forEach(e => e.classList.remove('active-cfg'));
    document.getElementById(`cfgitem-${cfgId}`)?.classList.add('active-cfg');
  }

  /* Reopen the config wizard from the live dashboard (Settings → Edit
     Configuration) to tweak picker/association thresholds, engine, or extra
     SeedLink sources without dropping the running session. openConfig()
     does the actual full-form fill (extra sources, host/port, agent
     url/token, sync badge, ...) from the saved config — without it, only
     the picker/gamma thresholds would refresh and everything else would
     show whatever was last in the DOM (empty after a page refresh). */
  async editSettings() {
    document.getElementById('om-settings-menu')?.classList.add('hidden');
    this._editingFromDashboard = true;
    if (this._currentCfgId) {
      await this.openConfig(this._currentCfgId);
    } else {
      this._showPanel('om-wizard-cfg');
    }
    // Overlay the live picker/association values — may differ from the
    // saved config if realtime was started with tweaked, unsaved thresholds.
    fetch('/api/online/realtime/config').then(r => r.json())
      .then(c => this._applyCfgForm(c)).catch(() => {});
  }

  /* Close the config wizard: back to the dashboard if it was reopened via
     editSettings() (session still running), otherwise the normal wizard
     close (used by the initial new-connection flow) → home. */
  closeWizardCfg() {
    if (this._editingFromDashboard) {
      this._editingFromDashboard = false;
      this._showPanel('om-dashboard');
    } else {
      this.showHome();
    }
  }

  /* ── Tab switch inside the config wizard (Sync SeisComP | RingServer) ────── */
  switchCfgTab(tab) {
    ['seiscomp', 'ringserver'].forEach(t => {
      document.getElementById(`om-tab-${t}`)?.classList.toggle('active', t === tab);
      document.getElementById(`om-tabpanel-${t}`)?.classList.toggle('hidden', t !== tab);
    });
  }

  /* ── Tab switch panel feed: 'event' (detail) | 'list' | 'picks' ───────────── */
  showFeedTab(tab) {
    this._feedTab = tab;
    document.getElementById('om-tab-event')?.classList.toggle('active', tab === 'event');
    document.getElementById('om-tab-list')?.classList.toggle('active',  tab === 'list');
    document.getElementById('om-tab-picks')?.classList.toggle('active', tab === 'picks');
    const evEl = document.getElementById('om-event-tab');            // detail card
    const lsEl = document.getElementById('om-event-feed-list-wrap'); // daftar event + toolbar
    const plEl = document.getElementById('om-pick-log-list');        // pick log
    if (evEl) evEl.style.display = tab === 'event' ? 'flex'  : 'none';
    if (lsEl) lsEl.style.display = tab === 'list'  ? 'flex'  : 'none';
    if (plEl) plEl.style.display = tab === 'picks' ? ''      : 'none';
    // Trigger a fetch immediately when the user opens the Pick Log so no delay is felt
    if (tab === 'picks') this._pollPickLog();
  }

  /* ── Connect RingServer (Tab 2, without an agent) ─────────────────────────── */
  async connectRingServer() {
    const host = document.getElementById('om-cfg-rs-host')?.value.trim();
    const port = parseInt(document.getElementById('om-cfg-rs-port')?.value || 18000);
    const inv  = document.getElementById('om-cfg-rs-inventory')?.value.trim()
              || this._currentCfg?.inventory_path || '';
    const log  = document.getElementById('om-rs-log');
    if (!host) { this._setStatus('red', 'Enter the RingServer host'); return; }
    if (log) log.textContent = 'Connecting…';
    await this.connect(host, port, null, inv);
    if (log) log.textContent = '';
  }

  showDirectConnect() {
    // Fill the inventory from detect when empty
    fetch('/api/online/detect').then(r => r.json()).then(d => {
      const inv = document.getElementById('om-inventory');
      if (inv && !inv.value && d.default_inventory) inv.value = d.default_inventory;
    }).catch(() => {});
    this._showPanel('om-direct-form');
  }

  /* ── Open a config from the sidebar ───────────────────────────────────────── */
  async openConfig(cfgId) {
    switchProjectMode('online');   // ensure the Online tab is active
    this._showPanel('om-wizard-cfg');
    this._setStatus('grey', `Loading config ${cfgId}…`);
    try {
      const r  = await fetch(`/api/configs/${cfgId}`);
      const c  = await r.json();
      if (!r.ok) { this._setStatus('red', c.error || 'Config not found'); return; }

      this._currentCfg   = c;
      this._currentCfgId = cfgId;

      const _areaTag = (c.region && c.region.limit)
        ? `  ▢ Area ${(+c.region.lat).toFixed(2)},${(+c.region.lon).toFixed(2)}`
          + (c.parent_id ? ` (from ${c.parent_id})` : '')
        : '';
      document.getElementById('om-cfg-title').textContent =
        `${c.name} · ${cfgId}${_areaTag}`;
      const badge = document.getElementById('om-cfg-sync-badge');
      const ls = c.last_sync;  // { ok, error, time, ... } — result of the last sync attempt
      if (c.synced) {
        badge.textContent = 'synced';
        badge.style.cssText = 'color:#22c55e;background:#14532d;padding:0 5px;border-radius:3px';
        badge.title = ls?.time ? `Synced ${ls.time}` : 'Already synced to SeisComP';
      } else {
        badge.textContent = 'not synced';
        badge.style.cssText = 'color:#f59e0b;background:#44250a;padding:0 5px;border-radius:3px;cursor:help';
        badge.title = ls?.error
          ? `Last sync FAILED (${ls.time || ''}):\n${ls.error}`
          : 'Never synced to SeisComP yet. Open the "Sync SeisComP" tab then click Sync.';
      }
      // Show the failure reason in the sync log so it is clear without hovering
      const syncLog = document.getElementById('om-sync-log');
      if (syncLog) {
        if (!c.synced && ls?.error) {
          syncLog.textContent = `⚠ Last sync failed (${ls.time || ''}):\n${ls.error}`;
          syncLog.style.color = '#f59e0b';
        } else if (!c.synced) {
          syncLog.textContent = 'Never synced yet. Fill in the Agent URL/Token + remote SeedLink, then click "Sync SeisComP".';
          syncLog.style.color = '';
        }
      }

      // Fill the step-2 form from the config data
      const v = id => document.getElementById(id);
      this._renderExtraSources(c.extra_sources);
      if (c.seedlink_host) v('om-cfg-sl-host').value = c.seedlink_host;
      if (c.seedlink_port) v('om-cfg-sl-port').value = c.seedlink_port;
      if (c.agent_url)     v('om-cfg-agent-url').value = c.agent_url;
      if (c.agent_token)   v('om-cfg-agent-token').value = c.agent_token;
      if (c.local_sl_host) v('om-cfg-local-host').value = c.local_sl_host;
      if (c.local_sl_port) v('om-cfg-local-port').value = c.local_sl_port;
      // Main inventory XML — was never pre-filled, so the RingServer tab's
      // field looked empty when reopening a saved config even though one
      // was already set.
      const invField = v('om-cfg-rs-inventory');
      if (invField) invField.value = c.inventory_path || c.inventory || '';
      const accelChk = v('om-cfg-include-accel');
      if (accelChk) accelChk.checked = !!c.include_accel;
      const archField = v('om-cfg-archive-days');
      if (archField) archField.value = c.archive_days ?? 1;

      // When synced: hide the remote SeedLink + local host/port fields;
      // enable the inject button.
      const isSynced = !!c.synced;
      // The source SeedLink IP is ALWAYS shown — editable & savable at any time,
      // including after sync (the monitor connects directly to this IP, Option 1).
      v('om-cfg-sl-block')?.style?.setProperty('display', '');
      v('om-cfg-local-hostport')?.style?.setProperty('display', isSynced ? 'none' : 'flex');
      const si = v('om-cfg-synced-info');
      if (si) si.style.display = isSynced ? '' : 'none';
      // Inject button: only enabled when synced
      const injBtn = v('om-btn-inject-now');
      if (injBtn) injBtn.disabled = !isSynced;
      const injBadge = v('om-inject-badge');
      if (injBadge) {
        injBadge.textContent = isSynced ? 'active' : 'needs sync';
        injBadge.style.cssText = isSynced
          ? 'font-size:.63rem;padding:0 4px;border-radius:3px;background:#14532d;color:#22c55e'
          : 'font-size:.63rem;padding:0 4px;border-radius:3px;background:#44250a;color:#f59e0b';
      }
      // Restore the auto-inject checkbox from localStorage
      const autoInjectChk = v('om-inject-auto');
      if (autoInjectChk) autoInjectChk.checked = localStorage.getItem('sw_auto_inject') === '1';

      this._setStatus('grey', `Config loaded — ${c.n_stations || 0} stations`);
      // Automatically check the agent status
      this.agentRefreshStatus();

      // Highlight sidebar
      document.querySelectorAll('.cfg-item').forEach(e => e.classList.remove('active-cfg'));
      document.getElementById(`cfgitem-${cfgId}`)?.classList.add('active-cfg');
    } catch (e) {
      this._setStatus('red', 'Failed to load config: ' + e.message);
    }
  }

  /* ── Step 1: Upload inventory XML from the browser (XHR + progress %) ────── */
  uploadInventory(inputEl) {
    const file = inputEl?.files?.[0];
    if (!file) return;
    const statusEl = document.getElementById('om-upload-status');
    const self = this;

    statusEl.style.color = '';
    statusEl.textContent = `Uploading ${file.name}… 0%`;
    this._setStatus('grey', 'Uploading inventory…');

    const fd = new FormData();
    fd.append('file', file);
    const xhr = new XMLHttpRequest();

    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.round(ev.loaded / ev.total * 100);
      statusEl.textContent = `Uploading ${file.name}… ${pct}%`;
    };

    xhr.onload = async () => {
      inputEl.value = '';
      let r = null;
      try { r = JSON.parse(xhr.responseText); } catch { r = null; }
      if (r === null) {
        // Non-JSON response — usually an HTML page from a reverse proxy (nginx
        // client_max_body_size) or a 413/502 error. Show the status + a snippet of
        // the raw body so the cause is visible, not just "Invalid response".
        const snippet = (xhr.responseText || '').replace(/<[^>]+>/g, ' ')
                          .replace(/\s+/g, ' ').trim().slice(0, 160);
        const hint = xhr.status === 413
          ? ' (file exceeds the proxy/server limit — check nginx client_max_body_size)'
          : (xhr.status === 0 ? ' (connection lost / blocked)' : '');
        statusEl.textContent =
          `Upload failed: HTTP ${xhr.status}${hint}` + (snippet ? ` — ${snippet}` : '');
        self._setStatus('red', `Upload failed (HTTP ${xhr.status})`);
        console.error('[upload inventory] non-JSON response', xhr.status, xhr.responseText?.slice(0, 500));
        return;
      }
      if (xhr.status !== 200 || r.error) {
        statusEl.textContent = 'Upload failed: ' + (r.error || `HTTP ${xhr.status}`);
        self._setStatus('red', 'Upload failed');
        return;
      }
      document.getElementById('om-new-inventory').value = r.path;
      statusEl.style.color = '#22c55e';
      statusEl.textContent = `✓ ${r.filename} — ${r.n_stations} stations (saved on the server)`;
      self._setStatus('green', `Upload OK — ${r.n_stations} stations`);
      await self.previewInventory();
    };

    xhr.onerror = () => {
      inputEl.value = '';
      statusEl.textContent = 'Upload error — connection failed';
      self._setStatus('red', 'Upload error');
    };

    xhr.open('POST', '/api/online/inventory/upload');
    xhr.send(fd);
  }

  /* Re-upload the MAIN inventory XML from the Edit Configuration flow (e.g.
     station positions changed since the config was first created). Persists
     immediately to the saved config (so it survives a reload/re-edit) and
     updates _currentCfg in memory so BOTH reconnect paths pick it up —
     connectRingServer() reads the DOM field directly, startMonitorFromConfig()
     reads _currentCfg.inventory_path. Actually applying it to the already-
     running session still needs a reconnect (host:port + inventory are both
     compared now, so a real refresh happens instead of a silent resume). */
  async uploadMainInventory(inputEl) {
    const file = inputEl?.files?.[0];
    if (!file) return;
    const statusEl = document.getElementById('om-cfg-rs-inv-status');
    const invField = document.getElementById('om-cfg-rs-inventory');
    if (statusEl) { statusEl.style.color = ''; statusEl.textContent = `Uploading ${file.name}… 0%`; }

    const fd = new FormData();
    fd.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable || !statusEl) return;
      statusEl.textContent = `Uploading ${file.name}… ${Math.round(ev.loaded / ev.total * 100)}%`;
    };
    xhr.onload = async () => {
      inputEl.value = '';
      let r = null;
      try { r = JSON.parse(xhr.responseText); } catch { r = null; }
      if (!r || xhr.status !== 200 || r.error) {
        if (statusEl) { statusEl.style.color = '#ef4444'; statusEl.textContent = `Upload failed: ${r?.error || `HTTP ${xhr.status}`}`; }
        return;
      }
      if (invField) invField.value = r.path;
      if (this._currentCfgId) {
        try {
          await fetch(`/api/online/configs/${this._currentCfgId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ inventory_path: r.path }),
          });
          if (this._currentCfg) this._currentCfg.inventory_path = r.path;
        } catch (_) { /* saved locally in the field either way */ }
      }
      if (statusEl) { statusEl.style.color = '#22c55e'; statusEl.textContent = `✓ ${r.filename} — ${r.n_stations} stations, saved. Click Connect RingServer to apply to the live session.`; }
    };
    xhr.onerror = () => {
      inputEl.value = '';
      if (statusEl) { statusEl.style.color = '#ef4444'; statusEl.textContent = 'Upload error — connection failed'; }
    };
    xhr.open('POST', '/api/online/inventory/upload');
    xhr.send(fd);
  }

  /* ── Step 1: Preview inventory on the map ─────────────────────────────────── */
  async previewInventory() {
    const inv = document.getElementById('om-new-inventory').value.trim();
    if (!inv) { this._setStatus('amber', 'Enter the inventory path first'); return; }
    this._setStatus('grey', 'Loading inventory…');
    try {
      const r = await (await fetch(
        `/api/online/stations?inventory=${encodeURIComponent(inv)}`
      )).json();
      if (!r.stations || !r.stations.length) {
        this._setStatus('red', r.error || 'No stations found');
        return;
      }
      document.getElementById('om-new-map-ph')?.remove();
      document.getElementById('om-new-sta-count').textContent =
        `${r.count} stations found`;
      this._newStations = r.stations || [];   // for in-box counting when drawing
      this._initNewMap(r.stations);
      // Enable "Set Area" now that a map + stations exist.
      const areaBtn = document.getElementById('om-new-setarea-btn');
      if (areaBtn) areaBtn.disabled = false;
      this._setStatus('green', `${r.count} stations from the inventory`);
    } catch (e) {
      this._setStatus('red', 'Preview failed: ' + e.message);
    }
  }

  _initNewMap(stations) {
    const el = document.getElementById('om-new-map');
    if (!el) return;
    if (this._newMap) { this._newMap.remove(); this._newMap = null; }

    const built = SW.buildOnlineMap('om-new-map', { leafletOpts: { zoom: 6 } });
    if (!built) return;
    this._newMap = built.map;
    this._newMarkers = [];

    stations.forEach(s => {
      const m = L.marker([s.lat, s.lon], { icon: _stationIcon('#3b82f6', 13) })
        .bindTooltip(
          `<b>${s.net}.${s.sta}</b><br>${s.name}<br>${s.lat.toFixed(3)}, ${s.lon.toFixed(3)}`,
          { direction: 'top', className: 'sw-tooltip' }
        )
        .addTo(this._newMap);
      this._newMarkers.push({ marker: m, lat: s.lat, lon: s.lon });
    });
    if (stations.length > 1) {
      this._newMap.fitBounds(stations.map(s => [s.lat, s.lon]), { padding: [15, 15] });
    } else if (stations.length === 1) {
      this._newMap.setView([stations[0].lat, stations[0].lon], 8);
    }
  }

  _clearNewMap() {
    if (this._newMap) { this._newMap.remove(); this._newMap = null; }
    this._newMarkers = [];
    this._newArea = null;
    this._areaDrawing = false;
    const ph = document.getElementById('om-new-map');
    if (ph && !document.getElementById('om-new-map-ph')) {
      ph.innerHTML = '<div class="om-placeholder" id="om-new-map-ph" style="height:100%;display:flex;align-items:center;justify-content:center">Map preview — click Preview after filling in the inventory</div>';
    }
    const cnt = document.getElementById('om-new-sta-count');
    if (cnt) cnt.textContent = '';
    document.getElementById('om-new-area-box')?.classList.add('hidden');
    document.getElementById('om-new-savearea-btn')?.classList.add('hidden');
    const setBtn = document.getElementById('om-new-setarea-btn');
    if (setBtn) setBtn.disabled = true;
  }

  /* ── Set Area: draw a monitoring-area box on the preview map ───────────────── */
  toggleAreaDraw() {
    if (!this._newMap) { this._setStatus('amber', 'Click "View Map XML" first'); return; }
    this._areaDrawing = !this._areaDrawing;
    const btn = document.getElementById('om-new-setarea-btn');
    document.getElementById('om-new-area-box')?.classList.remove('hidden');
    if (this._areaDrawing) {
      if (btn) btn.classList.add('btn-blue-sw');
      this._enableAreaDraw();
      this._setStatus('grey', 'Drag on the map to draw the monitoring area');
    } else {
      if (btn) btn.classList.remove('btn-blue-sw');
      this._disableAreaDraw();
    }
  }

  _enableAreaDraw() {
    const map = this._newMap;
    if (!map) return;
    map.dragging.disable();
    let start = null, rect = null;
    const self = this;
    this._areaHandlers = {
      down(e) {
        start = e.latlng;
        if (rect) { map.removeLayer(rect); rect = null; }
      },
      move(e) {
        if (!start) return;
        const b = L.latLngBounds(start, e.latlng);
        if (!rect) {
          rect = L.rectangle(b, { color: '#22c55e', weight: 2,
                                  fillColor: '#22c55e', fillOpacity: 0.08 }).addTo(map);
        } else { rect.setBounds(b); }
      },
      up(e) {
        if (!start) return;
        const b = L.latLngBounds(start, e.latlng);
        start = null;
        if (self._areaRect) map.removeLayer(self._areaRect);
        self._areaRect = rect; rect = null;
        self._commitArea(b);
        self.toggleAreaDraw();   // one box per drag → exit draw mode
      },
    };
    map.on('mousedown', this._areaHandlers.down);
    map.on('mousemove', this._areaHandlers.move);
    map.on('mouseup',   this._areaHandlers.up);
    if (map._container) map._container.style.cursor = 'crosshair';
  }

  _disableAreaDraw() {
    const map = this._newMap;
    if (!map || !this._areaHandlers) return;
    map.off('mousedown', this._areaHandlers.down);
    map.off('mousemove', this._areaHandlers.move);
    map.off('mouseup',   this._areaHandlers.up);
    map.dragging.enable();
    if (map._container) map._container.style.cursor = '';
    this._areaHandlers = null;
  }

  _commitArea(bounds) {
    const latMin = bounds.getSouth(), latMax = bounds.getNorth();
    const lonMin = bounds.getWest(),  lonMax = bounds.getEast();
    this._newArea = { lat_min: latMin, lat_max: latMax,
                      lon_min: lonMin, lon_max: lonMax };
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v.toFixed(3); };
    set('om-area-latmin', latMin); set('om-area-latmax', latMax);
    set('om-area-lonmin', lonMin); set('om-area-lonmax', lonMax);

    // Highlight in-box vs out-of-box stations + count.
    let nIn = 0;
    (this._newMarkers || []).forEach(({ marker, lat, lon }) => {
      const inside = (lat >= latMin && lat <= latMax && lon >= lonMin && lon <= lonMax);
      if (inside) nIn++;
      marker.setIcon(_stationIcon(inside ? '#22c55e' : '#64748b', inside ? 14 : 11));
    });
    const clat = (latMin + latMax) / 2, clon = (lonMin + lonMax) / 2;
    const info = document.getElementById('om-area-info');
    if (info) info.innerHTML =
      `<b>${nIn}</b> stations inside · center ${clat.toFixed(3)}, ${clon.toFixed(3)}`;
    const saveBtn = document.getElementById('om-new-savearea-btn');
    if (saveBtn) saveBtn.classList.toggle('hidden', nIn < 4);
    if (nIn < 4) this._setStatus('amber', `Only ${nIn} stations in box — need ≥ 4`);
    else this._setStatus('green', `${nIn} stations inside the area`);
  }

  /* ── Set Area: create a NEW region-limited config from the drawn box ───────── */
  async saveAreaConfig() {
    const name = document.getElementById('om-new-name').value.trim();
    const inv  = document.getElementById('om-new-inventory').value.trim();
    if (!name) { this._setStatus('amber', 'Enter a config name'); return; }
    if (!inv)  { this._setStatus('amber', 'Enter the inventory path'); return; }
    if (!this._newArea) { this._setStatus('amber', 'Draw an area on the map first'); return; }

    this._setStatus('grey', 'Creating base config…');
    try {
      // 1) Create the base (full-inventory) config — stays intact as the parent.
      const base = await (await fetch('/api/online/configs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, inventory_path: inv }),
      })).json();
      if (base.error) { this._setStatus('red', base.error); return; }

      // 2) Clone it into a region-limited area config (grid builds in background).
      this._setStatus('grey', 'Applying area & generating NLLoc grid…');
      const area = await (await fetch(`/api/online/configs/${base.id}/area`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: `${name} — Area`, region: this._newArea }),
      })).json();
      if (area.error) { this._setStatus('red', area.error); return; }

      this._setStatus('green',
        `Area config ${area.id}: ${area.n_stations} stations · grid ${area.grid_status}`);
      try { await loadConfigList(); } catch (_) {}
      await this.openConfig(area.id);
    } catch (e) {
      this._setStatus('red', 'Save failed: ' + e.message);
    }
  }

  /* ── Step 1: Save the online config ───────────────────────────────────────── */
  async saveNewConfig() {
    const name = document.getElementById('om-new-name').value.trim();
    const inv  = document.getElementById('om-new-inventory').value.trim();
    if (!name) { this._setStatus('amber', 'Enter a config name'); return; }
    if (!inv)  { this._setStatus('amber', 'Enter the inventory path'); return; }

    this._setStatus('grey', 'Menyimpan config…');
    try {
      const r = await (await fetch('/api/online/configs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, inventory_path: inv }),
      })).json();

      if (r.error) { this._setStatus('red', r.error); return; }

      this._setStatus('green', `Config saved: ${r.id}`);
      // Refresh sidebar
      try { await loadConfigList(); } catch (_) {}
      // Go straight to wizard step 2
      await this.openConfig(r.id);
    } catch (e) {
      this._setStatus('red', 'Save failed: ' + e.message);
    }
  }

  /* ── Step 2: Discover streams via agent ───────────────────────────────────── */
  async discoverStreams() {
    const agentUrl   = document.getElementById('om-cfg-agent-url').value.trim();
    const agentToken = document.getElementById('om-cfg-agent-token').value.trim();
    const slHost     = document.getElementById('om-cfg-sl-host').value.trim();
    const slPort     = document.getElementById('om-cfg-sl-port').value || 18000;
    const logEl      = document.getElementById('om-discover-result');

    if (!agentUrl) { this._setStatus('amber', 'Enter the Agent URL first'); return; }

    logEl.textContent = 'Querying SeedLink via agent…';
    this._setStatus('grey', 'Discovering streams…');
    try {
      const r = await (await fetch(
        `${agentUrl}/info/streams?host=${encodeURIComponent(slHost)}&port=${slPort}`,
        { headers: { Authorization: `Bearer ${agentToken}` } }
      )).json();
      if (!r.ok) { logEl.textContent = 'Error: ' + (r.error || JSON.stringify(r)); this._setStatus('red', 'Discovery failed'); return; }
      logEl.textContent =
        `${r.n_streams} streams, ${r.n_stations} stations\n` +
        (r.stations || []).slice(0, 10).map(s =>
          `${s.net}.${s.sta}: ${s.channels.map(c => c.cha).join(',')}`
        ).join('\n') +
        (r.n_stations > 10 ? `\n…and ${r.n_stations - 10} more` : '');
      this._setStatus('green', `${r.n_streams} streams found from ${slHost}`);
    } catch (e) {
      logEl.textContent = 'Failed: ' + e.message;
      this._setStatus('red', 'Discovery failed — check the agent URL and token');
    }
  }

  /* ── Step 2: Sync SeisComP via agent ─────────────────────────────────────── */
  async syncSeisComP() {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    const agentUrl   = document.getElementById('om-cfg-agent-url').value.trim();
    const agentToken = document.getElementById('om-cfg-agent-token').value.trim();
    const slHost     = document.getElementById('om-cfg-sl-host').value.trim();
    const slPort     = parseInt(document.getElementById('om-cfg-sl-port').value || 18000);
    const logEl      = document.getElementById('om-sync-log');

    if (!agentUrl) { this._setStatus('amber', 'Enter the Agent URL'); return; }
    if (!slHost)   { this._setStatus('amber', 'Enter the remote SeedLink host'); return; }

    // Save to the config first
    await fetch(`/api/online/configs/${this._currentCfgId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_url: agentUrl, agent_token: agentToken,
                             seedlink_host: slHost, seedlink_port: slPort }),
    }).catch(() => {});

    const btn = document.getElementById('om-btn-sync');
    if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
    logEl.textContent = 'Starting SeisComP sync…';
    this._setStatus('grey', 'Sync SeisComP…');

    try {
      const r = await (await fetch(
        `/api/online/configs/${this._currentCfgId}/sync`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' } }
      )).json();

      if (r.steps) {
        logEl.textContent = r.steps.map(s =>
          `[${s.ok ? '✓' : '✗'}] ${s.step}` +
          (s.rc !== undefined ? ` (rc=${s.rc})` : '') +
          (s.count !== undefined ? ` — ${s.count} items` : '') +
          (s.skipped ? ' (skip)' : '')
        ).join('\n');
      }

      if (r.ok) {
        this._setStatus('green', `Sync successful — ${r.n_streams || 0} streams, ${r.n_stations || 0} stations`);
        // Update badge
        const badge = document.getElementById('om-cfg-sync-badge');
        if (badge) {
          badge.textContent = 'synced';
          badge.style.cssText = 'color:#22c55e;background:#14532d;padding:0 5px;border-radius:3px';
        }
        try { await loadConfigList(); } catch (_) {}
      } else {
        this._setStatus('red', 'Sync failed — see the log');
      }
    } catch (e) {
      logEl.textContent = 'Error: ' + e.message;
      this._setStatus('red', 'Sync error: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> Sync SeisComP'; }
    }
  }

  /* Cross-check each station's channel (from the inventory, already epoch-
     corrected server-side) against what the SeedLink server actually
     carries right now (via slinktool). An inventory can go stale relative
     to the live acquisition chain — e.g. a station recorded as BHZ may now
     only transmit HNZ/SHZ — without the exported XML ever being
     regenerated; only a live query catches that. */
  async verifyChannels() {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    const slHost = document.getElementById('om-cfg-sl-host')?.value.trim();
    const slPort = parseInt(document.getElementById('om-cfg-sl-port')?.value || 18000);
    const logEl  = document.getElementById('om-verify-log');
    const btn    = document.getElementById('om-btn-verify-cha');
    if (!slHost) { this._setStatus('amber', 'Enter the remote SeedLink host first'); return; }

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Verifying…'; }
    if (logEl) logEl.textContent = 'Querying SeedLink via slinktool…';
    this._setStatus('grey', 'Verifying channels…');

    try {
      const r = await (await fetch(
        `/api/online/configs/${this._currentCfgId}/verify_channels`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: slHost, port: slPort }) }
      )).json();

      if (!r.ok) {
        if (logEl) logEl.textContent = '✗ ' + (r.error || 'Verification failed');
        this._setStatus('red', 'Channel verification failed');
        return;
      }

      const lines = [`${r.matched}/${r.total} stations match the live SeedLink channel.`,
        `${r.cached || 0} station channel(s) cached — used as the fallback if a future connect's slinktool query times out.`];
      if (r.mismatched.length) {
        lines.push(`\nMismatched (${r.mismatched.length}) — inventory says X, live has Y:`);
        for (const m of r.mismatched.slice(0, 30)) {
          lines.push(`  ${m.net}.${m.sta}: expected ${m.expected}, live has [${m.live.join(', ')}]`);
        }
        if (r.mismatched.length > 30) lines.push(`  … and ${r.mismatched.length - 30} more`);
      }
      if (r.missing.length) {
        lines.push(`\nNot found on the live server (${r.missing.length}):`);
        for (const m of r.missing.slice(0, 15)) lines.push(`  ${m.net}.${m.sta} (expected ${m.expected})`);
        if (r.missing.length > 15) lines.push(`  … and ${r.missing.length - 15} more`);
      }
      if (logEl) logEl.textContent = lines.join('\n');

      const pct = r.total ? Math.round(r.matched / r.total * 100) : 0;
      this._setStatus(pct >= 90 ? 'green' : 'amber',
        `Channel check: ${r.matched}/${r.total} match (${pct}%)`);
    } catch (e) {
      if (logEl) logEl.textContent = 'Error: ' + e.message;
      this._setStatus('red', 'Verify error: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-check2-square"></i> Verify Channels'; }
    }
  }

  /* ── Extra SeedLink sources (multi-server sessions) ───────────────────────
     One session may ingest SEVERAL SeedLink servers; each extra source can
     carry its own sensor XML (inventory) so more stations stream in. Rows are
     stored on the config as extra_sources: [{host, port, inventory_path}]. */
  addExtraSourceRow(src = {}) {
    const wrap = document.getElementById('om-extra-src-rows');
    if (!wrap) return;
    const rowId = `xsrc-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const row = document.createElement('div');
    row.className = 'om-xsrc-row';
    row.dataset.rowId = rowId;
    row.style.cssText = 'display:flex;flex-direction:column;gap:.25rem;padding:.3rem;border:1px solid var(--border);border-radius:6px';
    row.innerHTML =
      `<div style="display:flex;gap:.3rem;align-items:center">
         <input class="sw-input om-xsrc-host" placeholder="host / IP" style="flex:1" value="${(src.host || '').replace(/"/g, '&quot;')}"/>
         <input class="sw-input om-xsrc-port" type="number" placeholder="18000" style="width:76px" value="${src.port || ''}"/>
         <button class="btn-sw btn-ghost-sw btn-sm-sw" title="Remove source"
                 onclick="this.closest('.om-xsrc-row').remove()"><i class="bi bi-x-lg"></i></button>
       </div>
       <div style="display:flex;gap:.3rem;align-items:center">
         <input class="sw-input om-xsrc-inv" placeholder="inventory XML path (optional)" style="flex:2" value="${(src.inventory_path || src.inventory || '').replace(/"/g, '&quot;')}"/>
         <input type="file" accept=".xml" class="om-xsrc-file-input" style="display:none" id="${rowId}-file"
                onchange="OM.uploadExtraSourceInventory(this)"/>
         <button class="btn-sw btn-ghost-sw btn-sm-sw" title="Upload inventory XML for this source"
                 onclick="document.getElementById('${rowId}-file').click()"><i class="bi bi-upload"></i></button>
       </div>
       <div class="om-xsrc-upload-status" style="font-size:.64rem;color:var(--text-muted)"></div>`;
    wrap.appendChild(row);
  }

  /* Upload an inventory XML for one Extra SeedLink source row, reusing the
     same generic /api/online/inventory/upload endpoint as the main inventory
     upload — the returned server-side path is written into that row's field. */
  uploadExtraSourceInventory(inputEl) {
    const file = inputEl?.files?.[0];
    if (!file) return;
    const row = inputEl.closest('.om-xsrc-row');
    const statusEl = row?.querySelector('.om-xsrc-upload-status');
    const invField = row?.querySelector('.om-xsrc-inv');
    if (statusEl) { statusEl.style.color = ''; statusEl.textContent = `Uploading ${file.name}… 0%`; }

    const fd = new FormData();
    fd.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable || !statusEl) return;
      statusEl.textContent = `Uploading ${file.name}… ${Math.round(ev.loaded / ev.total * 100)}%`;
    };
    xhr.onload = () => {
      inputEl.value = '';
      let r = null;
      try { r = JSON.parse(xhr.responseText); } catch { r = null; }
      if (!r || xhr.status !== 200 || r.error) {
        if (statusEl) { statusEl.style.color = '#ef4444'; statusEl.textContent = `Upload failed: ${r?.error || `HTTP ${xhr.status}`}`; }
        return;
      }
      if (invField) invField.value = r.path;
      if (statusEl) { statusEl.style.color = '#22c55e'; statusEl.textContent = `✓ ${r.filename} — ${r.n_stations} stations`; }
    };
    xhr.onerror = () => {
      inputEl.value = '';
      if (statusEl) { statusEl.style.color = '#ef4444'; statusEl.textContent = 'Upload error — connection failed'; }
    };
    xhr.open('POST', '/api/online/inventory/upload');
    xhr.send(fd);
  }

  _renderExtraSources(list) {
    const wrap = document.getElementById('om-extra-src-rows');
    if (!wrap) return;
    wrap.innerHTML = '';
    (list || []).forEach(s => this.addExtraSourceRow(s));
  }

  _readExtraSources() {
    return [...document.querySelectorAll('#om-extra-src-rows .om-xsrc-row')]
      .map(row => ({
        host: row.querySelector('.om-xsrc-host')?.value.trim() || '',
        port: parseInt(row.querySelector('.om-xsrc-port')?.value || 0, 10) || 0,
        inventory_path: row.querySelector('.om-xsrc-inv')?.value.trim() || '',
      }))
      .filter(s => s.host && s.port);
  }

  async saveExtraSources() {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    const sources = this._readExtraSources();
    try {
      const r = await fetch(`/api/online/configs/${this._currentCfgId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ extra_sources: sources }),
      });
      if (!r.ok) { this._setStatus('red', 'Failed to save extra sources'); return; }
      if (this._currentCfg) this._currentCfg.extra_sources = sources;
      const saved = document.getElementById('om-extra-src-saved');
      if (saved) { saved.style.display = ''; setTimeout(() => { saved.style.display = 'none'; }, 2500); }
      this._setStatus('green', `${sources.length} extra SeedLink source(s) saved`);
    } catch (e) {
      this._setStatus('red', 'Error saving extra sources: ' + e.message);
    }
  }

  /* Toggle subscribing to accelerometer bands (HN/BN/EN/...) for every
     station, on top of the default seismometer bands (BH/HH/SH/EH). Saves
     immediately (like a normal toggle switch); taking effect on the live
     session still needs a reconnect, since it changes what's subscribed. */
  async toggleIncludeAccel(checked) {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    try {
      const r = await fetch(`/api/online/configs/${this._currentCfgId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include_accel: checked }),
      });
      if (!r.ok) { this._setStatus('red', 'Failed to save the accelerometer toggle'); return; }
      if (this._currentCfg) this._currentCfg.include_accel = checked;
      const saved = document.getElementById('om-cfg-accel-saved');
      if (saved) { saved.style.display = ''; setTimeout(() => { saved.style.display = 'none'; }, 3500); }
      this._setStatus('green', `Accelerometer channels ${checked ? 'enabled' : 'disabled'} — reconnect to apply`);
    } catch (e) {
      this._setStatus('red', 'Error saving the accelerometer toggle: ' + e.message);
    }
  }

  /* SDS archive retention (days) — independent of the live ring buffer, which
     is short/fixed. Takes effect the next time slarchive (re)starts, i.e.
     the next "Start Real-Time Detection". */
  async saveArchiveDays(value) {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    const days = Math.max(1, Math.min(90, parseInt(value) || 1));
    try {
      const r = await fetch(`/api/online/configs/${this._currentCfgId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archive_days: days }),
      });
      if (!r.ok) { this._setStatus('red', 'Failed to save the archive retention'); return; }
      if (this._currentCfg) this._currentCfg.archive_days = days;
      const saved = document.getElementById('om-cfg-archive-saved');
      if (saved) { saved.style.display = ''; setTimeout(() => { saved.style.display = 'none'; }, 3500); }
      this._setStatus('green', `Archive retention set to ${days} day(s)`);
    } catch (e) {
      this._setStatus('red', 'Error saving the archive retention: ' + e.message);
    }
  }

  /* ── Start monitoring from a config — connect DIRECTLY to the remote seedlink
     that config owns (seedlink_host:port), not the shared local SeisComP chain.
     This isolates each config's source (e.g. one config's SeedLink server vs
     another's) so sessions never mix & each config gets its own waveform
     feed. Fallback order: config's seedlink fields → remote field → local
     field → localhost. ─────────────────────────────────────────────────── */
  /* ── Save the SeedLink IP to the config (editable at any time, including after
     it's synced). Persisted to meta.json via PUT + syncs _currentCfg so
     the next monitor connection uses the new IP. ──────────────────────────── */
  async saveSeedlink() {
    if (!this._currentCfgId) { this._setStatus('red', 'No active config'); return; }
    const host = document.getElementById('om-cfg-sl-host')?.value.trim() || '';
    const port = parseInt(document.getElementById('om-cfg-sl-port')?.value || 18000);
    if (!host) { this._setStatus('amber', 'Enter the SeedLink IP / host first'); return; }
    if (!port || port < 1 || port > 65535) { this._setStatus('amber', 'Invalid SeedLink port'); return; }
    try {
      const r = await fetch(`/api/online/configs/${this._currentCfgId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seedlink_host: host, seedlink_port: port }),
      });
      if (!r.ok) { this._setStatus('red', 'Failed to save the SeedLink IP'); return; }
      if (this._currentCfg) {
        this._currentCfg.seedlink_host = host;
        this._currentCfg.seedlink_port = port;
      }
      const saved = document.getElementById('om-cfg-sl-saved');
      if (saved) { saved.style.display = ''; setTimeout(() => { saved.style.display = 'none'; }, 2500); }
      this._setStatus('green', 'SeedLink IP saved');
      try { await loadConfigList(); } catch (_) {}
    } catch (e) {
      this._setStatus('red', 'Error saving the SeedLink IP: ' + e.message);
    }
  }

  async startMonitorFromConfig() {
    if (!this._currentCfg) { this._setStatus('red', 'No active config'); return; }
    const cfg = this._currentCfg;
    // Prefer the field (latest edit, possibly unsaved) → cfg → local fallback.
    const host = (document.getElementById('om-cfg-sl-host')?.value.trim()
                  || cfg.seedlink_host
                  || document.getElementById('om-cfg-local-host')?.value.trim()
                  || 'localhost');
    const port = parseInt(document.getElementById('om-cfg-sl-port')?.value
                  || cfg.seedlink_port
                  || document.getElementById('om-cfg-local-port')?.value
                  || 18000);
    const inv  = cfg.inventory_path || '';
    this._setStatus('grey', 'Connecting directly to SeedLink…');
    await this.connect(host, port, null, inv,
      { agentUrl: cfg.agent_url, agentToken: cfg.agent_token });
  }

  /* ── Direct Connect (Fase 1 fallback) ────────────────────────────────────── */
  async connectManual() {
    const host = document.getElementById('om-host').value.trim();
    const port = parseInt(document.getElementById('om-port').value, 10);
    const inv  = document.getElementById('om-inventory').value.trim();
    if (!host || !port) { this._setStatus('red', 'Host and port are required'); return; }
    await this.connect(host, port, null, inv);
  }

  /* ── Core Connect ─────────────────────────────────────────────────────────── */
  async connect(host, port, streams, inventory, bridge) {
    this._setStatus('grey', 'Connecting to SeedLink…');
    try {
      const body = { host, port };
      if (streams)   body.streams   = streams;
      if (inventory) body.inventory = inventory;
      if (bridge?.agentUrl)   body.agent_url   = bridge.agentUrl;
      if (bridge?.agentToken) body.agent_token = bridge.agentToken;
      if (this._currentCfg?.id) body.cfg_id = this._currentCfg.id;
      // Extra SeedLink sources (multi-server session): form rows first
      // (latest edit), else what is saved on the config.
      const extras = (this._readExtraSources().length
                      ? this._readExtraSources()
                      : (this._currentCfg?.extra_sources || []))
        .filter(s => s.host && s.port)
        .map(s => ({ host: s.host, port: +s.port,
                     inventory: s.inventory_path || s.inventory || '' }));
      if (extras.length) body.sources = extras;
      // Form checkbox first (latest edit), else what is saved on the config.
      const accelChk = document.getElementById('om-cfg-include-accel');
      body.include_accel = accelChk ? accelChk.checked : !!this._currentCfg?.include_accel;

      const r = await (await fetch('/api/online/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })).json();

      if (r.error) { this._setStatus('red', r.error); return; }

      // Store the last connection params → used by _autoReconnect when the server session
      // vanishes (e.g. server restart) so the waveform recovers without a manual click.
      // cfg_id must travel with it — otherwise a reconnect silently detaches the
      // session from its config and "Edit Configuration" can't find it again.
      this._lastConnect = { host, port, streams, inventory, bridge,
                            sources: body.sources || null, cfg_id: body.cfg_id || null,
                            include_accel: body.include_accel };

      this._activeStreams = r.streams || [];
      if (this._activeStreams.length > 0) {
        const s = this._activeStreams[0];
        this._station = `${s[0]}.${s[1]}.${s[2]}.${s[3]}`;
      }

      if (r.sources_skipped && r.sources_skipped.length) {
        const detail = r.sources_skipped.map((s, i) => `Extra ${i + 1} (${s.reason})`).join(', ');
        this._setStatus('amber', `Connected, but skipped: ${detail}`);
      } else {
        this._setStatus('green', r.resumed ? 'Live (resume)' : 'Connected');
      }
      document.getElementById('om-conn-info').innerHTML = this._addrBadge(true);
      document.getElementById('om-streams-info').textContent =
        `· ${r.n_streams || this._activeStreams.length} streams`;

      document.getElementById('om-btn-disconnect').classList.remove('hidden');
      document.getElementById('om-wv-station').textContent = `— ${this._activeStreams.length} stations`
        + (r.bridge_registered ? ' · bridge: SeisComP ✓' : '')
        + (r.resumed ? ' · buffer preserved' : '');

      this._showPanel('om-dashboard');
      // When resuming an already-running session, the server buffer is still full —
      // do not show the loading overlay (only needed for a fresh, empty connection).
      if (!r.resumed) {
        document.getElementById('om-wv-loading').style.display = 'flex';
        this._wvFirstDrawDone = false;
      }
      // Load the map from EVERY inventory (primary + extra sources) — passing
      // only r.inventory silently dropped extra sources' stations from the map
      // even though the backend (/api/online/stations) already merges a
      // comma-separated list via get_stations_multi().
      const invAll = (r.inventory_paths?.length ? r.inventory_paths : [r.inventory]).filter(Boolean);
      if (invAll.length) this._loadStationMap(invAll.join(','));
      this._startPoll();

      // Run the options from the confirmation modal (if any)
      if (this._pendingAutoDetect) {
        this._pendingAutoDetect = false;
        if (!this._realtimeActive) this.toggleRealtime();
      }
      if (this._pendingDoSlarchive !== undefined) {
        this._pendingDoSlarchive = undefined;
        // slarchive is started automatically by api_realtime_start when detection begins
      }
    } catch (e) {
      this._setStatus('red', 'Connect failed: ' + e.message);
    }
  }

  // Auto-reconnect when the server session vanishes (server restart) — re-POST /connect
  // with the last params, 10 s cooldown, without resetting the poll (already running).
  async _autoReconnect() {
    if (!this._lastConnect || this._reconnecting) return;
    const now = Date.now();
    if (this._lastReconnectAt && now - this._lastReconnectAt < 10000) return;
    this._reconnecting = true;
    this._lastReconnectAt = now;
    try {
      const lc = this._lastConnect;
      const body = { host: lc.host, port: lc.port };
      if (lc.streams)   body.streams   = lc.streams;
      if (lc.inventory) body.inventory = lc.inventory;
      if (lc.bridge?.agentUrl)   body.agent_url   = lc.bridge.agentUrl;
      if (lc.bridge?.agentToken) body.agent_token = lc.bridge.agentToken;
      if (lc.sources)            body.sources     = lc.sources;
      if (lc.cfg_id)             body.cfg_id      = lc.cfg_id;
      body.include_accel = !!lc.include_accel;
      // Pull the latest saved extra sources + accelerometer toggle from the
      // config instead of only trusting this tab's cache — a change made
      // since this tab's last connect() (e.g. from another tab, or a direct
      // API call) would otherwise silently vanish on every server restart.
      if (lc.cfg_id) {
        try {
          const c = await (await fetch(`/api/configs/${lc.cfg_id}`)).json();
          if (c?.extra_sources?.length) {
            body.sources = c.extra_sources
              .filter(s => s.host && s.port)
              .map(s => ({ host: s.host, port: +s.port,
                           inventory: s.inventory_path || s.inventory || '' }));
          }
          if (typeof c?.include_accel === 'boolean') body.include_accel = c.include_accel;
        } catch (_) { /* fall back to lc.* above */ }
      }
      const r = await fetch('/api/online/connect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(r => r.json());
      if (r.ok) {
        this._snapDone = false;      // new session → request a fresh snapshot
        this._sessionEpoch = null;
        this._setStatus('green', `Live (reconnect) — ${lc.host}:${lc.port}`);
      }
    } catch (_) { /* retry on the next tick */ }
    finally { this._reconnecting = false; }
  }

  // Called on page LOAD (DOMContentLoaded). If the server still has a live
  // session (browser refreshed but the server kept running), restore the dashboard +
  // history WITHOUT a manual reconnect. The first tick fetches /snapshot → returns the
  // buffer (up to ~31 minutes), so the waveform does not "start over".
  async restoreIfLive() {
    // Re-entrancy guard: avoid a double restore (page load + tab switch at once).
    if (this._restoring) return;
    // If the dashboard is already shown & the poll runs → no restore needed.
    const dash = document.getElementById('om-dashboard');
    if (dash && !dash.classList.contains('hidden') && this._statusTimer) return;
    this._restoring = true;
    try {
      const st = await fetch('/api/online/status').then(r => r.json());
      if (!st.connected) return;   // no session → leave the default (offline)
      switchProjectMode('online');
      // store params for auto-reconnect should the server session vanish later
      this._lastConnect = { host: st.host, port: st.port, streams: null,
                            inventory: st.inventory_path, bridge: null,
                            cfg_id: st.cfg_id || null };
      // A page refresh loses _currentCfgId (fresh JS state) even though the
      // server session is still tied to a config — restore it so "Edit
      // Configuration" can reload the saved form instead of an empty one.
      this._currentCfgId = st.cfg_id || null;
      this._activeStreams = (st.streams || []).map(k => String(k).split('.'));
      this._showPanel('om-dashboard');
      document.getElementById('om-btn-disconnect')?.classList.remove('hidden');
      document.getElementById('om-btn-settings')?.classList.remove('hidden');
      const ci = document.getElementById('om-conn-info');
      if (ci) ci.innerHTML = this._addrBadge(true);
      const si = document.getElementById('om-streams-info');
      if (si) si.textContent = `· ${(st.streams || []).length} streams`;
      document.getElementById('om-wv-station').textContent =
        `— ${(st.streams || []).length} stations · session restored`;
      this._setStatus('green', 'Live (restored)');
      document.getElementById('om-wv-loading').style.display = 'flex';
      this._wvFirstDrawDone = false;
      // Same fix as connect(): load every inventory (primary + extra sources),
      // not just the primary one, or extra sources' stations never show on the map.
      const invAllR = (st.inventory_paths?.length ? st.inventory_paths : [st.inventory_path]).filter(Boolean);
      if (invAllR.length) this._loadStationMap(invAllR.join(','));
      this._startPoll();   // _snapDone=false → first tick = snapshot (history)
    } catch (_) { /* no server / offline → ignore */ }
    finally { this._restoring = false; }
  }

  /* ── Agent Management ─────────────────────────────────────────────────────── */
  async agentRefreshStatus() {
    // The Agent status panel (dot + label) is optional in the compact config
    // layout — no-op cleanly when it isn't on the page.
    const dot = document.getElementById('om-agent-dot');
    const txt = document.getElementById('om-agent-status-txt');
    if (!dot || !txt) return;
    try {
      const r = await (await fetch('/api/agent/status')).json();
      if (r.running) {
        dot.className = 'om-dot om-dot-green';
        txt.textContent = `Running — localhost:${r.port}`;
        txt.style.color = '#22c55e';
      } else if (!r.has_conf) {
        dot.className = 'om-dot om-dot-amber';
        txt.textContent = 'Token not generated yet';
        txt.style.color = '';
      } else {
        dot.className = 'om-dot om-dot-grey';
        txt.textContent = 'Stopped';
        txt.style.color = '';
      }
    } catch (e) {
      document.getElementById('om-agent-status-txt').textContent = 'Error: ' + e.message;
    }
  }

  async agentInit() {
    const btn = document.getElementById('om-btn-agent-init');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Generating…'; }
    document.getElementById('om-agent-log-row').textContent = 'Generating token…';
    try {
      const r = await (await fetch('/api/agent/init', { method: 'POST' })).json();
      if (r.error) {
        document.getElementById('om-agent-log-row').textContent = 'Error: ' + r.error;
        return;
      }
      if (r.token) {
        // Show the token
        document.getElementById('om-agent-token-val').textContent = r.token;
        document.getElementById('om-agent-token-row').classList.remove('hidden');
        document.getElementById('om-agent-token-row').style.display = 'flex';
        document.getElementById('om-agent-log-row').textContent = '✓ Token generated successfully. Copy and save it — it is shown only once.';
        // Auto-fill URL (localhost)
        const urlEl = document.getElementById('om-cfg-agent-url');
        if (urlEl && !urlEl.value) urlEl.value = 'http://localhost:7001';
      } else {
        document.getElementById('om-agent-log-row').textContent = r.output || 'Init complete (token not found in the output)';
      }
      await this.agentRefreshStatus();
    } catch (e) {
      document.getElementById('om-agent-log-row').textContent = 'Error: ' + e.message;
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-key"></i> Generate Token'; }
    }
  }

  async agentStart() {
    const btn = document.getElementById('om-btn-agent-start');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Starting…'; }
    document.getElementById('om-agent-log-row').textContent = 'Starting agent…';
    try {
      const r = await (await fetch('/api/agent/start', { method: 'POST' })).json();
      document.getElementById('om-agent-log-row').textContent =
        r.ok ? `✓ ${r.message}${r.pid ? ` (PID ${r.pid})` : ''}` : `✗ ${r.error || r.message}`;
      await this.agentRefreshStatus();
      // Auto-fill the URL when empty
      const urlEl = document.getElementById('om-cfg-agent-url');
      if (urlEl && !urlEl.value) urlEl.value = 'http://localhost:7001';
    } catch (e) {
      document.getElementById('om-agent-log-row').textContent = 'Error: ' + e.message;
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Start'; }
    }
  }

  async agentStop() {
    const btn = document.getElementById('om-btn-agent-stop');
    if (btn) { btn.disabled = true; }
    try {
      const r = await (await fetch('/api/agent/stop', { method: 'POST' })).json();
      document.getElementById('om-agent-log-row').textContent = r.ok ? '✓ Agent stopped' : '✗ Stop failed';
      await this.agentRefreshStatus();
    } catch (e) {
      document.getElementById('om-agent-log-row').textContent = 'Error: ' + e.message;
    } finally {
      if (btn) { btn.disabled = false; }
    }
  }

  agentCopyToken() {
    const val = document.getElementById('om-agent-token-val')?.textContent;
    if (!val) return;
    navigator.clipboard?.writeText(val).then(() => {
      document.getElementById('om-agent-log-row').textContent = '✓ Token disalin ke clipboard';
    }).catch(() => {
      // Fallback manual selection
      const el = document.getElementById('om-agent-token-val');
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
    });
  }

  agentAutoFill() {
    const token = document.getElementById('om-agent-token-val')?.textContent?.trim();
    if (token) document.getElementById('om-cfg-agent-token').value = token;
    const urlEl = document.getElementById('om-cfg-agent-url');
    if (urlEl && !urlEl.value) urlEl.value = 'http://localhost:7001';
    document.getElementById('om-agent-log-row').textContent = '✓ Token & URL telah di-auto-fill ke form Sync';
  }

  toggleTokenVisibility() {
    const inp = document.getElementById('om-cfg-agent-token');
    const eye = document.getElementById('om-token-eye');
    if (!inp) return;
    inp.type = inp.type === 'password' ? 'text' : 'password';
    eye.innerHTML = inp.type === 'password' ? '<i class="bi bi-eye"></i>' : '<i class="bi bi-eye-slash"></i>';
  }

  /* ── Disconnect ────────────────────────────────────────────────────────────── */
  async disconnect() {
    this._clientBufs.clear();
    this._snapDone = false;
    this._sessionEpoch = null;
    this._pickLogSince = 0.0;
    this._pickLogTotal = 0;
    this._pickStore.clear();
    this._lastConnect = null;   // manual disconnect → do not auto-reconnect
    this._stopPoll();
    try { await fetch('/api/online/realtime/stop', { method: 'POST' }); } catch (_) {}
    try { await fetch('/api/online/disconnect', { method: 'POST' }); } catch (_) {}
    document.getElementById('om-btn-disconnect').classList.add('hidden');
    document.getElementById('om-btn-settings')?.classList.add('hidden');
    document.getElementById('om-settings-menu')?.classList.add('hidden');
    document.getElementById('om-streams-info').textContent = '';
    // Switch the tab back to Event (detail) and reset the pick log UI
    this.showFeedTab('event');
    const badge = document.getElementById('om-pick-badge');
    if (badge) badge.textContent = '0';
    const plEl = document.getElementById('om-pick-log-list');
    if (plEl) plEl.innerHTML = '';
    this._setStatus('grey', 'Disconnected');
    this._clearMap();
    this.hideStationInfo();
    if (this._currentCfgId) {
      await this.openConfig(this._currentCfgId);   // back to the config wizard
    } else {
      this.showHome();
    }
  }

  /* ── Poll Loop ─────────────────────────────────────────────────────────────── */
  _startPoll() {
    this.refreshSdsChip();
    this._clientBufs.clear();
    this._snapDone     = false;
    this._sessionEpoch = null;
    this._rtt        = null;
    this._tickFails  = 0;
    this._specStaleMs = 0;
    this._pickLogSince = 0.0;
    this._pickLogTotal = 0;
    this._pickStore.clear();
    this._assocLogSeen = 0;
    const plEl = document.getElementById('om-pick-log-list');
    if (plEl) plEl.innerHTML =
      '<div class="om-placeholder">Waiting for picks from the bridge/PhaseNet…</div>';
    const badge = document.getElementById('om-pick-badge');
    if (badge) badge.textContent = '0';
    this._stopPoll();
    // Waveform: SSE push (server sends data when available) — independent of client
    // polling, so a slow connection does not cause "Disconnected" or an empty waveform.
    this._startWaveformSSE();
    // Status (packet count, marker colors, auto-reconnect): light poll every 5 s.
    this._statusTimer    = setInterval(() => this._tick(), 5000);
    this._axisTimer      = setInterval(() => this._redrawAxis(), 1000);
    this._diagTimer      = setInterval(() => this._updateNetStatus(), 2000);
    this._specTimer      = setInterval(() => this._tickSpec(), 5000);
    this._pickLogTimer   = setInterval(() => this._pollPickLog(), 2000);
    this._tick();           // first status immediately
    this._tickSpec();
    this._pollPickLog();
    const outer = document.getElementById('om-wv-plot');
    if (outer && !this._scrollAttached) {
      this._scrollAttached = true;
      outer.addEventListener('scroll', () => {
        // renderWaveformCanvas culls rows outside the current viewport for
        // speed, keyed off scrollTop at draw time. Redraws otherwise only
        // happen on the next SSE/poll tick, so a row scrolled into view
        // shows blank until then ("disappears, then reappears"). Repaint
        // from the already-cached data (no network call) so the canvas
        // stays in sync with the scroll position as it happens — throttled
        // to one repaint per animation frame since scroll fires much faster.
        if (!this._scrollRaf) {
          this._scrollRaf = requestAnimationFrame(() => {
            this._scrollRaf = null;
            if (this._lastGoodStreams) {
              this._drawWaveformAll(this._lastGoodStreams, this._lastSpecsByKey || {});
            }
          });
        }
        clearTimeout(this._scrollDebounce);
        this._scrollDebounce = setTimeout(() => this._tickSpec(), 200);
      });
    }
    this._attachWvHover();
    document.getElementById('om-btn-settings')?.classList.remove('hidden');
    this._restoreAutoStartUI();
    this._loadCatalog();
    this._loadRelocIfAvailable();
    this._realtimeTimer = setInterval(() => this._pollRealtime(), 5000);
    this._pollRealtime();
    this._maybeAutoStartRealtime();
    // Once the operator has run HypoDD at least once, keep it fresh: check every
    // 10 min whether new events arrived and, if so, re-run automatically (see
    // _autoRelocCheck). Never auto-starts a relocation that was never requested.
    this._relocAutoTimer = setInterval(() => this._autoRelocCheck(), 10 * 60 * 1000);
  }

  _stopPoll() {
    this._stopWaveformSSE();
    if (this._statusTimer)   { clearInterval(this._statusTimer);   this._statusTimer   = null; }
    if (this._axisTimer)     { clearInterval(this._axisTimer);     this._axisTimer     = null; }
    if (this._specTimer)     { clearInterval(this._specTimer);     this._specTimer     = null; }
    if (this._diagTimer)     { clearInterval(this._diagTimer);     this._diagTimer     = null; }
    if (this._realtimeTimer) { clearInterval(this._realtimeTimer); this._realtimeTimer = null; }
    if (this._pickLogTimer)  { clearInterval(this._pickLogTimer);  this._pickLogTimer  = null; }
    if (this._relocAutoTimer){ clearInterval(this._relocAutoTimer);this._relocAutoTimer= null; }
  }

  _pausePoll() {
    this._stopPoll();
    this._staModalCloseTimer?.();
  }

  _resumePoll() {
    if (this._statusTimer) return;
    if (!this._wvFirstDrawDone) return;
    this._startWaveformSSE();
    this._statusTimer   = setInterval(() => this._tick(), 5000);
    this._axisTimer     = setInterval(() => this._redrawAxis(), 1000);
    this._specTimer     = setInterval(() => this._tickSpec(), 5000);
    this._diagTimer     = setInterval(() => this._updateNetStatus(), 2000);
    this._realtimeTimer = setInterval(() => this._pollRealtime(), 5000);
    this._pickLogTimer  = setInterval(() => this._pollPickLog(), 2000);
    this._relocAutoTimer = setInterval(() => this._autoRelocCheck(), 10 * 60 * 1000);
    this._tick();
  }

  /* ── SSE Waveform Stream ──────────────────────────────────────────────────────
   * Replaces polling /waveform/delta every 2s. The server pushes data to the client
   * when available — a long-lived HTTP connection (EventSource). Slow internet does not
   * cause a timeout/"Disconnected": data arrives later but is never lost.
   * EventSource reconnects automatically after a brief network drop.
   * ─────────────────────────────────────────────────────────────────────────── */
  /* Loading-placeholder diagnostics: "Loading waveform data…" used to sit
     unchanged forever when something upstream was broken, hiding the reason.
     While the placeholder is visible, diagnose every 3s from state this module
     already tracks and show the concrete cause + what to check — the message
     is corrected/extended, never removed, until real data draws over it. */
  _startWvLoadingDiag() {
    this._stopWvLoadingDiag();
    this._wvLoadingSince = Date.now();
    this._wvDiagTimer = setInterval(() => this._updateWvLoadingDiag(), 3000);
  }

  _stopWvLoadingDiag() {
    if (this._wvDiagTimer) { clearInterval(this._wvDiagTimer); this._wvDiagTimer = null; }
  }

  _updateWvLoadingDiag() {
    const el = document.getElementById('om-wv-loading');
    if (!el || el.style.display === 'none') { this._stopWvLoadingDiag(); return; }
    const secs = Math.round((Date.now() - (this._wvLoadingSince || Date.now())) / 1000);
    if (secs < 8) return;   // normal startup latency — keep the plain message

    let cause, tips;
    if ((this._tickFails || 0) > 0 || this._wfOk === false) {
      cause = 'The waveform stream (SSE) is not connected — the server is unreachable or restarting.';
      tips  = ['Check that the SeisWork service is running (systemctl --user status seiswork).',
               'Reload this page once the server is back.'];
    } else if (this._lastSlConn === false) {
      cause = 'Server is up, but SeedLink is not connected — no data is flowing in.';
      tips  = ['Check the SeedLink host/port in this config (Settings / Sync tab).',
               'Verify the remote SeedLink server is reachable from the SeisWork machine.'];
    } else {
      cause = 'Connected — no drawable waveform packets received yet for the selected stations.';
      tips  = ['The stations may be down, or transmitting a different channel than configured.',
               'Run channel verification (Sync tab), or select another station.',
               'Data appears automatically as soon as packets arrive — no reload needed.'];
    }
    el.innerHTML =
      `<div style="display:flex;flex-direction:column;gap:.3rem;align-items:center;text-align:center;max-width:520px;padding:.4rem">
        <div><i class="bi bi-hourglass-split"></i>&nbsp;Loading waveform data… <span style="color:#94a3b8">(${secs}s)</span></div>
        <div style="font-size:.68rem;color:#d97706"><i class="bi bi-exclamation-triangle"></i> ${cause}</div>
        <ul style="font-size:.64rem;color:#64748b;text-align:left;margin:0;padding-left:1.1rem">
          ${tips.map(t => `<li>${t}</li>`).join('')}
        </ul>
      </div>`;
  }

  _startWaveformSSE() {
    this._stopWaveformSSE();
    this._startWvLoadingDiag();
    // Binds this connection to the config we know about client-side, so it
    // keeps following THIS config even if a different one becomes active
    // server-side in the meantime. Omitted (no known cfg yet) → server
    // follows whichever config is currently active, same as before this
    // stream knew about cfg_id.
    const url = this._currentCfgId
      ? `/api/online/waveform/stream?cfg_id=${encodeURIComponent(this._currentCfgId)}`
      : '/api/online/waveform/stream';
    const es = new EventSource(url);
    this._waveformSSE = es;

    es.addEventListener('message', (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }

      this._lastTickMs = Date.now();
      this._tickFails  = 0;

      if (data.server_time) {
        this._updateServerOffset(data.server_time);
        this._updateDataOffset(data.server_time);
      }
      if (data.sl_connected != null) {
        this._updateNetStatus({ slConnected: data.sl_connected });
      }

      if (data.type === 'snapshot') {
        this._applySnapshot(data);
      } else if (data.type === 'delta') {
        this._applyDelta(data);
      } else if (data.type === 'heartbeat') {
        this._updateNetStatus({ slConnected: !!data.connected, wfOk: true });
        return;
      } else {
        return;
      }

      // Render only when the online tab is visible — otherwise the buffer is kept
      // and renders as soon as the user returns to the tab (SSE keeps running).
      if (document.getElementById('sw-online-root')?.classList.contains('hidden')) {
        this._updateNetStatus({ wfOk: true });
        return;
      }
      const streams = this._clientBufsToStreams();
      if (!streams.length) return;
      const good = streams.filter(s => s.points.length > 1);
      good.sort((a, b) => (a.net + a.sta + a.cha) < (b.net + b.sta + b.cha) ? -1 : 1);
      if (!good.length) return;
      this._lastGoodStreams     = good;
      this._wvRowOrderKeys      = good.map(s => s.key);
      this._wvRowOrder          = good.map(s => `${s.net}.${s.sta}`);
      this._wvRowH              = 46 + 48 + 1;
      const spsEl = document.getElementById('om-wv-sps');
      if (spsEl) spsEl.textContent = `(${good.length} stream)`;
      const _loadingEl = document.getElementById('om-wv-loading');
      const _good = good, _specs = this._lastSpecsByKey || {};
      requestAnimationFrame(() => {
        this._drawWaveformAll(_good, _specs);
        if (_loadingEl) _loadingEl.style.display = 'none';
        this._stopWvLoadingDiag();
        this._wvFirstDrawDone = true;
      });
      this._updateNetStatus({ wfOk: true });
    });

    es.addEventListener('open', () => {
      this._tickFails = 0;
      this._updateNetStatus({ wfOk: true });
    });

    es.addEventListener('error', () => {
      // EventSource reconnects automatically — only record the failure for the "Disconnected" hysteresis
      this._tickFails = (this._tickFails || 0) + 1;
      this._updateNetStatus({ wfOk: false });
    });
  }

  _stopWaveformSSE() {
    this._stopWvLoadingDiag();
    if (this._waveformSSE) {
      this._waveformSSE.close();
      this._waveformSSE = null;
    }
  }

  /* ── Fase 2: Real-time PhaseNet → Asosiasi (GaMMA) → Magnitude ML ────────── */
  /* ── Menu Settings (gear) ──────────────────────────────────────────────────── */
  toggleSettingsMenu(ev) {
    if (ev) ev.stopPropagation();
    const menu = document.getElementById('om-settings-menu');
    if (!menu) return;
    const willOpen = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !willOpen);
    if (willOpen) {
      // close on outside click (one-shot)
      const close = (e) => {
        if (!document.getElementById('om-btn-settings')?.contains(e.target)) {
          menu.classList.add('hidden');
          document.removeEventListener('click', close);
        }
      };
      setTimeout(() => document.addEventListener('click', close), 0);
    }
  }

  setAutoStart(checked) {
    try { localStorage.setItem('om_autostart_rt', checked ? '1' : '0'); } catch (_) {}
  }

  _restoreAutoStartUI() {
    let on = false;
    try { on = localStorage.getItem('om_autostart_rt') === '1'; } catch (_) {}
    const cb = document.getElementById('om-set-autostart');
    if (cb) cb.checked = on;
  }

  _maybeAutoStartRealtime() {
    let on = false;
    try { on = localStorage.getItem('om_autostart_rt') === '1'; } catch (_) {}
    if (on && !this._realtimeActive) {
      // small delay so the realtime status gets polled first (avoids a double start)
      setTimeout(() => { if (!this._realtimeActive) this.toggleRealtime(); }, 1500);
    }
  }

  /* ── Persistent event catalog (loaded from disk) ──────────────────────────── */
  async _loadCatalog() {
    try {
      const r = await (await fetch('/api/online/realtime/catalog?limit=200')).json();
      const events = this._filterByViewer(r.events || []);
      if (events.length && (!this._events || !this._events.length)) {
        this._events = events;
        this._renderEventFeed(events);
        this._renderEventDetail(events[this._selEventIdx] || events[0] || null,
                                this._lastSdsPath || '');
        this._plotEventsOnMap(events);
      }
    } catch (_) { /* transient */ }
  }

  async dedupCatalog() {
    try {
      const r = await (await fetch('/api/online/realtime/dedup', { method: 'POST' })).json();
      const msg = r.removed > 0
        ? `${r.removed} duplicate events removed. Remaining: ${r.remaining} events.`
        : `No duplicates found. Total: ${r.remaining} events.`;
      alert(msg);
      // Refresh event feed
      const ev = await (await fetch('/api/online/realtime/catalog?limit=200')).json();
      this._events = this._filterByViewer(ev.events || []);
      this._renderEventFeed(this._events);
      this._renderEventDetail(this._events[0] || null, this._lastSdsPath || '');
    } catch (e) {
      alert('Dedup failed: ' + e.message);
    }
  }

  // ── Confirm Start Monitoring modal ───────────────────────────────────────
  openMonitorModal() {
    const cfg = this._currentCfg;
    const cfgId = this._currentCfgId;
    const v = id => document.getElementById(id);

    // Build checklist items from the current state (SeedLink host/port is
    // configured, not shown — see _addrBadge/the IP-masking note above).
    const isSynced = !!cfg?.synced;
    const nSta  = cfg?.n_stations || '?';
    const inv   = cfg?.inventory_path || cfg?.inventory || '(auto-detect)';

    const pThr = parseFloat(v('om-p-thr')?.value ?? 0.3);
    const sThr = parseFloat(v('om-s-thr')?.value ?? 0.6);

    // Extra SeedLink sources — form rows first (latest, possibly unsaved edit),
    // else whatever is saved on the config. Same priority connect() uses, so
    // what's shown here is exactly what will be sent.
    const extras = (this._readExtraSources().length
                    ? this._readExtraSources()
                    : (cfg?.extra_sources || [])).filter(s => s.host && s.port);

    const items = [
      { ok: !!cfgId,   label: `Config: <b>${cfg?.name || cfgId || '—'}</b>`,
        sub: cfgId || '' },
      { ok: isSynced,  label: 'Status: ' + (isSynced ? '<b style="color:#22c55e">synced</b> with SeisComP'
                                                       : '<b style="color:#f59e0b">not synced yet</b> — run Sync first'),
        warn: !isSynced },
      { ok: true,      label: 'SeedLink: <b>configured</b>',
        sub: 'direct connection to the config seedlink' },
      { ok: !!inv,     label: 'Inventory: ' + `<span style="font-family:monospace;font-size:.75em">${
                                (inv.length > 42 ? '…' + inv.slice(-40) : inv)}</span>` },
      { ok: nSta > 0,  label: `Stations: <b>${nSta}</b> active` },
      { ok: true,      label: `PhaseNet threshold: P <b>${pThr}</b> · S <b>${sThr}</b>` },
      { ok: true,      label: 'SDS waveform: <b>SeisComP archive</b>' +
        (localStorage.getItem('sw_auto_inject') === '1' ? ' + slarchive (fallback)' : '') },
      ...(extras.length ? [{
        ok: true,
        label: `Extra SeedLink sources: <b>${extras.length}</b>`,
        sub: extras.map((s, i) => `Extra ${i + 1}${(s.inventory_path || s.inventory) ? ' (own inventory)' : ' (no inventory — will be skipped)'}`).join(', '),
        warn: extras.some(s => !(s.inventory_path || s.inventory)),
      }] : []),
    ];

    const cl = v('om-monitor-checklist');
    if (cl) {
      cl.innerHTML = items.map(it => `
        <div style="display:flex;gap:.5rem;align-items:flex-start">
          <span style="font-size:.85rem;margin-top:.05rem;flex-shrink:0;color:${
            it.warn ? '#f59e0b' : (it.ok ? '#22c55e' : '#ef4444')}">
            <i class="bi bi-${it.warn ? 'exclamation-triangle-fill' : (it.ok ? 'check-circle-fill' : 'x-circle-fill')}"></i>
          </span>
          <div style="line-height:1.4">
            ${it.label}
            ${it.sub ? `<div style="font-size:.7rem;color:var(--text-muted)">${it.sub}</div>` : ''}
          </div>
        </div>`).join('');
    }

    // Sync the modal options with the card state
    const autoInj = v('om-mc-inject');
    if (autoInj) autoInj.checked = localStorage.getItem('sw_auto_inject') === '1';
    const autoInj2 = v('om-inject-auto');
    if (autoInj2) autoInj2.checked = localStorage.getItem('sw_auto_inject') === '1';
    const autoStartCard = v('om-set-autostart');
    const autoDetectChk = v('om-mc-autodetect');
    if (autoDetectChk && autoStartCard) autoDetectChk.checked = autoStartCard.checked;

    // Disable the Start button when not yet synced (warning, but can proceed)
    const confirmBtn = v('om-mc-confirm-btn');
    if (confirmBtn) confirmBtn.disabled = false;  // can still start; synced is only a warning

    v('om-monitor-modal-backdrop').style.display = '';
    v('om-monitor-modal').style.display = '';
  }

  closeMonitorModal() {
    document.getElementById('om-monitor-modal-backdrop').style.display = 'none';
    document.getElementById('om-monitor-modal').style.display = 'none';
  }

  async confirmStartMonitor() {
    const v = id => document.getElementById(id);
    const autoDetect  = v('om-mc-autodetect')?.checked ?? true;
    const autoInject  = v('om-mc-inject')?.checked    ?? false;
    const doSlarchive = v('om-mc-slarchive')?.checked ?? true;
    // Sync the card's auto-start checkbox with the modal choice
    const autoStartChk = v('om-set-autostart');
    if (autoStartChk) { autoStartChk.checked = autoDetect; OM.setAutoStart(autoDetect); }

    // Save the inject choice to localStorage before connecting
    localStorage.setItem('sw_auto_inject', autoInject ? '1' : '0');
    if (v('om-inject-auto')) v('om-inject-auto').checked = autoInject;
    if (v('om-mc-inject')) v('om-mc-inject').checked = autoInject;

    // Disable the confirm button while processing
    if (v('om-mc-confirm-btn')) {
      v('om-mc-confirm-btn').disabled = true;
      v('om-mc-confirm-btn').innerHTML = '<i class="bi bi-hourglass-split"></i> Starting…';
    }

    // Start monitoring (startMonitorFromConfig already handles connect + auto-start)
    this.closeMonitorModal();
    // Pass options to startMonitorFromConfig via temporary flags
    this._pendingAutoDetect  = autoDetect;
    this._pendingDoSlarchive = doSlarchive;
    await this.startMonitorFromConfig();

    if (v('om-mc-confirm-btn')) {
      v('om-mc-confirm-btn').disabled = false;
      v('om-mc-confirm-btn').innerHTML = '<i class="bi bi-play-fill"></i> Start';
    }
  }

  // ── Inject picks ke SeisComP ─────────────────────────────────────────────
  toggleAutoInject(on) {
    localStorage.setItem('sw_auto_inject', on ? '1' : '0');
    const log = document.getElementById('om-inject-log');
    if (log) log.textContent = on ? 'Auto-inject active — new picks will be sent every cycle' : '';
  }

  async injectNow() {
    const log = document.getElementById('om-inject-log');
    try {
      if (log) log.textContent = 'Injecting located events into scevent…';
      // Inject SeisWork's OWN located events (origin + arrivals + picks) straight
      // into scevent under agency "SeisWork" — NOT picks into scautoloc. When the
      // config has an agent (SeisComP on another host / public IP), route the SCML
      // through the agent's /dispatch (mode=auto); else scdispatch runs locally.
      const cfg = this._currentCfg || {};
      const body = {
        agency_id: 'SeisWork', author: 'SeisWork-AI',
        method_id: 'PhaseNet-REAL-NLLoc',
        since: this._lastInjectISO || 0,
        mode: (cfg.agent_url ? 'remote' : 'local'),
        agent_url: cfg.agent_url || '', agent_token: cfg.agent_token || '',
      };
      const r = await (await fetch('/api/online/inject/events', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })).json();
      // Advance the watermark so the next inject only sends NEW events (UTC ISO).
      if (r.n_events > 0) this._lastInjectISO = new Date().toISOString();
      if (log) log.textContent = r.ok
        ? `✓ ${r.n_events} event(s), ${r.n_picks} pick(s) → scevent (agency=SeisWork, via ${r.via}) — ${new Date().toISOString().slice(11,19)}Z`
        : `✗ ${r.error || (r.agent && r.agent.error) || 'inject failed'}`;
    } catch (e) {
      if (log) log.textContent = '✗ ' + e.message;
    }
  }

  // Read all values from the picker+gamma config form
  _readCfgForm() {
    const _num = (id, def) => { const v = parseFloat(document.getElementById(id)?.value); return isFinite(v) ? v : def; };
    const _int = (id, def) => { const v = parseInt(document.getElementById(id)?.value); return isFinite(v) ? v : def; };
    return {
      p_threshold        : _num('om-p-thr', 0.3),
      s_threshold        : _num('om-s-thr', 0.6),
      denoise            : document.getElementById('om-denoise')?.checked ?? false,
      denoise_pretrained : document.getElementById('om-denoise-pretrained')?.value || 'original',
      assoc_backend: document.getElementById('om-assoc-backend')?.value || 'real',
      gamma: {
        min_picks_per_eq: _int('om-gamma-min-picks', 4),
        min_stations    : _int('om-gamma-min-stations', 4),
        max_sigma       : _num('om-gamma-max-sigma',  2.0),
        depth_max       : _num('om-gamma-depth-max',  60.0),
        method          : document.getElementById('om-gamma-method')?.value || 'BGMM',
        use_amplitude   : document.getElementById('om-gamma-use-amp')?.checked ?? true,
      },
    };
  }

  // Show/hide the GaMMA params per selected engine (REAL → hidden).
  onBackendChange() {
    const be   = document.getElementById('om-assoc-backend')?.value || 'real';
    const isReal = be !== 'gamma';
    const params = document.getElementById('om-gamma-params');
    const note   = document.getElementById('om-real-note');
    if (params) params.style.display = isReal ? 'none' : 'block';
    if (note)   note.style.display   = isReal ? 'block' : 'none';
  }

  // Fill the form from a config object (e.g. from the /config API)
  _applyCfgForm(cfg) {
    if (!cfg) return;
    const _set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const _chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };
    _set('om-p-thr',              cfg.picker?.p_threshold        ?? 0.3);
    _set('om-s-thr',              cfg.picker?.s_threshold        ?? 0.6);
    _chk('om-denoise',            cfg.picker?.denoise            ?? false);
    _set('om-denoise-pretrained', cfg.picker?.denoise_pretrained ?? 'original');
    // sync visibility of DeepDenoiser model row
    { const row = document.getElementById('om-denoise-row');
      if (row) row.style.display = (cfg.picker?.denoise) ? 'grid' : 'none'; }
    _set('om-gamma-min-picks', cfg.gamma?.min_picks_per_eq ?? 4);
    _set('om-gamma-min-stations', cfg.gamma?.min_stations ?? 4);
    _set('om-gamma-max-sigma', cfg.gamma?.max_sigma   ?? 2.0);
    _set('om-gamma-depth-max', cfg.gamma?.depth_max   ?? 60.0);
    _set('om-gamma-method',    cfg.gamma?.method      ?? 'BGMM');
    _chk('om-gamma-use-amp',   cfg.gamma?.use_amplitude ?? true);
    _set('om-assoc-backend',   cfg.backend ?? 'real');
    this.onBackendChange();   // sync GaMMA param visibility with the selected engine
  }

  // Lock/unlock the config form based on the detection status
  _setCfgLocked(locked) {
    ['om-picker-cfg-block', 'om-gamma-cfg-block'].forEach(id => {
      document.getElementById(id)?.classList.toggle('cfg-locked', locked);
    });
    const note = document.getElementById('om-cfg-running-note');
    if (note) note.classList.toggle('visible', locked);
  }

  async toggleRealtime() {
    const btn = document.getElementById('om-btn-realtime');
    if (btn) btn.disabled = true;
    try {
      if (this._realtimeActive) {
        await fetch('/api/online/realtime/stop', { method: 'POST' });
      } else {
        const payload = this._readCfgForm();
        const r = await (await fetch('/api/online/realtime/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })).json();
        if (r.error) { this._setStatus('red', r.error); return; }
      }
      await this._pollRealtime();
    } catch (e) {
      this._setStatus('red', 'Realtime toggle failed: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async _pollRealtime() {
    try {
      const st = await (await fetch('/api/online/realtime/status')).json();
      this._realtimeActive = !!(st.picker?.running || st.associator?.running);
      const btn   = document.getElementById('om-btn-realtime');
      const label = document.getElementById('om-realtime-label');
      const rtSt  = document.getElementById('om-rt-status');
      if (btn && label) {
        btn.classList.toggle('btn-danger-sw', this._realtimeActive);
        btn.classList.toggle('btn-ghost-sw', !this._realtimeActive);
        label.textContent = this._realtimeActive ? 'Stop Real-Time Detection' : 'Start Real-Time Detection';
      }
      // Lock/unlock the config form + load active values from the server while detection runs
      this._setCfgLocked(this._realtimeActive);
      if (this._realtimeActive && !this._cfgLoaded) {
        this._cfgLoaded = true;
        fetch('/api/online/realtime/config').then(r => r.json()).then(c => this._applyCfgForm(c)).catch(() => {});
      }
      if (!this._realtimeActive) this._cfgLoaded = false;
      if (rtSt) {
        const slSt = st.slarchive;
        const liveStatus = st.live_session || {};
        const sdsBuiltin = liveStatus.sds_path;
        const slStatus = sdsBuiltin
          ? `<span style="color:#22c55e;margin-left:.5rem" title="Built-in ObsPy SDS active → ${sdsBuiltin} (${liveStatus.sds_written||0} traces)">● SDS built-in</span>`
          : slSt?.running
          ? `<span style="color:#22c55e;margin-left:.5rem" title="slarchive active → online_sds">● slarchive</span>`
          : slSt?.skipped
          ? `<span style="color:#60a5fa;margin-left:.5rem" title="SeisComP SDS is fresh → slarchive is not needed">≡ sc-archive</span>`
          : `<span style="color:#94a3b8;margin-left:.5rem">○ slarchive</span>`;
        const nl = st.associator?.nlloc;
        const nlBadge = nl
          ? (nl.active
              ? `<span style="color:#22c55e;margin-left:.5rem" title="Hypocenter di-refine NonLinLoc, grid ${nl.bundled ? 'bundel' : 'lokal'}: ${nl.grid_dir}">● NLLoc ${nl.profile}</span>`
              : `<span style="color:#94a3b8;margin-left:.5rem" title="${nl.reason || 'inactive'}">○ NLLoc</span>`)
          : '';
        // Association backend (REAL / GaMMA / PyOcto / glass3) — which "engine" is used
        this._assocBackend = st.associator?.backend || 'real';
        const beInfo  = this._beInfo(this._assocBackend);
        const beLabel = beInfo.label;
        const beBadge = `<span style="color:${beInfo.color};margin-left:.5rem" title="Association uses ${beLabel}">${beInfo.icon} ${beLabel}</span>`;
        rtSt.innerHTML = this._realtimeActive
          ? `picker: ${st.picker.n_cycles} cycles, ${st.picker.n_picks_total} pick · `
            + `association ${beLabel}: ${st.associator.n_events_total} event`
            + (st.picker.error ? ` · ⚠ ${st.picker.error}` : '')
            + (st.associator.error ? ` · ⚠ ${st.associator.error}` : '')
            + beBadge
            + nlBadge
            + slStatus
          : '';
      }
      if (this._realtimeActive) {
        const ev = await (await fetch('/api/online/realtime/events')).json();
        const events = this._filterByViewer(ev.events || []);
        // A newly-associated event → blink its recording stations the SAME
        // color as a raw pick (see _blinkStation): visually, "these picks
        // just became an event" rather than a different, unrelated color.
        // Guarded by _seenEventIds so re-polling the same event every few
        // seconds doesn't re-trigger the blink forever.
        events.forEach(e => {
          if (!e.event_id || this._seenEventIds.has(e.event_id)) return;
          this._seenEventIds.add(e.event_id);
          (e.stations || []).forEach(key => {
            const [net, sta] = key.split('.');
            if (net && sta) this._blinkStation(net, sta, '#facc15');
          });
        });
        this._events = events;
        this._renderEventFeed(events);
        this._renderEventDetail(events[this._selEventIdx] || events[0] || null,
                                st.associator?.sds_path || '');
        this._renderEngineCard(st.engine_log || [], st.picker, st.associator,
                               st.associator?.sds_path || '');
        // Show the map per mode: relocated or original
        if (this._showingReloc && this._relocEvents?.length) {
          this._plotEventsOnMap(this._relocEvents);
        } else {
          this._plotEventsOnMap(events);
        }
        // Auto-inject picks into SeisComP when enabled
        if (localStorage.getItem('sw_auto_inject') === '1') {
          this.injectNow().catch(() => {});
        }
      }
    } catch (_) { /* transient */ }
  }

  // ── Event row (used both in the inline panel and the catalog modal) ────────
  // Networks of the recording stations (source discriminator, e.g. AM=Raspberry Shake).
  _eventNetworks(ev) {
    let nets = ev.networks;
    if ((!nets || !nets.length) && ev.stations && ev.stations.length)
      nets = [...new Set(ev.stations.map(s => String(s).split('.')[0]).filter(Boolean))].sort();
    return nets || [];
  }

  _netColor(n) {
    const map = { AM: '#7c3aed', IA: '#0ea5e9', AF: '#f59e0b', AK: '#ef4444', '7G': '#10b981' };
    if (map[n]) return map[n];
    let h = 0; for (const c of n) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    const palette = ['#6366f1', '#06b6d4', '#f97316', '#84cc16', '#ec4899', '#14b8a6'];
    return palette[h % palette.length];
  }

  // Badge jaringan pencatat — dibedakan per config/sumber.
  _netBadge(ev) {
    const nets = this._eventNetworks(ev);
    if (!nets.length) return '';
    return `<span title="Recording station networks: ${nets.join(', ')}">${nets.map(n =>
      `<span style="display:inline-block;background:${this._netColor(n)};color:#fff;border-radius:3px;` +
      `padding:0 4px;font-size:.6rem;font-weight:700;margin-right:.2rem;letter-spacing:.02em">${n}</span>`
    ).join('')}</span>`;
  }

  // Per-config mirror: filter events to the config's STATIONS (precise), falling back
  // to networks. An event passes if ≥1 recording station belongs to this config.
  _filterByViewer(events) {
    if (!Array.isArray(events)) return [];
    const stas = this._viewerStas
      || (typeof window !== 'undefined' && window.SEISWORK_VIEWER_STAS) || null;
    if (stas && stas.length) {
      const set = new Set(stas);
      return events.filter(ev => (ev.stations || []).some(s => set.has(String(s))));
    }
    const nets = this._viewerNets
      || (typeof window !== 'undefined' && window.SEISWORK_VIEWER_NETS) || null;
    if (!nets || !nets.length) return events;
    return events.filter(ev => this._eventNetworks(ev).some(n => nets.includes(n)));
  }

  /* ── Share: read-only mirror URL (port 3346) for the active config ───────── */
  shareMirror() {
    const cfgId = this._currentCfgId
      || (typeof window !== 'undefined' && window.SEISWORK_VIEWER_CFG) || '';
    const name = (this._currentCfg && this._currentCfg.name)
      || (typeof window !== 'undefined' && window.SEISWORK_VIEWER_CFG_NM) || '';
    const slug = String(name || cfgId).toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || String(cfgId);
    if (!slug) { this._setStatus('amber', 'Open a config first to create a mirror link'); return; }
    const port = (typeof window !== 'undefined' && window.SEISWORK_MIRROR_PORT) || 3346;
    const url = `${location.protocol}//${location.hostname}:${port}/${slug}`;
    const inp = document.getElementById('om-share-url');
    if (inp) inp.value = url;
    const a = document.getElementById('om-share-open');
    if (a) a.href = url;
    const sub = document.getElementById('om-share-sub');
    if (sub) sub.textContent = name
      ? `Read-only live mirror for config "${name}" (port ${port}).`
      : `Mirror live read-only (port ${port}).`;
    const copied = document.getElementById('om-share-copied');
    if (copied) copied.style.display = 'none';
    const bd = document.getElementById('om-share-bd');
    if (bd) bd.style.display = 'flex';
  }

  closeShare() {
    const bd = document.getElementById('om-share-bd');
    if (bd) bd.style.display = 'none';
  }

  async copyShare() {
    const inp = document.getElementById('om-share-url');
    if (!inp) return;
    try { await navigator.clipboard.writeText(inp.value); }
    catch (_) { inp.select(); try { document.execCommand('copy'); } catch (__) {} }
    const copied = document.getElementById('om-share-copied');
    if (copied) copied.style.display = '';
  }

  _eventRow(ev, i, sel) {
    const ncTxt = _ncText(ev.nearest_city);
    const ncStr = ncTxt
      ? `<span class="om-evf-city"><i class="bi bi-geo-alt-fill"></i>${ncTxt}</span>`
      : '';
    return `
    <div class="om-evf-row ${i === sel ? 'sel' : ''}" onclick="OM.selectEvent(${i});OM.closeCatalogModal()">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${typeof _TZ !== 'undefined' ? _TZ.fmt(ev.datetime) : ev.datetime}</b>
        <span class="om-evf-mag">${ev.mag != null ? 'M' + ev.mag.toFixed(2) : 'M —'}</span>
      </div>
      <div style="color:var(--text-muted);font-size:.7rem">
        ${this._netBadge(ev)}${ev.lat.toFixed(3)}, ${ev.lon.toFixed(3)} · ${ev.depth_km.toFixed(1)} km · ${ev.nsta} sta
      </div>
      ${ncStr}
    </div>`;
  }

  _renderEventFeed(events) {
    // Badge jumlah event pada tab "List"
    const evBadge = document.getElementById('om-event-badge');
    if (evBadge) evBadge.textContent = events.length;

    // Update the catalog modal content (if opened / already in the DOM)
    const modal = document.getElementById('om-catalog-modal-list');
    if (modal) {
      const sel = this._selEventIdx || 0;
      if (!events.length) {
        modal.innerHTML = '<div class="om-placeholder" style="padding:1rem">No events detected yet.</div>';
      } else {
        modal.innerHTML = events.map((ev, i) => this._eventRow(ev, i, sel)).join('');
      }
    }

    // Update panel inline (om-event-feed-list) — event terbaru + daftar ringkas di bawahnya
    const el = document.getElementById('om-event-feed-list');
    if (!el) return;
    if (!events.length) {
      el.innerHTML = `<div class="om-placeholder">Waiting for events from ${this._beInfo(this._assocBackend).label}…</div>`;
      return;
    }
    const ev0 = events[0];
    const nc0Txt = _ncText(ev0.nearest_city);
    const ncStr0 = nc0Txt
      ? `<span class="om-evf-city"><i class="bi bi-geo-alt-fill"></i>${nc0Txt}</span>`
      : '';
    const sel = this._selEventIdx || 0;
    const listHtml = events.slice(1).map((ev, i) => {
      const loc = _ncText(ev.nearest_city) || `${ev.lat.toFixed(2)}, ${ev.lon.toFixed(2)}`;
      const isSel = (i + 1) === sel;
      return `<div class="om-catalog-item ${isSel ? 'sel' : ''}" onclick="OM.selectEvent(${i + 1})">
        <span class="om-catalog-item-mag">${ev.mag != null ? 'M' + ev.mag.toFixed(1) : 'M—'}</span>
        <div class="om-catalog-item-info">
          <div class="om-catalog-item-time">${typeof _TZ !== 'undefined' ? _TZ.fmt(ev.datetime) : ev.datetime} ${this._netBadge(ev)}</div>
          <div class="om-catalog-item-loc"><i class="bi bi-geo-alt-fill"></i> ${loc}</div>
        </div>
      </div>`;
    }).join('');
    el.innerHTML = `
      <div class="om-evf-row ${sel === 0 ? 'sel' : ''}" onclick="OM.selectEvent(0)">
        <div style="font-size:.65rem;color:var(--text-muted);margin-bottom:.1rem">Event Terbaru</div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <b>${typeof _TZ !== 'undefined' ? _TZ.fmt(ev0.datetime) : ev0.datetime} ${_sinceQuakeBadge(ev0.datetime, this._nowSec())}</b>
          <span class="om-evf-mag">${ev0.mag != null ? 'M' + ev0.mag.toFixed(2) : 'M —'}</span>
        </div>
        <div style="color:var(--text-muted);font-size:.7rem">
          ${this._netBadge(ev0)}${ev0.lat.toFixed(3)}, ${ev0.lon.toFixed(3)} · ${ev0.depth_km.toFixed(1)} km · ${ev0.nsta} sta
        </div>
        ${ncStr0}
      </div>
      ${events.length > 1 ? `
      <div class="om-catalog-hdr"><i class="bi bi-list-ul"></i> Event Catalog (${events.length - 1} previous)</div>
      <div class="om-catalog-list">${listHtml}</div>` : ''}`;
  }

  // Select an event from the list → show in the detail card + zoom the map
  selectEvent(idx) {
    this._selEventIdx = idx;
    const ev = (this._events || [])[idx];
    this._renderEventDetail(ev || null, this._lastSdsPath || '');
    this._renderEventFeed(this._events || []);
    if (ev && this._map) this._map.setView([ev.lat, ev.lon], Math.max(this._map.getZoom(), 7));
    // Update markers + station lines to the selected event (not always event[0])
    if (this._events?.length) this._plotEventsOnMap(this._events);
    this.showFeedTab('event');
  }

  // ── Modal Katalog Gempa Terdeteksi ──────────────────────────────────────────
  openCatalogModal() {
    const bd = document.getElementById('om-catalog-modal-bd');
    const md = document.getElementById('om-catalog-modal');
    if (!bd || !md) return;
    bd.style.display = '';
    md.style.display = 'flex';
    // Render daftar ke modal
    const sel = this._selEventIdx || 0;
    const events = this._events || [];
    const list = document.getElementById('om-catalog-modal-list');
    const cnt = document.getElementById('om-catalog-modal-count');
    if (cnt) cnt.textContent = `(${events.length} event)`;
    if (list) {
      list.innerHTML = events.length
        ? events.map((ev, i) => this._eventRow(ev, i, sel)).join('')
        : '<div class="om-placeholder" style="padding:1rem">No events detected yet.</div>';
    }
  }

  closeCatalogModal() {
    const bd = document.getElementById('om-catalog-modal-bd');
    const md = document.getElementById('om-catalog-modal');
    if (bd) bd.style.display = 'none';
    if (md) md.style.display = 'none';
  }

  /* ── Detail card event (IDEvent, lat/lon, Mag, RMS, depth, gap + bola focal) ── */
  _renderEventDetail(ev, sdsPath) {
    this._lastSdsPath = sdsPath;
    const el = document.getElementById('om-event-detail');
    if (!el) return;
    if (!ev) {
      el.innerHTML = '<div class="om-placeholder" style="margin:0">No events detected yet</div>';
      return;
    }
    const f = (v, d, suf = '') => (v != null && !isNaN(v)) ? v.toFixed(d) + suf : '—';
    // Solution already known (event field or the per-event cache that survives
    // the poll's this._events replacement) → embed the PNG straight into the
    // card. Rendering a canvas first and swapping it for the image afterwards
    // made the beachball visibly blink on every status poll.
    const knownFocal = (ev.focal && typeof ev.focal === 'object' && ev.focal)
                    || this._focalCache?.[ev.event_id]?.focal || null;
    const beachHtml = knownFocal
      ? `<img class="om-evd-beach" src="${this._focalPngUrl(ev.event_id, knownFocal)}"
              alt="beachball" width="86" height="86" onerror="this.style.display='none'">`
      : `<canvas id="om-evd-beach" class="om-evd-beach" width="86" height="86"></canvas>`;
    el.innerHTML = `
      <div class="om-evd-grid">
        <div id="om-evd-beach-wrap" class="om-evd-beach-wrap">
          ${beachHtml}
        </div>
        <div class="om-evd-fields">
          <div class="om-evd-mag">${ev.mag != null ? 'M ' + ev.mag.toFixed(2) : 'M —'}</div>
          <div class="om-evd-row"><span>ID Event</span><b>${ev.event_id || '—'}</b></div>
          <div class="om-evd-row"><span>Origin Time</span><b>${typeof _TZ !== 'undefined' ? _TZ.fmt(ev.datetime) : ev.datetime} <span style="font-size:.62rem;color:#64748b">${typeof _TZ !== 'undefined' ? (_TZ.h===7?'WIB':'UTC') : ''}</span> ${_sinceQuakeBadge(ev.datetime, this._nowSec())}</b></div>
          <div class="om-evd-row"><span>Lat / Lon</span><b>${f(ev.lat,3)}, ${f(ev.lon,3)}</b></div>
          <div class="om-evd-row"><span>Depth</span><b>${f(ev.depth_km,1,' km')}</b></div>
          ${(() => { const nc = ev.nearest_city; return nc && nc.dist_km != null
            ? `<div class="om-evd-row om-evd-city"><span>Location</span><b><i class="bi bi-geo-alt-fill" style="margin-right:.2rem"></i>${Math.round(nc.dist_km)} km ${_dirFull(nc.direction)} ${nc.city}</b></div>`
            : ''; })()}
          <div class="om-evd-row"><span>RMS</span><b>${f(ev.rms,2,' s')}</b></div>
          <div class="om-evd-row"><span>Gap / Nsta</span><b>${f(ev.gap,0,'°')} / ${ev.nsta}</b></div>
          ${this._eventNetworks(ev).length
            ? `<div class="om-evd-row"><span>Network</span><b>${this._netBadge(ev)}</b></div>`
            : ''}
          <div class="om-evd-row"><span>Located by</span><b>${
              ev.loc_method === 'NLLoc'
                ? `<span style="color:#22c55e">NonLinLoc</span> <span style="font-size:.62rem;color:#64748b">(association ${ev.assoc || this._beInfo(this._assocBackend).label})</span>`
              : ev.loc_method
                ? `${ev.loc_method} <span style="font-size:.62rem;color:#64748b">(NLLoc refine produced no solution)</span>`
                : `${this._beInfo(this._assocBackend).label} <span style="font-size:.62rem;color:#64748b">(old event, before the refine feature)</span>`
            }</b></div>
        </div>
      </div>
      <div class="om-evd-focal-note" id="om-evd-focal-note">
        Focal (FocoNet): <span style="color:#94a3b8">loading…</span>
      </div>
      <button class="btn-sw btn-blue-sw btn-sm-sw" style="width:100%;margin-top:.4rem;justify-content:center"
              onclick="OM.showEventWaveform('${ev.event_id}')">
        <i class="bi bi-activity"></i> View Waveform &amp; Pick Results
      </button>`;
    if (!knownFocal) this._drawBeachball(document.getElementById('om-evd-beach'), ev.focal);
    this._fetchEventFocal(ev);
  }

  // Fetch/compute the focal mechanism (FocoNet) for this event on demand.
  // When available → show the beachball PNG + SDR; on failure → the reason.
  // The detail card is re-rendered by every status poll AND this._events is
  // replaced with fresh objects each cycle, so per-event results are cached in
  // this._focalCache (keyed by event_id) — successes forever, failures with a
  // cooldown — otherwise a failing event would re-attempt the whole FocoNet
  // computation (SDS read + inference) every few seconds.
  async _fetchEventFocal(ev) {
    const note = document.getElementById('om-evd-focal-note');
    const wrap = document.getElementById('om-evd-beach-wrap');
    const eid  = ev.event_id;
    this._focalCache = this._focalCache || {};
    const FAIL_RETRY_MS = 60000;   // failed events retry at most once a minute
    const cached = this._focalCache[eid];
    if (!ev.focal && cached?.focal) ev.focal = cached.focal;
    // Already present on the event (in-memory cache) → render directly
    if (ev.focal && typeof ev.focal === 'object') {
      this._showFocal(ev, ev.focal); return;
    }
    if (cached?.error && (Date.now() - cached.t) < FAIL_RETRY_MS) {
      if (note) note.innerHTML = `Focal (FocoNet): <span style="color:#f59e0b">${this.core.esc(cached.error)}</span>`;
      return;
    }
    if (note) note.innerHTML = 'Focal (FocoNet): <span style="color:#94a3b8">computing polarities…</span>';
    try {
      const d = await fetch(`/api/online/realtime/event_focal?id=${encodeURIComponent(eid)}`)
                      .then(r => r.json());
      // The event can change while the fetch runs → ensure it is still the same event
      if ((this._events || [])[this._selEventIdx]?.event_id !== eid) return;
      if (d.focal) {
        ev.focal = d.focal;
        this._focalCache[eid] = { focal: d.focal, t: Date.now() };
        this._showFocal(ev, d.focal);
      } else {
        this._focalCache[eid] = { error: d.error || 'unavailable', t: Date.now() };
        if (note) note.innerHTML = `Focal (FocoNet): <span style="color:#f59e0b">${this.core.esc(d.error || 'unavailable')}</span>`;
        if (wrap) this._drawBeachball(document.getElementById('om-evd-beach'), null);
      }
    } catch (e) {
      this._focalCache[eid] = { error: e.message, t: Date.now() };
      if (note) note.innerHTML = `Focal (FocoNet): <span style="color:#f87171">failed: ${this.core.esc(e.message)}</span>`;
    }
  }

  // Stable PNG URL for one solution: versioned by SDR (re-render only when the
  // solution actually changes) — a Date.now() buster here made the browser
  // re-download the beachball on every status poll, which looked like the
  // image endlessly refreshing.
  _focalPngUrl(eid, focal) {
    const v = `${Math.round(focal.strike || 0)}_${Math.round(focal.dip || 0)}_${Math.round(focal.rake || 0)}_${focal.n_pol || 0}`;
    return `/api/online/realtime/event_focal_png?id=${encodeURIComponent(eid)}&v=${v}`;
  }

  _showFocal(ev, focal) {
    const note = document.getElementById('om-evd-focal-note');
    const wrap = document.getElementById('om-evd-beach-wrap');
    if (wrap) {
      const url = this._focalPngUrl(ev.event_id, focal);
      // Same image already in place (poll re-render) → leave the DOM alone.
      const img = wrap.querySelector('img.om-evd-beach');
      if (!img || img.getAttribute('src') !== url) {
        wrap.innerHTML =
          `<img class="om-evd-beach" src="${url}"
                alt="beachball" width="86" height="86"
                onerror="this.style.display='none'">`;
      }
    }
    if (note) {
      note.innerHTML =
        `Focal (${focal.method || 'FocoNet'}): ` +
        `<span style="color:#4ade80">strike ${Math.round(focal.strike)}° · ` +
        `dip ${Math.round(focal.dip)}° · rake ${Math.round(focal.rake)}°</span>` +
        ` <span style="color:#64748b">(${focal.n_pol} pol)</span>`;
    }
  }

  /* ── Modal: station waveform + pick results around the event ─────────────── */
  async showEventWaveform(eid) {
    if (!eid) return;
    const bd = document.getElementById('om-evw-backdrop');
    const md = document.getElementById('om-evw-modal');
    const ld = document.getElementById('om-evw-loading');
    bd?.classList.remove('hidden'); md?.classList.remove('hidden');
    if (ld) ld.style.display = 'flex';
    document.getElementById('om-evw-title').textContent = 'Event ' + eid;
    document.getElementById('om-evw-sub').textContent = '';
    this._evwCorrStack = null;  // new event → drop any cached CC/stack from the previous one
    try {
      const data = await fetch(
        `/api/online/realtime/event_waveform?id=${encodeURIComponent(eid)}`
      ).then(r => r.json());
      if (data.error || !data.streams || !data.streams.length) {
        const msg = data.error || (data.streams && !data.streams.length ? 'Waveform unavailable — the ring-buffer data has moved past the window. Try the SDS archive.' : 'no data');
        document.getElementById('om-evw-sub').textContent = msg;
        if (ld) {
          ld.innerHTML = `<span style="color:#f87171;font-size:.78rem;text-align:center;padding:.6rem"><i class="bi bi-exclamation-triangle" style="font-size:1.2rem;display:block;margin-bottom:.3rem"></i>${msg}</span>`;
          ld.style.display = 'flex';
        }
        return;
      }
      this._evwData = data;
      this._renderEvwTitle(data);
      const _nc = data.event.nearest_city;
      const _ncTxt = _nc && _nc.dist_km != null
        ? `  ·  ${Math.round(_nc.dist_km)} km ${_nc.direction} of ${_nc.city}` : '';
      const ev = data.event;
      document.getElementById('om-evw-sub').textContent =
        `${ev.lat.toFixed(3)}, ${ev.lon.toFixed(3)} · ${ev.depth_km.toFixed(1)} km · ${data.streams.length} sta${_ncTxt}`;
      this._drawEventWaveform(data);
      if (ld) ld.style.display = 'none';
      // Auto-load CC/stack if toggle already on
      const ccTog = document.getElementById('om-evw-cc-tog');
      if (ccTog && ccTog.checked) this.loadCorrStack();
    } catch (e) {
      document.getElementById('om-evw-sub').textContent = 'failed to load: ' + e.message;
      if (ld) ld.style.display = 'none';
    }
  }

  _renderEvwTitle(data) {
    if (!data) return;
    const ev = data.event;
    const tzBtn = document.getElementById('om-evw-tz-btn');
    if (tzBtn) {
      tzBtn.textContent = this._evwTzUtc ? 'UTC' : 'WIB';
      tzBtn.title = this._evwTzUtc ? 'Click to show WIB (UTC+7)' : 'Click to show UTC';
    }
    let timeStr;
    if (this._evwTzUtc) {
      // Selalu UTC
      const d = new Date(ev.datetime.replace(' ', 'T') + (ev.datetime.includes('Z') ? '' : 'Z'));
      timeStr = d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
    } else {
      // WIB = UTC+7 (paksa offset, abaikan setting global _TZ)
      const _d2 = new Date(ev.datetime.replace(' ', 'T') + (ev.datetime.includes('Z') ? '' : 'Z'));
      _d2.setTime(_d2.getTime() + 7 * 3600000);
      const _p = n => String(n).padStart(2, '0');
      timeStr = `${_d2.getUTCFullYear()}-${_p(_d2.getUTCMonth()+1)}-${_p(_d2.getUTCDate())} `
              + `${_p(_d2.getUTCHours())}:${_p(_d2.getUTCMinutes())}:${_p(_d2.getUTCSeconds())} WIB`;
    }
    document.getElementById('om-evw-title').textContent =
      `${timeStr}  ·  ${ev.mag != null ? 'M ' + ev.mag.toFixed(2) : 'M —'}`;
  }

  toggleEvwTz() {
    this._evwTzUtc = !this._evwTzUtc;
    this._renderEvwTitle(this._evwData);
  }

  closeEventWaveform() {
    document.getElementById('om-evw-backdrop')?.classList.add('hidden');
    document.getElementById('om-evw-modal')?.classList.add('hidden');
    const tog = document.getElementById('om-evw-cc-tog');
    if (tog) tog.checked = false;
    const row = document.getElementById('om-evw-cc-row');
    if (row) row.style.display = 'none';
    this._evwCorrStack = null;
  }

  // --- Controller (cross-correlation matrix + stack, lazy-loaded) ---
  // Shares its rendering (drawStackCanvas/drawCCMatrix) with the offline Result
  // Viewer's CC & Stack panel — see result-viewer-waveform.js.
  toggleCorrStack(on) {
    const rowEl = document.getElementById('om-evw-cc-row');
    if (!on) { if (rowEl) rowEl.style.display = 'none'; return; }
    if (rowEl) rowEl.style.display = 'flex';
    if (!this._evwCorrStack && this._evwData) this.loadCorrStack();
    else { this._renderCorrStack(); }
  }

  async loadCorrStack() {
    if (!this._evwData) return;
    const eid = this._evwData.event.event_id;
    const infoEl = document.getElementById('om-evw-cc-info');
    if (infoEl) infoEl.textContent = '⟳ Cross-correlating stations & building stack…';
    try {
      const url = `/api/online/realtime/event_waveform/corr_stack?id=${encodeURIComponent(eid)}`;
      const res = await fetch(url); const d = await res.json();
      if (!res.ok || d.error) { if (infoEl) infoEl.textContent = '⚠ ' + (d.error || 'error'); return; }
      this._evwCorrStack = d;
      const nSta = (d.cc && d.cc.stations || d.stack && d.stack.stations || []).length;
      if (infoEl) {
        infoEl.textContent = d.cc
          ? `${nSta} stations  ·  mean CC = ${d.cc.overall_mean_cc.toFixed(3)}  ·  phase ${d.phase}`
          : `${nSta} stations  ·  phase ${d.phase} (not enough stations for a CC matrix)`;
      }
      this._renderCorrStack();
    } catch (e) { if (infoEl) infoEl.textContent = '⚠ ' + e.message; }
  }

  _renderCorrStack() {
    // Stack overlay only — the live modal skips the per-pair CC matrix (mean CC
    // is already in the info line; the full matrix lives in the offline Result
    // Viewer's CC & Stack panel).
    const cs = this._evwCorrStack;
    drawStackCanvas(document.getElementById('om-evw-stack-canvas'), cs && cs.stack);
  }

  /* ── Status & lokasi penyimpanan SDS (modal + chip header engine) ──────────── */
  async openSdsStatus() {
    const bd = document.getElementById('om-sds-backdrop');
    const md = document.getElementById('om-sds-modal');
    const body = document.getElementById('om-sds-body');
    bd?.classList.remove('hidden'); md?.classList.remove('hidden');
    if (body) body.innerHTML = '<div class="om-placeholder">Loading SDS status…</div>';
    try {
      const d = await fetch('/api/online/sds_status').then(r => r.json());
      this._renderSdsStatus(d);
      this._applySdsChip(d);
    } catch (e) {
      if (body) body.innerHTML = `<div class="om-placeholder" style="color:#f87171">Failed to load: ${e.message}</div>`;
    }
  }

  closeSdsStatus() {
    document.getElementById('om-sds-backdrop')?.classList.add('hidden');
    document.getElementById('om-sds-modal')?.classList.add('hidden');
  }

  // Update the compact chip in the Engine header — label = the active archive target
  async refreshSdsChip() {
    try {
      const d = await fetch('/api/online/sds_status').then(r => r.json());
      this._applySdsChip(d);
    } catch (_) { /* silent — the chip is optional */ }
  }

  _applySdsChip(d) {
    const map = {
      seiscomp:   { label: 'SDS: SeisComP',  src: 'SeisComP', cls: 'om-sds-ok' },
      online_sds: { label: 'SDS: SeisWork',  src: 'SeisWork', cls: 'om-sds-self' },
      ring_only:  { label: 'SDS: RAM saja',  src: 'RAM saja', cls: 'om-sds-warn' },
    };
    const m = map[d.archiving_target] || map.ring_only;
    // Store the SDS source → shown in the engine card's "SDS" row ("connected | SeisWork").
    this._sdsSource = m.src;
    // The header chip moved to the engine card's SDS row; if the element is missing,
    // just stop here (the source is already stored in this._sdsSource).
    const chip = document.getElementById('om-sds-chip');
    const txt  = document.getElementById('om-sds-chip-txt');
    if (!chip || !txt) return;
    txt.textContent = m.label;
    chip.className = 'om-sds-chip ' + m.cls;
  }

  _renderSdsStatus(d) {
    const body = document.getElementById('om-sds-body');
    if (!body) return;
    const sc = d.seiscomp || {}, ow = d.online_sds || {};
    const yn = (b) => b
      ? '<span class="om-sds-badge om-sds-b-ok">YES</span>'
      : '<span class="om-sds-badge om-sds-b-no">NO</span>';
    const freshBadge = (s) => !s.exists ? ''
      : (s.fresh
          ? '<span class="om-sds-badge om-sds-b-ok">FRESH (&lt;2h)</span>'
          : '<span class="om-sds-badge om-sds-b-warn">STALE</span>');
    const card = (title, icon, s, isTarget) => `
      <div class="om-sds-card ${isTarget ? 'om-sds-card-active' : ''}">
        <div class="om-sds-card-hdr">
          <i class="bi ${icon}"></i> ${title}
          ${isTarget ? '<span class="om-sds-badge om-sds-b-active">IN USE</span>' : ''}
        </div>
        <div class="om-sds-path">${s.path || '—'}</div>
        <div class="om-sds-grid">
          <span>Folder</span><b>${yn(s.exists)} ${freshBadge(s)}</b>
          <span>Jumlah file</span><b>${s.n_files == null ? '<span style="color:#64748b">arsip besar — tak dihitung</span>' : s.n_files}</b>
          <span>Ukuran</span><b>${s.size_mb == null ? '—' : s.size_mb + ' MB'}</b>
          <span>Last day</span><b>${s.last_day || '—'}</b>
          <span>File termutakhir</span><b>${s.last_mtime || '—'}</b>
        </div>
      </div>`;
    const target = d.archiving_target;
    const tIcon = target === 'seiscomp' ? 'bi-hdd-network'
                : target === 'online_sds' ? 'bi-hdd' : 'bi-memory';
    const tLabel = target === 'seiscomp' ? 'SDS SeisComP (scarchive eksternal)'
                 : target === 'online_sds' ? 'SDS SeisWork (slarchive)'
                 : 'Ring buffer (RAM) — not yet archived to disk';
    body.innerHTML = `
      <div class="om-sds-active-banner om-sds-tgt-${target}">
        <div><i class="bi ${tIcon}"></i> <b>Active archive: ${tLabel}</b></div>
        ${d.archiving_path ? `<div class="om-sds-path" style="margin-top:.2rem">${d.archiving_path}</div>` : ''}
        <div class="om-sds-reason">${d.decision_reason || ''}</div>
      </div>

      ${card('SDS SeisComP (arsip utama)', 'bi-hdd-network', sc, target === 'seiscomp')}
      ${card('SDS SeisWork (live / fallback)', 'bi-hdd', ow, target === 'online_sds')}

      <div class="om-sds-sec">
        <div class="om-sds-sec-title"><i class="bi bi-diagram-3"></i> How SeisWork decides (automatic)</div>
        <ol class="om-sds-steps">
          <li>Detects the SeisComP root (env <code>SEISCOMP_ROOT</code> → <code>config.yaml</code> → default candidates), SDS = <code>&lt;root&gt;/var/lib/archive</code>.</li>
          <li>If that SDS is <b>fresh</b> (data &lt;2 hours old) → the external scarchive is considered active: SeisWork <b>only reads</b>, <b>slarchive is SKIPPED</b>.</li>
          <li>If <b>not fresh / absent</b> → SeisWork runs its <b>own slarchive</b> into <code>work/online_sds</code> (retention ${d.retain_days} days).</li>
          <li>If detection is not running yet → data stays <b>RAM-only</b> (ring buffer) for the session.</li>
        </ol>
        <div class="om-sds-sec-title" style="margin-top:.5rem"><i class="bi bi-sort-down"></i> Event waveform read order</div>
        <ol class="om-sds-steps">
          ${(d.read_priority || []).map(x => `<li>${x}</li>`).join('')}
        </ol>
      </div>

      <div class="om-sds-foot">
        slarchive: ${d.slarchive_running ? '<b style="color:#4ade80">running</b>' : '<b style="color:#f59e0b">stopped</b>'}
        · live session: ${d.session_connected ? '<b style="color:#4ade80">connected</b>' : 'disconnected'}
        · fresh window: ${d.fresh_window_hours}h · checked: ${d.checked_at || '—'}
      </div>`;
  }

  // Colormap viridis (256 entries, pre-computed RGBA)
  get _specCM() {
    if (this.__specCM) return this.__specCM;
    const lut = new Uint8ClampedArray(256 * 4);
    const stops = [
      [0,    68,   1,  84],
      [0.13, 71,  44, 122],
      [0.25, 59,  82, 139],
      [0.38, 44, 113, 142],
      [0.50, 33, 145, 140],
      [0.63, 39, 174, 128],
      [0.75, 94, 201,  98],
      [0.88,175, 220,  56],
      [1.0, 253, 231,  37],
    ];
    for (let i = 0; i < 256; i++) {
      const v = i / 255;
      let si = 0;
      for (let k = stops.length - 2; k >= 0; k--) { if (v >= stops[k][0]) { si = k; break; } }
      const lo = stops[si], hi = stops[Math.min(si + 1, stops.length - 1)];
      const t = lo[0] === hi[0] ? 0 : (v - lo[0]) / (hi[0] - lo[0]);
      lut[i*4]   = Math.round(lo[1] + t * (hi[1] - lo[1]));
      lut[i*4+1] = Math.round(lo[2] + t * (hi[2] - lo[2]));
      lut[i*4+2] = Math.round(lo[3] + t * (hi[3] - lo[3]));
      lut[i*4+3] = 255;
    }
    return (this.__specCM = lut);
  }

  // Render the event waveform: each station = a waveform row + spectrogram.
  // UTC time axis on top; stations sorted by first arrival.
  _drawEventWaveform(data) {
    const canvas = document.getElementById('om-evw-canvas');
    const body   = document.getElementById('om-evw-body');
    if (!canvas || !body) return;
    const streams = data.streams || [];
    if (!streams.length) return;

    const W   = (body.clientWidth || 700) - 16;
    const LW  = 100;   // station label column width
    const RW  = 8;     // right margin
    const pW  = W - LW - RW;
    const AH  = 34;    // axis height (two rows: UTC on top + relative offset below)
    const WH  = 54;    // waveform row height per station
    const SH  = 56;    // spectrogram row height per station
    const RH  = WH + SH;
    const H   = AH + streams.length * RH;
    const dpr = window.devicePixelRatio || 1;
    canvas.width  = W * dpr; canvas.height = H * dpr;
    canvas.style.width = '100%'; canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const t0 = data.t0, tA = t0 - data.pre, tB = t0 + data.post;
    const span = tB - tA;
    const toX = t => LW + (t - tA) / span * pW;

    // Helpers
    const _toUTC = ep => new Date(ep * 1000).toISOString().slice(11, 16); // "HH:MM"
    const _toRel = dt => {                    // seconds from OT → "+M:SS" / "−M:SS" / "OT"
      if (Math.abs(dt) < 0.5) return 'OT';
      const sign = dt < 0 ? '−' : '+';
      const abs  = Math.abs(dt);
      const m    = Math.floor(abs / 60);
      const s    = Math.round(abs % 60);
      return s === 0 ? `${sign}${m}m` : `${sign}${m}:${String(s).padStart(2,'0')}`;
    };

    // ── Background
    ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);

    // ── Axis (two rows within AH=34px)
    // Top row (0..16): absolute UTC
    // Bottom row (16..34): offset relative to OT + window bounds
    ctx.fillStyle = '#1e293b'; ctx.fillRect(0, 0, W, AH);

    const _tickStep = span <= 60 ? 5 : span <= 120 ? 10 : span <= 300 ? 30
                    : span <= 600 ? 60 : span <= 1200 ? 120 : 300;
    const _pxPerTick = pW / (span / _tickStep);
    const _skipAlt   = _pxPerTick < 42;   // skip alternate labels when too dense
    let _tickIdx = 0;
    const _ft = Math.ceil(tA / _tickStep) * _tickStep;

    for (let _t = _ft; _t <= tB; _t += _tickStep, _tickIdx++) {
      const x = toX(_t);
      if (x < LW + 1 || x > W - RW - 1) continue;
      // Tick mark di tengah axis (batas baris atas/bawah)
      ctx.strokeStyle = '#334155'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, 14); ctx.lineTo(x, AH); ctx.stroke();

      if (!_skipAlt || _tickIdx % 2 === 0) {
        ctx.textAlign = 'center';
        // Baris atas: UTC "HH:MM"
        ctx.fillStyle = '#94a3b8'; ctx.font = '8px monospace';
        ctx.fillText(_toUTC(_t), x, 11);
        // Baris bawah: offset relatif "−3m", "+1m", dll
        ctx.fillStyle = '#64748b'; ctx.font = '7.5px monospace';
        ctx.fillText(_toRel(_t - t0), x, AH - 3);
      }
    }

    // Left column labels: "UTC" on top, "from OT" below
    ctx.textAlign = 'left';
    ctx.fillStyle = '#475569'; ctx.font = 'bold 7px monospace';
    ctx.fillText('UTC',    3, 11);
    ctx.fillStyle = '#334155'; ctx.font = '7px monospace';
    ctx.fillText('from OT', 1, AH - 3);

    // Left "−Xm" and right "+Xm" boundary labels right at the plot edge
    const _preMin  = Math.round(data.pre  / 60);
    const _postMin = Math.round(data.post / 60);
    ctx.textAlign = 'left';
    ctx.fillStyle = '#ef4444'; ctx.font = 'bold 7.5px monospace';
    ctx.fillText(`−${_preMin}m`, LW + 2, AH - 3);
    ctx.textAlign = 'right';
    ctx.fillText(`+${_postMin}m`, W - RW - 2, AH - 3);

    // Garis separator baris atas/bawah axis
    ctx.strokeStyle = '#2d3748'; ctx.lineWidth = 0.5;
    ctx.beginPath(); ctx.moveTo(LW, 15); ctx.lineTo(W - RW, 15); ctx.stroke();

    // Garis OT di axis (hijau tebal)
    const _ox = toX(t0);
    ctx.strokeStyle = '#16a34a'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(_ox, 1); ctx.lineTo(_ox, AH); ctx.stroke();
    // "OT" label on the bottom axis row right at the origin line
    ctx.fillStyle = '#16a34a'; ctx.font = 'bold 7.5px monospace'; ctx.textAlign = 'center';
    ctx.fillText('OT', _ox, AH - 3);

    // ── Per station
    streams.forEach((s, i) => {
      const yTop  = AH + i * RH;     // top of the waveform row
      const ySep  = yTop + WH;       // separator waveform↔spektrogram
      const yBot  = yTop + RH;       // bawah spektrogram

      // Background alternating
      if (i % 2) { ctx.fillStyle = '#f0f4f8'; ctx.fillRect(0, yTop, W, RH); }

      // ── Station label (left)
      ctx.fillStyle = '#1e293b'; ctx.font = 'bold 10px monospace'; ctx.textAlign = 'left';
      ctx.fillText(`${s.net}.${s.sta}`, 3, yTop + 16);
      ctx.fillStyle = '#6b7280'; ctx.font = '8px monospace';
      ctx.fillText(`${s.cha}`, 3, yTop + 29);
      ctx.fillText(`${s.dist_km}km`, 3, yTop + 40);

      // ── Waveform trace
      const yMid = yTop + WH / 2;
      const pts = s.points || [];
      if (pts.length > 1) {
        let vmax = 1e-12;
        for (const p of pts) vmax = Math.max(vmax, Math.abs(p.v));
        const sc = (WH * 0.44) / vmax;
        ctx.strokeStyle = '#1e3a5f'; ctx.lineWidth = 0.8; ctx.beginPath();
        let started = false;
        for (const p of pts) {
          const x = toX(p.t);
          if (x < LW || x > W - RW) continue;
          const y = yMid - p.v * sc;
          started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true);
        }
        ctx.stroke();
        // Garis nol tipis
        ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 0.4;
        ctx.beginPath(); ctx.moveTo(LW, yMid); ctx.lineTo(W - RW, yMid); ctx.stroke();
      }

      // ── Spektrogram
      const sp = s.spec;
      if (sp && sp.db && sp.db.length && sp.t && sp.t.length) {
        this._drawSpecPanel(ctx, sp, LW, ySep, pW, SH, tA, tB);
      } else {
        // Placeholder when there is no spectrogram data
        ctx.fillStyle = '#1e293b'; ctx.fillRect(LW, ySep, pW, SH);
        ctx.fillStyle = '#334155'; ctx.font = '8px sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('spectrogram unavailable', LW + pW / 2, ySep + SH / 2 + 3);
      }
      // Label "Hz" di kiri spektrogram
      ctx.fillStyle = '#94a3b8'; ctx.font = '7px monospace'; ctx.textAlign = 'left';
      if (sp && sp.f && sp.f.length) {
        const fMax = sp.f[sp.f.length - 1];
        ctx.fillText(`${Math.round(fMax)}Hz`, 3, ySep + 10);
        ctx.fillText('0Hz', 3, yBot - 4);
      }

      // ── Garis origin + picks (span full row WH+SH)
      const ox = toX(t0);
      ctx.strokeStyle = '#16a34a'; ctx.lineWidth = 1.4; ctx.setLineDash([5, 3]);
      ctx.beginPath(); ctx.moveTo(ox, yTop); ctx.lineTo(ox, yBot); ctx.stroke();
      ctx.setLineDash([]);

      (s.picks || []).forEach(pk => {
        const isP  = (pk.phase || 'P').toUpperCase().startsWith('P');
        const isAI = pk.source === 'phasenet' || pk.source === 'stored' || pk.source === 'assoc';
        const col  = isAI
          ? (isP ? '#fb923c' : '#c084fc')
          : (isP ? '#ef4444' : '#3b82f6');
        const x = toX(pk.t);
        if (x < LW || x > W - RW) return;
        ctx.strokeStyle = col; ctx.lineWidth = isAI ? 1.4 : 2;
        if (isAI) ctx.setLineDash([5, 3]);
        ctx.beginPath(); ctx.moveTo(x, yTop); ctx.lineTo(x, yBot); ctx.stroke();
        ctx.setLineDash([]);
        // Label pick (waveform area saja)
        ctx.fillStyle = col; ctx.font = 'bold 9px monospace'; ctx.textAlign = 'left';
        ctx.fillText(isP ? 'P' : 'S', x + 2, yTop + 12);
      });

      // ── Garis pemisah bawah baris
      ctx.strokeStyle = '#334155'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(0, yBot); ctx.lineTo(W, yBot); ctx.stroke();
      // Garis tipis pemisah waveform↔spektrogram
      ctx.strokeStyle = '#475569'; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(LW, ySep); ctx.lineTo(W - RW, ySep); ctx.stroke();
    });
  }

  // Draw the spectrogram panel (from the spec API) onto ctx in the (x0,y0,pW,sH) area
  // Interpolation: one small offscreen canvas (nT×nF) → drawImage scaling
  _drawSpecPanel(ctx, spec, x0, y0, pW, sH, tA, tB) {
    const { db, t: specT, f, vmin, vmax } = spec;
    const nF = db.length, nT = db[0]?.length || 0;
    if (!nF || !nT) return;

    const oc  = document.createElement('canvas');
    oc.width  = nT; oc.height = nF;
    const oct = oc.getContext('2d');
    const img = oct.createImageData(nT, nF);
    const pix = img.data;
    const cm  = this._specCM;
    const dv  = vmax - vmin + 1e-10;

    for (let fi = 0; fi < nF; fi++) {
      const row = nF - 1 - fi;   // flip: frekuensi rendah di bawah
      for (let ti = 0; ti < nT; ti++) {
        const val = Math.max(0, Math.min(1, (db[fi][ti] - vmin) / dv));
        const ci  = Math.round(val * 255);
        const idx = (row * nT + ti) * 4;
        pix[idx]   = cm[ci * 4];
        pix[idx+1] = cm[ci * 4 + 1];
        pix[idx+2] = cm[ci * 4 + 2];
        pix[idx+3] = 255;
      }
    }
    oct.putImageData(img, 0, 0);

    // Compute the canvas x-range corresponding to the time window
    const tSpan = tB - tA;
    const t0sp  = specT[0], t1sp = specT[nT - 1];
    const xS    = x0 + Math.max(0,   (t0sp - tA) / tSpan * pW);
    const xE    = x0 + Math.min(pW,  (t1sp - tA) / tSpan * pW);
    const wS    = Math.max(1, xE - xS);

    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'medium';
    ctx.drawImage(oc, xS, y0, wS, sH);
  }

  // Draw the focal ball. Grey placeholder (no solution yet) or an SDR beachball
  // (when focal={strike,dip,rake} becomes available). Currently always the placeholder.
  _drawBeachball(canvas, focal) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const S = 86; canvas.width = S * dpr; canvas.height = S * dpr;
    const ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,S,S);
    const cx = S/2, cy = S/2, r = S/2 - 4;
    if (!focal) {
      // Placeholder: lingkaran abu putus-putus + tanda tanya
      ctx.fillStyle = '#1e293b'; ctx.beginPath(); ctx.arc(cx,cy,r,0,2*Math.PI); ctx.fill();
      ctx.strokeStyle = '#475569'; ctx.lineWidth = 1.5; ctx.setLineDash([4,3]);
      ctx.beginPath(); ctx.arc(cx,cy,r,0,2*Math.PI); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#64748b'; ctx.font = 'bold 22px sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText('?', cx, cy+1);
      ctx.font = '7px monospace'; ctx.fillStyle = '#475569'; ctx.textBaseline = 'alphabetic';
      ctx.fillText('focal', cx, S-3);
      return;
    }
    // (When a focal SDR becomes available → render nodal planes here.)
    ctx.fillStyle = '#fff'; ctx.beginPath(); ctx.arc(cx,cy,r,0,2*Math.PI); ctx.fill();
    ctx.strokeStyle = '#334155'; ctx.lineWidth = 1.5; ctx.stroke();
  }

  /* ── Engine & Diagnostics card (gabungan: status mesin + engine log) ──────── */
  _renderEngineCard(engineLog, picker, assoc, sdsPath) {
    const el = document.getElementById('om-eng-body');
    if (!el) return;
    const dot = on => `<span class="om-eng-dot ${on ? 'on' : 'off'}"></span>`;
    const pkRun = !!picker?.running, asRun = !!assoc?.running;
    // Active associator label (REAL / GaMMA / PyOcto / glass3)
    const beInfoCard = this._beInfo(assoc?.backend);
    const beLbl = beInfoCard.label;
    const beTag = beInfoCard.tag;
    // DeepDenoiser status — its own card, rendered BEFORE PhaseNet (it runs
    // as a pre-processing stage ahead of PhaseNet in the pipeline).
    const ddOn      = !!picker?.denoise;
    const ddReady   = !!picker?.denoiser_ready;
    const ddPre     = picker?.denoise_pretrained || 'original';
    const ddRowHtml = `
        <div class="om-eng-row" title="DeepDenoiser — cleans noise from the waveform before PhaseNet">
          <div class="om-eng-row-hdr">${dot(ddOn && ddReady)} <b>DeepDenoiser</b></div>
          <span>${!ddOn ? 'off' : ddReady ? `on · pretrained=${ddPre}` : `loading pretrained=${ddPre}…`}</span>
        </div>`;
    const statusHtml = `
      <div class="om-eng-status">
        ${ddRowHtml}
        <div class="om-eng-row">
          <div class="om-eng-row-hdr">${dot(pkRun)} <b>PhaseNet</b></div>
          <span>${pkRun ? `${picker.n_cycles} cycles · ${picker.n_picks_total} pick · ${picker.n_stations} sta` : 'off'}</span>
          ${picker?.error ? `<span class="om-eng-err" style="font-size:.6rem">⚠ ${picker.error}</span>` : ''}
        </div>
        <div class="om-eng-row">
          <div class="om-eng-row-hdr">${dot(asRun)} <b>${beLbl}</b></div>
          <span>${asRun ? `${assoc.n_cycles} cycles · ${assoc.n_events_total} event` : 'off'}</span>
          ${assoc?.error ? `<span class="om-eng-err" style="font-size:.6rem">⚠ ${assoc.error}</span>` : ''}
        </div>
        <div class="om-eng-row" onclick="OM.openSdsStatus()" style="cursor:pointer" title="Status &amp; location of waveform storage (SDS)">
          <div class="om-eng-row-hdr">${dot(!!sdsPath)} <b>SDS</b></div>
          <span>${sdsPath ? 'connected' : 'ring buffer'}${(() => {
            // SDS source: when connected, derive DIRECTLY from the path (fresh each poll);
            // when on the ring buffer, use the archive target from the chip (this._sdsSource).
            const src = sdsPath
              ? (String(sdsPath).includes('online_sds') ? 'SeisWork' : 'SeisComP')
              : (this._sdsSource || '');
            return src ? ` | ${src}` : '';
          })()}</span>
        </div>
      </div>`;
    const tag = src => {
      // 'gamma' = the engine-log source key for the associator (REAL/GaMMA) — the label
      // follows the active backend (beTag) for consistency with the "engine" in use.
      const m = { phasenet: ['AI','om-eng-ai'], gamma: [beTag,'om-eng-gmm'],
                  scautopick: ['SC','om-eng-sc'], system: ['SYS','om-eng-sys'],
                  denoiser: ['DD','om-eng-dd'] };
      const [t, c] = m[src] || ['?','om-eng-sys'];
      return `<span class="om-eng-tag ${c}">${t}</span>`;
    };
    const logRows = (engineLog || []).slice().reverse().map(l => {
      const tStr = (() => { const _d = new Date(l.t * 1000); const _p = n => String(n).padStart(2,'0'); return `${_p(_d.getUTCHours())}:${_p(_d.getUTCMinutes())}:${_p(_d.getUTCSeconds())}`; })();
      const lv = l.level === 'event' ? 'om-al-event'
               : l.level === 'error' ? 'om-al-err'
               : l.level === 'warn'  ? 'om-al-wait' : 'om-al-none';
      return `<div class="om-al-row ${lv}">
                <span class="om-al-t">${tStr}</span>${tag(l.source)}
                <span class="om-al-msg">${l.msg || ''}</span>
              </div>`;
    }).join('') || '<div class="om-placeholder" style="padding:.4rem">Waiting for engine activity…</div>';
    el.innerHTML = `${statusHtml}
      <div class="om-eng-loghdr">Engine Log — ${ddOn ? 'DeepDenoiser → ' : ''}PhaseNet · ${beLbl}</div>
      <div class="om-eng-log">${logRows}</div>`;
  }

  /* ── Plot the LATEST event on the map + a thin white line to each station ── */
  _plotEventsOnMap(events) {
    if (!this._map || typeof L === 'undefined') return;
    if (!this._eventLayer) this._eventLayer = L.layerGroup().addTo(this._map);
    if (!this._stationLineLayer) this._stationLineLayer = L.layerGroup().addTo(this._map);
    this._eventLayer.clearLayers();
    this._stationLineLayer.clearLayers();

    const selIdx = this._selEventIdx || 0;
    const ev = (events || [])[selIdx] || (events || [])[0];
    if (!ev) return;

    const rad = ev.mag != null ? Math.max(7, 5 + ev.mag * 2.4) : 8;
    const m = L.circleMarker([ev.lat, ev.lon], {
      radius: rad,
      color: '#fca5a5', weight: 1.5,
      fillColor: '#ef4444', fillOpacity: 0.9,
      pane: 'eventPane',
    });
    const nc = ev.nearest_city;
    const ncTxtMap = _ncText(nc);
    const ncStr = ncTxtMap
      ? `<br><span style="color:#93c5fd">📍 ${ncTxtMap}</span>`
      : '';
    // Show the city distance in the map panel header
    const locEl = document.getElementById('om-map-event-loc');
    if (locEl) locEl.innerHTML = ncTxtMap
      ? `<i class="bi bi-geo-alt-fill" style="margin-right:.2rem"></i>${ncTxtMap}`
      : '';
    m.bindPopup(
      `<b>${ev.mag != null ? 'M' + ev.mag.toFixed(2) : 'M —'}</b> · ` +
      `${ev.depth_km.toFixed(1)} km<br>` +
      `${typeof _TZ !== 'undefined' ? _TZ.fmt(ev.datetime) : ev.datetime}<br>` +
      `${ev.lat.toFixed(3)}, ${ev.lon.toFixed(3)}` +
      `<br>RMS ${ev.rms != null && !isNaN(ev.rms) ? ev.rms.toFixed(2) + 's' : '—'}` +
      ncStr
    );
    m.bindTooltip(
      `<b>${ev.mag != null ? 'M' + ev.mag.toFixed(2) : 'M —'}</b> · ` +
      `${ev.depth_km.toFixed(0)} km · ${ev.nsta} sta` +
      `<br>${typeof _TZ !== 'undefined' ? _TZ.fmt(ev.datetime) : ev.datetime}`,
      { direction: 'top', className: 'sw-tooltip', sticky: true }
    );
    m.on('click', () => this.selectEvent(0));
    this._eventLayer.addLayer(m);

    // Lines to recording stations. Ideally uses ev.stations (the NET.STA
    // associated by GaMMA — accurate). When empty (old event without a stations field),
    // fall back to the `nsta` NEAREST stations as an ESTIMATE (not all stations,
    // not misleading) — drawn dimmer + finely dashed to distinguish.
    const recStations = new Set(ev.stations || []);
    const allMarkers  = Object.entries(this._markers || {});
    let targets, approx = false;
    if (recStations.size > 0) {
      targets = allMarkers.filter(([key]) => recStations.has(key));
    } else if (ev.nsta > 0 && allMarkers.length) {
      approx = true;
      targets = allMarkers
        .map(([key, mk]) => {
          const ll = mk.getLatLng();
          const dlat = ll.lat - ev.lat, dlon = ll.lng - ev.lon;
          return { key, mk, d2: dlat * dlat + dlon * dlon };
        })
        .sort((a, b) => a.d2 - b.d2)
        .slice(0, ev.nsta)
        .map(o => [o.key, o.mk]);
    } else {
      targets = [];
    }
    // Epicenter→station lines (BotListener/botgui style: thin white lines to each
    // recording station). Associated stations = brighter solid lines; nearest
    // estimates = dim dashed lines.
    const col  = approx ? 'rgba(255,255,255,0.32)' : 'rgba(125,211,252,0.75)';
    const dash = approx ? '3,6' : null;
    targets.forEach(([key, mk]) => {
      const ll = mk.getLatLng();
      const line = L.polyline([[ll.lat, ll.lng], [ev.lat, ev.lon]], {
        color: col, weight: approx ? 1.0 : 1.4, dashArray: dash, interactive: false,
      });
      this._stationLineLayer.addLayer(line);
      // Mark recording stations (not estimates) with a bright blue icon like the reference
      if (!approx && mk.setIcon) mk.setIcon(_stationIcon('#38bdf8', 15));
    });
  }

  /* ── Pick Log: polling inkremental + render ───────────────────────────────── */
  async _pollPickLog() {
    try {
      const url = `/api/online/picks/recent?since=${this._pickLogSince}&n=100`;
      const data = await fetch(url).then(r => r.json());
      const picks = data.picks || [];
      if (picks.length > 0) {
        this._pickLogSince = Math.max(...picks.map(p => p.t));
        this._appendPickLog(picks);
        picks.forEach(p => this._blinkStation(p.net, p.sta, '#facc15'));
      }
      const total = data.total ?? this._pickLogTotal;
      if (total !== this._pickLogTotal) {
        this._pickLogTotal = total;
        const badge = document.getElementById('om-pick-badge');
        if (badge) {
          badge.textContent = total;
          if (picks.length > 0) {
            badge.classList.add('has-new');
            setTimeout(() => badge.classList.remove('has-new'), 700);
          }
        }
      }
    } catch (_) { /* transient */ }
  }

  _appendPickLog(picks) {
    // ── Fill the pick store (authoritative source for waveform rendering) ───
    const cutoff = this._nowSec() - 1800;
    for (const p of picks) {
      const k = `${p.net}.${p.sta}`;
      let arr = this._pickStore.get(k);
      if (!arr) { arr = []; this._pickStore.set(k, arr); }
      const dk = `${Number(p.t).toFixed(1)}_${p.phase}`;
      if (!arr.some(x => `${Number(x.t).toFixed(1)}_${x.phase}` === dk)) {
        arr.push({ t: p.t, phase: p.phase, source: p.source });
      }
      // Trim to the 30-minute window so it does not grow unbounded
      if (arr.length > 200) {
        const filtered = arr.filter(x => x.t >= cutoff);
        this._pickStore.set(k, filtered.length ? filtered : arr.slice(-50));
      }
    }

    const el = document.getElementById('om-pick-log-list');
    if (!el) return;
    // Remove the placeholder if still present
    const ph = el.querySelector('.om-placeholder');
    if (ph) ph.remove();
    const wasAtTop = el.scrollTop < 40;
    // picks arrive ascending from the server → reverse so newest is on top
    const frag = document.createDocumentFragment();
    for (let i = picks.length - 1; i >= 0; i--) {
      const p = picks[i];
      const dt  = new Date(p.t * 1000);
      const hh  = String(dt.getUTCHours()).padStart(2, '0');
      const mm  = String(dt.getUTCMinutes()).padStart(2, '0');
      const ss  = (dt.getUTCSeconds() + dt.getUTCMilliseconds() / 1000)
                    .toFixed(2).padStart(5, '0');
      const tStr    = `${hh}:${mm}:${ss}`;
      const phClass = p.phase === 'S' ? 'om-pick-badge-S' : 'om-pick-badge-P';
      const srcCls  = p.source === 'phasenet' ? 'om-pick-src-phasenet' : 'om-pick-src-scautopick';
      const entry   = document.createElement('div');
      entry.className = 'om-pick-entry';
      entry.innerHTML =
        `<span class="om-pick-t">${tStr}</span>` +
        `<span class="om-pick-sta">${p.net}.${p.sta}</span>` +
        `<span class="om-pick-cha">${p.cha || '&nbsp;&nbsp;—'}</span>` +
        `<span class="om-pick-badge ${phClass}">${p.phase}</span>` +
        `<span class="om-pick-src ${srcCls}">${p.source}</span>`;
      frag.appendChild(entry);
    }
    el.insertBefore(frag, el.firstChild);
    // Trim to max 300 rows so the DOM does not grow unbounded
    while (el.children.length > 300) el.removeChild(el.lastChild);
    if (wasAtTop) el.scrollTop = 0;
  }

  // Fast redraw of the time axis only (not the whole heavy waveform canvas) —
  // used by the 1 s timer so the entire axis (not just the "now" line) keeps
  // flowing forward with the system clock continuously.
  _redrawAxis() {
    const outer = document.getElementById('om-wv-plot');
    const axisCanvas = document.getElementById('om-wv-axis');
    if (!outer || !axisCanvas) return;
    renderTimeAxis(axisCanvas, outer.clientWidth || 600, this._nowSec());
  }

  /* ── Network status panel ────────────────────────────────────────────────────
   * Shows RTT, waveform status, spectrogram status, and timeout/stale warnings
   * in the status bar (#om-net-status). Called every time _tick/_tickSpec finishes.
   * Poor-internet scenario (190-270ms): RTT shows yellow, warns above 500ms/timeout.
   * ─────────────────────────────────────────────────────────────────────────── */
  _updateNetStatus({ rtt, wfOk, specOk, specMs, timeout, specTimeout, slConnected } = {}) {
    if (rtt != null)          this._rtt = rtt;
    if (wfOk != null)         this._wfOk  = wfOk;
    if (specOk != null)       this._specOk = specOk;
    if (specMs != null)       this._specMs = specMs;
    if (slConnected != null)  this._lastSlConn = slConnected;  // persists even when the poll fails
    if (timeout)        this._wfTimeout  = true; else if (wfOk)  this._wfTimeout  = false;
    if (specTimeout)    this._specTimeout = true; else if (specOk) this._specTimeout = false;

    const r        = this._rtt || 0;
    const rttColor = r < 300 ? '#22c55e' : r < 600 ? '#f59e0b' : '#ef4444';
    const rttLabel = r < 300 ? 'Baik' : r < 600 ? 'Lambat' : 'Buruk';
    const rttTxt   = r > 0 ? `${r}ms` : '—';

    const staleSec = this._lastTickMs ? Math.round((Date.now() - this._lastTickMs) / 1000) : null;

    // ── Warning messages (poor connectivity) — used by the status-bar icon
    //    AND the detail block in the diagnostics panel. ───
    const warnMsgs = [];
    if (r > 500)                  warnMsgs.push(`RTT ${r}ms — fairly slow, the spectrogram may be delayed.`);
    if (this._wfTimeout)          warnMsgs.push('Waveform timeout — slow server or network.');
    if (this._specTimeout)        warnMsgs.push('Spectrogram timeout — skipped this cycle, retrying in 8s.');
    if (staleSec != null && staleSec > 15) warnMsgs.push(`Data has not updated for ${staleSec}s.`);

    // Dynamic warning icon: appears (pulsing) ONLY on network problems;
    // with a healthy network → no icon (no space used). Hover → tooltip with the
    // full message. Shared by the compact status bar AND the diagnostics RTT row
    // (so no separate warning text block that pushes the engine log down).
    const _warnIcon = (extra = '') => warnMsgs.length
      ? `<span class="om-net-warn-icon ${extra}" data-tip="${
           warnMsgs.map(m => '⚠ ' + m).join('\n').replace(/"/g, '&quot;')
         }">⚠</span>`
      : '';

    // ── Status bar ringkas ───
    const barEl = document.getElementById('om-net-status');
    if (barEl) {
      barEl.innerHTML = r > 0
        ? `<span style="color:${rttColor};font-weight:600" title="Ping from the browser to the SeisWork server">Server ping: ${rttTxt}</span>${_warnIcon()}`
        : '';
    }

    // ── Panel diagnostik detail ───
    const diag = document.getElementById('om-net-diag');
    if (!diag) return;
    diag.classList.remove('hidden');

    const _set = (id, html, color) => {
      const e = document.getElementById(id);
      if (e) { e.innerHTML = html; if (color) e.style.color = color; }
    };

    _set('om-nd-rtt',
      r > 0 ? `<span style="color:${rttColor}">${rttTxt} — ${rttLabel}</span>${_warnIcon('om-warn-left')}` : '—');

    // SeedLink (engine↔data source): independent of the browser connection — the server
    // buffer keeps running even with an intermittent browser. Show the last known status.
    const slOk = this._lastSlConn;
    _set('om-nd-slconn',
      slOk == null ? '—'
      : slOk ? `<span style="color:#22c55e">● Connected</span>`
             : `<span style="color:#f59e0b">↻ Reconnecting…</span>`);

    // Browser↔SeisWork: hysteresis of ≥2 consecutive failures so a slow connection
    // (occasional timeouts) does not immediately show "Disconnected" and confuse the user.
    const failCount = this._tickFails || 0;
    const browserDisconnected = (this._wfOk === false) && failCount >= 2;
    _set('om-nd-conn',
      browserDisconnected
        ? `<span style="color:#ef4444">✕ Disconnected</span>`
        : `<span style="color:#22c55e">● Connected</span>`);

    let specDetail = '—';
    if (this._specTimeout)      specDetail = `<span style="color:#f59e0b">⏱ Timeout (>4s)</span>`;
    else if (this._specOk === false) specDetail = `<span style="color:#ef4444">✕ Failed</span>`;
    else if (this._specMs)      specDetail = `<span style="color:#22c55e">${this._specMs}ms · 30 minutes</span>`;
    _set('om-nd-spec', specDetail);

    const lastMs = this._lastTickMs;
    const agoSec = lastMs ? Math.round((Date.now() - lastMs) / 1000) : null;
    _set('om-nd-last',
      agoSec == null ? '—' :
      agoSec < 5  ? `<span style="color:#22c55e">${agoSec}s ago</span>` :
      agoSec < 15 ? `<span style="color:#f59e0b">${agoSec}s ago</span>` :
                    `<span style="color:#ef4444">${agoSec}s ago — data is late</span>`);

    // Note: the full warning text block (#om-nd-warn) was REMOVED — messages now live
    // only in the ⚠ icon tooltip next to RTT (see warnIcon). This frees vertical space
    // so the engine log can move up (user request).
  }

  // Rows (stations) currently visible in the waveform panel's scroll viewport —
  // used so spectrograms are computed/requested only for relevant rows
  // (lazy load), not every station each poll (a scipy spectrogram is not cheap
  // when its window follows the waveform window, now 30 minutes).
  _visibleStreamKeys() {
    const outer = document.getElementById('om-wv-plot');
    if (!outer || !this._wvRowOrderKeys?.length || !this._wvRowH) return [];
    const rowH = this._wvRowH;
    const first = Math.max(0, Math.floor(outer.scrollTop / rowH) - 1);
    const last  = Math.min(this._wvRowOrderKeys.length - 1,
                            Math.ceil((outer.scrollTop + outer.clientHeight) / rowH) + 1);
    return this._wvRowOrderKeys.slice(first, last + 1);
  }

  // _tickSpec: fetch spectrograms SEPARATELY from the main waveform tick.
  // 4 s timeout — if the server is slow, skip this cycle; the waveform keeps going.
  async _tickSpec() {
    if (this._showSpec === false) return;                 // spectrogram view is off
    if (document.getElementById('sw-online-root')?.classList.contains('hidden')) return;
    if (!this._lastGoodStreams) return;
    const keys = this._visibleStreamKeys();
    if (!keys.length) return;
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), 6000);
    try {
      const t0 = Date.now();
      const cfgParam = this._currentCfgId ? `&cfg_id=${encodeURIComponent(this._currentCfgId)}` : '';
      const resp = await fetch(
        `/api/online/spectrogram/all?window=1800&keys=${encodeURIComponent(keys.join(','))}${cfgParam}`,
        { signal: ctrl.signal }
      );
      clearTimeout(tid);
      const sp = await resp.json();
      this._specStaleMs = 0;
      const specsByKey = Object.assign({}, this._lastSpecsByKey);
      (sp.specs || []).forEach(s => { specsByKey[s.key] = s; });
      this._lastSpecsByKey = specsByKey;
      if (this._lastGoodStreams) {
        const _g = this._lastGoodStreams, _sp = specsByKey;
        requestAnimationFrame(() => this._drawWaveformAll(_g, _sp));
      }
      this._updateNetStatus({ specMs: Date.now() - t0, specOk: true });
    } catch (e) {
      clearTimeout(tid);
      this._specStaleMs = (this._specStaleMs || 0) + 8000;
      this._updateNetStatus({ specOk: false, specTimeout: e.name === 'AbortError' });
    }
  }

  // _refreshVisibleSpectrograms: alias (called from the scroll event)
  _refreshVisibleSpectrograms() {
    this._tickSpec();
    if (this._showDenoise) this._tickDenoise();   // recompute denoise for newly-visible rows
  }

  /* ── View toggles: Spectrogram on/off + Denoise (DeepDenoiser) on/off ──────── */
  toggleSpecView(on) {
    this._showSpec = !!on;
    if (this._lastGoodStreams) this._drawWaveformAll(this._lastGoodStreams, this._lastSpecsByKey || {});
    if (on) this._tickSpec();                      // refetch spectrograms when re-enabled
  }

  toggleDenoiseView(on) {
    this._showDenoise = !!on;
    const st = document.getElementById('om-wv-denoise-status');
    clearInterval(this._denoiseTimer);
    if (on) {
      if (st) st.textContent = '· loading…';
      this._tickDenoise();
      this._denoiseTimer = setInterval(() => this._tickDenoise(), 6000);
    } else {
      this._denoiseTimer   = null;
      this._denoisedByKey  = {};
      if (st) st.textContent = '';
      if (this._lastGoodStreams) this._drawWaveformAll(this._lastGoodStreams, this._lastSpecsByKey || {});
    }
  }

  // Fetch DeepDenoiser-cleaned points for the visible rows; overlay them on redraw.
  async _tickDenoise() {
    if (!this._showDenoise) return;
    if (document.getElementById('sw-online-root')?.classList.contains('hidden')) return;
    if (!this._lastGoodStreams) return;
    const keys = this._visibleStreamKeys();
    if (!keys.length) return;
    const st = document.getElementById('om-wv-denoise-status');
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), 15000);
    try {
      const cfgParam = this._currentCfgId ? `&cfg_id=${encodeURIComponent(this._currentCfgId)}` : '';
      const resp = await fetch(
        `/api/online/waveform/denoised?window=1800&keys=${encodeURIComponent(keys.join(','))}${cfgParam}`,
        { signal: ctrl.signal });
      clearTimeout(tid);
      const j = await resp.json();
      if (!j.ready) {
        if (st) st.textContent = j.loading ? '· loading model…' : '· unavailable';
        return;
      }
      const dn = Object.assign({}, this._denoisedByKey);
      Object.entries(j.denoised || {}).forEach(([k, pts]) => { dn[k] = pts; });
      this._denoisedByKey = dn;
      if (st) st.textContent = '· on';
      if (this._lastGoodStreams) this._drawWaveformAll(this._lastGoodStreams, this._lastSpecsByKey || {});
    } catch (e) {
      clearTimeout(tid);
      if (st) st.textContent = (e.name === 'AbortError') ? '· timeout' : '· error';
    }
  }

  /* ── Client buffer helpers (scrttv-style delta fetch) ─────────────────────
   * Instead of re-fetching the full 30-minute payload every 2s (580 KB), we:
   *   1. Snapshot ONCE on connect — a list of contiguous segments per stream
   *   2. Delta every tick — only new segments since the cursor (DATA time) per stream
   *   3. Accumulate in _clientBufs, rendering from there (independent of large payloads)
   * Each stream carries `last_t` (the last sample data time) → becomes buf.cursor.
   * Segments reconstruct the CORRECT time (t = t0 + i*step per segment) so gaps
   * acquisition is drawn as-is with no time compression.
   * ─────────────────────────────────────────────────────────────────────────── */
  _segmentsToPts(segments, into) {
    // Keep time monotonically increasing: delta segments can overlap/step back from
    // the last point in the buffer (overlapping delta windows / retransmission).
    // Points with t <= the last point are SKIPPED — otherwise the polyline draws
    // a line sweeping back to the left (the "connector line" bug).
    let lastT = into.length ? into[into.length - 1].t : -Infinity;
    for (const seg of (segments || [])) {
      const step = seg.step > 0 ? seg.step : 1.0;
      const vs = seg.vs || [];
      for (let i = 0; i < vs.length; i++) {
        const t = seg.t0 + i * step;
        if (t <= lastT) continue;
        into.push({ t, v: vs[i] });
        lastT = t;
      }
    }
  }

  _applySnapshot(snap) {
    this._clientBufs.clear();
    for (const s of (snap.streams || [])) {
      const pts = [];
      this._segmentsToPts(s.segments, pts);
      if (!pts.length) continue;
      this._clientBufs.set(s.key, {
        pts, picks: s.picks || [],
        meta: { net: s.net, sta: s.sta, loc: s.loc, cha: s.cha, sr: s.sr },
        cursor: (s.last_t != null) ? s.last_t : pts[pts.length - 1].t,
      });
    }
    this._snapDone     = true;
    this._sessionEpoch = (snap.session_epoch != null) ? snap.session_epoch : this._sessionEpoch;
    this._updateServerOffset(snap.server_time);
    this._updateDataOffset(snap.server_time);
  }

  _applyDelta(delta) {
    // Detect server restarts via session_epoch — no longer via server_time going backward
    // (the cursor is now data-time based, so server_time is no longer used
    // as a cursor). When the epoch changes → the old buffer is invalid, re-snapshot.
    if (delta.session_epoch != null && this._sessionEpoch != null
        && delta.session_epoch !== this._sessionEpoch) {
      this._clientBufs.clear();
      this._snapDone     = false;
      this._sessionEpoch = null;
      return;
    }
    const MAX_PTS  = 4200;          // 1800s window @ envelope 2 titik/dtk (~3600) + headroom
    const WIN_PICK = 1800;
    const cutoff   = this._nowSec() - WIN_PICK;

    for (const s of (delta.streams || [])) {
      let buf = this._clientBufs.get(s.key);
      if (!buf) {
        buf = { pts: [], picks: [],
                meta: { net: s.net, sta: s.sta, loc: s.loc, cha: s.cha, sr: s.sr },
                cursor: null };
        this._clientBufs.set(s.key, buf);
      }
      this._segmentsToPts(s.segments_new, buf.pts);
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
    this._updateServerOffset(delta.server_time);
    this._updateDataOffset(delta.server_time);
  }

  _clientBufsToStreams() {
    const streams = [];
    for (const [key, buf] of this._clientBufs) {
      const m = buf.meta;
      // Picks from the pick store (NET.STA key) — authoritative, shown on all of the
      // station's channels. Merged with old delta picks (if any) as a fallback.
      const storePicks = this._pickStore.get(`${m.net}.${m.sta}`) || [];
      const picks = storePicks.length ? storePicks : (buf.picks || []);
      streams.push({ key, net: m.net, sta: m.sta, loc: m.loc, cha: m.cha,
                     points: buf.pts, picks });
    }
    return streams;
  }

  // _tick: only a light status poll every 5 s — waveform data already arrives via SSE push.
  async _tick() {
    if (document.getElementById('sw-online-root')?.classList.contains('hidden')) return;
    const t0 = Date.now();
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), 8000);
      const st = await fetch('/api/online/status', { signal: ctrl.signal })
                        .then(r => { clearTimeout(tid); return r.json(); });
      const rtt = Date.now() - t0;
      this._rtt = rtt;

      document.getElementById('om-conn-info').innerHTML =
        `${this._addrBadge(!!st.connected)} ${st.n_packets} pkt · ${st.last_data_time || '—'}`;
      if (st.streams?.length > 0) this._updateMarkerColors(st.streams);
      if (!st.connected) {
        this._setStatus('amber', st.error ? 'Lost connection: ' + st.error
                                          : 'Server session lost — reconnecting…');
        this._autoReconnect();
      } else {
        this._setStatus('green', 'Live');
      }
      this._updateNetStatus({ rtt, slConnected: st.connected });
    } catch (e) {
      this._updateNetStatus({ rtt: Date.now() - t0 });
    }
  }

  /* ── Waveform + Spectrogram Draw — delegasi ke fungsi bersama
     waveform-canvas-render.js (also used by the full-page zoom view),
     so the dashboard & full-page views always look identical. ──────────── */
  _drawWaveformAll(good, specsByKey) {
    const outer  = document.getElementById('om-wv-plot');
    const canvas = document.getElementById('om-wv-canvas');
    if (!outer || !canvas || !good.length) return;
    const showSpec = this._showSpec !== false;                       // spectrogram toggle
    const streams  = this._showDenoise ? this._applyDenoiseToStreams(good) : good;
    const { rowH, rowOrderKeys } =
      renderWaveformCanvas(canvas, outer, streams, specsByKey, undefined, showSpec, this._nowSec());
    this._wvRowH = rowH;
    this._wvRowOrderKeys = rowOrderKeys;
    this._redrawAxis();
  }

  /* Overlay the DeepDenoiser-cleaned points onto the streams whose key we have a
     denoised copy for; raw rows (not yet computed / off-screen) stay unchanged. */
  _applyDenoiseToStreams(good) {
    const dn = this._denoisedByKey || {};
    if (!Object.keys(dn).length) return good;
    return good.map(s => {
      const pts = dn[s.key];
      return (pts && pts.length > 1) ? Object.assign({}, s, { points: pts }) : s;
    });
  }

  // Scroll the waveform panel so the station row clicked on the map is visible.
  _scrollWaveformTo(net, sta) {
    const outer = document.getElementById('om-wv-plot');
    if (!outer || !this._wvRowOrder) return;
    const idx = this._wvRowOrder.indexOf(`${net}.${sta}`);
    if (idx < 0) return;
    outer.scrollTo({ top: idx * this._wvRowH, behavior: 'smooth' });
  }

  /* ── Station side-info panel — alternative when the Leaflet map is slow/blank
     (tergantung CDN basemap eksternal). Dipicu hover label baris waveform ATAU
     or a map marker is clicked — shows location, channels, and a live mini-waveform. ── */
  _attachWvHover() {
    const outer = document.getElementById('om-wv-plot');
    if (!outer || this._wvHoverAttached) return;
    this._wvHoverAttached = true;
    outer.addEventListener('mousemove', e => {
      const rect = outer.getBoundingClientRect();
      const x = e.clientX - rect.left;
      if (x > 84) { this._scheduleHideStationInfo(); return; }  // not in the label area
      const yAbs = outer.scrollTop + (e.clientY - rect.top);
      const idx = Math.floor(yAbs / (this._wvRowH || 95));
      const key = this._wvRowOrderKeys?.[idx];
      if (!key) return;
      const [net, sta] = key.split('.');
      clearTimeout(this._hoverHideTimer);
      clearTimeout(this._hoverShowTimer);
      this._hoverShowTimer = setTimeout(() => this.showStationInfo(net, sta), 180);
    });
    outer.addEventListener('mouseleave', () => this._scheduleHideStationInfo());

    // Double-click anywhere on a row → open single-station detail modal
    outer.addEventListener('dblclick', e => {
      const rect = outer.getBoundingClientRect();
      const yAbs = outer.scrollTop + (e.clientY - rect.top);
      const idx  = Math.floor(yAbs / (this._wvRowH || 95));
      const key  = this._wvRowOrderKeys?.[idx];
      if (!key) return;
      const [net, sta] = key.split('.');
      e.preventDefault();
      this.showStationDetail(net, sta);
    });
  }
  _scheduleHideStationInfo() {
    clearTimeout(this._hoverShowTimer);
    clearTimeout(this._hoverHideTimer);
    this._hoverHideTimer = setTimeout(() => this.hideStationInfo(), 300);
  }

  /* ── Station Viewer Modal (RaspberryShake StationView style) ──────────────
   * Shows ALL active channels (with data in _clientBufs) for the
   * selected station, each with its own 2-minute waveform canvas.
   * Auto-refreshes every 2s while the modal is open (following live data).
   * ─────────────────────────────────────────────────────────────────────── */
  showStationInfo(net, sta) {
    clearTimeout(this._hoverHideTimer);
    clearInterval(this._staModalTimer);

    this._staModalNet = net;
    this._staModalSta = sta;

    // Fill in the header metadata
    const known = (this._stationList || []).find(s => s.net === net && s.sta === sta);
    document.getElementById('om-sta-info-net').textContent   = net;
    document.getElementById('om-sta-info-title').textContent = sta;
    document.getElementById('om-sta-info-name').textContent  = known?.name || '';
    document.getElementById('om-sta-info-loc').textContent   = known
      ? `${known.lat.toFixed(4)}°, ${known.lon.toFixed(4)}°` : '—';
    document.getElementById('om-sta-info-elev').textContent  = known
      ? `${known.elev ?? '?'} m` : '— m';

    // Show the modal + backdrop
    document.getElementById('om-sta-backdrop').classList.remove('hidden');
    document.getElementById('om-sta-info').classList.remove('hidden');

    // Draw immediately, then auto-refresh every 2 s
    this._renderStationModal();
    this._staModalTimer = setInterval(() => this._renderStationModal(), 2000);
  }

  hideStationInfo() {
    clearInterval(this._staModalTimer);
    this._staModalTimer = null;
    document.getElementById('om-sta-info')?.classList.add('hidden');
    document.getElementById('om-sta-backdrop')?.classList.add('hidden');
  }

  // helper so _pausePoll can close the modal too
  _staModalCloseTimer() { this.hideStationInfo(); }

  /* ── Single Station Detail Modal (double-click waveform row) ─────────────
   * Shows all of the station's channels with a large canvas + spectrogram +
   * a time axis, a selectable 5m/15m/30m window, refreshing every 1 second.
   * ─────────────────────────────────────────────────────────────────────── */
  showStationDetail(net, sta) {
    clearInterval(this._staDetTimer);
    this._staDetNet = net;
    this._staDetSta = sta;
    if (!this._staDetWin) this._staDetWin = 300;  // default 5 minutes

    const known = (this._stationList || []).find(s => s.net === net && s.sta === sta);
    document.getElementById('om-sta-det-net').textContent   = net;
    document.getElementById('om-sta-det-title').textContent = sta;
    document.getElementById('om-sta-det-name').textContent  = known?.name || '';
    document.getElementById('om-sta-det-loc').textContent   = known
      ? `${known.lat.toFixed(4)}°, ${known.lon.toFixed(4)}°` : '—';
    document.getElementById('om-sta-det-elev').textContent  = known
      ? `${known.elev ?? '?'} m` : '— m';

    // Sync active button
    document.querySelectorAll('.om-sta-det-wbtn').forEach(btn => {
      btn.classList.toggle('active', parseInt(btn.dataset.win) === this._staDetWin);
    });

    document.getElementById('om-sta-det-bd').classList.remove('hidden');
    document.getElementById('om-sta-det').classList.remove('hidden');

    this._renderStationDetail();
    this._staDetTimer = setInterval(() => this._renderStationDetail(), 1000);

    // Close with Esc
    if (!this._staDetEscHandler) {
      this._staDetEscHandler = e => { if (e.key === 'Escape') this.hideStationDetail(); };
      document.addEventListener('keydown', this._staDetEscHandler);
    }
  }

  hideStationDetail() {
    clearInterval(this._staDetTimer);
    this._staDetTimer = null;
    document.getElementById('om-sta-det')?.classList.add('hidden');
    document.getElementById('om-sta-det-bd')?.classList.add('hidden');
    if (this._staDetEscHandler) {
      document.removeEventListener('keydown', this._staDetEscHandler);
      this._staDetEscHandler = null;
    }
  }

  setStaDetWin(sec) {
    this._staDetWin = sec;
    document.querySelectorAll('.om-sta-det-wbtn').forEach(btn => {
      btn.classList.toggle('active', parseInt(btn.dataset.win) === sec);
    });
    this._renderStationDetail();
  }

  _renderStationDetail() {
    const net = this._staDetNet, sta = this._staDetSta;
    if (!net || !sta) return;
    const win = this._staDetWin || 300;

    // Collect all of this station's active channels
    const staPicks = this._pickStore.get(`${net}.${sta}`) || [];
    const streams  = [];
    for (const [key, buf] of this._clientBufs) {
      if (buf.meta.net === net && buf.meta.sta === sta && buf.pts.length > 1) {
        streams.push({
          key, net: buf.meta.net, sta: buf.meta.sta,
          loc: buf.meta.loc, cha: buf.meta.cha,
          points: buf.pts, picks: staPicks.length ? staPicks : (buf.picks || []),
        });
      }
    }
    streams.sort((a, b) => {
      const order = c => c.endsWith('Z') ? 0 : c.endsWith('N') || c.endsWith('1') ? 1
                       : c.endsWith('E') || c.endsWith('2') ? 2 : 3;
      return order(a.cha) - order(b.cha);
    });

    // Update metadata
    const lastT = streams[0]?.points?.at(-1)?.t;
    if (lastT) {
      const d = new Date(lastT * 1000), _p = n => String(n).padStart(2, '0');
      document.getElementById('om-sta-det-time').textContent =
        `${_p(d.getUTCHours())}:${_p(d.getUTCMinutes())}:${_p(d.getUTCSeconds())} UTC`;
    }
    const allPicks = streams.flatMap(s => s.picks || []);
    const nP = allPicks.filter(p => p.phase === 'P').length;
    const nS = allPicks.filter(p => p.phase === 'S').length;
    document.getElementById('om-sta-det-picks').textContent =
      nP + nS ? `${nP} P · ${nS} S (30 mnt)` : '';

    const outer = document.getElementById('om-sta-det-outer');
    const canvas = document.getElementById('om-sta-det-canvas');
    if (!outer || !canvas) return;

    if (!streams.length) {
      canvas.style.display = 'none';
      outer.innerHTML = '<div style="padding:1.5rem;text-align:center;color:#94a3b8;font-size:.82rem">' +
        '<i class="bi bi-signal" style="font-size:1.4rem;display:block;margin-bottom:.5rem"></i>' +
        'No live data for this station yet</div>';
      return;
    }
    if (!canvas.parentElement) outer.appendChild(canvas);
    canvas.style.display = 'block';

    // Row height: capped at MAX_ROW (170px) per channel → add clientHeight
    const fakeOuter = {
      clientWidth : outer.clientWidth  || 900,
      clientHeight: streams.length * 170,  // target MAX_ROW per channel
      scrollTop   : 0,
    };
    const nowSec = this._nowSec();
    const specs  = this._lastSpecsByKey || {};
    renderWaveformCanvas(canvas, fakeOuter, streams, specs, win, true, nowSec);

    // Time axis aligned to the same window
    const axisEl = document.getElementById('om-sta-det-axis');
    if (axisEl) renderTimeAxis(axisEl, outer.clientWidth || 900, nowSec, win);
  }

  _renderStationModal() {
    const net = this._staModalNet, sta = this._staModalSta;
    if (!net || !sta) return;

    // Collect all of this station's active channels from the client buffer
    const staPicks = this._pickStore.get(`${net}.${sta}`) || [];
    const streams = [];
    for (const [key, buf] of this._clientBufs) {
      if (buf.meta.net === net && buf.meta.sta === sta && buf.pts.length > 1) {
        streams.push({
          key, net: buf.meta.net, sta: buf.meta.sta,
          loc: buf.meta.loc, cha: buf.meta.cha,
          points: buf.pts, picks: staPicks.length ? staPicks : (buf.picks || []),
        });
      }
    }
    // Urutkan: Z → N/1 → E/2 → lainnya
    streams.sort((a, b) => {
      const order = c => c.endsWith('Z') ? 0 : c.endsWith('N') || c.endsWith('1') ? 1
                       : c.endsWith('E') || c.endsWith('2') ? 2 : 3;
      return order(a.cha) - order(b.cha);
    });

    // Metadata header
    const lastT = streams.length ? streams[0].points[streams[0].points.length - 1]?.t : null;
    document.getElementById('om-sta-info-time').textContent = lastT ? (() => {
      const _d = new Date(lastT * 1000); const _p = n => String(n).padStart(2,'0');
      return `${_p(_d.getUTCHours())}:${_p(_d.getUTCMinutes())}:${_p(_d.getUTCSeconds())} UTC`;
    })() : '—';
    const allPicks = streams.flatMap(s => s.picks || []);
    const nP = allPicks.filter(p => p.phase === 'P').length;
    const nS = allPicks.filter(p => p.phase === 'S').length;
    document.getElementById('om-sta-info-picks-summary').textContent =
      nP + nS ? `${nP} P-pick · ${nS} S-pick in the last 30 minutes` : '';

    const container = document.getElementById('om-sta-info-channels');
    if (!container) return;

    if (!streams.length) {
      container.innerHTML = '<div class="om-sta-ch-empty"><i class="bi bi-signal" style="margin-right:.3rem"></i>No live data for this station yet</div>';
      return;
    }

    // One canvas for all of this station's channels — use renderWaveformCanvas
    // with EXACTLY the same window & spectrogram as the main panel (WV_WIN 30 minutes,
    // showSpec=true). Spectrograms come from the _lastSpecsByKey cache already
    // fetched by the main panel, so no extra request is needed.
    let canvas = container.querySelector('canvas.om-sta-modal-canvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.className = 'om-sta-modal-canvas';
      canvas.style.cssText = 'display:block;width:100%;border-radius:4px';
      container.innerHTML = '';
      container.appendChild(canvas);
    }
    // fakeOuter: clientWidth + clientHeight so modal waveform rows stay DYNAMIC
    // (filling the now-larger modal height instead of a fixed 95px). clientHeight comes
    // from the channels container (flex:1 inside the modal).
    const fakeOuter = {
      clientWidth : container.clientWidth  || 620,
      clientHeight: container.clientHeight || 0,
    };
    const specs = this._lastSpecsByKey || {};
    // win=undefined → use the WV_WIN default (1800 s), showSpec=true → identical to the main plot
    renderWaveformCanvas(canvas, fakeOuter, streams, specs, undefined, true, this._nowSec());

    // PSD: refresh only on first open (or every ~10 s) — the obspy computation
    // on the server is fairly heavy; no need for every 2 s like the waveform.
    const nowMs = Date.now();
    if (!this._psdCollapsed &&
        (!this._lastPsdAt || nowMs - this._lastPsdAt > 10000 ||
         this._lastPsdKey !== `${net}.${sta}`)) {
      this._lastPsdAt  = nowMs;
      this._lastPsdKey = `${net}.${sta}`;
      this._fetchStationPsd(net, sta, streams[0].cha);
    }
  }

  /* ── Station PSD (Power Spectral Density via obspy, Task 4) ────────────────── */
  toggleStationPsd() {
    this._psdCollapsed = !this._psdCollapsed;
    const cv  = document.getElementById('om-sta-psd-canvas');
    const tog = document.getElementById('om-sta-psd-toggle');
    if (cv)  cv.style.display = this._psdCollapsed ? 'none' : 'block';
    if (tog) tog.innerHTML = this._psdCollapsed
      ? '<i class="bi bi-chevron-right"></i>' : '<i class="bi bi-chevron-down"></i>';
    if (!this._psdCollapsed && this._staModalNet)
      this._fetchStationPsd(this._staModalNet, this._staModalSta);
  }

  async _fetchStationPsd(net, sta, cha) {
    const info = document.getElementById('om-sta-psd-info');
    try {
      if (info) info.textContent = '· menghitung…';
      const q = `/api/online/psd?net=${net}&sta=${sta}` + (cha ? `&cha=${cha}` : '');
      const data = await fetch(q).then(r => r.json());
      if (data.error || !data.freqs?.length) {
        if (info) info.textContent = data.error ? `· ${data.error}` : '· insufficient data';
        this._drawStationPsd(null);
        return;
      }
      if (info) info.textContent =
        `· ${data.cha} · ${data.sr} Hz · ${data.window_s}s window`;
      this._drawStationPsd(data);
    } catch (e) {
      if (info) info.textContent = '· failed';
      this._drawStationPsd(null);
    }
  }

  _drawStationPsd(data) {
    const canvas = document.getElementById('om-sta-psd-canvas');
    if (!canvas) return;
    const W = canvas.clientWidth || 600, H = 130;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#0b1120'; ctx.fillRect(0, 0, W, H);
    if (!data) {
      ctx.fillStyle = '#475569'; ctx.font = '11px monospace'; ctx.textAlign = 'center';
      ctx.fillText('PSD unavailable', W / 2, H / 2);
      return;
    }
    const { freqs, psd } = data;
    const PADL = 42, PADR = 8, PADT = 8, PADB = 22;
    const pw = W - PADL - PADR, ph = H - PADT - PADB;
    // Sumbu X: log10(frekuensi); Y: dB
    const fmin = Math.max(freqs[0], 0.01), fmax = freqs[freqs.length - 1];
    const lxmin = Math.log10(fmin), lxmax = Math.log10(fmax);
    let ymin = Infinity, ymax = -Infinity;
    for (const v of psd) { if (v < ymin) ymin = v; if (v > ymax) ymax = v; }
    const yr = (ymax - ymin) || 1;
    ymin -= yr * 0.05; ymax += yr * 0.05;
    const toX = f => PADL + (Math.log10(Math.max(f, fmin)) - lxmin) / (lxmax - lxmin) * pw;
    const toY = v => PADT + (1 - (v - ymin) / (ymax - ymin)) * ph;
    // Grid + label dB
    ctx.strokeStyle = '#1e293b'; ctx.fillStyle = '#64748b';
    ctx.font = '9px monospace'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yv = ymin + (ymax - ymin) * i / 4, y = toY(yv);
      ctx.beginPath(); ctx.moveTo(PADL, y); ctx.lineTo(W - PADR, y); ctx.stroke();
      ctx.textAlign = 'right'; ctx.fillText(yv.toFixed(0), PADL - 4, y + 3);
    }
    // Grid X di dekade frekuensi (0.1, 1, 10)
    ctx.textAlign = 'center';
    for (const fv of [0.1, 1, 10, 100]) {
      if (fv < fmin || fv > fmax) continue;
      const x = toX(fv);
      ctx.strokeStyle = '#1e293b';
      ctx.beginPath(); ctx.moveTo(x, PADT); ctx.lineTo(x, H - PADB); ctx.stroke();
      ctx.fillStyle = '#64748b'; ctx.fillText(fv + ' Hz', x, H - PADB + 13);
    }
    // Kurva PSD
    ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1.4; ctx.beginPath();
    for (let i = 0; i < freqs.length; i++) {
      const x = toX(freqs[i]), y = toY(psd[i]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    // Label sumbu Y
    ctx.save(); ctx.translate(10, H / 2); ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = '#94a3b8'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
    ctx.fillText('dB (10·log₁₀ m²/s⁴/Hz)', 0, 0); ctx.restore();
  }

  /* ── Station Map (Leaflet) ──────────────────────────────────────────────── */
  /* Manual refresh button — re-fetches the CURRENT live session's inventory
     set from the server (primary + every extra source) and redraws the map.
     Needed after a source is added/changed through a direct API call, or any
     other path that updates the live session without going through this
     browser tab's connect()/restoreIfLive() (which already do this). Does
     not touch the SeedLink connection itself — map-only refresh. */
  async refreshStationMap() {
    const btn = document.getElementById('om-map-refresh-btn');
    try {
      if (btn) btn.disabled = true;
      const st = await (await fetch('/api/online/status')).json();
      const invAll = (st.inventory_paths?.length ? st.inventory_paths : [st.inventory_path]).filter(Boolean);
      if (!invAll.length) { this._setStatus('amber', 'No inventory on the live session'); return; }
      await this._loadStationMap(invAll.join(','));
      this._setStatus('green', 'Station map refreshed');
    } catch (e) {
      this._setStatus('red', 'Map refresh failed: ' + e.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async _loadStationMap(inventoryPath) {
    try {
      // A region-limited config (Set Area) must only show its in-box stations —
      // the endpoint already bbox-filters when the box params are supplied.
      const reg = this._currentCfg?.region;
      let url = `/api/online/stations?inventory=${encodeURIComponent(inventoryPath)}`;
      if (reg && reg.limit &&
          ['lat_min', 'lat_max', 'lon_min', 'lon_max'].every(k => reg[k] != null)) {
        url += `&lat_min=${reg.lat_min}&lat_max=${reg.lat_max}` +
               `&lon_min=${reg.lon_min}&lon_max=${reg.lon_max}`;
      }
      const r = await (await fetch(url)).json();
      if (!r.stations?.length) return;
      this._stationList = r.stations;
      const areaTag = (reg && reg.limit) ? ' (in area)' : '';
      document.getElementById('om-map-sta-count').textContent = `${r.count} stations${areaTag}`;
      this._initMap(r.stations);
    } catch (e) { console.warn('[OM] station map load error:', e); }
  }

  _initMap(stations) {
    const mapEl = document.getElementById('om-station-map');
    if (!mapEl) return;
    if (this._map) { this._map.remove(); this._map = null; }
    this._markers = {};

    const loadingEl = document.getElementById('om-map-loading');
    if (loadingEl) loadingEl.style.display = 'flex';
    const hideLoading = () => { if (loadingEl) loadingEl.style.display = 'none'; };

    const built = SW.buildOnlineMap('om-station-map', {
      leafletOpts: { zoom: 7 },
      onTilesLoaded: hideLoading,
    });
    if (!built) { hideLoading(); return; }
    this._map = built.map;
    // External basemap CDNs can be slow / time out on office networks — never
    // let the "Loading basemap…" overlay hang forever when the tile load event
    // never fires. The side info panel (hover/click) still works without the map.
    setTimeout(hideLoading, 6000);

    stations.forEach(sta => {
      const key = `${sta.net}.${sta.sta}`;

      const marker = L.marker([sta.lat, sta.lon], {
        icon: _stationIcon('#6b7280', 14),  // grey = no data yet
        pane: 'stationPane',
      }).addTo(this._map);

      marker.bindTooltip(
        `<b>${sta.net}.${sta.sta}</b><br>${sta.name}<br>` +
        `${sta.lat.toFixed(3)}, ${sta.lon.toFixed(3)}<br>Ch: ${sta.default_channel}`,
        { direction: 'top', className: 'sw-tooltip' }
      );

      marker.on('click', () => {
        this._station = `${sta.net}.${sta.sta}..${sta.default_channel}`;
        Object.entries(this._markers).forEach(([k, m]) => {
          m.setIcon(_stationIcon(this._activeStreams.some(
            s => `${s[0]}.${s[1]}` === k
          ) ? '#22c55e' : '#6b7280', 14));
        });
        marker.setIcon(_stationIcon('#f59e0b', 16));
        this._scrollWaveformTo(sta.net, sta.sta);
        this.showStationInfo(sta.net, sta.sta);
      });

      this._markers[key] = marker;
    });

    // Monitoring-area boundary (config "Set Area"): draw the region box so the
    // operator sees the limit the session is subscribed/located within.
    const reg = this._currentCfg?.region;
    if (reg && reg.limit &&
        ['lat_min', 'lat_max', 'lon_min', 'lon_max'].every(k => reg[k] != null)) {
      L.rectangle([[reg.lat_min, reg.lon_min], [reg.lat_max, reg.lon_max]], {
        color: '#22c55e', weight: 2, dashArray: '6 4',
        fill: true, fillColor: '#22c55e', fillOpacity: 0.05, interactive: false,
      }).addTo(this._map).bindTooltip(
        `Monitoring area · center ${(+reg.lat).toFixed(2)}, ${(+reg.lon).toFixed(2)}`,
        { sticky: true, className: 'sw-tooltip' });
    }

    const fit = () => {
      if (!this._map) return;
      if (stations.length > 1) {
        // Small padding + higher maxZoom → tighter framing; station markers
        // & quake epicenters appear larger/clearer.
        this._map.fitBounds(stations.map(s => [s.lat, s.lon]), { padding: [8, 8], maxZoom: 10 });
      } else if (stations.length === 1) {
        this._map.setView([stations[0].lat, stations[0].lon], 9);
      }
    };
    fit();
    // Leaflet often computes a container size of 0 when the map is created while a
    // new panel is appearing (grid not laid out yet) → markers & tiles render at the
    // wrong positions / stations look "invisible". invalidateSize() + a re-fit
    // after the layout settles fixes it.
    [120, 400, 900].forEach(ms => setTimeout(() => {
      if (!this._map) return;
      this._map.invalidateSize(false);
      fit();
    }, ms));
  }

  // Blink a station's map marker — used for both a raw pick AND, later, that
  // same station being folded into an associated event (same color both
  // times, see the constructor note on _seenEventIds). Reverts to whatever
  // its steady-state color would be (selected / active / idle) once the
  // timer runs out, so it never gets stuck highlighted.
  _blinkStation(net, sta, color = '#facc15', durationMs = 4000) {
    const key = `${net}.${sta}`;
    const marker = this._markers?.[key];
    if (!marker) return;
    const selKey = this._station ? `${this._station.split('.')[0]}.${this._station.split('.')[1]}` : '';
    if (selKey === key) return;   // don't mask the "you clicked this" marker
    marker.setIcon(_stationIcon(color, 15, true));
    clearTimeout(this._blinkTimers[key]);
    this._blinkTimers[key] = setTimeout(() => {
      const mk = this._markers?.[key];
      if (!mk) return;
      const isActive = (this._activeStreams || []).some(s => `${s[0]}.${s[1]}` === key);
      mk.setIcon(_stationIcon(isActive ? '#22c55e' : '#6b7280', 14));
    }, durationMs);
  }

  _updateMarkerColors(activeStreams) {
    const activeKeys = new Set(
      activeStreams.map(s => { const p = s.split('.'); return `${p[0]}.${p[1]}`; })
    );
    Object.entries(this._markers).forEach(([key, marker]) => {
      // Never overwrite the currently selected (orange) marker
      if (`${this._station.split('.')[0]}.${this._station.split('.')[1]}` === key) return;
      const color = activeKeys.has(key) ? '#22c55e' : '#6b7280';
      marker.setIcon(_stationIcon(color, 14));
    });
  }

  _clearMap() {
    if (this._map) { this._map.remove(); this._map = null; }
    this._markers          = {};
    this._stationList      = [];
    this._eventLayer       = null;
    this._stationLineLayer = null;
  }

  /* ── HypoDD relocation for the online catalog ─────────────────────────────── */
  async runOnlineHypoDD() {
    const btn  = document.getElementById('om-btn-hypodd');
    const stat = document.getElementById('om-hypodd-status');

    const _setUI = (busy, msg) => {
      if (btn)  { btn.disabled = busy; }
      if (stat) { stat.style.display = msg ? '' : 'none'; stat.textContent = msg || ''; }
    };

    // Check whether a relocation is already running
    try {
      const s = await (await fetch('/api/online/realtime/reloc')).json();
      if (s.status?.state === 'running') {
        _setUI(true, 'Running…');
        this._pollOnlineHypoDD();
        return;
      }
      if (s.status?.state === 'done' && s.result) {
        const reuse = confirm(
          `A previous relocation result already exists (${s.result.n_reloc} events, ` +
          `${(s.result.run_at || '').slice(0,16)}).\n\nRun again?`
        );
        if (!reuse) {
          this._applyOnlineReloc(s.result);
          return;
        }
      }
    } catch (_) {}

    _setUI(true, 'Starting…');
    try {
      const r = await fetch('/api/online/realtime/reloc', { method: 'POST' });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      _setUI(true, 'Running… (ph2dt → hypoDD)');
      this._pollOnlineHypoDD();
    } catch (e) {
      _setUI(false, '');
      alert('Failed to start HypoDD: ' + e.message);
    }
  }

  _pollOnlineHypoDD() {
    const btn  = document.getElementById('om-btn-hypodd');
    const stat = document.getElementById('om-hypodd-status');
    const _setUI = (busy, msg) => {
      if (btn)  btn.disabled = busy;
      if (stat) { stat.style.display = msg ? '' : 'none'; stat.textContent = msg || ''; }
    };

    const poll = async () => {
      try {
        const s = await (await fetch('/api/online/realtime/reloc')).json();
        const state = s.status?.state;
        if (state === 'running') {
          _setUI(true, 'Running…');
          setTimeout(poll, 5000);
        } else if (state === 'done' && s.result) {
          _setUI(false, `✓ ${s.result.n_reloc} events direlokasi`);
          this._applyOnlineReloc(s.result);
        } else if (state === 'error') {
          _setUI(false, '');
          alert('HypoDD error: ' + (s.status?.error || 'unknown'));
        } else {
          setTimeout(poll, 5000);
        }
      } catch (_) {
        setTimeout(poll, 8000);
      }
    };
    setTimeout(poll, 5000);
  }

  _applyOnlineReloc(result) {
    if (!result?.events?.length) return;
    const n = result.events.length;
    this._relocEvents = result.events;
    // Snapshot of how many events were in the catalog when this run started —
    // _autoRelocCheck compares against this to know a re-run is actually needed.
    this._relocEventCountAtLastRun = (this._events || []).length;
    this._showingReloc = true;
    this._plotEventsOnMap(result.events);
    this._updateRelocToggleBtn();
    const badge = document.getElementById('om-hypodd-status');
    if (badge) { badge.style.display = ''; badge.textContent = `✓ ${n} terelokasi`; }
    console.log(`[OnlineHypoDD] ${n} events shown on the map (global IASP91)`);
  }

  // Called every 10 min (see _relocAutoTimer). Only keeps a relocation that the
  // operator has already requested at least once (state 'done'/'error') up to
  // date — never auto-starts HypoDD for a session that never asked for it, and
  // never overlaps a run already in progress (mirrors the backend's own guard).
  async _autoRelocCheck() {
    let status;
    try {
      status = (await (await fetch('/api/online/realtime/reloc')).json()).status;
    } catch (_) { return; }
    if (!status || (status.state !== 'done' && status.state !== 'error')) return;
    const curN = (this._events || []).length;
    if (curN <= (this._relocEventCountAtLastRun || 0)) return; // nothing new since last run
    try {
      const r = await fetch('/api/online/realtime/reloc', { method: 'POST' });
      const d = await r.json();
      if (r.ok && d.ok) {
        console.log('[OnlineHypoDD] Auto re-relocation started (new events since last run)');
        this._pollOnlineHypoDD();
      }
    } catch (_) { /* transient — next tick will retry */ }
  }

  toggleRelocView() {
    if (!this._relocEvents?.length) return;
    this._showingReloc = !this._showingReloc;
    this._plotEventsOnMap(this._showingReloc ? this._relocEvents : (this._events || []));
    this._updateRelocToggleBtn();
  }

  _updateRelocToggleBtn() {
    const btn = document.getElementById('om-btn-reloc-toggle');
    if (!btn) return;
    if (this._relocEvents?.length) {
      btn.style.display = '';
      btn.textContent = this._showingReloc
        ? `📍 Relokasi (${this._relocEvents.length})`
        : `🔵 Asli`;
      btn.title = this._showingReloc
        ? 'Showing: HypoDD position — click to revert to the original position'
        : 'Showing: original position — click to see the HypoDD relocation result';
    } else {
      btn.style.display = 'none';
    }
  }

  async _loadRelocIfAvailable() {
    try {
      const d = await (await fetch('/api/online/realtime/reloc')).json();
      if (d.status?.state === 'done' && d.result?.events?.length) {
        this._relocEvents = d.result.events;
        this._showingReloc = false;   // show originals first; toggle available
        this._updateRelocToggleBtn();
        const badge = document.getElementById('om-hypodd-status');
        if (badge) {
          badge.style.display = '';
          badge.textContent = `✓ ${d.result.events.length} relocations available`;
        }
      } else if (d.status?.state === 'running') {
        const btn = document.getElementById('om-btn-hypodd');
        if (btn) btn.disabled = true;
        const badge = document.getElementById('om-hypodd-status');
        if (badge) { badge.style.display = ''; badge.textContent = 'Running…'; }
        this._pollOnlineHypoDD();
      }
    } catch (_) {}
  }

  /* ── Helpers ──────────────────────────────────────────────────────────────── */
  // Never render the real SeedLink server IP:port on screen (status bar is
  // visible in screenshots/shared views) — a small plug icon shows connected
  // state instead, without exposing where to.
  _addrBadge(connected) {
    return connected
      ? '<i class="bi bi-hdd-network-fill" style="color:#27ae60" title="SeedLink connected"></i>'
      : '<i class="bi bi-hdd-network" style="color:#5a5a5a" title="SeedLink disconnected"></i>';
  }
  _setStatus(color, text) {
    const dot  = document.getElementById('om-status-dot');
    const span = document.getElementById('om-status-text');
    if (dot)  dot.className = 'om-dot om-dot-' + color;
    if (span) span.textContent = text;
  }
}

/* ── Helper: seismic station triangle icon ────────────────────────────────── */
function _stationIcon(fillColor = '#3b82f6', size = 14, blink = false) {
  const w = Math.round(size * 1.15);
  const h = size;
  return L.divIcon({
    className: blink ? 'om-sta-blink' : '',
    html: `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">
      <polygon points="${w/2},1 ${w-1},${h-1} 1,${h-1}"
               fill="${fillColor}" stroke="#0f2744" stroke-width="1.5" stroke-linejoin="round"/>
    </svg>`,
    iconSize:   [w, h],
    iconAnchor: [w / 2, h],     // anchor di ujung bawah segitiga
    tooltipAnchor: [0, -h],
  });
}

const OM = new OnlineMonitorModule(SW);

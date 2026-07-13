class BootstrapUiModule {
  constructor(core) {
    this.core = core;
    // --- Model ---
    this.splashPct = 0;
  }

  // --- View (splash progress bar) ---
  splashSet(txt) { const e = document.getElementById('sw-splash-status'); if (e) e.textContent = txt; }
  // Set progress to a target % (animates upward, never backwards) with a label.
  splashProgress(pct, label) {
    this.splashPct = Math.max(this.splashPct, Math.min(100, Math.round(pct)));
    const bar = document.getElementById('sw-splash-bar');
    const num = document.getElementById('sw-splash-pct');
    if (bar) bar.style.width = this.splashPct + '%';
    if (num) num.textContent = this.splashPct + '%';
    if (label) this.splashSet(label);
  }
  splashStep(txt) {
    const e = document.getElementById('sw-splash-steps'); if (!e) return;
    e.innerHTML += (e.innerHTML ? ' · ' : '') + '<span style="color:#5cdb7a">✓</span> ' + this.core.esc(txt);
  }
  splashDone() {
    this.splashProgress(100, 'Ready.');
    const s = document.getElementById('sw-splash'); if (!s) return;
    setTimeout(() => {
      s.style.opacity = '0';
      setTimeout(() => { if (s.parentNode) s.parentNode.removeChild(s); }, 380);
    }, 220);
  }

  // --- View (federation connection badge) ---
  updateConnBadge() {
    const b = document.getElementById('sw-conn-badge');
    if (!b) return;
    if (this.core.REMOTE.base) {
      let host = this.core.REMOTE.base.replace(/^https?:\/\//, '');
      b.style.background = '#c0791f';
      b.textContent = '● ' + host;
      b.title = 'Connected to remote server: ' + this.core.REMOTE.base + '  —  click to return to Local mode';
      b.style.cursor = 'pointer';
      b.onclick = () => { if (confirm('Stop using the remote server and return to Local mode?')) this.backToLocal(); };
    } else {
      b.style.background = '#3a7d44';
      b.textContent = '● Local';
      b.title = 'Local mode (standalone)';
      b.style.cursor = 'default';
      b.onclick = null;
    }
  }

  // --- Controller (federation panel, called from onclick= in index.html) ---
  toggleServerPanel() {
    const p = document.getElementById('sw-server-panel');
    if (!p) return;
    const show = p.style.display === 'none' || !p.style.display;
    p.style.display = show ? 'block' : 'none';
    if (show) {
      document.getElementById('sw-srv-url').value   = this.core.REMOTE.base;
      document.getElementById('sw-srv-token').value = this.core.REMOTE.token;
      this.loadServerInfo();
    }
  }

  // Show THIS server's connection info (address + server_id + token) so the
  // operator can hand it to clients. The token value is only returned by the
  // server to a localhost / already-authenticated caller.
  async loadServerInfo() {
    const el = document.getElementById('sw-srvinfo');
    if (!el) return;
    el.innerHTML = '<div style="color:#8a93a6">Loading…</div>';
    try {
      // Always query the LOCAL server for its own identity (full URL bypasses the
      // remote-routing fetch patch). Abort after 6s so it never hangs on "Loading…".
      const ac = new AbortController();
      const tmr = setTimeout(() => ac.abort(), 6000);
      const opts = { cache: 'no-store', signal: ac.signal };
      if (this.core.REMOTE.token) opts.headers = { Authorization: 'Bearer ' + this.core.REMOTE.token };
      const r = await fetch(location.origin + '/api/server-info', opts);
      clearTimeout(tmr);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const urls = (d.urls && d.urls.length ? d.urls : [location.origin]);
      const urlRows = urls.map(u =>
        `<div style="display:flex;align-items:center;gap:.3rem">
           <code style="color:#7dd3fc">${this.core.esc(u)}</code>
           <i class="bi bi-clipboard" style="cursor:pointer;color:#8a93a6" title="Copy URL" onclick="BOOT.copyText('${this.core.esc(u)}')"></i>
         </div>`).join('');
      let tokRow;
      if (d.auth_required && d.token) {
        tokRow = `<div style="display:flex;align-items:center;gap:.3rem;margin-top:.25rem">
               <span style="color:#8a93a6">Token:</span>
               <code id="sw-srvtok" style="color:#fbbf24;word-break:break-all">${this.core.esc(d.token)}</code>
               <i class="bi bi-clipboard" style="cursor:pointer;color:#8a93a6" title="Copy token" onclick="BOOT.copyText('${this.core.esc(d.token)}')"></i>
             </div>`;
      } else if (d.auth_required) {
        tokRow = `<div style="margin-top:.25rem;color:#8a93a6">Token: <span style="color:#fbbf24">active</span>
               <span style="color:#6b7280">(open on the server machine to see the value)</span></div>`;
      } else {
        tokRow = `<div style="margin-top:.25rem;color:#8a93a6">Auth: <span style="color:#5cdb7a">open (no token)</span></div>`;
      }
      el.innerHTML = urlRows + tokRow +
        `<div style="margin-top:.25rem;color:#6b7280">server_id ${this.core.esc((d.server_id || '').slice(0, 12))}… · ${this.core.esc(d.hostname || '')}</div>`;
    } catch (e) {
      el.innerHTML = `<span style="color:#e0a">Failed to load server info: ${this.core.esc(e.name === 'AbortError' ? 'timeout' : e.message)}</span>`;
    }
  }

  // Set or generate this server's bearer token at runtime (operator on the server,
  // or already-authenticated). Enables auth so clients can connect with a token.
  async setServerToken(generate) {
    const inp = document.getElementById('sw-srv-settok');
    let tok = generate ? '' : ((inp && inp.value) || '').trim();
    try {
      const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' },
                     body: JSON.stringify({ token: tok, generate: !!generate }) };
      if (this.core.REMOTE.token) opts.headers['Authorization'] = 'Bearer ' + this.core.REMOTE.token;
      const d = await (await fetch(location.origin + '/api/server-info/token', opts)).json();
      if (d.error) { alert(d.error); return; }
      // Keep this GUI working against the now-protected local server.
      this.core.REMOTE.token = d.token || '';
      if (d.token) localStorage.setItem('sw_remote_token', d.token);
      else localStorage.removeItem('sw_remote_token');
      if (inp) inp.value = '';
      this.loadServerInfo();
      this.core.showAlert('save', d.token ? 'Token set' : 'Token removed', 'ok');
    } catch (e) { alert('Failed to set token: ' + e.message); }
  }

  copyText(t) {
    if (navigator.clipboard) navigator.clipboard.writeText(t)
      .then(() => this.core.showAlert('save', 'Copied', 'ok')).catch(() => {});
  }

  async connectServer() {
    const url   = (document.getElementById('sw-srv-url').value || '').trim();
    const token = (document.getElementById('sw-srv-token').value || '').trim();
    const status = document.getElementById('sw-srv-status');
    if (!url) { status.innerHTML = '<span style="color:#e0a">Enter Server URL.</span>'; return; }
    status.textContent = 'Connecting…';
    try {
      // Health check via server-side proxy — browser doesn't need to reach the remote directly.
      const r = await fetch('/api/remote/connect', {
        method : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body   : JSON.stringify({ url, token }),
      });
      const h = await r.json();
      if (!r.ok || h.error) throw new Error(h.error || 'connection failed');
      this.core.REMOTE.base = url; this.core.REMOTE.token = token;
      localStorage.setItem('sw_remote_base', url);
      localStorage.setItem('sw_remote_token', token);
      this.updateConnBadge();
      status.innerHTML = `<span style="color:#5cdb7a">Connected — ${this.core.esc(h.hostname || '')} `
        + `v${this.core.esc(h.version || '')}<br>server_id ${this.core.esc((h.server_id||'').slice(0,12))}…</span>`;
      if (typeof loadConfigList === 'function') { try { loadConfigList(); } catch (e) {} }
    } catch (e) {
      status.innerHTML = `<span style="color:#e0a">Failed: ${this.core.esc(e.message)}</span>`;
    }
  }

  disconnectServer() {
    this.core.REMOTE.base = ''; this.core.REMOTE.token = '';
    localStorage.removeItem('sw_remote_base');
    localStorage.removeItem('sw_remote_token');
    fetch('/api/remote/disconnect', { method: 'POST' }).catch(() => {});
    this.updateConnBadge();
    const status = document.getElementById('sw-srv-status');
    if (status) status.innerHTML = '<span style="color:#8a93a6">Local mode.</span>';
    if (typeof loadConfigList === 'function') { try { loadConfigList(); } catch (e) {} }
  }

  // User intervention: stop using the remote (whether it's live or unreachable/offline)
  // and switch fully to Local mode. Clears remote + offline state, dismisses the
  // offline banner, and reloads the local config/job lists. Wired to the offline
  // banner's "Work Local" button and the connection badge (click when in remote mode).
  backToLocal() {
    // Clear the remote both client-side (stops the fetch patch rerouting /api/* →
    // /api/remote/*) and server-side (drops _PROXY_CFG so the proxy stops targeting
    // the old host). Persist the cleared state BEFORE reloading.
    this.core.REMOTE.base = ''; this.core.REMOTE.token = '';
    localStorage.removeItem('sw_remote_base');
    localStorage.removeItem('sw_remote_token');
    this.core.OFFLINE.active = false;
    this.core.OFFLINE.data   = null;
    // A plain state clear leaves already-opened remote-routed connections (the
    // waveform SSE EventSource, in-flight polls) hanging — that is the "still stuck
    // on remote" symptom (high ping, slow uploads even after disconnect). A full
    // reload tears every one of them down and reboots cleanly in Local mode.
    const done = () => location.reload();
    fetch('/api/remote/disconnect', { method: 'POST' }).then(done, done);
    setTimeout(done, 800);   // fallback if the disconnect request hangs
  }

  // Operator-triggered restart of the LOCAL server (backend does os.execv → same
  // port). Recovers a wedged server without SSH. Targets location.origin so it never
  // restarts a connected remote. Polls /api/health until the server is back, reloads.
  async restartServer() {
    if (!confirm('Restart the local SeisWork server now?\nThe page reloads automatically when it is back.')) return;
    const call = (force) => fetch(location.origin + '/api/server/restart', {
      method : 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' },
                             this.core.REMOTE.token ? { Authorization: 'Bearer ' + this.core.REMOTE.token } : {}),
      body   : JSON.stringify({ force: !!force }),
    });
    try {
      let r = await call(false);
      if (r.status === 409) {   // a job is running
        if (!confirm('A job is currently running. Restart anyway? This interrupts it.')) return;
        r = await call(true);
      }
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || ('HTTP ' + r.status)); }
      this._waitServerBackAndReload();
    } catch (e) {
      this.core.showAlert('save', 'Restart failed: ' + e.message, 'err');
    }
  }

  _waitServerBackAndReload() {
    let ov = document.getElementById('sw-restart-overlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'sw-restart-overlay';
      ov.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;'
        + 'justify-content:center;flex-direction:column;gap:.8rem;background:rgba(10,14,20,.92);'
        + 'color:#e5e7eb;font-size:.9rem';
      document.body.appendChild(ov);
    }
    ov.innerHTML = '<div style="width:34px;height:34px;border:3px solid rgba(255,255,255,.15);'
      + 'border-top-color:#22c55e;border-radius:50%;animation:spin .7s linear infinite"></div>'
      + '<div>Restarting SeisWork server…</div>';
    let tries = 0;
    const poll = async () => {
      tries++;
      try {
        const r = await fetch(location.origin + '/api/health', { cache: 'no-store' });
        if (r.ok) { location.reload(); return; }
      } catch (e) { /* server still down — keep polling */ }
      if (tries > 60) {
        ov.innerHTML = '<div>Server did not come back after ~60s — reload the page manually.</div>';
        return;
      }
      setTimeout(poll, 1000);
    };
    setTimeout(poll, 1800);   // let the old process drop before polling
  }

  // --- Controller (offline mode — banner shown when remote is set but unreachable) ---
  showOfflineBanner(od) {
    let b = document.getElementById('sw-offline-banner');
    if (!b) {
      b = document.createElement('div');
      b.id = 'sw-offline-banner';
      b.className = 'sw-offline-banner';
      const nav = document.querySelector('.sw-navbar');
      if (nav) nav.insertAdjacentElement('afterend', b);
      else document.body.prepend(b);
    }
    const n = od.configs.reduce((s, c) => s + c.n_jobs, 0);
    b.innerHTML = `<i class="bi bi-wifi-off"></i>
      <span>Offline Mode &mdash; reading <b>${n} job</b> from local data
        <span style="font-family:monospace;font-size:.65rem;opacity:.65;margin-left:.3rem">${this.core.esc(od.sync_dir)}</span>
      </span>
      <button onclick="BOOT.retryConnect()" id="sw-offline-retry" class="btn-sw btn-ghost-sw btn-sm-sw" style="margin-left:.5rem;color:#fbbf24;border-color:rgba(251,191,36,.3)">
        <i class="bi bi-arrow-clockwise"></i> Retry Connection
      </button>
      <button onclick="BOOT.backToLocal()" id="sw-offline-golocal" class="btn-sw btn-ghost-sw btn-sm-sw" style="margin-left:.35rem" title="Stop using the remote and work with local data only">
        <i class="bi bi-hdd"></i> Work Local
      </button>`;
    b.classList.add('active');
  }

  async retryConnect() {
    if (!this.core.REMOTE.base) return;
    const btn = document.getElementById('sw-offline-retry');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-arrow-repeat" style="display:inline-block;animation:spin .6s linear infinite"></i> Connecting…'; }
    try {
      const r = await fetch('/api/remote/connect', {
        method : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body   : JSON.stringify({ url: this.core.REMOTE.base, token: this.core.REMOTE.token }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      this.core.OFFLINE.active = false;
      this.core.OFFLINE.data   = null;
      const b = document.getElementById('sw-offline-banner');
      if (b) b.classList.remove('active');
      await loadConfigList();
      await refreshJobs();
      this.core.showAlert('save', 'Reconnected to remote server', 'ok');
    } catch (e) {
      this.core.showAlert('save', `Connection failed: ${e.message}`, 'err');
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Retry Connection'; }
    }
  }

  renderOfflineConfigList(od) {
    const el = document.getElementById('config-list');
    document.getElementById('cfg-count-badge').textContent = od.configs.length;
    if (!od.configs.length) {
      el.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i>No local data stored</div>';
      return;
    }
    const _stepIcon = {
      picking: 'bi-activity', pick: 'bi-activity',
      assoc: 'bi-diagram-3', locate: 'bi-geo-alt', magnitude: 'bi-lightning-charge',
      velocity: 'bi-speedometer2', relocation: 'bi-arrows-expand',
    };
    const _stateColor = { done: '#4ade80', error: '#f87171', running: '#fbbf24', stopped: '#8a93a6' };
    el.innerHTML = od.configs.map(c => {
      const jobRows = c.jobs.map(j => {
        const icon  = _stepIcon[j.step]  || 'bi-gear';
        const color = _stateColor[j.state] || '#8a93a6';
        const info  = j.kind === 'picking'
          ? (j.picks  ? `${j.picks.toLocaleString()} picks` : '')
          : (j.events ? `${j.events.toLocaleString()} events` : '');
        const csvFile = j.files?.find(f => f.endsWith('.csv'));
        return `<div class="cfg-offline-job">
          <i class="bi ${icon}" style="color:#8a93a6;font-size:.68rem;flex-shrink:0"></i>
          <span style="font-size:.67rem;color:#c9d1d9;flex-shrink:0">${this.core.esc(j.step)}/${this.core.esc(j.method)}</span>
          ${info ? `<span style="font-size:.62rem;color:var(--text-muted)">${this.core.esc(info)}</span>` : ''}
          <span style="margin-left:auto;font-size:.6rem;color:${color};flex-shrink:0">${this.core.esc(j.state)}</span>
          ${csvFile ? `<a href="/api/local/result/${this.core.esc(j.id)}/${this.core.esc(csvFile)}" download title="Download ${csvFile}" style="color:#22d3ee;font-size:.68rem;flex-shrink:0"><i class="bi bi-download"></i></a>` : ''}
        </div>`;
      }).join('');
      return `<div class="cfg-item" id="cfgitem-${c.id}" style="flex-direction:column;align-items:stretch;gap:.25rem">
        <div style="display:flex;align-items:center;gap:.45rem">
          <span class="cfg-id" style="color:#fbbf24;background:rgba(251,191,36,.1)">${this.core.esc(c.id)}</span>
          <div class="cfg-info">
            <div class="cfg-name">${this.core.esc(c.name)}</div>
            <div class="cfg-meta">${c.n_jobs} jobs stored locally</div>
          </div>
          <div class="cfg-actions">
            <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="BOOT.offlineOpenWork('${c.id}')" title="View results in Work Page" style="color:#a78bfa"><i class="bi bi-grid-1x2"></i></button>
            <span style="font-size:.6rem;color:#f97316;border:1px solid rgba(249,115,22,.4);border-radius:3px;padding:0 4px;flex-shrink:0">[offline]</span>
          </div>
        </div>
        ${jobRows ? `<div class="cfg-offline-jobs">${jobRows}</div>` : ''}
      </div>`;
    }).join('');
  }

  async offlineOpenWork(cfgId) {
    // In offline mode: set this.core.activeConfigId directly (no remote config metadata),
    // then open the work page so Result Viewer can load from local sync data.
    this.core.activeConfigId = cfgId;
    this.core.activeProjectName = cfgId;
    _updateProjectBadge();
    document.querySelectorAll('.cfg-item').forEach(e => e.classList.remove('active-cfg'));
    document.getElementById(`cfgitem-${cfgId}`)?.classList.add('active-cfg');
    try { await openWorkPage(cfgId); } catch { }
  }

  async syncOfflineCfg(cfgId) {
    const btn  = document.getElementById(`syncbtn-${cfgId}`);
    const bar  = document.getElementById(`syncbar-${cfgId}`);
    const fill = document.getElementById(`syncfill-${cfgId}`);

    const _spin = `<i class="bi bi-arrow-repeat" style="display:inline-block;animation:spin .6s linear infinite"></i>`;
    const setBtn = (html, color) => { if (!btn) return; btn.innerHTML = html; if (color) btn.style.color = color; };
    const setPct = pct => {
      if (bar)  bar.classList.add('active');
      if (fill) fill.style.width = Math.min(pct, 100) + '%';
    };

    if (btn) btn.disabled = true;
    setBtn(_spin, '#22d3ee');
    setPct(0);

    try {
      const r = await fetch('/api/local/sync/pull', {
        method : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body   : JSON.stringify({ cfg_id: cfgId }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || r.statusText); }

      const reader  = r.body.getReader();
      const decoder = new TextDecoder();
      let   buf     = '';
      let   doneEv  = null;

      outer: while (true) {
        const { done, value } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let ev;
          try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === 'error') throw new Error(ev.error);
          if (ev.type === 'start') {
            setBtn(`${_spin}<span style="font-size:.58rem;margin-left:2px">0%</span>`, '#22d3ee');
          } else if (ev.type === 'progress') {
            setBtn(`${_spin}<span style="font-size:.58rem;margin-left:2px">${ev.pct}%</span>`, '#22d3ee');
            setPct(ev.pct);
          } else if (ev.type === 'done') {
            doneEv = ev;
          }
        }
        if (done) break;
      }

      if (!doneEv) throw new Error('stream ended without a done event');
      setPct(100);
      const msg = `Sync complete: ${doneEv.pulled} downloaded, ${doneEv.skipped} already up to date`
                + (doneEv.errors ? `, ${doneEv.errors} failed` : '');
      setBtn('<i class="bi bi-cloud-check"></i>', '#4ade80');
      if (btn) btn.title = msg;
      this.core.showAlert('save', msg + (doneEv.sync_dir ? ` → ${doneEv.sync_dir}` : ''), 'ok');
      setTimeout(() => { if (bar) { bar.classList.remove('active'); if (fill) fill.style.width = '0%'; } }, 2500);

    } catch (e) {
      setBtn('<i class="bi bi-cloud-slash"></i>', '#f87171');
      if (bar) bar.classList.remove('active');
      this.core.showAlert('save', `Sync failed: ${e.message}`, 'err');
    } finally {
      if (btn) btn.disabled = false;
    }
  }
}

const BOOT = new BootstrapUiModule(SW);
document.addEventListener('DOMContentLoaded', () => BOOT.updateConnBadge());
setTimeout(() => BOOT.splashDone(), 15000);

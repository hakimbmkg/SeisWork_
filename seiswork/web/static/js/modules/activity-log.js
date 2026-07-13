// SeisWork — Global Activity Log (navbar) — by HakimBMKG
// One place where every success/error across the app becomes visible, so the
// user can identify what SeisWork just did (or failed to do) without hunting
// through per-panel alert banners and terminals. Three automatic feeds:
//   1. SW.showAlert(...)      — every inline panel alert also lands here
//   2. fetch('/api/…')        — failed calls always; successful mutations (POST/
//                               PUT/DELETE) too, minus known high-frequency polls
//   3. window error events    — uncaught JS errors / promise rejections
// UI: a live ticker in the navbar (latest entry, color-coded) + a dropdown with
// the recent history and an unread-error badge. Ring buffer only (no storage).
class ActivityLogModule {
  constructor(core) {
    this.core = core;
    this.entries = [];          // [{t: Date, type: 'ok'|'err'|'warn', src, msg}]
    this.MAX = 200;
    this.unreadErr = 0;
    this.open = false;
    // POST endpoints that fire on a timer — logging them would drown the feed.
    this.POLL_SKIP = [
      '/api/online/waveform/delta',
    ];
    this._hookShowAlert();
    this._hookFetch();
    this._hookErrors();
    document.addEventListener('click', (e) => {
      if (this.open && !e.target.closest('#sw-actlog-wrap')) this.toggle(false);
    });
  }

  // --- Model ---
  add(type, src, msg) {
    msg = String(msg ?? '').trim();
    if (!msg) return;
    const last = this.entries[0];
    // Collapse immediate repeats (retry loops, repeated polls) into one entry
    if (last && last.type === type && last.src === src && last.msg === msg) {
      last.n = (last.n || 1) + 1;
      last.t = new Date();
    } else {
      this.entries.unshift({ t: new Date(), type, src, msg, n: 1 });
      if (this.entries.length > this.MAX) this.entries.pop();
      if (type === 'err' && !this.open) this.unreadErr++;
    }
    this.renderTicker();
    if (this.open) this.renderList();
  }
  ok(src, msg)  { this.add('ok',  src, msg); }
  err(src, msg) { this.add('err', src, msg); }

  clear() {
    this.entries = [];
    this.unreadErr = 0;
    this.renderTicker();
    this.renderList();
  }

  // --- Feeds ---
  _hookShowAlert() {
    const orig = this.core.showAlert.bind(this.core);
    this.core.showAlert = (prefix, msg, type) => {
      orig(prefix, msg, type);
      if (msg) this.add(type === 'err' ? 'err' : 'ok', prefix, msg);
    };
  }

  _hookFetch() {
    const origFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = ((init && init.method) || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
      // Only same-origin API calls; absolute URLs (remote upstreams) stay out.
      const isApi = !url.includes('://') && url.includes('/api/');
      const path = url.split('?')[0];
      const skip = !isApi || this.POLL_SKIP.some(p => path.startsWith(p));
      try {
        const res = await origFetch(input, init);
        if (!skip) {
          if (!res.ok) {
            // Pull the API's own error message when the body is JSON (clone so
            // the caller can still read the body normally).
            let detail = '';
            try {
              const d = await res.clone().json();
              detail = d && (d.error || d.message) ? `: ${d.error || d.message}` : '';
            } catch (_) { /* non-JSON body */ }
            this.add('err', 'api', `${method} ${path} → HTTP ${res.status}${detail}`);
          } else if (method !== 'GET') {
            this.add('ok', 'api', `${method} ${path} → OK`);
          }
        }
        return res;
      } catch (e) {
        // Aborted fetches are deliberate cancellations (poll timeout via
        // AbortController, request superseded on tab switch) — "signal is
        // aborted without reason" is routine plumbing, not a failure worth
        // showing the user.
        if (!skip && !this._isAbort(e)) this.add('err', 'api', `${method} ${path} → ${e.message}`);
        throw e;
      }
    };
  }

  _isAbort(e) {
    return !!e && (e.name === 'AbortError' || /\babort/i.test(e.message || ''));
  }

  _hookErrors() {
    window.addEventListener('error', (e) => {
      if (e.message) this.add('err', 'js', e.message);
    });
    window.addEventListener('unhandledrejection', (e) => {
      const r = e.reason;
      if (this._isAbort(r)) return;   // cancelled fetch bubbling up — not an error
      this.add('err', 'js', (r && (r.message || r.toString())) || 'unhandled rejection');
    });
  }

  // --- View ---
  _fmtT(d) {
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  renderTicker() {
    const tick = document.getElementById('sw-actlog-ticker');
    const badge = document.getElementById('sw-actlog-badge');
    if (badge) {
      badge.style.display = this.unreadErr > 0 ? 'inline-flex' : 'none';
      badge.textContent = this.unreadErr > 99 ? '99+' : String(this.unreadErr);
    }
    if (!tick) return;
    const e = this.entries[0];
    if (!e) { tick.innerHTML = '<span class="sw-actlog-empty">activity log</span>'; return; }
    const cls = e.type === 'err' ? 'al-err' : 'al-ok';
    tick.innerHTML =
      `<span class="sw-actlog-dot ${cls}"></span>` +
      `<span class="sw-actlog-msg" title="${this.core.esc(e.msg)}">${this.core.esc(e.msg)}</span>`;
  }

  renderList() {
    const list = document.getElementById('sw-actlog-list');
    if (!list) return;
    if (!this.entries.length) {
      list.innerHTML = '<div class="sw-actlog-none">No activity yet — successes and errors from every panel and API call will appear here.</div>';
      return;
    }
    list.innerHTML = this.entries.map(e => {
      const cls = e.type === 'err' ? 'al-err' : 'al-ok';
      const icon = e.type === 'err' ? 'bi-x-circle-fill' : 'bi-check-circle-fill';
      const times = e.n > 1 ? ` <span class="sw-actlog-n">×${e.n}</span>` : '';
      return `<div class="sw-actlog-row">
        <i class="bi ${icon} sw-actlog-ic ${cls}"></i>
        <span class="sw-actlog-time">${this._fmtT(e.t)}</span>
        <span class="sw-actlog-src">${this.core.esc(e.src)}</span>
        <span class="sw-actlog-txt">${this.core.esc(e.msg)}${times}</span>
      </div>`;
    }).join('');
  }

  toggle(force) {
    this.open = force !== undefined ? force : !this.open;
    const panel = document.getElementById('sw-actlog-panel');
    if (panel) panel.classList.toggle('open', this.open);
    if (this.open) {
      this.unreadErr = 0;
      this.renderTicker();
      this.renderList();
    }
  }
}

const SWLOG = new ActivityLogModule(SW);

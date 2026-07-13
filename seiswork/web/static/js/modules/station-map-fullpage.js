/**
 * station-map-fullpage.js — by HakimBMKG
 * Dedicated full-page zoom view for the live Station Map — mirrors the
 * dashboard's Station Map panel (online-monitor.js _initMap/_blinkStation)
 * but self-contained, same pattern as waveform-fullpage.js. Never calls
 * /api/online/connect — only reads the already-running live session.
 */
(() => {
  // Which config this tab is scoped to — passed by the dashboard as
  // ?cfg_id= when it opens this page (window.open(...)). Missing (opened
  // directly, or an older link) → every fetch below omits cfg_id and the
  // server falls back to whichever config is currently active, same as
  // before this page knew about cfg_id.
  const _cfgId = new URLSearchParams(location.search).get('cfg_id') || null;
  const _cfgParam = _cfgId ? `cfg_id=${encodeURIComponent(_cfgId)}` : '';
  let   _region = null;   // this config's monitoring-area box, fetched once (below)

  const markers      = {};   // 'NET.STA' → L.marker
  const blinkTimers   = {};   // 'NET.STA' → setTimeout id
  const seenEventIds = new Set();
  const stationsByKey = {};  // 'NET.STA' → {net,sta,lat,lon,elev,name,channels,default_channel}
  const pickStore     = new Map();   // 'NET.STA' → [{t,phase,source}] (last 30 min)
  let   activeStreams = [];
  let   map           = null;
  let   eventLayer    = null;
  // ── Realtime waveform modal state ──
  let   modalNet = null, modalSta = null, modalTimer = null;

  // Fault style keys ('Sesar ...') come straight from the source GeoJSON's own
  // `tipe` property values — left as-is (not translated), same convention as
  // seiswork.js/catalog_map.html: it must match the data, not the UI language.
  const FAULT_STYLE = {
    'Sesar Geser'        : { color: '#f59e0b', weight: 0.7, opacity: 1 },
    'Sesar Geser Symbol' : { color: '#f59e0b', weight: 0.5, opacity: .9, dashArray: '6,4' },
    'Sesar Naik'         : { color: '#ef4444', weight: 0.7, opacity: 1 },
    'Sesar Turun'        : { color: '#60a5fa', weight: 0.7, opacity: 1 },
    'Sesar Turun Sysmbol': { color: '#60a5fa', weight: 0.5, opacity: .9, dashArray: '6,4' },
    'Sesar Naik Sysmbol' : { color: '#ef4444', weight: 0.5, opacity: .9, dashArray: '6,4' },
  };
  const FAULT_DEFAULT = { color: '#a78bfa', weight: 0.7, opacity: 1 };

  function stationIcon(fillColor = '#3b82f6', size = 14, blink = false) {
    const w = Math.round(size * 1.15);
    const h = size;
    return L.divIcon({
      className: blink ? 'sm-blink' : '',
      html: `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">
        <polygon points="${w/2},1 ${w-1},${h-1} 1,${h-1}"
                 fill="${fillColor}" stroke="#0f2744" stroke-width="1.5" stroke-linejoin="round"/>
      </svg>`,
      iconSize: [w, h], iconAnchor: [w / 2, h], tooltipAnchor: [0, -h],
    });
  }

  function blinkStation(net, sta, color = '#facc15', durationMs = 4000) {
    const key = `${net}.${sta}`;
    const marker = markers[key];
    if (!marker) return;
    marker.setIcon(stationIcon(color, 15, true));
    clearTimeout(blinkTimers[key]);
    blinkTimers[key] = setTimeout(() => {
      const mk = markers[key];
      if (!mk) return;
      const isActive = activeStreams.some(k => k === key);
      mk.setIcon(stationIcon(isActive ? '#22c55e' : '#6b7280', 14));
    }, durationMs);
  }

  function initMap() {
    map = L.map('sm-map', { zoomControl: false, preferCanvas: false });
    map.fitBounds([[-11, 94], [6, 141]]);   // Indonesia default, until stations load and re-fit
    L.control.zoom({ position: 'topright' }).addTo(map);
    // Panes — z-index identical to seiswork.js buildOnlineMap / catalog_map.html
    if (!map.getPane('geojsonPane'))   { const p = map.createPane('geojsonPane');   p.style.zIndex = 450; }
    if (!map.getPane('stationPane'))   { const p = map.createPane('stationPane');   p.style.zIndex = 460; }
    if (!map.getPane('eventPane'))     { const p = map.createPane('eventPane');     p.style.zIndex = 470; }

    const baseLayers = {
      dark : L.tileLayer('/tiles/carto_dark/{z}/{x}/{y}.png',
               { attribution: '&copy; CartoDB', maxZoom: 19, subdomains: ['a', 'b', 'c', 'd'] }),
      bathy: L.tileLayer('/tiles/esri_ocean/{z}/{x}/{y}.png',
               { attribution: '&copy; Esri — GEBCO, NOAA, NGDC', maxZoom: 13, maxNativeZoom: 13 }),
      topo : L.tileLayer('/tiles/opentopomap/{z}/{x}/{y}.png',
               { attribution: '&copy; OpenTopoMap (CC-BY-SA)', maxZoom: 17, subdomains: ['a', 'b', 'c'] }),
    };
    // The map itself stays CartoDB Dark (same as the main SeisWork map) — only
    // the surrounding chrome (topbar/legend/layer control) is light/white.
    let activeBase = 'dark';
    baseLayers.dark.addTo(map);
    baseLayers.dark.once('load', () => { const l = document.getElementById('sm-loading'); if (l) l.style.display = 'none'; });
    setTimeout(() => { const l = document.getElementById('sm-loading'); if (l) l.style.display = 'none'; }, 6000);
    eventLayer = L.layerGroup().addTo(map);

    // ── Layer control (basemap + overlays) — same markup/behavior as the main map ──
    const mapEl = document.getElementById('sm-map');
    const ctrlDiv = document.createElement('div');
    ctrlDiv.className = 'layer-control';
    ctrlDiv.innerHTML = `
      <div class="layer-toggle" id="sm-ltbtn" title="Basemap layers"><i class="bi bi-layers"></i></div>
      <div class="layer-panel" id="sm-lpanel">
        <div class="layer-sec-title">Basemap</div>
        <div class="layer-opt" id="sm-opt-bathy">
          <span class="radio-dot"></span><i class="bi bi-water" style="font-size:.78rem;width:14px"></i> Topo + Bathy
        </div>
        <div class="layer-opt" id="sm-opt-topo">
          <span class="radio-dot"></span><i class="bi bi-mountain" style="font-size:.78rem;width:14px"></i> OpenTopoMap
        </div>
        <div class="layer-opt active" id="sm-opt-dark">
          <span class="radio-dot"></span><i class="bi bi-moon-stars" style="font-size:.78rem;width:14px"></i> CartoDB Dark
        </div>
        <div class="layer-sec-title" style="margin-top:.4rem">Overlay</div>
        <div class="layer-opt active" id="sm-opt-fault">
          <span class="chk-box">✓</span><i class="bi bi-slash-lg" style="font-size:.78rem;width:14px"></i> Faults
        </div>
        <div class="layer-opt active" id="sm-opt-volcano">
          <span class="chk-box">✓</span><i class="bi bi-fire" style="font-size:.78rem;width:14px"></i> Volcanoes
        </div>
      </div>`;
    mapEl.appendChild(ctrlDiv);

    function setBase(name) {
      if (name === activeBase) { closeLayerPanel(); return; }
      map.removeLayer(baseLayers[activeBase]);
      baseLayers[name].addTo(map);
      activeBase = name;
      ['bathy', 'topo', 'dark'].forEach(n =>
        document.getElementById(`sm-opt-${n}`)?.classList.toggle('active', n === name));
      closeLayerPanel();
    }
    function closeLayerPanel() {
      document.getElementById('sm-lpanel')?.classList.remove('open');
      document.getElementById('sm-ltbtn')?.classList.remove('open');
    }
    document.getElementById('sm-ltbtn').onclick = () => {
      document.getElementById('sm-lpanel').classList.toggle('open');
      document.getElementById('sm-ltbtn').classList.toggle('open');
    };
    document.getElementById('sm-opt-bathy').onclick = () => setBase('bathy');
    document.getElementById('sm-opt-topo').onclick  = () => setBase('topo');
    document.getElementById('sm-opt-dark').onclick  = () => setBase('dark');
    map.on('click', closeLayerPanel);

    document.getElementById('sm-opt-fault').onclick   = () => toggleOv('fault');
    document.getElementById('sm-opt-volcano').onclick = () => toggleOv('volcano');
    loadOverlays();
  }

  // ── Overlay layers (faults, volcanoes) — same GeoJSON + style as the main map ──
  const ovLayers  = { fault: null, volcano: null };
  const ovVisible = { fault: true, volcano: true };

  async function loadOverlays() {
    try {
      const data = await fetch('/footages/indogigis.geojson').then(r => r.ok ? r.json() : null);
      if (data) {
        ovLayers.fault = L.geoJSON(data, {
          pane : 'geojsonPane',
          style: feat => {
            const s = { ...(FAULT_STYLE[feat.properties?.tipe || ''] || FAULT_DEFAULT) };
            s.pane = 'geojsonPane';
            return s;
          },
          onEachFeature: (feat, lyr) =>
            lyr.bindTooltip(feat.properties?.tipe || 'Fault',
              { className: 'sw-tooltip', sticky: false, direction: 'top' }),
        });
        if (ovVisible.fault) ovLayers.fault.addTo(map);
      }
    } catch (_) { /* transient */ }

    try {
      const data = await fetch('/footages/Volcano.geojson').then(r => r.ok ? r.json() : null);
      if (data) {
        ovLayers.volcano = L.geoJSON(data, {
          pane: 'geojsonPane',
          pointToLayer: (feat, latlng) => {
            const name = (feat.properties?.name || '').split('|')[0].trim();
            const icon = L.divIcon({
              className: '',
              html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 64 64">
                       <circle cx="24" cy="10" r="6"  fill="#ff6600" opacity="0.9"/>
                       <circle cx="32" cy="6"  r="7"  fill="#ff9900" opacity="0.95"/>
                       <circle cx="40" cy="10" r="5.5" fill="#ff6600" opacity="0.9"/>
                       <circle cx="32" cy="13" r="5"  fill="#ffcc00" opacity="0.8"/>
                       <path d="M22 32 Q14 42 10 56 L20 56 Q22 46 26 36Z" fill="#cc2200" opacity="0.75"/>
                       <path d="M42 32 Q50 42 54 56 L44 56 Q42 46 38 36Z" fill="#cc2200" opacity="0.75"/>
                       <polygon points="32,15 4,58 60,58" fill="#8B4513" stroke="#5a2d0c" stroke-width="1.5" stroke-linejoin="round"/>
                       <polygon points="32,16 26,28 38,28" fill="white" opacity="0.45"/>
                       <ellipse cx="32" cy="15" rx="7" ry="2.5" fill="#333" opacity="0.6"/>
                     </svg>`,
              iconSize: [22, 22], iconAnchor: [11, 20],
            });
            return L.marker(latlng, { icon, pane: 'geojsonPane' })
              .bindTooltip(`<b>${name}</b>`, { className: 'sw-tooltip', direction: 'top' });
          },
        });
        if (ovVisible.volcano) ovLayers.volcano.addTo(map);
      }
    } catch (_) { /* transient */ }
  }

  function toggleOv(key) {
    const lyr = ovLayers[key];
    if (!lyr) return;
    ovVisible[key] = !ovVisible[key];
    if (ovVisible[key]) lyr.addTo(map); else map.removeLayer(lyr);
    const el = document.getElementById(`sm-opt-${key}`);
    if (el) {
      el.classList.toggle('active', ovVisible[key]);
      el.querySelector('.chk-box').textContent = ovVisible[key] ? '✓' : '';
    }
  }

  function plotStations(stations) {
    Object.values(markers).forEach(m => map.removeLayer(m));
    for (const key of Object.keys(markers)) delete markers[key];

    stations.forEach(sta => {
      const key = `${sta.net}.${sta.sta}`;
      stationsByKey[key] = sta;
      const marker = L.marker([sta.lat, sta.lon], {
        icon: stationIcon('#6b7280', 14), pane: 'stationPane',
      }).addTo(map);
      marker.bindTooltip(
        `<b>${sta.net}.${sta.sta}</b><br>${sta.name || ''}<br>` +
        `${sta.lat.toFixed(3)}, ${sta.lon.toFixed(3)}<br>Ch: ${sta.default_channel || ''}`,
        { direction: 'top', className: 'sw-tooltip' }
      );
      marker.on('click', () => openStationModal(sta.net, sta.sta));
      markers[key] = marker;
    });

    if (stations.length > 1) {
      map.fitBounds(stations.map(s => [s.lat, s.lon]), { padding: [16, 16], maxZoom: 10 });
    } else if (stations.length === 1) {
      map.setView([stations[0].lat, stations[0].lon], 9);
    }
    document.getElementById('sm-count').textContent = `${stations.length} stations`;
  }

  function updateMarkerColors() {
    const active = new Set(activeStreams);
    Object.entries(markers).forEach(([key, marker]) => {
      marker.setIcon(stationIcon(active.has(key) ? '#22c55e' : '#6b7280', 14));
    });
  }

  function plotEvent(ev) {
    eventLayer.clearLayers();
    if (!ev) return;
    const rad = ev.mag != null ? Math.max(7, 5 + ev.mag * 2.4) : 8;
    const m = L.circleMarker([ev.lat, ev.lon], {
      radius: rad, color: '#fca5a5', weight: 1.5,
      fillColor: '#ef4444', fillOpacity: 0.9, pane: 'eventPane',
    }).addTo(eventLayer);
    m.bindPopup(
      `<b>${ev.mag != null ? 'M' + ev.mag.toFixed(2) : 'M —'}</b> · ${ev.depth_km?.toFixed(1)} km<br>` +
      `${ev.datetime || ''}<br>${ev.lat.toFixed(3)}, ${ev.lon.toFixed(3)}`
    );
  }

  // Fetch this config's monitoring-area box (if it has "Set Area" enabled)
  // ONCE at load — used to bbox-filter the station list below so a
  // region-limited config's full-page map only ever shows ITS box, not
  // every station in the (possibly much wider) inventory file it shares
  // with other configs.
  async function fetchRegion() {
    if (!_cfgId) return;
    try {
      const cfg = await (await fetch(`/api/configs/${encodeURIComponent(_cfgId)}`)).json();
      const reg = cfg?.region;
      if (reg?.limit && [reg.lat_min, reg.lat_max, reg.lon_min, reg.lon_max].every(v => v != null)) {
        _region = reg;
      }
    } catch (_) { /* transient — falls back to the unfiltered inventory */ }
  }

  async function loadStations() {
    try {
      const st = await (await fetch(`/api/online/status${_cfgParam ? '?' + _cfgParam : ''}`)).json();
      document.getElementById('sm-status').textContent =
        st.connected ? `Live · ${st.n_packets} pkt` : (st.error ? `Lost: ${st.error}` : 'No active live session');
      activeStreams = (st.streams || []).map(k => { const p = k.split('.'); return `${p[0]}.${p[1]}`; });
      const invAll = (st.inventory_paths?.length ? st.inventory_paths : [st.inventory_path]).filter(Boolean);
      if (!invAll.length) return;
      let url = `/api/online/stations?inventory=${encodeURIComponent(invAll.join(','))}`;
      if (_region) {
        url += `&lat_min=${_region.lat_min}&lat_max=${_region.lat_max}` +
               `&lon_min=${_region.lon_min}&lon_max=${_region.lon_max}`;
      }
      const r = await (await fetch(url)).json();
      if (!r.stations?.length) return;
      plotStations(r.stations);
      updateMarkerColors();
    } catch (_) { /* transient */ }
  }

  async function pollStatus() {
    try {
      const st = await (await fetch(`/api/online/status${_cfgParam ? '?' + _cfgParam : ''}`)).json();
      document.getElementById('sm-status').textContent =
        st.connected ? `Live · ${st.n_packets} pkt` : (st.error ? `Lost: ${st.error}` : 'No active live session');
      activeStreams = (st.streams || []).map(k => { const p = k.split('.'); return `${p[0]}.${p[1]}`; });
      updateMarkerColors();
    } catch (_) { /* transient */ }
  }

  let pickSince = 0.0;
  async function pollPicks() {
    try {
      const data = await (await fetch(`/api/online/picks/recent?since=${pickSince}&n=100${_cfgParam ? '&' + _cfgParam : ''}`)).json();
      const picks = data.picks || [];
      if (!picks.length) return;
      pickSince = Math.max(...picks.map(p => p.t));
      const cutoff = Date.now() / 1000 - 1800;
      for (const p of picks) {
        const k = `${p.net}.${p.sta}`;
        let arr = pickStore.get(k);
        if (!arr) { arr = []; pickStore.set(k, arr); }
        const dk = `${Number(p.t).toFixed(1)}_${p.phase}`;
        if (!arr.some(x => `${Number(x.t).toFixed(1)}_${x.phase}` === dk))
          arr.push({ t: p.t, phase: p.phase, source: p.source });
        if (arr.length > 200) {
          const f = arr.filter(x => x.t >= cutoff);
          pickStore.set(k, f.length ? f : arr.slice(-50));
        }
      }
      picks.forEach(p => blinkStation(p.net, p.sta, '#facc15'));
    } catch (_) { /* transient */ }
  }

  // ── Realtime waveform modal (opened on station marker click) ──────────────
  // Reuses the shared renderWaveformCanvas/renderTimeAxis (waveform-canvas-render.js,
  // same module the dashboard and the waveform full-page use) so the look is
  // identical everywhere. Fetches only THIS station's channels
  // (/api/online/waveform?station=...) rather than the full session snapshot
  // — light enough to poll every 2s just for the open modal.
  function openStationModal(net, sta) {
    modalNet = net; modalSta = sta;
    clearInterval(modalTimer);
    const known = stationsByKey[`${net}.${sta}`];
    document.getElementById('sm-wv-badge').textContent = net;
    document.getElementById('sm-wv-title').textContent = sta;
    document.getElementById('sm-wv-name').textContent  = known?.name || '';
    document.getElementById('sm-wv-loc').textContent    = known
      ? `${known.lat.toFixed(4)}°, ${known.lon.toFixed(4)}°` : '—';
    document.getElementById('sm-wv-elev').textContent   = known
      ? `${known.elev ?? '?'} m` : '— m';
    document.getElementById('sm-wv-backdrop').classList.remove('hidden');
    document.getElementById('sm-wv-modal').classList.remove('hidden');
    renderStationModal();
    modalTimer = setInterval(renderStationModal, 2000);
  }

  window.closeStationModal = function() {
    clearInterval(modalTimer);
    modalTimer = null;
    document.getElementById('sm-wv-backdrop')?.classList.add('hidden');
    document.getElementById('sm-wv-modal')?.classList.add('hidden');
  };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') window.closeStationModal(); });

  async function renderStationModal() {
    const net = modalNet, sta = modalSta;
    if (!net || !sta) return;
    const known = stationsByKey[`${net}.${sta}`];
    const channels = known?.channels?.length ? known.channels : [known?.default_channel].filter(Boolean);
    const picks = pickStore.get(`${net}.${sta}`) || [];

    const results = await Promise.all(channels.map(cha =>
      fetch(`/api/online/waveform?station=${net}.${sta}..${cha}${_cfgParam ? '&' + _cfgParam : ''}`)
        .then(r => r.json()).catch(() => null)
    ));
    const streams = [];
    channels.forEach((cha, i) => {
      const pts = results[i]?.points || [];
      if (pts.length > 1) {
        streams.push({ key: `${net}.${sta}..${cha}`, net, sta, loc: '', cha, points: pts, picks });
      }
    });
    streams.sort((a, b) => {
      const order = c => c.endsWith('Z') ? 0 : c.endsWith('N') || c.endsWith('1') ? 1
                       : c.endsWith('E') || c.endsWith('2') ? 2 : 3;
      return order(a.cha) - order(b.cha);
    });

    const emptyEl = document.getElementById('sm-wv-empty');
    if (!streams.length) {
      emptyEl.style.display = 'block';
      return;
    }
    emptyEl.style.display = 'none';

    const lastT = streams[0].points[streams[0].points.length - 1]?.t;
    document.getElementById('sm-wv-time').textContent = lastT ? (() => {
      const d = new Date(lastT * 1000); const p = n => String(n).padStart(2, '0');
      return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`;
    })() : '—';
    const nP = picks.filter(p => p.phase === 'P').length;
    const nS = picks.filter(p => p.phase === 'S').length;
    document.getElementById('sm-wv-picks').textContent =
      (nP + nS) ? `${nP} P-pick · ${nS} S-pick in the last 30 minutes` : '';

    const canvas = document.getElementById('sm-wv-canvas');
    const outer  = document.getElementById('sm-wv-plot');
    const nowSec = Date.now() / 1000;
    const r = renderWaveformCanvas(canvas, outer, streams, {}, undefined, false, nowSec);
    renderTimeAxis(document.getElementById('sm-wv-axis'), outer.clientWidth || 600, nowSec);
  }

  async function pollEvents() {
    try {
      // /realtime/catalog reads the PERSISTENT, per-cfg_id disk catalog —
      // /realtime/events instead reflects whichever config's associator
      // happens to be the one process-wide instance currently running,
      // which is not necessarily THIS page's config. Same event shape
      // (newest first, `stations` field included), so this is a drop-in
      // swap; matches the pattern already used by the main dashboard.
      const r = await (await fetch(`/api/online/realtime/catalog?limit=50${_cfgParam ? '&' + _cfgParam : ''}`)).json();
      const events = r.events || [];
      if (!events.length) return;
      plotEvent(events[0]);
      events.forEach(e => {
        if (!e.event_id || seenEventIds.has(e.event_id)) return;
        seenEventIds.add(e.event_id);
        (e.stations || []).forEach(key => {
          const [net, sta] = key.split('.');
          if (net && sta) blinkStation(net, sta, '#facc15');
        });
      });
    } catch (_) { /* transient */ }
  }

  window.refreshStations = loadStations;

  initMap();
  fetchRegion().then(loadStations);
  pollEvents();
  setInterval(pollStatus, 5000);
  setInterval(pollPicks,  2000);
  setInterval(pollEvents, 5000);
  [120, 400, 900].forEach(ms => setTimeout(() => map && map.invalidateSize(false), ms));
})();

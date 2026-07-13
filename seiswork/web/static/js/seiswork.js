/* SeisWork GUI — JavaScript — by HakimBMKG */

// ═══════════════════════════════════════════
//  SeisWorkCore — state & utility bersama lintas-fungsi (Tahap 2 refactor).
//  The other 351 functions in this file REMAIN plain globals; only the shared ones
//  (alert/escape helpers, the map-plotting cluster, progress bar, pipeline
//  registry) moved in here so there are no more loose globals that
//  could collide. Singleton: const SW = new SeisWorkCore();
// ═══════════════════════════════════════════
class SeisWorkCore {
  constructor() {
    // --- Model (Category A: internal state owned by a single utility) ---
    this.plotlyPromise = null;
    this.SM_LAYOUT = {
      margin: { t: 8, b: 28, l: 38, r: 8 },
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: '#94a3b8', size: 10 },
      xaxis: { gridcolor: '#1e293b', zerolinecolor: '#334155' },
      yaxis: { gridcolor: '#1e293b', zerolinecolor: '#334155' },
    };

    // --- Model (Category B: unique state identifiers used across functions) ---
    // Remote-server federation (client mirror): connection persists across page
    // reloads (localStorage) so the GUI stays mirrored without re-entering URL+token.
    this.REMOTE = {
      base : localStorage.getItem('sw_remote_base')  || '',
      token: localStorage.getItem('sw_remote_token') || '',
    };
    // Offline mode: active when remote is set but unreachable.
    this.OFFLINE = { active: false, data: null };

    this.activeConfigId = null;
    this.activeProjectName = '';   // name of the active project (config) for the navbar badge
    this.projectMode = 'offline';  // 'offline' (default, unchanged GUI) | 'online' (Live Monitor, see online-monitor.js)
    this.wpCfgId = null;
    this.mapOverlays = null;   // { faults:[{lons,lats,color}], volcanoes:[{lon,lat,name}], coast:[[[lon,lat]..]] }

    // channel key = `${step}-${method}`, e.g. 'assoc-gamma', 'locate-nlloc'.
    this.pipeActiveJob = {};
    this.pipeViewedJob = {};   // ch → jobId currently displayed in terminal

    // HTML element ID maps (channel → DOM id)
    this.PIPE_TERM = {
      'assoc-gamma'        : 'al-term-assoc-gamma',
      'assoc-real'         : 'al-term-assoc-real',
      'assoc-pyocto'       : 'al-term-assoc-pyocto',
      'assoc-glass3'       : 'al-term-assoc-glass3',
      'locate-nlloc'       : 'al-term-locate-nlloc',
      'locate-locsat'      : 'al-term-locate-locsat',
      'velocity-velest'    : 'pipe-term-velocity',
      'relocation-hypodd'  : 'pipe-term-relocation',
      'imaging-simul2000'  : 'pipe-term-imaging',
      'detect-matchlocate' : 'pipe-term-detect',
      'magnitude-ml-gamma' : 'pipe-term-ml-gamma',
      'magnitude-ml-real'  : 'pipe-term-ml-real',
      'magnitude-ml-nlloc' : 'pipe-term-ml-nlloc',
      'magnitude-ml-locsat': 'pipe-term-ml-locsat',
      'magnitude-ml-hypodd': 'pipe-term-ml-hypodd',
      'mechanism-skhash'   : 'pipe-term-mechanism',
    };
    this.PIPE_JOBS_LIST = {
      'assoc-gamma'        : 'al-jobs-gamma',
      'assoc-real'         : 'al-jobs-real',
      'locate-nlloc'       : 'al-jobs-nlloc',
      'locate-locsat'      : 'al-jobs-locsat',
      'velocity-velest'    : 'pipe-jobs-velocity',
      'relocation-hypodd'  : 'pipe-jobs-relocation',
      'detect-matchlocate' : 'pipe-jobs-detect',
      'magnitude-ml-gamma' : 'pipe-jobs-ml-gamma',
      'magnitude-ml-real'  : 'pipe-jobs-ml-real',
      'magnitude-ml-nlloc' : 'pipe-jobs-ml-nlloc',
      'magnitude-ml-locsat': 'pipe-jobs-ml-locsat',
      'magnitude-ml-hypodd': 'pipe-jobs-ml-hypodd',
      'imaging-simul2000'  : 'pipe-jobs-imaging',
      'mechanism-skhash'   : 'pipe-jobs-mechanism',
    };
    this.PIPE_JOBS_WRAP = {
      'assoc-gamma'        : 'al-jobs-wrap-gamma',
      'assoc-real'         : 'al-jobs-wrap-real',
      'locate-nlloc'       : 'al-jobs-wrap-nlloc',
      'locate-locsat'      : 'al-jobs-wrap-locsat',
      'velocity-velest'    : 'pipe-jobs-wrap-velocity',
      'relocation-hypodd'  : 'pipe-jobs-wrap-relocation',
      'detect-matchlocate' : 'pipe-jobs-wrap-detect',
      'magnitude-ml-gamma' : 'pipe-jobs-wrap-ml-gamma',
      'magnitude-ml-real'  : 'pipe-jobs-wrap-ml-real',
      'magnitude-ml-nlloc' : 'pipe-jobs-wrap-ml-nlloc',
      'magnitude-ml-locsat': 'pipe-jobs-wrap-ml-locsat',
      'magnitude-ml-hypodd': 'pipe-jobs-wrap-ml-hypodd',
      'imaging-simul2000'  : 'pipe-jobs-wrap-imaging',
      'mechanism-skhash'   : 'pipe-jobs-wrap-mechanism',
    };
    // Keyed by sidebar PAGE NAME (switchWpStep), not the API step name.
    // STEP_INPUT_SELECTS entries: [selectId, forStep, forMethod?]
    this.STEP_CHANNELS = {
      assocloc:  ['assoc-gamma', 'assoc-real', 'locate-nlloc', 'locate-locsat'],
      mechanism: ['mechanism-skhash'],
      velocity:  ['velocity-velest'],
      relocation:['relocation-hypodd'],
      imaging:   ['imaging-simul2000'],
      detect:    ['detect-matchlocate'],
    };
    this.STEP_INPUT_SELECTS = {
      assocloc:  [['ga-input', null], ['rl-input', null], ['nl-input', 'locate'], ['ls-input', 'locate']],
      mechanism: [['mech-input', 'mechanism']],
      velocity:  [['vel-catalog', 'velocity']],
      relocation:[['hd-input', 'relocation']],
      imaging:   [],
      detect:    [['mld-input', 'detect']],
    };

    // --- Model (Kategori C: cluster peta Leaflet utama + region-draw state) ---
    this.map = null;
    this.bathyLayer = null;
    this.topoLayer = null;
    this.darkLayer = null;
    this.activeBase = 'dark';
    this.regionRect = null;
    this.drawTempRect = null;
    this.handles = [];
    this.stationLayer = null;
    this.allStations = [];   // full unfiltered list
    this.currentStations = [];   // filtered to region
    this.currentFdsnUrl = '';
    this.drawMode = false;
    this.drawStart = null;
    this.isDragging = false;
    this.overlays = {};
  }

  // --- View (alert banners + HTML-escape, used everywhere) ---
  showAlert(prefix, msg, type) {
    this.clearAlert(prefix);
    if (!msg) return;
    const el = document.getElementById(`${prefix}-alert-${type}`);
    if (el) { el.textContent = msg; el.classList.add('show'); }
  }
  clearAlert(prefix) {
    ['err', 'ok'].forEach(t => { const el = document.getElementById(`${prefix}-alert-${t}`); if (el) { el.textContent = ''; el.classList.remove('show'); } });
  }
  esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // --- View (skeleton loading rows) ---
  skelRows(n) {
    const ws = ['80px','50px','95px','40px','28px'];
    return Array.from({length: n}, () =>
      `<div class="skel-row">${ws.map(w =>
        `<div class="skel-line" style="width:${w};height:8px"></div>`).join('')}</div>`
    ).join('');
  }

  // --- Model (lazy-load Plotly, used by every chart-based feature) ---
  ensurePlotly() {
    if (window.Plotly) return Promise.resolve();
    if (this.plotlyPromise) return this.plotlyPromise;
    this.plotlyPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = '/static/js/vendor/plotly-2.35.2.min.js';
      s.charset = 'utf-8';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('failed to load Plotly'));
      document.head.appendChild(s);
    });
    return this.plotlyPromise;
  }

  // --- Model (fetch JSON with byte-level progress — uses ReadableStream + Content-Length) ---
  async fetchJSON(url, onPct) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const total = parseInt(resp.headers.get('Content-Length') || '0');
    if (!resp.body || !total) {
      if (onPct) onPct(55);
      const d = await resp.json();
      if (onPct) onPct(100);
      return d;
    }
    const reader = resp.body.getReader();
    const chunks = [];
    let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (onPct) onPct(Math.min(94, Math.round(received / total * 100)));
    }
    if (onPct) onPct(100);
    return JSON.parse(await new Blob(chunks).text());
  }

  // --- Controller (input file selectors — shared by assoc/locate/velocity/relocation/detect) ---
  async loadPicksFiles(selectId) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    if (!this.activeConfigId) {
      sel.innerHTML = '<option value="">— no active project —</option>';
      return;
    }
    sel.innerHTML = '<option value="">⟳ Loading…</option>';
    try {
      const files = await (await fetch(`/api/pipeline/picks-files?cfg_id=${this.activeConfigId}`)).json();
      if (!files.length) {
        sel.innerHTML = '<option value="">— no completed picks jobs yet —</option>';
        return;
      }
      sel.innerHTML = files.map(f => {
        const p = f.picks || {};
        const lbl = `[${f.job_id}] ${f.method} — ${p.total || 0} picks (${(f.ts || '').slice(0, 16)})`;
        return `<option value="${this.esc(f.path)}">${this.esc(lbl)}</option>`;
      }).join('');
    } catch (e) {
      sel.innerHTML = `<option value="">Error: ${this.esc(e.message)}</option>`;
    }
  }

  async loadCatalogFiles(selectId, forStep, forMethod) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    if (!this.activeConfigId) {
      sel.innerHTML = '<option value="">— no active project —</option>';
      return;
    }
    sel.innerHTML = '<option value="">⟳ Loading…</option>';
    try {
      const parts = ['cfg_id=' + this.activeConfigId];
      if (forStep)   parts.push('step='   + forStep);
      if (forMethod) parts.push('method=' + forMethod);
      const url = `/api/pipeline/catalog-files?${parts.join('&')}`;
      const files = await (await fetch(url)).json();
      if (!files.length) {
        sel.innerHTML = '<option value="">— no completed catalog jobs yet —</option>';
        return;
      }
      sel.innerHTML = files.map(f => {
        const lbl = `[${f.job_id}] ${f.step}/${f.method}${f.filtered ? '_filter' : ''} — ${f.events || 0} events (${(f.ts || '').slice(0, 16)})`;
        return `<option value="${this.esc(f.path)}">${this.esc(lbl)}</option>`;
      }).join('');
    } catch (e) {
      sel.innerHTML = `<option value="">Error: ${this.esc(e.message)}</option>`;
    }
  }

  // --- Model (safe number formatter, used by any statistics panel) ---
  fmt(v, n = 4) { return (v == null || isNaN(v)) ? '–' : Number(v).toFixed(n); }

  // Base Plotly layout (transparent bg) shared by the AL & Reloc statistics panels.
  smLayout(overrides) {
    return Object.assign({}, this.SM_LAYOUT,
      { xaxis: Object.assign({}, this.SM_LAYOUT.xaxis, overrides?.xaxis),
        yaxis: Object.assign({}, this.SM_LAYOUT.yaxis, overrides?.yaxis) },
      overrides);
  }

  // --- Model (fault + volcano + coastline overlay cache, used by every result map) ---
  async ensureMapOverlays() {
    if (this.mapOverlays) return this.mapOverlays;
    const ov = { faults: [], volcanoes: [], coast: [] };
    try {
      const fd = await (await fetch('/footages/indogigis.geojson')).json();
      (fd.features || []).forEach(f => {
        if (f.geometry?.type === 'LineString') {
          const st = FAULT_STYLE[f.properties?.tipe || ''] || FAULT_DEFAULT;
          ov.faults.push({
            lons: f.geometry.coordinates.map(c => c[0]),
            lats: f.geometry.coordinates.map(c => c[1]),
            color: st.color,
          });
        }
      });
    } catch {}
    try {
      const vd = await (await fetch('/footages/Volcano.geojson')).json();
      (vd.features || []).forEach(f => {
        if (f.geometry?.type === 'Point') {
          const c = f.geometry.coordinates;
          ov.volcanoes.push({ lon: c[0], lat: c[1], name: f.properties?.name || '' });
        }
      });
    } catch {}
    try {
      // Coastline (Natural Earth) for wide Halmahera/Maluku bbox — used in 3D map
      const cd = await (await fetch('/api/footages/coastlines?lon_min=124&lat_min=-3&lon_max=132&lat_max=5')).json();
      if (cd.lines) ov.coast = cd.lines;
    } catch {}
    this.mapOverlays = ov;
    return ov;
  }

  // Bounds [W,E,S,N] from events + padding, to filter overlays to the region
  eventBounds(d, pad = 0.4) {
    const W = Math.min(...d.lons) - pad, E = Math.max(...d.lons) + pad;
    const S = Math.min(...d.lats) - pad, N = Math.max(...d.lats) + pad;
    return { W, E, S, N };
  }
  // Geographically-faithful aspectratio for scatter3d/mesh3d so the lon–lat plane
  // matches the 2D map (km-scaled, cos-lat corrected). The map PLANE is normalised
  // to its own longer side (so it always fills the box at true lon/lat proportion),
  // and the depth axis is scaled to the same km unit but CAPPED — otherwise a deep
  // catalog (e.g. depMax 200 km vs a ~110 km-wide region) makes the box a tall thin
  // tower with a tiny squashed map ("lonjong" in the 3D view, "mampat" in the stats
  // map). Returns {x,y,z}. `vex` exaggerates the vertical only (1 = true scale).
  geoAspect3d(lonMin, lonMax, latMin, latMax, depSpanKm, vex = 1) {
    const midLat = (latMin + latMax) / 2;
    const kmLon = Math.max(1e-3, (lonMax - lonMin) * 111.32 * Math.cos(midLat * Math.PI / 180));
    const kmLat = Math.max(1e-3, (latMax - latMin) * 110.57);
    const kmDep = Math.max(1e-3, depSpanKm * vex);
    const h = Math.max(kmLon, kmLat);          // normalise to the map's longer side
    const zCap = 0.5;                           // depth axis ≤ 50% of the map's long side
    return { x: kmLon / h, y: kmLat / h, z: Math.min(kmDep / h, zCap) };
  }
  inBounds(lon, lat, b) { return lon >= b.W && lon <= b.E && lat >= b.S && lat <= b.N; }
  segTouches(lons, lats, b) {
    for (let i = 0; i < lons.length; i++) if (this.inBounds(lons[i], lats[i], b)) return true;
    return false;
  }

  // Fault → trace per COLOR (per-type like main map). kind: '2d'|'3d'
  faultTraces(b, kind) {
    if (!this.mapOverlays?.faults?.length) return [];
    const groups = {};   // color → {lon:[],lat:[]}
    for (const seg of this.mapOverlays.faults) {
      if (!this.segTouches(seg.lons, seg.lats, b)) continue;
      const g = (groups[seg.color] ||= { lon: [], lat: [] });
      for (let i = 0; i < seg.lons.length; i++) { g.lon.push(seg.lons[i]); g.lat.push(seg.lats[i]); }
      g.lon.push(null); g.lat.push(null);
    }
    return Object.entries(groups).map(([color, g], idx) => kind === '3d'
      ? { type: 'scatter3d', mode: 'lines', x: g.lon, y: g.lat, z: g.lon.map(v => v == null ? null : 0),
          line: { color, width: 3 }, hoverinfo: 'skip', name: idx === 0 ? 'Fault' : '', showlegend: idx === 0 }
      : { type: 'scattermapbox', mode: 'lines', lon: g.lon, lat: g.lat,
          line: { color, width: 2 }, hoverinfo: 'skip', name: idx === 0 ? 'Fault' : '', showlegend: idx === 0 });
  }
  // Volcanoes within bounds
  volcanoesInBounds(b) {
    return (this.mapOverlays?.volcanoes || []).filter(v => this.inBounds(v.lon, v.lat, b));
  }
  // Coastline → segments touching bounds (z=0 for 3D)
  coastTrace3D(b) {
    if (!this.mapOverlays?.coast?.length) return null;
    const x = [], y = [], z = [];
    for (const seg of this.mapOverlays.coast) {
      if (!seg.length) continue;
      let touch = false;
      for (const [lo, la] of seg) if (this.inBounds(lo, la, b)) { touch = true; break; }
      if (!touch) continue;
      for (const [lo, la] of seg) { x.push(lo); y.push(la); z.push(0); }
      x.push(null); y.push(null); z.push(null);
    }
    if (!x.length) return null;
    return { type: 'scatter3d', mode: 'lines', x, y, z,
             line: { color: '#38bdf8', width: 1.5 }, hoverinfo: 'skip', name: 'Coastline' };
  }

  // ── Kompas peta ───────────────────────────────────────────────────────────
  // Static (north always up) for 2D map; dynamic (rose rotates with camera azimuth)
  // for 3D plots. by HakimBMKG.
  // Angle (degrees, CW from screen-top) to place the world North needle (+y)
  // according to the Plotly 3D camera projection. Derived from the screen camera
  // basis (forward = −eye, world up = +z): r=fxup, u=rxf, then N=(0,1,0) → (sx=N·r, sy=N·u).
  camHeadingDeg(eye) {
    const f = _V3.norm([-eye.x, -eye.y, -eye.z]);
    let r = _V3.cross(f, [0, 0, 1]);
    if (Math.hypot(r[0], r[1], r[2]) < 1e-6) r = [1, 0, 0];   // looking straight down
    r = _V3.norm(r);
    const u = _V3.cross(r, f);
    return Math.atan2(_V3.dot([0, 1, 0], r), _V3.dot([0, 1, 0], u)) * 180 / Math.PI;
  }
  compassSVG() {
    return `<svg viewBox="0 0 44 44" width="100%" height="100%">
      <circle cx="22" cy="22" r="20.5" fill="rgba(10,15,30,.74)" stroke="#3b5573" stroke-width="1"/>
      <g class="mc-rose">
        <polygon points="22,5 17.5,23 22,19 26.5,23" fill="#ef4444"/>
        <polygon points="22,39 17.5,21 22,25 26.5,21" fill="#8aa0b8"/>
        <circle cx="22" cy="22" r="1.6" fill="#cbd5e1"/>
        <text x="22" y="11.4" text-anchor="middle" font-size="7.5" font-weight="700" fill="#fecaca" font-family="Segoe UI,Arial">N</text>
      </g></svg>`;
  }
  mkCompass(host, corner, key) {
    if (!host) return null;
    if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
    let c = host.querySelector(key ? `:scope > .map-compass[data-for="${key}"]` : ':scope > .map-compass');
    if (!c) {
      c = document.createElement('div');
      c.className = 'map-compass mc-' + (corner || 'tr');
      if (key) c.setAttribute('data-for', key);
      c.innerHTML = this.compassSVG();
      host.appendChild(c);
    }
    return c;
  }
  addStaticCompass(host, corner) { this.mkCompass(host, corner || 'tr'); }
  // Dynamic for 3D plots. bindRelayout=true ONLY after Plotly.newPlot (new gd).
  add3DCompass(divId, host, bindRelayout, corner) {
    const plot = document.getElementById(divId);
    if (!plot) return;
    const c = this.mkCompass(host || plot.parentElement, corner || 'tr', divId);
    if (!c) return;
    const rose = c.querySelector('.mc-rose');
    const upd = () => {
      const sc = (plot._fullLayout && plot._fullLayout.scene) || (plot.layout && plot.layout.scene);
      const eye = sc && sc.camera && sc.camera.eye;
      if (eye && rose) rose.style.transform = `rotate(${this.camHeadingDeg(eye)}deg)`;
    };
    upd();
    if (bindRelayout && typeof plot.on === 'function') plot.on('plotly_relayout', upd);
  }

  // --- View (2D/3D epicenter map shared by AL/Reloc/Crosscorr stats) ---
  // Uses Plotly scattermapbox + style 'carto-darkmatter' (no token required).
  // d: { lons, lats, map_depths, map_mags, sta_lon, sta_lat, sta_name }
  plotEpicenterMap2D(divId, d) {
    const el = document.getElementById(divId);
    if (!el) return;
    if (!d.lons || !d.lons.length) {
      el.innerHTML = '<div style="color:#334155;font-size:.7rem;padding:2rem;text-align:center">No event coordinates</div>';
      return;
    }
    const depClip = d.map_depths.map(v => Math.min(v, 200));
    const magSize = d.map_mags.map(m => Math.max(4, Math.min(15, (m + 1) * 1.9)));
    const clat = d.lats.reduce((a, b) => a + b, 0) / d.lats.length;
    const clon = d.lons.reduce((a, b) => a + b, 0) / d.lons.length;

    const b = this.eventBounds(d);
    const traces = [];
    // Fault (line per type color like main map) — at bottom
    this.faultTraces(b, '2d').forEach(t => traces.push(t));
    // Event (epicenter)
    traces.push({
      type: 'scattermapbox', mode: 'markers',
      lon: d.lons, lat: d.lats,
      text: d.lons.map((_, i) => `Lon ${d.lons[i]}, Lat ${d.lats[i]}<br>Dep ${d.map_depths[i]} km · M${d.map_mags[i]}`),
      hoverinfo: 'text',
      marker: {
        size: magSize, color: depClip, colorscale: 'Viridis', reversescale: true,
        colorbar: { title: { text: 'km', font: { size: 9, color: '#cbd5e1' } },
                    thickness: 10, len: 0.7, tickfont: { size: 8, color: '#cbd5e1' } },
        opacity: 0.85,
      },
      name: 'Event',
    });
    // Volcanoes — emoji icon 🌋 (like main map)
    const vols = this.volcanoesInBounds(b);
    if (vols.length) traces.push({
      type: 'scattermapbox', mode: 'text',
      lon: vols.map(v => v.lon), lat: vols.map(v => v.lat),
      text: vols.map(() => '🌋'), textfont: { size: 16 },
      hovertext: vols.map(v => 'Volcano ' + v.name), hoverinfo: 'text',
      name: 'Volcano',
    });
    // Stations (yellow triangle)
    if (d.sta_lon && d.sta_lon.length) traces.push({
      type: 'scattermapbox', mode: 'text',
      lon: d.sta_lon, lat: d.sta_lat,
      text: d.sta_name.map(() => '▲'), textfont: { size: 12, color: '#facc15' },
      hovertext: d.sta_name.map(s => 'Station ' + s), hoverinfo: 'text',
      name: 'Station',
    });
    Plotly.newPlot(divId, traces, {
      mapbox: { style: 'carto-darkmatter', center: { lon: clon, lat: clat }, zoom: 8.2 },
      paper_bgcolor: '#0d1626', font: { color: '#cbd5e1', size: 10 },
      margin: { t: 0, b: 0, l: 0, r: 0 }, showlegend: true,
      legend: { x: 0, y: 1, bgcolor: 'rgba(13,22,38,.7)', font: { size: 9, color: '#cbd5e1' },
                bordercolor: '#334155', borderwidth: 1 },
    }, { responsive: true, displayModeBar: false, scrollZoom: true });
    this.addStaticCompass(document.getElementById(divId)?.parentElement, 'tr');
  }

  // ── 3D hypocenter map (lon, lat, depth) ──────────────────────────────────
  plotHypoMap3D(divId, d) {
    const el = document.getElementById(divId);
    if (!el) return;
    if (!d.lons || !d.lons.length) {
      el.innerHTML = '<div style="color:#334155;font-size:.7rem;padding:2rem;text-align:center">No event coordinates</div>';
      return;
    }
    const depNeg = d.map_depths.map(v => -v);
    const depClip = d.map_depths.map(v => Math.min(v, 200));
    const magSize = d.map_mags.map(m => Math.max(2, Math.min(10, (m + 1) * 1.4)));
    const traces = [{
      type: 'scatter3d', mode: 'markers',
      x: d.lons, y: d.lats, z: depNeg,
      text: d.lons.map((_, i) => `${d.lons[i]}, ${d.lats[i]}<br>Dep ${d.map_depths[i]} km · M${d.map_mags[i]}`),
      hoverinfo: 'text',
      marker: {
        size: magSize, color: depClip, colorscale: 'Viridis', reversescale: true,
        colorbar: { title: { text: 'km', font: { size: 8, color: '#94a3b8' } },
                    thickness: 8, len: 0.55, x: 1.0, tickfont: { size: 7, color: '#94a3b8' } },
        opacity: 0.85, line: { width: 0 },
      },
      name: 'Event',
    }];
    const b = this.eventBounds(d);
    // Coastline (blue coastline) at surface z=0
    const ct = this.coastTrace3D(b);
    if (ct) traces.push(ct);
    // Fault colored by type at surface (z=0)
    this.faultTraces(b, '3d').forEach(t => traces.push(t));
    // Volcanoes — emoji icon 🌋 at surface
    const vols = this.volcanoesInBounds(b);
    if (vols.length) traces.push({
      type: 'scatter3d', mode: 'text',
      x: vols.map(v => v.lon), y: vols.map(v => v.lat), z: vols.map(() => 0),
      text: vols.map(() => '🌋'), textfont: { size: 13 },
      hovertext: vols.map(v => 'Volcano ' + v.name), hoverinfo: 'text', name: 'Volcano',
    });
    if (d.sta_lon && d.sta_lon.length) {
      traces.push({
        type: 'scatter3d', mode: 'text',
        x: d.sta_lon, y: d.sta_lat, z: d.sta_lon.map(() => 0),
        text: d.sta_name.map(() => '▲'), textfont: { size: 11, color: '#facc15' },
        hovertext: d.sta_name.map(s => 'Station ' + s), hoverinfo: 'text', name: 'Station',
      });
    }
    Plotly.newPlot(divId, traces, {
      paper_bgcolor: '#0d1626',
      scene: {
        bgcolor: '#0d1626',
        // lon/lat range locked to event bbox (same as geoAspect3d) so that x & y
        // scales are uniform; without this, coastlines overflowing the bbox cause
        // unbalanced auto-range → map appears squashed/shifted.
        xaxis: { title: 'Longitude', titlefont: { size: 9, color: '#64748b' }, gridcolor: '#1e293b', tickfont: { size: 7, color: '#64748b' }, range: [b.W, b.E] },
        yaxis: { title: 'Latitude', titlefont: { size: 9, color: '#64748b' }, gridcolor: '#1e293b', tickfont: { size: 7, color: '#64748b' }, range: [b.S, b.N] },
        zaxis: { title: 'Depth (km)', titlefont: { size: 9, color: '#64748b' }, gridcolor: '#1e293b', tickfont: { size: 7, color: '#64748b' },
                 tickvals: [0,-50,-100,-150,-200], ticktext: ['0','50','100','150','200+'] },
        camera: { eye: { x: 1.5, y: -1.8, z: 0.9 } },
        aspectmode: 'manual',
        aspectratio: this.geoAspect3d(b.W, b.E, b.S, b.N,
                                  Math.max(1, ...d.map_depths.filter(v => isFinite(v)))),
      },
      margin: { t: 0, b: 0, l: 0, r: 0 }, font: { color: '#94a3b8', size: 9 }, showlegend: true,
      legend: { x: 0, y: 1, bgcolor: 'rgba(13,22,38,.7)', font: { size: 9, color: '#cbd5e1' },
                bordercolor: '#334155', borderwidth: 1 },
    }, { responsive: true, displayModeBar: false });
    this.add3DCompass(divId, document.getElementById(divId)?.parentElement, true, 'br');
  }

  // --- View (mini progress bar in the pipeline/picking terminal, shared) ---
  ensureProg(termId) {
    let bar = document.getElementById(`prog-${termId}`);
    if (bar) return bar;
    const term = document.getElementById(termId);
    if (!term) return null;
    bar = document.createElement('div');
    bar.id = `prog-${termId}`;
    bar.className = 'sw-prog';
    bar.innerHTML = '<div class="sw-prog-fill"></div><span class="sw-prog-txt"></span>';
    term.parentNode.insertBefore(bar, term);
    return bar;
  }
  // frac: 0..1 (determinate) | null (indeterminate). label is optional.
  setProgress(termId, frac, label) {
    const bar = this.ensureProg(termId);
    if (!bar) return;
    bar.style.display = '';
    const fill = bar.querySelector('.sw-prog-fill');
    const txt = bar.querySelector('.sw-prog-txt');
    if (frac == null) {
      fill.classList.add('indet');
      fill.style.width = '30%';
    } else {
      fill.classList.remove('indet');
      fill.style.width = `${Math.round(Math.min(1, Math.max(0, frac)) * 100)}%`;
    }
    if (txt) txt.textContent = label || (frac != null ? `${Math.round(frac * 100)}%` : '');
  }
  hideProgress(termId) {
    const bar = document.getElementById(`prog-${termId}`);
    if (bar) bar.style.display = 'none';
  }

  // --- Controller (refresh the per-channel job list, used by all pipeline steps) ---
  async refreshPipeJobsCh(ch, step) {
    if (!this.activeConfigId) return;
    try {
      const jobs = await (await fetch(`/api/pipeline/jobs?step=${step}&cfg_id=${this.activeConfigId}`)).json();
      const { method } = _chInfo(ch);
      const filtered = jobs.filter(j => j.method === method);
      _renderPipeJobsCh(ch, filtered);
    } catch { }
  }
}

const SW = new SeisWorkCore();

// ═══════════════════════════════════════════
//  Timezone helper — UTC vs WIB (UTC+7)
//  Stored in localStorage so setting persists across reload.
// ═══════════════════════════════════════════
const _TZ = {
  get h() { return parseInt(localStorage.getItem('sw_tz_offset') ?? '7', 10); },
  set h(v) { localStorage.setItem('sw_tz_offset', String(v)); },
  get label() { return this.h === 0 ? 'UTC' : `WIB (UTC+${this.h})`; },
  fmt(isoStr, showSec = true) {
    if (!isoStr) return '–';
    try {
      const d = new Date(isoStr.replace(' ', 'T').replace(/([+-]\d{2}:\d{2}|Z)$/, '') + 'Z');
      d.setTime(d.getTime() + this.h * 3600000);
      const p = n => String(n).padStart(2, '0');
      const base = `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
      return showSec ? `${base}:${p(d.getUTCSeconds())}` : base;
    } catch (_) { return isoStr.slice(0, 19).replace('T', ' '); }
  },
  toggle() {
    this.h = this.h === 0 ? 7 : 0;
    _tzUpdateUI();
  },
};

function _tzUpdateUI() {
  document.querySelectorAll('.sw-tz-toggle').forEach(b => {
    b.textContent = _TZ.label;
    b.title = `Click to switch to ${_TZ.h === 0 ? 'WIB' : 'UTC'}`;
  });
  // Re-render places that display datetime
  _rmUpdateDates();
}

function _rmUpdateDates() {
  // Rebuild all charts/labels that display datetime
  if (window._RM?.activeJob?.events) {
    try { _rmUpdate(); } catch (_) {}
  }
}

// Init timezone toggle button labels on load
document.addEventListener('DOMContentLoaded', () => _tzUpdateUI());

// ═══════════════════════════════════════════
//  Remote-server federation (client mirror)
//  When a remote server URL is set, every same-origin "/api/..." fetch is
//  transparently rerouted to that server with the bearer token attached, so the
//  whole GUI drives the remote server (jobs run + persist there; IDs sync).
//  Empty URL = local standalone mode (default).
// ═══════════════════════════════════════════
// Connection persists across page reloads (localStorage) so the GUI stays
// mirrored to the remote server without re-entering URL + token each time.
// All remote API calls are proxied server-side via /api/remote/<subpath>
// so the browser never needs direct network access to the remote server.
// SW.REMOTE/SW.OFFLINE → SeisWorkCore (SW.REMOTE/SW.OFFLINE)
(function _patchFetch() {
  const _orig = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    opts = opts || {};
    // Reroute /api/<X> → /api/remote/<X> when remote server is active.
    // The local Flask server proxies the request server-side.
    if (typeof url === 'string' && url.startsWith('/api/') && SW.REMOTE.base) {
      const sub = url.slice('/api/'.length);   // e.g. "configs" or "health"
      // Don't proxy in offline mode, remote-management, or local-only operations
      if (!SW.OFFLINE.active && !sub.startsWith('remote/') && !sub.startsWith('local/')) {
        url = '/api/remote/' + sub;
      }
    }
    return _orig(url, opts);
  };
})();

// Federation UI (server panel, offline banner, splash) → modules/bootstrap-ui.js (BOOT)

// _ensurePlotly → SeisWorkCore.ensurePlotly (SW.ensurePlotly)

// ═══════════════════════════════════════════
//  State
// ═══════════════════════════════════════════
// map/SW.bathyLayer/SW.topoLayer/SW.darkLayer/SW.activeBase/SW.regionRect/SW.drawTempRect/SW.handles/
// SW.stationLayer/SW.allStations/SW.currentStations/SW.currentFdsnUrl/SW.drawMode/SW.drawStart/
// SW.isDragging/SW.overlays → SeisWorkCore (SW.xxx, Kategori C)
// SW.activeConfigId/SW.activeProjectName → SeisWorkCore (SW.activeConfigId/SW.activeProjectName)

const FAULT_STYLE = {
  'Sesar Geser': { color: '#f59e0b', weight: 2.5, opacity: 1 },
  'Sesar Geser Symbol': { color: '#f59e0b', weight: 1.8, opacity: .9, dashArray: '6,4' },
  'Sesar Naik': { color: '#ef4444', weight: 2.5, opacity: 1 },
  'Sesar Turun': { color: '#60a5fa', weight: 2.5, opacity: 1 },
  'Sesar Turun Sysmbol': { color: '#60a5fa', weight: 1.8, opacity: .9, dashArray: '6,4' },
  'Sesar Naik Sysmbol': { color: '#ef4444', weight: 1.8, opacity: .9, dashArray: '6,4' },
};
const FAULT_DEFAULT = { color: '#a78bfa', weight: 2, opacity: 1 };

// ═══════════════════════════════════════════
//  Map init
// ═══════════════════════════════════════════
async function initMap() {
  BOOT.splashProgress(8, 'Preparing map…');
  SW.map = L.map('map', { zoomControl: true });
  SW.map.fitBounds([[6.5, 94.5], [-11.5, 141.5]]); // default landing view: whole Indonesia
  SW.addStaticCompass(document.getElementById('map'), 'br');

  SW.bathyLayer = L.tileLayer(
    '/tiles/esri_ocean/{z}/{x}/{y}.png',
    { attribution: '&copy; Esri — GEBCO, NOAA, NGDC', maxZoom: 13, maxNativeZoom: 13 }
  );
  SW.topoLayer = L.tileLayer(
    '/tiles/opentopomap/{z}/{x}/{y}.png',
    { attribution: '&copy; OpenTopoMap (CC-BY-SA)', maxZoom: 17, subdomains: ['a', 'b', 'c'] }
  );
  SW.darkLayer = L.tileLayer(
    '/tiles/carto_dark/{z}/{x}/{y}.png',
    { attribution: '&copy; CartoDB', maxZoom: 19, subdomains: ['a', 'b', 'c', 'd'] }
  );
  SW.darkLayer.addTo(SW.map);

  SW.map.on('mousemove', e => {
    document.getElementById('coord-bar').textContent =
      `${e.latlng.lat.toFixed(4)}° N   ${e.latlng.lng.toFixed(4)}° E`;
  });
  SW.map.on('click', () => { if (!SW.drawMode) closeLayerPanel(); });
  SW.map.on('mousedown', onMapMouseDown);
  SW.map.on('mousemove', onMapMouseMove);
  SW.map.on('mouseup', onMapMouseUp);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && SW.drawMode) cancelDrawMode(); });

  BOOT.splashProgress(25, 'Peta siap'); BOOT.splashStep('peta');
  try {
    BOOT.splashProgress(35, 'Loading base configuration…');
    const cfg = await (await fetch('/api/base-config')).json();
    fillRegionForm(cfg);
    BOOT.splashProgress(45); BOOT.splashStep('config dasar');
  } catch { }

  try { BOOT.splashProgress(50, 'Loading map layers (fault/volcano/slab)…'); await loadFootages(); BOOT.splashProgress(65); BOOT.splashStep('overlay'); } catch (e) {}
  // Reconnect to remote server before loading configs so proxy is ready.
  // If unreachable, fall back to offline mode using locally synced data.
  if (SW.REMOTE.base) {
    let _connected = false;
    try {
      BOOT.splashProgress(68, 'Reconnecting to remote server…');
      const _ac = new AbortController();
      const _t  = setTimeout(() => _ac.abort(), 4000);
      const _r  = await fetch('/api/remote/connect', {
        method : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body   : JSON.stringify({ url: SW.REMOTE.base, token: SW.REMOTE.token }),
        signal : _ac.signal,
      });
      clearTimeout(_t);
      _connected = _r.ok;
    } catch { }
    if (!_connected) {
      try {
        BOOT.splashProgress(69, 'Server unreachable — checking local data…');
        const od = await fetch('/api/local/offline').then(r => r.ok ? r.json() : null);
        if (od && od.configs.length) {
          SW.OFFLINE.active = true;
          SW.OFFLINE.data   = od;
          BOOT.showOfflineBanner(od);
        }
      } catch { }
    }
  }
  try { BOOT.splashProgress(70, 'Loading saved configuration…'); await loadConfigList(); BOOT.splashProgress(90); BOOT.splashStep('configuration'); } catch (e) {}
  try { BOOT.splashProgress(92, 'Loading job list…'); await refreshJobs(); BOOT.splashProgress(99); BOOT.splashStep('jobs'); } catch (e) {}
  BOOT.splashDone();
}

// ═══════════════════════════════════════════
//  GeoJSON SW.overlays
// ═══════════════════════════════════════════
const OVERLAY_META = {
  'indogigis.geojson': { label: 'Faults', icon: 'bi-slash-lg', defaultOn: true },
  'gigis.geojson': { label: 'Fault Symbols', icon: 'bi-pentagon', defaultOn: true },
  'namesesar.geojson': { label: 'Fault Names', icon: 'bi-fonts', defaultOn: false },
  'Volcano.geojson': { label: 'Volcanoes', icon: 'bi-fire', defaultOn: true },
};

async function loadFootages() {
  let files = [];
  try {
    const res = await fetch('/api/footages');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    files = await res.json();
  } catch (e) { console.error('[SeisWork] /api/footages failed:', e); return; }

  if (!SW.map.getPane('geojsonPane')) {
    const pane = SW.map.createPane('geojsonPane');
    pane.style.zIndex = 450;
  }
  if (!SW.map.getPane('faultNamePane')) {
    const pane = SW.map.createPane('faultNamePane');
    pane.style.zIndex = 455;  // above geojsonPane (450) so names sit on top of fault lines
  }

  const container = document.getElementById('overlay-opts');
  container.innerHTML = '';

  // Slab contours are served dynamically per-AOI via /api/slab (see
  // setupSlabOverlay) — drop the static footage so we don't show a stale duplicate.
  files = files.filter(f => f.toLowerCase() !== 'contour_slabs.geojson');

  // Layer loading spans splash progress 50→65% — step through it per file so
  // the percentage actually advances instead of sitting frozen on one number.
  const PCT_LO = 50, PCT_HI = 65;
  let i = 0;
  for (const filename of files) {
    const meta = OVERLAY_META[filename] || { label: filename.replace('.geojson', ''), icon: 'bi-geo', defaultOn: false };
    const key = filename.replace('.geojson', '').toLowerCase();
    i++;
    BOOT.splashProgress(PCT_LO + (PCT_HI - PCT_LO) * (i - 1) / files.length,
      `Loading layer: ${meta.label} (${i}/${files.length})…`);

    const div = document.createElement('div');
    div.className = `layer-opt${meta.defaultOn ? ' active' : ''}`;
    div.id = `opt-ov-${key}`;
    div.innerHTML = `<span class="chk-box">${meta.defaultOn ? '✓' : ''}</span>`
      + `<i class="bi ${meta.icon}" style="font-size:.78rem;width:14px"></i>`
      + `<span>${SW.esc(meta.label)}</span>`
      + `<span class="count-badge count-blue" id="cnt-${key}" style="margin-left:auto;font-size:.6rem">…</span>`;
    div.onclick = () => toggleOverlay(key);
    container.appendChild(div);

    const layer = await buildGeoJsonLayer(filename, key);
    const cnt = layer ? layer.getLayers().length : 0;
    const badge = document.getElementById(`cnt-${key}`);
    if (badge) badge.textContent = cnt > 0 ? cnt : '✗';

    SW.overlays[key] = { layer, visible: meta.defaultOn, label: meta.label, filename };
    if (meta.defaultOn && layer && cnt > 0) { layer.addTo(SW.map); }
    BOOT.splashProgress(PCT_LO + (PCT_HI - PCT_LO) * i / files.length);
  }
  setupSlabOverlay();
  updateLegendVisibility();
}

// ── Slab2 (auto) overlay — dynamic depth contours for the active AOI ───────────
// On/off layer on the main map. Contours are generated on demand from the
// official USGS Slab2.0 models for the active project region (or the current map
// view if no project is loaded), coloured by depth.
function setupSlabOverlay() {
  const container = document.getElementById('overlay-opts');
  if (!container || document.getElementById('opt-ov-slab2')) return;
  SW.overlays['slab2'] = { layer: null, visible: false, label: 'Slab2 (auto)', dynamic: true, bboxKey: null };
  const div = document.createElement('div');
  div.className = 'layer-opt';
  div.id = 'opt-ov-slab2';
  div.innerHTML = `<span class="chk-box"></span>`
    + `<i class="bi bi-layers-half" style="font-size:.78rem;width:14px"></i>`
    + `<span>Slab2 (auto)</span>`
    + `<span class="count-badge count-blue" id="cnt-slab2" style="margin-left:auto;font-size:.6rem">○</span>`;
  div.onclick = () => toggleSlabOverlay();
  container.appendChild(div);
}

// Depth (km, positive down) → colour: shallow warm, deep cool.
function _slabDepthColor(depKm) {
  const stops = [
    [0,   '#d73027'], [50,  '#fc8d59'], [100, '#fee090'],
    [150, '#e0f3f8'], [200, '#91bfdb'], [300, '#4575b4'], [500, '#313695'],
  ];
  let c = stops[stops.length - 1][1];
  for (let k = 0; k < stops.length; k++) { if (depKm <= stops[k][0]) { c = stops[k][1]; break; } }
  return c;
}

function _slabBbox() {
  const r = (typeof getRegionForm === 'function') ? getRegionForm() : {};
  if ([r.lon_min, r.lat_min, r.lon_max, r.lat_max].every(v => isFinite(v))) {
    return [r.lon_min, r.lat_min, r.lon_max, r.lat_max];
  }
  const b = SW.map.getBounds();
  return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
}

async function buildSlabLayer(bbox) {
  const [lon_min, lat_min, lon_max, lat_max] = bbox;
  const url = `/api/slab?lon_min=${lon_min}&lat_min=${lat_min}&lon_max=${lon_max}&lat_max=${lat_max}`;
  const data = await (await fetch(url)).json();
  if (!data?.features?.length) return { layer: null, n: 0, regions: [] };
  if (!SW.map.getPane('geojsonPane')) { const p = SW.map.createPane('geojsonPane'); p.style.zIndex = 450; }
  if (!SW.map.getPane('faultNamePane')) { const p = SW.map.createPane('faultNamePane'); p.style.zIndex = 455; }
  const layer = L.geoJSON(data, {
    pane: 'geojsonPane',
    style: feat => {
      const dep = -(feat.properties?.ELEV || 0);
      return { color: _slabDepthColor(dep), weight: 1.6, opacity: 0.85, pane: 'geojsonPane' };
    },
    onEachFeature: (feat, lyr) => {
      const dep = -(feat.properties?.ELEV || 0);
      const rg = (feat.properties?.region || '').toUpperCase();
      lyr.bindTooltip(`Slab ${rg} — ${dep.toFixed(0)} km`, { className: 'sw-tooltip', sticky: true });
    },
  });
  return { layer, n: layer.getLayers().length, regions: data.regions || [] };
}

async function toggleSlabOverlay() {
  const ov = SW.overlays['slab2'];
  if (!ov) return;
  const badge = document.getElementById('cnt-slab2');
  const optEl = document.getElementById('opt-ov-slab2');
  ov.visible = !ov.visible;
  if (optEl) {
    optEl.classList.toggle('active', ov.visible);
    optEl.querySelector('.chk-box').textContent = ov.visible ? '✓' : '';
  }
  if (!ov.visible) {
    if (ov.layer) SW.map.removeLayer(ov.layer);
    if (badge) badge.textContent = '○';
    return;
  }
  // turning on → (re)build for the current AOI
  const bbox = _slabBbox();
  const key = bbox.map(v => (+v).toFixed(2)).join(',');
  if (!ov.layer || ov.bboxKey !== key) {
    if (ov.layer) { SW.map.removeLayer(ov.layer); ov.layer = null; }
    if (badge) badge.textContent = '…';
    try {
      const { layer, n } = await buildSlabLayer(bbox);
      ov.layer = layer; ov.bboxKey = key;
      if (badge) badge.textContent = n > 0 ? n : '✗';
    } catch (e) { if (badge) badge.textContent = '✗'; console.error('[SeisWork] slab overlay:', e); return; }
  } else if (badge) {
    badge.textContent = ov.layer.getLayers().length;
  }
  if (ov.layer && ov.visible) ov.layer.addTo(SW.map);
}

// Refresh the slab overlay when the active region changes (only if shown).
async function refreshSlabOverlay() {
  const ov = SW.overlays['slab2'];
  if (!ov || !ov.visible) return;
  const bbox = _slabBbox();
  const key = bbox.map(v => (+v).toFixed(2)).join(',');
  if (ov.bboxKey === key) return;
  if (ov.layer) { SW.map.removeLayer(ov.layer); ov.layer = null; }
  const badge = document.getElementById('cnt-slab2');
  if (badge) badge.textContent = '…';
  try {
    const { layer, n } = await buildSlabLayer(bbox);
    ov.layer = layer; ov.bboxKey = key;
    if (badge) badge.textContent = n > 0 ? n : '✗';
    if (ov.layer) ov.layer.addTo(SW.map);
  } catch (e) { if (badge) badge.textContent = '✗'; }
}

// Representative [lat, lon] for a (Multi)LineString: middle vertex of its
// Midpoint lat/lng + local bearing of the longest segment — used to anchor
// and rotate the fault-name label so it follows the fault direction.
function _faultLabelInfo(geom) {
  if (!geom) return null;
  let lines = [];
  if (geom.type === 'MultiLineString') lines = geom.coordinates || [];
  else if (geom.type === 'LineString') lines = [geom.coordinates || []];
  let best = null;
  for (const ln of lines) if (ln && (!best || ln.length > best.length)) best = ln;
  if (!best || !best.length) return null;
  const mid = Math.floor(best.length / 2);
  const c = best[mid];
  if (!c || c.length < 2) return null;
  const ll = [c[1], c[0]];

  // Local direction: vector spanning a window around the midpoint.
  const i0 = Math.max(0, mid - Math.max(1, Math.floor(best.length * 0.15)));
  const i1 = Math.min(best.length - 1, mid + Math.max(1, Math.floor(best.length * 0.15)));
  const p0 = best[i0], p1 = best[i1];
  let angle = 0;
  if (p0 && p1 && (p1[0] !== p0[0] || p1[1] !== p0[1])) {
    const dlon = p1[0] - p0[0];
    const dlat = p1[1] - p0[1];
    const cosLat = Math.cos(c[1] * Math.PI / 180);
    // Screen angle: atan2(-dlat, dlon*cosLat) — negative dlat because screen-y is inverted.
    angle = Math.atan2(-dlat, dlon * cosLat) * 180 / Math.PI;
    // Keep text readable: normalize to [-90°, 90°] so it's never upside-down.
    if (angle > 90) angle -= 180;
    if (angle < -90) angle += 180;
  }
  return { ll, angle };
}

// Fault-name labels (namesesar.geojson) — combine the named-fault database with
// the fault lines by drawing ONLY the names. One label per unique fault name
// (anchored on its longest segment) to avoid duplicate clutter.
function buildFaultNameLayer(data) {
  const grp = L.layerGroup();
  const seen = new Map();   // Name → best (longest) feature
  for (const f of data.features || []) {
    const nm = (f.properties?.Name || '').trim();
    if (!nm) continue;
    const len = +(f.properties?.Length_km) || 0;
    const prev = seen.get(nm);
    if (!prev || len > prev.__len) { f.__len = len; seen.set(nm, f); }
  }
  for (const f of seen.values()) {
    const info = _faultLabelInfo(f.geometry);
    if (!info) continue;
    const p = f.properties || {};
    const nm = (p.Name || '').trim();
    const mmax = p.Mmax ? ` · M<sub>max</sub> ${p.Mmax}` : '';
    const reg = p.Region ? `<br><small>${SW.esc(p.Region)}</small>` : '';
    const rot = info.angle.toFixed(1);
    L.marker(info.ll, {
      pane: 'faultNamePane',
      icon: L.divIcon({
        className: 'fault-name-label',
        html: `<span style="transform:translate(-50%,-50%) rotate(${rot}deg)">${SW.esc(nm)}</span>`,
        iconSize: null,
      }),
    })
      .bindTooltip(`<b>${SW.esc(nm)}</b>${mmax}${reg}`, { className: 'sw-tooltip', sticky: true })
      .addTo(grp);
  }
  return grp;
}

async function buildGeoJsonLayer(filename, key) {
  try {
    const res = await fetch(`/footages/${filename}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data?.features?.length) throw new Error('empty features');

    // Cache raw data so online map can reuse without re-fetching
    SW._geoRaw = SW._geoRaw || {};
    SW._geoRaw[filename] = data;

    if (key === 'namesesar') {
      return buildFaultNameLayer(data);
    }

    if (key === 'volcano') {
      return L.geoJSON(data, {
        pane: 'geojsonPane',
        pointToLayer: (feat, latlng) => {
          const name = (feat.properties?.name || '').split('|')[0].trim();
          return L.marker(latlng, { icon: volcanoIcon(), pane: 'geojsonPane' })
            .bindTooltip(`<b>${SW.esc(name)}</b>`, { className: 'sw-tooltip' });
        },
      });
    }
    return L.geoJSON(data, {
      pane: 'geojsonPane',
      style: feat => ({
        ...(FAULT_STYLE[feat.properties?.tipe || ''] || FAULT_DEFAULT),
        pane: 'geojsonPane',
      }),
      onEachFeature: (feat, lyr) => {
        const tipe = feat.properties?.tipe || 'Unknown';
        lyr.bindTooltip(`<b>${SW.esc(tipe)}</b>`, { className: 'sw-tooltip', sticky: false });
      },
    });
  } catch (e) { console.error(`[SeisWork] buildGeoJsonLayer ${filename}:`, e); return null; }
}

// ══════════════════════════════════════════════════════════════════════════════
//  SW.buildOnlineMap — build a Leaflet map with exactly the same layers as the
//  offline map (basemap + overlays + compass + layer control). Used by
//  online-monitor.js for the station map dashboard and the wizard preview.
// ══════════════════════════════════════════════════════════════════════════════
SW.buildOnlineMap = function(divId, opts = {}) {
  const el = document.getElementById(divId);
  if (!el) return null;

  // zoomControl is disabled then re-added manually at the TOP-RIGHT — the layer control
  // (the bi-layers button) sits at the TOP-LEFT; with the zoom control also top-left
  // they stack and cover each other. Keep them in separate corners.
  const map = L.map(divId, Object.assign({ zoomControl: false }, opts.leafletOpts || {}));
  L.control.zoom({ position: 'topright' }).addTo(map);

  // Basemap layers — a new instance per map (one layer can only live on one map)
  const baseLayers = {
    bathy: L.tileLayer(
      '/tiles/esri_ocean/{z}/{x}/{y}.png',
      { attribution: '&copy; Esri — GEBCO, NOAA, NGDC', maxZoom: 13, maxNativeZoom: 13 }),
    topo: L.tileLayer(
      '/tiles/opentopomap/{z}/{x}/{y}.png',
      { attribution: '&copy; OpenTopoMap (CC-BY-SA)', maxZoom: 17, subdomains: ['a', 'b', 'c'] }),
    dark: L.tileLayer(
      '/tiles/carto_dark/{z}/{x}/{y}.png',
      { attribution: '&copy; CartoDB', maxZoom: 19, subdomains: ['a', 'b', 'c', 'd'] }),
  };
  let activeBase = 'dark';
  baseLayers.dark.addTo(map);
  // One-shot notification when the first basemap tile finishes loading — used by the
  // caller to hide the loading overlay (the map often looks "blank white" merely
  // because it waits on tiles from an external CDN, not an error).
  if (opts.onTilesLoaded) baseLayers[activeBase].once('load', opts.onTilesLoaded);

  // GeoJSON panes — z-index: fault(450) → volcano/namesesar(455) → station(460) → event(470)
  if (!map.getPane('geojsonPane'))   { const p = map.createPane('geojsonPane');   p.style.zIndex = 450; }
  if (!map.getPane('faultNamePane')) { const p = map.createPane('faultNamePane'); p.style.zIndex = 455; }
  if (!map.getPane('stationPane'))   { const p = map.createPane('stationPane');   p.style.zIndex = 460; }
  if (!map.getPane('eventPane'))     { const p = map.createPane('eventPane');     p.style.zIndex = 470; }

  // GeoJSON overlays from the cache (filled by buildGeoJsonLayer on offline map load)
  const geoRaw = SW._geoRaw || {};
  for (const [filename, data] of Object.entries(geoRaw)) {
    const key  = filename.replace('.geojson', '').toLowerCase();
    const meta = OVERLAY_META[filename] || { defaultOn: false };
    let layer;
    try {
      if (key === 'namesesar') {
        layer = buildFaultNameLayer(data);
      } else if (key === 'volcano') {
        layer = L.geoJSON(data, {
          pane: 'geojsonPane',
          pointToLayer: (feat, latlng) => {
            const name = (feat.properties?.name || '').split('|')[0].trim();
            return L.marker(latlng, { icon: volcanoIcon(), pane: 'geojsonPane' })
              .bindTooltip(`<b>${SW.esc(name)}</b>`, { className: 'sw-tooltip' });
          },
        });
      } else {
        // Faults: the online map uses thin lines (weight 0.6) so quakes/stations stay visible
        const faultW = opts.thinFaults !== false ? 0.6 : null;
        layer = L.geoJSON(data, {
          pane: 'geojsonPane',
          style: feat => {
            const base = { ...(FAULT_STYLE[feat.properties?.tipe || ''] || FAULT_DEFAULT) };
            if (faultW !== null) base.weight = faultW;
            base.pane = 'geojsonPane';
            return base;
          },
          onEachFeature: (feat, lyr) => {
            const tipe = feat.properties?.tipe || 'Unknown';
            lyr.bindTooltip(`<b>${SW.esc(tipe)}</b>`, { className: 'sw-tooltip', sticky: false });
          },
        });
      }
    } catch (e) { continue; }
    if (layer && meta.defaultOn) layer.addTo(map);
  }

  // Layer control (reuse CSS classes .layer-control / .layer-panel / .layer-opt)
  const uid = divId.replace(/[^a-z0-9]/gi, '');
  const ctrlDiv = document.createElement('div');
  ctrlDiv.className = 'layer-control';
  ctrlDiv.style.cssText = 'position:absolute;top:10px;left:10px;z-index:500;pointer-events:auto';
  ctrlDiv.innerHTML = `
    <div class="layer-toggle" id="${uid}-ltbtn" title="Map layers">
      <i class="bi bi-layers"></i>
    </div>
    <div class="layer-panel" id="${uid}-lpanel">
      <div class="layer-sec-title">Basemap</div>
      <div class="layer-opt" id="${uid}-opt-bathy">
        <span class="radio-dot"></span><i class="bi bi-water" style="font-size:.78rem;width:14px"></i> Topo + Bathy
      </div>
      <div class="layer-opt" id="${uid}-opt-topo">
        <span class="radio-dot"></span><i class="bi bi-mountain" style="font-size:.78rem;width:14px"></i> OpenTopoMap
      </div>
      <div class="layer-opt active" id="${uid}-opt-dark">
        <span class="radio-dot"></span><i class="bi bi-moon-stars" style="font-size:.78rem;width:14px"></i> CartoDB Dark
      </div>
    </div>`;
  el.appendChild(ctrlDiv);

  const _togglePanel = () => {
    document.getElementById(`${uid}-lpanel`).classList.toggle('open');
    document.getElementById(`${uid}-ltbtn`).classList.toggle('open');
  };
  const _closePanel = () => {
    document.getElementById(`${uid}-lpanel`)?.classList.remove('open');
    document.getElementById(`${uid}-ltbtn`)?.classList.remove('open');
  };
  const setBase = (name) => {
    if (name === activeBase) { _closePanel(); return; }
    map.removeLayer(baseLayers[activeBase]);
    baseLayers[name].addTo(map);
    activeBase = name;
    ['bathy', 'topo', 'dark'].forEach(n =>
      document.getElementById(`${uid}-opt-${n}`)?.classList.toggle('active', n === name)
    );
    _closePanel();
  };

  document.getElementById(`${uid}-ltbtn`).onclick = _togglePanel;
  document.getElementById(`${uid}-opt-bathy`).onclick = () => setBase('bathy');
  document.getElementById(`${uid}-opt-topo`).onclick  = () => setBase('topo');
  document.getElementById(`${uid}-opt-dark`).onclick  = () => setBase('dark');

  // Close the panel on outside click
  map.on('click', _closePanel);

  // Compass
  SW.addStaticCompass(el, 'br');

  // The map is often created right after the panel is unhidden via classList (online-monitor.js
  // _showPanel) — Leaflet computes a 0x0 size when the container is not yet laid out
  // by the browser. invalidateSize after a short delay fixes empty basemap tiles.
  setTimeout(() => map.invalidateSize(), 150);

  return { map, setBase };
};

function toggleOverlay(key) {
  const ov = SW.overlays[key];
  if (!ov || !ov.layer) return;
  ov.visible = !ov.visible;
  if (ov.visible) ov.layer.addTo(SW.map);
  else SW.map.removeLayer(ov.layer);
  const el = document.getElementById(`opt-ov-${key}`);
  if (el) {
    el.classList.toggle('active', ov.visible);
    el.querySelector('.chk-box').textContent = ov.visible ? '✓' : '';
  }
  updateLegendVisibility();
}

function updateLegendVisibility() {
  const sesarOn = SW.overlays['indogigis']?.visible || SW.overlays['gigis']?.visible;
  const volcanoOn = SW.overlays['volcano']?.visible;
  document.getElementById('map-legend').style.display = sesarOn ? '' : 'none';
  document.getElementById('legend-volcano').style.display = volcanoOn ? '' : 'none';
}

function volcanoIcon() {
  return L.divIcon({
    className: '',
    html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 64 64">
             <!-- eruption cloud -->
             <circle cx="24" cy="10" r="6"  fill="#ff6600" opacity="0.9"/>
             <circle cx="32" cy="6"  r="7"  fill="#ff9900" opacity="0.95"/>
             <circle cx="40" cy="10" r="5.5" fill="#ff6600" opacity="0.9"/>
             <circle cx="32" cy="13" r="5"  fill="#ffcc00" opacity="0.8"/>
             <!-- lava flow left -->
             <path d="M22 32 Q14 42 10 56 L20 56 Q22 46 26 36Z" fill="#cc2200" opacity="0.75"/>
             <!-- lava flow right -->
             <path d="M42 32 Q50 42 54 56 L44 56 Q42 46 38 36Z" fill="#cc2200" opacity="0.75"/>
             <!-- volcano cone -->
             <polygon points="32,15 4,58 60,58" fill="#8B4513" stroke="#5a2d0c" stroke-width="1.5" stroke-linejoin="round"/>
             <!-- snow cap -->
             <polygon points="32,16 26,28 38,28" fill="white" opacity="0.45"/>
             <!-- crater opening -->
             <ellipse cx="32" cy="15" rx="7" ry="2.5" fill="#333" opacity="0.6"/>
           </svg>`,
    iconSize: [22, 22], iconAnchor: [11, 20],
  });
}

// ═══════════════════════════════════════════
//  Layer switcher
// ═══════════════════════════════════════════
function toggleLayerPanel() {
  const panel = document.getElementById('layer-panel');
  const btn = document.getElementById('layer-toggle-btn');
  const open = panel.classList.toggle('open');
  btn.classList.toggle('open', open);
}
function closeLayerPanel() {
  document.getElementById('layer-panel').classList.remove('open');
  document.getElementById('layer-toggle-btn').classList.remove('open');
}
function setBaseLayer(name) {
  if (name === SW.activeBase) { closeLayerPanel(); return; }
  const layers = { bathy: SW.bathyLayer, topo: SW.topoLayer, dark: SW.darkLayer };
  SW.map.removeLayer(layers[SW.activeBase]);
  layers[name].addTo(SW.map);
  SW.activeBase = name;
  ['bathy', 'topo', 'dark'].forEach(n => {
    document.getElementById(`opt-${n}`).classList.toggle('active', n === name);
  });
  closeLayerPanel();
}

// ═══════════════════════════════════════════
//  Draw mode
// ═══════════════════════════════════════════
function toggleDrawMode() { SW.drawMode ? cancelDrawMode() : enableDrawMode(); }

function enableDrawMode() {
  SW.drawMode = true; SW.isDragging = false; SW.drawStart = null;
  SW.map.dragging.disable();
  SW.map.getContainer().style.cursor = 'crosshair';
  document.getElementById('draw-btn').classList.add('drawing');
  document.getElementById('btn-draw-region').classList.add('btn-draw-active');
  document.getElementById('map-hint').classList.add('show');
  clearRegionRect();
}
function cancelDrawMode() {
  SW.drawMode = false; SW.isDragging = false; SW.drawStart = null;
  SW.map.dragging.enable();
  SW.map.getContainer().style.cursor = '';
  document.getElementById('draw-btn').classList.remove('drawing');
  document.getElementById('btn-draw-region').classList.remove('btn-draw-active');
  document.getElementById('map-hint').classList.remove('show');
  if (SW.drawTempRect) { SW.map.removeLayer(SW.drawTempRect); SW.drawTempRect = null; }
}

function onMapMouseDown(e) {
  if (!SW.drawMode) return;
  SW.isDragging = true; SW.drawStart = e.latlng;
  if (SW.drawTempRect) { SW.map.removeLayer(SW.drawTempRect); SW.drawTempRect = null; }
}
function onMapMouseMove(e) {
  if (!SW.drawMode || !SW.isDragging || !SW.drawStart) return;
  const bounds = L.latLngBounds(SW.drawStart, e.latlng);
  if (SW.drawTempRect) SW.drawTempRect.setBounds(bounds);
  else SW.drawTempRect = L.rectangle(bounds, { className: 'region-rect-draw', interactive: false }).addTo(SW.map);
  fillBoundsToInputs(bounds, false);
}
function onMapMouseUp(e) {
  if (!SW.drawMode || !SW.isDragging || !SW.drawStart) return;
  const bounds = L.latLngBounds(SW.drawStart, e.latlng);
  SW.isDragging = false;
  if (Math.abs(bounds.getNorth() - bounds.getSouth()) < 0.01 || Math.abs(bounds.getEast() - bounds.getWest()) < 0.01) { cancelDrawMode(); return; }
  fillBoundsToInputs(bounds, true);
  cancelDrawMode();
  drawFinalRect(bounds);
  addHandles(bounds);
  if (SW.allStations.length) _applyRegionFilter();
}

function fillBoundsToInputs(bounds, flash) {
  const map_ = {
    'lat-min': bounds.getSouth().toFixed(4), 'lat-max': bounds.getNorth().toFixed(4),
    'lon-min': bounds.getWest().toFixed(4), 'lon-max': bounds.getEast().toFixed(4)
  };
  for (const [id, val] of Object.entries(map_)) {
    const el = document.getElementById(id); el.value = val;
    if (flash) { el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 600); }
  }
}
function drawFinalRect(bounds) {
  clearRegionRect();
  SW.regionRect = L.rectangle(bounds, { className: 'region-rect-final', interactive: false }).addTo(SW.map);
}
function clearRegionRect() {
  if (SW.regionRect) { SW.map.removeLayer(SW.regionRect); SW.regionRect = null; }
  clearHandles();
}
function addHandles(bounds) {
  clearHandles();
  const positions = [bounds.getNorthWest(), bounds.getNorthEast(), bounds.getSouthWest(), bounds.getSouthEast()];
  const clss = ['nw', 'ne', 'sw', 'se'];
  const builders = [
    (ll, b) => L.latLngBounds(ll, [b.getSouth(), b.getEast()]),
    (ll, b) => L.latLngBounds(ll, [b.getSouth(), b.getWest()]),
    (ll, b) => L.latLngBounds(ll, [b.getNorth(), b.getEast()]),
    (ll, b) => L.latLngBounds(ll, [b.getNorth(), b.getWest()]),
  ];
  positions.forEach((pos, idx) => {
    const icon = L.divIcon({ className: `handle-marker ${clss[idx]}`, iconSize: [12, 12] });
    const m = L.marker(pos, { icon, draggable: true, zIndexOffset: 1000 }).addTo(SW.map);
    m.on('drag', ev => {
      const cur = SW.regionRect ? SW.regionRect.getBounds() : L.latLngBounds(pos, pos);
      const newB = builders[idx](ev.latlng, cur);
      if (SW.regionRect) SW.regionRect.setBounds(newB);
      fillBoundsToInputs(newB, false);
      const np = [newB.getNorthWest(), newB.getNorthEast(), newB.getSouthWest(), newB.getSouthEast()];
      SW.handles.forEach((h, i) => { if (i !== idx) h.setLatLng(np[i]); });
    });
    m.on('dragend', () => { if (SW.allStations.length) _applyRegionFilter(); });
    SW.handles.push(m);
  });
}
function clearHandles() { SW.handles.forEach(h => SW.map.removeLayer(h)); SW.handles = []; }

// ═══════════════════════════════════════════
//  Region form
// ═══════════════════════════════════════════
function onRegionInput() {
  const r = getRegionForm();
  if (isNaN(r.lat_min) || isNaN(r.lat_max) || isNaN(r.lon_min) || isNaN(r.lon_max)) return;
  const b = L.latLngBounds([r.lat_min, r.lon_min], [r.lat_max, r.lon_max]);
  drawFinalRect(b); addHandles(b);
  if (SW.allStations.length) _applyRegionFilter();
}
function fillRegionForm(cfg) {
  const set = (id, v) => { if (v !== undefined && v !== null && v !== '') document.getElementById(id).value = v; };
  set('cfg-name', cfg.name); set('lat-min', cfg.lat_min); set('lat-max', cfg.lat_max);
  set('lon-min', cfg.lon_min); set('lon-max', cfg.lon_max); set('depth-max', cfg.depth_max);
  if (cfg.starttime) set('starttime', cfg.starttime.slice(0, 16));
  if (cfg.endtime) set('endtime', cfg.endtime.slice(0, 16));
  onRegionInput();
}
function getRegionForm() {
  return {
    name: document.getElementById('cfg-name').value.trim(),
    lat_min: parseFloat(document.getElementById('lat-min').value),
    lat_max: parseFloat(document.getElementById('lat-max').value),
    lon_min: parseFloat(document.getElementById('lon-min').value),
    lon_max: parseFloat(document.getElementById('lon-max').value),
    depth_max: parseFloat(document.getElementById('depth-max').value) || 60,
    starttime: document.getElementById('starttime').value,
    endtime: document.getElementById('endtime').value,
  };
}
function fitMapToRegion() {
  const r = getRegionForm();
  if (isNaN(r.lat_min) || isNaN(r.lon_min)) return;
  onRegionInput();
  SW.map.fitBounds([[r.lat_min, r.lon_min], [r.lat_max, r.lon_max]], { padding: [40, 40] });
}
function setBoundsFromMap() {
  const b = SW.map.getBounds();
  document.getElementById('lat-min').value = b.getSouth().toFixed(4);
  document.getElementById('lat-max').value = b.getNorth().toFixed(4);
  document.getElementById('lon-min').value = b.getWest().toFixed(4);
  document.getElementById('lon-max').value = b.getEast().toFixed(4);
  onRegionInput();
}

// ═══════════════════════════════════════════
//  Stations
// ═══════════════════════════════════════════
function dzDragOver(e) { e.preventDefault(); document.getElementById('drop-zone').classList.add('dragover'); }
function dzDragLeave() { document.getElementById('drop-zone').classList.remove('dragover'); }
function dzDrop(e) { e.preventDefault(); dzDragLeave(); handleCsvFile(e.dataTransfer.files[0]); }

async function handleCsvFile(file) {
  if (!file) return; SW.clearAlert('sta');
  const fd = new FormData(); fd.append('file', file);
  try {
    const res = await fetch('/api/stations/upload', { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) { SW.showAlert('sta', d.error, 'err'); return; }
    SW.currentFdsnUrl = ''; setStations(d.stations);
    // alert shown by _applyRegionFilter — no need to showAlert here
  } catch (e) { SW.showAlert('sta', 'Error: ' + e.message, 'err'); }
}

const FDSN_PRESETS = {
  iris: 'http://service.iris.edu/fdsnws/station/1/query?network=_US-USARRAY&level=station',
  geofon: 'http://geofon.gfz-potsdam.de/fdsnws/station/1/query?level=station',
  resif: 'http://ws.resif.fr/fdsnws/station/1/query?level=station',
};
function fillFdsnPreset(sel) { if (sel.value) document.getElementById('fdsn-url').value = FDSN_PRESETS[sel.value] || ''; sel.value = ''; }

async function fetchFdsn() {
  const url = document.getElementById('fdsn-url').value.trim();
  if (!url) { SW.showAlert('sta', 'URL required.', 'err'); return; }
  const btn = document.getElementById('fdsn-btn');
  btn.disabled = true; btn.innerHTML = '<span class="sw-spinner"></span> Fetching...';
  SW.clearAlert('sta');
  try {
    const d = await (await fetch('/api/stations/fdsn?url=' + encodeURIComponent(url))).json();
    if (!d.stations) { SW.showAlert('sta', d.error, 'err'); return; }
    SW.currentFdsnUrl = url; setStations(d.stations);
    // alert shown by _applyRegionFilter — no need to showAlert here
  } catch (e) { SW.showAlert('sta', 'Error: ' + e.message, 'err'); }
  finally { btn.disabled = false; btn.innerHTML = '<i class="bi bi-cloud-download"></i> Fetch Data'; }
}

function _applyRegionFilter() {
  const r = getRegionForm();
  const hasRegion = !isNaN(r.lat_min) && !isNaN(r.lat_max) && !isNaN(r.lon_min) && !isNaN(r.lon_max);

  // Parse region time window (ISO strings → Date; null = no constraint)
  const tStart = r.starttime ? new Date(r.starttime) : null;
  const tEnd = r.endtime ? new Date(r.endtime) : null;
  const hasTime = tStart || tEnd;

  const filtered = SW.allStations.filter(s => {
    // --- spatial filter ---
    if (hasRegion) {
      if (+s.lat < r.lat_min || +s.lat > r.lat_max) return false;
      if (+s.lon < r.lon_min || +s.lon > r.lon_max) return false;
    }
    // --- temporal filter (only when station has time metadata AND region has time) ---
    if (hasTime && (s.start_time || s.end_time)) {
      const sOn = s.start_time ? new Date(s.start_time) : null;
      const sOff = s.end_time ? new Date(s.end_time) : null;
      // station must overlap region window: sOn <= tEnd  AND  sOff >= tStart
      if (tEnd && sOn && sOn > tEnd) return false;
      if (tStart && sOff && sOff < tStart) return false;
    }
    return true;
  });
  SW.currentStations = filtered;

  document.getElementById('sta-table-body').innerHTML =
    filtered.slice(0, 200).map(s => `<tr>
      <td>${SW.esc(s.network)}</td><td><b>${SW.esc(s.station)}</b></td>
      <td style="text-align:right">${(+s.lat).toFixed(3)}</td>
      <td style="text-align:right">${(+s.lon).toFixed(3)}</td>
      <td style="text-align:right">${(+s.elev).toFixed(0)}</td></tr>`).join('') +
    (filtered.length > 200 ? `<tr><td colspan="5" style="color:var(--text-muted);text-align:center">+${filtered.length - 200} more…</td></tr>` : '');
  plotStations();
  document.getElementById('sta-summary').style.display = SW.allStations.length ? '' : 'none';
  const total = SW.allStations.length;
  const active = filtered.length;
  const outside = total - active;
  document.getElementById('sta-count-badge').textContent = outside > 0 ? `${active} / ${total}` : `${active}`;
  document.getElementById('sta-header-badge').innerHTML = active
    ? `<span class="count-badge count-blue">${active}${outside > 0 ? ` / ${total}` : ''}</span>` : '';
  if (total > 0) {
    if (outside > 0) {
      SW.showAlert('sta', `${active} of ${total} stations active in area/time (${outside} outside ignored)`, 'ok');
    } else if (active > 0) {
      SW.showAlert('sta', `${active} stations — all within area`, 'ok');
    }
  }
}

function setStations(stations) {
  SW.allStations = stations;
  _applyRegionFilter();
}
function plotStations() {
  if (SW.stationLayer) { SW.map.removeLayer(SW.stationLayer); SW.stationLayer = null; }
  if (!SW.allStations.length) return;
  SW.stationLayer = L.layerGroup();
  const palette = { 'IA': '#ff9800', '7G': '#4caf50', 'GE': '#9c27b0', 'IU': '#2196f3', 'II': '#00bcd4' };
  const activeSet = new Set(SW.currentStations.map(s => `${s.network}.${s.station}`));
  SW.allStations.forEach(s => {
    const key = `${s.network}.${s.station}`;
    const active = activeSet.has(key);
    const col = active ? (palette[s.network] || '#e8531a') : '#888';
    const size = active ? 20 : 14;
    const icon = L.divIcon({
      className: '',
      html: `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 20 20" style="opacity:${active ? 1 : 0.35}">
             <polygon points="10,2 1,19 19,19" fill="${col}" stroke="white" stroke-width="1.5" stroke-linejoin="round"/>
           </svg>`,
      iconSize: [size, size], iconAnchor: [size / 2, size],
    });
    const label = active ? '' : ' <span style="color:#aaa;font-size:.75em">(outside area)</span>';
    L.marker([s.lat, s.lon], { icon })
      .bindTooltip(`<b>${SW.esc(s.network)}.${SW.esc(s.station)}</b>${label}<br>${(+s.lat).toFixed(4)}°N &nbsp;${(+s.lon).toFixed(4)}°E<br>Elev: ${(+s.elev).toFixed(0)} m`,
        { className: 'sw-tooltip', sticky: false })
      .addTo(SW.stationLayer);
  });
  SW.stationLayer.addTo(SW.map);
}
function clearStations() {
  SW.allStations = []; SW.currentStations = []; SW.currentFdsnUrl = '';
  if (SW.stationLayer) { SW.map.removeLayer(SW.stationLayer); SW.stationLayer = null; }
  document.getElementById('sta-summary').style.display = 'none';
  document.getElementById('sta-header-badge').innerHTML = '';
  document.getElementById('sta-table-body').innerHTML = '';
  SW.clearAlert('sta');
}

// ═══════════════════════════════════════════
//  Save / load configs
// ═══════════════════════════════════════════
function saveConfig() { openSaveModal(); }

async function loadConfigList() {
  if (SW.OFFLINE.active && SW.OFFLINE.data) { BOOT.renderOfflineConfigList(SW.OFFLINE.data); return; }
  try {
    const cfgs = await (await fetch('/api/configs')).json();
    const el = document.getElementById('config-list');
    document.getElementById('cfg-count-badge').textContent = cfgs.length;
    if (!cfgs.length) { el.innerHTML = '<div class="empty-state"><i class="bi bi-inbox"></i>No configurations yet</div>'; return; }
    // Single global live session (at most one at a time) — if it belongs to
    // one of these configs, that config gets a "Live" button straight to the
    // running dashboard instead of the config-editing wizard.
    let liveCfgId = null;
    try {
      const st = await (await fetch('/api/online/status')).json();
      if (st.connected && st.cfg_id) liveCfgId = st.cfg_id;
    } catch (_) {}
    // When connected to a remote server, every config/session listed lives on
    // that server — flag it so it is never confused with local sessions.
    const remoteTag = SW.REMOTE.base
      ? `<span title="Session on the remote server ${SW.esc(SW.REMOTE.base)}" style="font-size:.58rem;color:#fbbf24;border:1px solid #7a5c1a;border-radius:3px;padding:0 3px;margin-left:.25rem">[remote]</span>`
      : '';
    el.innerHTML = cfgs.map(c => {
      const isOnline = c.type === 'online';
      const _lsErr = c.last_sync && !c.last_sync.ok ? c.last_sync.error : '';
      const _nsTip = _lsErr
        ? `Last sync failed: ${String(_lsErr).replace(/"/g, "'")}`
        : 'Never synced to SeisComP yet — open the config then click Sync';
      const syncBadge = isOnline && c.synced
        ? `<span title="Already synced to SeisComP" style="font-size:.57rem;color:#22c55e;border:1px solid #166534;border-radius:3px;padding:0 3px;margin-left:.25rem">synced</span>`
        : isOnline
          ? `<span title="${_nsTip}" style="font-size:.57rem;color:#f59e0b;border:1px solid #7a5c1a;border-radius:3px;padding:0 3px;margin-left:.25rem;cursor:help">not synced</span>`
          : '';
      // Region-limited config (created via Set Area): flag it clearly so the
      // area clone is not confused with its full-inventory parent.
      const _reg = c.region || {};
      const isArea = isOnline && _reg.limit;
      const areaBadge = isArea
        ? `<span title="Monitoring area only — ${c.n_stations || 0} stations inside the box, center ${(+_reg.lat).toFixed(2)}, ${(+_reg.lon).toFixed(2)}${c.parent_id ? ` (from ${SW.esc(c.parent_id)})` : ''}" style="font-size:.57rem;color:#38bdf8;border:1px solid #0e7490;border-radius:3px;padding:0 3px;margin-left:.25rem;cursor:help"><i class="bi bi-bounding-box" style="font-size:.55rem"></i> area</span>`
        : '';
      const openBtn = isOnline
        ? (c.id === liveCfgId
            ? `<button class="btn-sw btn-green-sw btn-sm-sw" onclick="OM.viewLive('${c.id}')" title="View the running live session"><i class="bi bi-broadcast-pin"></i> Live</button>`
            : `<button class="btn-sw btn-blue-sw btn-sm-sw" onclick="OM.openConfig('${c.id}')" title="Open Online Monitor" style="color:#fff"><i class="bi bi-broadcast"></i></button>`)
        : `<button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="openWorkPage('${c.id}')" title="Open Work Space" style="color:var(--accent2)"><i class="bi bi-grid-1x2"></i></button>
           <button class="btn-sw btn-blue-sw btn-sm-sw" onclick="loadConfig('${c.id}')" title="Load"><i class="bi bi-folder2-open"></i></button>`;
      const syncBtn = !isOnline && SW.REMOTE.base
        ? `<button class="btn-sw btn-ghost-sw btn-sm-sw" id="syncbtn-${c.id}" onclick="BOOT.syncOfflineCfg('${c.id}')" title="Sync offline" style="color:#22d3ee"><i class="bi bi-cloud-download"></i></button>`
        : '';
      return `
      <div class="cfg-item ${c.id === SW.activeConfigId ? 'active-cfg' : ''}" id="cfgitem-${c.id}">
        <span class="cfg-id">
          ${isOnline ? '<i class="bi bi-broadcast" style="color:#22c55e;margin-right:.2rem"></i>' : ''}${SW.esc(c.id)}${remoteTag}
        </span>
        <div class="cfg-info">
          <div class="cfg-name">${SW.esc(c.name)}${areaBadge}${syncBadge}</div>
          <div class="cfg-meta">${c.n_stations || 0} sta${isArea ? ' in area' : ''} &nbsp;·&nbsp; ${(c.created || '').slice(0, 16).replace('T', ' ')}</div>
        </div>
        <div class="cfg-actions">
          ${openBtn}${syncBtn}
          <button class="btn-sw btn-danger-sw btn-sm-sw" onclick="deleteConfig('${c.id}')" title="Delete"><i class="bi bi-trash3"></i></button>
        </div>
        ${!isOnline && SW.REMOTE.base ? `<div class="cfg-sync-bar" id="syncbar-${c.id}"><div class="cfg-sync-bar-fill" id="syncfill-${c.id}" style="width:0%"></div></div>` : ''}
      </div>`;
    }).join('');
  } catch { }
}
// Offline mode (syncOfflineCfg/_showOfflineBanner/_retryConnect/_renderOfflineConfigList/_offlineOpenWork) → modules/bootstrap-ui.js (BOOT)

async function loadConfig(id) {
  const res = await fetch(`/api/configs/${id}`);
  const c = await res.json();
  if (!res.ok) return;
  SW.activeConfigId = id;
  SW.activeProjectName = c.name || '';
  fillRegionForm({ ...c.region, name: c.name });
  if (c.stations?.length) setStations(c.stations);
  if (c.fdsn_url) { document.getElementById('fdsn-url').value = c.fdsn_url; switchStaTab('fdsn'); }
  if (c.waveform && Object.keys(c.waveform).length) fillWaveformForm(c.waveform);
  fitMapToRegion();
  document.querySelectorAll('.cfg-item').forEach(e => e.classList.remove('active-cfg'));
  document.getElementById(`cfgitem-${id}`)?.classList.add('active-cfg');
  _updateProjectBadge();
  try { refreshSlabOverlay(); } catch (e) {}   // follow the new AOI if slab layer is on
  SW.showAlert('save', `Loaded: "${SW.esc(c.name)}"`, 'ok');
}

// ── Project (config = project) — navbar badge + New Project ─────────────────────
function _updateProjectBadge() {
  const el = document.getElementById('sw-project-badge');
  if (!el) return;
  if (SW.activeConfigId) {
    el.innerHTML = `● ${SW.esc(SW.activeProjectName || 'Project')} `
      + `<span style="opacity:.7;font-family:monospace">${SW.esc(String(SW.activeConfigId).slice(0, 8))}</span>`;
    el.style.background = '#2f7d4f';
    el.title = `Active project: ${SW.activeProjectName || ''} (${SW.activeConfigId}) — all jobs under this ID`;
  } else {
    el.innerHTML = '● No project';
    el.style.background = '#5a5a5a';
    el.title = 'No active project — click New Project, or Load a config';
  }
}

function newProject() {
  if (!confirm('Start new project?\nWorkspace will close & region/station forms will be cleared. Fill in and click Save to create a new project ID (cfg_id).')) return;
  try { closeWorkPage(); } catch (e) { }
  SW.activeConfigId = null; SW.wpCfgId = null; SW.activeProjectName = '';
  try { const n = document.getElementById('cfg-name'); if (n) n.value = ''; } catch (e) { }
  try { setStations([]); } catch (e) { }
  try { document.querySelectorAll('.cfg-item').forEach(e => e.classList.remove('active-cfg')); } catch (e) { }
  _updateProjectBadge();
  try { loadConfigList(); } catch (e) { }
  const nm = document.getElementById('cfg-name');
  if (nm) { nm.focus(); nm.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  SW.showAlert('save', 'New project — fill in region & stations, then Save to create a project ID', 'ok');
}

// ── Projects dropdown (recent projects = saved configs) ─────────────────────────
async function toggleProjectMenu(ev) {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('sw-project-menu');
  if (!m) return;
  if (m.classList.contains('open')) { m.classList.remove('open'); return; }
  await renderProjectMenu();
  m.classList.add('open');
}
async function renderProjectMenu() {
  const m = document.getElementById('sw-project-menu');
  if (!m) return;
  m.innerHTML = '<div class="pm-hdr">Loading…</div>';
  try {
    const cfgs = await (await fetch('/api/configs')).json();
    if (!cfgs.length) { m.innerHTML = '<div class="pm-hdr">No projects yet — click "New Project".</div>'; return; }
    m.innerHTML = `<div class="pm-hdr">Recent projects (${cfgs.length})</div>`
      + cfgs.map(c => {
        const act = c.id === SW.activeConfigId ? ' pm-active' : '';
        const created = (c.created || '').slice(0, 16).replace('T', ' ');
        return `<div class="pm-item${act}" onclick="_openProjectFromMenu('${SW.esc(c.id)}')">
          <div class="pm-name">${SW.esc(c.name || c.id)}${act ? ' · active' : ''}</div>
          <div class="pm-meta">${SW.esc(c.id)} · ${c.n_stations || 0} sta · ${SW.esc(created)}</div>
        </div>`;
      }).join('');
  } catch (e) {
    m.innerHTML = '<div class="pm-hdr" style="color:#f87171">Failed to load project list.</div>';
  }
}
function _openProjectFromMenu(id) {
  document.getElementById('sw-project-menu')?.classList.remove('open');
  try { openWorkPage(id); } catch (e) { try { loadConfig(id); } catch (_) { } }
}
document.addEventListener('click', (e) => {
  const m = document.getElementById('sw-project-menu');
  if (m && m.classList.contains('open')
      && !e.target.closest('#sw-project-menu')
      && !e.target.closest('[onclick*="toggleProjectMenu"]')) {
    m.classList.remove('open');
  }
});
async function deleteConfig(id) {
  if (!confirm(`Delete configuration ${id}?`)) return;
  await fetch(`/api/configs/${id}`, { method: 'DELETE' });
  if (SW.activeConfigId === id) { SW.activeConfigId = null; SW.activeProjectName = ''; _updateProjectBadge(); }
  await loadConfigList();
}

// ═══════════════════════════════════════════
//  UI helpers
// ═══════════════════════════════════════════
function toggleCard(id) {
  const hdr = document.querySelector(`#card-${id} .sw-card-header`);
  const body = document.getElementById(`body-${id}`);
  const c = hdr.classList.toggle('collapsed');
  body.classList.toggle('hidden', c);
}
function switchStaTab(tab) {
  document.getElementById('tab-csv').style.display = tab === 'csv' ? '' : 'none';
  document.getElementById('tab-fdsn').style.display = tab === 'fdsn' ? '' : 'none';
  document.getElementById('tab-csv-btn').classList.toggle('active', tab === 'csv');
  document.getElementById('tab-fdsn-btn').classList.toggle('active', tab === 'fdsn');
}
// showAlert/clearAlert/esc → SeisWorkCore (SW.showAlert/SW.clearAlert/SW.esc)

// ═══════════════════════════════════════════
//  Waveform
// ═══════════════════════════════════════════
let wvPollTimer = null;
let _modalWvInfo = null;
let _saveMode = 'new';
let _lastJobs = {};

function switchWvTab(tab) {
  document.getElementById('tab-wv-path').style.display = tab === 'path' ? '' : 'none';
  document.getElementById('tab-wv-fdsn').style.display = tab === 'fdsn' ? '' : 'none';
  document.getElementById('tab-wvp-btn').classList.toggle('active', tab === 'path');
  document.getElementById('tab-wvf-btn').classList.toggle('active', tab === 'fdsn');
}
function fillWvClient(sel) {
  if (sel.value) { document.getElementById('wv-client').value = sel.value; document.getElementById('wv-client-status').innerHTML = ''; }
  sel.value = '';
}
function onWvAuthToggle(chk) {
  const fields = document.getElementById('wv-auth-fields');
  fields.style.display = chk.checked ? 'flex' : 'none';
  if (!chk.checked) {
    document.getElementById('wv-username').value = '';
    document.getElementById('wv-password').value = '';
  }
}
async function testWvClient() {
  const url = document.getElementById('wv-client').value.trim();
  const el = document.getElementById('wv-client-status');
  const btn = document.getElementById('btn-test-client');
  if (!url) { el.innerHTML = '<span style="color:#f44336">Empty URL/code</span>'; return; }
  btn.disabled = true;
  el.innerHTML = '<span class="sw-spinner"></span> Checking…';
  try {
    const res = await fetch('/api/waveform/test-client?url=' + encodeURIComponent(url));
    const d = await res.json();
    if (res.ok) {
      el.innerHTML = `<span style="color:#4caf50"><i class="bi bi-check-circle"></i> OK`
        + (d.version ? ` &nbsp;·&nbsp; v${SW.esc(d.version)}` : '')
        + `</span> <span style="color:var(--text-muted);font-size:.68rem">${SW.esc(d.base_url)}</span>`;
    } else {
      el.innerHTML = `<span style="color:#f44336"><i class="bi bi-x-circle"></i> ${SW.esc(d.error)}</span>`;
    }
  } catch (e) {
    el.innerHTML = `<span style="color:#f44336">Error: ${SW.esc(e.message)}</span>`;
  } finally { btn.disabled = false; }
}
function fillWvFromStations() {
  if (!SW.currentStations.length) { alert('Load stations first'); return; }
  document.getElementById('wv-net').value = [...new Set(SW.currentStations.map(s => s.network))].join(',');
  document.getElementById('wv-sta').value = [...new Set(SW.currentStations.map(s => s.station))].join(',');
}
async function testLocalPath() {
  const path = document.getElementById('wv-local-path').value.trim();
  const type = document.getElementById('wv-path-type').value;
  const el = document.getElementById('wv-path-status');
  const btn = document.getElementById('btn-test-path');
  if (!path) { el.innerHTML = '<span style="color:#f44336">Empty path</span>'; return; }
  btn.disabled = true;
  el.innerHTML = '<span class="sw-spinner"></span>';
  try {
    const res = await fetch('/api/waveform/test-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, type }),
    });
    const d = await res.json();
    if (res.ok) {
      _modalWvInfo = { source: 'path', files: d.n_files, size_mb: d.total_size_mb, path };
      el.innerHTML = `<span style="color:#4caf50"><i class="bi bi-check-circle"></i> ${d.n_files} files &nbsp;·&nbsp; ${d.total_size_mb} MB</span>`
        + ` <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="openSaveModal()" style="padding:1px 6px;font-size:.66rem;margin-left:.35rem"><i class="bi bi-save2"></i> Save Config</button>`;
      const badge = document.getElementById('wv-header-badge');
      if (badge) badge.innerHTML = '<span class="count-badge count-orange" style="font-size:.65rem">PATH</span>';
    } else {
      el.innerHTML = `<span style="color:#f44336"><i class="bi bi-x-circle"></i> ${SW.esc(d.error)}</span>`;
    }
  } catch (e) {
    el.innerHTML = `<span style="color:#f44336">Error: ${SW.esc(e.message)}</span>`;
  } finally { btn.disabled = false; }
}
function onWvChunkSel(sel) {
  document.getElementById('wv-chunk-manual').style.display = sel.value === '0' ? '' : 'none';
}
function getWvChunkHours() {
  const sel = document.getElementById('wv-chunk-sel');
  if (sel.value === '0') return parseInt(document.getElementById('wv-chunk-manual').value) || 24;
  return parseInt(sel.value);
}
function syncWvTime() {
  const r = getRegionForm();
  if (r.starttime) document.getElementById('wv-starttime').value = r.starttime.slice(0, 16);
  if (r.endtime) document.getElementById('wv-endtime').value = r.endtime.slice(0, 16);
}
function _wvChunkLabel(h) {
  const MAP = {
    '1': 'Per hour', '6': 'Per 6h', '12': 'Per 12h', '24': 'Per day',
    '168': 'Per week', '360': 'Per 15d', '720': 'Per month',
    '2160': 'Per 3mo', '4380': 'Per 6mo', '8760': 'Per year'
  };
  return MAP[String(h)] || `${h}h`;
}
function getWaveformConfig() {
  const isFdsn = document.getElementById('tab-wv-fdsn').style.display !== 'none';
  return {
    source: isFdsn ? 'fdsn' : 'path',
    path: document.getElementById('wv-local-path').value.trim(),
    path_type: document.getElementById('wv-path-type').value,
    fdsn: {
      client: document.getElementById('wv-client').value.trim(),
      networks: document.getElementById('wv-net').value.trim(),
      stations: document.getElementById('wv-sta').value.trim(),
      channels: document.getElementById('wv-cha').value.trim(),
      location: document.getElementById('wv-loc').value.trim(),
      chunk_hours: getWvChunkHours(),
      output_path: document.getElementById('wv-outpath').value.trim(),
      starttime: document.getElementById('wv-starttime').value,
      endtime: document.getElementById('wv-endtime').value,
    },
  };
}
function fillWaveformForm(wv) {
  if (!wv) return;
  if (wv.path) document.getElementById('wv-local-path').value = wv.path;
  if (wv.path_type) document.getElementById('wv-path-type').value = wv.path_type;
  const f = wv.fdsn || {};
  if (f.client) document.getElementById('wv-client').value = f.client;
  if (f.networks) document.getElementById('wv-net').value = f.networks;
  if (f.stations) document.getElementById('wv-sta').value = f.stations;
  if (f.channels) document.getElementById('wv-cha').value = f.channels;
  if (f.location) document.getElementById('wv-loc').value = f.location;
  if (f.output_path) document.getElementById('wv-outpath').value = f.output_path;
  if (f.starttime) document.getElementById('wv-starttime').value = f.starttime.slice(0, 16);
  if (f.endtime) document.getElementById('wv-endtime').value = f.endtime.slice(0, 16);
  if (f.chunk_hours) {
    const sel = document.getElementById('wv-chunk-sel');
    const opt = [...sel.options].find(o => o.value === String(f.chunk_hours));
    if (opt) { sel.value = f.chunk_hours; document.getElementById('wv-chunk-manual').style.display = 'none'; }
    else {
      sel.value = '0'; document.getElementById('wv-chunk-manual').value = f.chunk_hours;
      document.getElementById('wv-chunk-manual').style.display = '';
    }
  }
  switchWvTab(wv.source === 'fdsn' ? 'fdsn' : 'path');
  const badge = document.getElementById('wv-header-badge');
  if (badge) badge.innerHTML = `<span class="count-badge count-orange" style="font-size:.65rem">${wv.source === 'fdsn' ? 'FDSN' : 'PATH'}</span>`;
}
async function startWvDownload() {
  const cfg = getWaveformConfig();
  let starttime = cfg.fdsn.starttime;
  let endtime = cfg.fdsn.endtime;
  if (!starttime || !endtime) {
    const region = getRegionForm();
    starttime = starttime || region.starttime;
    endtime = endtime || region.endtime;
  }
  if (!cfg.fdsn.client) { alert('FDSN client required'); return; }
  if (!cfg.fdsn.networks) { alert('Network required'); return; }
  if (!starttime) { alert('Start time required'); return; }
  if (!endtime) { alert('End time required'); return; }
  const params = { ...cfg.fdsn, starttime: starttime.replace('T', ' '), endtime: endtime.replace('T', ' ') };
  // Parallel workers
  params.parallel_workers = parseInt(document.getElementById('wv-parallel').value) || 4;
  // Credentials — only sent to the server, never saved into the config
  const authChk = document.getElementById('wv-auth-chk');
  if (authChk && authChk.checked) {
    params.username = document.getElementById('wv-username').value.trim();
    params.password = document.getElementById('wv-password').value;
  }
  const btn = document.getElementById('btn-wv-start');
  btn.disabled = true; btn.innerHTML = '<span class="sw-spinner"></span> Starting…';
  try {
    const res = await fetch('/api/waveform/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    const d = await res.json();
    if (!res.ok) { alert('Error: ' + (d.error || 'Unknown')); return; }
    await refreshJobs();
    startWvPoll();
    const hdr = document.querySelector('#card-waveform .sw-card-header');
    if (hdr.classList.contains('collapsed')) toggleCard('waveform');
  } catch (e) { alert('Error: ' + e.message); }
  finally { btn.disabled = false; btn.innerHTML = '<i class="bi bi-cloud-arrow-down"></i> Start Download'; }
}
function retryAuthJob(jobId) {
  const j = _lastJobs[jobId];
  if (!j || !j.params) return;
  // Pre-fill the form from the params of the job that failed auth
  const p = j.params;
  if (p.client) document.getElementById('wv-client').value = p.client;
  if (p.networks) document.getElementById('wv-net').value = p.networks;
  if (p.stations) document.getElementById('wv-sta').value = p.stations;
  if (p.channels) document.getElementById('wv-cha').value = p.channels;
  if (p.location) document.getElementById('wv-loc').value = p.location;
  if (p.output_path) document.getElementById('wv-outpath').value = p.output_path;
  if (p.starttime) document.getElementById('wv-starttime').value = p.starttime.slice(0, 16).replace(' ', 'T');
  if (p.endtime) document.getElementById('wv-endtime').value = p.endtime.slice(0, 16).replace(' ', 'T');
  if (p.parallel_workers) document.getElementById('wv-parallel').value = String(p.parallel_workers);
  // Enable the auth checkbox and clear the fields — the user must re-enter them
  const authChk = document.getElementById('wv-auth-chk');
  authChk.checked = true;
  document.getElementById('wv-auth-fields').style.display = 'flex';
  document.getElementById('wv-username').value = '';
  document.getElementById('wv-password').value = '';
  switchWvTab('fdsn');
  // Scroll & expand card waveform
  const hdr = document.querySelector('#card-waveform .sw-card-header');
  if (hdr && hdr.classList.contains('collapsed')) toggleCard('waveform');
  document.getElementById('wv-username').focus();
}
async function stopJob(id) { await fetch(`/api/waveform/jobs/${id}`, { method: 'DELETE' }); await refreshJobs(); }
async function removeJob(id) { await fetch(`/api/waveform/jobs/${id}`, { method: 'DELETE' }); await refreshJobs(); }
async function refreshJobs() {
  if (SW.OFFLINE.active) return;
  try {
    const jobs = await (await fetch('/api/waveform/jobs')).json();
    renderJobs(jobs);
    if (!jobs.some(j => j.state === 'running')) stopWvPoll();
  } catch { }
}
function renderJobs(jobs) {
  const wrap = document.getElementById('wv-jobs-wrap');
  const list = document.getElementById('wv-jobs-list');
  const cnt = document.getElementById('wv-jobs-count');
  wrap.style.display = jobs.length ? '' : 'none';
  if (cnt) cnt.textContent = jobs.length;
  jobs.forEach(j => { _lastJobs[j.id] = j; });
  const LABEL = { running: 'Running', done: 'Done', error: 'Error', stopped: 'Stopped', auth_error: 'Auth Required' };
  list.innerHTML = jobs.map(j => {
    const pct = j.total > 0 ? Math.round(j.done / j.total * 100) : 0;
    const cls = `js-${j.state}`;
    const nw  = j.parallel_workers > 1 ? ` · ${j.parallel_workers} threads` : '';
    const skipTxt  = j.skipped > 0 ? ` · <span style="color:#64b5f6">${j.skipped} skipped</span>` : '';
    const errTxt   = j.errors  > 0 ? ` · <span style="color:#f44336">${j.errors} error(s)</span>` : '';
    const unit     = 'station-days';
    return `<div class="job-card">
      <div class="job-card-hdr">
        <span style="font-family:monospace;font-size:.67rem;color:var(--text-muted)">${SW.esc(j.id)}</span>
        <span class="job-state ${cls}">${LABEL[j.state] || j.state}</span>
        ${j.state === 'running'
        ? `<button class="btn-sw btn-danger-sw btn-sm-sw" onclick="stopJob('${j.id}')" style="margin-left:auto"><i class="bi bi-stop-fill"></i> Stop</button>`
        : `<button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="removeJob('${j.id}')" style="margin-left:auto" title="Remove"><i class="bi bi-x-lg"></i></button>`
      }
      </div>

      ${(j.state === 'running' || j.state === 'done' || j.state === 'stopped') ? `
        <div class="progress-wrap"><div class="progress-fill" style="width:${j.state==='done'?100:pct}%"></div></div>
        <div style="color:var(--text-muted);font-size:.75rem;margin-top:.15rem">
          ${j.done}/${j.total} ${unit} (${j.state==='done'?100:pct}%)${nw}${skipTxt}${errTxt}
        </div>
        ${j.current ? `<div style="color:var(--accent);font-size:.72rem;margin-top:.2rem;word-break:break-all">
          <i class="bi bi-geo-alt"></i> ${SW.esc(j.current)}
        </div>` : ''}
        ${j.state !== 'running' && j.output_path ? `
        <div style="display:flex;align-items:center;gap:.4rem;margin-top:.25rem;flex-wrap:wrap">
          <i class="bi bi-folder2-open" style="color:#4caf50"></i>
          <span style="font-size:.68rem;color:var(--text-muted);word-break:break-all;flex:1">${SW.esc(j.output_path)}</span>
          <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="saveFromJob('${j.id}')" style="padding:1px 6px;font-size:.66rem;flex-shrink:0">
            <i class="bi bi-save2"></i> Save Config
          </button>
        </div>` : ''}` : ''}

      ${j.state === 'error' ? `<div style="color:#f44336;margin-top:.2rem">${SW.esc(j.error || '')}</div>` : ''}
      ${j.state === 'auth_error' ? `
        <div style="color:#ff9800;margin-top:.2rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
          <i class="bi bi-shield-lock"></i>
          <span>${SW.esc(j.error || 'Authentication required or credentials invalid')}</span>
          <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="retryAuthJob('${j.id}')" style="padding:1px 8px;font-size:.66rem;color:#ff9800;border-color:#ff9800;margin-left:auto">
            <i class="bi bi-key"></i> Re-enter Credentials
          </button>
        </div>
        ${j.total > 0 ? `<div style="color:var(--text-muted);font-size:.72rem;margin-top:.2rem">
          ${j.done}/${j.total} ${unit} before error${skipTxt}${errTxt}
        </div>` : ''}` : ''}

      <div style="color:var(--text-muted);font-size:.67rem;margin-top:.2rem">
        ${SW.esc(j.params?.client || '')} · ${SW.esc(j.params?.networks || '')} · ${SW.esc(j.params?.starttime || '')} – ${SW.esc(j.params?.endtime || '')}
      </div>
    </div>`;
  }).join('');
}
function startWvPoll() { if (wvPollTimer) return; wvPollTimer = setInterval(refreshJobs, 2000); }
function stopWvPoll() { if (wvPollTimer) { clearInterval(wvPollTimer); wvPollTimer = null; } }

// ═══════════════════════════════════════════
//  QuakeLink / FDSN-Event Catalog Download
// ═══════════════════════════════════════════
let qlPollTimer = null;

function fillQlServer(sel) {
  if (sel.value) document.getElementById('ql-server').value = sel.value;
  sel.value = '';
}

function onQlArrivalsChange() {
  const hasArr = document.getElementById('ql-arrivals').checked;
  const phaRow = document.getElementById('ql-pha-row');
  if (phaRow) phaRow.style.opacity = hasArr ? '1' : '0.4';
  // auto-uncheck convert_pha when arrivals are not included
  if (!hasArr) {
    const chk = document.getElementById('ql-convert-pha');
    if (chk) chk.checked = false;
  }
}

function qlCopyRegionTime() {
  const r = getRegionForm();
  if (r.starttime) document.getElementById('ql-starttime').value = r.starttime.slice(0,16).replace(' ','T');
  if (r.endtime)   document.getElementById('ql-endtime').value   = r.endtime.slice(0,16).replace(' ','T');
}

function qlCopyRegionBbox() {
  const r = getRegionForm();
  if (!isNaN(r.lat_min)) document.getElementById('ql-latmin').value = r.lat_min;
  if (!isNaN(r.lat_max)) document.getElementById('ql-latmax').value = r.lat_max;
  if (!isNaN(r.lon_min)) document.getElementById('ql-lonmin').value = r.lon_min;
  if (!isNaN(r.lon_max)) document.getElementById('ql-lonmax').value = r.lon_max;
}

async function startQlDownload() {
  const server = (document.getElementById('ql-server').value || '').trim();
  const starttime = document.getElementById('ql-starttime').value;
  const endtime   = document.getElementById('ql-endtime').value;
  if (!server)    { alert('Server URL required'); return; }
  if (!starttime) { alert('Starttime required'); return; }
  if (!endtime)   { alert('Endtime required'); return; }

  const params = {
    base_url      : server,
    starttime     : starttime.replace('T', ' '),
    endtime       : endtime.replace('T', ' '),
    minlatitude   : document.getElementById('ql-latmin').value || undefined,
    maxlatitude   : document.getElementById('ql-latmax').value || undefined,
    minlongitude  : document.getElementById('ql-lonmin').value || undefined,
    maxlongitude  : document.getElementById('ql-lonmax').value || undefined,
    mindepth      : document.getElementById('ql-depmin').value || undefined,
    maxdepth      : document.getElementById('ql-depmax').value || undefined,
    minmagnitude  : document.getElementById('ql-magmin').value || undefined,
    maxmagnitude  : document.getElementById('ql-magmax').value || undefined,
    magnitudetype : document.getElementById('ql-magtype').value || 'M',
    minphases     : document.getElementById('ql-phmin').value || undefined,
    maxphases     : document.getElementById('ql-phmax').value || undefined,
    include_arrivals: document.getElementById('ql-arrivals').checked,
    format        : document.getElementById('ql-format').value || 'xml',
    convert_pha   : document.getElementById('ql-convert-pha').checked
                    && document.getElementById('ql-arrivals').checked
                    && (document.getElementById('ql-format').value || 'xml') === 'xml',
    output_path   : document.getElementById('ql-outpath').value.trim() || undefined,
    cfg_id        : SW.activeConfigId || '',
  };
  // remove undefined
  Object.keys(params).forEach(k => params[k] === undefined && delete params[k]);

  const btn = document.getElementById('btn-ql-start');
  btn.disabled = true;
  btn.innerHTML = '<span class="sw-spinner"></span> Starting…';
  try {
    const res = await fetch('/api/catalog/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    const d = await res.json();
    if (!res.ok) { alert('Error: ' + (d.error || 'Unknown')); return; }
    await refreshQlJobs();
    startQlPoll();
    const hdr = document.querySelector('#card-catalog-dl .sw-card-header');
    if (hdr && hdr.classList.contains('collapsed')) toggleCard('catalog-dl');
  } catch (e) { alert('Error: ' + e.message); }
  finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-cloud-arrow-down"></i> Download Catalog';
  }
}

let _qlJobsCache = [];

async function refreshQlJobs() {
  if (SW.OFFLINE.active || !SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/catalog/jobs?cfg_id=${encodeURIComponent(SW.activeConfigId)}`)).json();
    _qlJobsCache = jobs;
    renderQlJobs(jobs);
    if (!jobs.some(j => j.state === 'running' || j.state === 'converting')) stopQlPoll();
    // also refresh the selector in Overview
    _syncOvCatJobSel(jobs);
  } catch { }
}

function renderQlJobs(jobs) {
  const wrap = document.getElementById('ql-jobs-wrap');
  const list = document.getElementById('ql-jobs-list');
  const cnt  = document.getElementById('ql-jobs-count');
  if (!wrap) return;
  wrap.style.display = jobs.length ? '' : 'none';
  if (cnt) cnt.textContent = jobs.length;
  const QLABEL = { running: 'Downloading', converting: 'Converting…', done: 'Done', error: 'Error', stopped: 'Stopped' };
  list.innerHTML = jobs.map(j => {
    const isActive = j.state === 'running' || j.state === 'converting';
    const cls = j.state === 'running' ? 'js-downloading'
              : j.state === 'converting' ? 'js-running'
              : `js-${j.state}`;
    const pct   = j.total_bytes > 0 ? Math.round(j.downloaded / j.total_bytes * 100) : -1;
    const dlMb  = j.downloaded ? (j.downloaded / 1024 / 1024).toFixed(1) : '0';
    const totMb = j.total_bytes ? (j.total_bytes / 1024 / 1024).toFixed(1) : '?';
    const xmlName = j.output_file ? j.output_file.split('/').pop() : '';
    const phaName = j.pha_file   ? j.pha_file.split('/').pop()   : '';
    return `<div class="job-card">
      <div class="job-card-hdr">
        <span style="font-family:monospace;font-size:.67rem;color:var(--text-muted)">${SW.esc(j.id)}</span>
        <span class="job-state ${cls}">${QLABEL[j.state] || j.state}</span>
        ${isActive
          ? `<button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="removeQlJob('${j.id}')" style="margin-left:auto" title="Cancel"><i class="bi bi-x-lg"></i></button>`
          : `<button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="removeQlJob('${j.id}')" style="margin-left:auto" title="Remove"><i class="bi bi-trash"></i></button>`
        }
      </div>
      ${j.state === 'running' && pct >= 0 ? `
        <div class="progress-wrap"><div class="progress-fill" style="width:${pct}%"></div></div>
        <div style="color:var(--text-muted)">${dlMb} / ${totMb} MB (${pct}%)</div>` : ''}
      ${j.state === 'running' && pct < 0 ? `<div style="color:var(--text-muted)">${dlMb} MB downloaded…</div>` : ''}
      ${j.state === 'converting' ? `<div style="color:var(--accent2)"><span class="sw-spinner"></span> Converting XML → PHA…</div>` : ''}
      ${j.state === 'done' ? `
        <div style="margin-top:.2rem;display:flex;flex-direction:column;gap:.2rem">
          <div style="display:flex;align-items:center;gap:.35rem;flex-wrap:wrap">
            <i class="bi bi-file-earmark-code" style="color:#4caf50;font-size:.8rem"></i>
            <span style="color:#4caf50;font-size:.7rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${SW.esc(j.output_file || '')}">${SW.esc(xmlName)}</span>
            <span style="color:var(--text-muted);font-size:.66rem">${dlMb} MB</span>
            <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="viewCatalogStatsFromJob('${j.id}')" style="font-size:.65rem;padding:1px 7px;flex-shrink:0">
              <i class="bi bi-bar-chart-line"></i> Statistik
            </button>
            ${(!j.params?.format || j.params.format === 'xml') ? `
            <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="openQlCatalogInResultModal('${j.id}')" style="font-size:.65rem;padding:1px 7px;flex-shrink:0;color:var(--accent2);border-color:var(--accent2)">
              <i class="bi bi-map"></i> Result View
            </button>` : ''}
          </div>
          ${j.pha_file ? `
          <div style="display:flex;align-items:center;gap:.35rem">
            <i class="bi bi-file-earmark-text" style="color:var(--accent2);font-size:.8rem"></i>
            <span style="color:var(--accent2);font-size:.7rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${SW.esc(j.pha_file)}">${SW.esc(phaName)}</span>
            <span style="color:var(--text-muted);font-size:.63rem">PHA</span>
          </div>` : ''}
          ${j.pha_error ? `<div style="color:#ff9800;font-size:.67rem"><i class="bi bi-exclamation-triangle"></i> PHA error: ${SW.esc(j.pha_error)}</div>` : ''}
        </div>` : ''}
      ${j.state === 'error' ? `<div style="color:#f44336;margin-top:.15rem">${SW.esc(j.error || '')}</div>` : ''}
      <div style="color:var(--text-muted);font-size:.67rem;margin-top:.2rem">
        ${SW.esc((j.params?.base_url || '').replace(/^https?:\/\//, ''))} · ${SW.esc(j.params?.starttime || '')} – ${SW.esc(j.params?.endtime || '')}
        · ${SW.esc(j.params?.format || 'xml')}${j.params?.convert_pha ? ' + PHA' : ''}
      </div>
    </div>`;
  }).join('');
}

async function removeQlJob(id) {
  await fetch(`/api/catalog/jobs/${id}`, { method: 'DELETE' });
  await refreshQlJobs();
}

function startQlPoll() { if (qlPollTimer) return; qlPollTimer = setInterval(refreshQlJobs, 2500); }
function stopQlPoll()  { if (qlPollTimer) { clearInterval(qlPollTimer); qlPollTimer = null; } }

// ── Overview Catalog tab ──────────────────────────────────────────

function switchProjectMode(mode) {
  if (mode === SW.projectMode) return;
  SW.projectMode = mode;
  document.getElementById('sw-mode-offline').classList.toggle('active', mode === 'offline');
  document.getElementById('sw-mode-online').classList.toggle('active', mode === 'online');
  document.getElementById('main-wrap').style.display = mode === 'offline' ? '' : 'none';
  document.getElementById('sw-navbar-offline-controls').style.display = mode === 'offline' ? 'flex' : 'none';
  document.getElementById('sw-online-root').classList.toggle('hidden', mode !== 'online');
  // Load balancing: pause online polling when switching to offline — the server is
  // not kept busy fetching waveforms every 2 s while the user views the offline tab.
  // The client buffer is preserved (_clientBufs/_lastEpoch are not reset) so that
  // returning to online simply resumes from the last epoch (delta fetch).
  if (typeof OM !== 'undefined') {
    if (mode === 'offline') {
      OM._pausePoll?.();
      // Also hide the offline loading banner that may appear on top
      // while the user is on the online tab (the banner is shown by BOOT, which
      // does not know where the user is).
    } else if (OM._wvFirstDrawDone) {
      // Already visited the dashboard this session → resume (client buffer preserved).
      OM._resumePoll?.();
    } else {
      // Never connected this session → if the GLOBAL server session is live,
      // restore the dashboard + history (snapshot) to MATCH other users,
      // instead of starting at home/wizard. The waveform session is shared by all users.
      OM.restoreIfLive?.();
    }
  }
  // Offline banner (BOOT): show only in offline mode
  const banner = document.getElementById('sw-offline-banner');
  if (banner) banner.style.display = mode === 'offline' ? '' : 'none';
}

function switchOvRightTab(tab) {
  document.getElementById('ov-right-cov').style.display = tab === 'cov' ? '' : 'none';
  document.getElementById('ov-right-cat').style.display = tab === 'cat' ? '' : 'none';
  document.getElementById('ov-rtab-cov').classList.toggle('active', tab === 'cov');
  document.getElementById('ov-rtab-cat').classList.toggle('active', tab === 'cat');
  if (tab === 'cat') refreshQlJobsForOv();
  // Coverage canvas is sized to its container at build time. If it was built or
  // deferred while hidden, rebuild now that the panel is visible so the
  // availability pixels fill the panel width instead of staying shrunk.
  if (tab === 'cov' && _covData) renderCovGrid();
}

async function refreshQlJobsForOv() {
  if (!SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/catalog/jobs?cfg_id=${encodeURIComponent(SW.activeConfigId)}`)).json();
    _syncOvCatJobSel(jobs);
  } catch { }
}

function _syncOvCatJobSel(jobs) {
  const sel = document.getElementById('ov-cat-job-sel');
  if (!sel) return;
  const prev = sel.value;
  const doneJobs = jobs.filter(j => j.state === 'done');
  sel.innerHTML = '<option value="">— select download job —</option>'
    + doneJobs.map(j => {
      const label = `${j.id} · ${(j.params?.starttime || '').slice(0,10)} – ${(j.params?.endtime || '').slice(0,10)} · ${((j.downloaded||0)/1024/1024).toFixed(1)}MB`;
      return `<option value="${SW.esc(j.id)}"${j.id===prev?' selected':''}>${SW.esc(label)}</option>`;
    }).join('');
}

function onOvCatJobChange() {
  const id = document.getElementById('ov-cat-job-sel').value;
  if (id) loadOvCatalogStats(id);
}

function viewCatalogStatsFromJob(jobId) {
  switchWpStep('overview');
  switchOvRightTab('cat');
  const sel = document.getElementById('ov-cat-job-sel');
  if (sel) { sel.value = jobId; loadOvCatalogStats(jobId); }
}

let _catParseTimer = null;

async function loadOvCatalogStats(jobId) {
  const id = jobId || document.getElementById('ov-cat-job-sel').value;
  const wrap = document.getElementById('ov-cat-content');
  if (!id) {
    wrap.innerHTML = '<div style="padding:.8rem;text-align:center;color:var(--text-muted)">Select a completed catalog job</div>';
    return;
  }

  _renderCatProgress(wrap, 0, 'Starting…', null, null);
  if (_catParseTimer) { clearInterval(_catParseTimer); _catParseTimer = null; }

  try {
    const startRes = await fetch(`/api/catalog/stats/${id}/start`, { method: 'POST' });
    const startD   = await startRes.json();
    if (!startRes.ok) {
      wrap.innerHTML = `<div style="color:#f44336;padding:.5rem">${SW.esc(startD.error || 'Error')}</div>`;
      return;
    }

    _catParseTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/catalog/stats/${id}/progress`);
        const p   = await res.json();
        if (!res.ok || p.state === 'error') {
          clearInterval(_catParseTimer); _catParseTimer = null;
          wrap.innerHTML = `<div style="color:#f44336;padding:.5rem">${SW.esc(p.error || 'Error parsing catalog')}</div>`;
          return;
        }
        if (p.state === 'done') {
          clearInterval(_catParseTimer); _catParseTimer = null;
          await _renderCatalogStats(wrap, p.result, id);
          return;
        }
        _renderCatProgress(wrap, p.pct || 0, p.label || '…', p.n_done || null, p.n_events || null);
      } catch { }
    }, 400);

  } catch (e) {
    wrap.innerHTML = `<div style="color:#f44336;padding:.5rem">${SW.esc(e.message)}</div>`;
  }
}

function _renderCatProgress(wrap, pct, label, nDone, nTotal) {
  const pctStr = `${pct}%`;
  const countInfo = (nDone != null && nTotal != null)
    ? `<span style="font-size:.67rem;color:var(--text-muted)">${nDone.toLocaleString()} / ${nTotal.toLocaleString()} event</span>`
    : '';
  wrap.innerHTML = `
    <div style="padding:.9rem .4rem">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.35rem">
        <span style="font-size:.73rem;color:var(--text-muted)">${SW.esc(label)}</span>
        <span style="font-size:.75rem;font-weight:700;color:var(--accent2);font-family:monospace">${pctStr}</span>
      </div>
      <div style="background:var(--bg-input);border-radius:4px;height:8px;overflow:hidden;margin-bottom:.35rem">
        <div style="height:100%;width:${pctStr};background:linear-gradient(90deg,var(--accent2),#66b2ff);
             border-radius:4px;transition:width .35s ease"></div>
      </div>
      <div style="text-align:center">${countInfo}</div>
    </div>`;
}

async function _renderCatalogStats(wrap, d, jobId) {
  if (!d || !d.n_events) {
    wrap.innerHTML = '<div style="color:var(--text-muted);padding:.5rem;font-size:.75rem">No events in catalog</div>';
    return;
  }

  try { await SW.ensurePlotly(); } catch (_) { }

  const tile = (lbl, val, sub = '') =>
    `<div class="cat-stat-tile"><div class="cat-stat-lbl">${lbl}</div>` +
    `<div class="cat-stat-val">${val}</div>` +
    (sub ? `<div class="cat-stat-sub">${sub}</div>` : '') + `</div>`;

  wrap.innerHTML = `
    <div class="cat-stats-grid">
      ${tile('Total Event', d.n_events.toLocaleString())}
      ${tile('Period', `${(d.starttime||'').slice(0,7)} – ${(d.endtime||'').slice(0,7)}`)}
      ${d.mag?.min   != null ? tile('Magnitude',   `${(+d.mag.min).toFixed(2)} – ${(+d.mag.max).toFixed(2)}`, `Mean M${(+d.mag.mean).toFixed(2)}`) : ''}
      ${d.depth?.min != null ? tile('Depth',       `${d.depth.min}–${d.depth.max} km`, `Mean ${d.depth.mean} km`) : ''}
      ${d.rms?.mean  != null ? tile('RMS (mean)',   `${d.rms.mean} s`,   `Max ${d.rms.max} s`) : ''}
      ${d.phases?.mean != null ? tile('Phases (mean)', `${d.phases.mean}`, `Max ${d.phases.max}`) : ''}
    </div>
    ${jobId ? `<div style="margin-bottom:.4rem;text-align:right">
      <button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="openQlCatalogInResultModal('${SW.esc(jobId)}')"
        style="font-size:.68rem;color:var(--accent2);border-color:var(--accent2)">
        <i class="bi bi-map"></i> Open in Result View
      </button></div>` : ''}
    <div id="ov-cat-mag-plt" style="height:180px;position:relative"></div>
    <div id="ov-cat-dep-plt" style="height:180px;position:relative"></div>
    <div id="ov-cat-mon-plt" style="height:180px;position:relative"></div>`;

  if (typeof Plotly === 'undefined') return;

  const PC  = { responsive: true, displayModeBar: false };
  const DRK = {
    paper_bgcolor: 'transparent', plot_bgcolor: '#0a0f1e',
    font: { color: '#94a3b8', size: 9, family: 'Segoe UI,Arial,sans-serif' },
    margin: { l: 48, r: 14, t: 28, b: 50 },
    showlegend: false,
  };
  const ax = (title, extra) => Object.assign({
    title: { text: title, font: { size: 8.5, color: '#60a5fa' } },
    gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b',
    showline: true, linecolor: '#1e3a5f', mirror: true,
  }, extra || {});

  // — Magnitude histogram —
  if (d.mag?.hist?.length) {
    const bins = d.mag.hist.filter(b => b.count > 0);
    if (bins.length) {
      const mags = [];
      bins.forEach(b => { for (let i = 0; i < b.count; i++) mags.push(parseFloat(b.label)); });
      Plotly.newPlot('ov-cat-mag-plt', [{
        type: 'histogram', x: mags, xbins: { size: 0.5 },
        marker: { color: '#2196f3', opacity: 0.8, line: { color: '#1565c0', width: 0.8 } },
        name: 'Count',
      }], Object.assign({}, DRK, {
        title: { text: 'Magnitude Distribution', font: { size: 9, color: '#94a3b8' }, x: 0.5 },
        xaxis: ax('Magnitude', { dtick: 0.5 }),
        yaxis: ax('Event Count'),
      }), PC);
    }
  }

  // — Depth histogram —
  if (d.depth?.hist?.length) {
    const bins = d.depth.hist.filter(b => b.count > 0);
    if (bins.length) {
      const deps = [];
      bins.forEach(b => { for (let i = 0; i < b.count; i++) deps.push(parseFloat(b.label)); });
      Plotly.newPlot('ov-cat-dep-plt', [{
        type: 'histogram', x: deps, xbins: { size: 10 },
        marker: { color: '#e8531a', opacity: 0.8, line: { color: '#bf360c', width: 0.8 } },
        name: 'Count',
      }], Object.assign({}, DRK, {
        title: { text: 'Depth Distribution', font: { size: 9, color: '#94a3b8' }, x: 0.5 },
        xaxis: ax('Depth (km)', { dtick: 50 }),
        yaxis: ax('Event Count'),
      }), PC);
    }
  }

  // — Events per Month —
  if (d.monthly?.length) {
    Plotly.newPlot('ov-cat-mon-plt', [{
      type: 'bar',
      x: d.monthly.map(m => m.ym),
      y: d.monthly.map(m => m.count),
      marker: { color: '#9c27b0', opacity: 0.8, line: { color: '#6a0080', width: 0.6 } },
      text: d.monthly.map(m => String(m.count)),
      textposition: 'none',
      hovertemplate: '%{x}<br>%{y} event<extra></extra>',
    }], Object.assign({}, DRK, {
      title: { text: 'Events per Month', font: { size: 9, color: '#94a3b8' }, x: 0.5 },
      xaxis: ax('Month', { tickangle: -55, type: 'category' }),
      yaxis: ax('Event Count'),
      margin: { l: 48, r: 14, t: 28, b: 65 },
    }), PC);
  }
}

// ── Buka QuakeLink catalog di Result View ─────────────────────────────────────
async function openQlCatalogInResultModal(jobId) {
  try { await SW.ensurePlotly(); } catch (e) { alert('Plotly failed to load: ' + e.message); return; }

  document.getElementById('rm-bd').classList.remove('hidden');
  document.getElementById('rm-hdr-cfg').textContent = 'QuakeLink';
  // Clear the job selector immediately so stale data is not shown
  document.getElementById('rm-job-sel').innerHTML = '<option value="">Loading catalog…</option>';

  _RM.cfgId = '__ql__';
  _RM.data   = null;
  _RM.activeJob = null;
  _RM.mgMin = -9; _RM.mgMax = 9; _RM.depMax = 700; _RM.xsHalf = 50; _RM.az = 0;
  _RM.showFault = true; _RM.showFaultSym = true; _RM.showVolcano = true; _RM.showStation = false;
  _RM.showXS = true; _RM.showSlab = true; _RM.showTopo = true; _RM.showSlab3d = false;
  _RM.showVol3d = false; _RM.showCoast3d = false; _RM.showSlab2d = false; _RM.showTopo3d = false;
  _RM.showFault3d = false; _RM.showSlabMap = true;
  _RM.topoData = {}; _RM.coast3dData = null; _RM.slab2Data = null; _RM.topo3dData = null;

  _rmSetLoading(true, 'Loading QuakeLink catalog…', 5);
  _rmSetError(null);

  let data;
  try {
    const res = await fetch(`/api/catalog/as_result/${jobId}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to load catalog (HTTP ' + res.status + ')');
  } catch (e) {
    _rmSetLoading(false);
    _rmSetError('Error: ' + e.message);
    document.getElementById('rm-job-sel').innerHTML = `<option value="">Error: ${SW.esc(e.message)}</option>`;
    return;
  }

  _RM.data = data;
  _rmSetLoading(true, 'Preparing data…', 60);

  // Set the center from the region or the first event
  const r = data.region || {};
  if (r.lat_min != null && r.lat_max != null) {
    _RM.cla = (r.lat_min + r.lat_max) / 2;
    _RM.clo = (r.lon_min + r.lon_max) / 2;
  } else if (data.jobs?.[0]?.events?.length) {
    const ev0 = data.jobs[0].events[0];
    _RM.cla = parseFloat(ev0.lat);
    _RM.clo = parseFloat(ev0.lon);
  }

  if (!_RM.slabData) {
    _RM.slabData = await _rmFetchSlab(data.region);
  }

  _rmSetLoading(false);
  _rmPopulateJobSel();

  const nEv = data.jobs?.[0]?.events?.length || 0;
  document.getElementById('rm-hdr-cfg').textContent = `QuakeLink (${nEv} events)`;

  _rmInitMap();
  _rmUpdate();
}

// ═══════════════════════════════════════════
//  Work Page
// ═══════════════════════════════════════════
// SW.wpCfgId → SeisWorkCore (SW.wpCfgId)
let _wpCfgMeta = null;

async function openWorkPage(cfgId) {
  SW.wpCfgId = cfgId;
  // Keep SW.activeConfigId in sync with the open workspace: all job-listing fetches
  // filter by SW.activeConfigId, so opening a workspace WITHOUT first "Load"-ing it
  // would otherwise leave SW.activeConfigId pointing at another config (or null →
  // unfiltered) and leak other sessions' jobs into this one.
  SW.activeConfigId = cfgId;
  const res = await fetch(`/api/configs/${cfgId}`);
  if (!res.ok) { alert('Config not found'); return; }
  const cfg = await res.json();

  _wpCfgMeta = cfg;
  SW.activeProjectName = cfg.name || '';
  _updateProjectBadge();
  document.getElementById('wp-title').textContent = cfg.name || cfgId;
  // Auto-fill VELEST reference point from region midpoint
  const _r = cfg.region || {};
  if (!isNaN(parseFloat(_r.lat_min)) && !isNaN(parseFloat(_r.lat_max))) {
    const midLat = ((parseFloat(_r.lat_min) + parseFloat(_r.lat_max)) / 2).toFixed(3);
    const el = document.getElementById('vel-latref');
    if (el) el.value = midLat;
  }
  if (!isNaN(parseFloat(_r.lon_min)) && !isNaN(parseFloat(_r.lon_max))) {
    const midLon = ((parseFloat(_r.lon_min) + parseFloat(_r.lon_max)) / 2).toFixed(3);
    const el = document.getElementById('vel-lonref');
    if (el) el.value = midLon;
  }
  document.getElementById('wp-cfg-id').innerHTML = SW.esc(cfgId) +
    (SW.REMOTE.base ? ` <span style="color:#fbbf24;font-size:.62rem" title="Session on the remote server ${SW.esc(SW.REMOTE.base)}">[remote ${SW.esc(SW.REMOTE.base.replace(/^https?:\/\//, ''))}]</span>` : '');
  _renderOvCfgTable(cfg);
  _populateStaPicker();
  _populatePickJobSelector();   // populate pick overlay selector
  switchWpStep('overview');
  document.getElementById('wp-bd').classList.remove('hidden');
  document.getElementById('btn-view-result').style.display = 'inline-flex';

  document.getElementById('ov-cov-wrap').innerHTML =
    '<div style="padding:1.2rem;text-align:center;color:var(--text-muted);font-size:.76rem"><span class="sw-spinner"></span> Scanning archive…</div>';
  document.getElementById('ov-cov-pct').textContent = '';
  document.getElementById('ov-cov-gran').textContent = '';
  loadCoverage(cfgId);
}
function closeWorkPage() {
  exitSysmon();
  document.getElementById('wp-bd').classList.add('hidden');
  document.getElementById('btn-view-result').style.display = 'none';
  SW.wpCfgId = null;
}
function onWpBdClick(e) {
  if (e.target === document.getElementById('wp-bd')) closeWorkPage();
}
function switchWpStep(step) {
  if (step !== 'sysmon') exitSysmon();
  document.querySelectorAll('.wp-step').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.wp-page').forEach(el => el.classList.remove('active'));
  const sEl = document.getElementById(`wpstep-${step}`);
  const pEl = document.getElementById(`wpp-${step}`);
  if (sEl) sEl.classList.add('active');
  if (pEl) pEl.classList.add('active');
  if (step === 'sysmon') enterSysmon();
  if (step === 'output') refreshOutputPanel();
  if (step === 'picking') _restorePickSession();
  // Restore last session for each pipeline step (log + jobs + dropdown input)
  if (SW.STEP_CHANNELS[step]) _restorePipeSession(step);
  // Refresh session results for the active method tab in assocloc
  if (step === 'assocloc') {
    const activeM = ['gamma','real','nlloc','locsat'].find(m =>
      document.getElementById(`altab-${m}-btn`)?.classList.contains('active')
    ) || 'gamma';
    AL.refreshSessions(activeM);
  }
  // Refresh relocation session results (list of finished hypoDD runs + RMS stats)
  if (step === 'relocation') HD.refreshSessions();
  // Imaging: auto-load figures from the most recent completed tomography job
  if (step === 'imaging') TOMO.restoreFigures();
  // Mechanism: auto-load results from the most recent completed SKHASH job
  if (step === 'mechanism') MECH.restoreResults();
  // Presentation builder: load config when step is opened
  if (step === 'presentation') presBuilderLoad();
}

// _restoreTomoFigures → modules/tomography.js (TOMO.restoreFigures)

function switchMlTab(method) {
  ['gamma','real','nlloc','locsat','hypodd'].forEach(m => {
    document.getElementById(`mltab-${m}-btn`)?.classList.toggle('active', m === method);
    const p = document.getElementById(`ml-pane-${m}`);
    if (p) p.style.display = m === method ? '' : 'none';
  });
  SW.loadCatalogFiles(`ml-input-${method}`, 'magnitude', method);
  _refreshMlSessions(method);
}

async function _refreshMlSessions(method) {
  const ch = `magnitude-ml-${method}`;
  const listEl = document.getElementById(SW.PIPE_JOBS_LIST[ch]);
  const wrapEl = document.getElementById(SW.PIPE_JOBS_WRAP[ch]);
  if (!listEl || !SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/pipeline/jobs?step=magnitude&cfg_id=${SW.activeConfigId}`)).json();
    const filtered = jobs.filter(j => j.method === `ml-${method}`);
    _renderPipeJobsCh(ch, filtered);
    if (wrapEl) wrapEl.style.display = filtered.length ? '' : 'none';
  } catch { if (wrapEl) wrapEl.style.display = 'none'; }
}
function reloadCoverage() {
  if (!SW.wpCfgId) return;
  document.getElementById('ov-cov-wrap').innerHTML =
    '<div style="padding:1.2rem;text-align:center;color:var(--text-muted);font-size:.76rem"><span class="sw-spinner"></span> Scanning…</div>';
  loadCoverage(SW.wpCfgId);
}
function _renderOvCfgTable(cfg) {
  const r = cfg.region || {};
  const wv = cfg.waveform || {};
  const f = wv.fdsn || {};
  const srcLabel = wv.source === 'fdsn'
    ? `FDSN · ${f.client || '—'}`
    : (wv.path ? `${wv.path_type === 'sds' ? 'SDS' : 'Folder'} · Path` : '—');
  const rows = [
    ['Lat', `${r.lat_min ?? '—'} → ${r.lat_max ?? '—'}`],
    ['Lon', `${r.lon_min ?? '—'} → ${r.lon_max ?? '—'}`],
    ['Depth', `≤ ${r.depth_max ?? '—'} km`],
    ['Start', (r.starttime || '—').slice(0, 16).replace('T', ' ')],
    ['End', (r.endtime || '—').slice(0, 16).replace('T', ' ')],
    ['Stations', `${cfg.n_stations || 0}`],
    ['Source', srcLabel],
    ['Network', wv.source === 'fdsn' ? (f.networks || '—') : '—'],
  ];
  document.getElementById('ov-cfg-table').innerHTML =
    rows.map(([k, v]) => `<tr><td>${SW.esc(k)}</td><td>${SW.esc(v)}</td></tr>`).join('');
}
async function loadCoverage(cfgId) {
  try {
    const res = await fetch(`/api/configs/${cfgId}/coverage`);
    const data = await res.json();
    if (data.n_periods === 0) {
      document.getElementById('ov-cov-wrap').innerHTML =
        '<div class="empty-state" style="padding:.6rem"><i class="bi bi-calendar-x"></i> No time range defined in config.</div>';
      return;
    }
    document.getElementById('ov-cov-pct').textContent = data.pct_present != null ? `${data.pct_present}% present` : '';
    document.getElementById('ov-cov-gran').textContent = data.granularity === 'week' ? 'Per week' : 'Per day';
    _covZoom = 1;
    renderCovGrid(data);
  } catch (e) {
    document.getElementById('ov-cov-wrap').innerHTML =
      `<div class="empty-state" style="padding:.6rem"><i class="bi bi-x-circle"></i> ${SW.esc(e.message)}</div>`;
  }
}
let _covData = null;      // last coverage payload (for zoom re-render)
let _covZoom = 1;         // horizontal zoom factor of the coverage grid
let _covNeedsRender = false; // deferred render pending (tab was hidden at render time)
function renderCovGrid(data) {
  if (data) _covData = data;
  data = _covData;
  const wrap = document.getElementById('ov-cov-wrap');
  if (!wrap || !data) return;
  // If the Coverage tab is hidden (e.g. the user switched to the Catalog tab
  // while the archive scan was still running), defer: building the canvas now
  // would read offsetWidth=0 → fall back to a tiny 500px grid, leaving the
  // availability pixels shrunk when the tab is shown again. Re-render instead
  // when switchOvRightTab('cov') makes the panel visible.
  if (wrap.offsetParent === null || wrap.offsetWidth === 0) {
    _covNeedsRender = true;
    return;
  }
  _covNeedsRender = false;
  wrap.innerHTML = '';
  const staKeys = Object.keys(data.coverage);
  if (!staKeys.length || !data.n_periods) {
    wrap.innerHTML = '<div class="empty-state" style="padding:.6rem"><i class="bi bi-inbox"></i> No stations</div>';
    return;
  }
  if (!data.has_path) {
    const note = document.createElement('div');
    note.style.cssText = 'color:var(--text-muted);font-size:.73rem;padding:.25rem 0 .45rem';
    note.innerHTML = '<i class="bi bi-info-circle"></i> Waveform path not found — grid shows empty.';
    wrap.appendChild(note);
  }
  wrap.appendChild(_buildCovCanvas(data, staKeys, !data.has_path));
}
function _buildCovCanvas(data, staKeys, emptyMode) {
  const LBL_W = 80, ROW_H = 9, ROW_G = 1, HDR_H = 22, LEG_H = 18, PAD = 4;
  const n_per = data.n_periods, n_sta = staKeys.length;
  const avW = (document.getElementById('ov-cov-wrap').offsetWidth || 500) - PAD;
  const dataW = Math.max(60, avW - LBL_W);
  // Spread the periods across the FULL available width instead of giving each an
  // integer cellW and leaving a leftover gap. With integer floor, configs whose
  // dataW/n_per lands just under a whole number (e.g. 365 days → 1px cells) leave
  // a large empty band so the grid looks crammed ("mampat"), while others fill.
  // Rounded fractional boundaries make the data area exactly fill dataW at zoom=1
  // for any period count, so placement is precise regardless of n_periods.
  const dataPxW = Math.max(n_per, Math.round(dataW * _covZoom));
  const stepF   = dataPxW / n_per;                       // avg px per period (float)
  const cellW   = stepF;                                  // for label-density thresholds
  const xOf     = (pi) => LBL_W + Math.round(pi * dataPxW / n_per);
  const totalW  = LBL_W + dataPxW + PAD;
  const totalH = HDR_H + n_sta * (ROW_H + ROW_G) + LEG_H + PAD;
  const canvas = document.createElement('canvas');
  canvas.width = totalW; canvas.height = totalH;
  // No max-width: let the canvas grow with zoom; ov-cov-wrap (overflow-x:auto)
  // scrolls it, so zooming actually makes the day/week pixels bigger.
  canvas.style.cssText = 'display:block';
  const ctx = canvas.getContext('2d');
  ctx.font = '9px "Segoe UI",sans-serif';
  const labels = data.labels || [];
  let prevLblX = -999;
  data.month_marks.forEach(mm => {
    const x = xOf(mm.idx);
    ctx.fillStyle = '#2d3555'; ctx.fillRect(x, 3, 1, HDR_H - 7);
    if (x - prevLblX > 34) { ctx.fillStyle = '#8898aa'; ctx.fillText(mm.label, x + 2, 10); prevLblX = x; }
  });
  // When zoomed in enough, label each period with its date (MM-DD) so the time
  // detail is readable — every period if cells are wide, else every Nth.
  if (cellW >= 14 && labels.length) {
    ctx.font = '7.5px monospace'; ctx.textAlign = 'left';
    const stepN = Math.max(1, Math.ceil(42 / cellW));
    for (let pi = 0; pi < labels.length; pi += stepN) {
      const x = xOf(pi);
      const dt = (labels[pi] || '').slice(5, 10);   // MM-DD
      ctx.fillStyle = '#46506a'; ctx.fillRect(x, HDR_H - 5, 0.6, 5);
      ctx.fillStyle = '#7c89a8'; ctx.fillText(dt, x + 1.5, HDR_H - 1);
    }
  }
  staKeys.forEach((key, si) => {
    const vals = data.coverage[key] || [];
    const y = HDR_H + si * (ROW_H + ROW_G);
    ctx.font = '8.5px "Segoe UI",sans-serif'; ctx.fillStyle = '#6b7fa0';
    ctx.textAlign = 'right'; ctx.fillText(key.length > 10 ? key.slice(-10) : key, LBL_W - 4, y + ROW_H - 1); ctx.textAlign = 'left';
    if (emptyMode) { ctx.fillStyle = '#1e2235'; ctx.fillRect(LBL_W, y, dataPxW, ROW_H); }
    else vals.forEach((val, pi) => {
      const x0 = xOf(pi), span = xOf(pi + 1) - x0;
      const cw = span > 2 ? span - 1 : Math.max(1, span);   // 1px gap only when cells are wide enough
      ctx.fillStyle = _covColor(val); ctx.fillRect(x0, y, cw, ROW_H);
    });
  });
  const ly = HDR_H + n_sta * (ROW_H + ROW_G) + 5;
  const legend = [{ c: '#52c896', l: 'Full' }, { c: '#1e8c6e', l: 'Partial' }, { c: '#1a3550', l: 'Sparse' }, { c: '#1e2235', l: 'None' }];
  ctx.font = '8.5px "Segoe UI",sans-serif'; let lx = LBL_W;
  legend.forEach(item => { ctx.fillStyle = item.c; ctx.fillRect(lx, ly, 10, 9); ctx.fillStyle = '#8898aa'; ctx.fillText(item.l, lx + 13, ly + 9); lx += 50; });

  // Scroll over the grid → zoom the time axis in/out (horizontal).
  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const before = _covZoom;
    _covZoom = Math.max(1, Math.min(40, _covZoom * (ev.deltaY < 0 ? 1.25 : 0.8)));
    if (_covZoom !== before) renderCovGrid();
  }, { passive: false });

  // Map a mouse position to (station index, period index).
  const _covHit = (ev) => {
    const rect = canvas.getBoundingClientRect();
    const x = (ev.clientX - rect.left) * (canvas.width / rect.width);
    const y = (ev.clientY - rect.top) * (canvas.height / rect.height);
    const si = Math.floor((y - HDR_H) / (ROW_H + ROW_G));
    const pi = Math.floor((x - LBL_W) * n_per / dataPxW);
    return { si, pi, inData: x >= LBL_W && si >= 0 && si < staKeys.length };
  };

  // Hover → tooltip with station + date (+ coverage %).
  canvas.addEventListener('mousemove', (ev) => {
    const { si, pi, inData } = _covHit(ev);
    const labels = data.labels || [];
    if (!inData || pi < 0 || pi >= labels.length) { _hideCovTip(); return; }
    const date = (labels[pi] || '').slice(0, 10);
    const val = (data.coverage[staKeys[si]] || [])[pi];
    const pct = val != null ? Math.round(val * 100) + '%' : '–';
    const gran = data.granularity === 'week' ? ' (week)' : '';
    _showCovTip(ev.clientX, ev.clientY,
      `<b>${SW.esc(staKeys[si])}</b><br>${SW.esc(date)}${gran} · ${pct}`);
  });
  canvas.addEventListener('mouseleave', _hideCovTip);

  // Click a cell → load that station+date into the Waveform Viewer below.
  canvas.style.cursor = 'pointer';
  canvas.title = 'Hover = date · Click = show waveform · Scroll = zoom time';
  canvas.onclick = (ev) => {
    const { si, pi, inData } = _covHit(ev);
    if (!inData) return;
    const labels = data.labels || [];
    const date = (pi >= 0 && pi < labels.length) ? (labels[pi] || '').slice(0, 10) : '';
    _covLoadWaveform(staKeys[si], date);
  };
  return canvas;
}

function _showCovTip(x, y, html) {
  let t = document.getElementById('cov-tip');
  if (!t) {
    t = document.createElement('div'); t.id = 'cov-tip';
    t.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;background:#0f172a;' +
      'color:#e2e8f0;border:1px solid #334155;border-radius:5px;padding:.3rem .5rem;' +
      'font-size:.66rem;font-family:monospace;box-shadow:0 4px 14px rgba(0,0,0,.45);white-space:nowrap';
    document.body.appendChild(t);
  }
  t.innerHTML = html;
  t.style.display = 'block';
  // keep inside the viewport
  const ox = (x + 14 + 160 > window.innerWidth) ? x - 150 : x + 14;
  t.style.left = ox + 'px';
  t.style.top = (y + 14) + 'px';
}
function _hideCovTip() { const t = document.getElementById('cov-tip'); if (t) t.style.display = 'none'; }

// Coverage cell click → populate the Waveform Viewer controls and load.
function _covLoadWaveform(staKey, date) {
  const staSel = document.getElementById('wv-sta-pick');
  const dateEl = document.getElementById('wv-date-pick');
  if (staSel) {
    if (![...staSel.options].some(o => o.value === staKey)) {
      const o = document.createElement('option'); o.value = staKey; o.textContent = staKey; staSel.appendChild(o);
    }
    staSel.value = staKey;
  }
  if (dateEl && date) dateEl.value = date;
  (async () => {
    try { if (typeof fetchWvChannels === 'function') await fetchWvChannels(); } catch (e) { }
    loadWaveformViewer();
    setTimeout(() => document.getElementById('wv-canvas-outer')
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 120);
  })();
}
function _covColor(v) {
  if (v <= 0) return '#1e2235'; if (v < 0.15) return '#1a3550'; if (v < 0.45) return '#155f6b';
  if (v < 0.75) return '#1e8c6e'; if (v < 0.95) return '#2dbb7f'; return '#52c896';
}

// ═══════════════════════════════════════════
//  Save Configuration Modal
// ═══════════════════════════════════════════
function openSaveModal(wvInfo) {
  if (wvInfo) _modalWvInfo = wvInfo;
  const r = getRegionForm();
  document.getElementById('modal-name').value = r.name || '';
  document.getElementById('modal-region-rows').innerHTML = _buildRegionRows(r);
  document.getElementById('modal-wv-rows').innerHTML = _buildWvRows(getWaveformConfig(), _modalWvInfo);
  const optsEl = document.getElementById('modal-save-opts');
  if (SW.activeConfigId) {
    document.getElementById('modal-old-id').textContent = SW.activeConfigId;
    optsEl.style.display = '';
    selSaveMode('replace');
  } else {
    optsEl.style.display = 'none';
    _saveMode = 'new';
  }
  document.getElementById('save-modal-bd').classList.remove('hidden');
  setTimeout(() => document.getElementById('modal-name').focus(), 80);
}
function closeSaveModal() { document.getElementById('save-modal-bd').classList.add('hidden'); }
function onModalBdClick(e) { if (e.target === document.getElementById('save-modal-bd')) closeSaveModal(); }
function selSaveMode(mode) {
  _saveMode = mode;
  document.getElementById('save-opt-replace').classList.toggle('sel', mode === 'replace');
  document.getElementById('save-opt-new').classList.toggle('sel', mode === 'new');
  document.getElementById('save-opt-replace').querySelector('input').checked = (mode === 'replace');
  document.getElementById('save-opt-new').querySelector('input').checked = (mode === 'new');
}
function saveFromJob(id) {
  const j = _lastJobs[id];
  if (j) {
    _modalWvInfo = { source: 'fdsn', chunks: j.done, errors: j.errors || 0, path: j.output_path || '' };
    if (j.output_path) document.getElementById('wv-outpath').value = j.output_path;
  }
  openSaveModal();
}
function _buildRegionRows(r) {
  const row = (k, v) => `<div class="info-row"><span class="info-k">${k}</span><span class="info-v">${v}</span></div>`;
  const out = [];
  if (!isNaN(r.lat_min) && !isNaN(r.lat_max)) out.push(row('Lat', `${r.lat_min} → ${r.lat_max}`));
  if (!isNaN(r.lon_min) && !isNaN(r.lon_max)) out.push(row('Lon', `${r.lon_min} → ${r.lon_max}`));
  if (!isNaN(r.depth_max)) out.push(row('Depth', `${r.depth_max} km`));
  if (r.starttime) out.push(row('Start', r.starttime.slice(0, 16).replace('T', ' ')));
  if (r.endtime) out.push(row('End', r.endtime.slice(0, 16).replace('T', ' ')));
  return out.join('') || '<span style="color:var(--text-muted);font-size:.7rem">Not defined</span>';
}
function _buildWvRows(wv, avail) {
  const row = (k, v) => `<div class="info-row"><span class="info-k">${k}</span><span class="info-v">${v}</span></div>`;
  const out = [];
  if (wv.source === 'fdsn') {
    const f = wv.fdsn; out.push(row('Source', 'FDSN'));
    if (f.client) out.push(row('Client', SW.esc(f.client)));
    if (f.networks) out.push(row('Network', SW.esc(f.networks)));
    if (f.stations && f.stations !== '*') out.push(row('Station', SW.esc(f.stations)));
    if (f.starttime) out.push(row('Period', `${f.starttime.slice(0, 10)} → ${(f.endtime || '').slice(0, 10)}`));
    if (f.output_path) out.push(row('Output', SW.esc(f.output_path)));
  } else {
    out.push(row('Source', 'Local Path'));
    if (wv.path) out.push(row('Path', SW.esc(wv.path)));
    if (wv.path_type) out.push(row('Type', wv.path_type === 'sds' ? 'SDS Archive' : 'MiniSEED Folder'));
  }
  if (avail) {
    let av = '';
    if (avail.source === 'path' && avail.files != null) {
      av = `<i class="bi bi-check-circle" style="color:#4caf50"></i> ${avail.files} files &nbsp;·&nbsp; ${avail.size_mb} MB`;
      if (avail.path) av += `<div style="color:var(--text-muted);font-size:.67rem;margin-top:.12rem;word-break:break-all">${SW.esc(avail.path)}</div>`;
    } else if (avail.source === 'fdsn' && avail.chunks != null) {
      av = `<i class="bi bi-check-circle" style="color:#4caf50"></i> ${avail.chunks} chunks downloaded`
        + (avail.errors > 0 ? ` &nbsp;·&nbsp; <span style="color:#f44336">${avail.errors} error(s)</span>` : '');
      if (avail.path) av += `<div style="color:var(--text-muted);font-size:.67rem;margin-top:.12rem;word-break:break-all">${SW.esc(avail.path)}</div>`;
    }
    if (av) out.push(`<div class="info-avail">${av}</div>`);
  }
  return out.join('') || '<span style="color:var(--text-muted);font-size:.7rem">Not configured</span>';
}
async function submitSaveModal() {
  const name = document.getElementById('modal-name').value.trim();
  if (!name) {
    const el = document.getElementById('modal-name');
    el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 600); return;
  }
  const region = getRegionForm(); region.name = name;
  if (isNaN(region.lat_min)) { alert('Coordinates incomplete.'); return; }
  const btn = document.getElementById('btn-modal-save');
  btn.disabled = true; btn.innerHTML = '<span class="sw-spinner"></span>';
  const isReplace = (_saveMode === 'replace' && SW.activeConfigId);
  try {
    const url = isReplace ? `/api/configs/${SW.activeConfigId}` : '/api/configs';
    const method = isReplace ? 'PUT' : 'POST';
    const res = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, region, stations: SW.currentStations, fdsn_url: SW.currentFdsnUrl, waveform: getWaveformConfig() }),
    });
    const d = await res.json();
    if (!res.ok) { alert(d.error || 'Save failed'); return; }
    SW.activeConfigId = d.id;
    SW.activeProjectName = name;
    _updateProjectBadge();
    document.getElementById('cfg-name').value = name;
    closeSaveModal();
    SW.showAlert('save', `Saved — Project ID: ${d.id}`, 'ok');
    await loadConfigList();
  } catch (e) { alert('Error: ' + e.message); }
  finally { btn.disabled = false; btn.innerHTML = '<i class="bi bi-save2"></i> Save'; }
}

// ═══════════════════════════════════════════
//  Pick sub-tabs
// ═══════════════════════════════════════════

// Per-method terminal content cache (preserves live output per method)
const _terminalByMethod = {};

function switchPickTab(tab) {
  // Save current picker terminal content before switching (Match&Locate is not a
  // picker — it has its own terminal/jobs panel, so skip the shared one for it).
  const prevMethod = _getActivePickMethod();
  const term = document.getElementById('pick-terminal');
  if (term && prevMethod && prevMethod !== 'matchlocate')
    _terminalByMethod[prevMethod] = term.innerHTML;

  ['phasenet', 'phasenet_native', 'eqt', 'stalta', 'matchlocate', 'deepdenoiser'].forEach(t => {
    const el  = document.getElementById(`ptab-${t}`);
    const btn = document.getElementById(`ptab-${t}-btn`);
    if (el)  el.style.display = t === tab ? '' : 'none';
    if (btn) btn.classList.toggle('active', t === tab);
  });

  // Match&Locate (template-matching detection) — restore its own pipeline
  // session (terminal + jobs), not the picker terminal.
  if (tab === 'matchlocate') {
    if (typeof _restorePipeSession === 'function') _restorePipeSession('detect');
    return;
  }

  // Restore terminal content for the new picker method
  if (term) {
    const saved = _terminalByMethod[tab];
    term.innerHTML = saved ||
      '<div class="pick-term-placeholder">Picking output will appear here — select a method and click Run</div>';
  }

  // Restore last session result panel for this picker method
  if (tab !== 'deepdenoiser') _restorePickSession();
}

// ═══════════════════════════════════════════
//  DeepDenoiser standalone tab
// ═══════════════════════════════════════════
let _ddJobId = null;
let _ddPollTimer = null;
let _ddLogOffset = 0;

function _ddAppend(line, cls = '') {
  const term = document.getElementById('dd-terminal');
  if (!term) return;
  const ph = term.querySelector('.pick-term-placeholder');
  if (ph) ph.remove();
  const div = document.createElement('div');
  div.className = 'pick-term-line' + (cls ? ' pick-term-' + cls : '');
  div.textContent = line;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
}

async function startDenoiseJob() {
  if (!SW.wpCfgId) { alert('Open a configuration first'); return; }
  const btn  = document.getElementById('btn-run-deepdenoiser');
  const stop = document.getElementById('btn-stop-deepdenoiser');
  if (btn)  { btn.disabled = true; btn.innerHTML = '<span class="sw-spinner"></span> Starting…'; }

  const term = document.getElementById('dd-terminal');
  if (term) term.innerHTML = '';
  _ddLogOffset = 0;

  const params = {
    pretrained  : (document.getElementById('dd-pretrained')  || {}).value || 'original',
    data_source : (document.getElementById('dd-datasrc')     || {}).value || 'sds',
    highpass_hz : parseFloat((document.getElementById('dd-highpass') || {}).value || 0),
    workers     : parseInt((document.getElementById('dd-workers') || {}).value || 4),
  };

  try {
    const res = await fetch('/api/denoise/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cfg_id: SW.wpCfgId, params }),
    });
    const d = await res.json();
    if (!res.ok) { _ddAppend(`ERROR: ${d.error || 'Unknown'}`, 'err'); return; }
    _ddJobId = d.id;
    _ddAppend(`[SeisWork] Job ${d.id} started — DeepDenoiser standalone`, 'hdr');
    if (btn)  { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Run DeepDenoiser'; }
    if (stop) stop.style.display = '';
    _ddPollTimer = setInterval(_ddPoll, 1500);
  } catch (e) {
    _ddAppend(`ERROR: ${e.message}`, 'err');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Run DeepDenoiser'; }
  }
}

async function stopDenoiseJob() {
  if (!_ddJobId) return;
  clearInterval(_ddPollTimer);
  await fetch(`/api/denoise/jobs/${_ddJobId}`, { method: 'DELETE' });
  _ddAppend('[SeisWork] Job stopped.', 'warn');
  const stop = document.getElementById('btn-stop-deepdenoiser');
  if (stop) stop.style.display = 'none';
}

async function _ddPoll() {
  if (!_ddJobId) return;
  try {
    const res = await fetch(`/api/denoise/jobs/${_ddJobId}/log?offset=${_ddLogOffset}`);
    if (!res.ok) return;
    const d = await res.json();
    (d.lines || []).forEach(l => _ddAppend(l));
    _ddLogOffset = d.offset || _ddLogOffset;

    if (d.state === 'done') {
      clearInterval(_ddPollTimer);
      _ddAppend(`[SeisWork] Done! Output: ${d.out_dir || 'work/denoised_sds/'}`, 'hdr');
      const stop = document.getElementById('btn-stop-deepdenoiser');
      if (stop) stop.style.display = 'none';
    } else if (d.state === 'error' || d.state === 'stopped') {
      clearInterval(_ddPollTimer);
      _ddAppend(`[SeisWork] Job ${d.state}.`, 'err');
      const stop = document.getElementById('btn-stop-deepdenoiser');
      if (stop) stop.style.display = 'none';
    }
  } catch (_) {}
}

// Toggle DeepDenoiser options visibility (card inline di PhaseNet)
(function _initDenoiserToggle() {
  document.addEventListener('DOMContentLoaded', () => {
    const chk = document.getElementById('pn-denoise');
    const opts = document.getElementById('pn-denoise-opts');
    if (chk && opts) {
      chk.addEventListener('change', () => {
        opts.style.display = chk.checked ? '' : 'none';
      });
    }
  });
})();

// ═══════════════════════════════════════════
//  Waveform Viewer
// ═══════════════════════════════════════════
let _wvTrace = null;            // full-day data (drum overview, spectrogram, picks)
let _wvFull = null;            // alias of the full-day fetch
let _wvWin = null;            // current visible-window fetch (3-comp wiggle)
let _wvSpecData = null;
let _wvPicks = [];              // Current overlaid picks [{phase, frac, time, score}]
let _wvZoom = { t0: 0.0, t1: 1.0 }; // Zoom time fractions of the day (0=00:00, 1=24:00)
let _wvPanStart = null;            // mousedown x for pan
let _wvPanZ0 = null;            // zoom state at pan start
let _wvRefetchTimer = null;        // debounce window re-fetch on zoom/pan
let _wvWinReq = 0;            // request id to drop stale window responses

function _populateStaPicker() {
  const sel = document.getElementById('wv-sta-pick');
  const dateEl = document.getElementById('wv-date-pick');
  if (!sel || !_wpCfgMeta) return;
  const stations = _wpCfgMeta.stations || [];
  const region = _wpCfgMeta.region || {};
  sel.innerHTML = '<option value="">— select —</option>'
    + stations.map(s => `<option value="${SW.esc(s.network + '.' + s.station)}">${SW.esc(s.network + '.' + s.station)}</option>`).join('');
  if (dateEl && !dateEl.value && region.starttime)
    dateEl.value = region.starttime.slice(0, 10);
}
function onWvStaChange() { fetchWvChannels(); }
async function fetchWvChannels() {
  const staPick = (document.getElementById('wv-sta-pick') || {}).value || '';
  const dateVal = (document.getElementById('wv-date-pick') || {}).value || '';
  const sel = document.getElementById('wv-cha-pick');
  if (!staPick || !dateVal || !SW.wpCfgId || !sel) return;
  const [net, sta] = staPick.split('.');
  try {
    const res = await fetch(`/api/waveform/channels?cfg_id=${SW.wpCfgId}&net=${encodeURIComponent(net)}&sta=${encodeURIComponent(sta)}&date=${encodeURIComponent(dateVal)}`);
    const chas = await res.json();
    if (!Array.isArray(chas) || !chas.length) return;
    const hasHH = chas.some(c => c.startsWith('HH')), hasBH = chas.some(c => c.startsWith('BH')), hasEH = chas.some(c => c.startsWith('EH'));
    let opts = '';
    if (hasHH) opts += '<option value="HH?">HH? (all 3C)</option>';
    if (hasBH) opts += '<option value="BH?">BH? (all 3C)</option>';
    if (hasEH) opts += '<option value="EH?">EH? (all 3C)</option>';
    opts += chas.map(c => `<option value="${SW.esc(c)}">${SW.esc(c)}</option>`).join('');
    sel.innerHTML = opts;
    document.getElementById('wv-cha-info').textContent = `${chas.length} channels available`;
  } catch { }
}
function onWvSpecToggle() {
  const on = document.getElementById('wv-spec-toggle').checked;
  const sc = document.getElementById('wv-spec-canvas');
  if (!on) { sc.style.display = 'none'; return; }
  if (_wvSpecData) { sc.style.display = ''; }
  else if (_wvTrace) { loadWaveformSpectro(); }
}
// ── Populate pick job selector in waveform viewer ───────────────────────────
async function _populatePickJobSelector() {
  const sel = document.getElementById('wv-pick-job');
  if (!sel || !SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/picking/jobs?cfg_id=${SW.activeConfigId}`)).json();
    const done = jobs.filter(j => j.state === 'done');
    sel.innerHTML = '<option value="">— none —</option>' +
      done.map(j => `<option value="${SW.esc(j.id)}">${SW.esc(j.method || '?')} · ${SW.esc(j.id.slice(0, 8))} (${j.picks ? j.picks.total : '?'} picks)</option>`).join('');
  } catch { }
}

async function onWvPickJobChange() {
  const jobId = (document.getElementById('wv-pick-job') || {}).value || '';
  const staPick = (document.getElementById('wv-sta-pick') || {}).value || '';
  const dateVal = (document.getElementById('wv-date-pick') || {}).value || '';
  _wvPicks = [];
  if (jobId && staPick && dateVal) {
    try {
      const res = await fetch(`/api/picking/jobs/${jobId}/picks-for-viewer?station=${encodeURIComponent(staPick)}&date=${encodeURIComponent(dateVal)}`);
      const d = await res.json();
      if (Array.isArray(d)) _wvPicks = d;
    } catch { }
  }
  if (_wvTrace) _redrawWaveform();
}

// ── Percentile normalisasi amplitude (Bug 3 fix) ─────────────────────────────
function _pctNormAmp(tr) {
  const allAbs = [];
  for (let i = 0; i < tr.maxs.length; i++) {
    allAbs.push(Math.abs(tr.maxs[i]));
    allAbs.push(Math.abs(tr.mins[i]));
  }
  allAbs.sort((a, b) => a - b);
  // Use 99.5th percentile so spikes don't crush the rest of the trace
  const idx = Math.floor(allAbs.length * 0.995);
  return Math.max(allAbs[idx] || 0, tr.max_amp * 0.01, 1);
}

// ── Zoom helpers ─────────────────────────────────────────────────────────────
function _wvTimeLabel(t) {
  const h = Math.floor(t * 24), m = Math.floor((t * 24 - h) * 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}
function _updateZoomBar() {
  const bar = document.getElementById('wv-zoom-bar');
  const info = document.getElementById('wv-zoom-info');
  const mode = (document.getElementById('wv-view-mode') || {}).value || 'drum';
  if (!bar || !_wvTrace || mode === 'drum') { if (bar) bar.style.display = 'none'; return; }
  bar.style.display = '';
  const isFullDay = (_wvZoom.t0 <= 0.0001 && _wvZoom.t1 >= 0.9999);
  if (info) {
    info.textContent = isFullDay
      ? 'Full day (00:00 – 24:00)'
      : `${_wvTimeLabel(_wvZoom.t0)} – ${_wvTimeLabel(_wvZoom.t1)}  (×${(1 / (_wvZoom.t1 - _wvZoom.t0)).toFixed(1)} zoom)`;
  }
  const btn = document.getElementById('btn-wv-zoom-reset');
  if (btn) btn.style.display = isFullDay ? 'none' : '';
}
function resetWvZoom() {
  _wvZoom = { t0: 0.0, t1: 1.0 };
  if (_wvTrace) _redrawWaveform();
}
function _redrawWaveform() {
  if (!_wvTrace) return;
  const mode = (document.getElementById('wv-view-mode') || {}).value || 'drum';
  drawWaveformCanvas(_wvTrace, mode);
}

// Mode switch: drum = static 24 h overview (no zoom); 3-comp = focused wiggle.
function onWvModeChange() {
  if (!_wvFull) return;
  const mode = (document.getElementById('wv-view-mode') || {}).value || 'drum';
  if (mode === 'drum') {
    _wvZoom = { t0: 0, t1: 1 };          // drum does not need zoom — always full day
    _redrawWaveform();
  } else {
    _wvFocusComp3();                      // immediately show clear wiggle
  }
}

// Center the 3-component view on the most energetic ~90 s window of the day (like
// the Result View centers on the event) so a clean raw wiggle is visible at once
// instead of a full-day envelope blob, then re-fetch raw samples for that window.
function _wvFocusComp3() {
  const full = _wvFull;
  if (!full || !full.traces) { _redrawWaveform(); return; }
  const chas = Object.keys(full.traces);
  const zc = chas.find(c => c.endsWith('Z')) || chas[0];
  const tr = full.traces[zc];
  const env = tr && (tr.rms || tr.maxs);
  if (!env || !env.length) { _redrawWaveform(); _wvRefetchWindow(); return; }
  let bi = 0, bv = -Infinity;
  for (let i = 0; i < env.length; i++) { const v = Math.abs(env[i]); if (v > bv) { bv = v; bi = i; } }
  const center = (bi + 0.5) / env.length;        // fraction of day at peak energy
  // ±25 s (50 s window): at 100–200 Hz this stays under the backend raw-sample cap
  // (≈12 000 samples) so the focused view returns a true per-sample wiggle, not an
  // envelope — wider (e.g. 90 s @200 Hz = 18 000) would fall back to the blob.
  const half = 25 / 86400;
  let t0 = center - half, t1 = center + half;
  if (t0 < 0) { t1 -= t0; t0 = 0; }
  if (t1 > 1) { t0 -= (t1 - 1); t1 = 1; }
  _wvZoom = { t0: Math.max(0, t0), t1: Math.min(1, t1) };
  _redrawWaveform();
  _wvRefetchWindow();
}

// ── Canvas event listeners for zoom/pan ──────────────────────────────────────
let _wvZoomPanInited = false;
function _initWvZoomPan() {
  if (_wvZoomPanInited) return;   // prevent duplicate listeners on each Load press
  _wvZoomPanInited = true;
  const canvas = document.getElementById('wv-main-canvas');
  if (!canvas) return;

  const _wvMode = () => (document.getElementById('wv-view-mode') || {}).value || 'drum';

  canvas.addEventListener('wheel', e => {
    if (_wvMode() === 'drum') return;   // drum does not need zoom
    e.preventDefault();
    if (!_wvTrace) return;
    const rect = canvas.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    const span = _wvZoom.t1 - _wvZoom.t0;
    const factor = e.deltaY > 0 ? 1.35 : 0.74;  // out / in
    const newSpan = Math.max(0.005, Math.min(1.0, span * factor));
    const pivot = _wvZoom.t0 + frac * span;       // time point under cursor
    let t0 = pivot - frac * newSpan;
    let t1 = t0 + newSpan;
    if (t0 < 0) { t1 -= t0; t0 = 0; }
    if (t1 > 1) { t0 -= (t1 - 1); t1 = 1; }
    _wvZoom = { t0: Math.max(0, t0), t1: Math.min(1, t1) };
    _redrawWaveform();
    _wvRefetchWindow();
  }, { passive: false });

  canvas.addEventListener('mousedown', e => {
    if (!_wvTrace || _wvMode() === 'drum') return;   // no pan in drum
    _wvPanStart = e.clientX;
    _wvPanZ0 = { ..._wvZoom };
    canvas.style.cursor = 'grabbing';
  });
  canvas.addEventListener('mousemove', e => {
    if (_wvPanStart === null) return;
    const W = canvas.offsetWidth;
    const dx = e.clientX - _wvPanStart;
    const span = _wvPanZ0.t1 - _wvPanZ0.t0;
    const dt = -(dx / W) * span;
    let t0 = _wvPanZ0.t0 + dt;
    let t1 = _wvPanZ0.t1 + dt;
    if (t0 < 0) { t1 -= t0; t0 = 0; }
    if (t1 > 1) { t0 -= (t1 - 1); t1 = 1; }
    _wvZoom = { t0: Math.max(0, t0), t1: Math.min(1, t1) };
    _redrawWaveform();
  });
  const _stopPan = () => {
    if (_wvPanStart !== null) _wvRefetchWindow();   // crisp re-fetch after pan
    _wvPanStart = null; _wvPanZ0 = null;
    const c = document.getElementById('wv-main-canvas');
    if (c) c.style.cursor = 'grab';
  };
  canvas.addEventListener('mouseup', _stopPan);
  canvas.addEventListener('mouseleave', _stopPan);
}

async function loadWaveformViewer() {
  const staPick = (document.getElementById('wv-sta-pick') || {}).value || '';
  const dateVal = (document.getElementById('wv-date-pick') || {}).value || '';
  const chaVal = (document.getElementById('wv-cha-pick') || {}).value || 'HH?';
  const modeVal = (document.getElementById('wv-view-mode') || {}).value || 'drum';
  const msgEl = document.getElementById('wv-msg');
  const btn = document.getElementById('btn-wv-load');
  if (!staPick) { msgEl.innerHTML = '<span style="color:#f44336">Select a station first</span>'; return; }
  if (!dateVal) { msgEl.innerHTML = '<span style="color:#f44336">Select a date</span>'; return; }
  if (!SW.wpCfgId) return;
  const [net, sta] = staPick.split('.');
  btn.disabled = true;
  msgEl.innerHTML = '<span class="sw-spinner"></span> Loading waveform…';
  document.getElementById('wv-canvas-outer').style.display = 'none';
  document.getElementById('wv-spec-canvas').style.display = 'none';
  document.getElementById('wv-zoom-bar').style.display = 'none';
  _wvTrace = null; _wvFull = null; _wvWin = null; _wvSpecData = null; _wvPicks = []; _wvZoom = { t0: 0, t1: 1 };
  try {
    const outer = document.getElementById('wv-canvas-outer');
    // Drum lays 24 hour-rows side by side, each row ≈ full width → need ~1 bin per
    // output pixel per row (≈ width × 24), else each envelope bin renders as a fat
    // box. Envelope arrays are cheap, so fetch high-res for both modes.
    const baseW = outer.offsetWidth || 900;
    const px = Math.min(36000, Math.max(1800, Math.floor(baseW * 24)));
    const url = `/api/waveform/trace?cfg_id=${SW.wpCfgId}&net=${encodeURIComponent(net)}&sta=${encodeURIComponent(sta)}&cha=${encodeURIComponent(chaVal)}&date=${encodeURIComponent(dateVal)}&px=${px}&t0=0&t1=1`;
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) { msgEl.innerHTML = `<span style="color:#f44336"><i class="bi bi-x-circle"></i> ${SW.esc(data.error || 'Error')}</span>`; return; }
    _wvTrace = data; _wvFull = data; _wvWin = data; msgEl.innerHTML = '';
    // Initialize zoom/pan listeners once
    _initWvZoomPan();
    if (modeVal === 'drum') drawWaveformCanvas(data, modeVal);
    else _wvFocusComp3();            // 3-comp: focus directly on the strongest signal → a clear wiggle
    // Populate pick job selector and reload picks if a job is already selected
    await _populatePickJobSelector();
    await onWvPickJobChange();
    if (document.getElementById('wv-spec-toggle').checked) loadWaveformSpectro();
  } catch (e) { msgEl.innerHTML = `<span style="color:#f44336">Error: ${SW.esc(e.message)}</span>`; }
  finally { btn.disabled = false; }
}

function drawWaveformCanvas(_ignored, mode) {
  const outer = document.getElementById('wv-canvas-outer');
  outer.style.display = '';
  const oldPick = document.getElementById('wv-pick-canvas');
  if (oldPick) oldPick.remove();
  const W = outer.offsetWidth || 900;
  if (mode === 'drum') _drawDrumMode(W);
  else _drawComp3Mode(W);
  const canvas = document.getElementById('wv-main-canvas');
  if (canvas) canvas.style.cursor = (mode === 'drum') ? 'default' : 'grab';
  _updateZoomBar();
}

// Drum = full-day daily overview (envelope), rows = visible hours.
function _drawDrumMode(W) {
  const data = _wvFull || _wvTrace;
  if (!data || !data.traces) return;
  _wvZoom = { t0: 0, t1: 1 };                     // drum always full 24 h (no zoom)
  const canvas = document.getElementById('wv-main-canvas');
  const chas = Object.keys(data.traces);
  const anyTr = Object.values(data.traces)[0];
  if (!anyTr.mins) return;                       // drum needs envelope (full-day)
  const total = anyTr.mins.length;
  const i0 = Math.max(0, Math.floor(_wvZoom.t0 * total));
  const i1 = Math.min(total, Math.ceil(_wvZoom.t1 * total));
  i0_last = i0; i1_last = i1;
  const sliced = {};
  for (const [ch, tr] of Object.entries(data.traces))
    sliced[ch] = { ...tr, mins: tr.mins.slice(i0, i1), maxs: tr.maxs.slice(i0, i1), rms: tr.rms.slice(i0, i1) };
  const visHours = Math.max(1, Math.round((_wvZoom.t1 - _wvZoom.t0) * 24));
  const ROWS = visHours;
  const ROW_H = Math.max(22, Math.min(46, Math.floor((W * 0.6) / Math.max(1, ROWS))));
  const H = ROWS * (ROW_H + 1) + 16;
  canvas.width = W; canvas.height = H;
  canvas.style.cssText = 'display:block;width:100%';
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);
  const zCha = chas.find(c => c.endsWith('Z')) || chas[0];
  _renderDrum(ctx, sliced[zCha], data.traces[zCha], W, ROWS, ROW_H, zCha, _wvZoom.t0);
  _updateWvInfoBar(data, zCha);
  _overlayPickMarkers(canvas, ctx, W, H, ROWS, ROW_H, true);
}

// Slice a (full-day or windowed) fetch down to the visible zoom window, so a
// zoom/pan shows an instant preview before the crisp re-fetch arrives.
function _sliceWinToZoom(win, zoom) {
  const wt0 = (win.t0 != null ? win.t0 : 0), wt1 = (win.t1 != null ? win.t1 : 1);
  const span = wt1 - wt0;
  if (span <= 0) return win.traces;
  if (Math.abs(wt0 - zoom.t0) < 1e-4 && Math.abs(wt1 - zoom.t1) < 1e-4) return win.traces;
  const f0 = Math.max(0, (zoom.t0 - wt0) / span);
  const f1 = Math.min(1, (zoom.t1 - wt0) / span);
  const out = {};
  for (const [ch, tr] of Object.entries(win.traces)) {
    if (tr.samples) {
      const m = tr.samples.length;
      out[ch] = { ...tr, samples: tr.samples.slice(Math.floor(f0 * m), Math.max(Math.floor(f0 * m) + 1, Math.ceil(f1 * m))) };
    } else if (tr.maxs) {
      const m = tr.maxs.length, a = Math.floor(f0 * m), b = Math.max(a + 1, Math.ceil(f1 * m));
      out[ch] = { ...tr, mins: tr.mins.slice(a, b), maxs: tr.maxs.slice(a, b), rms: (tr.rms || []).slice(a, b) };
    } else out[ch] = tr;
  }
  return out;
}

// 3-Component = tall, centered wiggle traces of the visible window (re-fetched).
function _drawComp3Mode(W) {
  const win = _wvWin || _wvTrace;
  if (!win || !win.traces) return;
  const canvas = document.getElementById('wv-main-canvas');
  const traces = _sliceWinToZoom(win, _wvZoom);
  const chas = Object.keys(traces);
  if (!chas.length) return;
  // Big, centered: ~150px per channel so the wiggle shape is clearly visible.
  const ROW_H = Math.max(110, Math.min(220, Math.floor((W * 0.30))));
  const H = chas.length * (ROW_H + 6) + 10;
  canvas.width = W; canvas.height = H;
  canvas.style.cssText = 'display:block;width:100%';
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, 0, W, H);
  _render3CompWiggle(ctx, { ...win, traces }, W, ROW_H, _wvZoom);
  _updateWvInfoBar(_wvFull || win, chas[0]);
  _overlayPickMarkers(canvas, ctx, W, H, chas.length, ROW_H + 6, false);
}

// Mean (DC offset) of a trace — seismic data often sits on a huge DC level
// (e.g. ~5e6 counts); plotting must be relative to the mean or the tiny real
// oscillation is invisible (clamped flat to the row edge). SeisComP removes the
// mean for display; we do the same here.
function _wvMean(tr) {
  if (tr.samples && tr.samples.length) {
    let s = 0; for (const v of tr.samples) s += v; return s / tr.samples.length;
  }
  if (tr.maxs && tr.maxs.length) {
    let s = 0; for (let i = 0; i < tr.maxs.length; i++) s += (tr.maxs[i] + tr.mins[i]) / 2;
    return s / tr.maxs.length;
  }
  return 0;
}
// Percentile amplitude norm of the DEMEANED trace (robust to spikes).
function _wvNorm(tr, mean) {
  mean = mean || 0;
  let arr;
  if (tr.samples) arr = tr.samples.map(v => Math.abs(v - mean));
  else arr = (tr.maxs || []).map(v => Math.abs(v - mean)).concat((tr.mins || []).map(v => Math.abs(v - mean)));
  if (!arr.length) return 1;
  arr.sort((a, b) => a - b);
  const v = arr[Math.floor(arr.length * 0.995)] || arr[arr.length - 1] || 1;
  return v > 0 ? v : 1;
}

function _render3CompWiggle(ctx, win, W, ROW_H, zoom) {
  const order = c => c.endsWith('Z') ? 0 : (c.endsWith('N') || c.endsWith('1')) ? 1 : 2;
  const chas = Object.keys(win.traces).sort((a, b) => order(a) - order(b));
  const LBL_W = 54;
  const dataW = W - LBL_W - 6;
  const tSpan = zoom.t1 - zoom.t0;
  chas.forEach((cha, idx) => {
    const tr = win.traces[cha];
    const y0 = 5 + idx * (ROW_H + 6);
    const yMid = y0 + ROW_H / 2;
    const mean = _wvMean(tr);                       // remove DC offset
    // Amplitude = max |demeaned| in the visible window — identical to the Result
    // View renderer, so the wiggle fills the row WITHOUT spike-clamping flat-tops
    // (the old 99.5-percentile norm + clampY cut peaks off → looked unclean on zoom).
    let amp = 0;
    if (tr.samples) { for (const v of tr.samples) { const a = Math.abs(v - mean); if (a > amp) amp = a; } }
    else if (tr.maxs) { for (let i = 0; i < tr.maxs.length; i++) amp = Math.max(amp, Math.abs(tr.maxs[i] - mean), Math.abs(tr.mins[i] - mean)); }
    amp = amp || 1;
    const ampS = (ROW_H * 0.42) / amp;

    ctx.fillStyle = idx % 2 === 0 ? '#f8fafc' : '#f1f5f9';
    ctx.fillRect(0, y0, W, ROW_H);
    // center baseline
    ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 0.6;
    ctx.beginPath(); ctx.moveTo(LBL_W, yMid); ctx.lineTo(W, yMid); ctx.stroke();

    // Clip to the row (shape preserved, a rare overshoot cut cleanly), then draw a
    // single connected polyline (raw, thin dark line like Result View) or a filled
    // min/max envelope when decimated — never detached vertical sticks.
    const xAt = (i, m) => LBL_W + (m <= 1 ? 0 : (i / (m - 1)) * dataW);
    ctx.save(); ctx.beginPath(); ctx.rect(LBL_W, y0, W - LBL_W, ROW_H); ctx.clip();
    if (tr.samples && tr.samples.length) {
      const s = tr.samples, m = s.length;
      ctx.strokeStyle = '#3a3a3a'; ctx.lineWidth = 0.85; ctx.lineJoin = 'round';
      ctx.beginPath();
      for (let i = 0; i < m; i++) {
        const y = yMid - (s[i] - mean) * ampS;
        i === 0 ? ctx.moveTo(xAt(i, m), y) : ctx.lineTo(xAt(i, m), y);
      }
      ctx.stroke();
    } else if (tr.maxs && tr.maxs.length) {
      const mn = tr.mins, mx = tr.maxs, m = mx.length;
      ctx.fillStyle = '#3a3a3a'; ctx.beginPath();
      for (let i = 0; i < m; i++) {
        const y = yMid - (mx[i] - mean) * ampS;
        i === 0 ? ctx.moveTo(xAt(i, m), y) : ctx.lineTo(xAt(i, m), y);
      }
      for (let i = m - 1; i >= 0; i--) ctx.lineTo(xAt(i, m), yMid - (mn[i] - mean) * ampS);
      ctx.closePath(); ctx.fill();
    }
    ctx.restore();

    // labels
    ctx.fillStyle = '#0f2740'; ctx.font = 'bold 11px "Segoe UI",monospace'; ctx.textAlign = 'left';
    ctx.fillText(cha, 4, y0 + 14);
    ctx.fillStyle = '#64748b'; ctx.font = '8px monospace';
    ctx.fillText(tr.sample_rate + ' Hz', 4, y0 + 26);
    ctx.fillText(tr.samples ? 'raw' : 'env', 4, y0 + 37);

    // time-axis ticks adapt to zoom span
    ctx.fillStyle = '#64748b'; ctx.font = '8px monospace';
    const step = tSpan > 0.5 ? 6 : tSpan > 0.1 ? 1 : tSpan > 0.02 ? 0.25 : tSpan > 0.004 ? (1 / 60) : (1 / 600);
    const t0h = zoom.t0 * 24, t1h = zoom.t1 * 24;
    let tk = Math.ceil(t0h / step) * step;
    while (tk <= t1h + 1e-6) {
      const fx = (tk - t0h) / (t1h - t0h);
      if (fx >= 0 && fx <= 1) {
        const x = LBL_W + fx * dataW;
        ctx.strokeStyle = '#e2e8f0'; ctx.lineWidth = 0.5;
        ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y0 + ROW_H); ctx.stroke();
        const h = Math.floor(tk), mm = Math.floor((tk - h) * 60), ss = Math.round(((tk - h) * 60 - mm) * 60);
        const lbl = step < 1 / 30 ? `${String(h % 24).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
          : `${String(h % 24).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
        ctx.fillStyle = '#475569'; ctx.fillText(lbl, x + 2, y0 + ROW_H - 2);
      }
      tk = Math.round((tk + step) * 1e6) / 1e6;
    }
  });
}

// Debounced re-fetch of the visible window so zoom/pan stays crisp (3-comp).
function _wvRefetchWindow() {
  if (!_wvFull) return;
  const mode = (document.getElementById('wv-view-mode') || {}).value || 'drum';
  if (mode !== 'comp3' && mode !== '3comp' && mode !== 'comp') return;  // only 3-comp
  clearTimeout(_wvRefetchTimer);
  _wvRefetchTimer = setTimeout(async () => {
    const sta = _wvFull.sta, net = _wvFull.net, date = _wvFull.date;
    const cha = (document.getElementById('wv-cha-pick') || {}).value || 'HH?';
    const outer = document.getElementById('wv-canvas-outer');
    const px = Math.max(1800, Math.floor((outer.offsetWidth || 900) * 2));
    const reqId = ++_wvWinReq;
    try {
      const url = `/api/waveform/trace?cfg_id=${SW.wpCfgId}&net=${encodeURIComponent(net)}&sta=${encodeURIComponent(sta)}&cha=${encodeURIComponent(cha)}&date=${encodeURIComponent(date)}&px=${px}&t0=${_wvZoom.t0}&t1=${_wvZoom.t1}`;
      const d = await (await fetch(url)).json();
      if (reqId !== _wvWinReq) return;            // a newer request superseded this
      if (d && d.traces) { _wvWin = d; _redrawWaveform(); }
    } catch (e) { /* keep preview */ }
  }, 140);
}

// Bug 3 fix: drum plot renders with percentile amplitude normalization
function _renderDrum(ctx, tr, trFull, W, ROWS, ROW_H, cha, t0) {
  if (!tr || !tr.mins.length) return;
  const px = tr.mins.length;
  const LBL_W = 38;
  const pxPerRow = Math.ceil(px / ROWS);
  const dataW = W - LBL_W;
  const colW = dataW / pxPerRow;
  const half = ROW_H * 0.46;

  // Demean (DC offset) + 99.5th-percentile norm so the daily trace isn't a flat
  // line clamped to the row edge when the channel has a large DC level.
  const mean = _wvMean(tr);
  const normAmp = _wvNorm(tr, mean);

  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, W, ROW_H * ROWS + ROWS);
  ctx.fillStyle = '#1e3a5f'; ctx.font = 'bold 9px "Segoe UI",monospace'; ctx.textAlign = 'left';
  ctx.fillText(cha + ' — ' + trFull.sample_rate + ' Hz  |  ' +
    ((trFull.n_samples / trFull.sample_rate) / 3600).toFixed(1) + ' h', LBL_W, 11);

  // First-hour offset: how many px in the first row to skip before t0 alignment
  const startHour = Math.floor(t0 * 24);

  for (let row = 0; row < ROWS; row++) {
    const hourLabel = startHour + row;
    const y0 = row * (ROW_H + 1) + 16;
    const yMid = y0 + ROW_H / 2;

    // Subtle alternating row band (ObsPy uses a white face; keep a faint stripe
    // so adjacent same-color rows stay readable).
    ctx.fillStyle = row % 2 === 0 ? '#ffffff' : '#f6f8fb';
    ctx.fillRect(0, y0, W, ROW_H);
    ctx.fillStyle = '#e2e8f0'; ctx.fillRect(0, y0 + ROW_H, W, 1);
    ctx.fillStyle = '#64748b'; ctx.font = '8px monospace'; ctx.textAlign = 'left';
    ctx.fillText(String(hourLabel % 24).padStart(2, '0') + 'h', 2, y0 + ROW_H - 2);

    const a = row * pxPerRow;
    const b = Math.min(px, a + pxPerRow);
    if (b <= a) continue;
    const clampY = v => Math.max(y0, Math.min(y0 + ROW_H, v));
    const col = '#3a3a3a';                          // dark grey trace (seperti semula)
    // FILLED min/max envelope per row — top edge = maxs (left→right), bottom edge =
    // mins (right→left), closed into one continuous band. A filled band reads the
    // waveform pattern far more clearly than a thin stroke (which looked flat/"no
    // pattern" on a light bg).
    ctx.fillStyle = col;
    ctx.strokeStyle = col;
    ctx.lineWidth = 0.6;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    for (let i = a; i < b; i++) {
      const x = LBL_W + (i - a) * colW + 0.5;
      const y = clampY(yMid - ((tr.maxs[i] - mean) / normAmp) * half);
      if (i === a) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    for (let i = b - 1; i >= a; i--) {
      const x = LBL_W + (i - a) * colW + 0.5;
      const y = clampY(yMid - ((tr.mins[i] - mean) / normAmp) * half);
      ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
}

// ── Pick marker overlay (Bug 2) ───────────────────────────────────────────────
function _overlayPickMarkers(canvas, ctx, W, H, ROWS, ROW_H, isDrum) {
  if (!_wvPicks || !_wvPicks.length) return;
  const LBL_W = isDrum ? 38 : 50;
  const dataW = W - LBL_W;
  const t0 = _wvZoom.t0, t1 = _wvZoom.t1, span = t1 - t0;

  // Draw pick overlay canvas on top
  let pickCanvas = document.getElementById('wv-pick-canvas');
  if (!pickCanvas) {
    pickCanvas = document.createElement('canvas');
    pickCanvas.id = 'wv-pick-canvas';
    pickCanvas.className = 'wv-pick-canvas';
    canvas.parentNode.insertBefore(pickCanvas, canvas.nextSibling);
  }
  pickCanvas.width = canvas.width;
  pickCanvas.height = canvas.height;
  pickCanvas.style.cssText = `position:absolute;top:0;left:0;width:100%;height:${canvas.style.height || '100%'};pointer-events:none`;
  const pctx = pickCanvas.getContext('2d');
  pctx.clearRect(0, 0, pickCanvas.width, pickCanvas.height);

  const HDR_H = isDrum ? 16 : 0;

  _wvPicks.forEach(pk => {
    const frac = pk.frac;
    if (frac < t0 || frac > t1) return;

    const isP = (pk.phase || '').toUpperCase().startsWith('P');
    const col = isP ? 'rgba(255,100,80,0.85)' : 'rgba(60,180,255,0.85)';

    if (isDrum) {
      // Map frac → row + column position in drum plot
      const rowCount = ROWS;
      const pxPerRow = Math.ceil(((i1_last - i0_last) || 1) / rowCount);
      // Compute which row+col the pick falls in
      const relFrac = (frac - t0) / span;         // 0..1 within visible range
      const totalPx = ROWS * pxPerRow;
      const piFloat = relFrac * totalPx;
      const row = Math.floor(piFloat / pxPerRow);
      const col_px = piFloat - row * pxPerRow;
      if (row < 0 || row >= ROWS) return;
      const y0 = row * (ROW_H + 1) + HDR_H;
      const x = LBL_W + (col_px / pxPerRow) * dataW;
      pctx.strokeStyle = col; pctx.lineWidth = 1.5;
      pctx.beginPath(); pctx.moveTo(x, y0); pctx.lineTo(x, y0 + ROW_H); pctx.stroke();
      // label
      pctx.fillStyle = col; pctx.font = 'bold 7px monospace'; pctx.textAlign = 'left';
      pctx.fillText(isP ? 'P' : 'S', x + 2, y0 + 8);
    } else {
      // 3-comp: vertical line spanning all rows
      const x = LBL_W + ((frac - t0) / span) * dataW;
      pctx.strokeStyle = col; pctx.lineWidth = 1.5;
      pctx.setLineDash([4, 3]);
      pctx.beginPath(); pctx.moveTo(x, 0); pctx.lineTo(x, H); pctx.stroke();
      pctx.setLineDash([]);
      // label at top
      pctx.fillStyle = col; pctx.font = 'bold 9px monospace'; pctx.textAlign = 'center';
      pctx.fillText(isP ? 'P' : 'S', x, 14);
      // time tooltip via title-like rendering
      const tm = (pk.time || '').slice(11, 19);
      pctx.fillStyle = 'rgba(0,0,0,0.55)';
      const tw = pctx.measureText(tm).width;
      pctx.fillRect(x - tw / 2 - 2, 16, tw + 4, 10);
      pctx.fillStyle = col; pctx.font = '7px monospace';
      pctx.fillText(tm, x, 25);
    }
  });
}

// Drum overlay needs i0/i1 — track them here as module-level (set in drawWaveformCanvas)
let i0_last = 0, i1_last = 0;
function _updateWvInfoBar(data, cha) {
  const tr = data.traces[cha]; const bar = document.getElementById('wv-info-bar');
  if (!tr || !bar) return;
  const allC = Object.keys(data.traces).sort().join(', ');
  const dur = (tr.n_samples / tr.sample_rate / 3600).toFixed(2);
  bar.innerHTML = `
    <span><b>${SW.esc(data.net)}.${SW.esc(data.sta)}</b></span>
    <span><b>${SW.esc(data.date)}</b></span>
    <span>Ch: <b>${SW.esc(allC)}</b></span>
    <span>SR: <b>${tr.sample_rate} Hz</b></span>
    <span>Samples: <b>${tr.n_samples.toLocaleString()}</b></span>
    <span>Duration: <b>${dur} h</b></span>
    <span>Max amp: <b>${tr.max_amp > 999 ? (tr.max_amp / 1000).toFixed(1) + 'k' : tr.max_amp.toFixed(0)} counts</b></span>
    <span style="color:var(--border)">|</span>
    <span>${SW.esc(tr.start)} → ${SW.esc(tr.end)}</span>
  `;
}
async function loadWaveformSpectro() {
  if (!_wvTrace || !SW.wpCfgId) return;
  const data = _wvTrace, chas = Object.keys(data.traces);
  const zCha = chas.find(c => c.endsWith('Z')) || chas[0];
  const msgEl = document.getElementById('wv-msg');
  if (!zCha) return;
  msgEl.innerHTML = '<span class="sw-spinner"></span> Computing spectrogram (first 1 hour)…';
  try {
    const url = `/api/waveform/spectrogram?cfg_id=${SW.wpCfgId}&net=${encodeURIComponent(data.net)}&sta=${encodeURIComponent(data.sta)}&cha=${encodeURIComponent(zCha)}&date=${encodeURIComponent(data.date)}`;
    const res = await fetch(url); const sd = await res.json();
    if (!res.ok) { msgEl.innerHTML = `<span style="color:#f59e0b">Spectrogram: ${SW.esc(sd.error)}</span>`; return; }
    _wvSpecData = sd;
    msgEl.innerHTML = `<span style="color:var(--accent2);font-size:.68rem"><i class="bi bi-check-circle"></i> Spectrogram ${SW.esc(sd.channel)} &nbsp;·&nbsp; ${SW.esc(sd.start.slice(11, 19))}–${SW.esc(sd.end.slice(11, 19))} &nbsp;·&nbsp; ${sd.freqs.length} freq bins × ${sd.times.length} time bins</span>`;
    _drawSpecOverlay(sd);
    const specEl = document.getElementById('wv-spec-canvas');
    const pane = specEl && specEl.closest('.wp-content');
    if (pane && specEl) {
      const offset = specEl.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
      pane.scrollTo({ top: offset - 20, behavior: 'smooth' });
    }
  } catch (e) { msgEl.innerHTML = `<span style="color:#f59e0b">Spectrogram error: ${SW.esc(e.message)}</span>`; }
}
function _drawSpecOverlay(sd) {
  const canvas = document.getElementById('wv-main-canvas');
  const spec = document.getElementById('wv-spec-canvas');
  const W = canvas.width, SPEC_H = 160;
  spec.width = W; spec.height = SPEC_H;
  spec.style.cssText = 'display:block;position:absolute;bottom:28px;left:0;width:100%;pointer-events:none;opacity:0.82;';
  const ctx = spec.getContext('2d'); ctx.clearRect(0, 0, W, SPEC_H);
  const { freqs, times, Sxx, vmin, vmax } = sd;
  const nf = freqs.length, nt = times.length;
  if (!nf || !nt) return;
  const range = vmax - vmin || 1, cellW = W / nt, cellH = SPEC_H / nf;
  ctx.fillStyle = 'rgba(4,6,16,0.95)'; ctx.fillRect(0, 0, W, SPEC_H);
  for (let ti = 0; ti < nt; ti++) {
    for (let fi = 0; fi < nf; fi++) {
      const val = Math.max(0, Math.min(1, (Sxx[fi][ti] - vmin) / range));
      const [r, g, b] = _viridis(val); ctx.fillStyle = `rgb(${r},${g},${b})`;
      const y = SPEC_H - (fi + 1) * cellH; ctx.fillRect(ti * cellW, y, Math.ceil(cellW) + 0.5, Math.ceil(cellH) + 0.5);
    }
  }
  ctx.font = '8px monospace'; ctx.textAlign = 'left';
  [1, 5, 10, 20, 50].forEach(hz => {
    const idx = freqs.findIndex(f => f >= hz); if (idx < 0 || idx >= nf) return;
    const y = SPEC_H - (idx / nf) * SPEC_H;
    if (y > 8 && y < SPEC_H - 3) {
      ctx.fillStyle = 'rgba(255,255,255,0.18)'; ctx.fillRect(0, y, W, 0.5);
      ctx.fillStyle = 'rgba(160,200,240,0.7)'; ctx.fillText(hz + 'Hz', 2, y - 2);
    }
  });
  ctx.fillStyle = 'rgba(33,150,243,.65)'; ctx.font = '8.5px monospace'; ctx.textAlign = 'left';
  ctx.fillText(`Spectrogram  ${SW.esc(sd.channel)}  0–${Math.round(freqs[freqs.length - 1])} Hz`, 34, 12);
}
function _viridis(t) {
  const s = [[68, 1, 84], [71, 17, 100], [72, 33, 115], [70, 48, 126], [66, 64, 134], [59, 79, 139], [52, 94, 141], [46, 108, 142], [40, 122, 142], [35, 136, 142], [31, 150, 139], [32, 163, 134], [40, 175, 127], [62, 188, 115], [90, 200, 100], [122, 209, 81], [157, 217, 59], [194, 223, 35], [230, 228, 25], [253, 231, 37]];
  const i = Math.min(s.length - 2, Math.floor(t * (s.length - 1))), f = t * (s.length - 1) - i;
  return [Math.round(s[i][0] + f * (s[i + 1][0] - s[i][0])), Math.round(s[i][1] + f * (s[i + 1][1] - s[i][1])), Math.round(s[i][2] + f * (s[i + 1][2] - s[i][2]))];
}

// ═══════════════════════════════════════════
//  PICKING JOBS
// ═══════════════════════════════════════════
let _pickPollTimer = null;
let _activePickJobId = null;
let _pickLogOffset = 0;
let _pickBgMode = false;   // false=live, true=background

function getPickParams(method) {
  if (method === 'phasenet') return {
    model: document.getElementById('pn-model').value,
    data_source: document.getElementById('pn-datasrc').value,
    workers: parseInt(document.getElementById('pn-workers').value),
    channels: (document.getElementById('pn-channels') || {}).value || '',
    p_thr: parseFloat(document.getElementById('pn-pthr').value),
    s_thr: parseFloat(document.getElementById('pn-sthr').value),
    min_score: parseFloat(document.getElementById('pn-minscore').value),
    io_proc: parseInt(document.getElementById('pn-ioproc').value),
    gpu_workers: parseInt(document.getElementById('pn-gpuworkers').value),
    denoise: !!(document.getElementById('pn-denoise') || {}).checked,
    denoise_pretrained: (document.getElementById('pn-denoise-pretrained') || {}).value || 'original',
  };
  if (method === 'phasenet_native') return {
    workers: parseInt(document.getElementById('pnn-workers').value),
    p_thr: parseFloat(document.getElementById('pnn-pthr').value),
    s_thr: parseFloat(document.getElementById('pnn-sthr').value),
  };
  if (method === 'eqt') return {
    model: document.getElementById('eqt-model').value,
    data_source: document.getElementById('eqt-datasrc').value,
    workers: parseInt(document.getElementById('eqt-workers').value),
    channels: (document.getElementById('pn-channels') || {}).value || '',
    det_thr: parseFloat(document.getElementById('eqt-dthr').value),
    p_thr: parseFloat(document.getElementById('eqt-pthr').value),
    s_thr: parseFloat(document.getElementById('eqt-sthr').value),
  };
  if (method === 'stalta') return {
    sta: parseFloat(document.getElementById('sl-sta').value),
    lta: parseFloat(document.getElementById('sl-lta').value),
    thr_on: parseFloat(document.getElementById('sl-tron').value),
    thr_off: parseFloat(document.getElementById('sl-troff').value),
    f_lo: parseFloat(document.getElementById('sl-flo').value),
    f_hi: parseFloat(document.getElementById('sl-fhi').value),
    min_len: parseFloat(document.getElementById('sl-minlen').value),
    max_len: parseFloat(document.getElementById('sl-maxlen').value),
    n_workers: parseInt(document.getElementById('sl-workers').value) || 4,
    channels: (document.getElementById('pn-channels') || {}).value || '',
  };
  return {};
}

async function startPickJob(method) {
  if (!SW.wpCfgId) { alert('Open a configuration first'); return; }
  const params = getPickParams(method);
  const bgMode = _pickBgMode;
  const btn = document.getElementById(`btn-run-${method}`);
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="sw-spinner"></span> Starting…'; }
  clearPickTerminal(method);

  try {
    const res = await fetch('/api/picking/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cfg_id: SW.wpCfgId, method, params }),
    });
    const d = await res.json();
    if (!res.ok) { appendPickLine(`ERROR: ${d.error || 'Unknown'}`, 'err', method); return; }
    _activePickJobId = d.id;
    _pickLogOffset = 0;
    appendPickLine(`[SeisWork GUI] Job ${d.id} started — method: ${method}`, 'hdr', method);
    SW.setProgress('pick-terminal', null, 'Job started — waiting for picker output…');
    if (bgMode) {
      appendPickLine(`[SeisWork GUI] Background mode — job running in the background`, 'warn', method);
      appendPickLine(`[SeisWork GUI] Click "View Output" in the job list to see the log`, 'dim', method);
    }
    await refreshPickJobs();
    if (!bgMode) startPickPoll();
  } catch (e) { appendPickLine(`ERROR: ${e.message}`, 'err', method); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Run'; } }
}

function setPickBgMode(mode) {
  _pickBgMode = mode;
  document.querySelectorAll('.pick-mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === (mode ? 'bg' : 'live'));
  });
}

function startPickPoll() {
  if (_pickPollTimer) return;
  _pickPollTimer = setInterval(async () => {
    if (_activePickJobId) await pollPickLog();
    await refreshPickJobs();
  }, 1500);
}
function stopPickPoll() {
  if (_pickPollTimer) { clearInterval(_pickPollTimer); _pickPollTimer = null; }
}

async function pollPickLog() {
  if (!_activePickJobId) return;
  try {
    const res = await fetch(`/api/picking/jobs/${_activePickJobId}/log?offset=${_pickLogOffset}`);
    const d = await res.json();
    if (d.lines && d.lines.length) {
      const method = _getActivePickMethod();
      // Batch-append to avoid per-line reflow
      const term = document.getElementById('pick-terminal');
      if (term) {
        const ph = term.querySelector('.pick-term-placeholder');
        if (ph) ph.remove();
        const frag = document.createDocumentFragment();
        let lastPg = null;
        d.lines.forEach(line => {
          const div = document.createElement('div');
          div.className = `term-${_lineClass(line)}`;
          div.textContent = line;
          frag.appendChild(div);
          const pg = _parsePickProgress(line);
          if (pg) lastPg = pg;
        });
        term.appendChild(frag);
        const rows = term.querySelectorAll('div[class^="term-"]');
        if (rows.length > _TERM_MAX_ROWS) {
          for (let i = 0; i < rows.length - _TERM_MAX_ROWS; i++) rows[i].remove();
        }
        const autoEl = document.getElementById('pick-autoscroll');
        if (!autoEl || autoEl.checked) term.scrollTop = term.scrollHeight;
        if (lastPg) SW.setProgress('pick-terminal', lastPg.frac, lastPg.label);
      }
      _pickLogOffset = d.offset;
    }
    if (d.state && d.state !== 'running' && d.state !== 'pending') {
      stopPickPoll();
      SW.hideProgress('pick-terminal');
      const method = _getActivePickMethod();
      appendPickLine(
        `[SeisWork GUI] Job done: ${d.state.toUpperCase()}`,
        d.state === 'done' ? 'info' : 'err', method
      );
      if (d.state === 'done') await _showPickResult(_activePickJobId, method);
      await refreshPickJobs();
    }
  } catch { }
}

// ── per-method last session storage ──────────────────────────────────────────
const _lastJobByMethod = {};   // method → { jobId, picks, started, finished }

async function _showPickResult(jobId, method) {
  // 1. fetch job meta
  let job = null;
  if (SW.activeConfigId) {
    try {
      const jobs = await (await fetch(`/api/picking/jobs?cfg_id=${SW.activeConfigId}`)).json();
      job = jobs.find(j => j.id === jobId);
    } catch { }
  }
  if (!job) return;

  // 2. append summary to live terminal
  const p = job.picks;
  if (p) {
    appendPickLine('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━', 'dim', method);
    appendPickLine(`  Picks result: total=${p.total}  P=${p.P}  S=${p.S}`, 'info', method);
    appendPickLine('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━', 'dim', method);
  }

  // 3. store in per-method cache and open result panel
  _lastJobByMethod[method] = {
    jobId, picks: job.picks, started: job.started,
    finished: job.finished, method
  };
  await openPickResultPanel(jobId, job);

  // 4. Auto-replace pick overlay in waveform viewer with the latest job
  //    (without requiring the user to manually select from the dropdown)
  await _populatePickJobSelector();
  const sel = document.getElementById('wv-pick-job');
  if (sel && p && p.total > 0) {
    sel.value = jobId;          // select this job in the dropdown
    await onWvPickJobChange();  // reload overlay → replace old picks
  }
}

// ── Result panel ──────────────────────────────────────────────────────────────
let _prActiveTab = 'log';
let _prCurrentJobId = null;

async function openPickResultPanel(jobId, job) {
  _prCurrentJobId = jobId;
  const panel = document.getElementById('pick-result-panel');
  if (!panel) return;
  panel.style.display = '';

  // meta bar
  const p = job.picks;
  const metaEl = document.getElementById('pr-meta');
  if (metaEl) {
    const pBadge = p
      ? `<span class="pick-cnt-badge"><i class="bi bi-lightning-charge" style="font-size:.58rem"></i>${p.total} &nbsp;P:${p.P} S:${p.S}</span>`
      : '';
    metaEl.innerHTML = `
      <span class="pick-job-id" style="font-size:.7rem">${SW.esc(jobId)}</span>
      <span class="pick-job-method">${SW.esc(job.method || '')}</span>
      ${pBadge}
      <span style="font-size:.62rem;color:var(--text-muted)">${SW.esc(job.started || '')}</span>`;
  }

  // download button
  const dlBtn = document.getElementById('pr-dl-btn');
  if (dlBtn) {
    if (p) {
      dlBtn.style.display = '';
      dlBtn.href = `/api/picking/jobs/${jobId}/picks.csv`;
      dlBtn.download = `picks_${jobId}.csv`;
    } else {
      dlBtn.style.display = 'none';
    }
  }

  // load active tab content
  if (_prActiveTab === 'log') await _prLoadLog(jobId);
  else await _prLoadCsv(jobId);
}

function switchPickResultTab(tab) {
  _prActiveTab = tab;
  document.getElementById('prtab-log-btn')?.classList.toggle('active', tab === 'log');
  document.getElementById('prtab-csv-btn')?.classList.toggle('active', tab === 'csv');
  document.getElementById('prtab-log-content').style.display = tab === 'log' ? '' : 'none';
  document.getElementById('prtab-csv-content').style.display = tab === 'csv' ? '' : 'none';
  if (!_prCurrentJobId) return;
  if (tab === 'log') _prLoadLog(_prCurrentJobId);
  else _prLoadCsv(_prCurrentJobId);
}

// ── Skeleton helpers ──────────────────────────────────────────────────────────
function _skelLines(widths) {
  return `<div class="skel-log-wrap">${
    widths.map(w => `<div class="skel-line" style="width:${w};height:9px"></div>`).join('')
  }</div>`;
}
// _skelRows/_fetchJSON → SeisWorkCore (SW.skelRows/SW.fetchJSON)

// State for log pagination (scroll-up = load older lines)
const _prLog = { jobId: null, start: 0, total: 0 };
const _LOG_PAGE = 500;

async function _prLoadLog(jobId) {
  const el = document.getElementById('pr-log-term');
  if (!el) return;
  _prLog.jobId = jobId;
  _prLog.start = 0;
  _prLog.total = 0;

  // Skeleton + inline progress bar
  el.innerHTML = `
    <div class="load-pct-wrap" style="padding:.3rem .6rem;border-bottom:1px solid #0f172a">
      <div class="load-pct-bar" style="width:90px"><div class="load-pct-fill" id="prlog-pct-fill" style="width:0%"></div></div>
      <span id="prlog-pct-txt" style="font-size:.63rem;color:#64748b;font-family:monospace">0%</span>
      <span style="font-size:.63rem;color:#334155">Loading log…</span>
    </div>
    ${_skelLines(['70%','85%','50%','90%','65%','40%','78%','55%','80%','45%'])}`;

  function _setPct(p) {
    const f = document.getElementById('prlog-pct-fill');
    const t = document.getElementById('prlog-pct-txt');
    if (f) f.style.width = p + '%';
    if (t) t.textContent = p + '%';
  }

  try {
    const d = await SW.fetchJSON(`/api/picking/jobs/${jobId}/log-full?limit=${_LOG_PAGE}`, _setPct);
    if (!d.lines || !d.lines.length) {
      el.innerHTML = '<div class="pick-term-placeholder">Log empty.</div>';
      return;
    }
    _prLog.start = d.start;
    _prLog.total = d.total;
    _renderLogPage(el, d, jobId, /*prepend=*/false);
    el.scrollTop = el.scrollHeight;
    _attachLogTopSentinel(el, jobId);
  } catch (e) {
    el.innerHTML = `<div class="term-err">${SW.esc(e.message)}</div>`;
  }
}

function _renderLogPage(el, d, jobId, prepend) {
  const frag = document.createDocumentFragment();
  if (d.has_prev && !prepend) {
    const btn = _makeLogOlderBtn(jobId, d.start);
    frag.appendChild(btn);
    const sent = document.createElement('div');
    sent.id = 'pr-log-top-sentinel'; sent.style.height = '1px';
    frag.appendChild(sent);
    const note = document.createElement('div');
    note.className = 'term-dim';
    note.textContent = `… ${d.start.toLocaleString()} previous rows (scroll ↑ to load) …`;
    frag.appendChild(note);
  }
  d.lines.forEach(line => {
    const div = document.createElement('div');
    div.className = `term-${_lineClass(line)}`;
    div.textContent = line;
    frag.appendChild(div);
  });
  if (prepend) {
    el.insertBefore(frag, el.firstChild);
  } else {
    el.innerHTML = '';
    el.appendChild(frag);
  }
}

function _makeLogOlderBtn(jobId, startLine) {
  const btn = document.createElement('div');
  btn.id = 'pr-log-older-btn';
  btn.className = 'log-load-older';
  btn.textContent = `↑ Load ${startLine.toLocaleString()} earlier rows`;
  btn.onclick = () => _loadOlderLog(jobId);
  return btn;
}

function _attachLogTopSentinel(el, jobId) {
  const sent = document.getElementById('pr-log-top-sentinel');
  if (!sent || _prLog.start <= 0) return;
  const obs = new IntersectionObserver(([entry]) => {
    if (entry.isIntersecting) { obs.disconnect(); _loadOlderLog(jobId); }
  }, { root: el, threshold: 0 });
  obs.observe(sent);
}

async function _loadOlderLog(jobId) {
  const el = document.getElementById('pr-log-term');
  if (!el || _prLog.start <= 0 || _prLog.jobId !== jobId) return;

  const btn  = document.getElementById('pr-log-older-btn');
  const sent = document.getElementById('pr-log-top-sentinel');
  if (btn)  { btn.textContent = 'Loading…'; btn.onclick = null; }
  if (sent) sent.remove();

  const newStart = Math.max(0, _prLog.start - _LOG_PAGE);
  const limit    = _prLog.start - newStart;

  try {
    const d = await SW.fetchJSON(`/api/picking/jobs/${jobId}/log-full?start=${newStart}&limit=${limit}`);
    _prLog.start = newStart;

    if (btn) btn.remove();

    const prevScrollH = el.scrollHeight;

    const frag = document.createDocumentFragment();
    if (newStart > 0) {
      const newBtn = _makeLogOlderBtn(jobId, newStart);
      frag.appendChild(newBtn);
      const newSent = document.createElement('div');
      newSent.id = 'pr-log-top-sentinel'; newSent.style.height = '1px';
      frag.appendChild(newSent);
    }
    const sep = document.createElement('div');
    sep.className = 'term-dim';
    sep.textContent = `─── rows ${newStart + 1}–${newStart + d.lines.length} ───`;
    frag.appendChild(sep);
    d.lines.forEach(line => {
      const div = document.createElement('div');
      div.className = `term-${_lineClass(line)}`;
      div.textContent = line;
      frag.appendChild(div);
    });
    const sep2 = document.createElement('div');
    sep2.className = 'term-dim';
    sep2.textContent = `─────────────────────────────────────────`;
    frag.appendChild(sep2);

    el.insertBefore(frag, el.firstChild);
    // Keep scroll position so old content doesn't jump
    el.scrollTop += el.scrollHeight - prevScrollH;
    _attachLogTopSentinel(el, jobId);
  } catch (e) {
    if (btn) { btn.textContent = '⚠ Failed — click to retry'; btn.onclick = () => _loadOlderLog(jobId); }
  }
}

// State for CSV pagination (scroll-down = load more)
const _prCsv = {};  // { [jobId]: { offset, total, hasMore, loading } }
const _CSV_PAGE = 200;

async function _prLoadCsv(jobId) {
  const infoEl  = document.getElementById('pr-csv-info');
  const theadEl = document.getElementById('pr-csv-thead');
  const tbodyEl = document.getElementById('pr-csv-tbody');
  const wrapEl  = tbodyEl ? tbodyEl.closest('.pr-csv-wrap') : null;
  if (!tbodyEl) return;
  if (tbodyEl.dataset.loaded === jobId) return;

  _prCsv[jobId] = { offset: 0, total: 0, hasMore: false, loading: true };

  // Skeleton + progress bar in info bar
  tbodyEl.innerHTML = Array.from({length: 5}, () =>
    `<tr>${Array.from({length: 6}, (_, i) =>
      `<td><div class="skel-line" style="width:${[70,55,90,45,80,60][i]||65}%;height:8px"></div></td>`
    ).join('')}</tr>`
  ).join('');
  if (infoEl) infoEl.innerHTML = `<span class="load-pct-wrap">
    <span class="load-pct-bar" style="width:80px"><span class="load-pct-fill" id="csv-pct-fill" style="width:0%"></span></span>
    <span id="csv-pct-txt" style="font-size:.62rem;color:#64748b;font-family:monospace">0%</span>
    <span style="font-size:.62rem;color:#334155">Loading…</span>
  </span>`;

  try {
    const d = await SW.fetchJSON(`/api/picking/jobs/${jobId}/preview?offset=0&limit=${_CSV_PAGE}`, pct => {
      const f = document.getElementById('csv-pct-fill');
      const t = document.getElementById('csv-pct-txt');
      if (f) f.style.width = pct + '%';
      if (t) t.textContent = pct + '%';
    });
    if (d.error) { tbodyEl.innerHTML = `<tr><td class="term-err">${SW.esc(d.error)}</td></tr>`; return; }

    _prCsv[jobId] = { offset: d.showing, total: d.total, hasMore: d.has_more, loading: false };

    theadEl.innerHTML = `<tr>${d.columns.map(c => `<th>${SW.esc(c)}</th>`).join('')}</tr>`;

    const frag = document.createDocumentFragment();
    d.rows.forEach(row => {
      const tr = document.createElement('tr');
      tr.innerHTML = row.map(cell => `<td>${SW.esc(String(cell ?? ''))}</td>`).join('');
      frag.appendChild(tr);
    });
    if (d.has_more) frag.appendChild(_makeCsvSentinelRow());

    tbodyEl.innerHTML = '';
    tbodyEl.appendChild(frag);
    tbodyEl.dataset.loaded = jobId;
    _updateCsvInfo(jobId, infoEl);

    if (d.has_more && wrapEl) _attachCsvBottomSentinel(tbodyEl, wrapEl, jobId);
  } catch (e) {
    tbodyEl.innerHTML = `<tr><td class="term-err">${SW.esc(e.message)}</td></tr>`;
  }
}

function _makeCsvSentinelRow() {
  const tr = document.createElement('tr');
  tr.id = 'csv-load-sentinel';
  tr.innerHTML = `<td colspan="99" class="csv-load-more">▼ scroll to load more rows…</td>`;
  return tr;
}

function _updateCsvInfo(jobId, infoEl) {
  if (!infoEl) return;
  const st = _prCsv[jobId];
  if (!st) return;
  const pct = st.total ? Math.round(st.offset / st.total * 100) : 100;
  infoEl.innerHTML = `<span class="load-pct-wrap">
    <span class="load-pct-bar" style="width:70px"><span class="load-pct-fill" style="width:${pct}%"></span></span>
    <span style="font-size:.62rem;color:#64748b;font-family:monospace">${pct}%</span>
    <span style="font-size:.62rem;color:#94a3b8">${st.offset.toLocaleString()} / ${st.total.toLocaleString()} rows</span>
    ${st.hasMore ? '<span style="font-size:.62rem;color:#475569">— scroll↓ lanjut</span>' : ''}
  </span>`;
}

function _attachCsvBottomSentinel(tbodyEl, wrapEl, jobId) {
  const sent = document.getElementById('csv-load-sentinel');
  if (!sent) return;
  const obs = new IntersectionObserver(([entry]) => {
    if (entry.isIntersecting) { obs.disconnect(); _loadMoreCsv(jobId, tbodyEl, wrapEl); }
  }, { root: wrapEl, threshold: 0 });
  obs.observe(sent);
}

async function _loadMoreCsv(jobId, tbodyEl, wrapEl) {
  const st = _prCsv[jobId];
  if (!st || st.loading || !st.hasMore) return;
  st.loading = true;

  const oldSent = document.getElementById('csv-load-sentinel');
  if (oldSent) oldSent.remove();

  const spinner = document.createElement('tr');
  spinner.id = 'csv-load-spinner';
  spinner.innerHTML = `<td colspan="99" style="text-align:center;padding:.4rem"><span class="sw-spinner"></span></td>`;
  tbodyEl.appendChild(spinner);

  const infoEl = document.getElementById('pr-csv-info');

  try {
    const d = await SW.fetchJSON(`/api/picking/jobs/${jobId}/preview?offset=${st.offset}&limit=${_CSV_PAGE}`);
    spinner.remove();
    st.offset += d.showing;
    st.hasMore = d.has_more;
    st.loading = false;

    const frag = document.createDocumentFragment();
    d.rows.forEach(row => {
      const tr = document.createElement('tr');
      tr.innerHTML = row.map(cell => `<td>${SW.esc(String(cell ?? ''))}</td>`).join('');
      frag.appendChild(tr);
    });
    if (d.has_more) frag.appendChild(_makeCsvSentinelRow());
    tbodyEl.appendChild(frag);
    _updateCsvInfo(jobId, infoEl);
    if (d.has_more) _attachCsvBottomSentinel(tbodyEl, wrapEl, jobId);
  } catch {
    spinner.remove();
    st.loading = false;
    const errRow = document.createElement('tr');
    errRow.innerHTML = `<td colspan="99" class="csv-load-more" style="color:#f87171">Failed to load — scroll↓ to retry</td>`;
    tbodyEl.appendChild(errRow);
    _attachCsvBottomSentinel(tbodyEl, wrapEl, jobId);
  }
}

async function _restorePickSession() {
  const method = _getActivePickMethod();
  const panel = document.getElementById('pick-result-panel');

  // Show panel + skeleton log immediately — remove blank gap
  if (panel) {
    panel.style.display = '';
    const logEl = document.getElementById('pr-log-term');
    if (logEl && !logEl.dataset.loaded) {
      logEl.innerHTML = _skelLines(['65%','82%','48%','90%','60%','75%','38%','85%','55%','70%']);
    }
    const metaEl = document.getElementById('pr-meta');
    if (metaEl && !metaEl.innerHTML.trim()) {
      metaEl.innerHTML = `<div class="skel-line" style="width:180px;height:9px"></div>`;
    }
  }

  let jobs = [];
  if (SW.activeConfigId) {
    try {
      jobs = await (await fetch(`/api/picking/jobs?cfg_id=${SW.activeConfigId}`)).json();
    } catch { }
  }

  // 0. Job still RUNNING for this method — reattach live log + poll
  const running = jobs.find(j => j.state === 'running' && j.method === method);
  if (running && !_pickPollTimer) {
    await viewPickJobLog(running.id);   // load log from the start + startPickPoll()
    await refreshPickJobs();
    return;
  }

  // 1. Cache this method's session
  const cached = _lastJobByMethod[method];
  if (cached) {
    const job = jobs.find(j => j.id === cached.jobId);
    if (job) { await openPickResultPanel(cached.jobId, job); return; }
  }

  // 2. Server — last done job for this method
  const last = jobs.find(j => j.state === 'done' && j.method === method);
  if (last) {
    _lastJobByMethod[method] = {
      jobId: last.id, picks: last.picks,
      started: last.started, method
    };
    await openPickResultPanel(last.id, last);
    return;
  }

  // No session for this method — hide panel (clear skeleton too)
  if (panel) {
    panel.style.display = 'none';
    const logEl = document.getElementById('pr-log-term');
    if (logEl) logEl.innerHTML = '<div class="pick-term-placeholder">Loading log…</div>';
    const metaEl = document.getElementById('pr-meta');
    if (metaEl) metaEl.innerHTML = '';
  }
}

function _lineClass(line) {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('traceback') || l.includes('exception')) return 'err';
  if (l.includes('warning') || l.includes('warn')) return 'warn';
  if (l.includes('[seiswork') || l.includes('[ok]') || l.includes('done') || l.includes('completed')) return 'info';
  if (l.startsWith('  ') || l.match(/^\s+at /)) return 'dim';
  return 'ok';
}

function _getActivePickMethod() {
  for (const m of ['phasenet', 'phasenet_native', 'eqt', 'stalta', 'matchlocate', 'deepdenoiser']) {
    const el = document.getElementById(`ptab-${m}`);
    if (el && el.style.display !== 'none') return m;
  }
  return 'phasenet';
}

function appendPickLine(text, cls = 'ok', method) {
  const term = document.getElementById('pick-terminal');
  if (!term) return;
  const ph = term.querySelector('.pick-term-placeholder');
  if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = `term-${cls}`;
  line.textContent = text;
  term.appendChild(line);
  const rows = term.querySelectorAll('div[class^="term-"]');
  if (rows.length > _TERM_MAX_ROWS) {
    for (let i = 0; i < rows.length - _TERM_MAX_ROWS; i++) rows[i].remove();
  }
  const autoEl = document.getElementById('pick-autoscroll');
  if (!autoEl || autoEl.checked) term.scrollTop = term.scrollHeight;
}

function clearPickTerminal(method) {
  const term = document.getElementById('pick-terminal');
  if (term) term.innerHTML = '<div class="pick-term-placeholder">Output will appear here…</div>';
  _pickLogOffset = 0;
}

async function viewPickJobLog(jobId, fullLoad = false) {
  _activePickJobId = jobId;
  _pickLogOffset = 0;
  clearPickTerminal();
  appendPickLine(`[SeisWork GUI] Loading log for job ${jobId}…`, 'dim');
  try {
    const url = fullLoad
      ? `/api/picking/jobs/${jobId}/log?offset=0`
      : `/api/picking/jobs/${jobId}/log?tail=60`;
    const res = await fetch(url);
    const d = await res.json();
    clearPickTerminal();
    const term = document.getElementById('pick-terminal');
    if (d.tail && d.total_size > 0 && term) {
      const banner = document.createElement('div');
      banner.className = 'term-dim';
      banner.style.cssText = 'border-bottom:1px solid #334155;padding-bottom:3px;margin-bottom:3px;cursor:pointer';
      const kb = Math.round(d.total_size / 1024);
      banner.innerHTML = `▲ ${kb} KB log sebelumnya — <span style="color:#60a5fa;text-decoration:underline" onclick="viewPickJobLog('${jobId}',true)">Load full log</span>`;
      term.appendChild(banner);
    }
    let lastPg = null;
    if (d.lines && term) {
      const frag = document.createDocumentFragment();
      d.lines.forEach(l => {
        const div = document.createElement('div');
        div.className = `term-${_lineClass(l)}`;
        div.textContent = l;
        frag.appendChild(div);
        const pg = _parsePickProgress(l);
        if (pg) lastPg = pg;
      });
      term.appendChild(frag);
      term.scrollTop = term.scrollHeight;
    }
    _pickLogOffset = d.offset || 0;
    if (d.state === 'running') {
      if (lastPg) SW.setProgress('pick-terminal', lastPg.frac, lastPg.label);
      else SW.setProgress('pick-terminal', null, 'Job running…');
      startPickPoll();
    } else {
      SW.hideProgress('pick-terminal');
      appendPickLine(`[SeisWork GUI] State: ${d.state || 'unknown'}`, 'info');
      if (d.state === 'done') await _showPickResult(jobId, _getActivePickMethod());
    }
  } catch (e) { appendPickLine(`ERROR: ${e.message}`, 'err'); }
}

async function stopPickJob(jobId) {
  await fetch(`/api/picking/jobs/${jobId}`, { method: 'DELETE' });
  stopPickPoll();
  SW.hideProgress('pick-terminal');
  appendPickLine(`[SeisWork GUI] Job ${jobId} stopped`, 'warn');
  await refreshPickJobs();
}

async function refreshPickJobs() {
  if (!SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/picking/jobs?cfg_id=${SW.activeConfigId}`)).json();
    renderPickJobs(jobs);
  } catch { }
}

function renderPickJobs(jobs) {
  const el = document.getElementById('pick-jobs-list');
  if (!el) return;
  const wrap = document.getElementById('pick-jobs-wrap');
  if (wrap) wrap.style.display = jobs.length ? '' : 'none';
  if (!jobs.length) { el.innerHTML = ''; return; }

  const STATE_CSS = { running: 'js-running', done: 'js-done', error: 'js-error', stopped: 'js-stopped', pending: 'js-running' };
  const STATE_LBL = { running: 'Running', done: 'Done', error: 'Error', stopped: 'Stopped', pending: 'Pending' };

  el.innerHTML = jobs.slice(0, 8).map(j => {
    const sc = STATE_CSS[j.state] || 'js-stopped';
    const slbl = STATE_LBL[j.state] || j.state;
    const picksBadge = j.picks
      ? `<span class="pick-cnt-badge" title="P=${j.picks.P} S=${j.picks.S}">
           <i class="bi bi-lightning-charge" style="font-size:.58rem"></i>${j.picks.total}
         </span>`
      : '';
    const dlBtn = (j.state === 'done' && j.picks)
      ? `<a class="btn-sw btn-ghost-sw btn-sm-sw"
            href="/api/picking/jobs/${j.id}/picks.csv"
            download="picks_${j.id}.csv"
            title="Download picks.csv">
           <i class="bi bi-download"></i>
         </a>`
      : '';
    return `<div class="pick-job-card">
      <span class="pick-job-id">${SW.esc(j.id)}</span>
      <span class="pick-job-method">${SW.esc(j.method || '')}</span>
      <span class="job-state ${sc}" style="margin-left:.2rem">${slbl}</span>
      ${picksBadge}
      <span class="pick-job-view-btn" style="margin-left:auto" onclick="viewPickJobLog('${j.id}')">View log</span>
      ${dlBtn}
      ${j.state === 'running'
        ? `<button class="btn-sw btn-danger-sw btn-sm-sw" onclick="stopPickJob('${j.id}')"><i class="bi bi-stop-fill"></i></button>`
        : `<button class="btn-sw btn-ghost-sw btn-sm-sw" onclick="removePickJob('${j.id}')" title="Remove"><i class="bi bi-x-lg"></i></button>`
      }
    </div>`;
  }).join('');
}

async function removePickJob(jobId) {
  await fetch(`/api/picking/jobs/${jobId}`, { method: 'DELETE' });
  await refreshPickJobs();
}

// ═══════════════════════════════════════════
//  OUTPUT PANEL
// ═══════════════════════════════════════════
let _outTab = 'files';

function switchOutputTab(tab) {
  _outTab = tab;
  ['files', 'jobs', 'dirs'].forEach(t => {
    document.getElementById(`otab-${t}`)?.style.setProperty('display', t === tab ? '' : 'none');
    document.getElementById(`otab-${t}-btn`)?.classList.toggle('active', t === tab);
  });
  refreshOutputPanel();
}

async function refreshOutputPanel() {
  if (_outTab === 'files') await _loadOutputFolders();
  else if (_outTab === 'jobs') await _loadOutputJobs();
  else await _loadOutputDirs();
}

// ── Output Files tab — folder-per-run browser + file viewer modal ──────────────
async function _loadOutputFolders() {
  const el = document.getElementById('out-files-list');
  if (!el) return;
  const cfg = SW.wpCfgId || SW.activeConfigId || '';
  if (!cfg) return;
  try {
    const folders = await (await fetch(`/api/output/folders?cfg_id=${cfg}`)).json();
    if (!folders.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:.73rem;padding:.4rem 0">No output yet. Run one of the functions (picking, association, location, velocity, relocation, detection).</div>';
      return;
    }
    el.innerHTML = folders.map(f => {
      const st = f.state === 'done' ? '#34d399' : (f.state === 'running' ? '#fbbf24' : '#9ca3af');
      const label = `${SW.esc(f.step)}${f.method ? ' / ' + SW.esc(f.method) : ''}`;
      return `
      <div class="out-folder" style="border:1px solid var(--border);border-radius:8px;margin-bottom:.4rem;overflow:hidden">
        <div onclick="toggleOutputFolder('${f.job_id}', this)" style="display:flex;align-items:center;gap:.5rem;padding:.5rem .6rem;cursor:pointer;background:rgba(255,255,255,.02)">
          <i class="bi bi-folder-fill" style="color:#f0c674;font-size:1rem"></i>
          <div style="flex:1;min-width:0">
            <div style="font-size:.76rem;font-weight:600">${label}
              <span style="font-size:.6rem;color:${st}">● ${SW.esc(f.state || '')}</span></div>
            <div style="font-size:.62rem;color:var(--text-muted);font-family:monospace">${SW.esc(f.job_id)} · ${f.n_files} file · ${(f.finished || '').slice(0, 16).replace('T', ' ')}</div>
          </div>
          <i class="bi bi-chevron-down out-folder-chev" style="color:var(--text-muted);transition:transform .15s"></i>
        </div>
        <div class="out-folder-files" data-job="${f.job_id}" data-loaded="0" style="display:none;border-top:1px solid var(--border)"></div>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<div style="color:#f87171;font-size:.72rem">Failed to load: ${SW.esc(e.message)}</div>`;
  }
}

async function toggleOutputFolder(jobId, hdr) {
  const wrap = hdr.parentElement.querySelector('.out-folder-files');
  const chev = hdr.querySelector('.out-folder-chev');
  if (!wrap) return;
  const open = wrap.style.display !== 'none';
  if (open) { wrap.style.display = 'none'; if (chev) chev.style.transform = ''; return; }
  wrap.style.display = '';
  if (chev) chev.style.transform = 'rotate(180deg)';
  if (wrap.dataset.loaded === '1') return;
  wrap.innerHTML = '<div style="padding:.5rem;color:var(--text-muted);font-size:.7rem"><span class="sw-spinner"></span> Loading files…</div>';
  try {
    const d = await (await fetch(`/api/output/files?job=${encodeURIComponent(jobId)}`)).json();
    const files = d.files || [];
    if (!files.length) { wrap.innerHTML = '<div style="padding:.5rem;color:var(--text-muted);font-size:.7rem">Folder empty.</div>'; return; }
    // Build a nested directory tree from the relative paths and render it.
    const tree = _buildFileTree(files);
    wrap.innerHTML = _renderFileTree(jobId, tree, 0);
    wrap.dataset.loaded = '1';
  } catch (e) {
    wrap.innerHTML = `<div style="padding:.5rem;color:#f87171;font-size:.7rem">Failed: ${SW.esc(e.message)}</div>`;
  }
}

// ── File tree (cluster a job's output files by subfolder) ──────────────────────
function _buildFileTree(files) {
  const root = { dirs: {}, files: [] };
  for (const f of files) {
    const parts = (f.rel || f.name).split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [] };
      node = node.dirs[parts[i]];
    }
    node.files.push(f);
  }
  return root;
}
function _countTreeFiles(node) {
  let n = node.files.length;
  for (const d in node.dirs) n += _countTreeFiles(node.dirs[d]);
  return n;
}
function _renderFileTree(jobId, node, depth) {
  let html = '';
  // Sub-directories first (collapsible), then files — both alphabetical.
  for (const dn of Object.keys(node.dirs).sort()) {
    const pad = (0.6 + depth * 1.0).toFixed(2);
    const child = _renderFileTree(jobId, node.dirs[dn], depth + 1);
    const nf = _countTreeFiles(node.dirs[dn]);
    html += `
      <div class="tree-dir">
        <div onclick="_toggleTreeDir(this)" style="display:flex;align-items:center;gap:.4rem;padding:.28rem .6rem;padding-left:${pad}rem;cursor:pointer;font-size:.7rem;border-top:1px solid rgba(255,255,255,.04)" onmouseover="this.style.background='rgba(255,255,255,.03)'" onmouseout="this.style.background=''">
          <i class="bi bi-chevron-right tree-chev" style="font-size:.58rem;color:var(--text-muted);transition:transform .12s"></i>
          <i class="bi bi-folder-fill" style="color:#f0c674"></i>
          <span style="flex:1;font-family:monospace">${SW.esc(dn)}/</span>
          <span style="color:var(--text-muted);font-size:.6rem">${nf} file</span>
        </div>
        <div class="tree-children" style="display:none">${child}</div>
      </div>`;
  }
  for (const f of node.files.slice().sort((a, b) => a.name.localeCompare(b.name))) {
    const pad = (0.6 + depth * 1.0 + 1.0).toFixed(2);
    html += `
      <div onclick="openFileViewer('${jobId}', '${encodeURIComponent(f.rel)}')" style="display:flex;align-items:center;gap:.45rem;padding:.28rem .6rem;padding-left:${pad}rem;cursor:pointer;font-size:.7rem;border-top:1px solid rgba(255,255,255,.04)" onmouseover="this.style.background='rgba(96,165,250,.08)'" onmouseout="this.style.background=''">
        <i class="bi ${_fileIcon(f.name)}" style="color:#60a5fa"></i>
        <span style="flex:1;font-family:monospace">${SW.esc(f.name)}</span>
        <span style="color:var(--text-muted);font-size:.62rem">${_fmtSize(f.size)}</span>
        <a href="/api/output/download?job=${encodeURIComponent(jobId)}&name=${encodeURIComponent(f.rel)}" onclick="event.stopPropagation()" title="Download" style="color:var(--text-muted)"><i class="bi bi-download"></i></a>
      </div>`;
  }
  return html;
}
function _toggleTreeDir(hdr) {
  const children = hdr.parentElement.querySelector('.tree-children');
  const chev = hdr.querySelector('.tree-chev');
  if (!children) return;
  const open = children.style.display !== 'none';
  children.style.display = open ? 'none' : '';
  if (chev) chev.style.transform = open ? '' : 'rotate(90deg)';
}

function _fileIcon(name) {
  const n = (name || '').toLowerCase();
  if (n.endsWith('.csv')) return 'bi-filetype-csv';
  if (n.endsWith('.json')) return 'bi-filetype-json';
  if (n.endsWith('.yaml') || n.endsWith('.yml')) return 'bi-filetype-yml';
  if (n.endsWith('.log') || n.endsWith('.txt') || n.endsWith('.out')) return 'bi-file-text';
  if (n.endsWith('.png') || n.endsWith('.jpg') || n.endsWith('.pdf')) return 'bi-file-image';
  return 'bi-file-earmark';
}
let _ofvPath = '';
async function openFileViewer(jobId, relEnc) {
  const rel = decodeURIComponent(relEnc);
  document.getElementById('ofv-bd').classList.remove('hidden');
  document.getElementById('ofv-name').textContent = rel.split('/').pop();
  document.getElementById('ofv-path').textContent = '…';
  document.getElementById('ofv-size').textContent = '';
  document.getElementById('ofv-loading').style.display = '';
  const pre = document.getElementById('ofv-content');
  pre.style.display = 'none'; pre.textContent = '';
  document.getElementById('ofv-dl').href =
    `/api/output/download?job=${encodeURIComponent(jobId)}&name=${encodeURIComponent(rel)}`;
  try {
    const d = await (await fetch(`/api/output/file?job=${encodeURIComponent(jobId)}&name=${encodeURIComponent(rel)}`)).json();
    if (d.error) throw new Error(d.error);
    _ofvPath = d.path || '';
    document.getElementById('ofv-path').textContent = d.path || '';
    document.getElementById('ofv-size').textContent =
      _fmtSize(d.size) + (d.truncated ? ' (first 1 MB shown)' : '');
    document.getElementById('ofv-loading').style.display = 'none';
    pre.style.display = '';
    pre.textContent = d.binary
      ? '[binary file — cannot be displayed as text. Use Download.]'
      : (d.content || '(empty)');
  } catch (e) {
    document.getElementById('ofv-loading').style.display = 'none';
    pre.style.display = '';
    pre.textContent = 'Failed to load: ' + e.message;
  }
}
function closeFileViewer() {
  document.getElementById('ofv-bd').classList.add('hidden');
}
function copyOfvPath() {
  if (_ofvPath && navigator.clipboard) {
    navigator.clipboard.writeText(_ofvPath).then(() => SW.showAlert('save', 'Path copied', 'ok')).catch(() => {});
  }
}

// ── Picking Jobs tab ──────────────────────────────────────────────────────────
async function _loadOutputJobs() {
  const el = document.getElementById('out-jobs-list');
  if (!el || !SW.activeConfigId) return;
  try {
    const jobs = await (await fetch(`/api/picking/jobs?cfg_id=${SW.activeConfigId}`)).json();
    if (!jobs.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:.73rem;padding:.4rem 0">No picking jobs yet.</div>';
      return;
    }
    el.innerHTML = jobs.map(j => _renderOutputJobCard(j)).join('');
  } catch (e) {
    el.innerHTML = `<div class="term-err" style="font-size:.72rem">${SW.esc(e.message)}</div>`;
  }
}

function _renderOutputJobCard(j) {
  const STATE_CSS = { running: 'js-running', done: 'js-done', error: 'js-error', stopped: 'js-stopped' };
  const sc = STATE_CSS[j.state] || 'js-stopped';
  const slbl = { running: 'Running', done: 'Done', error: 'Error', stopped: 'Stopped' }[j.state] || j.state;
  const picks = j.picks
    ? `<span class="pick-cnt-badge"><i class="bi bi-lightning-charge" style="font-size:.58rem"></i>${j.picks.total} &nbsp;P:${j.picks.P} S:${j.picks.S}</span>`
    : '';
  const dlBtn = (j.state === 'done' && j.picks)
    ? `<a class="btn-sw btn-ghost-sw btn-sm-sw"
          href="/api/picking/jobs/${j.id}/picks.csv"
          download="picks_${j.id}.csv" title="Download picks.csv">
         <i class="bi bi-download"></i>
       </a>`
    : '';
  const jobDir = `seiswork/web/tmp/picking/${j.id}`;
  return `
  <div class="out-job-card" id="ojc-${j.id}">
    <div class="out-job-header" onclick="toggleOutJobFiles('${j.id}')">
      <span class="pick-job-id">${SW.esc(j.id)}</span>
      <span class="pick-job-method">${SW.esc(j.method || '')}</span>
      <span class="job-state ${sc}" style="margin-left:.2rem">${slbl}</span>
      ${picks}
      <span style="margin-left:auto;font-size:.63rem;color:var(--text-muted)">${SW.esc(j.started || '')}</span>
      ${dlBtn}
      <i class="bi bi-chevron-down out-job-chevron" style="font-size:.65rem;color:var(--text-muted)"></i>
    </div>
    <div class="out-job-path"><i class="bi bi-folder2"></i> ${SW.esc(jobDir)}</div>
    <div class="out-job-files" id="ojf-${j.id}" style="display:none"></div>
  </div>`;
}

async function toggleOutJobFiles(jobId) {
  const el = document.getElementById(`ojf-${jobId}`);
  const chv = document.querySelector(`#ojc-${jobId} .out-job-chevron`);
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : '';
  if (chv) chv.style.transform = open ? '' : 'rotate(180deg)';
  if (!open && !el.dataset.loaded) {
    el.innerHTML = '<div style="padding:.3rem .5rem;font-size:.68rem;color:var(--text-muted)"><span class="sw-spinner"></span></div>';
    try {
      const files = await (await fetch(`/api/picking/jobs/${jobId}/files`)).json();
      el.innerHTML = files.length
        ? files.map(f => `
          <div class="out-file-row">
            <i class="bi bi-file-earmark${f.name.endsWith('.csv') ? '-spreadsheet' : f.name.endsWith('.log') ? '-text' : ''}" style="color:var(--text-muted);font-size:.7rem"></i>
            <span class="out-file-name">${SW.esc(f.name)}</span>
            <span class="out-file-size">${_fmtSize(f.size)}</span>
            <span class="out-file-mtime">${SW.esc(f.mtime)}</span>
            <span class="out-file-path" title="${SW.esc(f.path)}">${SW.esc(f.path)}</span>
            ${f.download
            ? `<a class="btn-sw btn-ghost-sw btn-sm-sw" href="/api/picking/jobs/${jobId}/picks.csv" download title="download"><i class="bi bi-download"></i></a>`
            : ''}
          </div>`).join('')
        : '<div style="padding:.3rem .5rem;font-size:.68rem;color:var(--text-muted)">No files.</div>';
      el.dataset.loaded = '1';
    } catch (e) {
      el.innerHTML = `<div class="term-err" style="padding:.3rem;font-size:.68rem">${SW.esc(e.message)}</div>`;
    }
  }
}

// ── Pipeline Dirs tab ─────────────────────────────────────────────────────────
async function _loadOutputDirs() {
  const el = document.getElementById('out-dirs-list');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-muted);font-size:.73rem;padding:.4rem 0"><span class="sw-spinner" style="margin-right:.4rem"></span> Loading…</div>';
  try {
    const dirs = await (await fetch('/api/results')).json();
    el.innerHTML = dirs.map(d => `
    <div class="out-dir-card">
      <div class="out-dir-header">
        <span class="out-dir-label"><i class="bi bi-folder2${d.exists ? '-open' : ''}"></i>&nbsp; ${SW.esc(d.label)}</span>
        <span class="out-dir-count">${d.file_count} file${d.file_count !== 1 ? 's' : ''}</span>
        <span class="out-dir-size">${_fmtSize(d.size_total)}</span>
        ${d.exists
        ? `<span class="out-dir-status out-dir-ok"><i class="bi bi-check-circle-fill"></i></span>`
        : `<span class="out-dir-status out-dir-empty"><i class="bi bi-dash-circle"></i> empty</span>`}
      </div>
      <div class="out-dir-path"><i class="bi bi-folder2"></i> ${SW.esc(d.path)}</div>
      ${d.files.length
        ? `<div class="out-dir-files">${d.files.map(f => `
          <div class="out-file-row">
            <i class="bi bi-file-earmark${f.name.endsWith('.csv') ? '-spreadsheet' : f.name.endsWith('.log') ? '-text' : ''}" style="color:var(--text-muted);font-size:.7rem"></i>
            <span class="out-file-name">${SW.esc(f.name)}</span>
            <span class="out-file-size">${_fmtSize(f.size)}</span>
            <span class="out-file-mtime">${SW.esc(f.mtime)}</span>
            <span class="out-file-path" title="${SW.esc(f.path)}">${SW.esc(f.path)}</span>
          </div>`).join('')}</div>`
        : ''}
    </div>`).join('');
  } catch (e) {
    el.innerHTML = `<div class="term-err" style="font-size:.72rem">${SW.esc(e.message)}</div>`;
  }
}

function _fmtSize(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024, units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), units.length - 1);
  return (bytes / Math.pow(k, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
}

// ═══════════════════════════════════════════
//  SYSTEM MONITOR
// ═══════════════════════════════════════════
let _smTimer = null;
let _smActive = false;

function enterSysmon() {
  _smActive = true;
  pollSysInfo();
  _smTimer = setInterval(pollSysInfo, 2000);
}
function exitSysmon() {
  _smActive = false;
  if (_smTimer) { clearInterval(_smTimer); _smTimer = null; }
}

async function pollSysInfo() {
  if (!_smActive) return;
  try {
    const d = await (await fetch('/api/sysinfo')).json();
    if (d.error) return;
    renderSysInfo(d);
  } catch { }
}

function renderSysInfo(d) {
  _renderCpu(d);
  _renderRam(d);
  _renderGpu(d);
  _renderDisk(d);
  _renderActiveJobs(d);
  _renderProcs(d);
  _renderStatusBar(d);
}

function _renderActiveJobs(d) {
  const el = document.getElementById('sm-active-jobs');
  if (!el) return;
  const jobs = d.active_jobs || [];
  const cnt = document.getElementById('sm-active-count');
  if (cnt) cnt.textContent = jobs.length ? `${jobs.length} running` : '';
  if (!jobs.length) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:.7rem;padding:.3rem 0">No active jobs.</div>`;
    return;
  }
  el.innerHTML = jobs.map(j => {
    const kindLbl = j.kind === 'pick'
      ? `Picking${j.method ? ' · ' + SW.esc(j.method) : ''}`
      : `Pipeline${j.method ? ' · ' + SW.esc(j.method) : ''}`;
    let prog = '', bar = '';
    if (j.batches) {
      const pct = Math.min(100, Math.round(100 * (j.batch || 0) / j.batches));
      prog = `batch ${j.batch}/${j.batches} · ${pct}%`;
      bar = `<div class="mem-bar-track" style="height:6px;margin-top:.25rem">
        <div class="mem-bar-fill" style="width:${pct}%;background:#22c55e"></div>
      </div>`;
    }
    return `<div style="padding:.4rem .1rem;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap">
        <span class="sm-live-dot" style="background:#22c55e"></span>
        <span style="font-weight:600;font-size:.72rem">${kindLbl}</span>
        <span title="SeisWork session ${SW.esc(j.job_id)}" style="font-size:.58rem;color:#fbbf24;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.35);border-radius:3px;padding:0 4px">⬢ ${SW.esc(j.job_id)}</span>
        <span style="margin-left:auto;font-size:.62rem;color:var(--text-muted)">PID ${j.pid} · ${SW.esc(j.elapsed || '')}</span>
      </div>
      ${prog ? `<div style="font-size:.62rem;color:var(--text-muted);margin-top:.15rem">${prog}</div>` : ''}
      ${bar}
    </div>`;
  }).join('');
}

function _renderCpu(d) {
  const el = document.getElementById('sm-cpu-bars');
  if (!el || !d.cpu) return;
  const cores = d.cpu.per_core || [];
  el.innerHTML = `<div class="cpu-grid">${cores.map((pct, i) => {
    const c = pct > 80 ? '#f44336' : pct > 50 ? '#f59e0b' : '#4caf50';
    return `<div class="cpu-bar-row">
      <span class="cpu-bar-lbl">C${i}</span>
      <div class="cpu-bar-track"><div class="cpu-bar-fill" style="width:${pct}%;background:${c}"></div></div>
      <span class="cpu-bar-pct">${pct.toFixed(0)}%</span>
    </div>`;
  }).join('')}</div>`;

  const infoEl = document.getElementById('sm-cpu-info');
  if (infoEl && d.cpu.freq_mhz) {
    infoEl.textContent = `${cores.length} cores · ${(d.cpu.freq_mhz / 1000).toFixed(2)} GHz`;
  }
}

function _renderRam(d) {
  const el = document.getElementById('sm-ram-bars');
  if (!el || !d.ram) return;
  const r = d.ram;
  const pct = r.pct;
  const c = pct > 85 ? '#f44336' : pct > 65 ? '#f59e0b' : '#2196f3';
  el.innerHTML = `<div class="mem-bar-block">
    <div class="mem-bar-header">
      <span class="mem-bar-label">RAM</span>
      <span class="mem-bar-value">${r.used_gb.toFixed(1)} / ${r.total_gb.toFixed(1)} GiB</span>
    </div>
    <div class="mem-bar-track">
      <div class="mem-bar-fill" style="width:${pct}%;background:${c}"></div>
      <div class="mem-bar-text">${pct.toFixed(0)}%</div>
    </div>
  </div>
  <div class="mem-bar-block" style="margin-top:.35rem">
    <div class="mem-bar-header">
      <span class="mem-bar-label">Swap</span>
      <span class="mem-bar-value">${r.swap_used_gb.toFixed(1)} / ${r.swap_total_gb.toFixed(1)} GiB</span>
    </div>
    <div class="mem-bar-track">
      <div class="mem-bar-fill" style="width:${r.swap_pct}%;background:#9c27b0"></div>
      <div class="mem-bar-text">${r.swap_pct.toFixed(0)}%</div>
    </div>
  </div>`;
}

function _renderGpu(d) {
  const el = document.getElementById('sm-gpu-info');
  if (!el) return;
  if (!d.gpu || !d.gpu.length) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:.72rem;padding:.4rem 0">
      <i class="bi bi-gpu-card" style="opacity:.3"></i> No GPU detected (nvidia-smi not available)
    </div>`;
    return;
  }
  el.innerHTML = d.gpu.map(g => {
    const uc = g.util_pct > 80 ? '#f44336' : g.util_pct > 40 ? '#f59e0b' : '#9c27b0';
    const vPct = g.mem_total_mb > 0 ? Math.round(g.mem_used_mb / g.mem_total_mb * 100) : 0;
    return `<div class="gpu-item">
      <div class="gpu-name">${SW.esc(g.name)} — ${g.temp_c}°C</div>
      <div class="gpu-bars">
        <div class="mem-bar-block">
          <div class="mem-bar-header">
            <span class="mem-bar-label">GPU Util</span>
            <span class="mem-bar-value">${g.util_pct}%</span>
          </div>
          <div class="mem-bar-track">
            <div class="mem-bar-fill" style="width:${g.util_pct}%;background:${uc}"></div>
          </div>
        </div>
        <div class="mem-bar-block" style="margin-top:.3rem">
          <div class="mem-bar-header">
            <span class="mem-bar-label">VRAM</span>
            <span class="mem-bar-value">${g.mem_used_mb} / ${g.mem_total_mb} MB (${vPct}%)</span>
          </div>
          <div class="mem-bar-track">
            <div class="mem-bar-fill" style="width:${vPct}%;background:#7b1fa2"></div>
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function _renderDisk(d) {
  const el = document.getElementById('sm-disk-info');
  if (!el || !d.disk) return;
  if (!d.disk.length) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:.72rem">No disk info.</div>`;
    return;
  }
  el.innerHTML = d.disk.slice(0, 4).map(dk => {
    const c = dk.pct > 90 ? '#f44336' : dk.pct > 70 ? '#f59e0b' : '#4caf50';
    return `<div class="mem-bar-block" style="margin-bottom:.35rem">
      <div class="mem-bar-header">
        <span class="mem-bar-label" title="${SW.esc(dk.device)}">${SW.esc(dk.mount)}</span>
        <span class="mem-bar-value">${dk.used_gb.toFixed(1)} / ${dk.total_gb.toFixed(1)} GB</span>
      </div>
      <div class="mem-bar-track">
        <div class="mem-bar-fill" style="width:${dk.pct}%;background:${c}"></div>
        <div class="mem-bar-text">${dk.pct.toFixed(0)}%</div>
      </div>
    </div>`;
  }).join('') + `<div style="font-size:.65rem;color:var(--text-muted);margin-top:.3rem">
    Uptime: ${d.uptime || '–'} &nbsp;·&nbsp; ${d.ts || ''}
  </div>`;
}

function _renderProcs(d) {
  const el = document.getElementById('sm-proc-table-body');
  if (!el || !d.procs) return;
  el.innerHTML = d.procs.slice(0, 12).map(p => {
    const cc = p.cpu_pct > 50 ? 'proc-cpu-hi' : p.cpu_pct > 20 ? 'proc-cpu-md' : 'proc-cpu-lo';
    // Tag SeisWork session processes with their job_id so it's clear which
    // session is loading the system.
    const tag = p.session
      ? ` <span title="SeisWork session ${SW.esc(p.session)}" style="font-size:.58rem;color:#fbbf24;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.35);border-radius:3px;padding:0 4px;margin-left:.3rem;vertical-align:middle">⬢ ${SW.esc(p.session)}</span>`
      : '';
    return `<tr${p.session ? ' style="background:rgba(251,191,36,.05)"' : ''}>
      <td class="proc-pid">${p.pid}</td>
      <td class="proc-name" title="${SW.esc(p.name)}${p.session ? ' — session ' + SW.esc(p.session) : ''}">${SW.esc(p.name)}${tag}</td>
      <td class="r ${cc}">${p.cpu_pct.toFixed(1)}%</td>
      <td class="r">${p.mem_pct.toFixed(1)}%</td>
    </tr>`;
  }).join('');
}

function _renderStatusBar(d) {
  const el = document.getElementById('sm-status-bar');
  if (!el) return;
  const cpu = d.cpu ? d.cpu.total : 0;
  const ram = d.ram ? d.ram.pct : 0;
  const cc = cpu > 80 ? 'hi' : cpu > 50 ? 'md' : 'ok';
  const rc = ram > 85 ? 'hi' : ram > 65 ? 'md' : 'ok';
  el.innerHTML = `
    <div class="sm-stat-item"><div class="sm-live-dot"></div><span class="sm-stat-lbl">Live</span></div>
    <div class="sm-stat-item">
      <span class="sm-stat-lbl">CPU avg:</span>
      <span class="sm-stat-val ${cc}">${cpu.toFixed(0)}%</span>
    </div>
    <div class="sm-stat-item">
      <span class="sm-stat-lbl">RAM:</span>
      <span class="sm-stat-val ${rc}">${d.ram ? d.ram.used_gb.toFixed(1) : '–'} / ${d.ram ? d.ram.total_gb.toFixed(1) : '–'} GB</span>
    </div>
    ${d.gpu && d.gpu.length ? `<div class="sm-stat-item">
      <span class="sm-stat-lbl">GPU:</span>
      <span class="sm-stat-val">${d.gpu[0].util_pct}% · ${d.gpu[0].temp_c}°C</span>
    </div>` : ''}
    <div class="sm-stat-item">
      <span class="sm-stat-lbl">Cores:</span>
      <span class="sm-stat-val">${d.cpu ? d.cpu.cores : '–'}</span>
    </div>
    <div class="sm-stat-item">
      <span class="sm-stat-lbl">Up:</span>
      <span class="sm-stat-val">${d.uptime || '–'}</span>
    </div>
  `;
}

// ═══════════════════════════════════════════
//  PIPELINE JOBS  (Association / Location / Magnitude / Velocity / Relocation)
// ═══════════════════════════════════════════

// channel key = `${step}-${method}`,  e.g. 'assoc-gamma', 'locate-nlloc'
// For compound methods like 'ml-gamma', channel is 'magnitude-ml-gamma'.
let _pipePollTimers = {};
// SW.pipeActiveJob → SeisWorkCore (SW.pipeActiveJob)
let _pipeLogOffset = {};
let _pipeStableCnt = {};   // consecutive polls with no new output.log data

// ── Resolve step + method from a channel key ──────────────────────────────────
function _chInfo(ch) {
  const _OVR = {
    'magnitude-ml-gamma' : { step: 'magnitude', method: 'ml-gamma'  },
    'magnitude-ml-real'  : { step: 'magnitude', method: 'ml-real'   },
    'magnitude-ml-nlloc' : { step: 'magnitude', method: 'ml-nlloc'  },
    'magnitude-ml-locsat': { step: 'magnitude', method: 'ml-locsat' },
    'magnitude-ml-hypodd': { step: 'magnitude', method: 'ml-hypodd' },
  };
  if (_OVR[ch]) return _OVR[ch];
  const idx = ch.indexOf('-');
  return { step: ch.slice(0, idx), method: ch.slice(idx + 1) };
}

// SW.PIPE_TERM/SW.PIPE_JOBS_LIST/SW.PIPE_JOBS_WRAP → SeisWorkCore (SW.PIPE_TERM/SW.PIPE_JOBS_LIST/SW.PIPE_JOBS_WRAP)

// ── Tab switching for methods ──────────────────────────────────────────────────
function toggleGaDbscan(enabled) {
  const panel = document.getElementById('ga-dbscan-params');
  if (panel) panel.style.display = enabled ? '' : 'none';
}

// AL tab + Catalog Filter + Phase modal + Stats modal + external import → modules/association.js (AL)
// (loadPicksFiles/loadCatalogFiles STAY here — also shared by the velocity/relocation/detect steps)

// ── Input file selectors ──────────────────────────────────────────────────────
// importExternalPicks (dead code, target #ext-picks-* never existed in the HTML) → modules/association.js (AL.importPicksLegacy)
// loadPicksFiles/loadCatalogFiles → SeisWorkCore (SW.loadPicksFiles/SW.loadCatalogFiles)

// ── Parameter collectors ──────────────────────────────────────────────────────
function _getPipeParams(step, method) {
  const g = (id, def) => { const el = document.getElementById(id); return el ? el.value : def; };
  const gf = (id, def) => { const v = parseFloat(g(id, '')); return isNaN(v) ? def : v; };
  const gi = (id, def) => { const v = parseInt(g(id, ''), 10); return isNaN(v) ? def : v; };
  const gc = id => document.getElementById(id)?.checked ?? false;

  // Magnitude optional (checkbox on the association page) — included in assoc/locate
  const magP = () => gc('am-mag-enable') ? {
    compute_magnitude: true,
    mag_a: gf('am-a', 1.110), mag_b: gf('am-b', 0.591), mag_c: gf('am-c', 0.00189),
    mag_max_dist: gf('am-maxdist', 300.0), mag_paz: g('am-paz', ''),
  } : { compute_magnitude: false };

  if (step === 'assoc' && method === 'gamma') return {
    max_sigma: gf('ga-maxsig', 2.0),
    min_picks: gi('ga-minpick', 5),
    min_p: gi('ga-minp', 1),
    min_s: gi('ga-mins', 0),
    gmm_method: g('ga-method', 'BGMM'),
    use_amp: gc('ga-useamp'),
    max_time: gf('ga-maxtime', 30.0),
    use_dbscan: gc('ga-usedbscan'),
    dbscan_eps: gf('ga-dbscaneps', 10.0),
    dbscan_min_samples: gi('ga-dbscanmin', 3),
    dbscan_min_cluster: gi('ga-dbscancluster', 50),
    oversample: gi('ga-oversample', 5),
    cov_time: gf('ga-covtime', 5.0),
    cov_amp: gf('ga-covamp', 5.0),
    ...magP(),
  };
  if (step === 'assoc' && method === 'real') return {
    exec: g('rl-exec', 'REAL'),
    tt_db: g('rl-ttdb', 'config/ttdb.txt'),
    lat_center: gf('rl-latref', 0.951),
    // -R search
    rx: gf('rl-rx', 0.1), rh: gf('rl-rh', 20), tdx: gf('rl-tdx', 0.02),
    tdh: gf('rl-tdh', 2), tint: gf('rl-tint', 5),
    // -G tt-grid
    g_trx: gf('rl-gtrx', 1.4), g_trh: gf('rl-gtrh', 20),
    g_tdx: gf('rl-gtdx', 0.01), g_tdh: gf('rl-gtdh', 1),
    // -V velocity
    vp0: gf('rl-vp0', 6.2), vs0: gf('rl-vs0', 3.4),
    // -S threshold
    np0: gi('rl-np0', 3), ns0: gi('rl-ns0', 2), nps0: gi('rl-nps0', 8),
    npsboth0: 2, std0: 0.5, dtps: 0.1,
    nrt: gf('rl-nrt', 1.2), drt: gf('rl-drt', 0.0),
    n_workers: gi('rl-nworkers', 4),
    ...magP(),
  };
  if (step === 'assoc' && method === 'pyocto') return {
    n_picks: gi('po-npicks', 10),
    n_p_picks: gi('po-nppicks', 3),
    n_s_picks: gi('po-nspicks', 0),
    min_node_size: gf('po-nodesize', 10.0),
    pick_match_tolerance: gf('po-tol', 1.5),
    min_interevent_time: gf('po-mininterevent', 3.0),
    vp0: gf('po-vp0', 6.2), vs0: gf('po-vs0', 3.4),
    ...magP(),
  };
  if (step === 'assoc' && method === 'glass3') return {
    exec: g('g3-exec', 'glass-app'),
    n_cut: gi('g3-ncut', 5),
    stack_thresh: gf('g3-stackthresh', 2.5),
    sd_cutoff: gf('g3-sdcutoff', 6.0),
    resolution_km: gf('g3-resolution', 15.0),
    num_stations_per_node: gi('g3-stapernode', 10),
    n_threads: gi('g3-threads', 2),
    shutdown_wait: gi('g3-shutdownwait', 60),
    ...magP(),
  };
  if (step === 'locate' && method === 'nlloc') return {
    exec: g('nl-exec', 'NLLoc'),
    grid_dir: g('nl-griddir', ''),   // empty → backend uses the bundled global grid (config/nlloc_grids/global)
    time_dir: g('nl-timedir', ''),
    profile: g('nl-profile', 'jailolo'),
    save_scatter: gc('nl-scatter'),
    ...magP(),
  };
  if (step === 'locate' && method === 'locsat') return {
    exec: g('ls-exec', 'LocSAT'),
    model: g('ls-model', 'iasp91'),
    min_phases: gi('ls-minph', 4),
    ...magP(),
  };
  if (step === 'magnitude' && method.startsWith('ml')) return {
    // Hutton & Boore 1987: ML = log10(A) + a·log10(R) + c·R + b
    a: gf('ml-a', 1.110),
    b: gf('ml-b', 0.591),
    c: gf('ml-c', 0.00189),
    paz_file: g('ml-paz', ''),
    max_dist: gf('ml-maxdist', 300.0),
  };
  if (step === 'velocity' && method === 'velest') return {
    exec: g('vel-exec', 'velest'),
    mode: gi('vel-mode', 1),
    max_iter: gi('vel-maxiter', 10),
    damp_vel: gf('vel-dampv', 10.0),
    damp_sta: gf('vel-damps', 1.0),
    vpvs: gf('vel-vpvs', 1.730),
    vp_model: g('vel-vp-model-path', ''),
    distmax: gf('vel-distmax', 100.0),
    lat_ref: document.getElementById('vel-latref')?.value || '',
    lon_ref: document.getElementById('vel-lonref')?.value || '',
    // input quality filter (filter_pha)
    max_gap: gf('vel-maxgap', 220.0),
    min_mag: gf('vel-minmag', 0.1),
    min_phase: gi('vel-minphase', 4),
  };
  if (step === 'imaging' && method === 'simul2000') return {
    exec: g('tomo-exec', 'simul2000'),
    eqks_source: g('tomo-eqks', 'velest'),
    ref_lat: document.getElementById('tomo-latref')?.value || '',
    ref_lon: document.getElementById('tomo-lonref')?.value || '',
    vpvs: gf('tomo-vpvs', 1.716),
    bld: gf('tomo-bld', 0.1),
    damp_p: gf('tomo-dampp', 15.0),
    damp_s: gf('tomo-damps', 10.0),
    hitct: gf('tomo-hitct', 5.0),
    target_layer: gi('tomo-layer', 6),
    x_nodes: g('tomo-xnodes', ''),
    y_nodes: g('tomo-ynodes', ''),
    z_nodes: g('tomo-znodes', ''),
    vp_vals: g('tomo-vp', ''),
    z_from_velest: document.getElementById('tomo-zfromvelest')?.checked !== false,
    checkerboard: document.getElementById('tomo-checker')?.checked || false,
    checker_percent: gf('tomo-checkerpct', 5.0),
  };
  if (step === 'mechanism' && method === 'skhash') return {
    network: g('mech-network', '7G'),
    channel: g('mech-channel', ''),  // empty = auto HH>BH>EH>SH per station
    z_nodes: g('mech-znodes', ''),
    vp_vals: g('mech-vp', ''),
    npolmin: gi('mech-npolmin', 5),
    max_agap: gf('mech-maxagap', 300),
    max_pgap: gf('mech-maxpgap', 90),
    nmc: gi('mech-nmc', 30),
  };
  if (step === 'relocation' && method === 'hypodd') return {
    // defaults = residual-diagnosis recommendation (run_hypodd_rekomendasi.ipynb)
    exec: g('hd-exec', 'hypoDD'),
    mode: g('hd-mode', 'catalog'),
    max_dist: gf('hd-maxdist', 60.0),
    max_sep: gf('hd-maxsep', 40.0),
    max_ngh: gi('hd-maxngh', 10),
    min_links: gi('hd-minlinks', 8),
    min_obs: gi('hd-minobs', 8),
    max_obs: gi('hd-maxobs', 32),
    obsct: gi('hd-obsct', 8),
    wdct: gf('hd-wdct', 40.0),
    damping: gf('hd-damp', 70.0),
    vpvs: gf('hd-vpvs', 1.730),
    velocity_model: g('hd-vmodel', 'auto'),
    // blok iterasi: NSET set bobot, WRCT diinterpolasi start→end
    nset: gi('hd-nset', 4),
    niter_per_set: gi('hd-niterset', 4),
    wrct_start: gf('hd-wrct0', 8.0),
    wrct_end: gf('hd-wrct1', 3.0),
    // GrowClust (mode=growclust)
    gc_rmin: gf('gc-rmin', 0.6),
    gc_delmax: gf('gc-delmax', 120.0),
    gc_rmsmax: gf('gc-rmsmax', 0.2),
    gc_ngoodmin: gi('gc-ngoodmin', 8),
    gc_iponly: gi('gc-iponly', 0),
    gc_maxdep: gf('gc-maxdep', 40.0),
    // .inp text from user edits (editor preview). Empty = auto-generate.
    ph2dt_inp_text:  (_hdInpOverride && _hdInpOverride.ph2dt)  || '',
    hypodd_inp_text: (_hdInpOverride && _hdInpOverride.hypodd) || '',
  };
  if (step === 'detect' && method === 'matchlocate') return {
    // default = LOC-FLOW PROC_MatchLocate / marktaup
    ml_min_mag: gf('ml-minmag', 0.5),
    ml_max_tmpl: gi('ml-maxtmpl', 10),
    ml_distmax: gf('ml-distmax', 0.3),
    ml_comp: g('ml-comp', 'HHZ'),
    ml_tleng: gf('ml-tleng', 40.0),
    ml_targetsr: gf('ml-targetsr', 100.0),
    ml_bplow: gf('ml-bplow', 2.0),
    ml_bphigh: gf('ml-bphigh', 8.0),
    ml_bothps: gi('ml-bothps', 0),
    ml_searchR: g('ml-searchR', '0.0/0.0/0'),
    ml_searchI: g('ml-searchI', '0.01/0.01/0.5'),
    ml_twindow: g('ml-twindow', '2.0/0.5/1.5'),
    ml_hthresh: g('ml-hthresh', '0.0/7.0'),
    ml_dintd: gf('ml-dintd', 3.0),
    ml_gc_refine: gi('ml-gcrefine', 0) === 1,
    ml_scan_start: g('ml-scanstart', ''),
    ml_scan_end: g('ml-scanend', ''),
  };
  return {};
}

function toggleRelocMode() {
  // Show GrowClust params only when mode=growclust; show hypoDD-only blocks
  // (ph2dt/iteration) otherwise.
  const mode = (document.getElementById('hd-mode') || {}).value || 'catalog';
  const gc = document.getElementById('gc-params');
  if (gc) gc.style.display = (mode === 'growclust') ? '' : 'none';
}

// HypoDD .inp editor + Relocation session results/RMS stats → modules/hypodd-reloc.js (HD)

// _fmt → SeisWorkCore.fmt (SW.fmt)

// Cross-Correlation Modal (CC Statistik | Waveform CC | RMS Residual) → modules/crosscorr.js (CC)

// _ensureMapOverlays/_eventBounds/_geoAspect3d/_inBounds/_segTouches/_faultTraces/
// _volcanoesInBounds/_coastTrace3D/SW.mapOverlays → SeisWorkCore (map-plotting cluster).

// Kompas peta (camHeadingDeg/compassSVG/mkCompass/addStaticCompass/add3DCompass) → SeisWorkCore.
// _V3 (pure vector helper) STAYS here, used by SW.camHeadingDeg.
const _V3 = {
  cross: (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
  dot: (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2],
  norm: a => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; },
};

function _pipeInputFile(step, method) {
  const ids = {
    'assoc-gamma'        : 'ga-input',
    'assoc-real'         : 'rl-input',
    'assoc-pyocto'       : 'po-input',
    'assoc-glass3'       : 'g3-input',
    'locate-nlloc'       : 'nl-input',
    'locate-locsat'      : 'ls-input',
    'magnitude-ml-gamma' : 'ml-input-gamma',
    'magnitude-ml-real'  : 'ml-input-real',
    'magnitude-ml-nlloc' : 'ml-input-nlloc',
    'magnitude-ml-locsat': 'ml-input-locsat',
    'magnitude-ml-hypodd': 'ml-input-hypodd',
    'velocity-velest'    : 'vel-catalog',
    'relocation-hypodd'  : 'hd-input',
    'detect-matchlocate' : 'mld-input',
    'mechanism-skhash'   : 'mech-input',
  };
  const el = document.getElementById(ids[`${step}-${method}`] || '');
  return el ? el.value.trim() : '';
}

// ── Core runner ───────────────────────────────────────────────────────────────
async function startPipeJob(step, method) {
  if (!SW.wpCfgId) { alert('Open a configuration first'); return; }
  const ch = `${step}-${method}`;
  const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
  const params = _getPipeParams(step, method);
  const input_file = _pipeInputFile(step, method);

  clearPipeTerm(termId);
  appendPipeLine(termId, `[SeisWork] Starting ${step}/${method}…`, 'hdr');

  try {
    const res = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cfg_id: SW.wpCfgId, step, method, params, input_file }),
    });
    const d = await res.json();
    if (!res.ok) { appendPipeLine(termId, `ERROR: ${d.error || 'Unknown'}`, 'err'); return; }
    SW.pipeActiveJob[ch] = d.id;
    _pipeLogOffset[ch] = 0;
    SW.pipeViewedJob[ch] = d.id;
    appendPipeLine(termId, `[SeisWork GUI] Job ${d.id} started`, 'info');
    SW.setProgress(termId, null, `Job ${d.id} running — ${step}/${method}…`);
    await SW.refreshPipeJobsCh(ch, step);
    _startPipePoll(ch, step);
  } catch (e) { appendPipeLine(termId, `ERROR: ${e.message}`, 'err'); }
}

function _startPipePoll(ch, step) {
  if (_pipePollTimers[ch]) return;
  _pipePollTimers[ch] = setInterval(async () => {
    await _pollPipeLogCh(ch, step);
    await SW.refreshPipeJobsCh(ch, step);
  }, 1800);
}

function _stopPipePoll(ch) {
  if (_pipePollTimers[ch]) { clearInterval(_pipePollTimers[ch]); delete _pipePollTimers[ch]; }
  _pipeStableCnt[ch] = 0;
}

async function _pollPipeLogCh(ch, step) {
  const jobId = SW.pipeActiveJob[ch];
  if (!jobId) return;
  const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
  try {
    const off = _pipeLogOffset[ch] || 0;
    const d = await (await fetch(`/api/pipeline/jobs/${jobId}/log?offset=${off}`)).json();
    if (d.lines && d.lines.length) {
      // New data arrived — clear any live-sublog section injected previously
      _pipeStableCnt[ch] = 0;
      const el = document.getElementById(termId);
      if (el) {
        el.querySelectorAll('.term-sublog').forEach(e => e.remove());
        // Batch-append via DocumentFragment to avoid per-line DOM reflow
        const frag = document.createDocumentFragment();
        const ph = el.querySelector('.pick-term-placeholder');
        if (ph) ph.remove();
        d.lines.forEach(l => {
          const div = document.createElement('div');
          div.className = `term-${_lineClass(l)}`;
          div.textContent = l;
          frag.appendChild(div);
        });
        el.appendChild(frag);
        const rows = el.querySelectorAll('div[class^="term-"]');
        if (rows.length > _TERM_MAX_ROWS) {
          for (let i = 0; i < rows.length - _TERM_MAX_ROWS; i++) rows[i].remove();
        }
        el.scrollTop = el.scrollHeight;
      }
      _pipeLogOffset[ch] = d.offset;
    } else {
      _pipeStableCnt[ch] = (_pipeStableCnt[ch] || 0) + 1;
    }
    // After 2+ consecutive polls with no output.log growth, fetch live sublog
    // (per-day REAL log) and display it inline so the user sees REAL progress.
    if (d.state === 'running' && (_pipeStableCnt[ch] || 0) >= 2) {
      try {
        const sub = await (await fetch(`/api/pipeline/jobs/${jobId}/reallog?tail=8`)).json();
        if (sub.lines && sub.lines.length && sub.name) {
          const el = document.getElementById(termId);
          if (el) {
            el.querySelectorAll('.term-sublog').forEach(e => e.remove());
            const hdr = document.createElement('div');
            hdr.className = 'term-sublog term-dim';
            hdr.style.cssText = 'border-top:1px solid #1e3a5f;margin-top:2px;padding-top:2px;opacity:.75';
            hdr.textContent = `── live: ${sub.name} ──`;
            el.appendChild(hdr);
            sub.lines.forEach(l => {
              const div = document.createElement('div');
              div.className = 'term-sublog term-dim';
              div.style.opacity = '.75';
              div.textContent = l;
              el.appendChild(div);
            });
            el.scrollTop = el.scrollHeight;
          }
        }
      } catch (_) {}
    }
    if (d.state && d.state !== 'running' && d.state !== 'pending') {
      _pipeStableCnt[ch] = 0;
      const el = document.getElementById(termId);
      if (el) el.querySelectorAll('.term-sublog').forEach(e => e.remove());
      _stopPipePoll(ch);
      SW.hideProgress(termId);
      appendPipeLine(termId,
        `[SeisWork GUI] Done: ${d.state.toUpperCase()}`,
        d.state === 'done' ? 'info' : 'err'
      );
      await SW.refreshPipeJobsCh(ch, step);
      if (d.state === 'done') await _showPipeResult(ch, step, jobId);
    }
  } catch { }
}

async function _showPipeResult(ch, step, jobId) {
  const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
  try {
    const d = await (await fetch(`/api/pipeline/jobs/${jobId}/preview?rows=5`)).json();
    if (!d.error && d.total != null) {
      appendPipeLine(termId, `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`, 'dim');
      appendPipeLine(termId, `  Output: ${d.total} result rows`, 'info');
      appendPipeLine(termId, `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`, 'dim');
    }
  } catch { }
  // Refresh downstream input selectors — [selId, forStep, forMethod?]
  const DOWNSTREAM = {
    'assoc-gamma'       : [['nl-input','locate'],['ls-input','locate'],['hd-input','relocation'],['vel-catalog','velocity']],
    'assoc-real'        : [['nl-input','locate'],['ls-input','locate'],['hd-input','relocation'],['vel-catalog','velocity']],
    'locate-nlloc'      : [['vel-catalog','velocity'],['hd-input','relocation']],
    'locate-locsat'     : [['vel-catalog','velocity'],['hd-input','relocation']],
  };
  (DOWNSTREAM[ch] || []).forEach(([selId, forStep, forMethod]) => SW.loadCatalogFiles(selId, forStep, forMethod));
  // Refresh session results for assocloc tabs
  const AL_SESSION_MAP = { 'assoc-gamma': 'gamma', 'assoc-real': 'real', 'locate-nlloc': 'nlloc', 'locate-locsat': 'locsat' };
  if (AL_SESSION_MAP[ch]) AL.refreshSessions(AL_SESSION_MAP[ch]);
  // Refresh relocation session list when hypoDD job completes
  if (ch === 'relocation-hypodd') HD.refreshSessions();
  // Auto-show tomography figures when an imaging job completes
  if (ch === 'imaging-simul2000') TOMO.showFigures(jobId);
  // Auto-show focal mechanism results when a mechanism job completes
  if (ch === 'mechanism-skhash') MECH.showResults(jobId);
}

// Imaging figure gallery + 3D viewer → modules/tomography.js (TOMO)
// _refreshPipeJobsCh → SeisWorkCore.refreshPipeJobsCh (SW.refreshPipeJobsCh)

function _renderPipeJobsCh(ch, jobs) {
  const listId = SW.PIPE_JOBS_LIST[ch];
  const wrapId = SW.PIPE_JOBS_WRAP[ch];
  const el = document.getElementById(listId);
  const wrap = document.getElementById(wrapId);
  if (!el) return;
  if (wrap) wrap.style.display = jobs.length ? '' : 'none';
  if (!jobs.length) { el.innerHTML = ''; return; }

  const STATE_CSS = { running: 'js-running', done: 'js-done', error: 'js-error', stopped: 'js-stopped' };
  const STATE_LBL = { running: 'Running', done: 'Done', error: 'Error', stopped: 'Stopped' };
  const { step } = _chInfo(ch);

  el.innerHTML = jobs.slice(0, 6).map(j => {
    const sc = STATE_CSS[j.state] || 'js-stopped';
    const slbl = STATE_LBL[j.state] || j.state;
    const ev = j.events != null ? `<span class="pick-cnt-badge">${j.events} ev</span>` : '';
    const isVelDone = (ch === 'velocity-velest' && j.state === 'done');
    const isTomoDone = (ch === 'imaging-simul2000' && j.state === 'done');
    const isMechDone = (ch === 'mechanism-skhash' && j.state === 'done');
    const modBtn = isVelDone
      ? `<span class="pick-job-view-btn" style="color:#60a5fa"
               onclick="VEL.showModelPlot('${j.id}')"><i class="bi bi-graph-up"></i> model</span>
         <span class="pick-job-view-btn" style="color:#a78bfa"
               onclick="VEL.showRmsPlot('${j.id}')"><i class="bi bi-activity"></i> RMS</span>`
      : isTomoDone
      ? `<span class="pick-job-view-btn" style="color:#60a5fa"
               onclick="TOMO.showFigures('${j.id}')"><i class="bi bi-images"></i> figures</span>
         <span class="pick-job-view-btn" style="color:#a78bfa"
               onclick="TOMO.open3d('${j.id}')"><i class="bi bi-box"></i> 3D velocity</span>`
      : isMechDone
      ? `<span class="pick-job-view-btn" style="color:#60a5fa"
               onclick="MECH.showResults('${j.id}','${SW.esc(j.cfg_id||'')}')"><i class="bi bi-compass"></i> beachballs</span>`
      : '';
    return `<div class="pick-job-card">
      <span class="pick-job-id">${SW.esc(j.id)}</span>
      <span class="pick-job-method">${SW.esc(j.method || '')}</span>
      <span class="job-state ${sc}" style="margin-left:.2rem">${slbl}</span>
      ${ev}
      <span class="pick-job-view-btn" style="margin-left:auto"
            onclick="viewPipeJobLog('${ch}','${step}','${j.id}')">log</span>
      ${modBtn}
      ${j.state === 'running'
        ? `<button class="btn-sw btn-danger-sw btn-sm-sw"
                   onclick="stopPipeJob('${ch}','${step}','${j.id}')"><i class="bi bi-stop-fill"></i></button>`
        : `<button class="btn-sw btn-ghost-sw btn-sm-sw"
                   onclick="removePipeJob('${step}','${j.id}')" title="Remove"><i class="bi bi-x-lg"></i></button>`
      }
    </div>`;
  }).join('');
}

async function viewPipeJobLog(ch, step, jobId, fullLoad = false) {
  SW.pipeActiveJob[ch] = jobId;
  _pipeLogOffset[ch] = 0;
  _pipeStableCnt[ch] = 0;
  SW.pipeViewedJob[ch] = jobId;
  const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
  clearPipeTerm(termId);
  appendPipeLine(termId, `[SeisWork GUI] Loading log for job ${jobId}…`, 'dim');
  try {
    const url = fullLoad
      ? `/api/pipeline/jobs/${jobId}/log?offset=0`
      : `/api/pipeline/jobs/${jobId}/log?tail=60`;
    const d = await (await fetch(url)).json();
    clearPipeTerm(termId);
    if (d.tail && d.total_size > 0) {
      // Show banner with "load full log" button
      const el = document.getElementById(termId);
      if (el) {
        const banner = document.createElement('div');
        banner.className = 'term-dim';
        banner.style.cssText = 'border-bottom:1px solid #334155;padding-bottom:3px;margin-bottom:3px;cursor:pointer';
        const kb = Math.round(d.total_size / 1024);
        banner.innerHTML = `▲ ${kb} KB log sebelumnya — <span style="color:#60a5fa;text-decoration:underline" onclick="viewPipeJobLog('${ch}','${step}','${jobId}',true)">Load full log</span>`;
        el.appendChild(banner);
      }
    }
    if (d.lines) {
      const el = document.getElementById(termId);
      if (el) {
        const frag = document.createDocumentFragment();
        d.lines.forEach(l => {
          const div = document.createElement('div');
          div.className = `term-${_lineClass(l)}`;
          div.textContent = l;
          frag.appendChild(div);
        });
        el.appendChild(frag);
        el.scrollTop = el.scrollHeight;
      }
    }
    _pipeLogOffset[ch] = d.offset || 0;
    if (d.state === 'running') {
      SW.setProgress(termId, null, `Job ${jobId} running…`);
      _startPipePoll(ch, step);
    } else {
      SW.hideProgress(termId);
      appendPipeLine(termId, `State: ${d.state || 'unknown'}`, 'info');
    }
  } catch (e) { appendPipeLine(termId, `ERROR: ${e.message}`, 'err'); }
}

async function stopPipeJob(ch, step, jobId) {
  await fetch(`/api/pipeline/jobs/${jobId}`, { method: 'DELETE' });
  _stopPipePoll(ch);
  const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
  SW.hideProgress(termId);
  appendPipeLine(termId, `[SeisWork GUI] Job ${jobId} stopped`, 'warn');
  await SW.refreshPipeJobsCh(ch, step);
}

async function removePipeJob(step, jobId) {
  await fetch(`/api/pipeline/jobs/${jobId}`, { method: 'DELETE' });
  // Refresh all channels for this step
  Object.keys(SW.PIPE_TERM).filter(ch => ch.startsWith(step + '-'))
    .forEach(ch => SW.refreshPipeJobsCh(ch, step));
}

// ── Terminal helpers ──────────────────────────────────────────────────────────
function clearPipeTerm(termId) {
  const el = document.getElementById(termId);
  if (el) el.innerHTML = '<div class="pick-term-placeholder">Output will appear here…</div>';
}

const _TERM_MAX_ROWS = 500;
function appendPipeLine(termId, text, cls = 'ok') {
  const el = document.getElementById(termId);
  if (!el) return;
  const ph = el.querySelector('.pick-term-placeholder');
  if (ph) ph.remove();
  const div = document.createElement('div');
  div.className = `term-${cls}`;
  div.textContent = text;
  el.appendChild(div);
  // Trim oldest rows to keep DOM small and browser responsive
  const rows = el.querySelectorAll('div[class^="term-"]');
  if (rows.length > _TERM_MAX_ROWS) {
    for (let i = 0; i < rows.length - _TERM_MAX_ROWS; i++) rows[i].remove();
  }
  el.scrollTop = el.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════════════════════
//  SESSION RESTORE PER STEP — by HakimBMKG
//  Every time a step page is opened, the log + last result position is restored
//  from the server (status.json per job), so the user always knows the last data
//  position even after a browser reload or page navigation.
// ═══════════════════════════════════════════════════════════════════════════════

// SW.STEP_CHANNELS/SW.STEP_INPUT_SELECTS/SW.pipeViewedJob → SeisWorkCore
// (SW.STEP_CHANNELS/SW.STEP_INPUT_SELECTS/SW.pipeViewedJob)

async function _restorePipeSession(page) {
  // 1. Populate dropdown inputs ([selId, forStep, forMethod?])
  (SW.STEP_INPUT_SELECTS[page] || []).forEach(([selId, forStep, forMethod]) => {
    const sel = document.getElementById(selId);
    if (sel && (!sel.options.length || !sel.value)) {
      if (forStep === null) SW.loadPicksFiles(selId);
      else SW.loadCatalogFiles(selId, forStep, forMethod);
    }
  });

  // 2. Restore each channel on this page
  const jobsByStep = {};
  for (const ch of (SW.STEP_CHANNELS[page] || [])) {
    const { step, method } = _chInfo(ch);
    if (!(step in jobsByStep)) {
      try {
        jobsByStep[step] = SW.activeConfigId
          ? await (await fetch(`/api/pipeline/jobs?step=${step}&cfg_id=${SW.activeConfigId}`)).json()
          : [];
      } catch { jobsByStep[step] = []; }
    }
    const filtered = jobsByStep[step].filter(j => j.method === method);
    _renderPipeJobsCh(ch, filtered);
    if (!filtered.length) continue;
    if (_pipePollTimers[ch]) continue;

    const running = filtered.find(j => j.state === 'running');
    const last = running || filtered[0];
    if (!running && SW.pipeViewedJob[ch] === last.id) continue;

    SW.pipeViewedJob[ch] = last.id;
    await viewPipeJobLog(ch, step, last.id);
    const termId = SW.PIPE_TERM[ch] || `pipe-term-${step}`;
    if (running) {
      SW.setProgress(termId, null, `Job ${last.id} still running — log live…`);
    } else if (last.state === 'done') {
      const ev = last.events != null ? ` — ${last.events} events` : '';
      appendPipeLine(termId, `[SeisWork GUI] Last session restored: job ${last.id} DONE${ev} (${last.finished || ''})`, 'info');
      if (last.result_file) appendPipeLine(termId, `[SeisWork GUI] Result: ${last.result_file}`, 'dim');
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
//  PROGRESS BAR — by HakimBMKG
//  Progress bar per terminal (determinate for picker `read=i/N`, indeterminate
//  for processes without total info) so the page doesn't look blank/frozen.
// ═══════════════════════════════════════════════════════════════════════════════

// _ensureProg/setProgress/hideProgress → SeisWorkCore (SW.ensureProg/SW.setProgress/SW.hideProgress)

// Parse picker progress from log line: "read=37/94 ... picks=13513  710s"
function _parsePickProgress(line) {
  const m = line.match(/read=(\d+)\/(\d+)/);
  if (m) {
    const done = parseInt(m[1], 10), total = parseInt(m[2], 10);
    const pk = line.match(/picks=(\d+)/);
    return { frac: total ? done / total : null, label: `Station-day ${done}/${total}${pk ? ` — ${pk[1]} picks` : ''}` };
  }
  const s = line.match(/^\s*(\d+) station-days processed\s+picks so far: (\d+)/);
  if (s) return { frac: null, label: `${s[1]} station-days — ${s[2]} picks` };
  return null;
}

// ═══════════════════════════════════════════════════════════════════════════════
//  GLOBAL NETWORK LOADER — by HakimBMKG
//  Thin bar at the top of the screen when a fetch is running >250 ms, so the
//  user knows the app is loading (not frozen / blank white).
// ═══════════════════════════════════════════════════════════════════════════════

(function () {
  let inflight = 0, showTimer = null;
  const bar = document.createElement('div');
  bar.id = 'sw-netbar';
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(bar));
  if (document.body) document.body.appendChild(bar);

  const _origFetch = window.fetch;
  window.fetch = function (...args) {
    inflight++;
    if (!showTimer) showTimer = setTimeout(() => { if (inflight > 0) bar.classList.add('on'); }, 250);
    const done = () => {
      inflight = Math.max(0, inflight - 1);
      if (inflight === 0) {
        if (showTimer) { clearTimeout(showTimer); showTimer = null; }
        bar.classList.remove('on');
      }
    };
    return _origFetch.apply(this, args).then(r => { done(); return r; }, e => { done(); throw e; });
  };
})();

// ═══════════════════════════════════════════════════════════════════════════════
//  RESULT VIEWER MODAL — by HakimBMKG
//  Leaflet map + Plotly cross-sections + waveform drawer
// ═══════════════════════════════════════════════════════════════════════════════

const _RM = {
  cfgId: null,
  data: null,       // { jobs, stations, region }
  activeJob: null,       // selected job object
  map: null,       // Leaflet instance
  lgEvs: null,       // event layer group
  lgSta: null,       // station layer group
  lgFault: null,       // fault lines (indogigis)
  lgFaultSym: null,      // fault symbols (gigis)
  lgVol: null,       // volcano markers
  ewBand: null,
  nsBand: null,
  rxBand: null,       // rotatable XS band polygon
  rxLine: null,       // rotatable XS center line
  xhair: null,
  cla: 1.0,        // cross-section center lat
  clo: 127.5,      // cross-section center lon
  xsHalf: 15,         // km half-width
  az: 0,          // rotatable XS azimuth (0–175°)
  mgMin: -9, mgMax: 9, depMax: 200,
  ewInit: false, nsInit: false, magInit: false,
  rxInit: false, init3d: false, statInit: false,
  // layer visibility
  showFault: true, showFaultSym: true, showVolcano: true,
  showStation: true, showXS: true, showSlab: true,
  showTopo: true, showSlab3d: true, showVol3d: true, showCoast3d: true,
  showSlab2d: true, showTopo3d: true, showFault3d: true,
  showSlabMap: true,   // Slab2 depth contours drawn on the 2-D Leaflet map
  lgSlabMap: null,     // Leaflet layerGroup for the map slab contours
  // 2D map time animation
  playing: false, playTimer: null, playFrame: 0, playSubset: null,
  // 3D independent time animation
  playing3d: false, playTimer3d: null, playFrame3d: 0, _3dEvIdx: null, _3dCamera: null,
  // cached data
  slabData: null,   // Contour_slabs GeoJSON (cached once)
  slab2Data: null,   // Slab2 reference EQ points [{lat,lon,dep,src}]
  volcanoes: [],     // [{lon,lat,name}] filtered to region
  topoData: {},     // elevation profile cache keyed by XS params
  coast3dData: null,     // Natural Earth coastlines for 3D (cached once)
  topo3dData: null,      // {lons, lats, z[][]} elevation grid for 3D (cached once)
};

// Depth colorscale (same as generate_seismap.py)
const _RM_CS = [
  [0, [255, 50, 50]],
  [10, [255, 140, 0]],
  [20, [255, 220, 0]],
  [50, [34, 197, 94]],
  [100, [59, 130, 246]],
  [200, [124, 58, 237]],
  [400, [159, 18, 57]],
];
function _rmD2c(d) {
  for (let i = 0; i < _RM_CS.length - 1; i++) {
    const [d0, c0] = _RM_CS[i], [d1, c1] = _RM_CS[i + 1];
    if (d >= d0 && d <= d1) {
      const t = (d - d0) / (d1 - d0);
      return [~~(c0[0] + t * (c1[0] - c0[0])), ~~(c0[1] + t * (c1[1] - c0[1])), ~~(c0[2] + t * (c1[2] - c0[2]))];
    }
  }
  return [159, 18, 57];
}
function _rmD2hex(d) { const c = _rmD2c(d); return '#' + c.map(v => v.toString(16).padStart(2, '0')).join(''); }
function _rmD2css(d) { const c = _rmD2c(d); return `rgb(${c[0]},${c[1]},${c[2]})`; }
const _RM_PLTCS = _RM_CS.map(([d, c]) => [d / 400, `rgb(${c[0]},${c[1]},${c[2]})`]);
function _rmM2r(m) { return Math.max(3, Math.min(13, 3 + (m ?? 1.5) * 1.9)); }

// Vertical exaggeration for topography/relief on the cross-sections. Real relief
// (<1.5 km) is invisible against a 0–200 km depth axis → looks flat. Exaggerating
// the elevation makes the surface visible while hover still reports true metres.
const _RM_TOPO_EXAG = 8;
// 3D topo exaggeration is computed adaptively at draw time (see _rm3dExag): the
// summit renders at ~5% of the depth axis so it's a subtle raise, not a towering
// peak. A finer 16×16 grid (_rmLoad3dTopo) recovers Mt Jailolo's narrow summit.
// Empty gap (km) between sea level (depth 0) and the topography band, so there is
// a clear blank boundary separating earthquake points (below 0) from the relief.
const _RM_TOPO_GAP = 4;
// Linear interpolation of a value sampled at monotonically increasing xs.
function _rmInterp(x, xs, vals) {
  if (!xs || !xs.length) return 0;
  if (x <= xs[0]) return vals[0];
  if (x >= xs[xs.length - 1]) return vals[vals.length - 1];
  for (let i = 1; i < xs.length; i++) {
    if (x <= xs[i]) {
      const t = (x - xs[i - 1]) / (xs[i] - xs[i - 1] || 1);
      return vals[i - 1] + t * (vals[i] - vals[i - 1]);
    }
  }
  return vals[vals.length - 1];
}
// Display depth (km, negative = above sea) for an elevation (m): exaggerated and
// lifted above 0 by _RM_TOPO_GAP so an empty band separates it from the events.
function _rmTopoY(elevM) { return -_RM_TOPO_GAP - Math.max(0, elevM) / 1000 * _RM_TOPO_EXAG; }

// ── Open / close ─────────────────────────────────────────────────────────────
async function openResultModal() {
  if (!SW.wpCfgId) return;
  try { await SW.ensurePlotly(); } catch (e) { alert('Failed to load Plotly: ' + e.message); return; }
  _RM.cfgId = SW.wpCfgId;
  document.getElementById('rm-bd').classList.remove('hidden');
  document.getElementById('rm-hdr-cfg').textContent = document.getElementById('wp-title').textContent;
  document.getElementById('rm-mg-min-v').textContent = 'All';
  document.getElementById('rm-mg-max-v').textContent = '7.0';
  document.getElementById('rm-dep-max-v').textContent = '200 km';
  document.getElementById('rm-xsw-v').textContent = '15 km';
  document.getElementById('rm-az-v').textContent = '0°';
  document.getElementById('rm-az').value = '0';
  document.getElementById('rm-rx-title').textContent = 'XS N–S';
  _RM.mgMin = -9; _RM.mgMax = 9; _RM.depMax = 200; _RM.xsHalf = 15; _RM.az = 0;
  _RM.showFault = true; _RM.showFaultSym = true; _RM.showVolcano = true; _RM.showStation = true;
  _RM.showXS = true; _RM.showSlab = true; _RM.showTopo = true; _RM.showSlab3d = true; _RM.showVol3d = true;
  _RM.showCoast3d = true; _RM.showSlab2d = true; _RM.showTopo3d = true; _RM.showFault3d = true;
  _RM.showSlabMap = true;
  _RM.topoData = {}; _RM.coast3dData = null; _RM.slab2Data = null; _RM.topo3dData = null;

  _rmSetLoading(true, 'Loading catalog… 0%', 0);
  _rmSetError(null);
  try {
    _RM.data = await SW.fetchJSON(`/api/result/${SW.wpCfgId}/catalog`,
      pct => _rmSetLoading(true, `Loading catalog… ${pct}%`, pct));
  } catch (e) {
    _rmSetLoading(false);
    _rmSetError('Error loading catalog: ' + e.message);
    return;
  }

  // Auto-generate Slab2.0 contours for this AOI (cached across modal opens)
  if (!_RM.slabData) {
    _rmSetLoading(true, 'Generating Slab2 model for AOI…');
    _RM.slabData = await _rmFetchSlab(_RM.data?.region);
  }

  _rmSetLoading(false);
  _rmPopulateJobSel();
  _rmInitMap();
  _rmUpdate();
  // Prefetch Slab2 reference after UI is drawn (non-blocking)
  _rmLoadSlab2();
}

function _rmSetLoading(on, msg, pct) {
  const el = document.getElementById('rm-loading-ovl');
  if (!el) return;
  el.classList.toggle('hidden', !on);
  const lbl = el.querySelector('.rm-loading-lbl');
  const bar = document.getElementById('rm-loading-bar');
  const fill = document.getElementById('rm-loading-fill');
  if (lbl && msg != null) lbl.textContent = msg;
  if (pct == null) {
    if (bar) bar.style.display = 'none';
  } else {
    if (bar) bar.style.display = '';
    if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }
}

function _rmSetError(msg) {
  const el = document.getElementById('rm-error-banner');
  if (!el) return;
  el.classList.toggle('hidden', !msg);
  const sp = el.querySelector('.rm-err-msg');
  if (sp) sp.textContent = msg || '';
}

function closeResultModal() {
  _rmPlayStop();
  _rmPlay3dStop();
  document.getElementById('rm-bd').classList.add('hidden');
  RMWV.closeModal();
  rmClose3d();
  if (_RM.map) { _RM.map.remove(); _RM.map = null; }
  ['rm-ns-plt', 'rm-ew-plt', 'rm-mag-plt', 'rm-rx-plt', 'rm-3d-plt', 'rm-wv-plot', 'rm-rms-plt', 'rm-fmd-plt'].forEach(id => {
    try { Plotly.purge(id); } catch (_) { }
  });
  _RM.ewInit = _RM.nsInit = _RM.magInit = _RM.rxInit = _RM.init3d = _RM.statInit = false;
  _RM.lgFault = _RM.lgFaultSym = _RM.lgVol = null;  // cleared by map.remove()
  // Reset tab to spatial
  document.querySelectorAll('.rm-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === 'spatial'));
  document.querySelectorAll('.rm-tab-pane').forEach(p => p.classList.toggle('active', p.id === 'rm-tab-spatial'));
}

// ── Benchmark modal (RMS antar metode vs referensi) ──────────────────────────
async function openBenchmarkModal() {
  document.getElementById('bm-bd').classList.remove('hidden');
  try { await SW.ensurePlotly(); } catch (e) { }
  await refreshBenchmark();
}
function closeBenchmarkModal() {
  document.getElementById('bm-bd').classList.add('hidden');
  try { Plotly.purge('bm-rms-plt'); } catch (_) { }
}
async function refreshBenchmark() {
  const load = document.getElementById('bm-loading');
  const cont = document.getElementById('bm-content');
  load.style.display = ''; cont.style.display = 'none';
  let data;
  try {
    data = await (await fetch('/api/benchmark')).json();
  } catch (e) {
    load.innerHTML = '<span style="color:#f87171">Error: ' + e.message + '</span>';
    return;
  }
  const items = data.items || [];
  const fmt = v => (v === null || v === undefined) ? '—' : v;
  const tb = document.getElementById('bm-tbody');
  tb.innerHTML = items.map(it => {
    const grpColor = it.group === 'reference' ? '#a78bfa' : '#34d399';
    const status = it.exists ? (it.n > 0 ? '<span style="color:#34d399">OK</span>'
                                         : '<span style="color:#fbbf24">empty</span>')
                             : '<span style="color:#6b7280">not yet</span>';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:.3rem .5rem;font-weight:600">${it.label}</td>
      <td><span style="color:${grpColor}">${it.group}</span></td>
      <td style="text-align:right">${fmt(it.n)}</td>
      <td style="text-align:right">${fmt(it.rms_median)}</td>
      <td style="text-align:right">${fmt(it.rms_mean)}</td>
      <td style="text-align:right">${fmt(it.depth_median)}</td>
      <td style="text-align:right">${it.mag_median != null ? (+it.mag_median).toFixed(2) : '—'}</td>
      <td style="text-align:right">${fmt(it.gap_median)}</td>
      <td>${status}</td></tr>`;
  }).join('');

  // RMS overlay: one histogram line per method that has an rms_hist
  const palette = ['#a78bfa', '#f472b6', '#60a5fa', '#34d399', '#fbbf24', '#fb923c', '#22d3ee', '#f87171'];
  const traces = [];
  let ci = 0;
  items.forEach(it => {
    const h = it.rms_hist;
    if (!h || !h.counts || !h.counts.length || !it.rms_n) return;
    const x = h.edges.slice(0, -1).map((e, i) => (e + h.edges[i + 1]) / 2);
    traces.push({
      x, y: h.counts, type: 'scatter', mode: 'lines',
      line: { color: palette[ci % palette.length], width: 2,
              dash: it.group === 'reference' ? 'dot' : 'solid' },
      name: `${it.label} (med ${it.rms_median ?? '—'})`,
    });
    ci++;
  });
  const layout = {
    margin: { l: 44, r: 10, t: 8, b: 36 },
    xaxis: { title: 'RMS residual (s)', gridcolor: '#2a2f3a' },
    yaxis: { title: '# event', gridcolor: '#2a2f3a' },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#cbd5e1', size: 10 },
    legend: { orientation: 'h', y: -0.22, font: { size: 9 } },
    showlegend: true,
  };
  try { Plotly.newPlot('bm-rms-plt', traces, layout, { displayModeBar: false, responsive: true }); } catch (_) { }

  load.style.display = 'none'; cont.style.display = '';
}

// ── Job selector ─────────────────────────────────────────────────────────────
function _rmPopulateJobSel() {
  const sel = document.getElementById('rm-job-sel');
  sel.innerHTML = '';

  const pipeJobs = _RM.data?.jobs || [];
  const sorted = [...pipeJobs].sort((a, b) => (b.finished || '').localeCompare(a.finished || ''));

  if (sorted.length > 0) {
    sorted.forEach(j => {
      const opt = document.createElement('option');
      opt.value = j.job_id;
      opt.textContent = `[${j.step}/${j.mode || j.method}${j.filtered ? '_filter' : ''}] ${j.n_events} events – ${(j.finished || '').slice(0, 16).replace('T', ' ')}`;
      sel.appendChild(opt);
    });
    _RM.activeJob = sorted[0];
    sel.value = _RM.activeJob.job_id;
    _rmUpdateHypoDDBtn(_RM.activeJob);

    // Also add QL downloads as an extra optgroup — only those owned by this
    // config's session (cfg_id), so catalogs from other configs do not appear.
    const qlDone = _qlJobsCache.filter(j => j.state === 'done' && j.cfg_id === SW.wpCfgId
                                            && (!j.params?.format || j.params.format === 'xml'));
    if (qlDone.length > 0) {
      const grp = document.createElement('optgroup');
      grp.label = '── QuakeLink Downloads ──';
      qlDone.forEach(j => {
        const opt = document.createElement('option');
        opt.value = `__ql__:${j.id}`;
        const t0 = (j.params?.starttime || '').slice(0, 10);
        const t1 = (j.params?.endtime   || '').slice(0, 10);
        opt.textContent = `[QL] ${t0} – ${t1}`;
        grp.appendChild(opt);
      });
      sel.appendChild(grp);
    }
    return;
  }

  // No pipeline jobs — try showing QL downloads only (this config session)
  const qlDone = _qlJobsCache.filter(j => j.state === 'done' && j.cfg_id === SW.wpCfgId
                                          && (!j.params?.format || j.params.format === 'xml'));
  if (qlDone.length > 0) {
    const grp = document.createElement('optgroup');
    grp.label = 'QuakeLink Downloads';
    qlDone.forEach(j => {
      const opt = document.createElement('option');
      opt.value = `__ql__:${j.id}`;
      const t0 = (j.params?.starttime || '').slice(0, 10);
      const t1 = (j.params?.endtime   || '').slice(0, 10);
      opt.textContent = `[QL] ${t0} – ${t1}`;
      grp.appendChild(opt);
    });
    sel.appendChild(grp);
    // Auto-select the first QL (but do not re-trigger when already in __ql__ mode)
    const firstQlId = qlDone[0].id;
    const firstQlVal = `__ql__:${firstQlId}`;
    sel.value = firstQlVal;
    _RM.activeJob = null;
    // Only auto-load when NOT already loading a QL (prevents a loop)
    if (_RM.cfgId !== '__ql__') {
      setTimeout(() => openQlCatalogInResultModal(firstQlId), 0);
    }
    return;
  }

  sel.innerHTML = '<option value="">No results</option>';
  _RM.activeJob = null;
}

function rmOnJobChange() {
  const val = document.getElementById('rm-job-sel').value;
  if (!val) return;

  // Selection from QuakeLink downloads
  if (val.startsWith('__ql__:')) {
    const qlId = val.slice(7);
    openQlCatalogInResultModal(qlId);
    _rmUpdateHypoDDBtn(null);
    return;
  }

  _RM.activeJob = _RM.data?.jobs?.find(j => j.job_id === val) || null;
  _rmUpdateHypoDDBtn(_RM.activeJob);
  _rmRes.loadedJob = null;
  RMWV.closeModal();
  _rmPlayStop();
  _rmPlay3dStop();
  _rmUpdate();
  if (document.getElementById('rm-tab-residual')?.classList.contains('active')) {
    rmResLoad();
  }
}

function _rmUpdateHypoDDBtn(job) {
  // HypoDD relocation is an Online Monitor feature (see om-btn-hypodd) — it does
  // not belong in the offline Result Viewer, so this button always stays hidden
  // here regardless of the selected job.
  const btn = document.getElementById('rm-btn-hypodd');
  if (!btn) return;
  btn.style.display = 'none';
}

async function rmRunHypoDD() {
  const job = _RM.activeJob;
  if (!job || !job.result_file) return;
  const cfgId = _RM.cfgId || SW.wpCfgId;
  if (!cfgId) return;

  const btn = document.getElementById('rm-btn-hypodd');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="sw-spin" style="display:inline-block;width:.8em;height:.8em;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite"></span>&thinsp;Running…'; }

  try {
    const r = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cfg_id: cfgId,
        step: 'relocation',
        method: 'hypodd',
        params: { mode: 'catalog', velocity_model: 'iasp91' },
        input_file: job.result_file,
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);

    const jobId = d.job_id;

    const poll = async () => {
      try {
        const jobs = await (await fetch(`/api/pipeline/jobs?step=relocation&cfg_id=${encodeURIComponent(cfgId)}`)).json();
        const found = (jobs || []).find(j => j.id === jobId);
        const state = found?.state || 'running';
        if (state === 'done') {
          // Reload the catalog and auto-select the new relocation job
          _RM.data = await SW.fetchJSON(`/api/result/${cfgId}/catalog`);
          _rmPopulateJobSel();
          const sel = document.getElementById('rm-job-sel');
          if (sel) {
            sel.value = jobId;
            rmOnJobChange();
          }
        } else if (state === 'error' || state === 'stopped') {
          alert(`HypoDD relocation finished with state: ${state}. Check the log in the Relocation panel.`);
          if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-geo-alt-fill"></i>&thinsp;HypoDD'; }
        } else {
          setTimeout(poll, 4000);
        }
      } catch (_) {
        setTimeout(poll, 4000);
      }
    };
    setTimeout(poll, 4000);

  } catch (e) {
    alert(`Failed to start HypoDD relocation: ${e.message}`);
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-geo-alt-fill"></i>&thinsp;HypoDD'; }
  }
}

// ── Active events (filtered) ─────────────────────────────────────────────────
function _rmActiveEvents() {
  if (_RM.playSubset !== null) return _RM.playSubset;
  if (!_RM.activeJob) return [];
  return _RM.activeJob.events
    .map(ev => ({
      ...ev,
      lat: parseFloat(ev.lat),
      lon: parseFloat(ev.lon),
      dep: parseFloat(ev.depth_km ?? 0),
      mag: ev.mag != null ? parseFloat(ev.mag) : null,
    }))
    .filter(ev => {
      if (isNaN(ev.lat) || isNaN(ev.lon)) return false;
      if (ev.mag != null && (ev.mag < _RM.mgMin || ev.mag > _RM.mgMax)) return false;
      if (ev.dep > _RM.depMax) return false;
      return true;
    });
}

// ── Time animation (Play) ─────────────────────────────────────────────────────
function _rmPlaySorted() {
  if (!_RM.activeJob) return [];
  return _RM.activeJob.events
    .map(ev => ({
      ...ev,
      lat: parseFloat(ev.lat), lon: parseFloat(ev.lon),
      dep: parseFloat(ev.depth_km ?? 0),
      mag: ev.mag != null ? parseFloat(ev.mag) : null,
    }))
    .filter(ev => !isNaN(ev.lat) && !isNaN(ev.lon) && ev.datetime && ev.dep <= _RM.depMax)
    .sort((a, b) => (a.datetime < b.datetime ? -1 : 1));
}

function rmPlayToggle() { _RM.playing ? _rmPlayPause() : _rmPlayStart(); }

function _rmPlayStart() {
  const all = _rmPlaySorted();
  if (!all.length) return;
  if (_RM.playFrame <= 0 || _RM.playFrame >= all.length) _RM.playFrame = 0;
  _RM.playing = true;
  const btn = document.getElementById('rm-play-btn');
  if (btn) btn.innerHTML = '<i class="bi bi-pause-fill"></i>';
  const spd = +(document.getElementById('rm-play-speed')?.value) || 300;
  _rmPlayAdvance(all);
  _RM.playTimer = setInterval(() => {
    const cur = _rmPlaySorted();
    if (_RM.playFrame >= cur.length) { _rmPlayStop(); return; }
    _rmPlayAdvance(cur);
  }, spd);
}

function _rmPlayPause() {
  if (_RM.playTimer) { clearInterval(_RM.playTimer); _RM.playTimer = null; }
  _RM.playing = false;
  const btn = document.getElementById('rm-play-btn');
  if (btn) btn.innerHTML = '<i class="bi bi-play-fill"></i>';
}

function _rmPlayStop() {
  _rmPlayPause();
  _RM.playFrame = 0; _RM.playSubset = null;
  if (_RM.lgEvs) _rmBuildMarkers();
  const info = document.getElementById('rm-play-info');
  if (info) info.textContent = '';
  const bar = document.getElementById('rm-play-bar-fill');
  if (bar) bar.style.width = '0%';
}

// ── 3D independent play (separate from 2D map play) ───────────────────────────
function rmPlay3dToggle() { _RM.playing3d ? _rmPlay3dPause() : _rmPlay3dStart(); }

// Named drag handlers so removeEventListener works correctly
function _rmPlay3dDragOn() {
  // User started orbiting/zooming — suspend restyle ticks so WebGL drag isn't interrupted
  if (_RM.playTimer3d) { clearInterval(_RM.playTimer3d); _RM.playTimer3d = null; }
}
function _rmPlay3dDragOff() {
  // User released — wait briefly so plotly_relayout fires and saves camera, then restart
  if (!_RM.playing3d || _RM.playTimer3d) return;
  setTimeout(() => {
    if (!_RM.playing3d || _RM.playTimer3d) return;
    const spd = +(document.getElementById('rm-3d-play-speed')?.value) || 300;
    _RM.playTimer3d = setInterval(() => {
      const cur = _rmPlaySorted();
      if (_RM.playFrame3d >= cur.length) { _rmPlay3dStop(); return; }
      _rmPlay3dAdvance(cur);
    }, spd);
  }, 150);
}

function _rmPlay3dStart() {
  const all = _rmPlaySorted();
  if (!all.length) return;
  if (_RM.playFrame3d <= 0 || _RM.playFrame3d >= all.length) _RM.playFrame3d = 0;
  _RM.playing3d = true;
  const btn = document.getElementById('rm-3d-play-btn');
  if (btn) btn.innerHTML = '<i class="bi bi-pause-fill"></i>';
  // Pause restyle ticks while user drags (orbit/zoom), resume on mouseup
  const plotEl = document.getElementById('rm-3d-plt');
  if (plotEl) {
    plotEl.addEventListener('mousedown', _rmPlay3dDragOn);
    plotEl.addEventListener('touchstart', _rmPlay3dDragOn, { passive: true });
  }
  document.addEventListener('mouseup', _rmPlay3dDragOff);
  document.addEventListener('touchend', _rmPlay3dDragOff);
  const spd = +(document.getElementById('rm-3d-play-speed')?.value) || 300;
  _rmPlay3dAdvance(all);
  _RM.playTimer3d = setInterval(() => {
    const cur = _rmPlaySorted();
    if (_RM.playFrame3d >= cur.length) { _rmPlay3dStop(); return; }
    _rmPlay3dAdvance(cur);
  }, spd);
}

function _rmPlay3dPause() {
  if (_RM.playTimer3d) { clearInterval(_RM.playTimer3d); _RM.playTimer3d = null; }
  _RM.playing3d = false;
  const btn = document.getElementById('rm-3d-play-btn');
  if (btn) btn.innerHTML = '<i class="bi bi-play-fill"></i>';
  // Remove drag listeners
  const plotEl = document.getElementById('rm-3d-plt');
  if (plotEl) {
    plotEl.removeEventListener('mousedown', _rmPlay3dDragOn);
    plotEl.removeEventListener('touchstart', _rmPlay3dDragOn);
  }
  document.removeEventListener('mouseup', _rmPlay3dDragOff);
  document.removeEventListener('touchend', _rmPlay3dDragOff);
}

function _rmPlay3dStop() {
  _rmPlay3dPause();
  _RM.playFrame3d = 0;
  if (_RM.init3d) _rmDraw3d();
  const info = document.getElementById('rm-3d-play-info');
  if (info) info.textContent = '';
  const bar = document.getElementById('rm-3d-play-bar-fill');
  if (bar) bar.style.width = '0%';
}

function _rmPlay3dAdvance(all) {
  _RM.playFrame3d = Math.min(_RM.playFrame3d + 1, all.length);
  const subset = all.slice(0, _RM.playFrame3d);
  const plotDiv = document.getElementById('rm-3d-plt');
  if (plotDiv && _RM.init3d && _RM._3dEvIdx != null) {
    // Inject saved camera into layout before restyle so WebGL re-render uses it
    if (_RM._3dCamera && plotDiv.layout?.scene) plotDiv.layout.scene.camera = _RM._3dCamera;
    Plotly.restyle(plotDiv, {
      x: [subset.map(e => e.lon)],
      y: [subset.map(e => e.lat)],
      z: [subset.map(e => e.dep)],
      text: [subset.map(e => `<b>${e.event_id}</b><br>M=${e.mag != null ? e.mag.toFixed(2) : '–'}<br>D=${e.dep.toFixed(1)} km`)],
      'marker.color': [subset.map(e => e.dep)],
      'marker.size': [subset.map(e => Math.max(2.5, _rmM2r(e.mag) * 0.55))],
    }, [_RM._3dEvIdx]);
  }
  const last = subset[_RM.playFrame3d - 1];
  const info = document.getElementById('rm-3d-play-info');
  if (info) info.textContent = `${_TZ.fmt(last?.datetime, false)}  ·  ${_RM.playFrame3d}/${all.length}`;
  const bar = document.getElementById('rm-3d-play-bar-fill');
  if (bar) bar.style.width = (all.length ? _RM.playFrame3d / all.length * 100 : 0) + '%';
}

function _rmPlayAdvance(all) {
  _RM.playFrame = Math.min(_RM.playFrame + 1, all.length);
  _RM.playSubset = all.slice(0, _RM.playFrame);
  // Update 2D map only (3D has its own independent play)
  _rmBuildMarkers();
  // Progress display
  const last = _RM.playSubset[_RM.playFrame - 1];
  const info = document.getElementById('rm-play-info');
  if (info) info.textContent = `${_TZ.fmt(last?.datetime, false)}  ·  ${_RM.playFrame}/${all.length}`;
  const bar = document.getElementById('rm-play-bar-fill');
  if (bar) bar.style.width = (all.length ? _RM.playFrame / all.length * 100 : 0) + '%';
}

// ── Full update ───────────────────────────────────────────────────────────────
function _rmUpdate() {
  _rmBuildMarkers();
  _rmUpdateBands();
  _rmDrawNS();
  _rmDrawEW();
  _rmDrawMag();
  _rmDrawRx();
  _rmUpdateStats();
  // keep the 3D view (incl. adaptive topo exaggeration) in sync with the depth slider
  const bd3d = document.getElementById('rm-3d-bd');
  if (bd3d && !bd3d.classList.contains('hidden')) _rmDraw3d();
}

// ── Map init ─────────────────────────────────────────────────────────────────
function _rmInitMap() {
  if (_RM.map) { _RM.map.remove(); _RM.map = null; _RM.ewInit = _RM.nsInit = _RM.magInit = false; }
  // Reset 3D init so newPlot is called again with the new dataset bounds
  _RM.init3d = false;

  const stas = _RM.data?.stations || [];
  if (stas.length) {
    const lats = stas.map(s => s.lat), lons = stas.map(s => s.lon);
    _RM.cla = (Math.min(...lats) + Math.max(...lats)) / 2;
    _RM.clo = (Math.min(...lons) + Math.max(...lons)) / 2;
  }

  _RM.map = L.map('rm-map', { center: [_RM.cla, _RM.clo], zoom: 10, zoomControl: true });
  SW.addStaticCompass(document.getElementById('rm-map'), 'bl');

  const carto = L.tileLayer('/tiles/carto_dark/{z}/{x}/{y}.png',
    { attribution: '© CartoDB', maxZoom: 19 }).addTo(_RM.map);
  L.control.layers({
    'Dark (CartoDB)': carto,
    'OpenStreetMap': L.tileLayer('/tiles/osm/{z}/{x}/{y}.png', { maxZoom: 19 }),
    'Satellite (Esri)': L.tileLayer('/tiles/esri_imagery/{z}/{x}/{y}.png', { maxZoom: 19 }),
    'Ocean Topo (Esri)': L.tileLayer('/tiles/esri_ocean/{z}/{x}/{y}.png', { maxZoom: 13 }),
    'OpenTopoMap': L.tileLayer('/tiles/opentopomap/{z}/{x}/{y}.png', { maxZoom: 17, subdomains: ['a', 'b', 'c'] }),
  }, {}, { position: 'topleft' }).addTo(_RM.map);

  // Cross-section bands
  _RM.ewBand = L.rectangle([[0, 0], [0, 0]], { color: '#f59e0b', weight: 1.5, fillColor: '#f59e0b', fillOpacity: .07, dashArray: '6 4', interactive: false }).addTo(_RM.map);
  _RM.nsBand = L.rectangle([[0, 0], [0, 0]], { color: '#60a5fa', weight: 1.5, fillColor: '#60a5fa', fillOpacity: .07, dashArray: '6 4', interactive: false }).addTo(_RM.map);
  _RM.rxBand = L.polygon([], { color: '#a78bfa', weight: 1.5, fillColor: '#a78bfa', fillOpacity: .07, dashArray: '5 3', interactive: false }).addTo(_RM.map);
  _RM.rxLine = L.polyline([], { color: '#a78bfa', weight: 2, dashArray: '8 4', interactive: false }).addTo(_RM.map);

  // Crosshair draggable marker
  const xhIcon = L.divIcon({
    className: '', iconSize: [34, 34], iconAnchor: [17, 17],
    html: `<svg width="34" height="34" viewBox="-17 -17 34 34" xmlns="http://www.w3.org/2000/svg">
      <line x1="-17" y1="0" x2="-6" y2="0" stroke="#f59e0b" stroke-width="2"/>
      <line x1="6" y1="0" x2="17" y2="0" stroke="#f59e0b" stroke-width="2"/>
      <line x1="0" y1="-17" x2="0" y2="-6" stroke="#f59e0b" stroke-width="2"/>
      <line x1="0" y1="6" x2="0" y2="17" stroke="#f59e0b" stroke-width="2"/>
      <circle cx="0" cy="0" r="5.5" fill="none" stroke="#f59e0b" stroke-width="2"/>
      <circle cx="0" cy="0" r="1.8" fill="#f59e0b"/>
    </svg>` });
  _RM.xhair = L.marker([_RM.cla, _RM.clo], { icon: xhIcon, draggable: true, zIndexOffset: 2000 }).addTo(_RM.map);
  _RM.xhair.on('dragend', e => {
    const ll = e.target.getLatLng();
    _RM.cla = +ll.lat.toFixed(4); _RM.clo = +ll.lng.toFixed(4);
    _rmUpdateBands(); _rmDrawNS(); _rmDrawEW();
  });
  _RM.map.on('click', e => {
    _RM.cla = +e.latlng.lat.toFixed(4); _RM.clo = +e.latlng.lng.toFixed(4);
    _RM.xhair.setLatLng([_RM.cla, _RM.clo]);
    _rmUpdateBands(); _rmDrawNS(); _rmDrawEW();
  });

  // Layer groups
  _RM.lgEvs = L.layerGroup().addTo(_RM.map);
  _RM.lgSta = L.layerGroup().addTo(_RM.map);

  // Slab2 depth contours on the map (uses the AOI slab already fetched)
  _rmBuildSlabMapLayer();

  // Ensure Leaflet knows its container size after CSS layout settles
  setTimeout(() => { if (_RM.map) _RM.map.invalidateSize(); }, 120);
  // Load fault/volcano overlays (async, non-blocking)
  _rmLoadOverlays();

  // Station markers
  stas.forEach(s => {
    const icon = L.divIcon({
      className: '', iconSize: [14, 13], iconAnchor: [7, 13],
      html: `<svg width="14" height="13" viewBox="0 0 14 13"><polygon points="7,1 13,12 1,12" fill="#ef4444" stroke="#fff" stroke-width="1.2" opacity=".9"/></svg>`
    });
    L.marker([s.lat, s.lon], { icon, zIndexOffset: 1000 })
      .bindTooltip(`<b>${s.network}.${s.station}</b><br>${s.lat.toFixed(4)}°N, ${s.lon.toFixed(4)}°E<br>Elev: ${s.elev || 0} m`,
        { sticky: true, className: 'rm-tip' })
      .addTo(_RM.lgSta);
  });
}

// ── Cross-section bands ───────────────────────────────────────────────────────
function _rmUpdateBands() {
  if (!_RM.map) return;
  const dLat = _RM.xsHalf / 111.32;
  const dLon = _RM.xsHalf / (111.32 * Math.cos(_RM.cla * Math.PI / 180));
  const r = _RM.data?.region || {};
  const latMin = r.lat_min ?? (_RM.cla - 0.3);
  const latMax = r.lat_max ?? (_RM.cla + 0.3);
  const lonMin = r.lon_min ?? (_RM.clo - 0.3);
  const lonMax = r.lon_max ?? (_RM.clo + 0.3);
  _RM.ewBand.setBounds([[_RM.cla - dLat, lonMin - 0.02], [_RM.cla + dLat, lonMax + 0.02]]);
  _RM.nsBand.setBounds([[latMin - 0.02, _RM.clo - dLon], [latMax + 0.02, _RM.clo + dLon]]);

  // Rotatable XS band
  if (_RM.rxBand && _RM.rxLine) {
    const azR = _RM.az * Math.PI / 180;
    const sinA = Math.sin(azR), cosA = Math.cos(azR);
    const cosLat = Math.cos(_RM.cla * Math.PI / 180);
    const KM = 111.32;
    const R = 250;  // extend 250 km each way
    const half = _RM.xsHalf;
    const uE = sinA, uN = cosA;    // along direction
    const vE = cosA, vN = -sinA;   // perpendicular direction
    const toLL = (dE, dN) => [_RM.cla + dN / KM, _RM.clo + dE / (KM * cosLat)];
    const corners = [
      toLL(uE * R + vE * half, uN * R + vN * half),
      toLL(uE * R - vE * half, uN * R - vN * half),
      toLL(-uE * R - vE * half, -uN * R - vN * half),
      toLL(-uE * R + vE * half, -uN * R + vN * half),
    ];
    _RM.rxBand.setLatLngs(corners);
    _RM.rxLine.setLatLngs([toLL(uE * R, uN * R), toLL(-uE * R, -uN * R)]);
  }
}

// ── Map event markers ─────────────────────────────────────────────────────────
function _rmBuildMarkers() {
  if (!_RM.lgEvs) return;
  _RM.lgEvs.clearLayers();
  const renderer = L.canvas({ padding: 0.5 });
  _rmActiveEvents().forEach(ev => {
    const r = _rmM2r(ev.mag);
    const mk = L.circleMarker([ev.lat, ev.lon], {
      radius: r, fillColor: _rmD2hex(ev.dep),
      color: 'rgba(255,255,255,0.7)', weight: 1,
      fillOpacity: 0.88, renderer,
    });
    const _ncTip = ev.nearest_city && ev.nearest_city.dist_km != null
      ? `<br><span style="color:#60a5fa">${Math.round(ev.nearest_city.dist_km)} km ${ev.nearest_city.direction} of ${ev.nearest_city.city}</span>`
      : '';
    mk.bindTooltip(
      `<b>${ev.event_id}</b><br>
       <span style="color:#94a3b8">${_TZ.fmt(ev.datetime)}</span><br>
       ${ev.lat.toFixed(4)}°N, ${ev.lon.toFixed(4)}°E<br>
       Depth: <b style="color:${_rmD2css(ev.dep)}">${ev.dep.toFixed(1)} km</b><br>
       Mag: <b>${ev.mag != null ? ev.mag.toFixed(2) : '–'}</b> &nbsp; RMS: <b>${ev.rms != null ? parseFloat(ev.rms).toFixed(3) : '–'} s</b><br>
       Phases: <b>${ev.nsta || '–'}</b>${_ncTip}`,
      { sticky: true, className: 'rm-tip', opacity: 0.97 });
    mk.on('click', () => _rmShowEventDetail(ev));
    _RM.lgEvs.addLayer(mk);
  });
}

// ── Plotly dark theme helper ──────────────────────────────────────────────────
const _RM_DARK = {
  paper_bgcolor: '#0f172a', plot_bgcolor: '#0a0f1e',
  font: { color: '#94a3b8', size: 9, family: 'Segoe UI,Arial,sans-serif' },
  margin: { l: 56, r: 16, t: 30, b: 46 },
};
const _RM_CFG = {
  responsive: true, displayModeBar: true, displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
  toImageButtonOptions: { filename: 'seiswork_result', scale: 2 },
};

function _rmXsLayout(xTitle, xRange, centerVal, isEW) {
  const TOPO_KM = 5;
  return Object.assign({}, _RM_DARK, {
    uirevision: 'fixed',
    xaxis: {
      title: { text: xTitle, font: { size: 11, color: '#60a5fa' } },
      range: xRange, gridcolor: '#1e3a5f', gridwidth: 1,
      zerolinecolor: 'rgba(37,99,235,0.25)', zerolinewidth: 1,
      tickfont: { size: 11 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    yaxis: {
      title: { text: 'Depth (km)', font: { size: 11, color: '#60a5fa' } },
      autorange: 'reversed',
      gridcolor: '#1e3a5f', gridwidth: 1,
      zerolinecolor: '#22c55e', zerolinewidth: 2,
      tickfont: { size: 11 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    showlegend: false,
    shapes: [
      {
        type: 'rect', xref: 'paper', x0: 0, x1: 1, y0: -TOPO_KM, y1: 0,
        fillcolor: 'rgba(34,197,94,0.06)', line: { width: 0 }, layer: 'below'
      },
      {
        type: 'line', x0: xRange[0], x1: xRange[1], y0: 0, y1: 0,
        line: { color: '#22c55e', width: 2 }
      },
      {
        type: 'line', x0: centerVal, x1: centerVal, y0: 0, y1: 1, yref: 'paper',
        line: { color: isEW ? 'rgba(245,158,11,0.25)' : 'rgba(96,165,250,0.25)', width: 1, dash: 'dash' }
      },
      ...[50, 100, 150, 200, 300, 400, 500].filter(d => d <= _RM.depMax).map(d => ({
        type: 'line', x0: xRange[0], x1: xRange[1], y0: d, y1: d,
        line: { color: 'rgba(30,58,95,0.6)', width: 0.8, dash: 'dot' }
      })),
    ],
    annotations: _RM.showTopo ? [{
      x: 0, y: 0, xref: 'paper', yref: 'paper', xanchor: 'left', yanchor: 'bottom',
      text: `Topo ×${_RM_TOPO_EXAG}`, showarrow: false,
      font: { size: 8, color: '#c8a96e' },
      bgcolor: 'rgba(15,23,42,0.7)', borderpad: 2,
    }] : [],
  });
}

function _rmMakeXsTrace(evs, axis) {
  return {
    type: 'scatter', mode: 'markers',
    x: evs.map(e => e[axis]),
    y: evs.map(e => e.dep),
    text: evs.map(e =>
      `<b>${e.event_id}</b><br>D=${e.dep.toFixed(1)}km M=${e.mag != null ? e.mag.toFixed(2) : '–'}<br>${_TZ.fmt(e.datetime)}<br>Method: ${e.method || ''}`),
    hoverinfo: 'text',
    marker: {
      color: evs.map(e => e.dep), colorscale: _RM_PLTCS, cmin: 0, cmax: 400,
      size: evs.map(e => _rmM2r(e.mag)),
      opacity: 0.9,
      symbol: 'circle',
      line: { color: 'rgba(255,255,255,0.6)', width: 0.9 },
    },
    customdata: evs.map(e => e.event_id),
  };
}

function _rmFilterXS(axis, center) {
  const half = axis === 'lat' ? _RM.xsHalf / 111.32 : _RM.xsHalf / (111.32 * Math.cos(_RM.cla * Math.PI / 180));
  return _rmActiveEvents().filter(e => Math.abs(e[axis] - center) <= half);
}

function _rmDrawNS() {
  const evs = _rmFilterXS('lon', _RM.clo);
  const r = _RM.data?.region || {};
  const latMin = r.lat_min != null ? r.lat_min - 0.01 : Math.min(...evs.map(e => e.lat), _RM.cla) - 0.05;
  const latMax = r.lat_max != null ? r.lat_max + 0.01 : Math.max(...evs.map(e => e.lat), _RM.cla) + 0.05;
  const evTr = _rmMakeXsTrace(evs, 'lat');
  evTr.customdata = evs.map(e => ({ eid: e.event_id, jid: _RM.activeJob?.job_id }));
  const topoTrs = _RM.showTopo ? _rmTopoNS(latMin, latMax) : [];
  const slabTrs = (_RM.showSlab && _RM.slabData) ? _rmSlabNS() : [];
  const slab2Tr = _RM.showSlab2d ? _rmSlab2NSTrace() : null;
  const allTrs = [...topoTrs, ...slabTrs, ...(slab2Tr ? [slab2Tr] : []), evTr];
  const ly = _rmXsLayout('Latitude (°)', [latMin, latMax], _RM.cla, false);
  if (!_RM.nsInit) {
    Plotly.newPlot('rm-ns-plt', allTrs, ly, _RM_CFG); _RM.nsInit = true;
    document.getElementById('rm-ns-plt').on('plotly_click', d => {
      const cd = d.points[0]?.customdata;
      if (cd?.eid) { const ev = _RM.activeJob.events.find(e => String(e.event_id) === String(cd.eid)); if (ev) _rmShowEventDetail({ ...ev, lat: parseFloat(ev.lat), lon: parseFloat(ev.lon), dep: parseFloat(ev.depth_km || 0), mag: ev.mag != null ? parseFloat(ev.mag) : null }); }
    });
  } else { Plotly.react('rm-ns-plt', allTrs, ly); }
}

function _rmDrawEW() {
  const evs = _rmFilterXS('lat', _RM.cla);
  const r = _RM.data?.region || {};
  const lonMin = r.lon_min != null ? r.lon_min - 0.01 : Math.min(...evs.map(e => e.lon), _RM.clo) - 0.05;
  const lonMax = r.lon_max != null ? r.lon_max + 0.01 : Math.max(...evs.map(e => e.lon), _RM.clo) + 0.05;
  const evTr = _rmMakeXsTrace(evs, 'lon');
  evTr.customdata = evs.map(e => ({ eid: e.event_id, jid: _RM.activeJob?.job_id }));
  const topoTrs = _RM.showTopo ? _rmTopoEW(lonMin, lonMax) : [];
  const slabTrs = (_RM.showSlab && _RM.slabData) ? _rmSlabEW() : [];
  const slab2Tr = _RM.showSlab2d ? _rmSlab2EWTrace() : null;
  const allTrs = [...topoTrs, ...slabTrs, ...(slab2Tr ? [slab2Tr] : []), evTr];
  const ly = _rmXsLayout('Longitude (°)', [lonMin, lonMax], _RM.clo, true);
  if (!_RM.ewInit) {
    Plotly.newPlot('rm-ew-plt', allTrs, ly, _RM_CFG); _RM.ewInit = true;
    document.getElementById('rm-ew-plt').on('plotly_click', d => {
      const cd = d.points[0]?.customdata;
      if (cd?.eid) { const ev = _RM.activeJob.events.find(e => String(e.event_id) === String(cd.eid)); if (ev) _rmShowEventDetail({ ...ev, lat: parseFloat(ev.lat), lon: parseFloat(ev.lon), dep: parseFloat(ev.depth_km || 0), mag: ev.mag != null ? parseFloat(ev.mag) : null }); }
    });
  } else { Plotly.react('rm-ew-plt', allTrs, ly); }
}

function _rmDrawMag() {
  const evs = _rmActiveEvents();
  const times = evs.map(e => _TZ.fmt(e.datetime));
  const mags = evs.map(e => e.mag);
  const deps = evs.map(e => e.dep);
  const tr = {
    type: 'scatter', mode: 'markers',
    x: times, y: mags,
    text: evs.map(e => `<b>${e.event_id}</b><br>M=${e.mag != null ? e.mag.toFixed(2) : '–'} D=${e.dep.toFixed(1)}km`),
    hoverinfo: 'text',
    marker: {
      color: deps, colorscale: _RM_PLTCS, cmin: 0, cmax: 400,
      size: mags.map(m => _rmM2r(m)), opacity: 0.88,
      line: { color: 'rgba(255,255,255,0.5)', width: 0.8 },
    },
    customdata: evs.map(e => e.event_id),
  };
  const ly = Object.assign({}, _RM_DARK, {
    margin: { l: 52, r: 16, t: 30, b: 60 },
    xaxis: { title: { text: 'Time', font: { size: 9, color: '#60a5fa' } }, gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b', showline: true, linecolor: '#1e3a5f', mirror: true },
    yaxis: { title: { text: 'Magnitude', font: { size: 9, color: '#60a5fa' } }, gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b', showline: true, linecolor: '#1e3a5f', mirror: true },
    showlegend: false,
  });
  if (!_RM.magInit) {
    Plotly.newPlot('rm-mag-plt', [tr], ly, _RM_CFG); _RM.magInit = true;
    document.getElementById('rm-mag-plt').on('plotly_click', d => {
      const eid = d.points[0]?.customdata;
      if (eid) { const ev = _RM.activeJob.events.find(e => String(e.event_id) === String(eid)); if (ev) _rmShowEventDetail({ ...ev, lat: parseFloat(ev.lat), lon: parseFloat(ev.lon), dep: parseFloat(ev.depth_km || 0), mag: ev.mag != null ? parseFloat(ev.mag) : null }); }
    });
  }
  else Plotly.react('rm-mag-plt', [tr], ly);
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function _rmUpdateStats() {
  const evs = _rmActiveEvents();
  const deps = evs.map(e => parseFloat(e.dep)).filter(v => !isNaN(v) && isFinite(v) && v >= 0);
  const mags = evs.map(e => parseFloat(e.mag)).filter(v => !isNaN(v) && isFinite(v));
  document.getElementById('rm-stat-events').textContent = `${evs.length} events`;
  document.getElementById('rm-stat-sta').textContent = `${_RM.data?.stations?.length || 0} sta`;
  document.getElementById('rm-stat-depth').textContent = deps.length
    ? `D ${Math.min(...deps).toFixed(0)}–${Math.max(...deps).toFixed(0)} km` : '–';
  document.getElementById('rm-stat-mag').textContent = mags.length
    ? `M ${Math.min(...mags).toFixed(2)}–${Math.max(...mags).toFixed(2)}` : '–';
}

// ── Filter controls ───────────────────────────────────────────────────────────
function rmMgMin(el) {
  _RM.mgMin = +el.value;
  document.getElementById('rm-mg-min-v').textContent = _RM.mgMin <= -1 ? 'All' : _RM.mgMin.toFixed(1);
  _rmUpdate();
}
function rmMgMax(el) {
  _RM.mgMax = +el.value;
  document.getElementById('rm-mg-max-v').textContent = _RM.mgMax.toFixed(1);
  _rmUpdate();
}
function rmDepMax(el) {
  _RM.depMax = +el.value;
  document.getElementById('rm-dep-max-v').textContent = _RM.depMax + ' km';
  _rmUpdate();
}
function rmXsw(el) {
  _RM.xsHalf = +el.value;
  document.getElementById('rm-xsw-v').textContent = _RM.xsHalf + ' km';
  _rmUpdateBands(); _rmDrawNS(); _rmDrawEW();
}

// ── Safe formatters ───────────────────────────────────────────────────────────
function _rmFmt(v, digits, unit, fallback) {
  const n = parseFloat(v);
  return (v != null && !isNaN(n) && isFinite(n)) ? n.toFixed(digits) + (unit || '') : (fallback || '–');
}

// ── Event waveform modal (waveform + picks) ──────────────────────────────────
function _rmShowEventDetail(ev) {
  document.getElementById('rm-wv-bd').classList.remove('hidden');

  const dep = parseFloat(ev.dep ?? ev.depth_km);
  const mag = ev.mag;
  const lat = parseFloat(ev.lat), lon = parseFloat(ev.lon);
  const depOk = !isNaN(dep) && isFinite(dep);
  const latStr = !isNaN(lat) ? `${lat.toFixed(4)}°` : '–';
  const lonStr = !isNaN(lon) ? `${lon.toFixed(4)}°` : '–';
  document.getElementById('rm-wv-title').textContent = `Event: ${ev.event_id}`;
  const _ncRm = ev.nearest_city;
  const _ncRmSpan = _ncRm && _ncRm.dist_km != null
    ? `<span style="color:#60a5fa">${Math.round(_ncRm.dist_km)} km ${_ncRm.direction} of ${_ncRm.city}</span>`
    : '';
  document.getElementById('rm-wv-meta').innerHTML =
    `<span>${_TZ.fmt(ev.datetime)} <span style="font-size:.62rem;color:#64748b">${_TZ.h===7?'WIB':'UTC'}</span></span>
     <span>${latStr}N, ${lonStr}E</span>
     <span style="color:${depOk ? _rmD2css(dep) : '#94a3b8'}">Depth: <b>${depOk ? dep.toFixed(1) + ' km' : '–'}</b></span>
     <span>Mag: <b>${_rmFmt(mag, 2)}</b></span>
     ${_ncRmSpan}
     <span>RMS: <b>${_rmFmt(ev.rms, 3, ' s')}</b></span>
     <span>Phases: <b>${ev.nsta ?? '–'}</b></span>
     <span>Method: <b>${ev.method || _RM.activeJob?.method || '–'}</b></span>`;

  document.getElementById('rm-wv-loading').style.display = 'flex';
  document.getElementById('rm-wv-err').style.display = 'none';
  document.getElementById('rm-wv-canvas-outer').style.display = 'none';
  const _togEl2 = document.getElementById('rm-wv-spec-tog'); if (_togEl2) _togEl2.checked = false;
  const _srEl2  = document.getElementById('rm-wv-spec-row'); if (_srEl2) _srEl2.style.display = 'none';
  RMWV.reset();

  fetch(`/api/result/${_RM.cfgId}/waveform?job_id=${_RM.activeJob.job_id}&event_id=${encodeURIComponent(ev.event_id)}`)
    .then(r => r.json())
    .then(data => {
      document.getElementById('rm-wv-loading').style.display = 'none';
      if (data.error) { _rmWvErr(data.error); return; }
      RMWV.drawCanvas(data);
    })
    .catch(err => {
      document.getElementById('rm-wv-loading').style.display = 'none';
      _rmWvErr('Waveform fetch error: ' + err.message);
    });
}

function _rmWvErr(msg) {
  const el = document.getElementById('rm-wv-err');
  el.style.display = 'flex';
  el.textContent = msg;
}

// Event Waveform Canvas (result viewer) → modules/result-viewer-waveform.js (RMWV)

// ── 3D Modal open / close / resize ───────────────────────────────────────────
// window.pywebview is injected by pywebview itself into every page it loads —
// a reliable "am I inside the native desktop window" check that needs no
// server round-trip and is safe to read at any time after the page is
// interactive (the object is injected before our own scripts run).
function _isNativeGui() { return typeof window.pywebview !== 'undefined'; }
(function _disable3dBtnIfNative() {
  function apply() {
    if (!_isNativeGui()) return;
    const btn = document.getElementById('rm-btn-3d');
    if (!btn) return;
    btn.disabled = true;   // .btn-sw:disabled already dims it (opacity .35)
    btn.title = '3D is not supported in this desktop window (WebGL disabled) — open SeisWork in a browser to view 3D';
  }
  apply();   // already injected by the time this script runs, in most backends
  window.addEventListener('pywebviewready', apply);   // safety net for the rest
})();

function rmOpen3d() {
  document.getElementById('rm-3d-bd').classList.remove('hidden');
  _rm3dApplySize();
  // The native desktop window (WebKitGTK, compositing forced off for VM
  // safety — see _harden_webkit_native_window in cli.py) doesn't just fail to
  // get a WebGL context, it crashes the WHOLE WebProcess the instant Plotly's
  // scatter3d tries to create one (confirmed: window goes blank white, not
  // just the 3D panel). A JS-side timeout can't recover from that — the JS
  // runtime dies with the process. So refuse before ever touching Plotly here.
  if (_isNativeGui()) {
    _rm3dShowError('3D is not supported in this desktop window (WebGL disabled). Open SeisWork in a browser to view 3D.');
    return;
  }
  _rm3dSetLoading(true, 'Rendering 3D layers…');   // shown until every layer is ready
  // Watchdog: still kept for the browser path — a working WebGL context can
  // still fail to materialize (old GPU driver, headless CI, etc.), leaving
  // Plotly.newPlot()'s promise unresolved and the overlay above spinning
  // forever. If nothing has rendered within 10s, show an explicit error.
  clearTimeout(_RM._3dWatchdog);
  _RM._3dWatchdog = setTimeout(() => {
    if (!_RM.init3d) {
      _rm3dShowError('3D failed to load (WebGL may be unavailable). Try reloading the page, or close this panel.');
    }
  }, 10000);
  _rmDraw3d();
  setTimeout(() => { try { Plotly.Plots.resize('rm-3d-plt'); } catch (_) { } }, 150);
}

// Which enabled 3D layers are still streaming in (async-loaded, trigger a redraw
// when done). The lazy-load overlay stays up until this list is empty.
function _rm3dPendingLayers() {
  const p = [];
  if (_RM.showCoast3d && _RM.coast3dData === null) p.push('coastline');
  if (_RM.showTopo3d  && _RM.topo3dData  === null) p.push('topography');
  if (_RM.showFault3d && SW.mapOverlays    === null) p.push('faults');
  return p;
}

function _rm3dSetLoading(on, msg) {
  const ovl = document.getElementById('rm-3d-loading');
  if (!ovl) return;
  ovl.classList.toggle('hidden', !on);
  ovl.classList.remove('rm-loading-err');
  if (on && msg) { const m = document.getElementById('rm-3d-load-msg'); if (m) m.textContent = msg; }
}
function _rm3dShowError(msg) {
  const ovl = document.getElementById('rm-3d-loading');
  if (!ovl) return;
  ovl.classList.remove('hidden');
  ovl.classList.add('rm-loading-err');
  const m = document.getElementById('rm-3d-load-msg');
  if (m) m.textContent = msg;
}
function rmClose3d() {
  _rmPlay3dStop();
  _RM._3dCamera = null;
  clearTimeout(_RM._3dWatchdog);
  const el = document.getElementById('rm-3d-bd');
  if (el) el.classList.add('hidden');
}
function _rm3dResize() {
  _rm3dApplySize();
  setTimeout(() => { try { Plotly.Plots.resize('rm-3d-plt'); } catch (_) { } }, 80);
}
function _rm3dApplySize() {
  const wEl = document.getElementById('rm3d-w'), hEl = document.getElementById('rm3d-h');
  if (!wEl || !hEl) return;
  const w = wEl.value, h = hEl.value;
  const m = document.getElementById('rm-3d-m');
  if (m) { m.style.setProperty('--rm3dw', w + 'vw'); m.style.setProperty('--rm3dh', h + 'vh'); m.style.width = w + 'vw'; m.style.height = h + 'vh'; }
  const wv = document.getElementById('rm3d-wv'), hv = document.getElementById('rm3d-hv');
  if (wv) wv.textContent = w + '%';
  if (hv) hv.textContent = h + '%';
}

// ── Tab switcher ──────────────────────────────────────────────────────────────
function rmTab(tab) {
  document.querySelectorAll('.rm-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.rm-tab-pane').forEach(p => p.classList.toggle('active', p.id === 'rm-tab-' + tab));
  if (tab === 'stat') {
    // draw on first switch; subsequent draws when filter changes via _rmUpdate
    _rmDrawStat();
    setTimeout(() => {
      ['rm-rms-plt', 'rm-fmd-plt'].forEach(id => { try { Plotly.Plots.resize(id); } catch (_) { } });
    }, 80);
  } else if (tab === 'residual') {
    rmResLoad();   // fetch latest relocation residual (once per open unless re-selected)
  }
}

// ── Residual HypoDD diagnostics (port of residual_hypodd.ipynb) ──────────────
// Residual tab FOLLOWS the main catalog selector (_RM.activeJob) — not standalone.
let _rmRes = { loadedJob: null, data: null };

const _RES_NO_RELOC_HTML =
  '<i class="bi bi-bar-chart-line" style="font-size:1rem;opacity:.5;flex-shrink:0"></i>' +
  '<span>No data for plot RMS &mdash; select a catalog from <b>relocation</b> results ' +
  '(HypoDD / GrowClust) in the catalog selection above.</span>';

async function rmResLoad() {
  const ld = document.getElementById('rm-res-loading');
  const er = document.getElementById('rm-res-error');
  const ct = document.getElementById('rm-res-content');
  if (!ld || !er || !ct) return;

  // helpers — inline style wins over class for rm-res-loading which has display:flex inline
  const ldShow = () => { ld.style.display = 'flex'; };
  const ldHide = () => { ld.style.display = 'none'; };

  // If no active job or not from relocation step → show guidance immediately (no API call)
  if (!_RM.activeJob || _RM.activeJob.step !== 'relocation') {
    ldHide(); ct.classList.add('hidden');
    er.classList.remove('hidden');
    er.innerHTML = _RES_NO_RELOC_HTML;
    return;
  }

  const cfg   = _RM.cfgId || SW.wpCfgId;
  const jobId = _RM.activeJob.job_id;

  // Cache hit — same job, just re-render (show content immediately)
  if (jobId === _rmRes.loadedJob && _rmRes.data) {
    ldHide(); er.classList.add('hidden');
    ct.classList.remove('hidden');
    _rmResRender(_rmRes.data);
    return;
  }

  // Show loading, fetch residual data for this specific relocation job
  ldShow(); er.classList.add('hidden'); ct.classList.add('hidden');
  try {
    const url = `/api/result/${cfg}/residual?job_id=${encodeURIComponent(jobId)}`;
    const r   = await fetch(url);
    const d   = await r.json();
    if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
    _rmRes.data      = d;
    _rmRes.loadedJob = jobId;
    ldHide(); ct.classList.remove('hidden');
    _rmResRender(d);
  } catch (e) {
    ldHide();
    er.classList.remove('hidden');
    er.innerHTML = _RES_NO_RELOC_HTML +
      `<span style="color:#475569;font-size:.66rem;margin-left:.35rem">(${e.message})</span>`;
  }
}

function _rmResRender(d) {
  const jobId = d.job?.job_id || '—';
  const mode  = d.job?.mode   || '';
  const fin   = (d.job?.finished || '').slice(0, 16).replace('T', ' ');
  const nEv   = d.summary?.n_events ?? '?';
  const nSta  = d.summary?.n_sta    ?? '?';
  const all   = d.stats?.find(s => s.fase === 'Semua') || {};

  // Unified top bar
  const el_id   = document.getElementById('rm-res-jobid');
  const el_meta = document.getElementById('rm-res-jobmeta');
  if (el_id)   el_id.textContent   = jobId;
  if (el_meta) el_meta.textContent = `${mode ? '[' + mode + ']' : ''}${fin ? '  ' + fin : ''}`;

  const el_rms  = document.getElementById('rm-res-rms-chip');
  const el_dist = document.getElementById('rm-res-dist-chip');
  const el_nev  = document.getElementById('rm-res-nev-chip');
  if (el_rms)  el_rms.textContent  = `RMS mean ${all.rms ?? '—'} s`;
  if (el_dist) el_dist.textContent = `Dist ${d.summary?.dist_min ?? '—'}–${d.summary?.dist_max ?? '—'} km`;
  if (el_nev)  el_nev.textContent  = `${nEv} events · ${nSta} sta`;

  // (No independent dropdown — residual follows _RM.activeJob)

  // Params
  const kv = (k, v) => `<span style="color:var(--text-muted)">${k}</span>=<b style="color:#cbd5e1">${v}</b>`;
  const ph = d.ph2dt_inp?.params;
  const phHtml = ph
    ? Object.entries(ph).map(([k, v]) => kv(k, v)).join('  ')
    : '<span style="color:var(--text-muted)">not available</span>';
  document.getElementById('rm-res-params').innerHTML =
    `<div><div style="color:#a78bfa;font-weight:600;margin-bottom:.2rem">ph2dt.inp</div><div style="font-family:monospace;line-height:1.7">${phHtml}</div></div>` +
    `<div><div style="color:#a78bfa;font-weight:600;margin-bottom:.2rem">hypoDD.inp</div>${_rmHypoddInp(d.hypodd_inp)}</div>`;

  // Plots
  _rmResHist(d);
  _rmResScatter(d);
  _rmResStation(d);
  _rmResConv(d);
}

// Render hypoDD.inp as structured params (data-type, clustering, solver, the
// per-set weighting schedule, and the 1D velocity model), with the raw file
// kept behind a collapsible <details>. Falls back to raw-only when the parser
// could not structure the file.
function _rmHypoddInp(h) {
  if (!h) return '<span style="color:var(--text-muted)">not available</span>';
  const rawBlock = h.raw
    ? `<details style="margin-top:.35rem"><summary style="cursor:pointer;color:var(--text-muted);font-size:.62rem">raw file</summary>` +
      `<pre style="margin:.2rem 0 0;max-height:160px;overflow:auto;font-size:.6rem;color:#9aa6b2;white-space:pre-wrap">${h.raw.replace(/</g, '&lt;')}</pre></details>`
    : '';
  if (!h.params) return rawBlock || '<span style="color:var(--text-muted)">not available</span>';

  const kv = (k, v) => `<span style="color:var(--text-muted)">${k}</span>=<b style="color:#cbd5e1">${v}</b>`;
  const p = h.params;
  const head = [
    kv('IDAT', p.IDAT), kv('IPHA', p.IPHA), kv('DIST', p.DIST + ' km'),
    kv('OBSCC', p.OBSCC), kv('OBSCT', p.OBSCT),
    kv('ISTART', p.ISTART), kv('ISOLV', p.ISOLV), kv('NSET', p.NSET),
  ].join('  ');

  // Weighting schedule table (one row per iteration set).
  let wTable = '';
  const w = h.weighting;
  if (w && w.rows && w.rows.length) {
    const th = w.cols.map(c => `<th style="padding:.1rem .3rem;color:var(--text-muted);font-weight:600">${c}</th>`).join('');
    const tr = w.rows.map(r =>
      `<tr>${w.cols.map(c => `<td style="padding:.1rem .3rem;text-align:right;color:#cbd5e1">${r[c] ?? '—'}</td>`).join('')}</tr>`).join('');
    wTable = `<div style="margin-top:.3rem;overflow:auto"><table style="border-collapse:collapse;font-size:.6rem"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
  }

  // 1D velocity model (TOP / VEL paired with vp/vs ratio).
  let mdl = '';
  const m = h.model;
  if (m && m.top && m.vel) {
    const cells = m.top.map((t, i) =>
      `<span style="display:inline-block;margin-right:.5rem"><span style="color:var(--text-muted)">${t}</span>km <b style="color:#cbd5e1">${m.vel[i] ?? '—'}</b></span>`).join('');
    mdl = `<div style="margin-top:.3rem;font-family:monospace;line-height:1.6">` +
          `<span style="color:var(--text-muted)">1D model (vp/vs=${p.RATIO}, ${p.NLAY} layers):</span><br>${cells}</div>`;
  }

  return `<div style="font-family:monospace;line-height:1.7">${head}</div>${wTable}${mdl}${rawBlock}`;
}

const _RES_LAYOUT = {
  paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  font: { color: '#cbd5e1', size: 9 }, margin: { l: 46, r: 8, t: 6, b: 34 },
  legend: { orientation: 'h', y: 1.08, font: { size: 8 } },
};
const _RES_CFG = { displayModeBar: false, responsive: true };

function _binCenters(edges) { return edges.slice(0, -1).map((e, i) => (e + edges[i + 1]) / 2); }

function _rmResHist(d) {
  const div = 'rm-res-hist';
  try { Plotly.purge(div); } catch (_) { }
  const traces = [];
  if (d.hist?.p?.counts?.length)
    traces.push({ x: _binCenters(d.hist.p.edges), y: d.hist.p.counts, type: 'bar', name: 'P', marker: { color: '#2ca02c' }, opacity: 0.6 });
  if (d.hist?.s?.counts?.length)
    traces.push({ x: _binCenters(d.hist.s.edges), y: d.hist.s.counts, type: 'bar', name: 'S', marker: { color: '#d62728' }, opacity: 0.6 });
  const lay = Object.assign({}, _RES_LAYOUT, {
    barmode: 'overlay', xaxis: { title: 'Residual (s)', gridcolor: '#2a2f3a', zeroline: true, zerolinecolor: '#64748b' },
    yaxis: { title: 'Frequency', gridcolor: '#2a2f3a' },
  });
  try { Plotly.newPlot(div, traces, lay, _RES_CFG); } catch (_) { }
}

function _rmResScatter(d) {
  const div = 'rm-res-scatter';
  try { Plotly.purge(div); } catch (_) { }
  const traces = [];
  if (d.scatter?.p?.x?.length)
    traces.push({ x: d.scatter.p.x, y: d.scatter.p.y, mode: 'markers', type: 'scatter', name: 'P', marker: { color: '#2ca02c', size: 3, opacity: 0.5 } });
  if (d.scatter?.s?.x?.length)
    traces.push({ x: d.scatter.s.x, y: d.scatter.s.y, mode: 'markers', type: 'scatter', name: 'S', marker: { color: '#d62728', size: 3, opacity: 0.5 } });
  const lay = Object.assign({}, _RES_LAYOUT, {
    xaxis: { title: 'Inter-event distance (km)', gridcolor: '#2a2f3a' },
    yaxis: { title: 'Residual (s)', gridcolor: '#2a2f3a', zeroline: true, zerolinecolor: '#64748b' },
  });
  try { Plotly.newPlot(div, traces, lay, _RES_CFG); } catch (_) { }
}

function _rmResStation(d) {
  const div = 'rm-res-station';
  try { Plotly.purge(div); } catch (_) { }
  const rows = d.per_station || [];
  const stas = rows.map(r => r.sta);
  const mk = (key, name, col) => {
    const pts = rows.filter(r => r[key]);
    return {
      x: pts.map(r => r.sta),
      y: pts.map(r => r[key].median),
      error_y: {
        type: 'data', symmetric: false,
        array: pts.map(r => r[key].q3 - r[key].median),
        arrayminus: pts.map(r => r[key].median - r[key].q1),
        color: col, thickness: 1.2, width: 2,
      },
      mode: 'markers', type: 'scatter', name, marker: { color: col, size: 6 },
    };
  };
  const traces = [mk('p', 'P', '#2ca02c'), mk('s', 'S', '#d62728')].filter(t => t.x.length);
  const lay = Object.assign({}, _RES_LAYOUT, {
    xaxis: { gridcolor: '#2a2f3a', tickangle: -45, tickfont: { size: 7 }, categoryarray: stas },
    yaxis: { title: 'Residual (s)', gridcolor: '#2a2f3a', zeroline: true, zerolinecolor: '#64748b' },
  });
  try { Plotly.newPlot(div, traces, lay, _RES_CFG); } catch (_) { }
}

function _rmResConv(d) {
  const div = 'rm-res-conv';
  const el = document.getElementById(div);
  try { Plotly.purge(div); } catch (_) { }
  const conv = d.convergence || [];
  if (!conv.length) {
    if (el) el.innerHTML = '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#475569;font-size:.74rem">No iterations (fallback relocation / catalog too sparse)</div>';
    return;
  }
  if (el) el.innerHTML = '';   // clear any previous "no iterations" text
  const it = conv.map(r => r.it);
  const traces = [
    { x: it, y: conv.map(r => r.rmsct_ms), mode: 'lines+markers', name: 'RMSCT (ms)', line: { color: '#60a5fa', width: 2 } },
    { x: it, y: conv.map(r => r.rmsst_ms), mode: 'lines+markers', name: 'RMSST max (ms)', line: { color: '#fb7185', width: 2 }, yaxis: 'y2' },
  ];
  const lay = Object.assign({}, _RES_LAYOUT, {
    xaxis: { title: 'Iteration', gridcolor: '#2a2f3a' },
    yaxis: { title: 'RMSCT (ms)', gridcolor: '#2a2f3a' },
    yaxis2: { title: 'RMSST (ms)', overlaying: 'y', side: 'right', showgrid: false },
  });
  try { Plotly.newPlot(div, traces, lay, _RES_CFG); } catch (_) { }
}

// ── Statistics: RMS + FMD dispatcher ─────────────────────────────────────────
function _rmDrawStat() {
  _rmDrawRMS();
  _rmDrawFMD();
}

// ── RMS Residual Distribution (normal fit) ────────────────────────────────────
function _rmDrawRMS() {
  const el   = document.getElementById('rm-rms-plt');
  const info = document.getElementById('rm-rms-info');
  if (!el) return;

  // Only meaningful for relocation catalogs (hypoDD.res / GrowClust residuals)
  if (!_RM.activeJob || _RM.activeJob.step !== 'relocation') {
    el.innerHTML =
      '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;' +
      'justify-content:center;gap:.55rem;color:#475569;text-align:center;padding:1rem">' +
      '<i class="bi bi-bar-chart-line" style="font-size:1.6rem;opacity:.5"></i>' +
      '<span style="font-size:.8rem">No data for plot RMS</span>' +
      '<span style="font-size:.7rem;color:#334155">Select data from relocation results (HypoDD / GrowClust)</span>' +
      '</div>';
    if (info) info.innerHTML = '';
    return;
  }

  const evs = _rmActiveEvents();
  const vals = evs.map(e => parseFloat(e.rms)).filter(v => !isNaN(v) && isFinite(v) && v >= 0 && v < 10);

  if (vals.length < 3) {
    el.innerHTML = '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#475569;font-size:.8rem">No RMS data in this catalog</div>';
    if (info) info.innerHTML = '';
    return;
  }

  const n = vals.length;
  const mean = vals.reduce((a, b) => a + b, 0) / n;
  const vari = vals.map(v => (v - mean) ** 2).reduce((a, b) => a + b, 0) / n;
  const std = Math.sqrt(vari);
  const med = [...vals].sort((a, b) => a - b)[Math.floor(n / 2)];

  // Normal PDF scaled to histogram count
  const vMax = Math.max(...vals);
  const xNorm = Array.from({ length: 120 }, (_, i) => i * vMax * 1.15 / 119);
  const binW = vMax / 25;
  const normY = xNorm.map(x =>
    n * binW * (1 / (std * Math.sqrt(2 * Math.PI))) * Math.exp(-0.5 * ((x - mean) / std) ** 2)
  );

  const traces = [
    {
      type: 'histogram', x: vals, nbinsx: 28, name: 'RMS',
      marker: { color: '#3b82f6', opacity: 0.7, line: { color: '#1d4ed8', width: 0.8 } },
      showlegend: false
    },
    {
      type: 'scatter', mode: 'lines', x: xNorm, y: normY,
      line: { color: '#f59e0b', width: 2.5 }, name: 'Normal fit', showlegend: false
    },
    {
      type: 'scatter', mode: 'lines',
      x: [mean, mean], y: [0, Math.max(...normY) * 1.15],
      line: { color: '#ef4444', width: 1.5, dash: 'dash' },
      name: `μ=${mean.toFixed(3)}s`, showlegend: false
    },
  ];

  const layout = Object.assign({}, _RM_DARK, {
    margin: { l: 52, r: 18, t: 18, b: 44 },
    xaxis: {
      title: { text: 'RMS Residual (s)', font: { size: 9, color: '#60a5fa' } },
      gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    yaxis: {
      title: { text: 'Count', font: { size: 9, color: '#60a5fa' } },
      gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    annotations: [{
      x: mean, y: 1, xref: 'x', yref: 'paper',
      text: `μ = ${mean.toFixed(3)} s`,
      showarrow: true, arrowhead: 2,
      arrowcolor: '#ef4444', arrowwidth: 2,
      ax: 40, ay: -36,
      font: { size: 11, color: '#ffffff', family: 'monospace' },
      bgcolor: 'rgba(180,30,30,0.85)',
      bordercolor: '#ef4444', borderwidth: 1.5,
      borderpad: 4,
    }],
    showlegend: false,
  });

  Plotly.newPlot('rm-rms-plt', traces, layout, _RM_CFG);
  document.getElementById('rm-rms-info').innerHTML =
    `<span class="rm-mc-chip">n = ${n}</span>` +
    `<span class="rm-mc-chip" style="color:#60a5fa;font-weight:600">RMS rata-rata = ${mean.toFixed(3)} s</span>` +
    `<span class="rm-mc-chip">σ = ${std.toFixed(3)} s</span>` +
    `<span class="rm-mc-chip">Median = ${med.toFixed(3)} s</span>`;
  _rmAppendResidualRMS();
}

// RMS of the double-difference residuals (hypoDD.res), identical to the value
// in the "Residual HypoDD" tab and to residual_hypodd.ipynb
// (RMS = √(mean(DT²)) for Semua/P/S). Fetched once and appended as chips so the
// Statistik tab also surfaces the relocation residual RMS.
async function _rmAppendResidualRMS() {
  const host = document.getElementById('rm-rms-info');
  if (!host) return;
  let rep = _rmRes.data;
  try {
    if (!rep) {
      const r = await fetch(`/api/result/${_RM.cfgId}/residual`);
      const d = await r.json();
      if (r.ok && d.ok !== false) { rep = d; _rmRes.data = d; }
    }
  } catch (_) { /* no relocation residual available */ }
  if (!rep || !rep.stats) return;
  const get = f => rep.stats.find(s => (s.fase || '').startsWith(f)) || {};
  const all = get('Semua'), p = get('P'), s = get('S');
  const chip = (lbl, v, col) =>
    `<span class="rm-mc-chip" style="border-color:${col}66;color:${col}">${lbl} ${v ?? '–'} s</span>`;
  // Remove a previous residual block (re-render safe), then append.
  host.querySelectorAll('[data-resid]').forEach(e => e.remove());
  const frag = document.createElement('span');
  frag.setAttribute('data-resid', '1');
  frag.style.display = 'inline-flex';
  frag.style.gap = '.4rem';
  frag.style.flexWrap = 'wrap';
  frag.innerHTML =
    `<span class="rm-mc-chip" style="background:#1e293b;color:#c8a96e;font-size:.6rem">RMS residual hypoDD.res:</span>` +
    chip('All', all.rms, '#cbd5e1') + chip('P', p.rms, '#2ca02c') + chip('S', s.rms, '#d62728');
  host.appendChild(frag);
}

// ── Frequency-Magnitude Distribution + Mc (MAXC, Aki 1965 MLE b-value) ───────
function _rmDrawFMD() {
  const el = document.getElementById('rm-fmd-plt');
  if (!el) return;
  const evs = _rmActiveEvents();
  const mags = evs.map(e => parseFloat(e.mag)).filter(v => !isNaN(v) && isFinite(v));

  if (mags.length < 10) {
    el.innerHTML = '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#475569;font-size:.8rem">Need at least 10 events with magnitude data</div>';
    document.getElementById('rm-mc-chips').innerHTML = '';
    return;
  }

  const BIN = 0.1;
  const mMin = Math.floor(Math.min(...mags) / BIN) * BIN;
  const mMax = Math.ceil(Math.max(...mags) / BIN) * BIN;
  const bins = [];
  for (let m = mMin; m <= mMax + BIN * 0.01; m += BIN) bins.push(+m.toFixed(2));

  // Non-cumulative per bin (centred), cumulative N≥M
  const nonCum = bins.map(b => mags.filter(m => m >= b - BIN / 2 && m < b + BIN / 2).length);
  const cum = bins.map(b => mags.filter(m => m >= b).length);

  // MAXC: Mc = bin centre at maximum non-cumulative count
  const maxIdx = nonCum.indexOf(Math.max(...nonCum));
  const Mc = bins[maxIdx];

  // Aki (1965) MLE b-value: b = log10(e) / (M_mean - (Mc - ΔM/2))
  const magsAboveMc = mags.filter(m => m >= Mc);
  const Nc = magsAboveMc.length;
  const meanM = magsAboveMc.reduce((a, b) => a + b, 0) / Nc;
  const b_val = Math.log10(Math.E) / (meanM - (Mc - BIN / 2));
  const a_val = Math.log10(Nc) + b_val * Mc;

  // G-R model line from Mc to mMax
  const grX = bins.filter(m => m >= Mc - 1e-9);
  const grY = grX.map(m => Math.pow(10, a_val - b_val * m));

  // Uncertainty δb (Shi & Bolt 1982): δb = 2.30 * b² * sqrt(Σ(Mi-M̄)² / (Nc*(Nc-1)))
  const varM = magsAboveMc.reduce((s, m) => s + (m - meanM) ** 2, 0) / (Nc * Math.max(Nc - 1, 1));
  const db = 2.30 * b_val * b_val * Math.sqrt(varM);

  const cumPos = cum.filter(v => v > 0);
  const cumMax = cumPos.length ? Math.max(...cumPos) : 10;

  const traces = [
    {
      type: 'bar', x: bins, y: nonCum, name: 'Non-kumulatif',
      marker: { color: '#3b82f6', opacity: 0.65, line: { color: '#1d4ed8', width: 0.7 } },
      showlegend: true
    },
    {
      type: 'scatter', mode: 'markers', x: bins, y: cum.map(v => v > 0 ? v : null),
      marker: { color: '#60a5fa', size: 5.5, symbol: 'circle', line: { color: '#bfdbfe', width: 0.8 } },
      name: 'Kumulatif N≥M', showlegend: true, yaxis: 'y2'
    },
    {
      type: 'scatter', mode: 'lines', x: grX, y: grY,
      line: { color: '#f59e0b', width: 2.2 },
      name: `G-R: a=${a_val.toFixed(2)}, b=${b_val.toFixed(2)}`,
      showlegend: true, yaxis: 'y2'
    },
    {
      type: 'scatter', mode: 'lines',
      x: [Mc, Mc], y: [0.8, cumMax],
      line: { color: '#ef4444', width: 2, dash: 'dash' },
      name: `Mc=${Mc.toFixed(1)} (MAXC)`, showlegend: true, yaxis: 'y2'
    },
  ];

  const layout = Object.assign({}, _RM_DARK, {
    margin: { l: 52, r: 62, t: 18, b: 44 },
    barmode: 'overlay',
    xaxis: {
      title: { text: 'Magnitude', font: { size: 9, color: '#60a5fa' } },
      gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f', mirror: true, dtick: 0.5,
    },
    yaxis: {
      title: { text: 'Count (non-kumulatif)', font: { size: 9, color: '#3b82f6' } },
      gridcolor: '#1e3a5f', tickfont: { size: 8 }, color: '#64748b',
      showline: true, linecolor: '#1e3a5f',
    },
    yaxis2: {
      title: { text: 'N ≥ M (kumulatif)', font: { size: 9, color: '#60a5fa' } },
      overlaying: 'y', side: 'right', type: 'log',
      gridcolor: 'rgba(0,0,0,0)', tickfont: { size: 8 }, color: '#64748b',
      showgrid: false, showline: true, linecolor: '#1e3a5f',
    },
    legend: {
      font: { size: 8 }, bgcolor: 'rgba(10,15,30,.88)',
      bordercolor: '#1e3a5f', borderwidth: 1, x: 0.01, y: 0.99, xanchor: 'left'
    },
    shapes: [{
      type: 'line', x0: Mc, x1: Mc, y0: 0, y1: 1, yref: 'paper',
      line: { color: '#ef4444', width: 1.5, dash: 'dash' }, layer: 'below',
    }],
  });

  Plotly.newPlot('rm-fmd-plt', traces, layout, _RM_CFG);
  document.getElementById('rm-mc-chips').innerHTML =
    `<span class="rm-mc-chip">Mc<sub>MAXC</sub> = ${Mc.toFixed(1)}</span>` +
    `<span class="rm-mc-chip">b = ${b_val.toFixed(2)} ± ${db.toFixed(2)}</span>` +
    `<span class="rm-mc-chip">a = ${a_val.toFixed(2)}</span>` +
    `<span class="rm-mc-chip">N(≥Mc) = ${Nc}</span>` +
    `<span class="rm-mc-chip">M̄ = ${meanM.toFixed(2)}</span>`;
}

// ── Load fault / volcano overlays onto result modal map ───────────────────────
async function _rmLoadOverlays() {
  if (!_RM.map) return;
  try {
    const fd = await (await fetch('/footages/indogigis.geojson')).json();
    _RM.lgFault = L.geoJSON(fd, {
      style: feat => (FAULT_STYLE[feat.properties?.tipe || ''] || FAULT_DEFAULT),
    });
    if (_RM.showFault) _RM.lgFault.addTo(_RM.map);
  } catch (_) { }
  try {
    const gd = await (await fetch('/footages/gigis.geojson')).json();
    _RM.lgFaultSym = L.geoJSON(gd, {
      style: () => ({ color: '#f97316', weight: 1, fillColor: '#f97316', fillOpacity: 0.7 }),
      pointToLayer: (feat, latlng) => L.circleMarker(latlng, { radius: 4, color: '#f97316', fillOpacity: 0.8 }),
    });
    if (_RM.showFaultSym) _RM.lgFaultSym.addTo(_RM.map);
  } catch (_) { }
  try {
    const vd = await (await fetch('/footages/Volcano.geojson')).json();
    _RM.lgVol = L.geoJSON(vd, {
      pointToLayer: (feat, latlng) => {
        const nm = (feat.properties?.name || '').split('|')[0].trim();
        return L.marker(latlng, { icon: volcanoIcon() })
          .bindTooltip(`<b>${SW.esc(nm)}</b>`, { className: 'rm-tip' });
      },
    });
    if (_RM.showVolcano) _RM.lgVol.addTo(_RM.map);
    // Cache local volcanoes for XS/3D display
    const r = _RM.data?.region || {};
    const pad = 1;
    _RM.volcanoes = (vd.features || [])
      .filter(f => {
        const [lo, la] = f.geometry.coordinates;
        return lo >= (r.lon_min || 120) - pad && lo <= (r.lon_max || 130) + pad &&
          la >= (r.lat_min || -2) - pad && la <= (r.lat_max || 10) + pad;
      })
      .map(f => ({
        lon: f.geometry.coordinates[0],
        lat: f.geometry.coordinates[1],
        name: (f.properties?.name || '').split('|')[0].trim(),
      }));
  } catch (_) { }
}

// ── Slab2 depth contours on the 2-D map ───────────────────────────────────────
// Renders the same AOI slab GeoJSON used by the cross-sections (_RM.slabData)
// as depth-coloured contour lines on the Leaflet map, on a dedicated pane below
// the event/station markers. Rebuilt on map init; toggled via 'slabmap'.
function _rmBuildSlabMapLayer() {
  if (!_RM.map) return;
  if (_RM.lgSlabMap) { _RM.map.removeLayer(_RM.lgSlabMap); _RM.lgSlabMap = null; }
  const feats = _RM.slabData?.features;
  if (!feats || !feats.length) return;
  if (!_RM.map.getPane('rmSlabPane')) {
    const p = _RM.map.createPane('rmSlabPane');
    p.style.zIndex = 250;               // above tiles (200), below overlays (400)
    p.style.pointerEvents = 'none';
  }
  _RM.lgSlabMap = L.geoJSON(_RM.slabData, {
    pane: 'rmSlabPane',
    style: feat => {
      const dep = -(feat.properties?.ELEV || 0);
      return { color: _slabDepthColor(dep), weight: 1.4, opacity: 0.8, pane: 'rmSlabPane' };
    },
    onEachFeature: (feat, lyr) => {
      const dep = -(feat.properties?.ELEV || 0);
      const rg = (feat.properties?.region || '').toUpperCase();
      lyr.bindTooltip(`Slab${rg ? ' ' + rg : ''} — ${dep.toFixed(0)} km`,
        { className: 'rm-tip', sticky: true });
    },
  });
  if (_RM.showSlabMap) _RM.lgSlabMap.addTo(_RM.map);
}

// ── Layer toggle handler ──────────────────────────────────────────────────────
function _rmLayerToggle(key, on) {
  switch (key) {
    case 'fault':
      _RM.showFault = on;
      if (_RM.lgFault && _RM.map) { on ? _RM.lgFault.addTo(_RM.map) : _RM.map.removeLayer(_RM.lgFault); }
      break;
    case 'faultsym':
      _RM.showFaultSym = on;
      if (_RM.lgFaultSym && _RM.map) { on ? _RM.lgFaultSym.addTo(_RM.map) : _RM.map.removeLayer(_RM.lgFaultSym); }
      break;
    case 'volcano':
      _RM.showVolcano = on;
      if (_RM.lgVol && _RM.map) { on ? _RM.lgVol.addTo(_RM.map) : _RM.map.removeLayer(_RM.lgVol); }
      break;
    case 'station':
      _RM.showStation = on;
      if (_RM.lgSta && _RM.map) { on ? _RM.lgSta.addTo(_RM.map) : _RM.map.removeLayer(_RM.lgSta); }
      break;
    case 'xs':
      _RM.showXS = on;
      [_RM.ewBand, _RM.nsBand, _RM.rxBand, _RM.rxLine, _RM.xhair].forEach(l => {
        if (l && _RM.map) { on ? l.addTo(_RM.map) : _RM.map.removeLayer(l); }
      });
      break;
    case 'slabmap':
      _RM.showSlabMap = on;
      if (!_RM.lgSlabMap && on) _rmBuildSlabMapLayer();
      else if (_RM.lgSlabMap && _RM.map) { on ? _RM.lgSlabMap.addTo(_RM.map) : _RM.map.removeLayer(_RM.lgSlabMap); }
      break;
    case 'slab': _RM.showSlab = on; _rmDrawRx(); _rmDrawNS(); _rmDrawEW(); break;
    case 'topo': _RM.showTopo = on; _rmDrawRx(); _rmDrawNS(); _rmDrawEW(); break;
    case 'slab3d': _RM.showSlab3d = on; if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d(); break;
    case 'vol3d': _RM.showVol3d = on; if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d(); break;
    case 'coast3d': _RM.showCoast3d = on; if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d(); break;
    case 'topo3d': _RM.showTopo3d = on; if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d(); break;
    case 'fault3d': _RM.showFault3d = on; if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d(); break;
    case 'slab2d': _RM.showSlab2d = on; _rmDrawRx(); _rmDrawNS(); _rmDrawEW(); break;
  }
}

// ── Auto Slab2.0 contours for the active AOI ──────────────────────────────────
// Generates depth contours from the official USGS Slab2.0 models covering
// whatever region the current session spans. Feeds both the 2-D cross-sections
// and the 3-D mesh surface. Falls back to the static footage if generation fails.
async function _rmFetchSlab(region) {
  const r = region || {};
  const pad = 0.5;
  if (r.lon_min != null && r.lon_max != null && r.lat_min != null && r.lat_max != null) {
    try {
      const url = `/api/slab?lon_min=${(+r.lon_min) - pad}&lat_min=${(+r.lat_min) - pad}`
                + `&lon_max=${(+r.lon_max) + pad}&lat_max=${(+r.lat_max) + pad}`;
      const d = await (await fetch(url)).json();
      if (d && d.features && d.features.length) return d;
    } catch (_) { }
  }
  // Fallback: historical static footage
  try { return await (await fetch('/footages/Contour_slabs.geojson')).json(); }
  catch (_) { return null; }
}

// ── Slab2 reference seismicity ────────────────────────────────────────────────
async function _rmLoadSlab2() {
  if (_RM.slab2Data !== null) return;
  _RM.slab2Data = [];
  const r = _RM.data?.region || {};
  const pad = 0.5;
  try {
    const url = `/api/slab2/points?lon_min=${(r.lon_min || 119) - pad}&lat_min=${(r.lat_min || -3) - pad}&lon_max=${(r.lon_max || 130) + pad}&lat_max=${(r.lat_max || 5) + pad}`;
    const d = await (await fetch(url)).json();
    if (d.points && d.points.length) {
      _RM.slab2Data = d.points;
      _rmDrawNS(); _rmDrawEW(); _rmDrawRx();
    }
  } catch (_) { }
}

function _rmSlab2NSTrace() {
  if (!_RM.slab2Data || !_RM.slab2Data.length) return null;
  const lonHalf = _RM.xsHalf / (111.32 * Math.cos(_RM.cla * Math.PI / 180));
  const pts = _RM.slab2Data.filter(p => Math.abs(p.lon - _RM.clo) <= lonHalf && p.dep <= _RM.depMax + 80);
  if (!pts.length) return null;
  return {
    type: 'scatter', mode: 'markers',
    x: pts.map(p => p.lat), y: pts.map(p => p.dep),
    text: pts.map(p => `Slab2 ${p.src.toUpperCase()} — D=${p.dep.toFixed(0)} km`),
    hoverinfo: 'text',
    marker: { color: 'rgba(168,85,247,0.35)', size: 3.5, symbol: 'circle' },
    name: 'Slab2 EQ ref', showlegend: false,
  };
}

function _rmSlab2EWTrace() {
  if (!_RM.slab2Data || !_RM.slab2Data.length) return null;
  const latHalf = _RM.xsHalf / 111.32;
  const pts = _RM.slab2Data.filter(p => Math.abs(p.lat - _RM.cla) <= latHalf && p.dep <= _RM.depMax + 80);
  if (!pts.length) return null;
  return {
    type: 'scatter', mode: 'markers',
    x: pts.map(p => p.lon), y: pts.map(p => p.dep),
    text: pts.map(p => `Slab2 ${p.src.toUpperCase()} — D=${p.dep.toFixed(0)} km`),
    hoverinfo: 'text',
    marker: { color: 'rgba(168,85,247,0.35)', size: 3.5, symbol: 'circle' },
    name: 'Slab2 EQ ref', showlegend: false,
  };
}

function _rmSlab2RxTrace(sinA, cosA, cosLat) {
  if (!_RM.slab2Data || !_RM.slab2Data.length) return null;
  const KM = 111.32, half = _RM.xsHalf;
  const pts = _RM.slab2Data
    .filter(p => p.dep <= _RM.depMax + 80)
    .map(p => {
      const dx = (p.lon - _RM.clo) * KM * cosLat, dy = (p.lat - _RM.cla) * KM;
      return { along: dx * sinA + dy * cosA, perp: dx * cosA - dy * sinA, dep: p.dep, src: p.src };
    })
    .filter(p => Math.abs(p.perp) <= half);
  if (!pts.length) return null;
  return {
    type: 'scatter', mode: 'markers',
    x: pts.map(p => p.along), y: pts.map(p => p.dep),
    text: pts.map(p => `Slab2 ${p.src.toUpperCase()} — D=${p.dep.toFixed(0)} km<br>⊥ ${p.perp.toFixed(1)} km`),
    hoverinfo: 'text',
    marker: { color: 'rgba(168,85,247,0.35)', size: 3.5, symbol: 'circle' },
    name: 'Slab2 EQ ref', showlegend: false,
  };
}

// ── Azimuth slider ────────────────────────────────────────────────────────────
let _azDebTimer = null;
function rmAz(el) {
  _RM.az = +el.value;
  document.getElementById('rm-az-v').textContent = _RM.az + '°';
  const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const toDir = d => dirs[Math.round(((d % 360) + 360) % 360 / 22.5) % 16];
  const a = _RM.az, b = (a + 180) % 360;
  const lbl = (a < 5) ? 'XS N–S' : (a > 85 && a < 95) ? 'XS E–W' : `XS ${toDir(b + 180)}–${toDir(a)} (Az ${a}°)`;
  document.getElementById('rm-rx-title').textContent = lbl;
  _rmUpdateBands();
  clearTimeout(_azDebTimer);
  _azDebTimer = setTimeout(() => _rmDrawRx(), 80);
}

// ── Elevation cache + async fetcher ──────────────────────────────────────────
async function _rmFetchTopo(key, lats, lons, redrawFn) {
  if (key in _RM.topoData) return;
  _RM.topoData[key] = null; // mark pending
  try {
    const url = `/api/result/elevation-profile?lats=${lats.map(v => v.toFixed(5)).join(',')}&lons=${lons.map(v => v.toFixed(5)).join(',')}`;
    const d = await (await fetch(url)).json();
    if (d.elevations && d.elevations.length === lats.length) {
      _RM.topoData[key] = { xs: lats, elevs: d.elevations };
      redrawFn();
    }
  } catch (_) { }
}

// Build terrain band from cached elevation (y in km, negative = above sea).
// Elevation is exaggerated ×_RM_TOPO_EXAG and lifted by _RM_TOPO_GAP; the band is
// filled only between the topo surface and the gap baseline (so the 0…-GAP strip
// stays blank as a boundary). Drawn as a closed polygon (surface L→R, baseline
// R→L). Hover (true metres) lives on the surface vertices.
function _rmTopoFillTrace(xs, elevs) {
  const topoY = elevs.map(e => _rmTopoY(e));
  const base = -_RM_TOPO_GAP;
  const px = [...xs, ...xs.slice().reverse()];
  const py = [...topoY, ...xs.map(() => base)];
  const cd = [...elevs.map(e => Math.max(0, e)), ...xs.map(() => null)];
  return {
    type: 'scatter', mode: 'lines',
    fill: 'toself', fillcolor: 'rgba(139,115,85,0.5)',
    x: px, y: py,
    line: { color: '#c8a96e', width: 2 },
    showlegend: false,
    hovertemplate: `Topo %{customdata:.0f} m (×${_RM_TOPO_EXAG})<extra></extra>`,
    customdata: cd,
  };
}

// ── Topo traces for N-S section (x = latitude degrees) ───────────────────────
function _rmTopoNS(latMin, latMax) {
  const KM = 111.32;
  const lonHalf = _RM.xsHalf / (KM * Math.cos(_RM.cla * Math.PI / 180));
  const traces = [];
  // Elevation profile (async, cached)
  const N = 50;
  const lats = Array.from({ length: N }, (_, i) => latMin + i * (latMax - latMin) / (N - 1));
  const lons = Array.from({ length: N }, () => _RM.clo);
  const key = `ns_${latMin.toFixed(3)}_${latMax.toFixed(3)}_${_RM.clo.toFixed(3)}`;
  if (!(key in _RM.topoData)) {
    _rmFetchTopo(key, lats, lons, () => _rmDrawNS());
  }
  const cached = _RM.topoData[key];
  if (cached) traces.push(_rmTopoFillTrace(cached.xs, cached.elevs));
  // Sea-level marker line
  traces.push({
    type: 'scatter', mode: 'lines', x: [latMin, latMax], y: [0, 0],
    line: { color: '#22c55e', width: 1.5 }, showlegend: false, hoverinfo: 'skip'
  });
  // Stations — red triangle (matches map station marker), on the (exaggerated) surface
  if (_RM.showStation) {
    const stas = (_RM.data?.stations || []).filter(s => Math.abs(s.lon - _RM.clo) <= lonHalf * 1.5);
    if (stas.length) traces.push(_rmStaXsTrace(stas.map(s => s.lat), stas, cached));
  }
  // Volcanoes — brown/orange cone (matches map volcano icon), sitting on the surface
  if (_RM.showVolcano) {
    const vols = _RM.volcanoes.filter(v => Math.abs(v.lon - _RM.clo) <= lonHalf * 1.5);
    if (vols.length) traces.push(_rmVolXsTrace(vols.map(v => v.lat), vols, cached));
  }
  return traces;
}

// Station marker trace for a cross-section: red up-triangle perched on the
// exaggerated topographic surface (xVals already in the section's x-coordinate).
function _rmStaXsTrace(xVals, stas, cached) {
  const ys = stas.map((s, i) => {
    const surf = cached ? _rmTopoY(_rmInterp(xVals[i], cached.xs, cached.elevs)) : 0;
    return Math.min(surf, _rmTopoY(s.elev || 0)) - 0.4;
  });
  return {
    type: 'scatter', mode: 'markers',
    x: xVals, y: ys,
    text: stas.map(s => `${s.network}.${s.station} (Elev ${s.elev || 0}m)`), hoverinfo: 'text',
    marker: { color: '#ef4444', size: 10, symbol: 'triangle-up', line: { color: '#fff', width: 1 } },
    showlegend: false
  };
}

// Volcano marker trace for a cross-section: a brown/orange cone (triangle) that
// echoes the map's volcano icon, placed on the exaggerated surface.
function _rmVolXsTrace(xVals, vols, cached) {
  const ys = xVals.map(x => (cached ? _rmTopoY(_rmInterp(x, cached.xs, cached.elevs)) : 0) - 0.6);
  return {
    type: 'scatter', mode: 'markers',
    x: xVals, y: ys,
    text: vols.map(v => v.name), hoverinfo: 'text',
    marker: { color: '#d97706', size: 14, symbol: 'triangle-up', line: { color: '#7c2d12', width: 1.4 } },
    showlegend: false
  };
}

// ── Topo traces for E-W section (x = longitude degrees) ──────────────────────
function _rmTopoEW(lonMin, lonMax) {
  const KM = 111.32;
  const latHalf = _RM.xsHalf / KM;
  const traces = [];
  // Elevation profile (async, cached)
  const N = 50;
  const lons = Array.from({ length: N }, (_, i) => lonMin + i * (lonMax - lonMin) / (N - 1));
  const lats = Array.from({ length: N }, () => _RM.cla);
  const key = `ew_${lonMin.toFixed(3)}_${lonMax.toFixed(3)}_${_RM.cla.toFixed(3)}`;
  if (!(key in _RM.topoData)) {
    _rmFetchTopo(key, lats, lons, () => _rmDrawEW());
  }
  const cached = _RM.topoData[key];
  if (cached) traces.push(_rmTopoFillTrace(lons, cached.elevs));
  traces.push({
    type: 'scatter', mode: 'lines', x: [lonMin, lonMax], y: [0, 0],
    line: { color: '#22c55e', width: 1.5 }, showlegend: false, hoverinfo: 'skip'
  });
  if (_RM.showStation) {
    const stas = (_RM.data?.stations || []).filter(s => Math.abs(s.lat - _RM.cla) <= latHalf * 1.5);
    if (stas.length) traces.push(_rmStaXsTrace(stas.map(s => s.lon), stas, cached));
  }
  if (_RM.showVolcano) {
    const vols = _RM.volcanoes.filter(v => Math.abs(v.lat - _RM.cla) <= latHalf * 1.5);
    if (vols.length) traces.push(_rmVolXsTrace(vols.map(v => v.lon), vols, cached));
  }
  return traces;
}

// ── Topo traces for rotatable XS (x = km along section) ──────────────────────
function _rmTopoRx(azR, sinA, cosA, cosLat, xMin, xMax) {
  const KM = 111.32, half = _RM.xsHalf;
  const traces = [];
  // Elevation profile along rotatable XS (async, cached)
  const N = 50;
  const alphas = Array.from({ length: N }, (_, i) => xMin + i * (xMax - xMin) / (N - 1));
  const rxLats = alphas.map(a => _RM.cla + (a * cosA) / KM);
  const rxLons = alphas.map(a => _RM.clo + (a * sinA) / (KM * cosLat));
  const key = `rx_${_RM.cla.toFixed(3)}_${_RM.clo.toFixed(3)}_${_RM.az}_${xMin.toFixed(0)}_${xMax.toFixed(0)}`;
  if (!(key in _RM.topoData)) {
    _rmFetchTopo(key, rxLats, rxLons, () => _rmDrawRx());
  }
  const cached = _RM.topoData[key];
  if (cached) traces.push(_rmTopoFillTrace(alphas, cached.elevs));
  traces.push({
    type: 'scatter', mode: 'lines', x: [xMin, xMax], y: [0, 0],
    line: { color: '#22c55e', width: 1.5 }, showlegend: false, hoverinfo: 'skip'
  });
  const proj = obj => {
    const dx = (obj.lon - _RM.clo) * KM * cosLat, dy = (obj.lat - _RM.cla) * KM;
    return { along: dx * sinA + dy * cosA, perp: dx * cosA - dy * sinA };
  };
  if (_RM.showStation) {
    const stas = (_RM.data?.stations || []).map(s => ({ ...proj(s), s })).filter(p => Math.abs(p.perp) <= half * 1.5);
    if (stas.length) traces.push({
      type: 'scatter', mode: 'markers',
      x: stas.map(p => p.along),
      y: stas.map(p => (cached ? _rmTopoY(_rmInterp(p.along, cached.xs, cached.elevs)) : 0) - 0.4),
      text: stas.map(p => `${p.s.network}.${p.s.station} (Elev ${p.s.elev || 0}m)<br>⊥ ${p.perp.toFixed(1)} km`), hoverinfo: 'text',
      marker: { color: '#ef4444', size: 10, symbol: 'triangle-up', line: { color: '#fff', width: 1 } },
      showlegend: false
    });
  }
  if (_RM.showVolcano) {
    const vols = _RM.volcanoes.map(v => ({ ...proj(v), v })).filter(p => Math.abs(p.perp) <= half * 1.5);
    if (vols.length) traces.push({
      type: 'scatter', mode: 'markers',
      x: vols.map(p => p.along),
      y: vols.map(p => (cached ? _rmTopoY(_rmInterp(p.along, cached.xs, cached.elevs)) : 0) - 0.6),
      text: vols.map(p => `${p.v.name}<br>⊥ ${p.perp.toFixed(1)} km`), hoverinfo: 'text',
      marker: { color: '#d97706', size: 14, symbol: 'triangle-up', line: { color: '#7c2d12', width: 1.4 } },
      showlegend: false
    });
  }
  return traces;
}

// ── Helper: bin points → smoothed slab profile line ───────────────────────────
// Bins (x,depth) pairs: per xBin, takes minimum depth (= slab top).
// Applies a Gaussian-weighted moving average (window 5) for smoothness.
function _slabProfile(pts, xBin) {
  if (!pts.length) return [[], []];
  const bins = {};
  for (const [x, d] of pts) {
    const k = Math.round(x / xBin) * xBin;
    const kk = k.toFixed(6);
    if (!(kk in bins) || d < bins[kk]) bins[kk] = d;
  }
  const xs = Object.keys(bins).map(Number).sort((a, b) => a - b);
  const ys = xs.map(x => bins[x.toFixed(6)]);
  // Gaussian weights for 5-point window (σ≈1.2)
  const gw = [0.054, 0.242, 0.399, 0.242, 0.054];
  const ysSmooth = ys.map((_, i) => {
    let wsum = 0, vsum = 0;
    for (let j = -2; j <= 2; j++) {
      const ii = i + j;
      if (ii >= 0 && ii < ys.length) { const w = gw[j + 2]; vsum += w * ys[ii]; wsum += w; }
    }
    return wsum > 0 ? vsum / wsum : ys[i];
  });
  return [xs, ysSmooth];
}

// ── Slab on N-S section (x = latitude °) — single interpolated line ──────────
function _rmSlabNS() {
  if (!_RM.slabData) return [];
  const lonHalf = _RM.xsHalf / (111.32 * Math.cos(_RM.cla * Math.PI / 180));
  const pts = [];
  for (const feat of _RM.slabData.features) {
    const dep = -feat.properties.ELEV;
    if (dep > _RM.depMax + 80) continue;
    for (const [lo, la] of feat.geometry.coordinates) {
      if (Math.abs(lo - _RM.clo) <= lonHalf) pts.push([la, dep]);
    }
  }
  const [xs, ys] = _slabProfile(pts, 0.02);
  if (!xs.length) return [];
  return [{
    type: 'scatter', mode: 'lines', x: xs, y: ys,
    line: { color: '#c084fc', width: 2.5, shape: 'spline', smoothing: 1.3 },
    name: 'Slab top', showlegend: false, hovertemplate: 'Slab %{y:.0f} km<extra></extra>'
  }];
}

// ── Slab on E-W section (x = longitude °) — single interpolated line ─────────
function _rmSlabEW() {
  if (!_RM.slabData) return [];
  const latHalf = _RM.xsHalf / 111.32;
  const pts = [];
  for (const feat of _RM.slabData.features) {
    const dep = -feat.properties.ELEV;
    if (dep > _RM.depMax + 80) continue;
    for (const [lo, la] of feat.geometry.coordinates) {
      if (Math.abs(la - _RM.cla) <= latHalf) pts.push([lo, dep]);
    }
  }
  const [xs, ys] = _slabProfile(pts, 0.025);
  if (!xs.length) return [];
  return [{
    type: 'scatter', mode: 'lines', x: xs, y: ys,
    line: { color: '#c084fc', width: 2.5, shape: 'spline', smoothing: 1.3 },
    name: 'Slab top', showlegend: false, hovertemplate: 'Slab %{y:.0f} km<extra></extra>'
  }];
}

// ── Slab on rotatable XS (x = km along) — single interpolated line ───────────
function _rmSlabRx(sinA, cosA, cosLat) {
  if (!_RM.slabData) return [];
  const KM = 111.32, half = _RM.xsHalf;
  const pts = [];
  for (const feat of _RM.slabData.features) {
    const dep = -feat.properties.ELEV;
    if (dep > _RM.depMax + 80) continue;
    for (const [lo, la] of feat.geometry.coordinates) {
      const dx = (lo - _RM.clo) * KM * cosLat, dy = (la - _RM.cla) * KM;
      const along = dx * sinA + dy * cosA;
      const perp = dx * cosA - dy * sinA;
      if (Math.abs(perp) <= half) pts.push([along, dep]);
    }
  }
  const [xs, ys] = _slabProfile(pts, 1.5);
  if (!xs.length) return [];
  return [{
    type: 'scatter', mode: 'lines', x: xs, y: ys,
    line: { color: '#c084fc', width: 2.5, shape: 'spline', smoothing: 1.3 },
    name: 'Slab top', showlegend: false, hovertemplate: 'Slab %{y:.0f} km<extra></extra>'
  }];
}

// ── Rotatable cross-section ───────────────────────────────────────────────────
function _rmDrawRx() {
  const evs = _rmActiveEvents();
  const az = _RM.az;
  const azR = az * Math.PI / 180;
  const sinA = Math.sin(azR), cosA = Math.cos(azR);
  const cosLat = Math.cos(_RM.cla * Math.PI / 180);
  const KM = 111.32, half = _RM.xsHalf;

  // Project and filter events
  const proj = evs.map(e => {
    const dx = (e.lon - _RM.clo) * KM * cosLat, dy = (e.lat - _RM.cla) * KM;
    return { along: dx * sinA + dy * cosA, perp: dx * cosA - dy * sinA, dep: e.dep, mag: e.mag, ev: e };
  }).filter(p => Math.abs(p.perp) <= half);

  const r = _RM.data?.region || {};
  const spanKm = Math.max(
    (r.lon_max - r.lon_min || 0.5) * KM * cosLat,
    (r.lat_max - r.lat_min || 0.5) * KM, 60
  ) * 0.55 + 15;
  const xMin = -spanKm, xMax = spanKm;

  const evTr = {
    type: 'scatter', mode: 'markers',
    x: proj.map(p => p.along), y: proj.map(p => p.dep),
    text: proj.map(p => `<b>${p.ev.event_id}</b><br>D=${p.dep.toFixed(1)}km M=${p.mag != null ? p.mag.toFixed(2) : '–'}<br>⊥ ${p.perp.toFixed(1)} km`),
    hoverinfo: 'text',
    marker: {
      color: proj.map(p => p.dep), colorscale: _RM_PLTCS, cmin: 0, cmax: 400,
      size: proj.map(p => _rmM2r(p.mag)), opacity: 0.9, symbol: 'circle',
      line: { color: 'rgba(255,255,255,0.6)', width: 0.8 },
    },
    customdata: proj.map(p => ({ eid: p.ev.event_id, jid: _RM.activeJob?.job_id })),
    showlegend: false,
  };
  const topoTrs = _RM.showTopo ? _rmTopoRx(azR, sinA, cosA, cosLat, xMin, xMax) : [];
  const slabTrs = (_RM.showSlab && _RM.slabData) ? _rmSlabRx(sinA, cosA, cosLat) : [];
  const slab2Tr = _RM.showSlab2d ? _rmSlab2RxTrace(sinA, cosA, cosLat) : null;
  const allTrs = [...topoTrs, ...slabTrs, ...(slab2Tr ? [slab2Tr] : []), evTr];

  // Axis labels
  const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const toDir = d => dirs[Math.round(((d % 360) + 360) % 360 / 22.5) % 16];
  const xTitle = `${toDir((az + 180) % 360)} ← (km along Az ${az}°) → ${toDir(az)}`;

  const ly = Object.assign({}, _RM_DARK, {
    uirevision: 'rx-fixed',
    margin: { l: 62, r: 16, t: 18, b: 52 },
    xaxis: {
      title: { text: xTitle, font: { size: 11, color: '#a78bfa' } },
      range: [xMin, xMax], gridcolor: '#1e3a5f', gridwidth: 1,
      zerolinecolor: 'rgba(167,139,250,0.25)', zerolinewidth: 1,
      tickfont: { size: 11 }, color: '#94a3b8',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    yaxis: {
      title: { text: 'Depth (km)', font: { size: 11, color: '#60a5fa' } },
      autorange: 'reversed',
      gridcolor: '#1e3a5f', gridwidth: 1,
      zerolinecolor: '#22c55e', zerolinewidth: 2,
      tickfont: { size: 11 }, color: '#94a3b8',
      showline: true, linecolor: '#1e3a5f', mirror: true,
    },
    showlegend: false,
    shapes: [
      {
        type: 'rect', xref: 'paper', x0: 0, x1: 1, y0: -5, y1: 0,
        fillcolor: 'rgba(34,197,94,0.06)', line: { width: 0 }, layer: 'below'
      },
      { type: 'line', x0: xMin, x1: xMax, y0: 0, y1: 0, line: { color: '#22c55e', width: 2 } },
      ...[50, 100, 150, 200, 300, 400, 500].filter(d => d <= _RM.depMax).map(d => ({
        type: 'line', x0: xMin, x1: xMax, y0: d, y1: d,
        line: { color: 'rgba(30,58,95,0.6)', width: 0.8, dash: 'dot' }
      })),
    ],
    annotations: _RM.showTopo ? [{
      x: 0, y: 0, xref: 'paper', yref: 'paper', xanchor: 'left', yanchor: 'bottom',
      text: `Topo ×${_RM_TOPO_EXAG}`, showarrow: false,
      font: { size: 8, color: '#c8a96e' },
      bgcolor: 'rgba(15,23,42,0.7)', borderpad: 2,
    }] : [],
  });

  if (!_RM.rxInit) {
    Plotly.newPlot('rm-rx-plt', allTrs, ly, _RM_CFG); _RM.rxInit = true;
    document.getElementById('rm-rx-plt').on('plotly_click', d => {
      const cd = d.points[0]?.customdata;
      if (cd?.eid) { const ev = _RM.activeJob?.events.find(e => String(e.event_id) === String(cd.eid)); if (ev) _rmShowEventDetail({ ...ev, lat: +ev.lat, lon: +ev.lon, dep: +(ev.depth_km || 0), mag: ev.mag != null ? +ev.mag : null }); }
    });
  } else { Plotly.react('rm-rx-plt', allTrs, ly); }
}

// ── 3D scatter plot ───────────────────────────────────────────────────────────
async function _rmLoadCoast3d() {
  if (_RM.coast3dData !== null) return;
  _RM.coast3dData = [];
  const r = _RM.data?.region || {};
  try {
    const url = `/api/footages/coastlines?lon_min=${r.lon_min || 119}&lat_min=${r.lat_min || -3}&lon_max=${r.lon_max || 130}&lat_max=${r.lat_max || 5}`;
    const d = await (await fetch(url)).json();
    if (d.lines && d.lines.length) {
      _RM.coast3dData = d.lines;
      if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d();
    }
  } catch (_) { }
}

function _rmCoast3dTraces() {
  if (!_RM.coast3dData || !_RM.coast3dData.length) return [];
  // Merge all coastline segments into one scatter3d trace (null = break).
  // z = -0.002 km (2 m above sea level) so the line floats above the topo surface
  // and is not hidden by it even when the surface opaque covers z=0 exactly.
  const xs = [], ys = [], zs = [];
  const zAbove = -0.002 * _rm3dExag();
  for (const seg of _RM.coast3dData) {
    if (!seg.length) continue;
    for (const [lo, la] of seg) { xs.push(lo); ys.push(la); zs.push(zAbove); }
    xs.push(null); ys.push(null); zs.push(null);
  }
  return [{
    type: 'scatter3d', mode: 'lines',
    x: xs, y: ys, z: zs,
    line: { color: '#38bdf8', width: 2 },
    hoverinfo: 'skip',
    name: 'Coastline', showlegend: false,
  }];
}

// ── Topo download panel ───────────────────────────────────────────────────────

const _RM_TOPO_PRESETS = {
  region:    () => { const r = _RM.data?.region || {}; return { lonMin: r.lon_min ?? 127.2, latMin: r.lat_min ?? 0.6, lonMax: r.lon_max ?? 128.0, latMax: r.lat_max ?? 1.6, label: 'Active Region', G: 16 }; },
  jailolo:   () => ({ lonMin: 127.0, latMin:  0.3, lonMax: 128.5, latMax:  2.0, label: 'Jailolo',        G: 20 }),
  halmahera: () => ({ lonMin: 126.5, latMin: -1.5, lonMax: 129.5, latMax:  3.5, label: 'Halmahera',      G: 24 }),
  maluku:    () => ({ lonMin: 124.0, latMin: -9.0, lonMax: 136.0, latMax:  4.0, label: 'Maluku',          G: 32 }),
  indonesia: () => ({ lonMin:  95.0, latMin:-11.0, lonMax: 141.0, latMax:  6.0, label: 'Indonesia',       G: 46 }),
};

function rmTopoPanel() {
  const p = document.getElementById('rm-topo-dl');
  if (!p) return;
  if (p.style.display === 'none') {
    p.style.display = '';
    rmTopoPreset('region');
    _rmTopoLoadCacheList();
  } else {
    p.style.display = 'none';
  }
}

function rmTopoPreset(name) {
  const p = _RM_TOPO_PRESETS[name]?.();
  if (!p) return;
  document.getElementById('rm-dl-lonmin').value = p.lonMin.toFixed(1);
  document.getElementById('rm-dl-latmin').value = p.latMin.toFixed(1);
  document.getElementById('rm-dl-lonmax').value = p.lonMax.toFixed(1);
  document.getElementById('rm-dl-latmax').value = p.latMax.toFixed(1);
  document.getElementById('rm-dl-label').value  = p.label;
  document.getElementById('rm-dl-G').value      = p.G;
}

async function rmTopoDownload() {
  const lonMin = parseFloat(document.getElementById('rm-dl-lonmin').value);
  const latMin = parseFloat(document.getElementById('rm-dl-latmin').value);
  const lonMax = parseFloat(document.getElementById('rm-dl-lonmax').value);
  const latMax = parseFloat(document.getElementById('rm-dl-latmax').value);
  const G      = parseInt(document.getElementById('rm-dl-G').value) || 32;
  const label  = document.getElementById('rm-dl-label').value || 'custom';
  const st     = document.getElementById('rm-topo-dl-status');
  if ([lonMin, latMin, lonMax, latMax].some(isNaN)) {
    st.innerHTML = '<span style="color:#f87171">Fill in all bbox coordinates.</span>'; return;
  }
  const pts = G * G, chunks = Math.ceil(pts / 100), estSec = chunks * 1.1;
  st.innerHTML = `<span style="color:#fbbf24">⏳ Downloading ${pts} points (~${estSec.toFixed(0)}s)…</span>`;
  document.getElementById('rm-dl-btn').disabled = true;
  try {
    const r = await fetch('/api/topo/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeader() },
      body: JSON.stringify({ lon_min: lonMin, lat_min: latMin, lon_max: lonMax, lat_max: latMax, G, label }),
    });
    const d = await r.json();
    if (d.status === 'ok') {
      st.innerHTML = `<span style="color:#4ade80">✓ Saved — ${d.points} points, peak ${d.peak_m} m (${d.filename})</span>`;
      _RM.topo3dData = null;   // force reload next 3D open
    } else if (d.status === 'already_cached') {
      st.innerHTML = `<span style="color:#a3e635">✓ Already saved (${d.filename})</span>`;
    } else {
      st.innerHTML = `<span style="color:#f87171">Failed: ${d.error || JSON.stringify(d)}</span>`;
    }
    _rmTopoLoadCacheList();
  } catch (e) {
    st.innerHTML = `<span style="color:#f87171">Error: ${e.message}</span>`;
  } finally {
    document.getElementById('rm-dl-btn').disabled = false;
  }
}

async function _rmTopoLoadCacheList() {
  const el = document.getElementById('rm-topo-cache-list');
  if (!el) return;
  try {
    const d = await (await fetch('/api/topo/list')).json();
    if (!d.length) { el.innerHTML = '<span style="color:#475569;font-size:.7rem">No cache yet.</span>'; return; }
    el.innerHTML = d.map(c => {
      const bb = `${c.lon_min}–${c.lon_max}, ${c.lat_min}–${c.lat_max}`;
      return `<div style="display:flex;align-items:center;gap:4px;padding:3px 0;border-bottom:1px solid #1e2a40">
        <span style="flex:1;color:#cbd5e1;font-size:.7rem">${c.label || c.key} · ${bb} · G=${c.G} · ${c.size_kb}KB</span>
        <button onclick="_rmTopoDelete('${c.key}')" title="Delete" style="background:none;border:none;color:#f87171;cursor:pointer;font-size:.85rem;padding:0 2px">×</button>
      </div>`;
    }).join('');
  } catch (e) {
    if (el) el.innerHTML = `<span style="color:#f87171;font-size:.7rem">Error: ${e.message}</span>`;
  }
}

async function _rmTopoDelete(key) {
  if (!confirm(`Delete cache "${key}"?`)) return;
  const st = document.getElementById('rm-topo-dl-status');
  try {
    const r = await fetch('/api/topo/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeader() },
      body: JSON.stringify({ key }),
    });
    const d = await r.json();
    if (st) st.innerHTML = d.status === 'deleted'
      ? `<span style="color:#4ade80">✓ Cache deleted.</span>`
      : `<span style="color:#f87171">${d.error}</span>`;
    _rmTopoLoadCacheList();
    _RM.topo3dData = null;
  } catch (e) {
    if (st) st.innerHTML = `<span style="color:#f87171">${e.message}</span>`;
  }
}

// Helper: auth header for local API calls (auth disabled by default in LAN mode)
function _authHeader() { return {}; }

// ── Longitude span of the region (for sizing 3D glyphs relative to the view).
function _rm3dSpan() {
  const r = _RM.data?.region || {};
  const span = (r.lon_max ?? 128) - (r.lon_min ?? 127);
  return Math.max(0.2, span);
}

// Build a single mesh3d trace of upward-pointing pyramids (one per point) — a
// triangle/cone glyph that survives 3D rotation (scatter3d has no triangle
// symbol). Base half-width dDeg (lon/lat degrees), apex height hKm above the
// surface (negative z = up). `octa` uses an 8-sided base for a rounder, more
// volcano-like cone. A companion scatter3d gives hover labels at each apex.
function _rmPyramidMesh(pts, dDeg, hKm, color, name, labels, octa) {
  if (!pts || !pts.length) return [];
  const X = [], Y = [], Z = [], I = [], J = [], K = [];
  const nb = octa ? 8 : 4;                       // base vertices
  const ring = Array.from({ length: nb }, (_, k) => {
    const a = (k / nb) * 2 * Math.PI;
    return [Math.cos(a), Math.sin(a)];
  });
  pts.forEach(([lo, la, bz]) => {
    const z0 = bz || 0;                          // base sits on the topo surface
    const base = X.length;
    ring.forEach(([cx, cy]) => { X.push(lo + cx * dDeg); Y.push(la + cy * dDeg); Z.push(z0); });
    X.push(lo); Y.push(la); Z.push(z0 - hKm);    // apex (up from the base)
    const apex = base + nb;
    for (let k = 0; k < nb; k++) {               // side faces
      I.push(base + k); J.push(base + (k + 1) % nb); K.push(apex);
    }
  });
  const lblX = [], lblY = [], lblZ = [], lblT = [];
  pts.forEach((p, i) => { lblX.push(p[0]); lblY.push(p[1]); lblZ.push((p[2] || 0) - hKm); lblT.push((labels && labels[i]) || ''); });
  return [
    {
      type: 'mesh3d', x: X, y: Y, z: Z, i: I, j: J, k: K,
      color, opacity: 0.95, flatshading: true, hoverinfo: 'skip',
      lighting: { diffuse: 0.7, specular: 0.15, ambient: 0.55 },
      name, showlegend: false,
    },
    {
      type: 'scatter3d', mode: 'markers', x: lblX, y: lblY, z: lblZ,
      text: lblT, hoverinfo: 'text',
      marker: { size: 2.5, color, opacity: 0.6 },
      name, showlegend: false,
    },
  ];
}

// Fetch a fine elevation grid over the region → cache for 3D.
// The grid is 16×16 = 256 points (resolves Mt Jailolo's narrow summit that the
// old coarse 10×10 grid stepped over). Requests are chunked CLIENT-SIDE to ≤100
// points/request so they pass opentopodata's per-request cap on the current
// server without needing a backend restart.
async function _rmLoad3dTopo() {
  if (_RM.topo3dData !== null) return;
  _RM.topo3dData = undefined;   // mark pending (distinct from null = not started)
  const r = _RM.data?.region || {};
  const lo0 = (r.lon_min ?? 127.2), lo1 = (r.lon_max ?? 128.0);
  const la0 = (r.lat_min ?? 0.6),   la1 = (r.lat_max ?? 1.6);
  const G = 16;
  try {
    const url = `/api/topo/grid?lon_min=${lo0}&lat_min=${la0}&lon_max=${lo1}&lat_max=${la1}&G=${G}`;
    const d = await (await fetch(url)).json();
    if (!d.lons || !d.lats || !d.elev) { _RM.topo3dData = null; return; }
    // Convert meters → km; store raw (exaggeration applied at draw time)
    const z = d.elev.map(row => row.map(v => Math.max(0, v) / 1000));
    let peak = 0;
    for (const row of z) for (const v of row) if (v > peak) peak = v;
    _RM.topo3dData = { lons: d.lons, lats: d.lats, elev: z, peak, source: d.source };
    if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d();
  } catch (_) { _RM.topo3dData = null; }
}

// Vertical exaggeration for 3D topo. The summit is targeted at ~3% of the depth
// axis (clearly visible alongside the slab) but is GUARANTEED at least 1% — even
// for a tall peak or a shallow depth axis, so the relief never collapses flat.
// summit_height / depMax == frac because summit = peak·exag = (frac·depMax/peak)·peak.
function _rm3dExag() {
  const peak = (_RM.topo3dData && _RM.topo3dData.peak) || 1;
  const ex    = 0.03 * _RM.depMax / peak;   // target summit ≈ 3% of depth axis
  const exMin = 0.01 * _RM.depMax / peak;   // floor: summit ≥ 1% of depth axis
  return Math.max(exMin, Math.min(40, ex));
}

// Topography as a Plotly surface (earthy colorscale), z in km (negative = up).
function _rmTopo3dTrace() {
  const t = _RM.topo3dData;
  if (!t || !t.elev) return [];
  const ex = _rm3dExag();
  const z  = t.elev.map(row => row.map(e => -e * ex));
  return [{
    type: 'surface', x: t.lons, y: t.lats, z,
    colorscale: [[0, '#1e3a5f'], [0.5, '#3f6212'], [0.8, '#a16207'], [1, '#e7e5e4']],
    reversescale: false, showscale: false, opacity: 0.5,
    hovertemplate: 'Topo %{customdata:.0f} m<extra></extra>',
    customdata: t.elev.map(row => row.map(e => Math.round(e * 1000))),
    lighting: { ambient: 0.8, diffuse: 0.5 },
    name: 'Topography', showlegend: false,
  }];
}

// Bilinear sample of the loaded 3D topo grid → surface z (km, negative=up) at a
// lon/lat, so stations and volcano cones sit ON the relief instead of being buried
// under it or floating at sea level. Returns 0 when the grid is not loaded yet.
function _rmTopo3dAt(lon, lat) {
  const t = _RM.topo3dData;
  if (!t || !t.elev) return 0;
  const { lons, lats, elev } = t;
  const nx = lons.length, ny = lats.length;
  const fx = (lon - lons[0]) / ((lons[nx - 1] - lons[0]) || 1) * (nx - 1);
  const fy = (lat - lats[0]) / ((lats[ny - 1] - lats[0]) || 1) * (ny - 1);
  const i0 = Math.max(0, Math.min(nx - 2, Math.floor(fx)));
  const j0 = Math.max(0, Math.min(ny - 2, Math.floor(fy)));
  const tx = Math.max(0, Math.min(1, fx - i0)), ty = Math.max(0, Math.min(1, fy - j0));
  const e00 = elev[j0][i0], e10 = elev[j0][i0 + 1], e01 = elev[j0 + 1][i0], e11 = elev[j0 + 1][i0 + 1];
  const e = (e00 * (1 - tx) + e10 * tx) * (1 - ty) + (e01 * (1 - tx) + e11 * tx) * ty;
  return -e * _rm3dExag();
}

function _rmDraw3d() {
  const evs = _rmActiveEvents();
  const plotDiv = document.getElementById('rm-3d-plt');
  if (!plotDiv) return;

  // Load coastlines on first 3D open (async, redraws when done)
  if (_RM.showCoast3d && _RM.coast3dData === null) _rmLoadCoast3d();
  // Load fault overlays lazily (shared cache with benchmark/hypo 3D maps)
  if (_RM.showFault3d && SW.mapOverlays === null) {
    SW.ensureMapOverlays().then(() => {
      if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) _rmDraw3d();
    });
  }

  const evTr = {
    type: 'scatter3d', mode: 'markers',
    x: evs.map(e => e.lon), y: evs.map(e => e.lat), z: evs.map(e => e.dep),
    text: evs.map(e => `<b>${e.event_id}</b><br>M=${e.mag != null ? e.mag.toFixed(2) : '–'}<br>D=${e.dep.toFixed(1)} km`),
    hoverinfo: 'text',
    marker: {
      color: evs.map(e => e.dep), colorscale: _RM_PLTCS, cmin: 0, cmax: Math.min(400, _RM.depMax),
      size: evs.map(e => Math.max(2.5, _rmM2r(e.mag) * 0.55)), opacity: 0.88,
      line: { color: 'rgba(255,255,255,0.3)', width: 0.5 },
    },
    name: 'Earthquakes', showlegend: false,
  };

  // Stations as small red BOXES sitting on the relief, with tiny name labels.
  // Volcanoes stay as orange cones (echoing the map icon) but smaller — both
  // anchored onto the topo surface so they ride the terrain, not float/bury.
  const dLon = _rm3dSpan();
  const onSurf = (lon, lat) => (_RM.showTopo3d ? _rmTopo3dAt(lon, lat) : 0);
  const stas = (_RM.showStation && _RM.data?.stations) ? _RM.data.stations : [];
  const staTrs = stas.length ? [{
    type: 'scatter3d', mode: 'markers+text',
    x: stas.map(s => s.lon), y: stas.map(s => s.lat),
    z: stas.map(s => onSurf(s.lon, s.lat) - 0.3),
    text: stas.map(s => `${s.network}.${s.station}`),
    textposition: 'top center', textfont: { size: 7, color: '#fca5a5' },
    hoverinfo: 'text',
    marker: { symbol: 'square', size: 4, color: '#ef4444',
              line: { color: '#fff', width: 0.5 } },
    name: 'Stations', showlegend: false,
  }] : [];

  const vols = (_RM.showVol3d) ? _RM.volcanoes : [];
  const volTrs = _rmPyramidMesh(
    vols.map(v => [v.lon, v.lat, onSurf(v.lon, v.lat)]),
    dLon * 0.007, Math.max(0.6, _RM.depMax * 0.006),
    '#d97706', 'Volcanoes', vols.map(v => v.name), true);

  // Topography surface (exaggerated relief) — load grid lazily on first 3D open.
  if (_RM.showTopo3d && _RM.topo3dData === null) _rmLoad3dTopo();
  const topoTrs = (_RM.showTopo3d && _RM.topo3dData) ? _rmTopo3dTrace() : [];

  const slabTrs = _RM.showSlab3d ? _rmSlab3dTraces() : [];
  const coastTrs = _RM.showCoast3d ? _rmCoast3dTraces() : [];
  // Fault traces (sesar & subduction): shared SW.mapOverlays cache, clipped to region
  const _fr = _RM.data?.region || {};
  const _fBounds = {
    W: (_fr.lon_min ?? 127) - 0.5, E: (_fr.lon_max ?? 128) + 0.5,
    S: (_fr.lat_min ?? 0.5) - 0.5, N: (_fr.lat_max ?? 1.5) + 0.5,
  };
  const faultTrs3d = (_RM.showFault3d && SW.mapOverlays) ? SW.faultTraces(_fBounds, '3d') : [];
  // coastTrs last → WebGL renders it on top of the topo surface
  const allTrs = [...topoTrs, evTr, ...staTrs, ...volTrs, ...slabTrs, ...faultTrs3d, ...coastTrs];
  _RM._3dEvIdx = topoTrs.length;  // track earthquake trace index for restyle during play

  const r = _RM.data?.region || {};
  // View bbox = REGION only + small symmetric margin, STABLE (does not follow slab extent).
  // Previously the bbox expanded with the slab (extending by degrees) → giant plane
  // made events shrink & the box FLAT/asymmetric, plus each slab toggle changed
  // bbox → aspect/range changed → view "reset". Now bbox fixed → any toggle
  // preserves camera (uirevision rm3d), depth proportional. Slab clipped to
  // region in 3D (2D NS/EW/Rx cross-sections still show the full slab).
  let loMin = r.lon_min ?? 127, loMax = r.lon_max ?? 128;
  let laMin = r.lat_min ?? 0.5, laMax = r.lat_max ?? 1.5;
  const mLon = (loMax - loMin) * 0.08, mLat = (laMax - laMin) * 0.08;
  loMin -= mLon; loMax += mLon; laMin -= mLat; laMax += mLat;
  const layout3d = {
    paper_bgcolor: '#0a0f1e',
    font: { color: '#94a3b8', size: 9, family: 'Segoe UI,Arial,sans-serif' },
    margin: { l: 0, r: 0, t: 0, b: 0 },
    scene: {
      bgcolor: '#0a0f1e',
      // Constant uirevision → Plotly preserves the user's orbit/zoom/pan across
      // every Plotly.react redraw (depth slider, layer toggle, async topo/coast
      // load); without it each redraw snapped the camera back ("restart").
      uirevision: 'rm3d',
      // Lock lon/lat range to the view bbox (SAME as used by _geoAspect3d).
      // Without this, Plotly auto-range follows coastlines that extend ±1° beyond
      // the region → degrees-per-unit scale x≠y → topo/mountains appear shifted
      // relative to the coastline. With locked range, lon & lat scale uniformly.
      xaxis: { title: { text: 'Longitude', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#1e3a5f', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, range: [loMin, loMax] },
      yaxis: { title: { text: 'Latitude', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#1e3a5f', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, range: [laMin, laMax] },
      zaxis: { title: { text: 'Depth (km)', font: { size: 14, color: '#60a5fa' } }, gridcolor: '#1e3a5f', zerolinecolor: '#22c55e', color: '#cbd5e1', tickfont: { size: 13, color: '#cbd5e1' }, autorange: 'reversed' },
      camera: { eye: { x: 1.5, y: -1.5, z: 0.8 }, center: { x: 0, y: 0, z: 0 } },
      aspectmode: 'manual',
      aspectratio: SW.geoAspect3d(loMin, loMax, laMin, laMax, _RM.depMax || 100),
    },
    showlegend: false,
  };

  // Lazy-load overlay: keep it up while any enabled layer is still streaming in;
  // hide it once the plot has rendered and nothing is pending.
  const _pending = _rm3dPendingLayers();
  if (_pending.length) _rm3dSetLoading(true, `Loading layer: ${_pending.join(', ')}…`);
  const _settleLoading = () => { if (!_rm3dPendingLayers().length) _rm3dSetLoading(false); };

  if (!_RM.init3d) {
    // WebGL may be unavailable (e.g. native GUI with compositing forced off) —
    // newPlot() can then throw synchronously or its promise can simply never
    // resolve. Either way don't mark init3d/bind listeners on a dead plot; the
    // rmOpen3d() watchdog shows an error if this leaves the overlay hanging.
    let _p;
    try {
      _p = Plotly.newPlot('rm-3d-plt', allTrs, layout3d, {
        responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ['resetCameraLastSave3d'],
        toImageButtonOptions: { filename: 'seiswork_3d', scale: 2 }
      });
    } catch (err) {
      clearTimeout(_RM._3dWatchdog);
      _rm3dShowError('Gagal merender 3D: ' + (err && err.message ? err.message : 'WebGL tidak tersedia'));
      return;
    }
    _p.then(() => {
      clearTimeout(_RM._3dWatchdog);
      _RM.init3d = true;
      // Track live camera so restyle/react can restore it after user orbit/zoom
      plotDiv.on('plotly_relayout', data => {
        if (data['scene.camera']) _RM._3dCamera = data['scene.camera'];
      });
      SW.add3DCompass('rm-3d-plt', plotDiv.parentElement, true, 'br');   // bind once on new gd
      _settleLoading();
    }).catch(err => {
      clearTimeout(_RM._3dWatchdog);
      _rm3dShowError('Gagal merender 3D: ' + (err && err.message ? err.message : 'WebGL tidak tersedia'));
    });
  } else {
    // Redraw (layer toggle / depth slider) must not snap the view back.
    // uirevision:'rm3d' (in layout.scene) already makes Plotly preserve the live
    // camera (result of pan/orbit/zoom) across react calls; we also copy the
    // camera as a fallback. aspectratio is NOT copied back so that depth slider
    // changes still update the box proportions.
    const liveScene = (plotDiv.layout && plotDiv.layout.scene)
      || (plotDiv._fullLayout && plotDiv._fullLayout.scene);
    if (liveScene && liveScene.camera) layout3d.scene.camera = liveScene.camera;
    Plotly.react('rm-3d-plt', allTrs, layout3d).then(_settleLoading);
    SW.add3DCompass('rm-3d-plt', plotDiv.parentElement, false, 'br');  // update direction only
  }
}

// ── Slab surface in 3D (interpolated surface) ────────────────────────────────
// The raw Slab2 contours are sparse poly-lines. We resample the scattered
// (lon,lat,depth) control points onto a regular NG×NG lon-lat grid via
// inverse-distance weighting, then render as a Plotly `surface` trace (not
// mesh3d+delaunay which produces jagged triangles and poor contour rendering).
// Grid cells farther than a search radius from any control point remain null,
// creating natural holes at the slab boundary instead of extrapolating.
// Depth-contour lines every 25 km give clear structure cues (similar to the
// Yellowstone slab cross-section convention used in USGS publications).
function _rmSlab3dTraces() {
  if (!_RM.slabData) return [];
  const pLo = [], pLa = [], pDep = [];
  for (const feat of _RM.slabData.features) {
    const dep = -feat.properties.ELEV;
    if (dep > _RM.depMax + 80) continue;
    for (const [lo, la] of feat.geometry.coordinates) {
      pLo.push(lo); pLa.push(la); pDep.push(dep);
    }
  }
  const n = pLo.length;
  if (n < 4) return [];

  const loMin = Math.min(...pLo), loMax = Math.max(...pLo);
  const laMin = Math.min(...pLa), laMax = Math.max(...pLa);
  const NG   = 56;                                        // denser grid → smoother surface
  const dLo  = (loMax - loMin) / (NG - 1) || 1e-6;
  const dLa  = (laMax - laMin) / (NG - 1) || 1e-6;
  const maxd = Math.hypot(dLo, dLa) * 2.4;              // mask radius (in degrees)
  const maxd2 = maxd * maxd;

  // Build NG×NG regular grid of IDW-interpolated depths (null = outside footprint)
  const gridLons = Array.from({length: NG}, (_, j) => loMin + j * dLo);
  const gridLats = Array.from({length: NG}, (_, i) => laMin + i * dLa);
  const gridZ    = Array.from({length: NG}, () => new Array(NG).fill(null));

  for (let i = 0; i < NG; i++) {
    const la = gridLats[i];
    for (let j = 0; j < NG; j++) {
      const lo = gridLons[j];
      let wsum = 0, zsum = 0, near2 = Infinity, exact = -1;
      for (let k = 0; k < n; k++) {
        const dx = lo - pLo[k], dy = la - pLa[k];
        const d2 = dx * dx + dy * dy;
        if (d2 < near2) near2 = d2;
        if (d2 < 1e-10) { exact = k; break; }
        const w = 1 / d2;                                // IDW power-2
        wsum += w; zsum += w * pDep[k];
      }
      if (near2 <= maxd2)
        gridZ[i][j] = exact >= 0 ? pDep[exact] : zsum / wsum;
    }
  }

  const hasData = gridZ.some(row => row.some(v => v !== null));
  if (!hasData) return [];

  const depMax = Math.min(400, _RM.depMax);
  const depMin = Math.min(...pDep);
  const cSize  = depMax <= 60 ? 10 : depMax <= 150 ? 25 : 50;

  return [{
    type: 'surface',
    x: gridLons, y: gridLats, z: gridZ,
    colorscale: _RM_PLTCS, cmin: 0, cmax: depMax,
    showscale: false,
    opacity: 0.65,
    contours: {
      z: { show: true, start: Math.floor(depMin / cSize) * cSize, end: depMax + cSize,
           size: cSize, color: 'rgba(255,255,255,0.45)', width: 1.5,
           usecolormap: false, highlightcolor: 'white' }
    },
    lighting: { diffuse: 0.55, specular: 0.12, roughness: 0.8, ambient: 0.7 },
    name: 'Slab', showlegend: false,
    hovertemplate: 'Slab %{z:.0f} km<extra></extra>',
  }];
}

// ── 3D camera presets ─────────────────────────────────────────────────────────
function _rm3dCam(mode) {
  const cams = {
    '3d': { eye: { x: 1.5, y: -1.5, z: 0.8 } },
    'top': { eye: { x: 0.001, y: 0.001, z: 2.5 } },
    'ns': { eye: { x: 0, y: -2.5, z: 0.4 } },
    'ew': { eye: { x: 2.5, y: 0, z: 0.4 } },
  };
  try { Plotly.relayout('rm-3d-plt', { 'scene.camera': cams[mode] }); } catch (_) { }
}

// Keyboard shortcut — Esc priority: waveform → 3D → result modal
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!document.getElementById('sw-changelog-ovl').classList.contains('hidden')) { SW.closeChangelog(); return; }
  const ofv = document.getElementById('ofv-bd');
  if (ofv && !ofv.classList.contains('hidden')) { closeFileViewer(); return; }
  if (!document.getElementById('rm-wv-bd').classList.contains('hidden')) { RMWV.closeModal(); return; }
  if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) { rmClose3d(); return; }
  if (!document.getElementById('tomo-3d-bd').classList.contains('hidden')) { TOMO.close3d(); return; }
  if (document.getElementById('mech-epi-modal').style.display !== 'none') { MECH.closeEpiDetail(); return; }
  if (!document.getElementById('mech-station-bd').classList.contains('hidden')) { MECH.closeStationModal(); return; }
  if (!document.getElementById('mech-3d-bd').classList.contains('hidden')) { MECH.close3d(); return; }
  if (!document.getElementById('rm-bd').classList.contains('hidden')) closeResultModal();
});

// Resize handler: keep Plotly panels and Leaflet responsive
window.addEventListener('resize', () => {
  if (!document.getElementById('rm-wv-bd').classList.contains('hidden')) {
    try { Plotly.Plots.resize('rm-wv-plot'); } catch (_) { }
  }
  if (!document.getElementById('rm-3d-bd').classList.contains('hidden')) {
    try { Plotly.Plots.resize('rm-3d-plt'); } catch (_) { }
  }
  if (!document.getElementById('tomo-3d-bd').classList.contains('hidden')) {
    try { Plotly.Plots.resize('tomo-3d-plt'); } catch (_) { }
  }
  if (document.getElementById('rm-bd').classList.contains('hidden')) return;
  if (_RM.map) _RM.map.invalidateSize();
  ['rm-ns-plt', 'rm-ew-plt', 'rm-mag-plt', 'rm-rx-plt', 'rm-rms-plt', 'rm-fmd-plt'].forEach(id => {
    try { Plotly.Plots.resize(id); } catch (_) { }
  });
});

// Velocity Model — Upload + Canvas Plot → modules/velest.js (VEL)

// ═══════════════════════════════════════════
//  Internet / server health badge (bottom-right)
// ═══════════════════════════════════════════
(function _initNetBadge() {
  async function _ping() {
    const dot = document.getElementById('sw-net-dot');
    const lbl = document.getElementById('sw-net-lbl');
    if (!dot || !lbl) return;
    if (!navigator.onLine) {
      dot.style.background = '#ef4444'; lbl.textContent = 'Offline'; return;
    }
    const t0 = performance.now();
    try {
      const ctrl = new AbortController();
      const tid  = setTimeout(() => ctrl.abort(), 5000);
      await fetch('/api/health', { signal: ctrl.signal, cache: 'no-store' });
      clearTimeout(tid);
      const ms = Math.round(performance.now() - t0);
      dot.style.background = ms < 150 ? '#22c55e' : ms < 600 ? '#f59e0b' : '#f97316';
      lbl.textContent = `${ms}ms`;
    } catch (e) {
      if (e.name === 'AbortError') {
        dot.style.background = '#f97316'; lbl.textContent = 'Timeout';
      } else {
        dot.style.background = '#ef4444'; lbl.textContent = 'Error';
      }
    }
  }
  window.addEventListener('online',  _ping);
  window.addEventListener('offline', () => {
    const d = document.getElementById('sw-net-dot');
    const l = document.getElementById('sw-net-lbl');
    if (d) d.style.background = '#ef4444';
    if (l) l.textContent = 'Offline';
  });
  setTimeout(_ping, 1800);
  setInterval(_ping, 15000);
})();

// ═══════════════════════════════════════════
//  Reconnect overlay — covers the last-good UI (instead of a blank white
//  navigation error) while the local server is unreachable, e.g. mid-restart
//  after a code edit, or a crash before the service watchdog brings it back.
// ═══════════════════════════════════════════
SW.reconnectShow = function (msg) {
  const ovl = document.getElementById('sw-reconnect-ovl');
  if (!ovl) return;
  ovl.classList.remove('hidden');
  const m = document.getElementById('sw-reconnect-msg');
  if (m && msg) m.textContent = msg;
};
SW.reconnectHide = function () {
  const ovl = document.getElementById('sw-reconnect-ovl');
  if (ovl) ovl.classList.add('hidden');
};
// Poll /api/health until the (possibly new) server answers, THEN reload — never
// navigate straight into a connection-refused blank page.
SW.reloadWhenHealthy = function () {
  SW.reconnectShow('Server updated — reconnecting…');
  let tries = 0;
  const poll = () => {
    tries++;
    fetch('/api/health', { cache: 'no-store' })
      .then(r => { if (r.ok) location.reload(); else setTimeout(poll, 800); })
      .catch(() => {
        if (tries === 3) SW.reconnectShow('Waiting for the SeisWork server to come back online…');
        setTimeout(poll, 800);
      });
  };
  poll();
};

// ═══════════════════════════════════════════
//  Changelog modal — reads CHANGELOG.json via /api/changelog
// ═══════════════════════════════════════════
let _changelogLoaded = false;
SW.openChangelog = function () {
  const ovl = document.getElementById('sw-changelog-ovl');
  if (!ovl) return;
  ovl.classList.remove('hidden');
  if (_changelogLoaded) return;
  fetch('/api/changelog', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => {
      _changelogLoaded = true;
      const body = document.getElementById('sw-changelog-body');
      const entries = (d && d.entries) || [];
      const disclaimer = d && d.disclaimer
        ? `<div class="sw-changelog-disclaimer"><i class="bi bi-exclamation-triangle-fill"></i> ${d.disclaimer}</div>`
        : '';
      if (!entries.length) { body.innerHTML = disclaimer + 'No changelog entries yet.'; return; }
      body.innerHTML = disclaimer + entries.map(e => `
        <div class="sw-changelog-entry">
          <div class="sw-changelog-ver">
            v${e.version || '?'}
            ${d.current === e.version ? '<span class="sw-changelog-current">current</span>' : ''}
            <span class="sw-changelog-date">${e.date || ''}</span>
          </div>
          <ul class="sw-changelog-list">
            ${(e.changes || []).map(c => `<li>${c}</li>`).join('')}
          </ul>
        </div>`).join('');
    })
    .catch(() => {
      document.getElementById('sw-changelog-body').textContent = 'Failed to load changelog.';
    });
};
SW.closeChangelog = function () {
  const ovl = document.getElementById('sw-changelog-ovl');
  if (ovl) ovl.classList.add('hidden');
};

// ═══════════════════════════════════════════
//  Sidebar toggle + boot
// ═══════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  const tog = document.getElementById('sidebar-toggle');
  const sb = document.getElementById('sidebar');
  tog.addEventListener('click', () => {
    const hidden = sb.classList.toggle('hidden');
    tog.classList.toggle('collapsed', hidden);
    tog.style.right = hidden ? '6px' : `calc(var(--sidebar-w) + 6px)`;
    setTimeout(() => SW.map.invalidateSize(), 260);
  });
  initMap();
  setPickBgMode(false);
  // Prime the QL jobs cache early so it is ready when the Result View opens
  refreshQlJobs().catch(() => {});
  // Restore the Online dashboard + history when the server still has a live session
  // (a browser refresh does not stop the seedlink session on the server).
  if (typeof OM !== 'undefined') {
    // Sync GaMMA-only param visibility with the engine dropdown's default value —
    // otherwise the GaMMA params stay visible (their default HTML state) until
    // realtime detection starts once, even when REAL is selected.
    OM.onBackendChange?.();
    OM.restoreIfLive().catch(() => {});
  }
});


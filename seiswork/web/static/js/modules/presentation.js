// ─────────────────────────────────────────────────────────────────────────────
//  PRESENTATION BUILDER (WYSIWYG) — by HakimBMKG
//  Module: presentation.js  (extracted from seiswork.js)
//  Depends on: _wpCfgId — global var from seiswork.js
// ─────────────────────────────────────────────────────────────────────────────

/* ══════════════════════════════════════════════════════════════════════
   MODEL — state, data transformasi, helpers
   ══════════════════════════════════════════════════════════════════════ */

const _RATIO_DIMS = { '16:9':[1280,720], '4:3':[1024,768], 'A4':[1123,794] };

const _PE = {
  slides      : [],
  slideIdx    : 0,
  selElId     : null,
  scale       : 0.6,
  drag        : null,
  dirty       : false,
  catJobs     : [],
  catSummaries: {},
  mapInst     : {},
  plotlyEls   : {},
  catData     : null,
};

function _peDims() {
  const ratio = document.getElementById('pres-meta-ratio')?.value || '16:9';
  return _RATIO_DIMS[ratio] || [1280, 720];
}
function _peW() { return _peDims()[0]; }
function _peH() { return _peDims()[1]; }

function _peFindEl(elId) {
  return (_PE.slides[_PE.slideIdx]?.elements || []).find(e => e.id === elId) || null;
}

function _peGetEvs(el) {
  if (!_PE.catData) return [];
  const jobs = _PE.catData.jobs || [];
  if (el.job_id) return jobs.find(j => j.job_id === el.job_id)?.events || [];
  return jobs.reduce((b, j) => (j.events?.length || 0) > b.length ? j.events : b, []);
}

function _peJobSummary(job) {
  const evs  = job.events || [];
  const mags = evs.map(e => +(e.mag ?? NaN)).filter(v => !isNaN(v));
  const deps = evs.map(e => +(e.depth_km ?? NaN)).filter(v => !isNaN(v));
  const dts  = evs.map(e => e.datetime || '').filter(Boolean).sort();
  const lats = evs.map(e => +e.lat).filter(v => !isNaN(v));
  const lons = evs.map(e => +e.lon).filter(v => !isNaN(v));
  const avg  = a => a.length ? a.reduce((s, v) => s + v, 0) / a.length : null;
  const mn   = a => a.length ? Math.min(...a) : null;
  const mx   = a => a.length ? Math.max(...a) : null;
  return {
    n      : evs.length,
    magMin : mn(mags), magMax: mx(mags), magAvg: avg(mags),
    depMin : mn(deps), depMax: mx(deps), depAvg: avg(deps),
    latMin : mn(lats), latMax: mx(lats),
    lonMin : mn(lons), lonMax: mx(lons),
    dtFrom : dts[0]?.slice(0, 10)             || null,
    dtTo   : dts[dts.length - 1]?.slice(0, 10) || null,
    gaps   : evs.map(e => +(e.gap ?? NaN)).filter(v => !isNaN(v)),
    rmss   : evs.map(e => +(e.rms ?? NaN)).filter(v => !isNaN(v)),
  };
}

function _peMigrateSlide(sl) {
  if (sl.elements) return sl;
  const W = 1280, H = 720, uid = () => `el-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  const els = [];
  const hdr = (txt, y = 20, fs = 30) => ({
    id: uid(), type: 'text', x: 40, y, w: W - 80, h: fs * 2.2,
    content: txt, fontSize: fs, fontWeight: '700', color: '#e2e8f0', textAlign: 'left', lineHeight: 1.3,
  });
  switch (sl.type) {
    case 'title':
      els.push({ id: uid(), type: 'text', x: 64, y: 210, w: W - 128, h: 160,
        content: sl.title || 'Title', fontSize: 52, fontWeight: '800', color: '#e2e8f0', textAlign: 'center', lineHeight: 1.2 });
      if (sl.subtitle) els.push({ id: uid(), type: 'text', x: 64, y: 390, w: W - 128, h: 80,
        content: sl.subtitle, fontSize: 22, fontWeight: '400', color: '#94a3b8', textAlign: 'center', lineHeight: 1.4 });
      if (sl.author) els.push({ id: uid(), type: 'text', x: 64, y: 620, w: W - 128, h: 50,
        content: sl.author, fontSize: 16, fontWeight: '400', color: '#445060', textAlign: 'center', lineHeight: 1.4 });
      break;
    case 'text':
      els.push(hdr(sl.title || 'Title', 22, 28));
      els.push({ id: uid(), type: 'text', x: 48, y: 90, w: W - 96, h: H - 120,
        content: sl.body || '', fontSize: 20, fontWeight: '400', color: '#e2e8f0', textAlign: 'left', lineHeight: 1.8 });
      break;
    case 'map_2d':
      els.push(hdr(sl.title || '2D Map', 18, 28));
      els.push({ id: uid(), type: 'map_2d', x: 20, y: 80, w: W - 40, h: H - 100, job_id: sl.job_id || '', zoom: sl.zoom || 7 });
      break;
    case 'map_3d':
      els.push(hdr(sl.title || '3D Map', 18, 28));
      els.push({ id: uid(), type: 'map_3d', x: 20, y: 80, w: W - 40, h: H - 100, job_id: sl.job_id || '' });
      break;
    case 'histogram':
      els.push(hdr(sl.title || 'Histogram', 18, 28));
      els.push({ id: uid(), type: 'histogram', x: 20, y: 80, w: W - 40, h: H - 100, job_id: sl.job_id || '', field: sl.field || 'magnitude' });
      break;
    case 'stat_table':
      els.push(hdr(sl.title || 'Statistics', 18, 28));
      els.push({ id: uid(), type: 'stat_table', x: 20, y: 80, w: W - 40, h: H - 100, job_id: sl.job_id || '' });
      break;
    case 'split':
      els.push(hdr(sl.title || 'Split', 18, 28));
      els.push({ id: uid(), type: sl.left?.type || 'map_2d',    x: 20,  y: 80, w: 610, h: H - 100, job_id: sl.job_id || '' });
      els.push({ id: uid(), type: sl.right?.type || 'histogram', x: 650, y: 80, w: 610, h: H - 100, job_id: sl.job_id || '', field: sl.right?.field || 'magnitude' });
      break;
    default:
      els.push(hdr(sl.title || 'Slide', 300, 36));
  }
  return { id: sl.id || `s-${Date.now()}`, bg: '', elements: els };
}

const _PE_STAT_FIELDS = [
  { id: 'total',       label: 'Total Events' },
  { id: 'mag_range',   label: 'Magnitude Range' },
  { id: 'mag_avg',     label: 'Average Magnitude' },
  { id: 'depth_range', label: 'Depth Range' },
  { id: 'depth_avg',   label: 'Average Depth' },
  { id: 'period',      label: 'Time Period' },
  { id: 'area',        label: 'Geographic Area (lat/lon)' },
  { id: 'gap_avg',     label: 'Average Gap' },
  { id: 'rms_avg',     label: 'Average RMS' },
];
const _PE_STAT_DEFAULT = ['total', 'mag_range', 'mag_avg', 'depth_range', 'depth_avg', 'period', 'area'];

/* ══════════════════════════════════════════════════════════════════════
   VIEW — render functions, DOM helpers
   ══════════════════════════════════════════════════════════════════════ */

function peShowEmptyCanvas() {
  const c = document.getElementById('pe-canvas');
  if (c) {
    const [W, H] = _peDims();
    c.style.width  = Math.round(W * _PE.scale) + 'px';
    c.style.height = Math.round(H * _PE.scale) + 'px';
    c.innerHTML = `<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.6rem;color:#2a3a52;font-size:.85rem;pointer-events:none">
      <i class="bi bi-easel2" style="font-size:2.5rem;opacity:.3"></i>
      <div>Add a slide to start</div></div>`;
  }
  _peHideProps();
}

function peRenderSlideList() {
  const list = document.getElementById('pe-slide-list');
  if (!list) return;
  if (!_PE.slides.length) {
    list.innerHTML = '<div class="pe-slide-empty">No slides yet.<br>Click <b>+ Add</b></div>';
    return;
  }
  list.innerHTML = _PE.slides.map((sl, i) => {
    const isAct  = i === _PE.slideIdx;
    const elTypes = (sl.elements || []).map(e => e.type).join(',');
    const typeTag = elTypes.includes('map_2d') ? '🗺' : elTypes.includes('map_3d') ? '🌐'
                  : elTypes.includes('histogram') ? '📊' : elTypes.includes('stat_table') ? '📋'
                  : elTypes.includes('text') ? 'T' : '?';
    const title = (sl.elements || []).find(e => e.type === 'text')?.content?.slice(0, 30) || `Slide ${i + 1}`;
    return `<div class="pe-slide-thumb${isAct ? ' active' : ''}" onclick="peEditSlide(${i})" title="${_pEsc(title)}">
      <div class="pe-thumb-canvas">
        <div class="pe-thumb-inner" style="background:${sl.bg || '#0d1320'}">
          <span class="pe-thumb-icon">${typeTag}</span>
          <span class="pe-thumb-txt">${_pEsc(title.slice(0, 18))}</span>
        </div>
        <span class="pe-thumb-num">${i + 1}</span>
      </div>
      <div class="pe-thumb-meta">
        <div class="pe-thumb-label">${_pEsc(title.slice(0, 22))}</div>
        <div class="pe-thumb-actions">
          <button onclick="event.stopPropagation();peMoveSlide(${i},-1)" title="Move up">↑</button>
          <button onclick="event.stopPropagation();peMoveSlide(${i},+1)" title="Move down">↓</button>
          <button onclick="event.stopPropagation();peDupSlide(${i})" title="Duplicate">⧉</button>
          <button onclick="event.stopPropagation();peDelSlide(${i})" title="Delete">✕</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

function peRenderCanvas() {
  const sl = _PE.slides[_PE.slideIdx];
  if (!sl) { peShowEmptyCanvas(); return; }
  const [W, H] = _peDims();
  const s = _PE.scale;
  const canvas = document.getElementById('pe-canvas');
  if (!canvas) return;
  canvas.style.width  = Math.round(W * s) + 'px';
  canvas.style.height = Math.round(H * s) + 'px';
  canvas.style.background = sl.bg || '#0d1320';
  canvas.innerHTML = '';
  (sl.elements || []).forEach(el => peRenderEl(canvas, el, s));
  peFitScale();
  document.getElementById('pe-zoom-lbl').textContent = Math.round(_PE.scale * 100) + '%';
}

function peRenderEl(canvas, el, s) {
  const div = document.createElement('div');
  div.className    = 'pe-el';
  div.id           = `peel-${el.id}`;
  div.dataset.eid  = el.id;
  div.style.left   = Math.round(el.x * s) + 'px';
  div.style.top    = Math.round(el.y * s) + 'px';
  div.style.width  = Math.round(el.w * s) + 'px';
  div.style.height = Math.round(el.h * s) + 'px';
  if (_PE.selElId === el.id) div.classList.add('selected');

  if (el.type === 'text') {
    div.classList.add('pe-el-text');
    const inner = document.createElement('div');
    inner.className = 'pe-el-text-inner';
    inner.style.cssText = `font-size:${Math.round(el.fontSize * s)}px;font-weight:${el.fontWeight || '400'};color:${el.color || '#e2e8f0'};text-align:${el.textAlign || 'left'};line-height:${el.lineHeight || 1.6};white-space:pre-wrap;word-break:break-word;padding:${Math.round(4 * s)}px`;
    inner.textContent = el.content || '';
    div.appendChild(inner);
    div.addEventListener('dblclick', e => { e.stopPropagation(); peInlineEdit(el.id); });
  } else if (el.type === 'shape') {
    div.classList.add('pe-el-shape');
    div.style.background   = el.fill        || 'rgba(59,130,246,.15)';
    div.style.border       = `${Math.max(1, Math.round((el.borderW || 2) * s))}px solid ${el.borderColor || '#3b82f6'}`;
    div.style.borderRadius = Math.round((el.radius || 0) * s) + 'px';
  } else {
    div.classList.add('pe-el-data');
    peRenderDataEl(div, el, s);
  }

  div.addEventListener('mousedown', e => { e.stopPropagation(); peElDown(e, el.id); });
  canvas.appendChild(div);
}

function peRenderDataEl(div, el, s) {
  div.innerHTML = '';
  div.style.overflow   = 'hidden';
  div.style.background = '#0b1420';
  div.style.borderRadius = '2px';

  if (el.type === 'stat_table') {
    div.innerHTML = _peStatTableHTML(el);
    return;
  }

  const job = _PE.catData?.jobs?.find(j => j.job_id === el.job_id)
    || (_PE.catJobs.reduce((b, j) => ((j.n_events || 0) > (b?.n_events || 0) ? j : b), null));
  const sm  = job ? (_PE.catSummaries[job.job_id] || _peJobSummary(job)) : null;
  const evs = _peGetEvs(el);

  const step  = job ? (job.step || '?') + (job.method ? '/' + job.method : '') : '–';
  const nev   = job ? (job.n_events || 0) : evs.length;
  const magTx = sm?.magMin != null ? `M${sm.magMin.toFixed(1)}–M${sm.magMax.toFixed(1)}` : '';
  const depTx = sm?.depMin != null ? `D${sm.depMin.toFixed(0)}–${sm.depMax.toFixed(0)} km` : '';
  const W = Math.round(el.w * s), H = Math.round(el.h * s);
  const icon = { map_2d: '🗺', map_3d: '🌐', histogram: '📊' }[el.type] || '📊';
  const fs   = Math.max(8, Math.round(12 * s));
  const fi   = Math.max(14, Math.round(32 * s));

  let svgContent = '';
  if (el.type === 'map_2d'   && evs.length) svgContent = _peMiniScatter(evs, W, H, s);
  if (el.type === 'histogram' && evs.length) svgContent = _peMiniHist(evs, el.field, W, H, s);
  if (el.type === 'map_3d'   && evs.length) svgContent = _peMini3D(evs, W, H, s);

  div.innerHTML = `
    <div style="position:absolute;inset:0;overflow:hidden">${svgContent}</div>
    <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:${Math.round(4 * s)}px;pointer-events:none">
      ${!svgContent ? `<span style="font-size:${fi}px;opacity:.3">${icon}</span>` : ''}
      <div style="background:rgba(10,16,28,.78);border:1px solid #1e2d40;border-radius:4px;padding:${Math.round(3 * s)}px ${Math.round(8 * s)}px;text-align:center;max-width:90%">
        <div style="font-size:${fs}px;color:#60a5fa;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">[${step}]</div>
        <div style="font-size:${Math.max(7, Math.round(10 * s))}px;color:#94a3b8">${nev} event${magTx ? ' · ' + magTx : ''}</div>
        ${depTx ? `<div style="font-size:${Math.max(6, Math.round(9 * s))}px;color:#445060">${depTx}</div>` : ''}
      </div>
    </div>`;
}

function _peMiniScatter(evs, W, H, s) {
  if (!evs.length) return '';
  const lats = evs.map(e => +e.lat), lons = evs.map(e => +e.lon);
  const latMin = Math.min(...lats), latMax = Math.max(...lats), latR = latMax - latMin || 1;
  const lonMin = Math.min(...lons), lonMax = Math.max(...lons), lonR = lonMax - lonMin || 1;
  const pad = Math.round(8 * s);
  const toX = lon => pad + (lon - lonMin) / lonR * (W - 2 * pad);
  const toY = lat => pad + (latMax - lat) / latR * (H - 2 * pad);
  const step = Math.max(1, Math.ceil(evs.length / 300));
  const circles = evs.filter((_, i) => i % step === 0).map(e => {
    const m = +(e.mag ?? 2), d = +(e.depth_km ?? 10);
    const r = Math.max(1, Math.min(4, 1 + m * 0.6));
    return `<circle cx="${toX(+e.lon).toFixed(1)}" cy="${toY(+e.lat).toFixed(1)}" r="${r}" fill="${_depCol(d)}" opacity=".65"/>`;
  }).join('');
  return `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="position:absolute;inset:0">${circles}</svg>`;
}

function _peMiniHist(evs, field, W, H, s) {
  const isMag = (field || 'magnitude') === 'magnitude';
  const vals = isMag
    ? evs.map(e => +(e.mag ?? 0)).filter(v => !isNaN(v) && v > -5)
    : evs.map(e => +(e.depth_km ?? 0)).filter(v => !isNaN(v));
  if (!vals.length) return '';
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const BINS = 16, binW = (mx - mn || 1) / BINS;
  const counts = Array(BINS).fill(0);
  vals.forEach(v => { const i = Math.min(BINS - 1, Math.floor((v - mn) / binW)); counts[i]++; });
  const maxC = Math.max(...counts) || 1;
  const bW = Math.max(2, (W - 16) / BINS), pad = 8;
  const bars = counts.map((c, i) => {
    const bH = Math.round((c / maxC) * (H - 24));
    const x = pad + i * bW, y = H - 12 - bH;
    const fill = isMag ? '#3b82f6' : _depCol(mn + i * binW);
    return `<rect x="${x.toFixed(1)}" y="${y}" width="${(bW - 1).toFixed(1)}" height="${bH}" fill="${fill}" opacity=".8"/>`;
  }).join('');
  return `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="position:absolute;inset:0">${bars}</svg>`;
}

function _peMini3D(evs, W, H, s) {
  if (!evs.length) return '';
  const lons = evs.map(e => +e.lon), deps = evs.map(e => +(e.depth_km ?? 0));
  const lonMin = Math.min(...lons), lonR = Math.max(...lons) - lonMin || 1;
  const depMax = Math.max(...deps) || 1;
  const pad  = Math.round(8 * s);
  const step = Math.max(1, Math.ceil(evs.length / 300));
  const circles = evs.filter((_, i) => i % step === 0).map(e => {
    const m = +(e.mag ?? 2), d = +(e.depth_km ?? 0);
    const x = pad + (+e.lon - lonMin) / lonR * (W - 2 * pad);
    const y = pad + (d / depMax) * (H - 2 * pad);
    const r = Math.max(1, Math.min(4, 1 + m * 0.6));
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" fill="${_depCol(d)}" opacity=".6"/>`;
  }).join('');
  return `<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" style="position:absolute;inset:0">${circles}</svg>`;
}

function _depCol(d) {
  if (d <= 15) return '#22c55e'; if (d <= 40) return '#3b82f6';
  if (d <= 70) return '#a855f7'; return '#ef4444';
}

function _peStatTableHTML(el) {
  const evs = _peGetEvs(el);
  if (!evs || !evs.length) return '<div style="height:100%;display:flex;align-items:center;justify-content:center;color:#2a3a52;font-size:12px">No data</div>';
  const fields = el.stat_fields || _PE_STAT_DEFAULT;
  const job    = _PE.catData?.jobs?.find(j => j.job_id === el.job_id);
  const s      = job ? _peJobSummary(job) : null;
  const mags = evs.map(e => +(e.mag ?? NaN)).filter(v => !isNaN(v));
  const deps = evs.map(e => +(e.depth_km ?? e.depth ?? NaN)).filter(v => !isNaN(v));
  const dts  = evs.map(e => e.datetime || '').filter(Boolean).sort();
  const lats = evs.map(e => +e.lat).filter(v => !isNaN(v));
  const lons = evs.map(e => +e.lon).filter(v => !isNaN(v));
  const gaps = evs.map(e => +(e.gap ?? NaN)).filter(v => !isNaN(v));
  const rmss = evs.map(e => +(e.rms ?? NaN)).filter(v => !isNaN(v));
  const avg  = a => a.length ? (a.reduce((x, v) => x + v, 0) / a.length) : null;
  const mn   = a => a.length ? Math.min(...a) : null;
  const mx   = a => a.length ? Math.max(...a) : null;
  const f1   = v => v != null ? v.toFixed(1) : '–';
  const f2   = v => v != null ? v.toFixed(2) : '–';
  const f0   = v => v != null ? v.toFixed(0) : '–';
  const allRows = {
    total       : ['Total Events',        evs.length.toLocaleString()],
    mag_range   : ['Magnitude Range',     mags.length ? `M${f1(mn(mags))} – M${f1(mx(mags))}` : '–'],
    mag_avg     : ['Average Magnitude',   mags.length ? `M${f2(avg(mags))}` : '–'],
    depth_range : ['Depth Range',         deps.length ? `${f0(mn(deps))} – ${f0(mx(deps))} km` : '–'],
    depth_avg   : ['Average Depth',       deps.length ? `${f1(avg(deps))} km` : '–'],
    period      : ['Time Period',          dts.length ? `${dts[0].slice(0, 10)} – ${dts[dts.length - 1].slice(0, 10)}` : '–'],
    area        : ['Geographic Area',      lats.length ? `${f2(mn(lats))}°–${f2(mx(lats))}°N, ${f2(mn(lons))}°–${f2(mx(lons))}°E` : '–'],
    gap_avg     : ['Average Gap',          gaps.length ? `${f0(avg(gaps))}°` : '–'],
    rms_avg     : ['Average RMS',          rmss.length ? `${f2(avg(rmss))} s` : '–'],
  };
  const rows   = fields.filter(f => allRows[f]).map(f => allRows[f]);
  const step   = job ? (job.step || '?') + (job.method ? '/' + job.method : '') : 'auto';
  const srcLbl = `Source: <b>[${step}]</b> · ${evs.length} event${s && s.dtFrom ? ` · ${s.dtFrom}–${s.dtTo}` : ''}`;
  return `<div style="padding:6px;height:100%;overflow:auto;font-size:10px">
    <div style="color:#445060;margin-bottom:4px;padding:3px 6px;background:#0c1620;border-radius:3px">${srcLbl}</div>
    <table style="border-collapse:collapse;width:100%;font-size:11px">
      <thead><tr>
        <th style="background:#0e1928;color:#60a5fa;padding:5px 8px;border:1px solid #1e2d40;text-align:left">Parameter</th>
        <th style="background:#0e1928;color:#60a5fa;padding:5px 8px;border:1px solid #1e2d40;text-align:right">Value</th>
      </tr></thead>
      <tbody>${rows.map((r, i) => `<tr style="background:${i % 2 ? '#0c1620' : ''}">
        <td style="padding:4px 8px;border:1px solid #1e2d40;color:#e2e8f0">${r[0]}</td>
        <td style="padding:4px 8px;border:1px solid #1e2d40;color:#60a5fa;text-align:right;font-weight:600">${r[1]}</td>
      </tr>`).join('')}</tbody>
    </table>
  </div>`;
}

function _peStatFieldsHTML(el) {
  const active = el.stat_fields || _PE_STAT_DEFAULT;
  const items = _PE_STAT_FIELDS.map(f =>
    `<label class="pe-stat-check">
      <input type="checkbox" value="${f.id}"${active.includes(f.id) ? ' checked' : ''}
             onchange="peApplyStatFields('${el.id}')"/>
      <span>${f.label}</span>
    </label>`
  ).join('');
  return `<div class="pe-prop-row" style="margin-top:.3rem">
    <label>Displayed Statistics</label>
    <div class="pe-stat-fields">${items}</div>
  </div>`;
}

function _peJobPickerHTML(el, elId) {
  if (!_PE.catJobs.length) {
    return `<div class="pe-prop-row">
      <label>Catalog Data Source</label>
      <div class="pe-job-loading">
        <div class="pe-cat-loading">⏳ Loading catalog list…</div>
        <button class="pe-refresh-job-btn" onclick="_peReloadJobSel('${elId}')">↻ Refresh</button>
      </div></div>`;
  }
  const cards = _PE.catJobs.map(j => {
    const s      = _PE.catSummaries[j.job_id] || _peJobSummary(j);
    const isSel  = el.job_id === j.job_id;
    const step   = j.step || '?';
    const meth   = j.method || j.mode || '';
    const label  = step + (meth ? ' / ' + meth : '');
    const id8    = (j.job_id || '').slice(0, 8);
    const f1 = v => v != null ? v.toFixed(1) : '–';
    const f0 = v => v != null ? v.toFixed(0) : '–';
    const magStr   = s.magMin != null ? `M${f1(s.magMin)} – M${f1(s.magMax)} avg M${f1(s.magAvg)}` : 'Mag: N/A';
    const depStr   = s.depMin != null ? `D ${f0(s.depMin)} – ${f0(s.depMax)} km` : 'Dep: N/A';
    const dtStr    = s.dtFrom ? `${s.dtFrom} → ${s.dtTo || '?'}` : '';
    const filtered = j.filtered ? ' 🔽 filtered' : '';
    return `<label class="pe-job-card${isSel ? ' selected' : ''}">
      <input type="radio" name="pejob-${elId}" value="${j.job_id}"${isSel ? ' checked' : ''}
             onchange="peSelectJob('${elId}','${j.job_id}')"/>
      <div class="pe-job-card-body">
        <div class="pe-job-card-hdr">
          <span class="pe-job-label">${_pEsc(label)}${filtered}</span>
          <span class="pe-job-n">${s.n} ev</span>
        </div>
        <div class="pe-job-card-stats">${magStr}</div>
        <div class="pe-job-card-stats">${depStr}</div>
        ${dtStr ? `<div class="pe-job-card-dt">${dtStr}</div>` : ''}
        <div class="pe-job-card-id">${id8}</div>
      </div>
    </label>`;
  }).join('');
  const autoSel  = !el.job_id;
  const autoCard = `<label class="pe-job-card${autoSel ? ' selected' : ''}">
    <input type="radio" name="pejob-${elId}" value=""${autoSel ? ' checked' : ''}
           onchange="peSelectJob('${elId}','')"/>
    <div class="pe-job-card-body">
      <div class="pe-job-card-hdr"><span class="pe-job-label">Auto (most events)</span></div>
      <div class="pe-job-card-stats" style="color:#445060">Automatically selects the job with the most events</div>
    </div>
  </label>`;
  return `<div class="pe-prop-row">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.3rem">
      <label style="margin:0">Catalog Data Source</label>
      <button class="pe-refresh-job-btn" onclick="_peReloadJobSel('${elId}')" title="Refresh">↻</button>
    </div>
    <div class="pe-job-list">${autoCard}${cards}</div>
  </div>`;
}

function _peShowProps(elId) {
  const el = _peFindEl(elId);
  if (!el) return;
  const body  = document.getElementById('pe-props-body');
  const empty = document.getElementById('pe-props-empty');
  if (!body || !empty) return;
  empty.style.display = 'none';
  body.style.display  = '';

  let extra = '';
  if (el.type === 'text') {
    extra = `
      <div class="pe-prop-row"><label>Content</label>
        <textarea id="pp-content" rows="4" oninput="peApplyProp()">${_pEsc(el.content || '')}</textarea></div>
      <div class="pe-prop-row"><label>Font Size (px)</label>
        <input type="number" id="pp-fs" value="${el.fontSize || 20}" min="6" max="200" oninput="peApplyProp()"/></div>
      <div class="pe-prop-row"><label>Font Weight</label>
        <select id="pp-fw" onchange="peApplyProp()">
          <option value="400"${el.fontWeight === '400' ? ' selected' : ''}>Normal</option>
          <option value="600"${el.fontWeight === '600' ? ' selected' : ''}>Semi-Bold</option>
          <option value="700"${el.fontWeight === '700' ? ' selected' : ''}>Bold</option>
          <option value="800"${el.fontWeight === '800' ? ' selected' : ''}>Extra-Bold</option>
        </select></div>
      <div class="pe-prop-row"><label>Color</label>
        <div style="display:flex;gap:.3rem;align-items:center">
          <input type="color" id="pp-color" value="${el.color || '#e2e8f0'}" oninput="peApplyProp()"/>
          <input class="sw-input" id="pp-colorhex" value="${el.color || '#e2e8f0'}" style="width:80px;font-size:.65rem" oninput="document.getElementById('pp-color').value=this.value;peApplyProp()"/>
        </div></div>
      <div class="pe-prop-row"><label>Align</label>
        <div class="pe-align-btns">
          <button onclick="peSetAlign('left')"   class="${el.textAlign === 'left'   ? 'act' : ''}">⬛ L</button>
          <button onclick="peSetAlign('center')" class="${el.textAlign === 'center' ? 'act' : ''}">⬛ C</button>
          <button onclick="peSetAlign('right')"  class="${el.textAlign === 'right'  ? 'act' : ''}">⬛ R</button>
        </div></div>
      <div class="pe-prop-row"><label>Line Height</label>
        <input type="number" id="pp-lh" value="${el.lineHeight || 1.6}" step="0.1" min="0.8" max="3" oninput="peApplyProp()"/></div>`;
  } else if (el.type === 'shape') {
    extra = `
      <div class="pe-prop-row"><label>Fill</label>
        <input type="color" id="pp-fill" value="${el.fill || '#1e3a5f'}" oninput="peApplyProp()"/></div>
      <div class="pe-prop-row"><label>Border Color</label>
        <input type="color" id="pp-bcolor" value="${el.borderColor || '#3b82f6'}" oninput="peApplyProp()"/></div>
      <div class="pe-prop-row"><label>Border Width</label>
        <input type="number" id="pp-bw" value="${el.borderW || 2}" min="0" max="20" oninput="peApplyProp()"/></div>
      <div class="pe-prop-row"><label>Radius</label>
        <input type="number" id="pp-rad" value="${el.radius || 0}" min="0" max="360" oninput="peApplyProp()"/></div>`;
  } else {
    extra = _peJobPickerHTML(el, elId);
    if (el.type === 'histogram') extra += `
      <div class="pe-prop-row"><label>Field Histogram</label>
        <div class="pe-hfield-btns">
          <button onclick="peSetHistField('${elId}','magnitude')" class="${(el.field || 'magnitude') === 'magnitude' ? 'act' : ''}">📊 Magnitude</button>
          <button onclick="peSetHistField('${elId}','depth')"     class="${el.field === 'depth' ? 'act' : ''}">📊 Depth</button>
        </div></div>`;
    if (el.type === 'map_2d') extra += `
      <div class="pe-prop-row"><label>Initial Zoom</label>
        <input type="number" id="pp-zoom" value="${el.zoom || 7}" min="1" max="18" oninput="peApplyZoom('${elId}')"/></div>`;
    if (el.type === 'stat_table') extra += _peStatFieldsHTML(el);
  }

  body.innerHTML = `
    <div class="pe-props-hdr">
      <span><i class="bi ${_peTypeIcon(el.type)}"></i> ${_peTypeLabel(el.type)}</span>
      <button class="pe-del-btn" onclick="peDelEl('${elId}')" title="Delete element">✕</button>
    </div>
    <div class="pe-prop-row pe-pos-row">
      <label>X</label><input type="number" id="pp-x" value="${el.x}" oninput="peApplyPos()"/>
      <label>Y</label><input type="number" id="pp-y" value="${el.y}" oninput="peApplyPos()"/>
      <label>W</label><input type="number" id="pp-w" value="${el.w}" oninput="peApplyPos()"/>
      <label>H</label><input type="number" id="pp-h" value="${el.h}" oninput="peApplyPos()"/>
    </div>
    ${extra}`;
}

function _peHideProps() {
  const body  = document.getElementById('pe-props-body');
  const empty = document.getElementById('pe-props-empty');
  if (body)  body.style.display  = 'none';
  if (empty) empty.style.display = '';
}

function _peUpdatePosInputs(el) {
  const set = (id, v) => { const inp = document.getElementById(id); if (inp) inp.value = v; };
  set('pp-x', el.x); set('pp-y', el.y); set('pp-w', el.w); set('pp-h', el.h);
}

function _peApplyCanvasScale() {
  const s = _PE.scale;
  const [W, H] = _peDims();
  const canvas = document.getElementById('pe-canvas');
  if (!canvas) return;
  canvas.style.width  = Math.round(W * s) + 'px';
  canvas.style.height = Math.round(H * s) + 'px';
  document.getElementById('pe-zoom-lbl').textContent = Math.round(s * 100) + '%';
  const sl = _PE.slides[_PE.slideIdx];
  (sl?.elements || []).forEach(el => {
    const div = document.getElementById(`peel-${el.id}`);
    if (!div) return;
    div.style.left   = Math.round(el.x * s) + 'px';
    div.style.top    = Math.round(el.y * s) + 'px';
    div.style.width  = Math.round(el.w * s) + 'px';
    div.style.height = Math.round(el.h * s) + 'px';
    if (el.type === 'text') {
      const inner = div.querySelector('.pe-el-text-inner');
      if (inner) { inner.style.fontSize = Math.round(el.fontSize * s) + 'px'; inner.style.padding = Math.round(4 * s) + 'px'; }
    } else if (!['shape'].includes(el.type)) {
      peRenderDataEl(div, el, s);
    }
  });
}

function _peAddHandles(div, elId) {
  const wrap = document.createElement('div');
  wrap.className = 'pe-handle-set';
  ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw'].forEach(h => {
    const hd = document.createElement('div');
    hd.className = `pe-handle pe-handle-${h}`;
    hd.addEventListener('mousedown', e => { e.stopPropagation(); e.preventDefault(); peHandleDown(e, elId, h); });
    wrap.appendChild(hd);
  });
  div.appendChild(wrap);
}

function _peTypeIcon(t) {
  return { text: 'bi-type', map_2d: 'bi-map', map_3d: 'bi-box', histogram: 'bi-bar-chart-fill', stat_table: 'bi-table', shape: 'bi-square' }[t] || 'bi-file-slides';
}
function _peTypeLabel(t) {
  return { text: 'Text', map_2d: '2D Map', map_3d: '3D Map', histogram: 'Histogram', stat_table: 'Statistics Table', shape: 'Shape' }[t] || t;
}
function _pEsc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* Plotly loader — for future use */
let _peplotlyP = null;
function _ensurePlotlyFn() {
  if (window.Plotly) return Promise.resolve();
  if (_peplotlyP) return _peplotlyP;
  _peplotlyP = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = '/static/js/vendor/plotly-2.35.2.min.js';
    s.onload = res; s.onerror = rej; document.head.appendChild(s);
  });
  return _peplotlyP;
}

/* ══════════════════════════════════════════════════════════════════════
   CONTROLLER — event handlers, actions, side effects
   ══════════════════════════════════════════════════════════════════════ */

function presBuilderLoad() {
  if (!_wpCfgId) return;
  fetch(`/api/present/${_wpCfgId}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      _PE.slides = (data.slides || []).map(_peMigrateSlide);
      document.getElementById('pres-meta-title').value  = data.title  || '';
      document.getElementById('pres-meta-author').value = data.author || '';
      const rEl = document.getElementById('pres-meta-ratio');
      if (rEl && data.ratio) rEl.value = data.ratio;
      _PE.dirty    = false;
      _PE.slideIdx = 0;
      _PE.selElId  = null;
      peRenderSlideList();
      if (_PE.slides.length) peEditSlide(0);
      else peShowEmptyCanvas();
    }).catch(() => {});
  fetch(`/api/result/${_wpCfgId}/catalog`)
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;
      _PE.catJobs = d.jobs || [];
      _PE.catData = d;
      _PE.catSummaries = {};
      _PE.catJobs.forEach(j => { _PE.catSummaries[j.job_id] = _peJobSummary(j); });
      if (_PE.selElId) {
        const cur = _peFindEl(_PE.selElId);
        if (cur && !['text', 'shape'].includes(cur.type)) _peShowProps(_PE.selElId);
      }
    }).catch(() => {});
}

async function presSave() {
  if (!_wpCfgId) { alert('Select a project first.'); return; }
  const btn = document.getElementById('pres-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    await fetch(`/api/present/${_wpCfgId}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title : document.getElementById('pres-meta-title')?.value.trim()  || 'SeisWork Presentation',
        author: document.getElementById('pres-meta-author')?.value.trim() || '',
        ratio : document.getElementById('pres-meta-ratio')?.value || '16:9',
        slides: _PE.slides,
      }),
    });
    _PE.dirty = false;
    if (btn) { btn.innerHTML = '<i class="bi bi-check-lg"></i> Saved'; setTimeout(() => { btn.innerHTML = '<i class="bi bi-floppy"></i> Save'; btn.disabled = false; }, 1400); return; }
  } catch (e) { alert('Failed to save: ' + e.message); }
  if (btn) { btn.innerHTML = '<i class="bi bi-floppy"></i> Save'; btn.disabled = false; }
}

function presOpenViewer() {
  if (!_wpCfgId) { alert('Select a project first.'); return; }
  const ratio = document.getElementById('pres-meta-ratio')?.value || '16:9';
  const open  = () => window.open(`/present/${_wpCfgId}?ratio=${encodeURIComponent(ratio)}`, '_blank');
  if (_PE.dirty) presSave().then(open); else open();
}

function peEditSlide(idx) {
  _PE.slideIdx = idx;
  _PE.selElId  = null;
  _peClearMaps();
  peRenderSlideList();
  peRenderCanvas();
  _peHideProps();
}

function _peClearMaps() {
  _PE.mapInst   = {};
  _PE.plotlyEls = {};
}

function peAddSlide() {
  const uid = () => `el-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  const [W, H] = _peDims();
  const sl = {
    id: `s-${Date.now()}`,
    bg: '',
    elements: [
      { id: uid(), type: 'text', x: 64, y: Math.round(H / 2) - 80, w: W - 128, h: 160,
        content: 'New Slide', fontSize: 52, fontWeight: '800', color: '#e2e8f0', textAlign: 'center', lineHeight: 1.2 },
      { id: uid(), type: 'text', x: 64, y: Math.round(H / 2) + 90, w: W - 128, h: 60,
        content: 'Double-click to edit text', fontSize: 20, fontWeight: '400', color: '#94a3b8', textAlign: 'center', lineHeight: 1.4 },
    ],
  };
  _PE.slides.push(sl);
  _PE.dirty    = true;
  _PE.slideIdx = _PE.slides.length - 1;
  peRenderSlideList();
  peEditSlide(_PE.slideIdx);
}

function peMoveSlide(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= _PE.slides.length) return;
  [_PE.slides[i], _PE.slides[j]] = [_PE.slides[j], _PE.slides[i]];
  if (_PE.slideIdx === i) _PE.slideIdx = j;
  _PE.dirty = true;
  peRenderSlideList();
}

function peDupSlide(i) {
  const copy = JSON.parse(JSON.stringify(_PE.slides[i]));
  copy.id = `s-${Date.now()}`;
  copy.elements.forEach(e => { e.id = `el-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`; });
  _PE.slides.splice(i + 1, 0, copy);
  _PE.dirty = true;
  peRenderSlideList();
  peEditSlide(i + 1);
}

function peDelSlide(i) {
  if (_PE.slides.length === 1) { _PE.slides = []; peShowEmptyCanvas(); peRenderSlideList(); return; }
  _PE.slides.splice(i, 1);
  _PE.slideIdx = Math.min(_PE.slideIdx, _PE.slides.length - 1);
  _PE.dirty = true;
  peRenderSlideList();
  peEditSlide(_PE.slideIdx);
}

function peSelectEl(elId) {
  _PE.selElId = elId;
  document.querySelectorAll('.pe-el').forEach(d => d.classList.toggle('selected', d.dataset.eid === elId));
  document.querySelectorAll('.pe-handle-set').forEach(d => d.remove());
  if (elId) {
    const div = document.getElementById(`peel-${elId}`);
    if (div) _peAddHandles(div, elId);
    _peShowProps(elId);
  } else {
    _peHideProps();
  }
}

function peElDown(e, elId) {
  if (e.button !== 0) return;
  e.preventDefault();
  peSelectEl(elId);
  const el = _peFindEl(elId);
  if (!el) return;
  _PE.drag = {
    elId, mode: 'move',
    startMX: e.clientX, startMY: e.clientY,
    origX: el.x, origY: el.y, origW: el.w, origH: el.h,
  };
}

function peHandleDown(e, elId, handle) {
  e.preventDefault(); e.stopPropagation();
  const el = _peFindEl(elId);
  if (!el) return;
  _PE.drag = {
    elId, mode: handle,
    startMX: e.clientX, startMY: e.clientY,
    origX: el.x, origY: el.y, origW: el.w, origH: el.h,
  };
}

function peCanvasDown(e) {
  if (e.target === document.getElementById('pe-canvas')) peSelectEl(null);
}

function peDragMove(e) {
  if (!_PE.drag) return;
  const d = _PE.drag;
  const s = _PE.scale;
  const dx = (e.clientX - d.startMX) / s;
  const dy = (e.clientY - d.startMY) / s;
  const el = _peFindEl(d.elId);
  if (!el) return;
  const [W, H] = _peDims();
  const minW = 20, minH = 16;
  if (d.mode === 'move') {
    el.x = Math.max(0, Math.min(W - el.w, Math.round(d.origX + dx)));
    el.y = Math.max(0, Math.min(H - el.h, Math.round(d.origY + dy)));
  } else {
    let x = d.origX, y = d.origY, w = d.origW, h = d.origH;
    if (d.mode.includes('e')) w = Math.max(minW, Math.round(d.origW + dx));
    if (d.mode.includes('s')) h = Math.max(minH, Math.round(d.origH + dy));
    if (d.mode.includes('w')) { const nw = Math.max(minW, Math.round(d.origW - dx)); x = d.origX + d.origW - nw; w = nw; }
    if (d.mode.includes('n')) { const nh = Math.max(minH, Math.round(d.origH - dy)); y = d.origY + d.origH - nh; h = nh; }
    el.x = Math.max(0, x); el.y = Math.max(0, y);
    el.w = Math.min(W - el.x, w); el.h = Math.min(H - el.y, h);
  }
  const div = document.getElementById(`peel-${d.elId}`);
  if (div) {
    div.style.left   = Math.round(el.x * s) + 'px';
    div.style.top    = Math.round(el.y * s) + 'px';
    div.style.width  = Math.round(el.w * s) + 'px';
    div.style.height = Math.round(el.h * s) + 'px';
    if (el.type === 'text') {
      const inner = div.querySelector('.pe-el-text-inner');
      if (inner) inner.style.fontSize = Math.round(el.fontSize * s) + 'px';
    }
  }
  _peUpdatePosInputs(el);
}

function peDragEnd(e) {
  if (!_PE.drag) return;
  const { elId, mode } = _PE.drag;
  _PE.drag  = null;
  _PE.dirty = true;
  const el = _peFindEl(elId);
  if (el && !['text', 'shape'].includes(el.type)) {
    const div = document.getElementById(`peel-${elId}`);
    if (div) peRenderDataEl(div, el, _PE.scale);
  }
  if (mode !== 'move') peSelectEl(elId);
}

function peAddEl(type) {
  if (!_PE.slides.length) peAddSlide();
  const sl = _PE.slides[_PE.slideIdx];
  if (!sl) return;
  const uid = `el-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  const [W, H] = _peDims();
  const defaults = {
    text       : { content: 'New text', fontSize: 28, fontWeight: '400', color: '#e2e8f0', textAlign: 'left', lineHeight: 1.6, x: 80, y: 200, w: 600, h: 100 },
    map_2d     : { job_id: '', zoom: 7, x: 20, y: 80, w: W - 40, h: H - 100 },
    map_3d     : { job_id: '', x: 20, y: 80, w: W - 40, h: H - 100 },
    histogram  : { job_id: '', field: 'magnitude', x: 20, y: 80, w: W - 40, h: H - 100 },
    stat_table : { job_id: '', x: 20, y: 80, w: W - 40, h: H - 100 },
    shape      : { fill: 'rgba(59,130,246,.15)', borderColor: '#3b82f6', borderW: 2, radius: 8, x: 100, y: 100, w: 300, h: 200 },
  };
  const el = { id: uid, type, ...(defaults[type] || { x: 100, y: 100, w: 400, h: 200 }) };
  sl.elements.push(el);
  _PE.dirty = true;
  peRenderCanvas();
  peSelectEl(uid);
}

function peDelEl(elId) {
  const sl = _PE.slides[_PE.slideIdx];
  if (!sl) return;
  const idx = sl.elements.findIndex(e => e.id === elId);
  if (idx < 0) return;
  if (_PE.mapInst[elId]) { try { _PE.mapInst[elId].remove(); } catch (_) {} delete _PE.mapInst[elId]; }
  sl.elements.splice(idx, 1);
  _PE.selElId = null;
  _PE.dirty   = true;
  peRenderCanvas();
  _peHideProps();
  peRenderSlideList();
}

function peApplyProp() {
  const el = _PE.selElId ? _peFindEl(_PE.selElId) : null;
  if (!el) return;
  const v = id => document.getElementById(id)?.value;
  const n = id => parseFloat(document.getElementById(id)?.value) || 0;
  if (el.type === 'text') {
    el.content    = document.getElementById('pp-content')?.value ?? el.content;
    el.fontSize   = n('pp-fs')    || el.fontSize;
    el.fontWeight = v('pp-fw')    || el.fontWeight;
    el.color      = v('pp-color') || el.color;
    el.lineHeight = n('pp-lh')    || el.lineHeight;
    const div   = document.getElementById(`peel-${el.id}`);
    const inner = div?.querySelector('.pe-el-text-inner');
    if (inner) {
      inner.textContent      = el.content;
      inner.style.fontSize   = Math.round(el.fontSize * _PE.scale) + 'px';
      inner.style.fontWeight = el.fontWeight;
      inner.style.color      = el.color;
      inner.style.lineHeight = el.lineHeight;
    }
    const hex = document.getElementById('pp-colorhex');
    if (hex && document.activeElement !== hex) hex.value = el.color;
  } else if (el.type === 'shape') {
    el.fill        = v('pp-fill')   || el.fill;
    el.borderColor = v('pp-bcolor') || el.borderColor;
    el.borderW     = n('pp-bw');
    el.radius      = n('pp-rad');
    const div = document.getElementById(`peel-${el.id}`);
    if (div) {
      div.style.background   = el.fill;
      div.style.border       = `${Math.max(1, Math.round(el.borderW * _PE.scale))}px solid ${el.borderColor}`;
      div.style.borderRadius = Math.round(el.radius * _PE.scale) + 'px';
    }
  }
  _PE.dirty = true;
  peRenderSlideList();
}

function peApplyPos() {
  const el = _PE.selElId ? _peFindEl(_PE.selElId) : null;
  if (!el) return;
  const n = id => parseInt(document.getElementById(id)?.value) || 0;
  const [W, H] = _peDims();
  el.x = Math.max(0, Math.min(W, n('pp-x'))); el.y = Math.max(0, Math.min(H, n('pp-y')));
  el.w = Math.max(20, Math.min(W - el.x, n('pp-w'))); el.h = Math.max(16, Math.min(H - el.y, n('pp-h')));
  const div = document.getElementById(`peel-${el.id}`);
  const s   = _PE.scale;
  if (div) {
    div.style.left   = Math.round(el.x * s) + 'px'; div.style.top    = Math.round(el.y * s) + 'px';
    div.style.width  = Math.round(el.w * s) + 'px'; div.style.height = Math.round(el.h * s) + 'px';
  }
  if (!['text', 'shape'].includes(el.type)) peRenderDataEl(div, el, s);
  _PE.dirty = true;
}

function peSetAlign(align) {
  const el = _PE.selElId ? _peFindEl(_PE.selElId) : null;
  if (!el || el.type !== 'text') return;
  el.textAlign = align;
  const inner = document.querySelector(`#peel-${el.id} .pe-el-text-inner`);
  if (inner) inner.style.textAlign = align;
  document.querySelectorAll('.pe-align-btns button').forEach(b => b.classList.toggle('act', b.textContent.includes(align[0].toUpperCase())));
  _PE.dirty = true;
}

function peSelectJob(elId, jobId) {
  const el = _peFindEl(elId);
  if (!el) return;
  el.job_id = jobId;
  _PE.dirty = true;
  document.querySelectorAll('.pe-job-card').forEach(card => {
    const radio = card.querySelector('input[type=radio]');
    card.classList.toggle('selected', !!(radio && radio.checked));
  });
  peRefreshDataEl(elId);
}

function peSetHistField(elId, field) {
  const el = _peFindEl(elId);
  if (!el) return;
  el.field  = field;
  _PE.dirty = true;
  document.querySelectorAll('.pe-hfield-btns button').forEach(b =>
    b.classList.toggle('act', b.textContent.toLowerCase().includes(field === 'magnitude' ? 'magni' : 'depth'))
  );
  peRefreshDataEl(elId);
}

function peApplyZoom(elId) {
  const el = _peFindEl(elId);
  if (!el) return;
  el.zoom   = parseFloat(document.getElementById('pp-zoom')?.value) || 7;
  _PE.dirty = true;
}

function peApplyStatFields(elId) {
  const el = _peFindEl(elId);
  if (!el || el.type !== 'stat_table') return;
  el.stat_fields = Array.from(document.querySelectorAll('.pe-stat-fields input:checked')).map(i => i.value);
  _PE.dirty = true;
  const div = document.getElementById(`peel-${el.id}`);
  if (div) div.innerHTML = _peStatTableHTML(el);
}

function peRefreshDataEl(elId) {
  const el = _peFindEl(elId);
  if (!el) return;
  const div = document.getElementById(`peel-${elId}`);
  if (!div) return;
  peRenderDataEl(div, el, _PE.scale);
}

function peInlineEdit(elId) {
  const el = _peFindEl(elId);
  if (!el || el.type !== 'text') return;
  const div   = document.getElementById(`peel-${elId}`);
  const inner = div?.querySelector('.pe-el-text-inner');
  if (!inner) return;
  inner.contentEditable = 'true';
  inner.style.cursor    = 'text';
  inner.focus();
  inner.addEventListener('blur', () => {
    inner.contentEditable = 'false';
    inner.style.cursor    = '';
    el.content = inner.textContent || '';
    const ta = document.getElementById('pp-content');
    if (ta) ta.value = el.content;
    _PE.dirty = true;
    peRenderSlideList();
  }, { once: true });
  inner.addEventListener('keydown', e => { if (e.key === 'Escape') inner.blur(); });
}

function peFitScale() {
  const stage = document.getElementById('pe-stage');
  if (!stage) return;
  const [W, H] = _peDims();
  const sw = stage.clientWidth  - 40;
  const sh = stage.clientHeight - 60;
  _PE.scale = Math.min(Math.max(0.2, Math.min(sw / W, sh / H)), 2);
  _peApplyCanvasScale();
}

function peZoom(delta) {
  _PE.scale = Math.max(0.2, Math.min(2, _PE.scale + delta));
  _peApplyCanvasScale();
}

function _peReloadJobSel(elId) {
  if (!_wpCfgId) return;
  const btn = document.querySelector('.pe-refresh-job-btn');
  if (btn) { btn.textContent = '↻'; btn.style.animation = 'pespin .6s linear infinite'; }
  fetch(`/api/result/${_wpCfgId}/catalog`)
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (d) { _PE.catJobs = d.jobs || []; _PE.catData = d; }
      if (btn) { btn.style.animation = ''; btn.textContent = '↻'; }
      _peShowProps(elId || _PE.selElId);
    })
    .catch(() => { if (btn) { btn.style.animation = ''; } });
}

function presToggleAddMenu() { peAddSlide(); }
function presCloseAddMenu()  {}

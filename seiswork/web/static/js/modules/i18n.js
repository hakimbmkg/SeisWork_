// ─────────────────────────────────────────────────────────────────────────────
//  SeisWork i18n — English ⇄ Indonesian UI language toggle.
//
//  Overlay approach: the UI is authored in English (the canonical language);
//  switching to Indonesian walks the DOM and replaces text nodes / common
//  attributes (title, placeholder) whose trimmed content has an exact match in
//  the dictionary below. Originals are remembered so switching back to English
//  is a lossless restore. A MutationObserver translates panels that are
//  rendered later (modals, job cards, online dashboard).
//
//  Untranslated strings simply stay in English — extend I18N_ID over time.
//  The choice persists in localStorage ('sw_lang') across sessions.
// ─────────────────────────────────────────────────────────────────────────────

const I18N_ID = {
  // ── Navbar / global ────────────────────────────────────────────────────────
  'Simple Seismological Data Processing Framework': 'Framework Sederhana Pemrosesan Data Seismologi',
  'New Project': 'Proyek Baru',
  'Projects': 'Proyek',
  'Server': 'Server',
  'Offline': 'Offline',
  'Online': 'Online',
  'No project': 'Belum ada proyek',
  'Loading interface…': 'Memuat antarmuka…',
  'Loading…': 'Memuat…',

  // ── Sidebar cards ─────────────────────────────────────────────────────────
  'Station Input': 'Input Stasiun',
  'Region & Time': 'Wilayah & Waktu',
  'Catalog Download': 'Unduh Katalog',
  'Waveform Data': 'Data Waveform',
  'Saved Configurations': 'Konfigurasi Tersimpan',
  'Upload CSV': 'Unggah CSV',
  'Click or drag CSV / TXT file': 'Klik atau seret file CSV / TXT',
  'Fetch Data': 'Ambil Data',
  'Server Preset': 'Preset Server',
  'Loaded:': 'Termuat:',
  'stations': 'stasiun',
  'Clear': 'Bersihkan',
  'Replot': 'Plot ulang',
  'Active Region': 'Wilayah Aktif',
  'From Map View': 'Dari Tampilan Peta',
  'Fit to Map': 'Sesuaikan ke Peta',
  'Filter & Save': 'Filter & Simpan',
  'Data Source': 'Sumber Data',
  'Output Path': 'Path Keluaran',
  'Starttime': 'Waktu Mulai',
  'Endtime': 'Waktu Selesai',
  'Download': 'Unduh',
  'Save Config': 'Simpan Config',
  'Save': 'Simpan',
  'Cancel': 'Batal',
  'Close': 'Tutup',
  'Delete': 'Hapus',
  'Run': 'Jalankan',
  'Stop': 'Hentikan',
  'Start': 'Mulai',
  'Apply': 'Terapkan',
  'Reset': 'Reset',
  'Refresh': 'Segarkan',
  'Copy': 'Salin',
  'Copy path': 'Salin path',
  'Auto-fill': 'Isi otomatis',
  'Filter': 'Filter',
  'All': 'Semua',
  'None': 'Tidak ada',
  'Search': 'Cari',
  'Upload': 'Unggah',
  'Browse': 'Telusuri',

  // ── Work page steps ───────────────────────────────────────────────────────
  'Overview': 'Ringkasan',
  'Picking & Detection': 'Picking & Deteksi',
  'Assoc & Location': 'Asosiasi & Lokasi',
  'Mechanism': 'Mekanisme',
  'Velocity': 'Kecepatan',
  'Relocation': 'Relokasi',
  'Imaging': 'Pencitraan',
  'Output': 'Keluaran',
  'Sys Monitor': 'Monitor Sistem',
  'Presentation': 'Presentasi',
  'Picking Jobs': 'Job Picking',
  'Pipeline Dirs': 'Direktori Pipeline',
  'Output Files': 'File Keluaran',
  'Velocity Model': 'Model Kecepatan',
  'Focal Mechanisms': 'Mekanisme Fokal',
  'Focal Mechanism Detail': 'Detail Mekanisme Fokal',
  'Magnitude Completeness': 'Kelengkapan Magnitudo',
  'RMS Residual': 'Residual RMS',
  'CC Statistics': 'Statistik CC',
  'Waveform CC': 'CC Waveform',
  'Result Viewer': 'Penampil Hasil',
  'Catalog': 'Katalog',
  'Events': 'Event',
  'Map 2D': 'Peta 2D',
  'Map 3D': 'Peta 3D',
  'Histogram': 'Histogram',
  'Table': 'Tabel',
  'Text': 'Teks',
  'Statistics': 'Statistik',
  'Settings': 'Pengaturan',

  // ── Online monitor ────────────────────────────────────────────────────────
  'New Config': 'Config Baru',
  'Direct Connect': 'Koneksi Langsung',
  'Connect': 'Hubungkan',
  'Disconnect': 'Putuskan',
  'Start Monitoring': 'Mulai Monitoring',
  'Stop Monitoring': 'Hentikan Monitoring',
  'Monitor & Inject SeisComP': 'Monitor & Inject SeisComP',
  'Source data SeedLink IP': 'IP SeedLink sumber data',
  'Extra SeedLink sources': 'Sumber SeedLink tambahan',
  'Discover Streams': 'Temukan Stream',
  'Sync SeisComP': 'Sinkron SeisComP',
  'Agent URL': 'URL Agent',
  'Agent Token': 'Token Agent',
  'Inventory XML': 'XML Inventory',
  'Event Catalog': 'Katalog Event',
  'Pick Log': 'Log Pick',
  'Event List': 'Daftar Event',
  'Event Detail': 'Detail Event',
  'Station Map': 'Peta Stasiun',
  'Engine': 'Mesin',
  'Detection': 'Deteksi',
  'Share': 'Bagikan',

  // ── Common labels ─────────────────────────────────────────────────────────
  'Host': 'Host',
  'Port': 'Port',
  'Username': 'Nama pengguna',
  'Password': 'Kata sandi',
  'Station': 'Stasiun',
  'Network': 'Jaringan',
  'Channel': 'Kanal',
  'Depth': 'Kedalaman',
  'Magnitude': 'Magnitudo',
  'Latitude': 'Lintang',
  'Longitude': 'Bujur',
  'Time': 'Waktu',
  'Date': 'Tanggal',
  'Status': 'Status',
  'Progress': 'Progres',
  'Done': 'Selesai',
  'Running': 'Berjalan',
  'Failed': 'Gagal',
  'Error': 'Error',
  'Warning': 'Peringatan',
  'Workers': 'Worker',
  'Mode': 'Mode',
  'Model': 'Model',
  'Region': 'Wilayah',
  'Topography': 'Topografi',
  'Coastline': 'Garis pantai',
  'Volcano': 'Gunung api',
  'Fault Names': 'Nama Sesar',
  'Legend': 'Legenda',
};

const SW_I18N = {
  lang: localStorage.getItem('sw_lang') || 'en',
  _origText: new WeakMap(),   // text node → original English string
  _ATTRS: ['title', 'placeholder', 'data-tip'],
  _observer: null,

  // Translate (or restore) one text node.
  _applyText(node) {
    const raw = node.nodeValue;
    if (!raw) return;
    if (this.lang === 'id') {
      const key = raw.trim();
      const tr = I18N_ID[key];
      if (tr) {
        if (!this._origText.has(node)) this._origText.set(node, raw);
        node.nodeValue = raw.replace(key, tr);
      }
    } else if (this._origText.has(node)) {
      node.nodeValue = this._origText.get(node);
    }
  },

  _applyAttrs(el) {
    for (const a of this._ATTRS) {
      const cur = el.getAttribute?.(a);
      if (cur == null) continue;
      const orig = el.dataset ? el.dataset[`i18nOrig${a}`] : null;
      if (this.lang === 'id') {
        const tr = I18N_ID[cur.trim()];
        if (tr) {
          if (el.dataset && orig == null) el.dataset[`i18nOrig${a}`] = cur;
          el.setAttribute(a, tr);
        }
      } else if (orig != null) {
        el.setAttribute(a, orig);
        delete el.dataset[`i18nOrig${a}`];
      }
    }
  },

  _applyTree(root) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => {
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'CODE' || tag === 'PRE')
          return NodeFilter.FILTER_REJECT;
        return n.nodeValue && n.nodeValue.trim()
          ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    texts.forEach(n => this._applyText(n));
    if (root.querySelectorAll) {
      this._applyAttrs(root);
      root.querySelectorAll('[title],[placeholder],[data-tip]')
          .forEach(el => this._applyAttrs(el));
    }
  },

  // Translate late-rendered panels (modals, job cards, online dashboard).
  _startObserver() {
    if (this._observer) return;
    this._observer = new MutationObserver(muts => {
      if (this.lang !== 'id') return;
      for (const m of muts) {
        m.addedNodes.forEach(n => {
          if (n.nodeType === Node.TEXT_NODE) this._applyText(n);
          else if (n.nodeType === Node.ELEMENT_NODE) this._applyTree(n);
        });
      }
    });
    this._observer.observe(document.body, { childList: true, subtree: true });
  },

  setLang(lang) {
    this.lang = lang === 'id' ? 'id' : 'en';
    localStorage.setItem('sw_lang', this.lang);
    this._applyTree(document.body);
    this._startObserver();
    const btn = document.getElementById('sw-lang-toggle');
    // The button shows the language you would switch TO.
    if (btn) btn.innerHTML = this.lang === 'id'
      ? '<i class="bi bi-translate"></i> EN'
      : '<i class="bi bi-translate"></i> ID';
  },

  toggle() { this.setLang(this.lang === 'id' ? 'en' : 'id'); },

  init() {
    this._startObserver();
    if (this.lang === 'id') this.setLang('id');
    else {
      const btn = document.getElementById('sw-lang-toggle');
      if (btn) btn.innerHTML = '<i class="bi bi-translate"></i> ID';
    }
  },
};

document.addEventListener('DOMContentLoaded', () => SW_I18N.init());

#!/usr/bin/env python3
"""
SeisWork Online - Architecture & Workflow Doc Generator
Author : HakimBMKG

Separate from SeisWork_Dokumentasi_Lengkap.
Covers: product vision, two-way system architecture (SeisWork <-> SeisComP),
SeisWork Agent, Online Mode wizard, API reference, and implementation roadmap.

Output:
    docs/SeisWork_Online_Dokumentasi.html
    docs/SeisWork_Online_Dokumentasi.pdf

Run:
    PYTHONNOUSERSITE=1 /opt/miniconda3/envs/seiswork/bin/python docs/generate_online_doc.py
"""

import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"
OUT_HTML = DOCS / "SeisWork_Online_Dokumentasi.html"
OUT_PDF  = DOCS / "SeisWork_Online_Dokumentasi.pdf"
NOW = datetime.now().strftime("%d %B %Y")

# ── CSS ───────────────────────────────────────────────────────────────────────
PDF_CSS = """
@page {
  size: A4; margin: 1.8cm 1.9cm 2.0cm 1.9cm;
  @bottom-center {
    content: "SeisWork Online · Arsitektur & Alur Kerja · HakimBMKG · " counter(page) "/" counter(pages);
    font-size: 8pt; color: #888;
  }
  @top-right { content: "BMKG — Real-Time Seismological Integration"; font-size: 7.5pt; color: #aaa; }
}
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
       font-size: 9.6pt; line-height: 1.55; color: #232323; }
h1  { font-size: 22pt; color: #0a2342; border-bottom: 3px solid #2563a8;
      padding-bottom: 6px; margin: 0 0 4px; }
h2  { font-size: 14.5pt; color: #0a2342; border-bottom: 1.5px solid #cdd9e8;
      padding-bottom: 3px; margin: 22px 0 9px; page-break-after: avoid; }
h3  { font-size: 11pt; color: #1a4a7a; margin: 14px 0 5px; page-break-after: avoid; }
h4  { font-size: 9.8pt; color: #234e7a; margin: 10px 0 4px; }
p, li { margin: 3px 0; }
ul  { padding-left: 1.2em; }
code { font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 8.2pt;
       background: #eef2f7; color: #b8002f; padding: 1px 4px; border-radius: 3px; }
pre  { background: #0f1b2d; color: #e6edf3; padding: 9px 12px; border-radius: 6px;
       font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 7.9pt;
       line-height: 1.45; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; color: inherit; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 8.4pt;
        page-break-inside: avoid; }
th { background: #1a4a7a; color: #fff; text-align: left; padding: 4px 7px; }
td { border: 1px solid #d4dcea; padding: 3px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #f5f8fc; }
blockquote { border-left: 3px solid #2563a8; background: #f0f5fb; margin: 7px 0;
             padding: 5px 12px; color: #33506e; font-size: 9pt; }
a  { color: #2563a8; text-decoration: none; }
hr { border: none; border-top: 1px solid #d4dcea; margin: 16px 0; }

/* ── Diagram komponen ─────────────────────────────────── */
.arch-wrap   { page-break-inside: avoid; margin: 14px 0; }
.arch-row    { display: flex; gap: 0; align-items: stretch; margin: 0; }
.arch-server { border: 2px solid #1a4a7a; border-radius: 8px; padding: 8px 10px;
               margin: 6px 0; background: #f5f8fc; flex: 1; min-width: 0; }
.arch-server.gpu { border-color: #1a7a4a; background: #f0fbf5; }
.arch-server.sc  { border-color: #7a4a1a; background: #fdf5f0; }
.arch-server.agent { border-color: #5a1a7a; background: #f8f0fd; }
.arch-server.field { border-color: #1a4a7a; background: #f0f4fb; }
.arch-title  { font-weight: 700; font-size: 9pt; margin-bottom: 5px; }
.arch-title.gpu   { color: #1a7a4a; }
.arch-title.sc    { color: #7a4a1a; }
.arch-title.agent { color: #5a1a7a; }
.arch-title.field { color: #1a4a7a; }
.arch-item   { font-size: 8pt; color: #445; margin: 1px 0; padding-left: 8px; }
.arch-conn   { display: flex; flex-direction: column; align-items: center;
               justify-content: center; padding: 0 8px; font-size: 7.5pt;
               color: #555; min-width: 60px; gap: 2px; }
.arch-arrow  { font-size: 14pt; color: #2563a8; line-height: 1; }
.arch-label  { font-size: 7pt; color: #2563a8; font-weight: 600;
               text-align: center; white-space: nowrap; }

/* ── Flow wizard ──────────────────────────────────────── */
.wizard-wrap { display: flex; gap: 0; align-items: flex-start;
               margin: 14px 0; page-break-inside: avoid; }
.wizard-step { flex: 1; border: 1.5px solid #2563a8; border-radius: 7px;
               padding: 8px 10px; background: #f5f8fc; min-width: 0; }
.wizard-num  { display: inline-block; background: #2563a8; color: #fff;
               border-radius: 50%; width: 18px; height: 18px; text-align: center;
               line-height: 18px; font-size: 8pt; font-weight: 700; margin-right: 5px; }
.wizard-title { font-weight: 700; font-size: 9pt; color: #0a2342; margin-bottom: 5px; }
.wizard-item  { font-size: 7.8pt; color: #334; margin: 2px 0; padding-left: 6px; }
.wizard-arr   { display: flex; align-items: center; padding: 0 5px; font-size: 18pt;
                color: #2563a8; padding-top: 18px; }

/* ── Alur proses realtime ─────────────────────────────── */
.flow-chain  { margin: 10px 0; page-break-inside: avoid; }
.flow-node   { display: inline-block; border: 1.5px solid #1a4a7a; border-radius: 5px;
               padding: 4px 9px; font-size: 8pt; background: #f0f5fb;
               color: #0a2342; font-weight: 600; white-space: nowrap; }
.flow-node.trigger { border-color: #7a4a1a; background: #fdf5f0; color: #7a4a1a; }
.flow-node.agent   { border-color: #5a1a7a; background: #f8f0fd; color: #5a1a7a; }
.flow-node.proc    { border-color: #1a7a4a; background: #f0fbf5; color: #1a7a4a; }
.flow-node.out     { border-color: #2563a8; background: #e8f0fd; color: #1a4096; }
.flow-arr   { display: inline-block; margin: 0 4px; font-size: 10pt; color: #2563a8;
              font-weight: 700; vertical-align: middle; }
.flow-ann   { display: block; font-size: 7pt; color: #668; font-style: italic;
              margin-left: 4px; margin-bottom: 3px; }

/* ── Badge status ──────────────────────────────────────── */
.badge       { display: inline-block; border-radius: 3px; padding: 1px 6px;
               font-size: 7.5pt; font-weight: 700; }
.badge.done  { background: #d1fae5; color: #065f46; }
.badge.wip   { background: #fef3c7; color: #92400e; }
.badge.todo  { background: #ede9fe; color: #4c1d95; }
.badge.phase { background: #dbeafe; color: #1e40af; }

/* ── Note box ───────────────────────────────────────────── */
.note { border-left: 3px solid #f59e0b; background: #fffbeb; padding: 5px 10px;
        margin: 8px 0; font-size: 8.5pt; color: #44360a; border-radius: 0 5px 5px 0; }
.note.info { border-color: #2563a8; background: #eff6ff; color: #1e3a6e; }
.note.ok   { border-color: #16a34a; background: #f0fdf4; color: #14532d; }
"""

# ── Document content ───────────────────────────────────────────────────────
HEADER_HTML = f"""
<h1>SeisWork Online</h1>
<p style="margin:4px 0 2px; font-size:10.5pt; color:#1a4a7a; font-weight:600;">
  Arsitektur Sistem Integrasi Dua Arah: SeisWork ⇄ SeisComP
</p>
<p style="margin:0; font-size:8.5pt; color:#888;">
  by HakimBMKG &nbsp;·&nbsp; {NOW} &nbsp;·&nbsp; Dokumen terpisah dari SeisWork_Dokumentasi_Lengkap
</p>
<hr>
"""

SEC_1 = """
<h2>1. Latar Belakang &amp; Visi Produk</h2>

<p><strong>SeisWork Online</strong> menjadikan SeisWork sebagai <em>integrator terpusat</em>
antara jaringan sensor seismik (dikelola SeisComP di server lapangan) dengan engine pemrosesan
cerdas (PhaseNet GPU, REAL, ML magnitude, focal mechanism) di server SeisWork yang terpisah.</p>

<div class="note info">
  <strong>Keunggulan kompetitif:</strong> SeisWork berjalan di server GPU dengan penyimpanan besar
  dan OS fleksibel — terpisah dari server SeisComP yang biasanya ringan dan dekat sensor lapangan.
  Komunikasi dua arah memungkinkan SeisWork mengkonfigurasi SeisComP (push) sekaligus menerima
  data real-time dari SeisComP (pull), tanpa user perlu menyentuh SeisComP secara langsung.
</div>

<h3>Dua Skenario Deployment</h3>
<table>
  <tr><th>Skenario</th><th>Topologi</th><th>Akses Filesystem</th><th>Komunikasi</th></tr>
  <tr><td><strong>Local</strong></td>
      <td>SeisWork + SeisComP di mesin yang sama</td>
      <td>Langsung ke <code>$SEISCOMP_ROOT/etc/</code></td>
      <td>localhost + subprocess</td></tr>
  <tr><td><strong>Remote</strong></td>
      <td>SeisWork di server GPU, SeisComP di server lapangan</td>
      <td>Via SeisWork Agent (REST)</td>
      <td>HTTPS + bearer-token</td></tr>
</table>
<p>Kedua skenario menggunakan antarmuka yang <strong>sama</strong> dari sisi GUI —
hanya field <em>host Agent</em> yang berbeda (<code>localhost</code> vs IP server SeisComP).</p>
"""

SEC_2 = """
<h2>2. Arsitektur Sistem</h2>

<h3>2.1 Gambaran Komponen</h3>

<div class="arch-wrap">
  <div style="display:flex; gap:0; align-items:center;">

    <!-- Server SeisWork -->
    <div class="arch-server gpu" style="flex:2.2;">
      <div class="arch-title gpu">SERVER SEISWORK &nbsp;(GPU / Storage Besar)</div>
      <div class="arch-item">▸ Web GUI (Flask port 5000) — Wizard Online</div>
      <div class="arch-item">▸ FDSNWS downloader — unduh/pilih stasiun</div>
      <div class="arch-item">▸ Inventory writer — generate FDSNXML + binding</div>
      <div class="arch-item">▸ LiveSeedlinkSession — ring buffer numpy</div>
      <div class="arch-item">▸ PhaseNet re-pick (GPU, seisbench/PyTorch)</div>
      <div class="arch-item">▸ REAL associator + lokasi awal</div>
      <div class="arch-item">▸ ML magnitude (Hutton &amp; Boore 1987)</div>
      <div class="arch-item">▸ SKHASH / FocoNet focal mechanism</div>
      <div class="arch-item">▸ Catalog + Event Feed + Map + Mechanism panel</div>
    </div>

    <!-- Panah kiri-kanan -->
    <div class="arch-conn">
      <div class="arch-arrow">→</div>
      <div class="arch-label">PUSH<br>inventory<br>bindings<br>config</div>
      <div style="height:8px;"></div>
      <div class="arch-arrow">←</div>
      <div class="arch-label">PULL<br>SeedLink<br>picks<br>events</div>
    </div>

    <!-- Kolom kanan: Agent + SeisComP -->
    <div style="flex:1.8; display:flex; flex-direction:column; gap:6px;">

      <div class="arch-server agent">
        <div class="arch-title agent">SeisWork Agent &nbsp;(di server SeisComP)</div>
        <div class="arch-item">▸ Terima perintah dari SeisWork (REST + token)</div>
        <div class="arch-item">▸ Write etc/inventory/, etc/key/ (binding)</div>
        <div class="arch-item">▸ Jalankan update-config + scinv sync</div>
        <div class="arch-item">▸ Subscribe message bus → forward picks</div>
        <div class="arch-item">▸ Status: seiscomp status, restart module</div>
      </div>

      <div class="arch-conn" style="flex-direction:row; justify-content:center; min-width:0; padding:0;">
        <span class="arch-arrow" style="font-size:12pt;">↕</span>
        <span class="arch-label" style="margin-left:4px;">subprocess /<br>file I/O</span>
      </div>

      <div class="arch-server sc">
        <div class="arch-title sc">SERVER SEISCOMP &nbsp;(Lapangan / Ringan)</div>
        <div class="arch-item">▸ seedlink (port 18000) — distribusi waveform</div>
        <div class="arch-item">▸ scautopick — STA/LTA trigger</div>
        <div class="arch-item">▸ scautoloc — lokasi awal</div>
        <div class="arch-item">▸ etc/inventory/ — file FDSNXML</div>
        <div class="arch-item">▸ etc/key/ — binding stasiun</div>
      </div>

    </div>
  </div>

  <!-- FDSNWS di bawah -->
  <div style="display:flex; align-items:center; margin-top:8px; gap:8px;">
    <div class="arch-server field" style="flex:none; padding:5px 10px;">
      <div class="arch-title field" style="margin-bottom:2px;">FDSNWS Server</div>
      <div class="arch-item">(IRIS/GEOFON/BMKG/lokal)</div>
    </div>
    <div class="arch-conn" style="min-width:50px;">
      <div class="arch-arrow">→</div>
      <div class="arch-label">download<br>inventory</div>
    </div>
    <div style="font-size:8.5pt; color:#444;">SeisWork mengunduh StationXML via ObsPy
      <code>Client.get_stations()</code>, lalu user memilih stasiun di peta.</div>
  </div>
</div>

<h3>2.2 Alur Komunikasi Dua Arah</h3>

<table>
  <tr><th>Arah</th><th>Dari</th><th>Ke</th><th>Isi</th><th>Metode</th></tr>
  <tr><td><strong>Push A</strong></td><td>SeisWork</td><td>Agent → SeisComP</td>
      <td>Inventory XML (FDSNXML)</td><td>POST /push/inventory → write file + scinv sync</td></tr>
  <tr><td><strong>Push B</strong></td><td>SeisWork</td><td>Agent → SeisComP</td>
      <td>Binding key files (seedlink + scautopick)</td>
      <td>POST /push/bindings → write etc/key/ + update-config</td></tr>
  <tr><td><strong>Push C</strong></td><td>SeisWork</td><td>Agent → SeisComP</td>
      <td>Config processing (STA/LTA threshold, grid)</td>
      <td>POST /push/config → write profile + restart</td></tr>
  <tr><td><strong>Pull A</strong></td><td>SeisComP seedlink</td><td>SeisWork</td>
      <td>Waveform real-time (MSEED)</td>
      <td>EasySeedLinkClient → numpy ring buffer</td></tr>
  <tr><td><strong>Pull B</strong></td><td>SeisComP message bus</td><td>SeisWork</td>
      <td>Picks / trigger STA/LTA</td>
      <td>Agent subscribe bus → POST /api/online/trigger</td></tr>
  <tr><td><strong>Pull C</strong></td><td>SeisComP</td><td>SeisWork</td>
      <td>Status module (running/stopped)</td>
      <td>GET /status via Agent → seiscomp status</td></tr>
</table>
"""

SEC_3 = """
<h2>3. Alur Kerja Lengkap — Sistem Real-Time</h2>

<h3>3.1 Alur Push: SeisWork → SeisComP</h3>

<div class="flow-chain">
  <span class="flow-node">FDSNWS Server</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">ObsPy get_stations()</span>
  <span class="flow-arr">→</span>
  <span class="flow-node">StationXML</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">Pilih stasiun (peta)</span>
  <span class="flow-arr">→</span>
  <span class="flow-node out">Generate FDSNXML + binding keys</span>
  <br>
  <span class="flow-ann">↓ POST /push/inventory + /push/bindings ke Agent</span>
  <span class="flow-node agent">SeisWork Agent</span>
  <span class="flow-arr">→</span>
  <span class="flow-node">write etc/inventory/<br>write etc/key/</span>
  <span class="flow-arr">→</span>
  <span class="flow-node trigger">seiscomp update-config<br>+ scinv sync</span>
  <span class="flow-arr">→</span>
  <span class="flow-node out">SeisComP reload<br>stasiun baru aktif</span>
</div>

<h3>3.2 Alur Pull: SeisComP → SeisWork → Produk</h3>

<div class="flow-chain">
  <strong>Waveform (SeedLink):</strong><br>
  <span class="flow-node trigger">Sensor lapangan</span>
  <span class="flow-arr">→</span>
  <span class="flow-node">seedlink:18000</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">EasySeedLinkClient</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">numpy ring buffer<br>(per NET.STA.LOC.CHA)</span>
  <span class="flow-arr">→</span>
  <span class="flow-node out">Live waveform plot</span>
  <br><br>
  <strong>Trigger Picks (Bridge/Agent):</strong><br>
  <span class="flow-node trigger">scautopick STA/LTA</span>
  <span class="flow-arr">→</span>
  <span class="flow-node">SeisComP<br>message bus</span>
  <span class="flow-arr">→</span>
  <span class="flow-node agent">Agent subscribe<br>seiscomp.client</span>
  <span class="flow-arr">→</span>
  <span class="flow-node out">POST /api/online/trigger<br>{net,sta,time,phase}</span>
  <br><br>
  <strong>Pipeline Pemrosesan SeisWork (per trigger):</strong><br>
  <span class="flow-node out">/api/online/trigger</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">get_window() ring buffer<br>(±30 det sekitar trigger)</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">PhaseNet re-pick<br>(GPU, phasenet.py)</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">REAL: asosiasi<br>+ lokasi awal</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">ML magnitude<br>(H&amp;B 1987, WA)</span>
  <span class="flow-arr">→</span>
  <span class="flow-node proc">SKHASH/FocoNet<br>focal mechanism*</span>
  <span class="flow-arr">→</span>
  <span class="flow-node out">Event Feed + Map<br>+ Mechanism panel</span>
  <br>
  <span class="flow-ann">* Focal mechanism: opsional / manual-trigger per event</span>
</div>

<div class="note">
  <strong>STA/LTA sebagai gatekeeper murah:</strong> scautopick berjalan terus di SeisComP
  menggunakan resource ringan. Hanya trigger yang lulus threshold STA/LTA yang dikirim ke SeisWork.
  PhaseNet (GPU berat) hanya diaktifkan per-window trigger, bukan seluruh stream — efisien dan hemat GPU.
  Untuk gempa besar: STA/LTA dominan, PhaseNet perbaiki onset. Untuk gempa kecil: PhaseNet menjadi
  detektor utama (STA/LTA bisa tidak trigger, tapi PhaseNet lebih sensitif pada window re-pick).
</div>

<h3>3.3 Diagram Alir Keputusan (Magnitude &amp; Focal Mechanism)</h3>

<table>
  <tr><th>Kondisi event</th><th>Jalur deteksi</th><th>Magnitude</th><th>Focal mech.</th></tr>
  <tr><td>Gempa besar (M≥3)</td>
      <td>STA/LTA trigger → PhaseNet perbaiki onset</td>
      <td>ML via WA (ObsPy + PAZ inventory)</td>
      <td>SKHASH (polaritas P cukup)</td></tr>
  <tr><td>Gempa kecil (M&lt;3)</td>
      <td>STA/LTA mungkin tidak trigger → PhaseNet primary detection</td>
      <td>ML (bisa tidak akurat untuk M sangat kecil)</td>
      <td>Tidak otomatis (polaritas kurang)</td></tr>
  <tr><td>Event tanpa trigger STA/LTA</td>
      <td>Tidak diproses (kecuali ada mode scan penuh — fase masa depan)</td>
      <td>—</td><td>—</td></tr>
</table>
"""

SEC_4 = """
<h2>4. Wizard Online Mode — Antarmuka Pengguna</h2>

<p>Pengguna tidak perlu menyentuh SeisComP secara langsung. Semua konfigurasi dilakukan
melalui 3 langkah wizard di tab <em>Online Project</em> SeisWork GUI.</p>

<div class="wizard-wrap">

  <div class="wizard-step">
    <div class="wizard-title"><span class="wizard-num">1</span>Setup Stasiun</div>
    <div class="wizard-item">• Input FDSNWS URL + filter net/sta/cha/waktu</div>
    <div class="wizard-item">• Atau upload StationXML manual</div>
    <div class="wizard-item">• Unduh via ObsPy get_stations()</div>
    <div class="wizard-item">• Tampilkan stasiun di peta (marker per stasiun)</div>
    <div class="wizard-item">• Pilih stasiun dengan checkbox / klik peta</div>
    <div class="wizard-item">• Field: path inventory XML untuk ML (PAZ)</div>
    <div class="wizard-item">• Opsional: atur parameter STA/LTA, grid lokasi</div>
  </div>

  <div class="wizard-arr">›</div>

  <div class="wizard-step">
    <div class="wizard-title"><span class="wizard-num">2</span>Sync ke SeisComP</div>
    <div class="wizard-item">• Input: host:port Agent + token (setup sekali)</div>
    <div class="wizard-item">• Test koneksi ke Agent (/status)</div>
    <div class="wizard-item">• Push inventory XML → etc/inventory/</div>
    <div class="wizard-item">• Generate + push binding key files → etc/key/</div>
    <div class="wizard-item">• Agent: seiscomp update-config + scinv sync</div>
    <div class="wizard-item">• Opsional: restart seedlink / scautopick</div>
    <div class="wizard-item">• Status: ✓ Inventory · ✓ Bindings · ✓ Reload</div>
  </div>

  <div class="wizard-arr">›</div>

  <div class="wizard-step">
    <div class="wizard-title"><span class="wizard-num">3</span>Monitor Live</div>
    <div class="wizard-item">• Connect SeedLink ke host terdeteksi/manual</div>
    <div class="wizard-item">• Panel waveform live (ring buffer numpy)</div>
    <div class="wizard-item">• Terima picks dari Agent (bridge message bus)</div>
    <div class="wizard-item">• PhaseNet → REAL → ML → Focal mechanism</div>
    <div class="wizard-item">• Event Feed: card per event + progress badge</div>
    <div class="wizard-item">• Peta: plot lokasi event real-time</div>
    <div class="wizard-item">• Badge job card: ● Live / ○ Offline</div>
  </div>

</div>

<h3>4.1 Badge Job Card — Integrasi dengan Sidebar Offline</h3>
<p>Setiap job card di sidebar Offline menampilkan badge status Live jika ada sesi Online
yang terhubung ke <code>cfg_id</code> yang sama. Klik card saat badge Live aktif membuka
tab Online langsung ke monitoring session tersebut.</p>

<table>
  <tr><th>Badge</th><th>Kondisi</th><th>Aksi klik</th></tr>
  <tr><td><code>● Live</code> (hijau)</td>
      <td>Ada <code>_LIVE_SESSION</code> aktif dengan <code>cfg_id</code> cocok</td>
      <td>Buka tab Online → lanjut ke Step 3 Monitor</td></tr>
  <tr><td><code>○ Offline</code> (abu)</td>
      <td>Tidak ada sesi live untuk cfg_id ini</td>
      <td>Buka pipeline offline seperti biasa</td></tr>
</table>
"""

SEC_5 = """
<h2>5. SeisWork Agent — Spesifikasi</h2>

<p>Agent adalah skrip Python kecil (~300 baris) yang diinstal <strong>sekali</strong> di
server SeisComP. Berjalan sebagai proses daemon, menerima perintah dari SeisWork via REST,
sekaligus mem-forward picks dari message bus SeisComP ke SeisWork.</p>

<h3>5.1 Instalasi</h3>
<pre>
# Di server SeisComP (satu perintah):
bash seiswork-agent/install.sh

# Atau manual:
cp seiswork-agent/agent.py ~/seiswork-agent.py
chmod +x ~/seiswork-agent.py
~/seiswork-agent.py --init          # generate token, simpan ~/.seiswork-agent.conf
~/seiswork-agent.py --start         # jalankan daemon port 7001
</pre>
<p>Token ditampilkan sekali saat <code>--init</code>, disalin ke field "Agent Token" di SeisWork GUI.</p>

<h3>5.2 Endpoint Agent</h3>
<table>
  <tr><th>Method</th><th>Path</th><th>Fungsi</th></tr>
  <tr><td>GET</td><td><code>/status</code></td>
      <td>Output <code>seiscomp status</code>, versi SeisComP, path root, port seedlink</td></tr>
  <tr><td>POST</td><td><code>/push/inventory</code></td>
      <td>Terima file FDSNXML, write ke <code>etc/inventory/&lt;NET&gt;.xml</code>,
          jalankan <code>seiscomp exec scinv sync</code></td></tr>
  <tr><td>POST</td><td><code>/push/bindings</code></td>
      <td>Terima daftar stasiun, generate <code>etc/key/station_NET_STA</code>,
          jalankan <code>seiscomp update-config</code></td></tr>
  <tr><td>POST</td><td><code>/push/config</code></td>
      <td>Terima parameter (STA/LTA threshold, filter, grid), write ke profile SeisComP</td></tr>
  <tr><td>POST</td><td><code>/push/restart</code></td>
      <td>Restart module SeisComP yang ditentukan (seedlink/scautopick/scautoloc)</td></tr>
  <tr><td>POST</td><td><code>/register</code></td>
      <td>Daftarkan URL SeisWork untuk tujuan push picks
          (<code>{"seiswork_url": "http://...", "token": "..."}</code>)</td></tr>
</table>

<h3>5.3 Bridge Picks (Thread terpisah di Agent)</h3>
<pre>
# Berjalan di Python env SeisComP (seiscomp.client tersedia)
def _bridge_thread():
    app = seiscomp.client.Application()
    app.addMessagingSubscription("PICK")
    app.addMessagingSubscription("ORIGIN")
    while running:
        msg = app.messageQueue().pop()
        payload = parse_pick_or_origin(msg)
        requests.post(seiswork_url + "/api/online/trigger", json=payload,
                      headers={"Authorization": "Bearer " + token})
</pre>
<div class="note">
  Bridge thread hanya jalan di env Python SeisComP (bukan env seiswork), karena
  <code>seiscomp.client</code>/<code>seiscomp.io</code> ABI-incompatible dengan
  Python runtime env seiswork (versi berbeda + libseiscomp_core.so berbeda).
  Ini alasan utama Agent dibuat sebagai proses terpisah.
</div>

<h3>5.4 Keamanan</h3>
<ul>
  <li>Semua request ke Agent wajib menyertakan header <code>Authorization: Bearer &lt;token&gt;</code></li>
  <li>Token di-hash (SHA-256) di file konfigurasi Agent — tidak disimpan plain-text</li>
  <li>Agent bind ke <code>0.0.0.0:7001</code> (default) — disarankan firewall hanya izinkan IP SeisWork</li>
  <li>Koneksi HTTPS opsional (self-signed cert) untuk deployment production</li>
</ul>
"""

SEC_6 = """
<h2>6. Endpoint SeisWork untuk Online Mode</h2>

<p>Semua endpoint online sudah ada di <code>seiswork/web/app.py</code>
(Fase 1 selesai), ditambah endpoint baru untuk Fase 2+.</p>

<table>
  <tr><th>Method</th><th>Path</th><th>Status</th><th>Fungsi</th></tr>
  <tr><td>GET</td><td><code>/api/online/detect</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Auto-detect SeisComP (root, port, status)</td></tr>
  <tr><td>POST</td><td><code>/api/online/connect</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Mulai sesi SeedLink (ring buffer numpy)</td></tr>
  <tr><td>POST</td><td><code>/api/online/disconnect</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Hentikan sesi SeedLink</td></tr>
  <tr><td>GET</td><td><code>/api/online/status</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Poll status koneksi + jumlah paket</td></tr>
  <tr><td>GET</td><td><code>/api/online/waveform</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Ambil isi ring buffer per stasiun (downsampled ≤1000 pts)</td></tr>
  <tr><td>POST</td><td><code>/api/online/trigger</code></td>
      <td><span class="badge todo">Fase 2</span></td>
      <td>Terima trigger dari Agent (picks STA/LTA), jalankan pipeline PhaseNet→REAL→ML</td></tr>
  <tr><td>GET</td><td><code>/api/online/stations</code></td>
      <td><span class="badge todo">Fase 2</span></td>
      <td>Parse inventory XML, return daftar NET.STA untuk station selector</td></tr>
  <tr><td>POST</td><td><code>/api/online/sync</code></td>
      <td><span class="badge todo">Fase 3</span></td>
      <td>Push inventory + bindings ke Agent SeisComP</td></tr>
  <tr><td>POST</td><td><code>/api/online/agent/register</code></td>
      <td><span class="badge todo">Fase 3</span></td>
      <td>Simpan host:port:token Agent ke config</td></tr>
  <tr><td>GET</td><td><code>/api/online/events</code></td>
      <td><span class="badge todo">Fase 2</span></td>
      <td>Daftar event real-time (Event Feed) dengan status per tahap pipeline</td></tr>

  <tr><td colspan="4" style="background:#eef3fb;font-weight:bold">Endpoint baru (Juni 2026) — sudah berjalan</td></tr>
  <tr><td>GET</td><td><code>/api/online/waveform/snapshot</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Snapshot awal: segmen kontinyu per stream + <code>last_t</code> + <code>session_epoch</code> (dipakai restore history saat refresh)</td></tr>
  <tr><td>POST</td><td><code>/api/online/waveform/delta</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Delta cursor per-stream berbasis WAKTU DATA (<code>{cursors:{key:last_t}}</code>) → <code>segments_new</code>; perbaikan "waveform putus"</td></tr>
  <tr><td>GET</td><td><code>/api/online/spectrogram/all</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Spectrogram per stream (<code>?window=&amp;keys=</code>), lazy-load baris terlihat</td></tr>
  <tr><td>GET</td><td><code>/api/online/picks/recent</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Pick log real-time (<code>?since=&amp;n=</code>) — sumber otoritatif plot pick (scautopick + PhaseNet)</td></tr>
  <tr><td>GET</td><td><code>/api/online/psd</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Power Spectral Density (Welch via obspy, remove_response bila inventory ada) — modal stasiun</td></tr>
  <tr><td>POST</td><td><code>/api/online/trigger</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Terima pick scautopick dari Agent bridge → <code>add_pick()</code> + <code>add_bridge_pick()</code> (masuk rolling GaMMA)</td></tr>
  <tr><td>POST</td><td><code>/api/online/realtime/start</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Mulai PhaseNet picker + GaMMA associator (+ <code>sds_path</code> auto-detect)</td></tr>
  <tr><td>POST</td><td><code>/api/online/realtime/stop</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Hentikan picker + associator</td></tr>
  <tr><td>GET</td><td><code>/api/online/realtime/status</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Status picker + associator + <code>engine_log</code> (PhaseNet/GaMMA/scautopick)</td></tr>
  <tr><td>GET</td><td><code>/api/online/realtime/events</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Daftar event GaMMA real-time (lat/lon/depth/mag/rms/gap)</td></tr>
  <tr><td>GET</td><td><code>/api/online/realtime/log</code></td>
      <td><span class="badge done">Selesai</span></td>
      <td>Engine log terpadu (<code>?n=</code>) — bukti PhaseNet/GaMMA/scautopick berjalan</td></tr>
</table>
"""

SEC_7 = """
<h2>7. Struktur File — Komponen Online</h2>

<pre>
seiswork/
├── web/
│   ├── _seiscomp_detect.py      ✓ Selesai — auto-detect SeisComP root, port, status seedlink
│   ├── _seedlink_live.py        ✓ Selesai — LiveSeedlinkSession + _RingBuffer numpy
│   ├── app.py                   ✓ Route online/detect|connect|disconnect|status|waveform
│   ├── templates/index.html     ✓ Tab Online/Offline + #sw-online-root shell + Wizard placeholder
│   ├── static/
│   │   ├── js/modules/
│   │   │   └── online-monitor.js  ✓ Selesai — OnlineMonitorModule (OM) modul ke-10
│   │   └── css/seiswork.css     ✓ .sw-online-*, .om-dot-*, .om-panel-*
│   └── (baru Fase 2+):
│       ├── _online_trigger.py   ○ Proses pipeline per trigger (PhaseNet→REAL→ML)
│       └── _online_events.py    ○ Event store real-time + SSE/poll feed

seiswork-agent/                  ○ Fase 3 — direktori baru, agent terpisah
├── agent.py                     ○ Flask mini di env Python SeisComP
├── install.sh                   ○ Installer satu perintah
└── bridge.py                    ○ Thread subscriber message bus SeisComP
</pre>
"""

SEC_8 = """
<h2>8. Roadmap Implementasi</h2>

<table>
  <tr><th>Fase</th><th>Komponen</th><th>Prioritas</th><th>Status</th></tr>

  <tr><td><span class="badge phase">Fase 1</span><br>Integrator Dasar</td>
      <td>Auto-detect SeisComP + SeedLink live + waveform ring buffer +
          Tab Online/Offline + status bar</td>
      <td>Kritis</td>
      <td><span class="badge done">Selesai</span></td></tr>

  <tr><td><span class="badge phase">Fase 2</span><br>Trigger Pipeline</td>
      <td>
        <code>/api/online/trigger</code> endpoint · <code>_online_trigger.py</code> ·
        <code>get_window(t0,t1)</code> di ring buffer ·
        PhaseNet re-pick per-window · REAL asosiasi+lokasi ·
        ML magnitude · Event Feed panel (card + badge tahap) ·
        Badge "● Live / ○ Offline" di job card sidebar
      </td>
      <td>Tinggi</td>
      <td><span class="badge wip">Berikutnya</span></td></tr>

  <tr><td><span class="badge phase">Fase 3</span><br>Wizard + Sync</td>
      <td>
        Wizard 3-step di UI · FDSNWS downloader · Station map (checklist) ·
        Inventory writer (FDSNXML) · Binding key generator ·
        SeisWork Agent (v1) · Push inventory + bindings ke SeisComP ·
        <code>/api/online/sync</code> + <code>/api/online/agent/register</code>
      </td>
      <td>Tinggi</td>
      <td><span class="badge todo">Direncanakan</span></td></tr>

  <tr><td><span class="badge phase">Fase 4</span><br>Focal Mechanism</td>
      <td>SKHASH + FocoNet real-time · Manual-trigger per event card ·
          Polaritas P dari ring buffer · Beachball di Event Feed</td>
      <td>Sedang</td>
      <td><span class="badge todo">Direncanakan</span></td></tr>

  <tr><td><span class="badge phase">Fase 5</span><br>Peta Real-time</td>
      <td>Panel peta online (Leaflet) · Plot event real-time ·
          Station status (warna sinyal/tidak) · Slab2 kontur</td>
      <td>Sedang</td>
      <td><span class="badge todo">Direncanakan</span></td></tr>

  <tr><td><span class="badge phase">Fase 6</span><br>Catalog Live</td>
      <td>Catalog event real-time (tabel + export CSV) ·
          Isolasi dari catalog offline · Filter by time/magnitude ·
          Integrasi dengan Result Viewer offline</td>
      <td>Rendah</td>
      <td><span class="badge todo">Direncanakan</span></td></tr>

  <tr><td><span class="badge phase">Fase 7</span><br>Agent Remote</td>
      <td>SeisWork Agent lengkap (skenario remote) · HTTPS + token ·
          Push config STA/LTA + grid · Restart module SeisComP dari GUI ·
          Multi-server (lebih dari 1 SeisComP)</td>
      <td>Produksi</td>
      <td><span class="badge todo">Direncanakan</span></td></tr>
</table>

<div class="note ok">
  <strong>Urutan prioritas implementasi:</strong> Fase 2 (trigger pipeline) paling
  bernilai segera karena langsung menambah utilitas real-time. Fase 3 (wizard + sync)
  membuat produk siap pakai oleh user baru tanpa perlu setup SeisComP manual.
  Fase 7 (agent remote) menyelesaikan visi arsitektur dua arah penuh.
</div>
"""

SEC_9 = """
<h2>9. Keputusan Arsitektur yang Sudah Ditetapkan</h2>

<table>
  <tr><th>Keputusan</th><th>Pilihan</th><th>Alasan</th></tr>
  <tr><td>Trigger ingestion</td>
      <td>Bridge process terpisah (Agent)</td>
      <td><code>seiscomp.client</code>/<code>seiscomp.io</code> ABI-incompatible
          dengan env <code>seiswork</code> (Python 3.10.12 vs 3.10.20,
          <code>libseiscomp_core.so.18</code> berbeda). Direct import tidak bisa.</td></tr>
  <tr><td>Buffer waveform</td>
      <td>Numpy ring buffer (pre-allocated, overwrite melingkar)</td>
      <td>Sesi live berjalan lama (background thread persisten). Deque/list Python
          tumbuh tak terbatas dan memicu GC. Ring buffer O(1) per write, memory tetap.</td></tr>
  <tr><td>Lokasi real-time</td>
      <td>REAL saja (simultan assoc + grid-search)</td>
      <td>REAL lebih cepat dan tidak butuh travel-time grid pre-computed.
          NLLoc/GrowClust terlalu berat untuk jalur real-time per-trigger.</td></tr>
  <tr><td>Magnitude real-time</td>
      <td>ML WA (Hutton &amp; Boore 1987), PAZ dari inventory XML</td>
      <td>Formula sudah terkalibrasi dan tervalidasi untuk katalog Jailolo.
          Inventory XML (fullia7g.xml) menjadi sumber PAZ yang sama dengan pipeline offline.</td></tr>
  <tr><td>Focal mechanism</td>
      <td>Manual-trigger per event (bukan otomatis)</td>
      <td>Butuh cukup polaritas P bersih multi-stasiun — tidak selalu tersedia
          untuk setiap event kecil. Otomatis akan sering gagal.</td></tr>
  <tr><td>Deployment dua skenario</td>
      <td>Local (localhost) + Remote (Agent via HTTPS)</td>
      <td>Local untuk lab/PC tunggal. Remote untuk deployment produksi di jaringan pemantauan.</td></tr>
</table>
"""

SEC_10 = """
<h2>10. Pembaruan Fungsi Terbaru (Juni 2026)</h2>

<p>Rangkuman fungsi & perbaikan yang ditambahkan setelah Fase 1, semuanya
sudah berjalan di produksi (cfg Palu 47 stasiun IA, seedlink localhost:18000).</p>

<h3>10.1 Protokol Waveform — anti "putus" &amp; padat</h3>
<ul>
  <li><strong>Delta cursor per-stream berbasis waktu data</strong> (bukan jam dinding):
      <code>get_delta(cursors)</code> melanjutkan dari <code>last_t</code> tiap stream,
      meniru <code>_lastRecordTime</code> scrttv. Menghilangkan gap berkala &amp; freeze
      akibat latensi seedlink.</li>
  <li><strong>Envelope min/max per bin</strong> (<code>_segmentize</code>): tiap bin →
      2 nilai (min, max) → garis vertikal per pixel = seismogram PADAT (teknik
      RecordPolyline scrttv), bukan stride decimation yang kehilangan getaran.</li>
  <li><strong>Segmen kontinyu</strong> <code>{t0,step,vs}</code>: gap akuisisi nyata jadi
      segmen terpisah → waktu titik selalu benar.</li>
  <li><strong>Deteksi restart server</strong> via <code>session_epoch</code>; axis dipaku ke
      jam SERVER (offset EMA) bukan jam browser.</li>
  <li><strong>Headroom 2 menit</strong> di kanan + garis merah "now" full-height melintang
      semua baris; <strong>tinggi baris dinamis</strong> (sedikit stasiun → tinggi, banyak →
      pendek lalu scroll); background spectrogram terang.</li>
</ul>

<h3>10.2 Pick Log View &amp; plot pick</h3>
<ul>
  <li><strong>Pick Log</strong>: ring buffer 500 (<code>_pick_log</code>) +
      <code>get_pick_log(since,n)</code>; tab "Events | Pick Log" dengan badge counter.</li>
  <li><strong>Pick store client</strong> (key NET.STA) = sumber otoritatif render pick,
      diisi dari <code>/api/online/picks/recent</code> (poll 2s) — bukan dari delta yang rapuh.
      Dipakai panel utama, modal, dan full-page.</li>
  <li>Pick <strong>scautopick</strong> (garis solid, tag <code>SC</code>, P merah/S biru) vs
      <strong>AI/PhaseNet</strong> (garis dashed, tag <code>AI</code>, P oranye/S ungu) — dibedakan jelas.</li>
  <li><strong>Fix timezone</strong>: <code>_parse_t</code> memaksa UTC (dulu naive→lokal WIB,
      pick PhaseNet meleset 7 jam &amp; tak terplot).</li>
</ul>

<h3>10.3 Mesin Real-Time — PhaseNet → GaMMA → ML</h3>
<ul>
  <li><strong>RealtimePicker</strong> (PhaseNet/seisbench, 1 model, loop semua stasiun) +
      <strong>RealtimeAssociator</strong> (GaMMA self-contained, tanpa grid NLLoc).</li>
  <li><strong>Integrasi scautopick</strong>: <code>add_bridge_pick()</code> meneruskan pick bridge
      ke <code>rolling_picks</code> → bahan asosiasi GaMMA.</li>
  <li><strong>Threshold P&lt;S</strong> (P=0.3, S=0.6): input live vertikal-saja membuat head S
      out-of-distribution (S palsu); P lebih sensitif, S ditekan.</li>
  <li><strong>Akses SDS SeisComP</strong>: <code>_read_sds_trace()</code> via obspy SDS Client;
      magnitude fallback ke SDS saat ring buffer live belum mencakup waktu event.</li>
  <li><strong>Engine log terpadu</strong> (<code>_ENGINE_LOG</code>): satu aliran PhaseNet/GaMMA/
      scautopick dengan tag berwarna — bukti tiap kode berjalan.</li>
</ul>

<h3>10.4 Dashboard, Event &amp; Focal</h3>
<ul>
  <li><strong>Layout baru</strong>: waveform full tinggi (kiri); kanan 3 baris —
      Events+detail, Map+epicenter, Engine &amp; Diagnostics card (status mesin +
      net diag + engine log digabung).</li>
  <li><strong>Card detail event</strong>: ID Event, Waktu, Lat/Lon, Kedalaman, RMS, Gap/Nsta,
      Magnitude + <strong>bola focal</strong> (placeholder, hook <code>_drawBeachball</code> siap SDR).</li>
  <li><strong>Plot epicenter</strong> di map (marker merah ~magnitude, popup, klik → pilih).</li>
  <li><strong>Modal stasiun</strong>: waveform identik panel utama (30 menit + spectrogram) +
      <strong>PSD</strong> obspy; ukuran diperbesar.</li>
</ul>

<h3>10.5 Ketahanan Sesi</h3>
<ul>
  <li><strong>Restore saat refresh</strong>: <code>restoreIfLive()</code> di page load — bila server
      masih punya sesi, dashboard + history (snapshot s/d 31 menit) dipulihkan otomatis,
      tidak mulai dari nol.</li>
  <li><strong>Auto-reconnect</strong>: saat <code>connected:false</code> (server restart), client
      re-<code>/connect</code> pakai param terakhir (cooldown 10s).</li>
  <li><strong>Isolasi cfg_id</strong> &amp; toggle mode Offline/Online (segmented control kanan header).</li>
</ul>

<h3>10.6 Instalasi</h3>
<ul>
  <li><code>seiswork-agent/install_agent.sh</code>: tambah <strong>pymysql</strong> (wajib utk bridge
      DB-poll scautopick saat message bus <code>seiscomp.client</code> gagal di-import).</li>
  <li>Agent = <strong>step 9 opt-in</strong> (bukan default 1-8), dipasang ke Python SeisComP di
      host SeisComP; didokumentasikan di <code>install.sh --help</code>.</li>
</ul>

<div class="note ok">
  <strong>Sumber data:</strong> tampilan live memakai buffer ring numpy di server;
  untuk persistensi waveform event ke disk, sejak Juni 2026 SeisWork mengarsip ke SDS
  secara otomatis (lihat Bab 11). Magnitude/focal membaca SDS bila tersedia.
</div>
"""

# ── Section 11 ─────────────────────────────────────────────────────────────────
SEC_11 = """
<h2>11. Arsip Waveform ke SDS — Keputusan Otomatis &amp; Panel GUI (Juni 2026)</h2>

<p>Agar waveform event tetap bisa dibuka setelah keluar dari ring buffer (RAM),
SeisWork mengarsip data live ke <strong>SDS</strong> (SeisComP Data Structure:
<code>YYYY/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YYYY.DDD</code>). SeisWork sengaja
TIDAK menduplikasi arsip bila SeisComP sudah mengarsip sendiri — keputusan ini
diambil otomatis tiap deteksi real-time dimulai.</p>

<h3>11.1 Dua arsip SDS yang dikenali</h3>
<table>
  <tr><th>&nbsp;</th><th>SDS SeisComP (utama)</th><th>SDS SeisWork (live/fallback)</th></tr>
  <tr><td>Pola path</td><td><code>&lt;root&gt;/var/lib/archive</code></td><td><code>&lt;BASE_DIR&gt;/work/online_sds</code></td></tr>
  <tr><td>Diisi oleh</td><td>scarchive SeisComP (eksternal)</td><td><code>slarchive</code> yang dijalankan SeisWork</td></tr>
  <tr><td>Isi</td><td>arsip historis lengkap</td><td>data sesi live, retensi <code>SDS_RETAIN_DAYS</code> (3 hari)</td></tr>
  <tr><td>Penemuan root</td><td colspan="2">env <code>SEISCOMP_ROOT</code> → <code>config.yaml</code> (<code>locate.locsat.seiscomp_bin</code>) → kandidat <code>~/seiscomp</code>, <code>/opt/seiscomp</code>, <code>/usr/local/seiscomp</code></td></tr>
</table>

<h3>11.2 Alur keputusan otomatis (<code>start_slarchive</code>)</h3>
<ol>
  <li>Deteksi root SeisComP → <code>sds_dir = &lt;root&gt;/var/lib/archive</code>.</li>
  <li>Cek <strong>fresh</strong>: ada file <code>.D</code> termodifikasi &lt; 2 jam terakhir
      (<code>_seiscomp_sds_is_fresh</code>, early-exit → murah walau arsip ratusan GB).</li>
  <li><strong>Jika fresh</strong> → scarchive eksternal dianggap aktif: slarchive <strong>SKIP</strong>
      (hindari duplikasi); SeisWork cukup <em>membaca</em> dari SDS SeisComP.</li>
  <li><strong>Jika tidak fresh / tidak ada</strong> → SeisWork menjalankan <code>slarchive</code>
      sendiri ke <code>work/online_sds</code>.</li>
  <li><strong>Jika deteksi belum jalan</strong> → tidak ada arsip disk; waveform hanya di
      ring buffer (RAM) selama sesi.</li>
</ol>

<h3>11.3 slarchive — selektor stream WAJIB &amp; anti proses-yatim</h3>
<div class="note">
  <strong>Bug kritis yang diperbaiki:</strong> slarchive dijalankan TANPA selektor
  <code>-S</code>. Dalam mode multi-station, server SeedLink tidak mengirim paket apa pun
  tanpa daftar stream → slarchive hanya connect lalu <code>network timeout (600s)</code>
  berulang &amp; <strong>SDS tetap kosong</strong>. Inilah sebab waveform event lama tak pernah terarsip.
</div>
<ul>
  <li><strong>Selektor <code>-S NET_STA,…</code></strong> dibangun dari stasiun aktif sesi
      (<code>_slarchive_selector</code>) lalu diteruskan ke perintah slarchive. Terbukti:
      log berubah jadi <code>Received Data blockette</code> &amp; pohon SDS langsung terbentuk.</li>
  <li><strong>Pembersih proses yatim</strong> (<code>_kill_stray_slarchive</code>): saat server
      Python restart, global <code>_SLARCHIVE_PROC</code> hilang tapi child slarchive tetap hidup
      &amp; menumpuk (rebutan koneksi). Sebelum start, proses slarchive yatim yang menulis ke
      SDS yang sama dimatikan (scan <code>/proc</code>, best-effort lintas-OS).</li>
</ul>
<pre><code>slarchive -v -SDS work/online_sds -x work/slarchive_state.txt -Fi -nd 10 \\
          -S IA_APSI,IA_BBCI,IA_DOCM,... localhost:18000</code></pre>

<h3>11.4 Urutan baca waveform event (<code>event_waveform</code>)</h3>
<ol>
  <li><strong>Ring buffer (RAM)</strong> — untuk event di dalam sesi berjalan.</li>
  <li><strong>SDS SeisComP</strong> — bila ada.</li>
  <li><strong>SDS SeisWork</strong> (<code>work/online_sds</code>) — bila SeisComP SDS kosong/tak ada.</li>
</ol>
<p>Bila ketiganya nihil, pesan diagnosa membedakan <em>"event sebelum sesi live dimulai"</em>
(tak ada di ring buffer) vs <em>"gap arsip SDS"</em> — bukan lagi selalu menyalahkan SDS.</p>

<h3>11.5 Panel GUI "Penyimpanan (SDS)" &amp; endpoint status</h3>
<ul>
  <li><strong>Chip status</strong> di header panel <em>Engine &amp; Diagnostics</em> — label
      mengikuti keputusan otomatis: 🟢 <code>SDS: SeisComP</code> · 🔵 <code>SDS: SeisWork</code> ·
      🟡 <code>SDS: RAM saja</code>. Auto-refresh saat poll mulai.</li>
  <li><strong>Modal</strong> (klik chip): banner arsip aktif + alasan, 2 kartu SDS
      (path, FRESH/STALE, jumlah file, ukuran, hari terakhir, file termutakhir; kartu aktif
      ditandai), langkah keputusan otomatis, dan urutan baca waveform.</li>
  <li><strong>Endpoint</strong> <code>GET /api/online/sds_status</code> → <code>_sds_summary()</code>.
      Untuk arsip besar (SeisComP), statistik berat tidak dihitung; <em>freshness</em> &amp;
      <em>hari terakhir</em> diambil murah lewat cek folder <code>.D</code> / folder tahun terbaru
      agar cap-scan tidak membuat <code>fresh</code>/<code>last_day</code> keliru.</li>
</ul>

<h3>11.6 Perbaikan UI Monitor Online terkait</h3>
<ul>
  <li><strong>Kartu Event jadi tab terpisah</strong>: tab bar <em>Event | List | Pick Log</em> —
      kartu detail tak lagi terpotong (scrollable), daftar event pindah ke tab sendiri;
      proporsi tinggi panel (feed/map/engine) di-balance.</li>
  <li><strong>Peta stasiun</strong>: tambah <code>invalidateSize()</code> + re-<code>fitBounds</code>
      (memperbaiki marker stasiun yang tak tampil karena Leaflet sizing); framing di-zoom rapat;
      garis episenter→stasiun perekam (gaya BotListener) dipertegas.</li>
</ul>

<div class="note info">
  <strong>Cek cepat arsip jalan:</strong>
  <code>ps -eo args | grep slarchive</code> (pastikan ada <code>-S IA_...</code>) ·
  <code>du -sh work/online_sds</code> ·
  <code>tail -f work/slarchive.log</code> (cari <code>Received Data blockette</code>) ·
  atau buka panel <strong>Penyimpanan (SDS)</strong> di GUI.
</div>
"""

# ── Build HTML ─────────────────────────────────────────────────────────────────
def build_html() -> str:
    body = (
        HEADER_HTML + SEC_1 + SEC_2 + SEC_3 + SEC_4 +
        SEC_5 + SEC_6 + SEC_7 + SEC_8 + SEC_9 + SEC_10 + SEC_11
    )
    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<title>SeisWork Online — Arsitektur & Alur Kerja</title>
<style>
{PDF_CSS}
</style>
</head>
<body>
{body}
<hr>
<p style="font-size:7.5pt; color:#aaa; text-align:center; margin-top:10px;">
  SeisWork Online Documentation · by HakimBMKG · {NOW} · Dokumen ini terpisah dari SeisWork_Dokumentasi_Lengkap
</p>
</body>
</html>"""


def main():
    DOCS.mkdir(exist_ok=True)
    html = build_html()

    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"[OK] HTML → {OUT_HTML}")

    try:
        from weasyprint import HTML as WP
        WP(string=html, base_url=str(DOCS)).write_pdf(str(OUT_PDF))
        print(f"[OK] PDF  → {OUT_PDF}")
    except Exception as e:
        print(f"[WARN] PDF gagal: {e}")
        print("       Install weasyprint: pip install weasyprint")


if __name__ == "__main__":
    main()
